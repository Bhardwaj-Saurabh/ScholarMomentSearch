"""Completing component 45's span coverage — DESIGN.md §3g.

spec-guardian found component 45 partial against its own spec row: §3g lists
`src/rag/rerank.py`, `src/rag/query_enhance.py` and `src/llm.py`, and none of
them contained a single `tracing` reference. The embed spans "each tagged cache
hit-or-miss", the token/cost attributes and the candidate ids were specified
and never built, and no EVIDENCE entry declared them red.

Cache hit/miss is the attribute that earns its keep here: component 20 added a
Redis embedding cache, and "was this request slow because we recomputed three
embeddings" is otherwise unanswerable from a trace.
"""
from __future__ import annotations

import numpy as np
import pytest

from src import cache, config, tracing


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value if isinstance(value, bytes) else str(value).encode()

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    s = tracing.MemoryBackend()
    tracing.set_backends([s])
    yield s
    tracing.reset()


@pytest.fixture
def redis(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")
    fake = _FakeRedis()   # ONE store for the test — a fresh lambda per call
    monkeypatch.setattr(cache, "_client", lambda: fake)   # would reset it each time


def _by_name(sink):
    return {r["name"]: r for r in sink.spans}


# ── annotate(): attach attributes to the ACTIVE span from deep in the stack ──

def test_annotate_adds_attrs_to_the_innermost_span(sink):
    with tracing.span("outer"):
        with tracing.span("inner"):
            tracing.annotate(tokens=42)
    assert _by_name(sink)["inner"]["attrs"]["tokens"] == 42
    assert "tokens" not in _by_name(sink)["outer"]["attrs"]


def test_annotate_outside_any_span_is_a_noop(sink):
    tracing.annotate(tokens=1)          # must not raise


def test_annotate_is_a_noop_when_tracing_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    tracing.annotate(tokens=1)


# ── Embedding spans with cache hit/miss ──────────────────────────────────────

def test_embed_query_span_reports_a_cache_miss_then_a_hit(sink, redis, monkeypatch):
    from src.rag import embeddings

    monkeypatch.setattr(config, "TEXT_EMBED_PROVIDER", "fastembed")
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    monkeypatch.setattr(embeddings, "embed_query_local",
                        lambda t: np.zeros(4, dtype=np.float32))
    with tracing.span("root"):
        embeddings.embed_query("what is attention?")
    assert _by_name(sink)["embed_query"]["attrs"]["cache"] == "miss"

    sink.spans.clear()
    with tracing.span("root"):
        embeddings.embed_query("what is attention?")
    assert _by_name(sink)["embed_query"]["attrs"]["cache"] == "hit"


def test_embed_text_span_records_the_model(sink, redis, monkeypatch):
    from src.rag import embeddings

    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    monkeypatch.setattr(embeddings, "embed_text_local",
                        lambda t: np.zeros(4, dtype=np.float32))
    with tracing.span("root"):
        embeddings.embed_text("a query")
    span = _by_name(sink)["embed_text"]
    assert span["attrs"]["model"] == config.EMBED_VERSION
    assert span["attrs"]["cache"] in ("hit", "miss")


def test_sparse_embed_span_exists(sink, redis, monkeypatch):
    from src.rag import embeddings

    class _R:
        indices = np.asarray([1, 2], dtype=np.int64)
        values = np.asarray([0.1, 0.2], dtype=np.float32)

    class _M:
        def query_embed(self, texts):
            return iter([_R()])

    monkeypatch.setattr(embeddings, "_sparse_model", lambda: _M())
    with tracing.span("root"):
        embeddings.embed_sparse_query("q")
    assert "embed_sparse" in _by_name(sink)


# ── Rerank span carries the evidence, not just "it ran" ──────────────────────

def test_rerank_span_records_scores_and_candidates(sink, monkeypatch):
    from src.rag import rerank as rr

    class _CE:
        def predict(self, pairs, **kw):
            return [0.9, 0.1]

    monkeypatch.setattr(rr, "_model", lambda: _CE())
    windows = [
        {"video_id": None, "t": 0.0, "rrf": 0.5, "modalities": {"text"},
         "frame": None, "text": {"source_id": "doc_a", "page": 1, "text": "A"}},
        {"video_id": None, "t": 0.0, "rrf": 0.4, "modalities": {"text"},
         "frame": None, "text": {"source_id": "doc_b", "page": 2, "text": "B"}},
    ]
    with tracing.span("root"):
        rr.rerank("q", windows)
    span = _by_name(sink)["rerank_model"]
    assert span["attrs"]["scored"] == 2
    assert span["attrs"]["top_score"] == pytest.approx(0.9)


# ── LLM span carries tokens + cost ───────────────────────────────────────────

def test_llm_usage_annotates_tokens_and_cost_on_the_active_span(sink):
    """Every provider path funnels its token counts through
    `metrics.record_llm_usage`, so that is the seam — no OpenAI client to stub,
    and Anthropic gets the same treatment for free."""
    from src import metrics

    with tracing.span("llm_answer"):
        metrics.record_llm_usage("gpt-4o-mini", 1200, 300, kind="answer")
    attrs = _by_name(sink)["llm_answer"]["attrs"]
    assert attrs["input_tokens"] == 1200
    assert attrs["output_tokens"] == 300
    assert attrs["model"] == "gpt-4o-mini"
    assert attrs["cost_usd"] > 0


def test_llm_usage_outside_a_span_still_records_metrics(sink):
    """Annotation is additive — the counters must not depend on a span."""
    from src import metrics

    metrics.reset()
    metrics.record_llm_usage("gpt-4o-mini", 10, 5, kind="answer")
    assert metrics.snapshot()["input_tokens"] == 10
