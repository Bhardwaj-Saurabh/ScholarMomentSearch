"""Grounding hardening (Part 0 finding) — AGENTS.md non-negotiable #5:
"Grounded citations only... Empty retrieval -> empty results, not a fabricated
one." The grounding-auditor agent found two real violations against the live
stack: (1) a query naming a nonexistent "Mamba paper" got a confidently
fabricated answer built on an unrelated real citation instead of abstaining;
(2) a GPT-3 benchmark question got a real, correctly-numbered citation but a
statistic that doesn't match what that citation's text actually says.

Root cause: the LLM never saw WHICH source (video/paper/deck title) each
numbered moment came from — only a bare timestamp/locator and excerpt text —
so it had no structural signal to check a named-source question ("the Mamba
paper") against what's actually cited. src/rag/search.py's citations already
carry a `title` field (used by the UI); this wires it through to the LLM and
strengthens the system prompt's grounding rules to use it.

Pure logic only: _build_moments()/_label() with no network calls (frame
fetch is skipped entirely for citations with no video_id/idx, i.e. any
document citation), and prompt-text guard tests. LLM synthesis quality itself
was verified live against the real stack (see EVIDENCE.md) — that is not
something a unit test can assert on non-deterministic model output.
"""
from __future__ import annotations

from src import llm
from src.rag import search


def test_build_moments_includes_the_source_title():
    """Without a source title, the LLM has no way to check a question naming
    a specific paper/video/deck against what's actually been retrieved —
    this is the root cause the Mamba-paper fabrication traced back to."""
    citations = [
        {"n": 1, "kind": "paper", "title": "Attention Is All You Need",
         "locator": {"page": 6}, "text": "excerpt text"},
        {"n": 2, "kind": "video", "title": "3Blue1Brown — Attention",
         "video_id": "yt_x", "idx": None, "timestamp": "01:00",
         "transcript": "said text"},
    ]
    moments = search._build_moments("default", citations)
    assert moments[0]["source"] == "Attention Is All You Need"
    assert moments[1]["source"] == "3Blue1Brown — Attention"


def test_label_includes_the_source_title():
    line = llm._label(1, {"source": "Attention Is All You Need",
                          "timestamp": "page 6", "transcript": "excerpt"})
    assert "Attention Is All You Need" in line


def test_label_does_not_crash_without_a_source():
    line = llm._label(1, {"timestamp": "00:10", "transcript": "said"})
    assert "[1]" in line


def test_system_prompt_forbids_attributing_content_to_an_uncited_source():
    """Regression guard: the exact failure mode was answering about a named
    source ('the Mamba paper') using a moment from a DIFFERENT, unrelated
    source. Lock in that the prompt explicitly forbids this, so a future
    edit can't silently drop the guardrail without a test failing."""
    assert "never evidence about" in llm.SYSTEM or "not evidence about" in llm.SYSTEM
    assert "source titles" in llm.SYSTEM or "source title" in llm.SYSTEM


def test_system_prompt_requires_genuine_topical_relevance_not_adjacency():
    """Regression guard for the abstain-gate weakness: 'topically related'
    (e.g. both are ML papers) must not count as 'relevant' on its own."""
    assert "topically adjacent" in llm.SYSTEM or "adjacent" in llm.SYSTEM


def test_system_prompt_still_allows_answering_from_a_genuine_partial_match():
    """Don't overcorrect into over-abstaining: a real, on-topic partial match
    must still get answered (this is what protects recall_at_10)."""
    assert "don't refuse" in llm.SYSTEM or "do not refuse" in llm.SYSTEM
