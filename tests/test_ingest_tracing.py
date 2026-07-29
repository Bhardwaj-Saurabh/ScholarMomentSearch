"""Component 46 (DESIGN.md §3g) — ingest tracing + cross-process correlation.

Ingest spans two processes: the API accepts `POST /admin/documents` and returns
202, then a Prefect worker does the real work seconds later. Traced naively
that produces two unrelated traces, and the question you actually want to
answer — "what happened to the document I registered?" — still needs manual
correlation by eye.

The trace context therefore rides through **Redis**, not through the flow's
parameters. Two constraints forced that:

  * `ingest_video`'s signature lives in `src/ingest/pipeline.py`, which is
    CLAUDE.md-protected — a `traceparent` parameter cannot be added to it.
  * Changing `ingest_document`'s parameters would alter a registered Prefect
    deployment signature, which is a migration, not an edit.

A side-channel keyed by document id avoids both. It inherits `src/cache.py`'s
fail-open contract: no Redis, or a missing key, degrades to an UNCORRELATED
trace — never to a failed ingest. Losing a trace link is an inconvenience;
failing an ingest because telemetry was unavailable would be a defect.
"""
from __future__ import annotations

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

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, b"0")) + 1).encode()
        return int(self.store[key])


@pytest.fixture
def redis(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    return fake


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    s = tracing.MemoryBackend()
    tracing.set_backends([s])
    yield s
    tracing.reset()


# ── The side-channel itself ──────────────────────────────────────────────────

def test_trace_context_round_trips_through_redis(redis):
    from src import trace_link

    trace_link.stash("doc_abc", "trace-123")
    assert trace_link.pop("doc_abc") == "trace-123"


def test_missing_context_returns_none_not_an_error(redis):
    from src import trace_link

    assert trace_link.pop("doc_never_registered") is None


def test_context_is_consumed_so_a_retry_does_not_reuse_a_stale_trace(redis):
    """A Prefect retry re-runs the flow. Re-joining the original request's
    trace hours later would nest an unrelated run under it."""
    from src import trace_link

    trace_link.stash("doc_abc", "trace-123")
    assert trace_link.pop("doc_abc") == "trace-123"
    assert trace_link.pop("doc_abc") is None


def test_no_redis_degrades_to_no_context(monkeypatch):
    from src import trace_link

    monkeypatch.setattr(config, "REDIS_URL", "")
    trace_link.stash("doc_abc", "trace-123")      # must not raise
    assert trace_link.pop("doc_abc") is None


def test_broken_redis_never_raises(monkeypatch):
    from src import trace_link

    class _Broken:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise RuntimeError("redis down")
            return _boom

    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")
    monkeypatch.setattr(cache, "_client", lambda: _Broken())
    trace_link.stash("doc_abc", "trace-123")      # must not raise
    assert trace_link.pop("doc_abc") is None


def test_keys_are_namespaced_per_document(redis):
    from src import trace_link

    trace_link.stash("doc_a", "trace-a")
    trace_link.stash("doc_b", "trace-b")
    assert trace_link.pop("doc_a") == "trace-a"
    assert trace_link.pop("doc_b") == "trace-b"


# ── The worker side: spans, and joining the original trace ───────────────────

def test_ingest_flow_emits_a_span_per_stage(sink, redis, monkeypatch):
    from src.ingest import doc_pipeline

    monkeypatch.setattr(doc_pipeline, "t_fetch", lambda *a, **k: "/tmp/x.pdf")
    monkeypatch.setattr(doc_pipeline, "t_parse", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_caption", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_embed_index", lambda *a, **k: 1)
    monkeypatch.setattr(doc_pipeline.db, "bump_document_attempts", lambda d: 1)

    doc_pipeline.ingest_document.fn("doc_abc", "u_test", "paper")
    names = {r["name"] for r in sink.spans}
    assert "ingest_document" in names
    assert {"doc_fetch", "doc_parse", "doc_caption", "doc_embed_index"} <= names


def test_worker_joins_the_trace_the_api_started(sink, redis, monkeypatch):
    """The whole point: registration and indexing are ONE trace."""
    from src import trace_link
    from src.ingest import doc_pipeline

    trace_link.stash("doc_abc", "api-trace-999")
    monkeypatch.setattr(doc_pipeline, "t_fetch", lambda *a, **k: "/tmp/x.pdf")
    monkeypatch.setattr(doc_pipeline, "t_parse", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_caption", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_embed_index", lambda *a, **k: 1)
    monkeypatch.setattr(doc_pipeline.db, "bump_document_attempts", lambda d: 1)

    doc_pipeline.ingest_document.fn("doc_abc", "u_test", "paper")
    assert {r["trace_id"] for r in sink.spans} == {"api-trace-999"}


def test_without_a_stashed_context_the_ingest_still_traces(sink, redis, monkeypatch):
    """Degrades to an uncorrelated trace, never to a failure."""
    from src.ingest import doc_pipeline

    monkeypatch.setattr(doc_pipeline, "t_fetch", lambda *a, **k: "/tmp/x.pdf")
    monkeypatch.setattr(doc_pipeline, "t_parse", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_caption", lambda *a, **k: [{"text": "c"}])
    monkeypatch.setattr(doc_pipeline, "t_embed_index", lambda *a, **k: 1)
    monkeypatch.setattr(doc_pipeline.db, "bump_document_attempts", lambda d: 1)

    doc_pipeline.ingest_document.fn("doc_solo", "u_test", "paper")
    ids = {r["trace_id"] for r in sink.spans}
    assert len(ids) == 1 and "api-trace-999" not in ids


def test_a_failing_stage_is_recorded_and_still_raises(sink, redis, monkeypatch):
    from src.ingest import doc_pipeline

    def _boom(*a, **k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(doc_pipeline, "t_fetch", lambda *a, **k: "/tmp/x.pdf")
    monkeypatch.setattr(doc_pipeline, "t_parse", _boom)
    monkeypatch.setattr(doc_pipeline.db, "bump_document_attempts", lambda d: 1)
    monkeypatch.setattr(doc_pipeline.db, "set_document_status", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        doc_pipeline.ingest_document.fn("doc_abc", "u_test", "paper")
    errored = [r for r in sink.spans if r["error"]]
    assert errored, "a failing ingest stage recorded no error on its span"
