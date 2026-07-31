"""Deterministic ordering under score ties — src/rag/search.py.

**Why this file exists, and why it exists LATE.** The tie-break in
`_merge_hits`/`_fuse` was written during the precision@10 investigation and
committed with NO test. spec-guardian flagged it as a CLAUDE.md §2 E2
violation ("evals precede code… must exist AND fail before implementation
begins"), and it was right: this is a change to which citations users get, and
the existing suite passed only because its fixtures happen to have distinct
scores. The tie case — which with Qdrant's hybrid RRF is the COMMON case, at
11 distinct scores per 20 candidates — was entirely uncovered.

What is actually being pinned: given identical inputs, ordering must be
identical, INCLUDING among equal scores. This cannot make precision@10
deterministic on its own (the candidate SET Qdrant returns still varies at its
own limit boundary — measured, disclosed in EVIDENCE.md); it removes this
codebase's own contribution to the drift.
"""
from __future__ import annotations

from src.rag import search as rag_search


def _hits(order):
    """Text hits that are ALL tied on score — only identity distinguishes them."""
    return [{"source_id": s, "kind": "paper", "page": p, "text": t, "score": 0.5}
            for s, p, t in order]


def test_tied_hits_order_identically_regardless_of_input_order():
    a = _hits([("doc_b", 2, "B"), ("doc_a", 1, "A"), ("doc_c", 3, "C")])
    b = _hits([("doc_c", 3, "C"), ("doc_b", 2, "B"), ("doc_a", 1, "A")])
    ka = [(h["source_id"], h["page"]) for h in rag_search._merge_hits([a])]
    kb = [(h["source_id"], h["page"]) for h in rag_search._merge_hits([b])]
    assert ka == kb, f"tied hits ordered differently: {ka} vs {kb}"


def test_score_still_dominates_the_tie_break():
    """The tie-break must never let a lower score outrank a higher one — that
    would be a retrieval-quality regression disguised as determinism."""
    hits = [
        {"source_id": "doc_zzz", "kind": "paper", "page": 1, "text": "low", "score": 0.9},
        {"source_id": "doc_aaa", "kind": "paper", "page": 2, "text": "high", "score": 0.4},
    ]
    out = rag_search._merge_hits([hits])
    assert [h["score"] for h in out] == [0.9, 0.4]


def test_merge_is_repeatable_across_many_calls():
    hits = _hits([(f"doc_{i}", i, f"t{i}") for i in range(12)])
    runs = {tuple((h["source_id"], h["page"]) for h in rag_search._merge_hits([list(hits)]))
            for _ in range(20)}
    assert len(runs) == 1, "repeated _merge_hits calls disagreed on ordering"


def test_fuse_orders_tied_windows_deterministically():
    """`_fuse` sorts windows by rrf; document windows all get their own window,
    and equal rrf values are the norm under rank-quantized RRF."""
    tied = _hits([("doc_b", 2, "B"), ("doc_a", 1, "A"), ("doc_c", 3, "C")])
    runs = set()
    for _ in range(10):
        windows = rag_search._fuse([], list(tied))
        runs.add(tuple((w["text"]["source_id"], w["text"]["page"]) for w in windows))
    assert len(runs) == 1, f"_fuse produced {len(runs)} different orderings for tied input"


def test_score_still_wins_through_the_real_merge_then_fuse_path():
    """`_fuse` scores by RANK, not by raw score — it assumes its input was
    already ordered by `_merge_hits`. Writing this test against `_fuse` alone
    (passing unsorted hits) asserted something the function never promised, so
    it exercises the real composition instead. The precondition is worth
    pinning: if `_merge_hits` ever stopped sorting, `_fuse` would silently rank
    by arrival order."""
    hits = [
        {"source_id": "doc_low", "kind": "paper", "page": 1, "text": "l", "score": 0.1},
        {"source_id": "doc_high", "kind": "paper", "page": 2, "text": "h", "score": 0.9},
    ]
    windows = rag_search._fuse([], rag_search._merge_hits([hits]))
    assert windows[0]["text"]["source_id"] == "doc_high"


def test_tie_break_key_is_stable_for_hits_containing_none():
    """`_hit_key` returns tuples with None entries (a document hit has no
    video_id/idx/ms). The key is stringified for sorting, so None must not make
    it unstable or raise."""
    a = rag_search._hit_key({"source_id": "doc_a", "page": 3})
    b = rag_search._hit_key({"source_id": "doc_a", "page": 3})
    assert str(a) == str(b)
    assert None in a          # the shape this is guarding


# ── Component 59 (DESIGN.md §3m) — same-page chunks must not collapse ────────
# Document payloads carried no chunk ordinal, so every chunk from the same
# page shared one _hit_key and _merge_hits kept only the best-scoring one —
# silently discarding retrieved evidence even with a single sub-query.

def test_two_chunks_from_the_same_page_both_survive_merge():
    a = {"source_id": "doc_a", "kind": "paper", "page": 7, "chunk": 0,
         "text": "the encoder stacks six identical layers", "score": 0.9}
    b = {"source_id": "doc_a", "kind": "paper", "page": 7, "chunk": 1,
         "text": "multi-head attention uses eight heads", "score": 0.8}
    out = rag_search._merge_hits([[a, b]])
    assert len(out) == 2, "distinct chunks on one page are distinct evidence"


def test_identical_chunk_from_two_subqueries_still_dedupes():
    a = {"source_id": "doc_a", "kind": "paper", "page": 7, "chunk": 0,
         "text": "same chunk", "score": 0.7}
    a2 = dict(a, score=0.9)
    out = rag_search._merge_hits([[a], [a2]])
    assert len(out) == 1
    assert out[0]["score"] == 0.9  # best-scoring instance kept
