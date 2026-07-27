"""Component 6 (DESIGN.md) — admin router (src/api/admin.py): POST
/admin/documents (202 accept-then-enqueue) and GET /admin/sources (unified
video+document status).

Uses FastAPI's TestClient — an in-process, no-server-needed way to exercise
the real request/response/validation cycle through the actual app. This
doubles as the contract-probe layer (README's "API contract" checklist) until
a live docker-compose stack exists for the `contract-probe` skill's live-curl
variant (see EVIDENCE.md).

Real: Postgres (throwaway container), the FastAPI app/routing/pydantic
validation. Mocked: jobs.enqueue_document — a real call needs a live Prefect
deployment/worker (out of scope for an API unit test; already tested in
isolation in component 5's test_jobs_worker.py).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db
from src.config import ADMIN_TOKEN

AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture
def client(monkeypatch):
    from src.api import admin as admin_module
    monkeypatch.setattr(admin_module.jobs, "enqueue_document",
                        lambda *a, **k: "fake-flow-run-id")
    from src.app import app
    return TestClient(app)


@pytest.fixture
def cleanup():
    ids = []
    yield ids
    for i in ids:
        db.delete_document(i)


def test_register_document_returns_202_before_work(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper", "title": "Attention"},
        headers=AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["kind"] == "paper"
    assert body["id"].startswith("doc_")
    cleanup.append(body["id"])

    row = db.get_document(body["id"])
    assert row is not None
    assert row["status"] == "pending"  # nothing parsed/embedded yet


def test_register_document_requires_auth(client):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"})
    assert resp.status_code == 401


def test_register_document_rejects_bad_kind(client):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/x", "kind": "podcast"}, headers=AUTH)
    assert resp.status_code == 400


def test_register_document_rejects_missing_uri(client):
    resp = client.post("/admin/documents", json={"kind": "paper", "uri": ""}, headers=AUTH)
    assert resp.status_code == 400


def test_register_document_rejects_bad_uri_scheme(client):
    resp = client.post("/admin/documents", json={
        "uri": "ftp://old-protocol.example/x.pdf", "kind": "paper"}, headers=AUTH)
    assert resp.status_code == 400


def test_register_document_storage_ref_sets_storage_key(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "storage://decks/kdd-keynote.pdf", "kind": "deck", "title": "KDD Keynote"},
        headers=AUTH)
    assert resp.status_code == 202
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)
    row = db.get_document(doc_id)
    assert row["storage_key"] == "decks/kdd-keynote.pdf"
    assert row["uri"] == "storage://decks/kdd-keynote.pdf"


def test_register_document_returns_502_on_enqueue_failure(client, monkeypatch, cleanup):
    captured = {}

    def _boom(doc_id, user_id, kind):
        captured["doc_id"] = doc_id
        raise RuntimeError("Prefect Cloud unreachable")

    from src.api import admin as admin_module
    monkeypatch.setattr(admin_module.jobs, "enqueue_document", _boom)

    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    assert resp.status_code == 502
    assert captured["doc_id"]
    cleanup.append(captured["doc_id"])
    row = db.get_document(captured["doc_id"])
    assert row["status"] == "failed"


def test_list_sources_returns_unified_shape(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/2103.00020", "kind": "paper", "title": "CLIP"},
        headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)

    resp = client.get("/admin/sources", headers={"X-User-Id": "default"})
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    match = next(s for s in sources if s["id"] == doc_id)
    assert match == {"id": doc_id, "kind": "paper", "status": "pending",
                     "title": "CLIP", "pct": None, "chunk_count": None}


def test_list_sources_requires_no_auth(client):
    """Read-only listing is public/tenant-scoped, matching GET /api/videos'
    existing convention — only mutating routes carry the Bearer requirement."""
    resp = client.get("/admin/sources")
    assert resp.status_code == 200
