"""Grounding hardening, round 2 — the source-title fix (tests/test_llm.py)
closed the 2 originally-reported violations, but a fresh adversarial
re-check found it doesn't generalize: "What numerical rank value does the
CLIP paper recommend for low-rank adaptation?" retrieved ZERO CLIP citations
(all 6 were LoRA content) yet the answer opened with "The CLIP paper
recommends..." — the identical bug, CLIP swapped in for Mamba. Relying on
the model to correctly self-check a named source against what it actually
retrieved is unreliable (it's an abstract cross-referencing task); the more
mechanical, robust fix doesn't depend on the model's compliance at all: a
code-level check that catches "the X paper/deck/talk" naming patterns and
verifies X isn't actually a DIFFERENT, uncited source that exists elsewhere
in this tenant's corpus.

Known, disclosed limitation: this catches the SPECIFIC phrasing pattern all
5 known violations used ("the X paper/deck/talk/video"), not every possible
way to misattribute content — it's a real, mechanical backstop, not a full
faithfulness verifier. It also does not address a structurally different
failure the same re-check found (a false-premise question whose answer
contradicted its own correctly-cited source) — that needs a different kind
of check, not attempted here.

Pure logic only, no network: db.list_source_titles is monkeypatched (a real check
needs live tenant data).
"""
from __future__ import annotations

from src.rag import search


def test_short_name_strips_the_parenthetical_and_colon_suffix():
    assert search._short_name("CLIP (Radford et al. 2021)") == "CLIP"
    assert search._short_name("GPT-3: Language Models are Few-Shot Learners (Brown et al. 2020)") == "GPT-3"
    assert search._short_name("ReAct (Yao et al. 2022)") == "ReAct"


def test_check_named_source_flags_an_uncited_source_named_in_prose(monkeypatch):
    """The exact repro: 0 CLIP citations retrieved (all 6 are LoRA), but the
    answer names 'the CLIP paper' as if it were evidence."""
    monkeypatch.setattr(search.db, "list_source_titles", lambda uid: [
        {"title": "CLIP (Radford et al. 2021)"},
        {"title": "LoRA (Hu et al. 2021)"},
    ])
    citations = [{"title": "LoRA (Hu et al. 2021)"}, {"title": "Stanford CS224n Lecture 11"}]
    answer = ("The CLIP paper recommends a low-rank adaptation value of r "
             "that is significantly smaller than the dimensions [5, 6].")
    result = search._check_named_source_attribution(answer, citations, "default")
    assert result != answer
    assert "CLIP" in result  # names the offending source in the withheld-answer explanation


def test_check_named_source_allows_naming_an_actually_cited_source(monkeypatch):
    monkeypatch.setattr(search.db, "list_source_titles", lambda uid: [
        {"title": "GPT-3: Language Models are Few-Shot Learners (Brown et al. 2020)"},
    ])
    citations = [{"title": "GPT-3: Language Models are Few-Shot Learners (Brown et al. 2020)"}]
    answer = "The GPT-3 paper reports 51.4% accuracy on ARC-Challenge zero-shot [1]."
    result = search._check_named_source_attribution(answer, citations, "default")
    assert result == answer  # unchanged -- GPT-3 genuinely was cited


def test_check_named_source_catches_a_colloquial_short_name_mismatch(monkeypatch):
    """Real repro from a 3rd-round adversarial re-check: the model wrote
    'the chain-of-thought paper' but _short_name() of the real title
    ('Chain-of-Thought Prompting (Wei et al. 2022)') is 'Chain-of-Thought
    Prompting' -- an EXACT match on that derived short name missed this
    real violation entirely (0 CoT citations retrieved, all were RAG
    content, yet the answer confidently described CoT's mechanism). Needs
    substring/prefix matching, not exact equality."""
    monkeypatch.setattr(search.db, "list_source_titles", lambda uid: [
        {"title": "Chain-of-Thought Prompting (Wei et al. 2022)"},
        {"title": "RAG: Retrieval-Augmented Generation (Lewis et al. 2020)"},
    ])
    citations = [{"title": "RAG: Retrieval-Augmented Generation (Lewis et al. 2020)"}]
    answer = ("The chain-of-thought paper combines a dense passage retriever "
             "with a parametric knowledge index [1].")
    result = search._check_named_source_attribution(answer, citations, "default")
    assert result != answer
    assert "Chain-of-Thought" in result


def test_check_named_source_catches_a_descriptive_title_with_no_short_form(monkeypatch):
    """Live repro from grounding-auditor (2026-07-29): asked about "the CLIP
    ICML slide deck"; retrieval returned 6 kind=paper citations (the deck was
    never retrieved), but the answer opened with "The CLIP ICML slide deck
    states that...". _short_name() only reduces a title at a colon/paren
    ("CLIP (Radford et al. 2021)" -> "CLIP"); the real seeded deck title has
    neither ("Official ICML 2021 author slides for the CLIP paper"), so
    _short_name() returns it UNCHANGED — a full descriptive sentence that
    shares no contiguous substring with what the model wrote ("CLIP ICML
    slide"), even though both plainly refer to the same source via the
    shared identity token "CLIP". Pure substring containment misses this."""
    monkeypatch.setattr(search.db, "list_source_titles", lambda uid: [
        {"title": "CLIP (Radford et al. 2021)", "kind": "paper"},
        {"title": "Official ICML 2021 author slides for the CLIP paper", "kind": "deck"},
    ])
    citations = [{"title": "CLIP (Radford et al. 2021)", "kind": "paper"}]  # paper only, deck never retrieved
    answer = ("The CLIP ICML slide deck states that the pretraining dataset "
             "size for WIT is 400 million (image, text) pairs [2, 4].")
    result = search._check_named_source_attribution(answer, citations, "default")
    assert result != answer
    assert "ICML" in result or "slides" in result


def test_check_named_source_ignores_names_that_arent_in_the_corpus_at_all(monkeypatch):
    """Mamba isn't in this tenant's corpus at all (not even uncited) — the
    prompt-level fix already handles this well (verified live); this
    mechanical check shouldn't false-positive on names it can't find either
    way, since there's nothing in list_sources to flag."""
    monkeypatch.setattr(search.db, "list_source_titles", lambda uid: [
        {"title": "LoRA (Hu et al. 2021)"},
    ])
    citations = [{"title": "LoRA (Hu et al. 2021)"}]
    answer = "The Mamba paper does not appear among the retrieved moments."
    result = search._check_named_source_attribution(answer, citations, "default")
    assert result == answer


def test_check_named_source_ignores_plain_prose_with_no_named_source_pattern():
    answer = "Attention lets a transformer relate every token to every other token [1]."
    result = search._check_named_source_attribution(answer, [{"title": "x"}], "default")
    assert result == answer


def test_check_named_source_tolerates_list_sources_failure(monkeypatch):
    def _boom(uid):
        raise RuntimeError("db unreachable")
    monkeypatch.setattr(search.db, "list_sources", _boom)
    answer = "The CLIP paper recommends r=4 [1]."
    result = search._check_named_source_attribution(answer, [{"title": "x"}], "default")
    assert result == answer  # fail open -- don't crash the read path over this
