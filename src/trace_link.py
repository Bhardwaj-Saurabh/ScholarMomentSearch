"""Cross-process trace correlation — DESIGN.md §3g component 46.

Ingest happens in two processes: the API accepts a registration and returns
202, then a Prefect worker does the real work seconds later. Traced naively
that is two unrelated traces, and "what happened to the document I just
registered?" still requires correlating by eye.

**Why a Redis side-channel and not a flow parameter.** The obvious approach —
pass `traceparent` into the flow — is blocked twice over:

  * `ingest_video`'s signature lives in `src/ingest/pipeline.py`, which is
    CLAUDE.md-protected. It cannot gain a parameter.
  * Changing `ingest_document`'s parameters would alter an already-registered
    Prefect deployment signature. That is a migration, not an edit.

Keying by document/video id sidesteps both, and works identically for the
protected video path and the unprotected document one.

**Fail-open, like everything else here.** No Redis, a broken Redis, or a
missing key all degrade to "no context" — an UNCORRELATED trace, never a
failed ingest. Losing a trace link is an inconvenience; failing an ingest
because telemetry was unavailable would be a defect.

The context is **consumed on read**: a Prefect retry re-runs the flow, and
re-joining the original request's trace hours later would nest an unrelated run
underneath it.
"""
from __future__ import annotations

from . import cache

# Long enough to survive a queue backlog, short enough that abandoned keys age
# out on their own (documents can sit `pending` behind capacity for a while).
_TTL_S = 24 * 3600


def _key(source_id: str) -> str:
    return f"trace:{source_id}"


def stash(source_id: str, trace_id: str) -> None:
    """Record the trace the WORKER should join for this source. No-ops when
    caching is unavailable."""
    if not trace_id:
        return
    cache.set_bytes(_key(source_id), trace_id.encode(), ttl=_TTL_S)


def pop(source_id: str) -> str | None:
    """The trace id stashed at registration, consumed. None when absent."""
    raw = cache.get_bytes(_key(source_id))
    if not raw:
        return None
    cache.delete(_key(source_id))
    try:
        return raw.decode()
    except Exception:
        return None
