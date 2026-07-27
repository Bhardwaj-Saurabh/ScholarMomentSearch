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
import pytest

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
