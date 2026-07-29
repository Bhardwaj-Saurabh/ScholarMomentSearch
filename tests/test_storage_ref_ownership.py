"""Component 23 (DESIGN.md §3e) — `storage://` ownership check.

The hole this closes: `POST /admin/documents` took the storage key straight out
of a user-supplied `storage://` URI with no ownership check at all, while the
VIDEO path has always checked it (`src/api/videos.py:92-93`,
`key.startswith(f"{UPLOAD_KEY_PREFIX}{uid}/{video_id}")`). `doc_pipeline.t_fetch`
then downloads that key, parses it, embeds it under the CALLER's user_id, and
serves it back through /api/ask — so any ADMIN_TOKEN holder could read another
tenant's bucket objects into their own corpus.

The rule has to be precise, not just strict. The bucket has exactly three
tenant-scoped prefixes (config.py: `uploads/{uid}/`, `frames/{uid}/`,
`docs/{uid}/`); everything else in it is operator-dropped shared content. The
README's own contract example is `storage://decks/kdd-keynote.pdf` — no tenant
segment — and `tests/test_admin_api.py` asserts that shape returns 202. So:
a key UNDER a tenant-scoped prefix must belong to the caller; a key outside
those prefixes stays allowed. Rejecting every non-`docs/{uid}/` key would have
broken the documented contract, which is why this file tests both directions.
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
    """Mocks jobs.enqueue_document exactly like tests/test_admin_api.py's own
    client fixture. Without this the accepted-path tests reach REAL Prefect
    Cloud and schedule real flow runs for throwaway documents — which is both
    a side effect a unit test has no business causing, and the reason these
    tests passed alone but 502'd inside the full suite."""
    from src.api import admin as admin_module
    monkeypatch.setattr(admin_module.jobs, "enqueue_document",
                        lambda *a, **k: "fake-flow-run-id")
    from src.app import app
    return TestClient(app)


@pytest.fixture
def cleanup():
    ids: list[str] = []
    yield ids
    for i in ids:
        db.delete_document(i)


def _post(client, uri, uid="attacker", kind="paper"):
    return client.post("/admin/documents", json={"uri": uri, "kind": kind},
                       headers={**AUTH, "X-User-Id": uid})


def _post_and_track(client, cleanup, uri, **kw):
    """Register the created id for teardown BEFORE asserting anything. An
    assertion that fires first would skip the append and leak the row into
    the shared test Postgres, where tests/test_reconciler.py's all-tenant
    stale-document scan later picks it up (this exact leak already broke that
    suite once)."""
    resp = _post(client, uri, **kw)
    if resp.status_code == 202:
        cleanup.append(resp.json()["id"])
    return resp


# ── The actual cross-tenant read primitive ───────────────────────────────────

@pytest.mark.parametrize("uri", [
    "storage://docs/victim/doc_secret.pdf",       # another tenant's document
    "storage://uploads/victim/vid_private.mp4",   # another tenant's raw upload
    "storage://frames/victim/vid_x/000001.jpg",   # another tenant's frames
])
def test_rejects_another_tenants_key(client, uri):
    resp = _post(client, uri)
    assert resp.status_code == 403, (
        f"{uri} was accepted for tenant 'attacker' — cross-tenant read primitive")


def test_rejects_another_tenants_key_case_insensitively(client):
    """Bucket keys are case-sensitive, so 'Docs/victim/...' is a DIFFERENT key
    than 'docs/victim/...' and wouldn't hit the same object — but a prefix
    check that only matches exact lowercase would wave through a probe of the
    namespace. Reject the whole shape rather than reason about it."""
    resp = _post(client, "storage://Docs/victim/doc_secret.pdf")
    assert resp.status_code == 403


@pytest.mark.parametrize("uri", [
    "storage:// docs/victim/doc_secret.pdf",     # leading space
    "storage://\tdocs/victim/doc_secret.pdf",    # leading tab
    "storage://docs/victim/doc_secret.pdf ",     # trailing space
    "storage://docs\\victim\\doc_secret.pdf",    # backslash separators
])
def test_rejects_whitespace_and_backslash_evasions(client, uri):
    """Found by spec-guardian on the first cut: emptiness was checked on
    key.strip() while the prefix match used the raw key, so ' docs/victim/x'
    was waved through as shared content. It reads nothing (object stores treat
    that as a different key) — but this function's job is to reject the shape,
    not to depend on which variants happen not to resolve."""
    assert _post(client, uri).status_code == 403


@pytest.mark.parametrize("uri", [
    "storage://docs/../docs/victim/doc_secret.pdf",
    "storage://docs/attacker/../victim/doc_secret.pdf",
    "storage:///docs/victim/doc_secret.pdf",
])
def test_rejects_traversal_and_absolute_keys(client, uri):
    """A tenant-prefix check is only as good as the path normalization in
    front of it — '..' segments would otherwise walk out of the caller's
    namespace while still passing a naive startswith()."""
    resp = _post(client, uri)
    assert resp.status_code == 403


def test_rejects_empty_storage_key(client):
    assert _post(client, "storage://").status_code == 400


# ── What must KEEP working (documented contract + own-tenant access) ─────────

def test_allows_own_tenant_key(client, cleanup):
    resp = _post_and_track(client, cleanup, "storage://docs/attacker/doc_mine.pdf")
    assert resp.status_code == 202


def test_allows_shared_non_tenant_scoped_key(client, cleanup):
    """README.md's own contract example: `storage://decks/kdd-keynote.pdf`.
    'decks/' is not one of the three tenant-scoped prefixes, so this is
    operator-dropped shared content and must still be accepted — locking this
    down to `docs/{uid}/` only would break the documented contract and an
    existing passing test in tests/test_admin_api.py."""
    resp = _post_and_track(client, cleanup, "storage://decks/kdd-keynote.pdf", kind="deck")
    assert resp.status_code == 202


def test_http_uris_are_unaffected(client, cleanup):
    """This component only governs `storage://`; http(s) fetching is
    component 24's problem, and must not start 403ing here."""
    resp = _post_and_track(client, cleanup, "https://arxiv.org/pdf/1706.03762")
    assert resp.status_code == 202
