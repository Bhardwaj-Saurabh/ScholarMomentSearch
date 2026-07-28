"""Redis cache client — DESIGN.md §3d, component 19.

The ONLY module that imports `redis`. Every function here fails OPEN: any
Redis error (down, timeout, wrong type, whatever) is caught and degrades to a
cache miss / silent no-op — never raised. Components 20-22 call only these
six functions and trust that guarantee rather than re-proving it themselves,
so "a broken Redis never breaks a request" only has to be true in one place.

REDIS_URL unset -> enabled() is False and every function short-circuits
before ever constructing a client — caching is OFF, not "attempted and
failing" (mirrors CLIP_SERVICE_URL's own unset-means-"run without it" mode).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from . import config

logger = logging.getLogger(__name__)

_warned = False


def enabled() -> bool:
    return bool(config.REDIS_URL)


@lru_cache
def _client():
    import redis

    return redis.Redis.from_url(
        config.REDIS_URL,
        socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT_S,
        socket_timeout=config.REDIS_SOCKET_TIMEOUT_S,
    )


def _warn_once(exc: Exception) -> None:
    global _warned
    if not _warned:
        logger.warning("[cache] Redis unavailable (%r) — degrading to no-cache", exc)
        _warned = True


def get_json(key: str) -> Any | None:
    if not enabled():
        return None
    try:
        raw = _client().get(key)
    except Exception as exc:
        _warn_once(exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_json(key: str, value: Any, ttl: int) -> None:
    if not enabled():
        return
    try:
        _client().set(key, json.dumps(value), ex=ttl)
    except Exception as exc:
        _warn_once(exc)


def get_bytes(key: str) -> bytes | None:
    if not enabled():
        return None
    try:
        return _client().get(key)
    except Exception as exc:
        _warn_once(exc)
        return None


def set_bytes(key: str, value: bytes, ttl: int) -> None:
    if not enabled():
        return
    try:
        _client().set(key, value, ex=ttl)
    except Exception as exc:
        _warn_once(exc)


def incr(key: str) -> int | None:
    """Atomic per-key counter (component 22's per-tenant corpus_version).
    Returns None (not 0) on failure, so a caller can distinguish "couldn't
    reach Redis" from "counter is genuinely zero"."""
    if not enabled():
        return None
    try:
        return _client().incr(key)
    except Exception as exc:
        _warn_once(exc)
        return None


def delete(key: str) -> None:
    if not enabled():
        return
    try:
        _client().delete(key)
    except Exception as exc:
        _warn_once(exc)
