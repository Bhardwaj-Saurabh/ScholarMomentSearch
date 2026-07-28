"""Component 18 (DESIGN.md §3c) — GET /metrics (Prometheus) and
GET /admin/metrics (JSON), plus the request-timing middleware that feeds
both. Uses FastAPI's TestClient (see tests/test_admin_api.py's own docstring
for why this doubles as the contract-probe layer without a live stack).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db, metrics
from src.config import ADMIN_TOKEN

AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()
    metrics.reset()


@pytest.fixture
def client():
    from src.app import app
    return TestClient(app)


def test_metrics_prometheus_requires_auth(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_prometheus_returns_text_with_auth(client):
    resp = client.get("/metrics", headers=AUTH)
    assert resp.status_code == 200
    assert "# HELP" in resp.text
    assert "momentsearch_requests_total" in resp.text


def test_admin_metrics_requires_auth(client):
    assert client.get("/admin/metrics").status_code == 401


def test_admin_metrics_returns_json_with_auth(client):
    resp = client.get("/admin/metrics", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("cost_usd", "input_tokens", "output_tokens", "llm_answers",
               "requests", "rate_limited", "routes", "status_counts",
               "abstain_rate", "queue"):
        assert key in body


def test_middleware_buckets_by_route_template_not_raw_path(client):
    client.get("/api/videos/does-not-exist-1")
    client.get("/api/videos/does-not-exist-2")
    client.get("/api/videos/does-not-exist-3")
    resp = client.get("/admin/metrics", headers=AUTH)
    routes = {r["route"]: r["count"] for r in resp.json()["routes"]}
    assert routes.get("/api/videos/{video_id}") == 3  # one bucket, not three


def test_middleware_tracks_status_codes():
    from src.app import app
    c = TestClient(app)
    c.get("/api/videos/does-not-exist")  # 404
    resp = c.get("/admin/metrics", headers=AUTH)
    assert resp.json()["status_counts"].get("404") or resp.json()["status_counts"].get(404)


def test_middleware_measures_full_streaming_body_duration_not_just_setup(client, monkeypatch):
    """Real bug found live: /ask_stream is a StreamingResponse — Starlette's
    call_next() returns as soon as the response object exists, well BEFORE
    the SSE body has actually been sent. Timing only around call_next
    massively under-reports latency for every streaming route (observed
    live: 9.8ms avg for calls that actually took 14-21 SECONDS). The
    middleware must wrap the response's body_iterator so the recorded time
    covers the real, full duration."""
    import time

    from src.rag import search as rag_search

    def _slow_ask(*a, **k):
        time.sleep(0.2)
        return {"citations": [], "answer": "x", "llm_used": False, "abstained": True}

    monkeypatch.setattr(rag_search, "ask", _slow_ask)
    resp = client.get("/ask_stream?q=test")
    assert resp.status_code == 200

    snap = client.get("/admin/metrics", headers=AUTH).json()
    route = next(r for r in snap["routes"] if r["route"] == "/ask_stream")
    assert route["avg"] >= 150, (
        f"expected the recorded latency to cover the real ~200ms body-generation "
        f"delay, got {route['avg']}ms — looks like it's only timing call_next()")
