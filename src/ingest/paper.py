"""Paper (PDF) parsing — page-aware chunks with page numbers as the citation
locator (DESIGN.md component 2). Mirrors frames.py's role in the video flow: it
turns a local file into the units the shared enrich/embed/index stages consume.
Fetching (src/ingest/doc_pipeline.py, component 4) hands this a local path; this
module never touches the network or a database.

A chunk never spans two pages — that's what keeps a citation's `page` honest.
Section headings are detected by relative font size (a line noticeably larger
than the page's own body text) and carried forward until the next heading.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import fitz

# DESIGN.md targets ~500-800 tokens/chunk; no tokenizer dependency here, so this
# approximates with a char budget (~4 chars/token is a stable rule of thumb).
CHUNK_MAX_CHARS = 800 * 4
_HEADING_SIZE_RATIO = 1.15  # a span this much larger than the page's median body size is a heading
_HEADING_MAX_CHARS = 90


@dataclass
class PaperChunk:
    page: int          # 1-indexed — the citation locator
    section: str | None
    text: str


def _page_lines(page: "fitz.Page") -> list[tuple[str, float]]:
    """(text, font_size) for each non-empty line on the page, reading order."""
    lines: list[tuple[str, float]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line.get("spans", [])).strip()
            if not text:
                continue
            size = max((s["size"] for s in line.get("spans", [])), default=0.0)
            lines.append((text, size))
    return lines


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
    font size), so downstream embedding/indexing never has to guess a locator."""
    doc = fitz.open(path)
    try:
        chunks: list[PaperChunk] = []
        current_section: str | None = None
        for i in range(doc.page_count):
            lines = _page_lines(doc[i])
            if not lines:
                continue
            body_size = _median([size for _, size in lines])
            body_lines: list[str] = []
            for text, size in lines:
                if (body_size and size >= body_size * _HEADING_SIZE_RATIO
                        and len(text) <= _HEADING_MAX_CHARS):
                    current_section = text
                    continue
                body_lines.append(text)
            for piece in _split_within_page(" ".join(body_lines)):
                chunks.append(PaperChunk(page=i + 1, section=current_section, text=piece))
        return chunks
    finally:
        doc.close()
