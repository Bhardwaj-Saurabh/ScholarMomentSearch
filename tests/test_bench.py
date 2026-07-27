"""Component 9 (DESIGN.md) — fill benchmark/bench.py's 4 TODOs: labeled-query
recall@10, concurrent-ingest load, throughput, worker-kill resilience.

bench.py is fundamentally a live-stack black-box HTTP client — accept-latency,
search-p95-during-ingest, real throughput, and the docker-kill resilience
check all NEED a running server + worker + Docker, none of which exist in
this test environment (Part 0 was never stood up; see EVIDENCE.md). Rather
than fabricate a "pass" or skip testing entirely, this file unit-tests every
PURE piece of bench.py's new logic — SSE parsing, recall scoring, corpus-URI
cycling — with no network involved. The HTTP-calling glue (measure_recall,
run_concurrent_ingest_load, measure_throughput, run_resilience_check) is
implemented for real but is integration-level and untestable without a live
stack; EVIDENCE.md discloses this explicitly rather than claiming it's green.
"""
from __future__ import annotations

from benchmark import bench


def test_sse_events_parses_multiple_events():
    body = (
        'event: trace\ndata: {"stage": "retrieving"}\n\n'
        'event: citations\ndata: {"citations": [{"n": 1, "kind": "paper"}]}\n\n'
        'event: answer\ndata: {"answer": "hi"}\n\n'
    )
    events = bench._sse_events(body)
    names = [n for n, _ in events]
    assert names == ["trace", "citations", "answer"]
    assert events[1][1]["citations"][0]["kind"] == "paper"


def test_sse_events_handles_empty_body():
    assert bench._sse_events("") == []


def test_citations_from_sse_extracts_citations_event():
    body = (
        'event: trace\ndata: {"stage": "retrieving"}\n\n'
        'event: citations\ndata: {"citations": [{"n": 1, "kind": "video"}, '
        '{"n": 2, "kind": "paper", "locator": {"page": 4}}]}\n\n'
    )
    cites = bench._citations_from_sse(body)
    assert len(cites) == 2
    assert cites[1]["locator"] == {"page": 4}


def test_citations_from_sse_no_citations_event_returns_empty():
    body = 'event: trace\ndata: {"stage": "retrieving"}\n\n'
    assert bench._citations_from_sse(body) == []


def test_score_recall_full_coverage_scores_one():
    labeled = [{"query": "q1", "corpus_id": "attention", "expect_kinds": ["video", "paper"]}]
    by_query = {"q1": [{"kind": "video"}, {"kind": "paper"}, {"kind": "deck"}]}
    assert bench._score_recall(labeled, by_query) == 1.0


def test_score_recall_partial_coverage():
    labeled = [{"query": "q1", "corpus_id": "attention", "expect_kinds": ["video", "paper", "deck"]}]
    by_query = {"q1": [{"kind": "video"}]}
    assert bench._score_recall(labeled, by_query) == 1 / 3


def test_score_recall_averages_across_queries():
    labeled = [
        {"query": "q1", "corpus_id": "a", "expect_kinds": ["video", "paper"]},
        {"query": "q2", "corpus_id": "b", "expect_kinds": ["paper"]},
    ]
    by_query = {
        "q1": [{"kind": "video"}, {"kind": "paper"}],  # 2/2 = 1.0
        "q2": [{"kind": "video"}],                      # 0/1 = 0.0
    }
    assert bench._score_recall(labeled, by_query) == 0.5


def test_score_recall_missing_query_result_scores_zero_for_it():
    labeled = [{"query": "q1", "corpus_id": "a", "expect_kinds": ["paper"]}]
    assert bench._score_recall(labeled, {}) == 0.0


def test_score_recall_empty_labeled_list_is_zero_not_a_crash():
    assert bench._score_recall([], {}) == 0.0


def test_load_corpus_uris_returns_paper_and_deck_per_triplet():
    uris = bench._load_corpus_uris()
    kinds = {u["kind"] for u in uris}
    assert kinds == {"paper", "deck"}
    # 8 triplets in corpus.json -> 16 entries (one paper + one deck each)
    assert len(uris) == 16
    assert all(u["uri"].startswith("http") for u in uris)
    assert all(u.get("title") for u in uris)


def test_cycle_to_n_repeats_and_truncates_correctly():
    uris = [{"uri": "a"}, {"uri": "b"}, {"uri": "c"}]
    out = bench._cycle_to_n(uris, 7)
    assert len(out) == 7
    assert [u["uri"] for u in out] == ["a", "b", "c", "a", "b", "c", "a"]


def test_cycle_to_n_handles_n_smaller_than_list():
    uris = [{"uri": "a"}, {"uri": "b"}, {"uri": "c"}]
    out = bench._cycle_to_n(uris, 2)
    assert [u["uri"] for u in out] == ["a", "b"]
