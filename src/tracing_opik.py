"""Opik backend for the tracing facade — DESIGN.md §3g component 44.

Imported lazily by `src/tracing.py` ONLY when `OPIK_API_KEY` is set, so the
`opik` package (26 transitive deps incl. litellm) never has to be installed for
the app to run. An import failure here degrades to "no Opik backend", never to
a broken app.

**Why this buffers instead of streaming.** The first cut created the Opik trace
when the root span opened, attached child spans as they closed, then ended the
trace — and Opik itself warned about it, live:

    Calling Trace.end() shortly after creation with batching enabled may cause
    data loss.

Which is fair: Opik batches asynchronously, and a create-then-immediately-end
pair landing inside one batch window can race. RAG spans are milliseconds
apart, so that window is exactly where this app lives. Instead, every record
for a trace is buffered and the whole thing — trace plus all spans, each with
its REAL start and end timestamp — is submitted once when the root span closes.
One write per request, no update-after-create, nothing for batching to lose.

The buffer is keyed by trace id and popped on root completion, so a long-lived
API process cannot accumulate an entry per request.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from . import config

logger = logging.getLogger(__name__)


def _ts(epoch: float | None) -> _dt.datetime | None:
    """Opik wants timezone-aware datetimes; the facade records epoch floats."""
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)


# Fixed reference instant for deriving an Opik trace id from ours. Opik ids are
# UUIDv7, which embeds a timestamp — so converting with `now()` gives a
# DIFFERENT id each call, and the API process and the worker process (seconds
# apart) would produce two unrelated Opik traces from the same correlation id.
# Verified live: two conversions 1.2s apart were not equal. Pinning the
# reference makes the mapping a pure function of our trace id, so both
# processes land on the same Opik trace and component 46's cross-process
# correlation is one trace rather than two linked ones.
_ID_EPOCH = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)


def _opik_trace_id(our_trace_id: str) -> str | None:
    """Our 32-hex trace id -> a stable UUIDv7 Opik will accept, or None if it
    cannot be converted (then Opik assigns its own and we lose only the
    cross-process merge, not the trace)."""
    try:
        import uuid

        from opik import id_helpers

        return str(id_helpers.uuid4_to_uuid7(_ID_EPOCH, str(uuid.UUID(hex=our_trace_id))))
    except Exception:
        return None


class OpikBackend:
    def __init__(self):
        import opik

        self._client = opik.Opik(
            project_name=config.OPIK_PROJECT_NAME or None,
            workspace=config.OPIK_WORKSPACE or None,
        )
        self._lock = threading.Lock()
        self._buffers: dict[str, list[dict]] = {}

    def start(self, rec: dict) -> None:
        # Nothing is sent on start — see the module docstring.
        pass

    def end(self, rec: dict) -> None:
        tid = rec["trace_id"]
        with self._lock:
            self._buffers.setdefault(tid, []).append(rec)
            if rec["parent"] is not None:
                return                      # not the root yet; keep buffering
            records = self._buffers.pop(tid, [])

        root = rec
        opik_id = _opik_trace_id(tid)
        trace = self._client.trace(
            **({"id": opik_id} if opik_id else {}),
            name=root["name"],
            input=dict(root["attrs"]),
            output={"error": root["error"]} if root["error"] else {},
            start_time=_ts(root["start_ts"]),
            end_time=_ts(root["end_ts"]),
            metadata={"duration_ms": root["duration_ms"], "trace_id": tid},
        )
        # EVERY record is emitted as a span, roots included, and the Opik trace
        # is only a container. The first design let the root BE the trace,
        # which broke cross-process correlation two different ways in live
        # probes: the two processes' roots collided on the shared id, and the
        # second `trace()` call overwrote the first's name and timings — the
        # merged trace came back named `ingest_document` with the API's
        # `register_document` missing entirely. Treating the trace as a
        # container costs one slightly redundant span on single-process traces
        # and makes the multi-process case correct.
        for r in records:
            trace.span(
                name=r["name"],
                input=dict(r["attrs"]),
                output={"error": r["error"]} if r["error"] else {},
                start_time=_ts(r["start_ts"]),
                end_time=_ts(r["end_ts"]),
                metadata={"duration_ms": r["duration_ms"],
                          "parent_span_id": r["parent"]},
            )
