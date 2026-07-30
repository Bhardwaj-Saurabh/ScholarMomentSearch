"""Component 28 (DESIGN.md §3e) — real health check.

`GET /api/health` was a static `{"ok": True}` that nothing actually verified —
a machine could serve 200 with Postgres and Qdrant both unreachable. This
checks both, but keeps the same "degraded, not crash" philosophy as
component 33 (`Qdrant stopped => /api/ask degraded 200, not 500`): the HTTP
status stays 200 even when a dependency is down, because a stateless api
machine that can still serve *some* traffic should stay in Fly's rotation —
pulling it out via a failing health check would turn a degraded incident into
a total outage. The degradation is disclosed in the response BODY instead,
which is what `fly checks list` / monitoring actually reads.

Short-cached (DESIGN.md's own wording) so a 3s Fly probe interval can't hammer
Postgres/Qdrant on every hit.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config, health


@pytest.fixture(autouse=True)
def _reset_cache():
    health._cache.clear()
    yield
    health._cache.clear()


@pytest.fixture()
def client():
    from src.app import app
    return TestClient(app)


def test_health_ok_when_both_dependencies_reachable(monkeypatch):
    monkeypatch.setattr(health, "_ping_postgres", lambda: True)
    monkeypatch.setattr(health, "_ping_qdrant", lambda: True)
    body = health.check()
    assert body == {"ok": True, "postgres": True, "qdrant": True}


def test_health_degraded_when_postgres_down_but_still_200(monkeypatch, client):
    monkeypatch.setattr(health, "_ping_postgres", lambda: False)
    monkeypatch.setattr(health, "_ping_qdrant", lambda: True)
    resp = client.get("/api/health")
    assert resp.status_code == 200          # degraded, not crash
    body = resp.json()
    assert body["ok"] is False
    assert body["postgres"] is False
    assert body["qdrant"] is True


def test_health_degraded_when_qdrant_down(monkeypatch):
    monkeypatch.setattr(health, "_ping_postgres", lambda: True)
    monkeypatch.setattr(health, "_ping_qdrant", lambda: False)
    body = health.check()
    assert body == {"ok": False, "postgres": True, "qdrant": False}


def test_health_never_raises_even_if_a_dependency_check_throws(monkeypatch, client):
    def _boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(health, "_ping_postgres", _boom)
    monkeypatch.setattr(health, "_ping_qdrant", lambda: True)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["postgres"] is False


def test_health_result_is_cached_within_ttl(monkeypatch):
    calls = {"postgres": 0, "qdrant": 0}

    def _pg():
        calls["postgres"] += 1
        return True

    def _qd():
        calls["qdrant"] += 1
        return True

    monkeypatch.setattr(health, "_ping_postgres", _pg)
    monkeypatch.setattr(health, "_ping_qdrant", _qd)

    health.check()
    health.check()
    health.check()

    assert calls["postgres"] == 1, "cached result should skip re-pinging Postgres"
    assert calls["qdrant"] == 1, "cached result should skip re-pinging Qdrant"


def test_health_cache_expires_after_ttl(monkeypatch):
    calls = {"postgres": 0}

    def _pg():
        calls["postgres"] += 1
        return True

    monkeypatch.setattr(health, "_ping_postgres", _pg)
    monkeypatch.setattr(health, "_ping_qdrant", lambda: True)

    health.check()
    assert calls["postgres"] == 1

    # Simulate TTL expiry directly rather than sleeping in a test.
    health._cache["ts"] -= (config.HEALTH_CACHE_TTL_S + 1)
    health.check()
    assert calls["postgres"] == 2, "an expired cache entry must trigger a fresh check"
