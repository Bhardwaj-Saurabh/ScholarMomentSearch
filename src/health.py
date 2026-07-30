"""Component 28 (DESIGN.md §3e) — real dependency health check.

`GET /api/health` reports Postgres and Qdrant reachability instead of a bare
`{"ok": True}` that verified nothing. The HTTP status stays 200 even when a
dependency is down — see `check()` below for why — with the degradation
disclosed in the response body for `fly checks list` / monitoring to read.

Results are cached for `HEALTH_CACHE_TTL_S` seconds (module-level, per
process) so a tight Fly/Docker probe interval can't turn a health check into
load on Postgres or Qdrant.
"""
from __future__ import annotations

import time

from . import config, db
from .rag import vector_store

_cache: dict = {}


def _ping_postgres() -> bool:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return True


def _ping_qdrant() -> bool:
    vector_store.client().get_collections()
    return True


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def check() -> dict:
    """Degraded, not crash: the caller (GET /api/health) always returns 200.

    A stateless api machine that can't reach Postgres or Qdrant can often
    still serve *some* traffic (e.g. cached reads) — failing the HTTP status
    would pull it out of Fly's rotation and turn a degraded dependency into a
    total outage. The body carries the real signal instead.
    """
    now = time.monotonic()
    cached = _cache.get("body")
    ts = _cache.get("ts")
    if cached is not None and ts is not None and (now - ts) < config.HEALTH_CACHE_TTL_S:
        return cached

    postgres_ok = _safe(_ping_postgres)
    qdrant_ok = _safe(_ping_qdrant)
    body = {"ok": postgres_ok and qdrant_ok, "postgres": postgres_ok, "qdrant": qdrant_ok}

    _cache["body"] = body
    _cache["ts"] = now
    return body
