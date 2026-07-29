"""Component 25 (DESIGN.md §3e) — hardened auth layer.

Three defects in the inherited auth, all in a CLAUDE.md-protected file
(`src/api/videos.py::require_auth`), so all three are fixed additively in
app-level middleware rather than by editing it:

  1. It fails **OPEN**: `if not ADMIN_TOKEN: return`. An unset/empty token
     turns every "protected" route fully public. Fine as dev convenience,
     catastrophic if a deploy ever ships without the secret set — which is
     exactly the shape of accident that happens at 2am.
  2. It compares with `!=` on a plain string — not constant-time.
  3. `GET /api/llm` was never gated at all, and returns provider, model,
     base_url and a key hint for whatever tenant the (spoofable) X-User-Id
     header names.

The middleware runs ahead of routing, so it also closes the "a new route
forgets its Depends(require_auth)" failure mode structurally rather than by
convention. The existing route-level dependencies stay in place — redundant,
harmless, and it keeps the protected file untouched.

NOT in scope here (deliberate): the read endpoints `GET /api/videos` and
`GET /admin/sources` stay public exactly as today. Gating them would break the
browser UI, which sends no Authorization header on anything — that is
component 27's job, and 27 depends on this one.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config, db

GOOD = "test-admin-token"          # matches tests/conftest.py's ADMIN_TOKEN
BAD_SAME_LEN = "test-admin-tokeX"
BAD_DIFF_LEN = "nope"


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """Pin the token/env for every test here; individual tests override."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", GOOD)
    monkeypatch.setattr(config, "ENV", "development")


_TEST_VIDEO_ID = "yt_aaaaaaaaaaa"      # derived from the YouTube URL below
_TEST_DOC_URI = "https://x.example/p.pdf"


@pytest.fixture(autouse=True)
def _no_real_prefect(monkeypatch):
    """The accept-with-a-valid-token cases genuinely reach their handlers, so
    without this they schedule REAL Prefect Cloud flow runs. Mirrors
    tests/test_admin_api.py's own client fixture."""
    from src.api import admin as admin_module
    from src.api import videos as videos_module

    monkeypatch.setattr(admin_module.jobs, "enqueue_document",
                        lambda *a, **k: "fake-flow-run-id")
    monkeypatch.setattr(videos_module.jobs, "enqueue_video",
                        lambda *a, **k: "fake-flow-run-id")


@pytest.fixture(autouse=True)
def _purge_rows_this_file_creates():
    """`test_accepts_correct_token` reaches the real handlers, so POST
    /api/videos and POST /admin/documents actually insert rows. Left behind,
    they leak into the shared test Postgres and — once aged past
    RECONCILE_STALE_AFTER_S — get picked up by tests/test_reconciler.py's
    all-tenant stale scan, failing it. That has now bitten three separate
    test files in this program, so this fixture deletes by known identity
    rather than relying on each test to remember."""
    yield
    db.delete_video(_TEST_VIDEO_ID)
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM ms_documents WHERE uri = %s", (_TEST_DOC_URI,))


@pytest.fixture
def client():
    from src.app import app
    return TestClient(app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# Every route the middleware must gate. Bodies are deliberately minimal —
# these assert the AUTH decision, which happens before any handler logic, so
# a 4xx-for-bad-input response still proves auth passed.
PROTECTED = [
    ("POST", "/api/videos", {"url": f"https://youtu.be/{_TEST_VIDEO_ID[3:]}"}),
    ("POST", "/api/videos/presign", {"filename": "x.mp4", "content_type": "video/mp4", "size": 10}),
    ("POST", "/api/videos/vid_x/retry", None),
    ("DELETE", "/api/videos/vid_x", None),
    ("POST", "/admin/documents", {"uri": _TEST_DOC_URI, "kind": "paper"}),
    ("POST", "/admin/documents/doc_x/retry", None),
    ("PUT", "/api/llm", {"provider": "openai", "model": "gpt-4o-mini"}),
    ("POST", "/api/llm/test", None),
    ("DELETE", "/api/llm", None),
    ("GET", "/api/llm", None),          # NEW in this component — was fully public
    ("GET", "/metrics", None),
    ("GET", "/admin/metrics", None),
]


def _call(client, method, path, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_requires_a_token(client, method, path, body):
    assert _call(client, method, path, body).status_code == 401


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_rejects_wrong_token_same_length(client, method, path, body):
    assert _call(client, method, path, body, _auth(BAD_SAME_LEN)).status_code == 401


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_rejects_wrong_token_different_length(client, method, path, body):
    assert _call(client, method, path, body, _auth(BAD_DIFF_LEN)).status_code == 401


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_accepts_correct_token(client, method, path, body):
    """Auth must not be the thing that fails. Any non-401 is a pass here — a
    404/400/422 means the request reached the handler, which is the point."""
    assert _call(client, method, path, body, _auth(GOOD)).status_code != 401


@pytest.mark.parametrize("scheme", ["", "Token ", "bearer ", "Basic "])
def test_rejects_non_bearer_schemes(client, scheme):
    resp = _call(client, "DELETE", "/api/videos/vid_x", None,
                 {"Authorization": f"{scheme}{GOOD}"})
    assert resp.status_code == 401, f"scheme {scheme!r} was accepted"


# ── Fail closed when the secret is missing ───────────────────────────────────

def test_fails_closed_in_production_when_token_unset(client, monkeypatch):
    """The inherited behavior (`if not ADMIN_TOKEN: return`) silently makes
    every mutating route public. In production that must be a hard stop, not
    a convenience — a 503, because an unset server secret is a server
    misconfiguration, not a client error."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "ENV", "production")
    resp = _call(client, "DELETE", "/api/videos/vid_x", None)
    assert resp.status_code == 503


def test_fails_closed_in_production_even_with_a_token_presented(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "ENV", "production")
    resp = _call(client, "DELETE", "/api/videos/vid_x", None, _auth("anything"))
    assert resp.status_code == 503


@pytest.mark.parametrize("env", ["production", "prod", "prd", "staging",
                                 "Production", " production ", "typo-env", ""])
def test_fails_closed_for_any_non_dev_environment(client, monkeypatch, env):
    """spec-guardian finding: the first cut compared `ENV == "production"`, so
    `prod`/`staging`/a typo all failed OPEN — a safety default that depends on
    spelling one magic word correctly is not a safety default. The check is
    now an allowlist of dev environment names; everything else fails closed."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "ENV", env.strip().lower())
    assert _call(client, "DELETE", "/api/videos/vid_x", None).status_code == 503


@pytest.mark.parametrize("env", ["development", "dev", "local", "test", "testing"])
def test_dev_environments_still_allow_an_unset_token(client, monkeypatch, env):
    from src.api import videos as videos_module

    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "ENV", env)
    monkeypatch.setattr(videos_module, "ADMIN_TOKEN", "")
    assert _call(client, "DELETE", "/api/videos/vid_x", None).status_code != 503


def test_dev_convenience_preserved_when_token_unset(client, monkeypatch):
    """Unset token outside production keeps today's open behavior, so a fresh
    cloner can still run the stack with no configuration.

    Both `config.ADMIN_TOKEN` and `videos.ADMIN_TOKEN` are patched because the
    protected `src/api/videos.py` binds the token BY VALUE at import
    (`from ..config import ADMIN_TOKEN`), so patching only the config module
    leaves its route-level `require_auth` still holding the old value and the
    request 401s at the route instead of the middleware. Patching one place
    would test a state that cannot occur in production, where an unset env var
    means both are empty."""
    from src.api import videos as videos_module

    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "ENV", "development")
    monkeypatch.setattr(videos_module, "ADMIN_TOKEN", "")
    assert _call(client, "DELETE", "/api/videos/vid_x", None).status_code != 401


# ── Public surface must stay public ──────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/health", None),
    ("GET", "/api/config", None),
    ("GET", "/api/videos", None),
    ("GET", "/admin/sources", None),
    ("POST", "/api/ask", {"question": "what is attention?"}),
])
def test_public_routes_stay_public(client, method, path, body):
    """Read/ask endpoints are unchanged by this component. Locking them down
    would break the browser UI (which sends no Authorization on anything) —
    that is component 27's scope, and rate limiting is component 26's."""
    assert _call(client, method, path, body).status_code != 401


def test_ui_pages_stay_public(client):
    assert client.get("/").status_code == 200


# ── The structural guarantee: enforcement happens before routing ─────────────

def test_unknown_path_under_a_protected_prefix_is_gated(client):
    """A future route added under /admin or /api/videos is protected even if
    its author forgets Depends(require_auth) — that is the whole reason this
    lives in middleware rather than as another decorator."""
    resp = _call(client, "POST", "/admin/some-future-route", {})
    assert resp.status_code == 401


def test_constant_time_comparison_is_used():
    """Timing side-channels can't be asserted reliably in a unit test, so
    assert the mechanism instead: the token check must go through
    hmac.compare_digest, not `==`/`!=`."""
    import inspect

    from src import security

    src = inspect.getsource(security.token_ok)
    assert "compare_digest" in src
