"""Component 1 (DESIGN.md) — ms_documents table + unified GET /admin/sources query.

Mirrors the ms_videos pattern in src/db.py. DB layer only: no Qdrant/Prefect/LLM.
Requires a reachable Postgres — see tests/conftest.py.
"""
from __future__ import annotations

import uuid

import pytest

from src import db


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture
def cleanup():
    ids = {"documents": [], "videos": []}
    yield ids
    for i in ids["documents"]:
        db.delete_document(i)
    for i in ids["videos"]:
        db.delete_video(i)


def _mk_doc(doc_id, user_id="u_test", kind="paper",
           uri="https://arxiv.org/pdf/1706.03762", title="Attention Is All You Need"):
    return {"id": doc_id, "user_id": user_id, "kind": kind, "uri": uri,
            "storage_key": None, "source_hash": None, "title": title}


def _mk_video(video_id, user_id="u_test", title="Attention talk"):
    return {"id": video_id, "user_id": user_id, "source": "youtube",
            "url": "https://youtu.be/rBCqOTEfxvg", "storage_key": None,
            "source_hash": None, "title": title}


def _doc_id():
    return f"doc_{uuid.uuid4().hex[:10]}"


def test_upsert_pending_creates_row(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    row = db.upsert_pending_document(_mk_doc(doc_id))
    assert row["id"] == doc_id
    assert row["status"] == "pending"
    assert row["kind"] == "paper"
    assert row["attempts"] == 0


def test_upsert_pending_resets_on_resubmit(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    db.upsert_pending_document(_mk_doc(doc_id))
    db.set_document_status(doc_id, "failed", error="boom")
    row = db.upsert_pending_document(_mk_doc(doc_id))
    assert row["status"] == "pending"
    assert row["error"] is None


def test_set_status_updates_fields(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    db.upsert_pending_document(_mk_doc(doc_id))
    db.set_document_status(doc_id, "embedding", chunk_count=12, page_count=9,
                           embed_version="bge-small-en-v1.5-v1", progress=0.5)
    row = db.get_document(doc_id)
    assert row["status"] == "embedding"
    assert row["chunk_count"] == 12
    assert row["page_count"] == 9
    assert row["embed_version"] == "bge-small-en-v1.5-v1"
    assert row["progress"] == 0.5


def test_bump_attempts_increments(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    db.upsert_pending_document(_mk_doc(doc_id))
    assert db.bump_document_attempts(doc_id) == 1
    assert db.bump_document_attempts(doc_id) == 2


def test_find_duplicate_respects_status_and_user(cleanup):
    doc_id, other_id = _doc_id(), _doc_id()
    cleanup["documents"] += [doc_id, other_id]
    h = f"hash_{uuid.uuid4().hex}"
    db.upsert_pending_document(_mk_doc(doc_id, uri="https://x/1.pdf"))
    db.set_document_status(doc_id, "indexed", source_hash=h)
    db.upsert_pending_document(_mk_doc(other_id, uri="https://x/2.pdf"))
    db.set_document_status(other_id, "pending", source_hash=h)  # not yet indexed

    dup = db.find_duplicate_document("u_test", h, exclude_id=other_id)
    assert dup is not None and dup["id"] == doc_id
    assert db.find_duplicate_document("someone_else", h, exclude_id=other_id) is None


def test_list_documents_filters_by_user_and_status(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    db.upsert_pending_document(_mk_doc(doc_id, user_id="u_filter"))
    db.set_document_status(doc_id, "indexed")
    rows = db.list_documents("u_filter", status="indexed")
    assert any(r["id"] == doc_id for r in rows)
    assert all(r["status"] == "indexed" for r in rows)
    assert all(r["id"] != doc_id for r in db.list_documents("u_filter", status="failed"))


def test_documents_by_ids_batch_fetch(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    db.upsert_pending_document(_mk_doc(doc_id))
    out = db.documents_by_ids([doc_id, "doc_does_not_exist"])
    assert set(out.keys()) == {doc_id}
    assert out[doc_id]["title"] == "Attention Is All You Need"


def test_delete_document_removes_row():
    doc_id = _doc_id()
    db.upsert_pending_document(_mk_doc(doc_id))
    db.delete_document(doc_id)
    assert db.get_document(doc_id) is None


def test_list_sources_unifies_videos_and_documents(cleanup):
    doc_id, video_id = _doc_id(), f"yt_{uuid.uuid4().hex[:11]}"
    cleanup["documents"].append(doc_id)
    cleanup["videos"].append(video_id)
    user = "u_unify_test"

    db.upsert_pending(_mk_video(video_id, user_id=user, title="Attention talk"))
    db.set_status(video_id, "indexed")

    db.upsert_pending_document(_mk_doc(doc_id, user_id=user, title="Attention paper"))
    db.set_document_status(doc_id, "embedding", progress=0.6)

    sources = db.list_sources(user)
    by_id = {s["id"]: s for s in sources}

    assert by_id[video_id] == {"id": video_id, "kind": "video", "status": "indexed",
                               "title": "Attention talk", "pct": None}
    assert by_id[doc_id] == {"id": doc_id, "kind": "paper", "status": "embedding",
                             "title": "Attention paper", "pct": 60}

    ids_in_order = [s["id"] for s in sources]  # doc created after video -> newest first
    assert ids_in_order.index(doc_id) < ids_in_order.index(video_id)
