"""Admin API for papers & decks (DESIGN.md component 6) — mirrors
src/api/videos.py's register/status shape, extended to the two new source
kinds. Auth/tenancy dependencies are IMPORTED from videos.py, not duplicated —
the same convention src/api/search.py already uses for its Bearer-gated routes.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, jobs, trace_link, tracing
from ..config import DOC_KEY_PREFIX, FRAME_KEY_PREFIX, UPLOAD_KEY_PREFIX
from .videos import require_auth
from .videos import user_id as user_id_dep

router = APIRouter(prefix="/admin", tags=["admin"])

_ALLOWED_KINDS = ("paper", "deck")
_ALLOWED_SCHEMES = ("http://", "https://", "storage://")

# The three bucket namespaces that are per-tenant (src/config.py's key layout).
# Anything OUTSIDE these is operator-dropped shared content — README's own
# contract example is `storage://decks/kdd-keynote.pdf`, which has no tenant
# segment at all.
_TENANT_SCOPED_PREFIXES = (UPLOAD_KEY_PREFIX, FRAME_KEY_PREFIX, DOC_KEY_PREFIX)


def _check_storage_key_ownership(key: str, uid: str) -> None:
    """DESIGN.md §3e component 23. `POST /admin/documents` used to take this
    key verbatim, so any caller could name ANOTHER tenant's object and have it
    downloaded, parsed, embedded under their own user_id, and read back via
    /api/ask — a cross-tenant read primitive. The video path has always
    checked this (`src/api/videos.py:92-93`); the document path never did.

    The check has to be exact rather than merely strict: requiring
    `docs/{uid}/` would reject the shared-content shape README documents and
    tests/test_admin_api.py asserts. So only keys UNDER a tenant-scoped
    prefix are ownership-checked; keys outside them stay allowed.

    Prefix matching is case-INSENSITIVE on purpose. Bucket keys are
    case-sensitive, so `Docs/victim/x` names a different object than
    `docs/victim/x` and wouldn't read the victim's file — but it is still a
    probe of someone else's namespace, and rejecting the shape outright beats
    reasoning about which case variants happen to resolve."""
    if not key.strip():
        raise HTTPException(400, "storage:// uri needs a key.")
    # Normalize before comparing: a '..' segment would otherwise walk out of
    # the caller's namespace while still satisfying a naive startswith().
    if key.startswith("/") or ".." in key.split("/"):
        raise HTTPException(403, "Key must be a plain relative bucket key.")
    # Found by spec-guardian review of the first cut: emptiness was tested on
    # key.strip() but the prefix match ran on the RAW key, so " docs/victim/x"
    # (leading space/tab/newline) sailed through as "shared content". It reads
    # nothing today — object stores treat " docs/x" as a genuinely different
    # key — but the whole point of this function is to reject the SHAPE rather
    # than rely on which variants happen not to resolve. Backslashes get the
    # same treatment: they are not path separators here, so a key containing
    # one is never a legitimate reference to our own layout.
    if key != key.strip() or "\\" in key:
        raise HTTPException(403, "Key must be a plain relative bucket key.")
    lowered = key.lower()
    for prefix in _TENANT_SCOPED_PREFIXES:
        if lowered.startswith(prefix.lower()):
            owner = key[len(prefix):].split("/", 1)[0]
            if owner != uid:
                raise HTTPException(403, "Key belongs to a different tenant.")
            return


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
    if is_storage_ref:
        _check_storage_key_ownership(storage_key, uid)
    row = db.upsert_pending_document({
        "id": doc_id, "user_id": uid, "kind": req.kind, "uri": req.uri,
        "storage_key": storage_key, "source_hash": None, "title": req.title,
    })

    # Fire-and-forget schedule, exactly like /api/videos' register(): insert
    # (done above) -> enqueue -> 202. A failure here is the upstream's fault
    # (Prefect Cloud unreachable), not the caller's — 502, not 400/500.
    try:
        # Component 46: open a span for the registration and stash its trace id
        # so the WORKER joins the same trace seconds later. Via Redis rather
        # than a flow parameter — `ingest_video`'s signature is in a protected
        # file and changing `ingest_document`'s would alter a registered
        # Prefect deployment. Fails open: no Redis just means an uncorrelated
        # worker trace.
        with tracing.span("register_document", doc_id=row["id"], tenant=uid,
                          kind=req.kind, uri=req.uri) as _sp:
            trace_link.stash(row["id"], tracing.current_trace_id() or "")
            flow_run_id = jobs.enqueue_document(row["id"], uid, req.kind)
            _sp.set_attrs(flow_run_id=flow_run_id)
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
