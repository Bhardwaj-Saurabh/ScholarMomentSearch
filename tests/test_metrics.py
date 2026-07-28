"""Component 18 (DESIGN.md §3c) — live metrics/observability.

In-process, lock-protected counters — pure logic, fully unit-testable without
a live stack. reset() (test-only) clears module-level state between tests so
they don't bleed into each other.
"""
from __future__ import annotations

from src import metrics


def setup_function():
    metrics.reset()


# ── cost estimation ──────────────────────────────────────────────────────────

def test_cost_known_model_computes_from_pricing_table():
    cost = metrics._cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == metrics._PRICING["gpt-4o-mini"][0] + metrics._PRICING["gpt-4o-mini"][1]


def test_cost_unknown_model_falls_back_to_zero_not_a_crash():
    assert metrics._cost("some-self-hosted-vllm-model", 5000, 5000) == 0.0


def test_cost_zero_tokens_is_zero():
    assert metrics._cost("gpt-4o-mini", 0, 0) == 0.0


# ── percentile helper ────────────────────────────────────────────────────────

def test_percentile_empty_list_is_zero():
    assert metrics._percentile([], 0.95) == 0.0


def test_percentile_p50_of_sorted_list():
    assert metrics._percentile([10, 20, 30, 40, 50], 0.50) == 30


def test_percentile_p95_favors_the_high_end():
    values = list(range(1, 101))  # 1..100
    p95 = metrics._percentile(values, 0.95)
    assert p95 >= 95


# ── record_request / snapshot: route bucketing, status counts ──────────────

def test_record_request_buckets_by_route_not_raw_path():
    metrics.record_request("/api/frame/{video_id}/{name}", 200, 12.0)
    metrics.record_request("/api/frame/{video_id}/{name}", 200, 8.0)
    snap = metrics.snapshot()
    routes = {r["route"]: r for r in snap["routes"]}
    assert routes["/api/frame/{video_id}/{name}"]["count"] == 2
    assert snap["requests"] == 2


def test_record_request_tracks_status_counts_including_429():
    metrics.record_request("/api/ask", 200, 10.0)
    metrics.record_request("/api/ask", 429, 5.0)
    metrics.record_request("/api/ask", 429, 5.0)
    snap = metrics.snapshot()
    assert snap["status_counts"][429] == 2
    assert snap["rate_limited"] == 2


def test_snapshot_route_stats_include_avg_p50_p95():
    for ms in [10.0, 20.0, 30.0]:
        metrics.record_request("/ask_stream", 200, ms)
    snap = metrics.snapshot()
    route = next(r for r in snap["routes"] if r["route"] == "/ask_stream")
    assert route["count"] == 3
    assert route["avg"] == 20.0
    assert route["p50"] == 20.0


# ── record_llm_usage: tokens, cost, "LLM answers" only counts real answers ──

def test_record_llm_usage_accumulates_tokens_and_cost():
    metrics.record_llm_usage("gpt-4o-mini", 1000, 500, kind="answer")
    snap = metrics.snapshot()
    assert snap["input_tokens"] == 1000
    assert snap["output_tokens"] == 500
    assert snap["cost_usd"] > 0
    assert snap["llm_answers"] == 1


def test_record_llm_usage_non_answer_kind_does_not_count_as_llm_answer():
    metrics.record_llm_usage("gpt-4o-mini", 100, 50, kind="caption")
    metrics.record_llm_usage("gpt-4o-mini", 100, 50, kind="complete")
    snap = metrics.snapshot()
    assert snap["llm_answers"] == 0
    assert snap["input_tokens"] == 200  # tokens/cost still counted regardless of kind


def test_record_llm_usage_unknown_model_contributes_zero_cost_but_real_tokens():
    metrics.record_llm_usage("self-hosted-llama", 1000, 1000, kind="answer")
    snap = metrics.snapshot()
    assert snap["cost_usd"] == 0.0
    assert snap["input_tokens"] == 1000


# ── record_ask / abstain rate ────────────────────────────────────────────────

def test_record_ask_tracks_abstain_rate():
    metrics.record_ask({"abstained": False})
    metrics.record_ask({"abstained": True})
    metrics.record_ask({"abstained": True})
    snap = metrics.snapshot()
    assert snap["ask_total"] == 3
    assert snap["ask_abstained"] == 2
    assert snap["abstain_rate"] == round(2 / 3, 3)


def test_record_ask_zero_calls_gives_zero_rate_not_a_crash():
    snap = metrics.snapshot()
    assert snap["abstain_rate"] == 0.0
    assert snap["ask_total"] == 0


# ── Prometheus text exposition format ───────────────────────────────────────

def test_prometheus_text_includes_help_type_and_values():
    metrics.record_request("/api/ask", 200, 15.0)
    metrics.record_llm_usage("gpt-4o-mini", 100, 50, kind="answer")
    text = metrics.prometheus_text()
    assert "# HELP" in text
    assert "# TYPE" in text
    assert "momentsearch_requests_total 1" in text
    assert "momentsearch_llm_answers_total 1" in text


def test_prometheus_text_never_crashes_on_empty_state():
    text = metrics.prometheus_text()
    assert "momentsearch_requests_total 0" in text
