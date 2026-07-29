"""MomentSearch — unified API (one service, one port).

Three routers on one FastAPI app (:8000):
  - src/api/videos.py  /api/videos/*  — presigned uploads + registration +
                                        ingest status (Bearer auth)
  - src/api/admin.py   /admin/*       — paper/deck registration + unified
                                        source status (Assignment 3, Bearer
                                        auth on the mutating route)
  - src/api/search.py  public         — / (web UI), /api/ask, /api/config,
                                        local-dev media, /api/health

Heavy processing never happens here — the videos/admin routers only schedule
Prefect flow runs; worker.py (separate process, same image) executes the
ingest pipelines. Every durable byte lives in object storage, Qdrant, or
Postgres, so this process is stateless and disposable.

Run:
    uvicorn src.app:app --port 8000
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import config, db, metrics, security
from .api.admin import router as admin_router
from .api.metrics import router as metrics_router
from .api.search import router as search_router
from .api.videos import router as videos_router
from .rag import vector_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    # Create the Qdrant collection up front (known CLIP dims resolve without
    # loading the model) so a question before the first ingest returns
    # "no moments" instead of a 500. Qdrant being down must not block boot.
    try:
        vector_store.ensure_collection()          # visual (CLIP frames)
        if config.ENABLE_TRANSCRIPT:
            vector_store.ensure_text_collection()  # transcript (bge text)
    except Exception as exc:
        print(f"[startup] Qdrant not ready ({exc!r}) — search degrades to empty results")
    yield


app = FastAPI(title="MomentSearch", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def _auth_middleware(request, call_next):
    """DESIGN.md §3e component 25 — enforce the admin token ahead of routing.

    Registered BEFORE the metrics middleware on purpose. Starlette makes the
    most-recently-added middleware the OUTERMOST, so declaring metrics second
    keeps it wrapped around this one, and a 401 rejected here is still timed
    and counted in the status breakdown rather than vanishing from the
    dashboard.

    Why middleware and not another `Depends`: the inherited
    `videos.py::require_auth` is CLAUDE.md-protected (it fails open on an unset
    token and compares non-constant-time), and a dependency only protects
    routes that remembered to declare it — `GET /api/llm` never did. Enforcing
    here fixes both without touching the protected file, and makes "someone
    adds a route under /admin and forgets the Depends" structurally safe.
    """
    failure = security.auth_failure(
        request.method, request.url.path, request.headers.get("authorization"))
    if failure is not None:
        status, detail = failure
        return JSONResponse({"detail": detail}, status_code=status)
    return await call_next(request)


@app.middleware("http")
async def _metrics_middleware(request, call_next):
    """DESIGN.md §3c component 18: times every request and buckets it by
    ROUTE TEMPLATE, not the raw path — request.scope["route"] is populated
    once Starlette's router has matched (i.e. by the time call_next returns),
    so /api/videos/{video_id} stays one bucket regardless of how many
    distinct video_ids are ever requested.

    call_next() itself is not a reliable place to stop the timer: found live
    (EVIDENCE.md) — for /ask_stream's SSE, call_next() returns as soon as the
    response object exists, well BEFORE the body has actually been sent, so
    timing only around call_next massively under-reported latency for that
    route (observed: 9.8ms recorded for calls that actually took 14-21s).

    Fix: wrap response.body_iterator and record once the real, full body has
    finished draining. Starlette's BaseHTTPMiddleware always hands back an
    internal _StreamingResponse with a body_iterator, for EVERY response —
    JSON handlers included, not just genuine StreamingResponse routes — so
    every request goes through this same wrap-and-record path uniformly (no
    separate "fast path" for plain responses actually exists; verified
    against Starlette's own source, not assumed). The None-check below is a
    defensive fallback for a future/older Starlette shape, not a path this
    codebase's current dependency version ever takes."""
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    # An unmatched path gets a FIXED label, never the raw path. Two ways a
    # request arrives with no route: a 404 for a path no route matches, and
    # (since component 25) a request rejected by the auth middleware before
    # routing ever ran. Both are attacker-controllable, so bucketing them by
    # raw path is an unbounded-cardinality label — a scan or a burst of failed
    # auth would grow the metrics dict without limit. The trade-off, stated
    # plainly: individual unmatched paths are no longer distinguishable in
    # /metrics. Requests that DO match a route are unaffected and still bucket
    # by template, so /api/videos/{video_id} stays one row as before.
    path = route.path if route is not None else "<unmatched>"

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        metrics.record_request(path, response.status_code, (time.perf_counter() - start) * 1000)
        return response

    async def _timed_body():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            metrics.record_request(path, response.status_code, (time.perf_counter() - start) * 1000)

    response.body_iterator = _timed_body()
    return response


app.include_router(videos_router)
app.include_router(admin_router)
app.include_router(search_router)
app.include_router(metrics_router)
