"""Tracing facade — DESIGN.md §3g component 44.

The business code calls ONLY this module. Nothing in `src/rag/` or
`src/ingest/` imports Opik or OpenTelemetry, for two reasons:

  1. `opik` (2.2.11) pulls no OpenTelemetry packages — they are two independent
     SDKs with different models. Instrumenting the pipeline twice would double
     the edit surface and let the two drift apart.
  2. Backends are optional and swappable. `with span("rerank")` should be the
     same line whether it exports to Opik, to an OTLP collector, to both, or to
     nothing at all.

Three guarantees this module owes everyone else:

**No backend configured ⇒ genuine no-op.** Same convention as `REDIS_URL`,
`AUTH0_*` and `CLIP_SERVICE_URL`: an unconfigured stack behaves exactly as it
did before tracing existed.

**Fails open, always.** Every backend call is wrapped. A backend that is down,
slow, or raising must never surface in a user-facing response. Telemetry that
can break the product is worse than no telemetry — and this sits on a path
whose `accept_latency_p95_ms` SLA is already red.

**Never changes control flow.** A span records an exception and re-raises it
unchanged; it does not swallow, retry, or transform.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager

from . import config

logger = logging.getLogger(__name__)

# Per-thread span stack, so nesting works under the thread-pool that Starlette
# dispatches sync route handlers onto (up to 40 concurrent).
_local = threading.local()
_BACKENDS: list = []
_init_lock = threading.Lock()
_initialized = False
_warned = False


def _warn_once(exc: Exception) -> None:
    global _warned
    if not _warned:
        logger.warning("[tracing] backend error (%r) — continuing untraced", exc)
        _warned = True


class MemoryBackend:
    """In-memory sink. Used by tests so assertions are about THIS module's
    logic rather than a vendor SDK's, and handy for local debugging."""

    def __init__(self):
        self.spans: list[dict] = []

    def start(self, rec: dict) -> None:
        pass

    def end(self, rec: dict) -> None:
        self.spans.append(rec)


class _Span:
    """Handle yielded by `span()`. Exists even when tracing is disabled so
    callers never need `if tracing.enabled():` guards."""

    __slots__ = ("rec", "_live")

    def __init__(self, rec: dict | None):
        self.rec = rec
        self._live = rec is not None

    def set_attrs(self, **attrs) -> None:
        if not self._live:
            return
        for k, v in attrs.items():
            # Attribute values come from real data — chunk text, numpy scalars,
            # arbitrary objects. An unserializable one degrades to a marker
            # rather than taking down the request.
            try:
                self.rec["attrs"][k] = v if isinstance(
                    v, (str, int, float, bool, type(None), list, dict)) else str(v)
            except Exception:
                self.rec["attrs"][k] = "<unrepresentable>"

    def record_error(self, exc: BaseException) -> None:
        if not self._live:
            return
        try:
            self.rec["error"] = f"{type(exc).__name__}: {exc}"
        except Exception:
            self.rec["error"] = "<unrepresentable error>"


def enabled() -> bool:
    return bool(config.OPIK_API_KEY or config.OTEL_EXPORTER_OTLP_ENDPOINT)


def reset() -> None:
    """Drop backends and cached init state (tests, and re-reading config)."""
    global _BACKENDS, _initialized, _warned
    with _init_lock:
        _BACKENDS = []
        _initialized = False
        _warned = False
    _local.stack = []


def set_backends(backends: list) -> None:
    """Install backends explicitly, bypassing config-driven construction.

    A real injection point rather than reaching into a private global: tests
    need a deterministic in-memory sink, and lazy rebuild-from-config would
    otherwise clobber anything assigned directly.
    """
    global _BACKENDS, _initialized
    with _init_lock:
        _BACKENDS = list(backends)
        _initialized = True


def exported() -> list[dict]:
    """Spans captured by any MemoryBackend — test/debug helper."""
    out: list[dict] = []
    for b in _BACKENDS:
        out.extend(getattr(b, "spans", []))
    return out


def _build_backends() -> list:
    backends: list = []
    if config.OPIK_API_KEY:
        try:
            from .tracing_opik import OpikBackend

            backends.append(OpikBackend())
        except Exception as exc:
            _warn_once(exc)
    if config.OTEL_EXPORTER_OTLP_ENDPOINT:
        try:
            from .tracing_otel import OtelBackend

            backends.append(OtelBackend())
        except Exception as exc:
            _warn_once(exc)
    return backends


def _backends() -> list:
    global _BACKENDS, _initialized
    if _initialized:
        return _BACKENDS
    with _init_lock:
        if not _initialized:
            _BACKENDS = _build_backends()
            _initialized = True
    return _BACKENDS


def _stack() -> list:
    if not hasattr(_local, "stack"):
        _local.stack = []
    return _local.stack


def current_trace_id() -> str | None:
    """Trace id of the active span, for cross-process correlation
    (component 46) and for stamping onto responses."""
    st = _stack()
    return st[-1]["trace_id"] if st else None


@contextmanager
def span(name: str, *, trace_id: str | None = None, **attrs):
    """Record `name` as a span. Yields a handle; always safe to call."""
    backends = _backends() if enabled() else []
    if not backends:
        yield _Span(None)
        return

    st = _stack()
    parent = st[-1] if st else None
    rec = {
        "id": uuid.uuid4().hex,
        # An explicit trace_id adopts an existing trace (component 46's
        # cross-process correlation); otherwise inherit the parent's, or start
        # a new trace at the root.
        "trace_id": (parent["trace_id"] if parent else (trace_id or uuid.uuid4().hex)),
        "parent": parent["id"] if parent else None,
        "name": name,
        "attrs": dict(attrs),
        "error": None,
        "duration_ms": 0.0,
        # Absolute wall-clock bounds, so a backend can submit a span with its
        # real start AND end in ONE call rather than create-then-update.
        "start_ts": None,
        "end_ts": None,
    }
    handle = _Span(rec)
    for b in backends:
        try:
            b.start(rec)
        except Exception as exc:
            _warn_once(exc)

    st.append(rec)
    rec["start_ts"] = time.time()
    t0 = time.perf_counter()
    try:
        yield handle
    except BaseException as exc:
        # Record, then re-raise UNCHANGED. Observability observes; it must
        # never alter the caller's control flow.
        handle.record_error(exc)
        raise
    finally:
        rec["duration_ms"] = (time.perf_counter() - t0) * 1000
        rec["end_ts"] = time.time()
        st.pop()
        for b in backends:
            try:
                b.end(rec)
            except Exception as exc:
                _warn_once(exc)
