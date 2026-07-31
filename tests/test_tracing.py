"""Component 44 (DESIGN.md §3g) — the tracing facade.

Every other observability component calls ONLY this module, so the guarantees
have to hold here or they hold nowhere:

  * **No backend configured ⇒ genuine no-op.** Traces are optional
    infrastructure; an unconfigured stack must behave exactly as it did before
    tracing existed (same convention as REDIS_URL / AUTH0_* / CLIP_SERVICE_URL).
  * **Fails open, always.** A backend that is down, slow, or raising must never
    surface in a response. Telemetry that can break the product is worse than
    no telemetry — and this runs on a path whose `accept_latency_p95_ms` SLA is
    already red.
  * **One instrumentation, many backends.** `opik` (2.2.11) pulls no
    OpenTelemetry, so Opik and OTel are two independent SDKs. The business code
    must import neither — it calls `span()` and this module fans out.
"""
from __future__ import annotations

import pytest

from src import config, tracing


@pytest.fixture(autouse=True)
def _reset():
    tracing.reset()
    yield
    tracing.reset()


# ── Disabled by default ──────────────────────────────────────────────────────

def test_disabled_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    assert tracing.enabled() is False


def test_span_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    with tracing.span("retrieve", question="hello") as s:
        s.set_attrs(hits=5)
        s.record_error(ValueError("boom"))
    # Nothing raised, nothing recorded.
    assert tracing.exported() == []


def test_disabled_span_still_yields_a_usable_handle(monkeypatch):
    """Callers must not need `if tracing.enabled():` guards everywhere — the
    handle has to support the full API even when it does nothing."""
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    tracing.reset()
    with tracing.span("x") as s:
        assert s is not None
        s.set_attrs(a=1)


# ── Recording, when a backend IS configured ──────────────────────────────────

@pytest.fixture
def recording(monkeypatch):
    """Enable tracing with an in-memory backend so assertions are about OUR
    logic, not a vendor SDK's."""
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    monkeypatch.setattr(config, "OPIK_WORKSPACE", "test-ws")
    tracing.reset()
    sink = tracing.MemoryBackend()
    tracing.set_backends([sink])
    return sink


def test_records_a_span_with_attributes(recording):
    with tracing.span("retrieve", question="what is attention?") as s:
        s.set_attrs(candidates=20, abstained=False)
    (rec,) = recording.spans
    assert rec["name"] == "retrieve"
    assert rec["attrs"]["question"] == "what is attention?"
    assert rec["attrs"]["candidates"] == 20
    assert rec["attrs"]["abstained"] is False
    assert rec["duration_ms"] >= 0


def test_spans_nest_to_form_a_trace(recording):
    with tracing.span("ask"):
        with tracing.span("retrieve"):
            with tracing.span("rerank"):
                pass
        with tracing.span("llm_answer"):
            pass
    by_name = {s["name"]: s for s in recording.spans}
    assert by_name["retrieve"]["parent"] == by_name["ask"]["id"]
    assert by_name["rerank"]["parent"] == by_name["retrieve"]["id"]
    assert by_name["llm_answer"]["parent"] == by_name["ask"]["id"]
    assert by_name["ask"]["parent"] is None
    # One trace: every span shares the root's trace id.
    assert len({s["trace_id"] for s in recording.spans}) == 1


def test_exception_is_recorded_and_re_raised(recording):
    """A span must not swallow the caller's error — observability observes,
    it does not change control flow."""
    with pytest.raises(ValueError):
        with tracing.span("embed"):
            raise ValueError("model unavailable")
    (rec,) = recording.spans
    assert rec["error"] is not None
    assert "model unavailable" in rec["error"]


# ── Fail-open: the property everything else depends on ───────────────────────

def test_backend_that_raises_on_export_never_reaches_the_caller(monkeypatch):
    class _Exploding:
        def start(self, *a, **k): raise RuntimeError("backend down")
        def end(self, *a, **k): raise RuntimeError("backend down")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([_Exploding()])
    with tracing.span("retrieve") as s:      # must not raise
        s.set_attrs(x=1)


def test_one_broken_backend_does_not_stop_the_other(monkeypatch):
    class _Exploding:
        def start(self, *a, **k): raise RuntimeError("down")
        def end(self, *a, **k): raise RuntimeError("down")

    good = tracing.MemoryBackend()
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([_Exploding(), good])
    with tracing.span("retrieve"):
        pass
    assert len(good.spans) == 1, "a failing backend suppressed a healthy one"


def test_bad_attribute_values_do_not_break_the_span(recording):
    """Attributes come from real data (chunk text, numpy scalars, objects).
    An unserializable value must degrade, not explode."""
    class _Weird:
        def __repr__(self): raise RuntimeError("even repr fails")

    with tracing.span("fuse") as s:
        s.set_attrs(ok=1, weird=_Weird())
    assert recording.spans[0]["attrs"]["ok"] == 1


def test_span_never_swallows_time_even_on_backend_failure(monkeypatch):
    class _EndOnly:
        def start(self, *a, **k): return None
        def end(self, *a, **k): raise RuntimeError("export failed")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([_EndOnly()])
    with tracing.span("x"):
        pass          # the failure in end() must be swallowed


# ── record(): completed spans from outside our control ──────────────────────

def test_record_emits_a_completed_span(recording):
    """Used by the video-ingest Prefect hook, where the work happens inside a
    CLAUDE.md-protected file and can only be observed after the fact."""
    tracing.record("ingest_video", start_ts=1000.0, end_ts=1002.5,
                   video_id="yt_x", state="COMPLETED")
    (rec,) = recording.spans
    assert rec["name"] == "ingest_video"
    assert rec["duration_ms"] == 2500.0
    assert rec["attrs"]["video_id"] == "yt_x"
    assert rec["parent"] is None


def test_record_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    tracing.record("ingest_video", start_ts=1.0, end_ts=2.0)   # must not raise
    assert tracing.exported() == []


def test_record_never_raises_on_a_broken_backend(monkeypatch):
    class _Exploding:
        def start(self, *a, **k): raise RuntimeError("down")
        def end(self, *a, **k): raise RuntimeError("down")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([_Exploding()])
    tracing.record("ingest_video", start_ts=1.0, end_ts=2.0)


def test_record_clamps_a_negative_duration(recording):
    """Prefect hook times come from two different clocks; a skewed pair must
    not produce a negative duration in the dashboard."""
    tracing.record("ingest_video", start_ts=100.0, end_ts=90.0)
    assert recording.spans[0]["duration_ms"] == 0.0


def test_warm_is_safe_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    tracing.warm()
    assert tracing.enabled() is False


# ── Component 54 (DESIGN.md §3l) — span-stack propagation across threads ────
# `_stack()` is threading.local() ON PURPOSE, so concurrent requests never
# cross-contaminate spans. But that means a worker thread spawned MID-request
# (to run two branch searches concurrently) starts with an empty stack —
# span() there would see no parent and mint a disconnected new trace_id.
# current_stack()/adopt_stack() let a caller propagate its own span context
# into a thread it spawns.

def test_current_stack_snapshots_the_active_span(recording):
    with tracing.span("retrieve") as sp:
        snap = tracing.current_stack()
    assert len(snap) == 1
    assert snap[0]["id"] == sp.rec["id"]


def test_current_stack_is_empty_outside_any_span(recording):
    assert tracing.current_stack() == []


def test_adopt_stack_makes_a_new_thread_nest_under_the_caller(recording):
    import threading

    results = {}

    def worker(stack):
        tracing.adopt_stack(stack)
        with tracing.span("search_visual") as sp:
            results["id"] = sp.rec["id"]
            results["trace_id"] = sp.rec["trace_id"]
            results["parent"] = sp.rec["parent"]

    with tracing.span("retrieve") as parent_sp:
        snap = tracing.current_stack()
        t = threading.Thread(target=worker, args=(snap,))
        t.start()
        t.join()

    assert results["parent"] == parent_sp.rec["id"]
    assert results["trace_id"] == parent_sp.rec["trace_id"]


def test_without_adopt_stack_a_new_thread_would_start_a_disconnected_trace(recording):
    """Regression lock for the bug this component exists to prevent: proves
    the failure mode is real, not hypothetical, absent the fix."""
    import threading

    results = {}

    def worker():
        with tracing.span("search_visual") as sp:
            results["parent"] = sp.rec["parent"]
            results["trace_id"] = sp.rec["trace_id"]

    with tracing.span("retrieve") as parent_sp:
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert results["parent"] is None  # no adopt_stack() -> orphaned span
    assert results["trace_id"] != parent_sp.rec["trace_id"]


def test_adopt_stack_does_not_leak_into_a_pooled_threads_next_task(recording):
    """Thread pools reuse OS threads across tasks. A stack adopted for one
    submission must not silently apply to the next thing that thread runs."""
    import threading

    with tracing.span("retrieve") as parent_sp:
        snap = tracing.current_stack()

    def adopt_and_check():
        tracing.adopt_stack(snap)
        assert tracing.current_stack() != []

    def check_clean():
        assert tracing.current_stack() == []

    t1 = threading.Thread(target=adopt_and_check)
    t1.start()
    t1.join()
    # A DIFFERENT thread must never see thread 1's adopted stack.
    t2 = threading.Thread(target=check_clean)
    t2.start()
    t2.join()
