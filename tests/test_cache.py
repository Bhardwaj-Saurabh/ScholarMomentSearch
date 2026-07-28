"""Component 19 (DESIGN.md §3d) — the Redis cache client. `src/cache.py` is the
ONLY module that talks to `redis` directly; every function must fail OPEN
(degrade to a no-op / cache miss) on any Redis error, and caching must be
fully disabled -- never attempted -- when REDIS_URL is unset. Every other
cache-touching component (20-22) trusts these guarantees rather than
re-proving them, so this suite is the one place they get proven.
"""
from __future__ import annotations

import pytest

from src import cache, config


class _FakeClient:
    """In-memory stand-in for redis.Redis -- lets tests exercise real
    get/set/incr/delete round-trips without a live Redis."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def get(self, key):
        self.calls.append(("get", key))
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.calls.append(("set", key, value, ex))
        self.store[key] = value if isinstance(value, bytes) else value.encode()

    def incr(self, key):
        self.calls.append(("incr", key))
        self.store[key] = str(int(self.store.get(key, b"0")) + 1).encode()
        return int(self.store[key])

    def delete(self, key):
        self.calls.append(("delete", key))
        self.store.pop(key, None)


class _RaisingClient:
    """Every call raises, like a Redis that's up but hanging/erroring."""

    def _boom(self, *a, **k):
        import redis
        raise redis.RedisError("simulated failure")

    get = set = incr = delete = _boom


class _CalledIfDisabled:
    """Fails the test if ANY method is invoked -- used to prove disabled()
    short-circuits before ever touching a client."""

    def _fail(self, *a, **k):
        raise AssertionError("client touched while caching disabled")

    get = set = incr = delete = _fail


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # enabled() reads config.REDIS_URL fresh each call -- default to "set"
    # (most tests want caching on); individual tests override as needed.
    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")


def test_enabled_false_when_redis_url_unset(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "")
    assert cache.enabled() is False


def test_enabled_true_when_redis_url_set(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://somewhere:6379/0")
    assert cache.enabled() is True


def test_json_roundtrip(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    cache.set_json("k1", {"a": 1, "b": [1, 2]}, ttl=60)
    assert cache.get_json("k1") == {"a": 1, "b": [1, 2]}


def test_json_miss_returns_none(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    assert cache.get_json("missing") is None


def test_bytes_roundtrip(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    cache.set_bytes("frame:u:v:1", b"\xff\xd8fakejpeg", ttl=3600)
    assert cache.get_bytes("frame:u:v:1") == b"\xff\xd8fakejpeg"


def test_incr_increments(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    assert cache.incr("corpus_version:u1") == 1
    assert cache.incr("corpus_version:u1") == 2


def test_delete_removes_key(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    cache.set_json("k", "v", ttl=60)
    cache.delete("k")
    assert cache.get_json("k") is None


def test_ttl_passed_through_on_set_json(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    cache.set_json("k", "v", ttl=123)
    set_call = next(c for c in fake.calls if c[0] == "set")
    assert set_call[3] == 123  # ex=ttl


def test_ttl_passed_through_on_set_bytes(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    cache.set_bytes("k", b"v", ttl=456)
    set_call = next(c for c in fake.calls if c[0] == "set")
    assert set_call[3] == 456


@pytest.mark.parametrize("op", [
    lambda: cache.get_json("k"),
    lambda: cache.set_json("k", "v", ttl=1),
    lambda: cache.get_bytes("k"),
    lambda: cache.set_bytes("k", b"v", ttl=1),
    lambda: cache.incr("k"),
    lambda: cache.delete("k"),
])
def test_fails_open_on_redis_error(monkeypatch, op):
    """Every function must swallow a Redis error and behave as a no-op /
    miss -- never propagate the exception to the caller."""
    monkeypatch.setattr(cache, "_client", lambda: _RaisingClient())
    op()  # must not raise


@pytest.mark.parametrize("op", [
    lambda: cache.get_json("k"),
    lambda: cache.set_json("k", "v", ttl=1),
    lambda: cache.get_bytes("k"),
    lambda: cache.set_bytes("k", b"v", ttl=1),
    lambda: cache.incr("k"),
    lambda: cache.delete("k"),
])
def test_never_touches_client_when_disabled(monkeypatch, op):
    monkeypatch.setattr(config, "REDIS_URL", "")
    monkeypatch.setattr(cache, "_client", lambda: _CalledIfDisabled())
    op()  # must not raise AssertionError from _CalledIfDisabled either


def test_incr_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(cache, "_client", lambda: _RaisingClient())
    assert cache.incr("k") is None


def test_get_json_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(cache, "_client", lambda: _RaisingClient())
    assert cache.get_json("k") is None


def test_client_built_with_short_socket_timeouts(monkeypatch):
    """A hung-but-reachable Redis must not stall a request -- the client
    itself needs short connect/read timeouts, not just error handling.
    Exercises the REAL singleton builder (not the fake-client monkeypatch
    the other tests use), stubbing only redis.Redis.from_url."""
    import redis

    captured = {}

    def _fake_from_url(url, **kwargs):
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_fake_from_url))
    cache._client.cache_clear()
    try:
        cache._client()
    finally:
        cache._client.cache_clear()  # don't leak the stub client to later tests
    assert captured.get("socket_connect_timeout", 999) <= 1
    assert captured.get("socket_timeout", 999) <= 1
