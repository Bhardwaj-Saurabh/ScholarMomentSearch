"""Component 2 (DESIGN.md) — paper PDF -> page-aware chunks (src/ingest/paper.py).

Pure parsing/chunking over a local PDF file: no network, no DB, no Qdrant (those
are component 4's job). Fixture PDFs are built on the fly with PyMuPDF in tmp_path
and never committed — no media files in this repo (CLAUDE.md hygiene rule).

SLA relevance: this component has no direct network/queue surface, so no
benchmark/sla.json row applies to it standalone. It feeds ingestion throughput and
cross-source recall@10 once wired into the document flow (components 4 and 7).
"""
from __future__ import annotations

import fitz

from src.ingest import paper

BODY = 10.0
HEAD = 16.0


def _build_pdf(path, pages: list[list[tuple[str, float]]]):
    """pages: list of pages; each page is [(text, fontsize), ...] lines, top to
    bottom, each placed on its own line via insert_text (no auto-wrap tricks —
    insert_textbox silently drops text that overflows its rect in this PyMuPDF
    version, so lines are placed explicitly for deterministic fixtures)."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for text, size in lines:
            page.insert_text((72, y), text, fontsize=size)
            y += size + 4
    doc.save(str(path))
    doc.close()
    return path


def test_single_short_page_is_one_chunk(tmp_path):
    pdf = _build_pdf(tmp_path / "one.pdf", [
        [("This paper studies attention mechanisms in sequence models.", BODY)],
    ])
    chunks = paper.parse_pdf(pdf)
    assert len(chunks) == 1
    assert chunks[0].page == 1
    assert "attention mechanisms" in chunks[0].text


def test_page_number_tracks_each_page_and_never_crosses(tmp_path):
    pdf = _build_pdf(tmp_path / "three.pdf", [
        [("MARKERONE unique body text for the first page.", BODY)],
        [("MARKERTWO unique body text for the second page.", BODY)],
        [("MARKERTHREE unique body text for the third page.", BODY)],
    ])
    chunks = paper.parse_pdf(pdf)
    assert any("MARKERONE" in c.text and c.page == 1 for c in chunks)
    assert any("MARKERTWO" in c.text and c.page == 2 for c in chunks)
    assert any("MARKERTHREE" in c.text and c.page == 3 for c in chunks)
    for c in chunks:  # no chunk mixes markers from two different pages
        markers = ("MARKERONE", "MARKERTWO", "MARKERTHREE")
        assert sum(m in c.text for m in markers) <= 1


def test_heading_detected_by_font_size_and_carried_forward(tmp_path):
    pdf = _build_pdf(tmp_path / "sections.pdf", [
        [("Abstract", HEAD), ("This is the abstract body text of the paper.", BODY)],
        [("2 Method", HEAD), ("This page describes the method in detail.", BODY)],
        [("More method details continue here on a third page.", BODY)],  # no new heading
    ])
    chunks = paper.parse_pdf(pdf)
    by_page = {c.page: c for c in chunks}
    assert by_page[1].section == "Abstract"
    assert by_page[2].section == "2 Method"
    assert by_page[3].section == "2 Method"  # carries forward with no new heading


def test_heading_text_itself_excluded_from_body(tmp_path):
    pdf = _build_pdf(tmp_path / "heading_excl.pdf", [
        [("Introduction", HEAD), ("Body text that follows the heading.", BODY)],
    ])
    chunks = paper.parse_pdf(pdf)
    assert all("Introduction" not in c.text for c in chunks)
    assert any("Body text that follows" in c.text for c in chunks)


def test_long_page_splits_into_multiple_chunks_same_page(tmp_path):
    sentence = "Attention mechanisms let models weigh relevant context in sequences. "
    lines = [(sentence, BODY)] * 48  # ~3.3k chars on one page > the ~800-token budget
    pdf = _build_pdf(tmp_path / "long.pdf", [lines])
    chunks = paper.parse_pdf(pdf)
    assert len(chunks) > 1
    assert all(c.page == 1 for c in chunks)


def test_blank_page_produces_no_chunks_and_does_not_crash(tmp_path):
    pdf = _build_pdf(tmp_path / "blank.pdf", [
        [("Some real content on page one.", BODY)],
        [],  # blank page
        [("Real content again on page three.", BODY)],
    ])
    chunks = paper.parse_pdf(pdf)
    pages_seen = {c.page for c in chunks}
    assert 2 not in pages_seen
    assert {1, 3} <= pages_seen


# ── Component 14 (DESIGN.md §3a): table & figure extraction ──────────────────

def _build_table_pdf(path):
    """A page with an intro line, then a real ruling-line table (find_tables'
    default strategy needs actual vector lines — it doesn't fire on ordinary
    paragraph text, so this is a faithful 'real table' fixture, not a false
    positive risk for the plain-prose tests above)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Table 1 summarizes model results below.", fontsize=BODY)
    xs = [72, 172, 272, 372]
    ys = [110, 140, 170, 200]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    rows = [["Model", "Accuracy", "F1"], ["BERT", "92.1", "91.4"], ["GPT-3", "94.6", "93.9"]]
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            page.insert_text((xs[c] + 5, ys[r] + 20), val, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def test_table_becomes_its_own_structured_chunk(tmp_path):
    pdf = _build_table_pdf(tmp_path / "table.pdf")
    chunks = paper.parse_pdf(pdf)
    table_chunks = [c for c in chunks if c.section == "Table"]
    assert len(table_chunks) == 1
    text = table_chunks[0].text
    assert table_chunks[0].page == 1
    # row/column structure survives (cells of one row stay grouped together)
    assert "Model | Accuracy | F1" in text
    assert "BERT | 92.1 | 91.4" in text
    assert "GPT-3 | 94.6 | 93.9" in text


def test_table_cell_text_excluded_from_ordinary_prose_chunk(tmp_path):
    pdf = _build_table_pdf(tmp_path / "table2.pdf")
    chunks = paper.parse_pdf(pdf)
    prose_chunks = [c for c in chunks if c.section != "Table"]
    assert any("summarizes model results" in c.text for c in prose_chunks)
    # the table's own cell values never leak into the flattened prose chunk
    assert all("92.1" not in c.text for c in prose_chunks)


def _make_jpeg_bytes(size, color):
    from io import BytesIO

    from PIL import Image
    im = Image.new("RGB", size, color=color)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_large_embedded_image_becomes_a_caption_ready_chunk(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Figure 1 shows the model architecture.", fontsize=BODY)
    page.insert_image(fitz.Rect(72, 100, 472, 400), stream=_make_jpeg_bytes((400, 300), "white"))
    pdf = tmp_path / "figure.pdf"
    doc.save(str(pdf))
    doc.close()

    chunks = paper.parse_pdf(pdf)
    figure_chunks = [c for c in chunks if c.needs_caption]
    assert len(figure_chunks) == 1
    assert figure_chunks[0].page == 1
    assert figure_chunks[0].image_jpeg  # non-empty bytes, ready for llm.caption_image


def test_small_embedded_image_is_ignored_as_a_logo_not_a_figure(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Body text with a tiny logo icon nearby.", fontsize=BODY)
    page.insert_image(fitz.Rect(500, 700, 520, 720), stream=_make_jpeg_bytes((20, 20), "black"))
    pdf = tmp_path / "logo.pdf"
    doc.save(str(pdf))
    doc.close()

    chunks = paper.parse_pdf(pdf)
    assert not any(c.needs_caption for c in chunks)
