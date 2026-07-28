"""Admin API for papers & decks (DESIGN.md component 6) — mirrors
src/api/videos.py's register/status shape, extended to the two new source
kinds. Auth/tenancy dependencies are IMPORTED from videos.py, not duplicated —
the same convention src/api/search.py already uses for its Bearer-gated routes.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, jobs
from .videos import require_auth
from .videos import user_id as user_id_dep

router = APIRouter(prefix="/admin", tags=["admin"])

_ALLOWED_KINDS = ("paper", "deck")
_ALLOWED_SCHEMES = ("http://", "https://", "storage://")


class DocumentRequest(BaseModel):
    uri: str | None = None
    kind: str | None = None
    title: str | None = None


@router.post("/documents", status_code=202, dependencies=[Depends(require_auth)])
def register_document(req: DocumentRequest, uid: str = Depends(user_id_dep)):
    if not req.kind or req.kind not in _ALLOWED_KINDS:
        raise HTTPException(400, f"kind must be one of {_ALLOWED_KINDS}.")
    if not req.uri or not req.uri.strip():
        raise HTTPException(400, "uri is required.")
    if not req.uri.startswith(_ALLOWED_SCHEMES):
        raise HTTPException(400, f"uri must start with one of {_ALLOWED_SCHEMES}.")

    doc_id = f"doc_{uuid.uuid4().hex[:10]}"
    is_storage_ref = req.uri.startswith("storage://")
    storage_key = req.uri[len("storage://"):] if is_storage_ref else None
    row = db.upsert_pending_document({
        "id": doc_id, "user_id": uid, "kind": req.kind, "uri": req.uri,
        "storage_key": storage_key, "source_hash": None, "title": req.title,
    })

    # Fire-and-forget schedule, exactly like /api/videos' register(): insert
    # (done above) -> enqueue -> 202. A failure here is the upstream's fault
    # (Prefect Cloud unreachable), not the caller's — 502, not 400/500.
    try:
        flow_run_id = jobs.enqueue_document(row["id"], uid, req.kind)
        db.set_document_flow_run_id(row["id"], flow_run_id)
    except Exception as exc:
        db.set_document_status(row["id"], "failed", error=f"enqueue: {exc}")
        raise HTTPException(502, "Failed to schedule ingestion.") from exc

    return {"id": row["id"], "status": row["status"], "kind": row["kind"]}


@router.get("/sources")
def list_sources(uid: str = Depends(user_id_dep)):
    return {"sources": db.list_sources(uid)}


@router.post("/documents/{doc_id}/retry", status_code=202, dependencies=[Depends(require_auth)])
def retry_document(doc_id: str, uid: str = Depends(user_id_dep)):
    """Mirrors POST /api/videos/{id}/retry. Documents ride FIFO directly
    (component 5's decision — no dispatcher/fair-queue branch), so a retry
    always re-enqueues immediately."""
    row = db.get_document(doc_id)
    if row is None or row["user_id"] != uid:
        raise HTTPException(404, "Document not found.")
    db.set_document_status(doc_id, "pending", error=None)
    flow_run_id = jobs.enqueue_document(doc_id, uid, row["kind"])
    db.set_document_flow_run_id(doc_id, flow_run_id)
    return {"id": doc_id, "status": "pending", "flow_run_id": flow_run_id}
