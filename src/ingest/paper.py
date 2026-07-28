"""Paper (PDF) parsing — page-aware chunks with page numbers as the citation
locator (DESIGN.md component 2). Mirrors frames.py's role in the video flow: it
turns a local file into the units the shared enrich/embed/index stages consume.
Fetching (src/ingest/doc_pipeline.py, component 4) hands this a local path; this
module never touches the network or a database.

A chunk never spans two pages — that's what keeps a citation's `page` honest.
Section headings are detected by relative font size (a line noticeably larger
than the page's own body text) and carried forward until the next heading.

DESIGN.md §3a component 14: tables and embedded figures used to be invisible —
`page.get_text()` alone flattens a table into jumbled prose and drops images
entirely. A real ruling-line table (`page.find_tables()`'s default strategy;
it does not fire on ordinary paragraph text) becomes its own chunk with
row/column structure preserved, and its cell text is excluded from the
ordinary prose scan so it isn't duplicated as noise. A substantive embedded
image (not a tiny logo/icon) gets flagged `needs_caption`, mirroring
deck.py's slide-captioning path — the SAME doc-caption stage in
doc_pipeline.py already captions any chunk shaped that way, kind-agnostic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image

# DESIGN.md targets ~500-800 tokens/chunk; no tokenizer dependency here, so this
# approximates with a char budget (~4 chars/token is a stable rule of thumb).
CHUNK_MAX_CHARS = 800 * 4
_HEADING_SIZE_RATIO = 1.15  # a span this much larger than the page's median body size is a heading
_HEADING_MAX_CHARS = 90
_FIGURE_MIN_AREA = 120 * 120  # px^2 at PDF native scale — skips small logos/icons
_JPEG_QUALITY = 85


@dataclass
class PaperChunk:
    page: int          # 1-indexed — the citation locator
    section: str | None
    text: str
    needs_caption: bool = False
    image_jpeg: bytes | None = None


def _page_lines(page: "fitz.Page") -> list[tuple[str, float, "fitz.Rect"]]:
    """(text, font_size, bbox) for each non-empty line on the page, reading order."""
    lines: list[tuple[str, float, "fitz.Rect"]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line.get("spans", [])).strip()
            if not text:
                continue
            size = max((s["size"] for s in line.get("spans", [])), default=0.0)
            lines.append((text, size, fitz.Rect(line["bbox"])))
    return lines


def _overlap_area(a: "fitz.Rect", b: "fitz.Rect") -> float:
    x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
    x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _line_in_table(bbox: "fitz.Rect", table_bboxes: list["fitz.Rect"]) -> bool:
    area = max(bbox.get_area(), 1e-6)
    return any(_overlap_area(bbox, tb) >= 0.5 * area for tb in table_bboxes)


def _extract_tables(page: "fitz.Page") -> tuple[list[str], list["fitz.Rect"]]:
    """Real ruling-line tables -> one text blob per table, cells joined
    ' | ' within a row and rows newline-joined, so the structure survives as
    embeddable text instead of being flattened into surrounding prose.
    Best-effort: any failure here just means no table chunk, never a crash."""
    try:
        found = page.find_tables()
    except Exception:
        return [], []
    texts: list[str] = []
    bboxes: list[fitz.Rect] = []
    for table in found.tables:
        try:
            rows = table.extract()
        except Exception:
            continue
        lines = [" | ".join((cell or "").strip() for cell in row) for row in rows]
        lines = [ln for ln in lines if ln.strip(" |")]
        if not lines:
            continue
        texts.append("Table: " + "\n".join(lines))
        bboxes.append(fitz.Rect(table.bbox))
    return texts, bboxes


def _to_jpeg_bytes(image_bytes: bytes) -> bytes:
    im = Image.open(BytesIO(image_bytes)).convert("RGB")
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


def _extract_figures(doc: "fitz.Document", page: "fitz.Page", page_num: int) -> list["PaperChunk"]:
    """Embedded images large enough to be an actual figure/diagram (not a
    tiny logo/icon) are flagged needs_caption — the existing doc-caption
    stage (doc_pipeline.py's t_caption, kind-agnostic) vision-captions them
    exactly like a text-thin deck slide, before embedding."""
    chunks: list[PaperChunk] = []
    seen: set[int] = set()
    for img in page.get_images(full=True):
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)
        rects = page.get_image_rects(xref)
        if not rects or rects[0].width * rects[0].height < _FIGURE_MIN_AREA:
            continue
        try:
            image_jpeg = _to_jpeg_bytes(doc.extract_image(xref)["image"])
        except Exception:
            continue
        chunks.append(PaperChunk(page=page_num, section=None, text="",
                                 needs_caption=True, image_jpeg=image_jpeg))
    return chunks


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _split_within_page(text: str) -> list[str]:
    """Bound-size chunks within ONE page's text — never merges across pages."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_MAX_CHARS:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > CHUNK_MAX_CHARS:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def parse_pdf(path: Path) -> list[PaperChunk]:
    """PDF -> page-aware chunks. Each chunk carries the 1-indexed page it came
    from and the section heading active at that point (best-effort, via relative
    font size), so downstream embedding/indexing never has to guess a locator.

    Tables and substantive embedded figures (component 14) are pulled out as
    their own chunks before the ordinary prose scan runs, so a table's cells
    are never duplicated as jumbled prose."""
    doc = fitz.open(path)
    try:
        chunks: list[PaperChunk] = []
        current_section: str | None = None
        for i in range(doc.page_count):
            page = doc[i]
            table_texts, table_bboxes = _extract_tables(page)
            raw_lines = _page_lines(page)
            lines = [(t, s, b) for t, s, b in raw_lines if not _line_in_table(b, table_bboxes)]
            if lines:
                body_size = _median([size for _, size, _ in lines])
                body_lines: list[str] = []
                for text, size, _bbox in lines:
                    if (body_size and size >= body_size * _HEADING_SIZE_RATIO
                            and len(text) <= _HEADING_MAX_CHARS):
                        current_section = text
                        continue
                    body_lines.append(text)
                for piece in _split_within_page(" ".join(body_lines)):
                    chunks.append(PaperChunk(page=i + 1, section=current_section, text=piece))
            for table_text in table_texts:
                chunks.append(PaperChunk(page=i + 1, section="Table", text=table_text))
            chunks.extend(_extract_figures(doc, page, i + 1))
        return chunks
    finally:
        doc.close()
