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


def test_register_document_stays_202_when_enqueue_fails(client, monkeypatch, cleanup):
    """Component 56 (DESIGN.md §3m): Prefect dispatch is deferred to a
    background task, so a Prefect-Cloud failure can no longer fail the accept —
    the caller gets its 202 (the insert succeeded and the row exists), and the
    failure surfaces on the ROW as status='failed' (visible in /admin/sources,
    recoverable via the retry endpoint), not as a request error. This is the
    discriminating eval for the deferral: under the old in-request dispatch
    this exact scenario returned 502."""
    captured = {}

    def _boom(doc_id, user_id, kind):
        captured["doc_id"] = doc_id
        raise RuntimeError("Prefect Cloud unreachable")

    from src.api import admin as admin_module
    monkeypatch.setattr(admin_module.jobs, "enqueue_document", _boom)

    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    assert resp.status_code == 202
    assert captured["doc_id"], "dispatch must still be attempted (in the background)"
    cleanup.append(captured["doc_id"])
    row = db.get_document(captured["doc_id"])
    assert row["status"] == "failed"
    assert "enqueue" in (row["error"] or "")


def test_register_document_sets_flow_run_id_in_background(client, cleanup):
    """TestClient runs BackgroundTasks to completion before returning, so the
    flow_run_id written by the deferred dispatch is observable right after."""
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    assert resp.status_code == 202
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)
    row = db.get_document(doc_id)
    assert row["flow_run_id"] == "fake-flow-run-id"
    assert row["status"] == "pending"


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


# ── Component 11: retry (library panel "document lifecycle + retry") ────────

def test_retry_document_requires_auth(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)
    db.set_document_status(doc_id, "failed", error="boom")

    resp = client.post(f"/admin/documents/{doc_id}/retry")
    assert resp.status_code == 401


def test_retry_document_resets_to_pending_and_reenqueues(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)
    db.set_document_status(doc_id, "failed", error="network blip")

    resp = client.post(f"/admin/documents/{doc_id}/retry", headers=AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == doc_id
    assert body["status"] == "pending"

    row = db.get_document(doc_id)
    assert row["status"] == "pending"
    assert row["error"] is None


def test_retry_document_404_when_missing(client):
    resp = client.post("/admin/documents/doc_does_not_exist/retry", headers=AUTH)
    assert resp.status_code == 404


def test_retry_document_404_for_wrong_tenant(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)
    db.set_document_status(doc_id, "failed", error="boom")

    resp = client.post(f"/admin/documents/{doc_id}/retry",
                       headers={**AUTH, "X-User-Id": "someone-else"})
    assert resp.status_code == 404


# ── Component 34 (DESIGN.md §3e) — DELETE /admin/documents/{id} ─────────────
# db.delete_document had zero production callers before this: a paper or deck
# was permanent through the API. Ordered purge-vectors -> delete-object ->
# delete-row, surfacing a purge failure instead of swallowing it (component
# 53's cancel-on-delete rides along inside db.delete_document already).

def test_delete_document_requires_auth(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)

    resp = client.delete(f"/admin/documents/{doc_id}")
    assert resp.status_code == 401


def test_delete_document_purges_vectors_storage_and_row(client, monkeypatch):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    db.set_document_storage_key(doc_id, "docs/default/somefile.pdf")

    from src.api import admin as admin_module
    purged = {}
    monkeypatch.setattr(
        admin_module.vector_store, "delete_document_chunks",
        lambda uid, sid, **kw: purged.update(uid=uid, sid=sid, kwargs=kw))
    deleted_keys = []
    monkeypatch.setattr(admin_module.storage, "delete_key", deleted_keys.append)

    resp = client.delete(f"/admin/documents/{doc_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": doc_id}
    assert purged == {"uid": "default", "sid": doc_id, "kwargs": {"raise_on_error": True}}
    assert deleted_keys == ["docs/default/somefile.pdf"]
    assert db.get_document(doc_id) is None


def test_delete_document_skips_storage_delete_when_no_storage_key(client, monkeypatch):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]

    from src.api import admin as admin_module
    monkeypatch.setattr(admin_module.vector_store, "delete_document_chunks",
                        lambda uid, sid, **kw: None)
    deleted_keys = []
    monkeypatch.setattr(admin_module.storage, "delete_key", deleted_keys.append)

    resp = client.delete(f"/admin/documents/{doc_id}", headers=AUTH)
    assert resp.status_code == 200
    assert deleted_keys == []


def test_delete_document_404_when_missing(client):
    resp = client.delete("/admin/documents/doc_does_not_exist", headers=AUTH)
    assert resp.status_code == 404


def test_delete_document_404_for_wrong_tenant(client, cleanup):
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)

    resp = client.delete(f"/admin/documents/{doc_id}",
                         headers={**AUTH, "X-User-Id": "someone-else"})
    assert resp.status_code == 404


def test_delete_document_403_for_seeded_sample(client):
    from src.samples import CORPUS, seed_doc_id
    if not CORPUS:
        pytest.skip("benchmark/corpus.json not present in this checkout")
    sample_id = seed_doc_id(CORPUS[0]["id"], "paper")
    resp = client.delete(f"/admin/documents/{sample_id}", headers=AUTH)
    assert resp.status_code == 403


def test_delete_document_502_when_vector_purge_fails_row_survives(client, monkeypatch, cleanup):
    """RED-today scenario (DESIGN.md's primary eval for component 34): a purge
    failure must not leave a successful-looking delete with orphaned vectors —
    the row has to survive so the document is still visibly present/retryable
    rather than silently vanishing while its vectors linger."""
    resp = client.post("/admin/documents", json={
        "uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"}, headers=AUTH)
    doc_id = resp.json()["id"]
    cleanup.append(doc_id)

    from src.api import admin as admin_module

    def _boom(uid, sid, **kw):
        raise RuntimeError("Qdrant unreachable")
    monkeypatch.setattr(admin_module.vector_store, "delete_document_chunks", _boom)

    resp = client.delete(f"/admin/documents/{doc_id}", headers=AUTH)
    assert resp.status_code == 502
    assert db.get_document(doc_id) is not None  # row survives — not silently gone
