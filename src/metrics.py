"""Live metrics / observability — DESIGN.md §3c component 18.

Not part of the assignment's grading (eval/rubric.json, benchmark/sla.json
don't gate on this) — an operator-facing addition. In-process, lock-protected
counters, reset on process restart: this is a LIVE dashboard (3s auto-refresh
in the UI), not persisted analytics, so no new DB table for ephemeral
request/token counters.

Three things get tracked here:
  1. HTTP requests — per ROUTE TEMPLATE (never the raw path: a per-video_id
     route would explode into one row per id), latency, and status code.
  2. LLM token usage + an estimated cost from a static per-model pricing
     table — a best-effort estimate, not live pricing-API data. An
     unrecognized model (a tenant's own self-hosted vLLM/Ollama endpoint via
     base_url) has no real per-token bill, so it falls back to $0 rather than
     guessing.
  3. Grounding: how often ask() abstains vs. answers.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

_lock = threading.Lock()

_requests: dict[str, list[float]] = defaultdict(list)  # route -> [latency_ms, ...]
_status_counts: dict[int, int] = defaultdict(int)
_llm_answers = 0
_input_tokens = 0
_output_tokens = 0
_cost_usd = 0.0
_ask_total = 0
_ask_abstained = 0

# $ per 1M tokens (input, output). Best-effort, hand-maintained — update as
# published pricing changes. Unrecognized model -> (0.0, 0.0): no invented cost.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
}


def reset() -> None:
    """Test-only: clear all module-level state between tests."""
    global _llm_answers, _input_tokens, _output_tokens, _cost_usd
    global _ask_total, _ask_abstained
    with _lock:
        _requests.clear()
        _status_counts.clear()
        _llm_answers = 0
        _input_tokens = 0
        _output_tokens = 0
        _cost_usd = 0.0
        _ask_total = 0
        _ask_abstained = 0


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _PRICING.get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return s[idx]


def record_request(route: str, status_code: int, latency_ms: float) -> None:
    with _lock:
        _requests[route].append(latency_ms)
        _status_counts[status_code] += 1


def record_llm_usage(model: str, input_tokens: int, output_tokens: int, *, kind: str) -> None:
    """Also annotates the ACTIVE span (component 45): all four provider paths
    already funnel through here with the token counts, so hooking this one
    function gives every LLM span its tokens and cost without duplicating the
    logic per provider — and without src/llm.py needing to know tracing
    exists."""
    from . import tracing

    cost = _cost(model, input_tokens, output_tokens)
    tracing.annotate(model=model, kind=kind, input_tokens=input_tokens,
                     output_tokens=output_tokens, cost_usd=round(cost, 6))
    _record_llm_usage_impl(model, input_tokens, output_tokens, kind=kind)


def _record_llm_usage_impl(model: str, input_tokens: int, output_tokens: int,
                           *, kind: str) -> None:
    """kind: "answer" | "caption" | "complete" | "ping" — only "answer" counts
    toward the "LLM answers" stat; every kind's tokens/cost still accumulate,
    since it's all real LLM API spend regardless of which call site made it."""
    global _llm_answers, _input_tokens, _output_tokens, _cost_usd
    with _lock:
        _input_tokens += input_tokens
        _output_tokens += output_tokens
        _cost_usd += _cost(model, input_tokens, output_tokens)
        if kind == "answer":
            _llm_answers += 1


def record_ask(result: dict[str, Any]) -> None:
    global _ask_total, _ask_abstained
    with _lock:
        _ask_total += 1
        if result.get("abstained"):
            _ask_abstained += 1


def snapshot() -> dict[str, Any]:
    with _lock:
        routes = []
        for route, samples in _requests.items():
            routes.append({
                "route": route,
                "count": len(samples),
                "avg": round(sum(samples) / len(samples), 1) if samples else 0.0,
                "p50": round(_percentile(samples, 0.50), 1),
                "p95": round(_percentile(samples, 0.95), 1),
            })
        routes.sort(key=lambda r: r["count"], reverse=True)
        total_requests = sum(len(s) for s in _requests.values())
        return {
            "cost_usd": round(_cost_usd, 4),
            "input_tokens": _input_tokens,
            "output_tokens": _output_tokens,
            "llm_answers": _llm_answers,
            "requests": total_requests,
            "rate_limited": _status_counts.get(429, 0),
            "routes": routes,
            "status_counts": dict(_status_counts),
            "ask_total": _ask_total,
            "ask_abstained": _ask_abstained,
            "abstain_rate": round((_ask_abstained / _ask_total) if _ask_total else 0.0, 3),
        }


def prometheus_text() -> str:
    """Prometheus text exposition format. Hand-written rather than pulling in
    the prometheus_client dependency — the format is simple enough (HELP/TYPE
    once per metric name, then value lines) that a small new dependency isn't
    worth it for this scope."""
    snap = snapshot()
    lines = [
        "# HELP momentsearch_requests_total Total HTTP requests handled.",
        "# TYPE momentsearch_requests_total counter",
        f"momentsearch_requests_total {snap['requests']}",
        "# HELP momentsearch_llm_cost_usd Estimated cumulative LLM cost in USD.",
        "# TYPE momentsearch_llm_cost_usd counter",
        f"momentsearch_llm_cost_usd {snap['cost_usd']}",
        "# HELP momentsearch_llm_input_tokens_total Cumulative LLM input tokens.",
        "# TYPE momentsearch_llm_input_tokens_total counter",
        f"momentsearch_llm_input_tokens_total {snap['input_tokens']}",
        "# HELP momentsearch_llm_output_tokens_total Cumulative LLM output tokens.",
        "# TYPE momentsearch_llm_output_tokens_total counter",
        f"momentsearch_llm_output_tokens_total {snap['output_tokens']}",
        "# HELP momentsearch_llm_answers_total Total LLM-synthesized answers.",
        "# TYPE momentsearch_llm_answers_total counter",
        f"momentsearch_llm_answers_total {snap['llm_answers']}",
        "# HELP momentsearch_ask_abstain_rate Fraction of ask() calls that abstained.",
        "# TYPE momentsearch_ask_abstain_rate gauge",
        f"momentsearch_ask_abstain_rate {snap['abstain_rate']}",
    ]
    for code, count in sorted(snap["status_counts"].items()):
        lines.append(f'momentsearch_http_responses_total{{status="{code}"}} {count}')
    for r in snap["routes"]:
        label = r["route"].replace('"', "")
        lines.append(f'momentsearch_route_requests_total{{route="{label}"}} {r["count"]}')
        lines.append(f'momentsearch_route_latency_ms_avg{{route="{label}"}} {r["avg"]}')
    return "\n".join(lines) + "\n"
