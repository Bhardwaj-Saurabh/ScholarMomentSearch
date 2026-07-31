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


# ── Component 12 (DESIGN.md §3a): precision@10 — the complement recall never
# checks (kind-presence alone gives full credit to an off-topic citation of
# the expected kind). ─────────────────────────────────────────────────────────

def test_seed_corpus_id_map_covers_every_triplets_paper_deck_and_video():
    mapping = bench._seed_corpus_id_map()
    assert mapping["doc_seed_attention_paper"] == "attention"
    assert mapping["doc_seed_attention_deck"] == "attention"
    # 8 triplets * (paper + deck) + a yt_<id> entry per triplet with a video_url
    assert sum(1 for v in mapping.values() if True) >= 16 + 8


def test_score_precision_all_on_topic_scores_one():
    labeled = [{"query": "q1", "corpus_id": "attention"}]
    by_query = {"q1": [{"source_id": "doc_seed_attention_paper"},
                       {"video_id": "yt_abc"}]}
    id_to_corpus = {"doc_seed_attention_paper": "attention", "yt_abc": "attention"}
    assert bench._score_precision(labeled, by_query, id_to_corpus) == 1.0


def test_score_precision_penalizes_off_topic_noise():
    labeled = [{"query": "q1", "corpus_id": "attention"}]
    by_query = {"q1": [{"source_id": "doc_seed_attention_paper"},   # on-topic
                       {"source_id": "doc_seed_lora_deck"},        # off-topic
                       {"video_id": "yt_unrelated"}]}              # off-topic
    id_to_corpus = {"doc_seed_attention_paper": "attention",
                    "doc_seed_lora_deck": "lora"}
    assert bench._score_precision(labeled, by_query, id_to_corpus) == 1 / 3


def test_score_precision_unresolvable_citation_counts_as_off_topic():
    """A citation whose id isn't in the seed map at all (e.g. a user's own
    self-serve upload sharing the default tenant) is off-topic noise for this
    diagnostic, not a crash or a free pass."""
    labeled = [{"query": "q1", "corpus_id": "attention"}]
    by_query = {"q1": [{"source_id": "doc_user_upload_123"}]}
    assert bench._score_precision(labeled, by_query, {}) == 0.0


def test_score_precision_empty_labeled_list_is_zero_not_a_crash():
    assert bench._score_precision([], {}, {}) == 0.0


def test_score_precision_no_citations_for_a_query_skips_it_not_zero_drag():
    """Recall already penalizes zero-citation queries; precision (a
    NOISE-among-what-was-returned measure) shouldn't double-count that as a
    precision failure too — it just contributes nothing to the average."""
    labeled = [
        {"query": "q1", "corpus_id": "attention"},
        {"query": "q2", "corpus_id": "bert"},
    ]
    by_query = {"q1": [], "q2": [{"source_id": "doc_seed_bert_paper"}]}
    id_to_corpus = {"doc_seed_bert_paper": "bert"}
    assert bench._score_precision(labeled, by_query, id_to_corpus) == 1.0


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


# ── Part-0 finding: throughput/load/resilience must not dedup-shadow the
# already-seeded corpus (see EVIDENCE.md). ────────────────────────────────────

def test_req_sets_x_user_id_header_when_user_given(monkeypatch):
    """Repro for the dedup-shadowing bug: _req() must be ABLE to scope a
    request to a non-default tenant, or every corpus URI submitted by the
    benchmark collides with what component 10 already seeded under
    user_id='default' and gets marked 'skipped' — never 'indexed' — no
    matter how fast real ingest is."""
    captured = {}

    class _FakeResp:
        status = 202
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=30):
        captured["headers"] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setattr(bench.urllib.request, "urlopen", _fake_urlopen)
    bench._req("POST", "/admin/documents", body={"x": 1}, user="bench-throughput-abc123")
    assert captured["headers"].get("X-user-id") == "bench-throughput-abc123"


def test_req_omits_x_user_id_header_when_no_user_given(monkeypatch):
    captured = {}

    class _FakeResp:
        status = 200
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=30):
        captured["headers"] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setattr(bench.urllib.request, "urlopen", _fake_urlopen)
    bench._req("GET", "/admin/sources")
    assert "X-user-id" not in captured["headers"]


def test_fresh_bench_tenant_is_unique_per_call_and_labeled():
    a = bench._fresh_bench_tenant("throughput")
    b = bench._fresh_bench_tenant("throughput")
    assert a != b  # two runs (or two calls) must never collide with each other
    assert a.startswith("bench-throughput-")
    assert "default" not in a  # must never accidentally land on the seeded tenant


# ── Component 34 (DESIGN.md §3e) — bench.py cleans up its own test documents ─
# Without this, every measurement that creates fresh-tenant documents left
# them permanently in Postgres; the reconciler retried the un-fetchable ones
# (accept-latency's fake example.com URLs) forever, each retry adding a fresh
# Prefect scheduled run. That ongoing leak — not one-time debris — was the
# actual mechanism behind ingest_throughput_chunks_per_s reading 0.0
# (EVIDENCE.md 2026-07-31).

def test_delete_documents_calls_the_delete_route_for_every_id(monkeypatch):
    calls = []

    def _fake_req(method, path, body=None, token=None, timeout=30, user=None):
        calls.append((method, path, user))
        return 200, "{}", 1.0

    monkeypatch.setattr(bench, "_req", _fake_req)
    bench._delete_documents(["doc_a", "doc_b"], user="bench-test-1")

    assert sorted(calls) == [
        ("DELETE", "/admin/documents/doc_a", "bench-test-1"),
        ("DELETE", "/admin/documents/doc_b", "bench-test-1"),
    ]


def test_delete_documents_empty_list_makes_no_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(bench, "_req", lambda *a, **k: calls.append(1) or (200, "{}", 1.0))
    bench._delete_documents([])
    assert calls == []


def test_delete_documents_one_failure_does_not_stop_the_rest(monkeypatch):
    """`_req` already swallows HTTP-level failures (returns status 0 rather
    than raising) — this just confirms _delete_documents doesn't need its own
    try/except to get that guarantee, and that one bad id can't short-circuit
    cleanup of the others."""
    calls = []

    def _fake_req(method, path, body=None, token=None, timeout=30, user=None):
        calls.append(path)
        if "doc_a" in path:
            return 0, "connection reset", 1.0
        return 200, "{}", 1.0

    monkeypatch.setattr(bench, "_req", _fake_req)
    bench._delete_documents(["doc_a", "doc_b", "doc_c"])  # must not raise

    assert len(calls) == 3
