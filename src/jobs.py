"""Prefect Cloud trigger layer — the API schedules runs, workers execute them.

One flow ("ms-ingest-video" — the "ms-" prefix keeps it distinct from the
digital-twin-akash flow living in the same Prefect workspace), one deployment
("ingest", registered by worker.py's flow.serve()). The API never imports the
pipeline or its heavy deps (torch, ffmpeg) — it just asks Prefect Cloud to
schedule a run; any live worker picks it up. Retries/backoff live on the
flow's tasks (src/ingest/pipeline.py); failed runs are visible + retryable in
the Prefect Cloud UI.

Component 52 (DESIGN.md §3k): `run_deployment(name=...)` re-resolves the
deployment name to a UUID via a live Prefect Cloud round trip on EVERY call —
outside a flow/task context (exactly the API request-handler's situation)
Prefect's own client-reuse has nothing to attach to, so each call also opens a
fresh client. That lookup is pure waste: the deployment id never changes for
the process's lifetime. Measured in isolation (EVIDENCE.md 2026-07-31): ~299ms
for the lookup alone, on top of ~341ms for the actual flow-run creation — one
full avoidable round trip on every `/admin/documents` and `/admin/videos`
accept. `_deployment_id()` resolves each name once and caches it; dispatch
goes through `_create_flow_run_from_deployment()` against that cached id. If
the cached id turns out stale (deployment re-registered under a new id after
this process started), the create call fails once, the cache entry is
dropped, and `run_deployment()` — which re-resolves fresh — is used as a
one-time fallback rather than crashing the accept.
"""
from __future__ import annotations

import threading

from prefect.deployments import run_deployment

INGEST_DEPLOYMENT = "ms-ingest-video/ingest"
DOCUMENT_DEPLOYMENT = "ms-ingest-document/ingest"

_deployment_id_cache: dict[str, str] = {}

# Component 56 (DESIGN.md §3m): component 52 cached the deployment UUID but
# still constructed a brand-new Prefect client — a fresh TCP+TLS handshake to
# the US-hosted Prefect Cloud — on every dispatch. One long-lived client per
# process instead; httpx pools/reuses the underlying connection. If a dispatch
# fails (network blip, dead keep-alive), _reset_client() drops it so the
# fallback path — and every call after — starts clean.
_client = None
_client_lock = threading.Lock()


def _get_sync_client():
    global _client
    with _client_lock:
        if _client is None:
            from prefect.client.orchestration import get_client
            c = get_client(sync_client=True)
            c.__enter__()  # open the transport once; lives for the process
            _client = c
        return _client


def _reset_client() -> None:
    global _client
    with _client_lock:
        c, _client = _client, None
    if c is not None:
        try:
            c.__exit__(None, None, None)
        except Exception:
            pass


def _deployment_id(name: str) -> str:
    cached = _deployment_id_cache.get(name)
    if cached:
        return cached
    dep = _get_sync_client().read_deployment_by_name(name)
    resolved = str(dep.id)
    _deployment_id_cache[name] = resolved
    return resolved


def _create_flow_run_from_deployment(deployment_id: str, *, parameters: dict, name: str):
    return _get_sync_client().create_flow_run_from_deployment(
        deployment_id, parameters=parameters, name=name)


def _dispatch(deployment_name: str, parameters: dict, flow_run_name: str) -> str:
    deployment_id = _deployment_id(deployment_name)
    try:
        flow_run = _create_flow_run_from_deployment(
            deployment_id, parameters=parameters, name=flow_run_name)
    except Exception:
        # Either the cached id was stale (deployment re-registered) or the
        # long-lived client's connection died — drop both and fall back to a
        # full by-name resolve on a fresh client instead of failing the accept.
        _deployment_id_cache.pop(deployment_name, None)
        _reset_client()
        flow_run = run_deployment(
            name=deployment_name, parameters=parameters, timeout=0,
            flow_run_name=flow_run_name)
    return str(flow_run.id)


def enqueue_video(video_id: str, user_id: str) -> str:
    """Schedule the ingest flow for one video. Returns the Prefect flow-run id."""
    return _dispatch(INGEST_DEPLOYMENT,
                      {"video_id": video_id, "user_id": user_id},
                      f"ingest-{video_id}")


def enqueue_document(doc_id: str, user_id: str, kind: str) -> str:
    """Schedule the document ingest flow (paper or deck). Documents ride FIFO —
    enqueued immediately at registration, not through the WFQ claim table
    (DESIGN.md component 5's documented alternative to unifying the dispatcher).
    Returns the Prefect flow-run id."""
    return _dispatch(DOCUMENT_DEPLOYMENT,
                      {"doc_id": doc_id, "user_id": user_id, "kind": kind},
                      f"ingest-{doc_id}")
