"""Component 43 (DESIGN.md §3f) — Auth0 authentication.

This is the component that turns tenancy from *data partitioning* into an
actual *security boundary*. Until now `X-User-Id` was an unauthenticated header
anyone could set to any value.

Everything here runs against a SELF-SIGNED RSA keypair and a fake JWKS, so the
whole validation path is exercised with no live Auth0 tenant and no network.
That matters: the attack cases (alg confusion, `none`, wrong audience, wrong
issuer) are precisely the ones you cannot check by "logging in and seeing if it
works".

Two constraints from the codebase drive the design and are asserted here:
  1. `src/api/videos.py::user_id` is CLAUDE.md-protected and its
     `^[A-Za-z0-9_-]{1,64}$` regex REJECTS Auth0 subject format (`auth0|abc…`
     contains a `|`), so `sub` cannot be the tenant id directly.
  2. There are TWO independent tenancy implementations (that protected
     dependency, and `search.py::_uid`), so identity must be resolved in
     middleware — ahead of both — rather than patched into either.
"""
from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from src import config, db

jwt = pytest.importorskip("jwt")

DOMAIN = "test-tenant.us.auth0.com"
AUDIENCE = "https://momentsearch.test/api"
ISSUER = f"https://{DOMAIN}/"
KID = "test-key-1"


# ── A self-signed key + JWKS standing in for the Auth0 tenant ────────────────

@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key


@pytest.fixture(scope="module")
def jwks(keypair):
    from jwt.utils import base64url_encode

    nums = keypair.public_key().public_numbers()
    b = lambda n: base64url_encode(  # noqa: E731
        n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()
    return {"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                      "n": b(nums.n), "e": b(nums.e)}]}


@pytest.fixture(scope="module")
def private_pem(keypair):
    return keypair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()


def make_token(private_pem, *, sub="auth0|abc123", aud=AUDIENCE, iss=ISSUER,
               exp_delta=3600, alg="RS256", kid=KID, key=None, **extra):
    now = int(time.time())
    payload = {"sub": sub, "aud": aud, "iss": iss,
               "iat": now, "exp": now + exp_delta, **extra}
    return jwt.encode(payload, key if key is not None else private_pem,
                      algorithm=alg, headers={"kid": kid})


@pytest.fixture(autouse=True)
def _auth0_configured(monkeypatch, jwks):
    """Point the module at the fake tenant and pre-load the fake JWKS so no
    test ever reaches the network."""
    from src import auth0

    monkeypatch.setattr(config, "AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setattr(config, "AUTH0_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(config, "AUTH0_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(auth0, "_fetch_jwks", lambda: jwks)
    auth0.reset_cache()
    yield
    auth0.reset_cache()


# ── Happy path + tenant derivation ───────────────────────────────────────────

def test_valid_token_yields_a_tenant(private_pem):
    from src import auth0

    assert auth0.tenant_for_token(make_token(private_pem)) is not None


def test_tenant_id_satisfies_the_protected_files_regex(private_pem):
    """`videos.py::_USER_RE` is `^[A-Za-z0-9_-]{1,64}$` and that file cannot be
    edited, so a raw Auth0 `sub` (which contains `|`) would be rejected at the
    dependency. The derived tenant MUST always fit."""
    import re

    from src import auth0

    pattern = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    for sub in ["auth0|abc123", "google-oauth2|1234567890",
                "waad|Zm9vQGJhci5jb20", "auth0|" + "x" * 200, "a"]:
        tenant = auth0.tenant_for_sub(sub)
        assert pattern.match(tenant), f"{sub!r} produced an invalid tenant {tenant!r}"


def test_tenant_derivation_is_deterministic():
    from src import auth0

    assert auth0.tenant_for_sub("auth0|abc") == auth0.tenant_for_sub("auth0|abc")


def test_different_subs_get_different_tenants():
    from src import auth0

    assert auth0.tenant_for_sub("auth0|abc") != auth0.tenant_for_sub("auth0|xyz")


# ── Rejection cases — the ones that can't be found by "just logging in" ──────

def test_rejects_expired_token(private_pem):
    from src import auth0

    assert auth0.tenant_for_token(make_token(private_pem, exp_delta=-60)) is None


def test_rejects_wrong_audience(private_pem):
    from src import auth0

    assert auth0.tenant_for_token(
        make_token(private_pem, aud="https://somebody-elses/api")) is None


def test_rejects_wrong_issuer(private_pem):
    from src import auth0

    assert auth0.tenant_for_token(
        make_token(private_pem, iss="https://evil.example.com/")) is None


def test_rejects_bad_signature(private_pem):
    from src import auth0

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    assert auth0.tenant_for_token(make_token(private_pem, key=other_pem)) is None


def test_rejects_alg_none(private_pem):
    """The `none` algorithm: an unsigned token that claims it needs no
    verification."""
    from src import auth0

    unsigned = jwt.encode({"sub": "auth0|attacker", "aud": AUDIENCE, "iss": ISSUER,
                           "exp": int(time.time()) + 3600},
                          key=None, algorithm="none", headers={"kid": KID})
    assert auth0.tenant_for_token(unsigned) is None


def test_rejects_hs256_algorithm_confusion(keypair):
    """The classic RS256→HS256 confusion attack: re-sign the token with HMAC,
    using the server's PUBLIC key as the shared secret. A verifier that trusts
    the token's own `alg` header will happily accept it, because the "secret"
    is a value the attacker already has. RS256 must be pinned.

    The token is assembled by hand rather than with `jwt.encode`, because
    PyJWT refuses to ENCODE with an asymmetric key as an HMAC secret — a real
    attacker has no such scruples, and the point is to test OUR verifier."""
    import base64
    import hashlib
    import hmac as hmac_mod

    from src import auth0

    pub_pem = keypair.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = b64(json.dumps({"sub": "auth0|attacker", "aud": AUDIENCE,
                              "iss": ISSUER, "exp": int(time.time()) + 3600}).encode())
    signing_input = header + b"." + payload
    sig = b64(hmac_mod.new(pub_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + sig).decode()

    assert auth0.tenant_for_token(forged) is None


def test_rejects_unknown_kid(private_pem):
    from src import auth0

    assert auth0.tenant_for_token(make_token(private_pem, kid="not-a-real-kid")) is None


def test_rejects_garbage(private_pem):
    from src import auth0

    for junk in ["", "not.a.jwt", "a.b.c", "Bearer something"]:
        assert auth0.tenant_for_token(junk) is None


def test_disabled_when_not_configured(monkeypatch, private_pem):
    """AUTH0_* unset ⇒ the feature is OFF, and a token is simply not honored —
    same fail-safe convention as REDIS_URL / CLIP_SERVICE_URL."""
    from src import auth0

    monkeypatch.setattr(config, "AUTH0_DOMAIN", "")
    auth0.reset_cache()
    assert auth0.enabled() is False
    assert auth0.tenant_for_token(make_token(private_pem)) is None


# ── End-to-end через the app: identity precedence + gating ───────────────────

@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture(autouse=True)
def _no_real_prefect(monkeypatch):
    from src.api import admin as admin_module
    from src.api import videos as videos_module

    monkeypatch.setattr(admin_module.jobs, "enqueue_document", lambda *a, **k: "fake")
    monkeypatch.setattr(videos_module.jobs, "enqueue_video", lambda *a, **k: "fake")


@pytest.fixture
def client():
    from src.app import app
    return TestClient(app)


def test_jwt_tenant_overrides_a_spoofed_x_user_id(client, private_pem):
    """THE point of this component. If the client-supplied header can still
    win, the spoof is still open."""
    from src import auth0

    token = make_token(private_pem, sub="auth0|realuser")
    expected = auth0.tenant_for_sub("auth0|realuser")
    resp = client.get("/api/videos", headers={
        "Authorization": f"Bearer {token}", "X-User-Id": "victim"})
    assert resp.status_code == 200
    # The library returned must be the JWT's tenant, never "victim".
    resp2 = client.post("/api/videos", json={"url": "https://youtu.be/ccccccccccc"},
                        headers={"Authorization": f"Bearer {token}",
                                 "X-User-Id": "victim"})
    assert resp2.status_code == 202
    row = db.get_video("yt_ccccccccccc")
    try:
        assert row["user_id"] == expected, "spoofed X-User-Id won over the JWT"
        assert row["user_id"] != "victim"
    finally:
        db.delete_video("yt_ccccccccccc")


def test_admin_token_still_honors_x_user_id(client, monkeypatch):
    """benchmark/bench.py and eval/eval.py authenticate with ADMIN_TOKEN and
    select a tenant with X-User-Id. Breaking that breaks the graded SLA gates,
    so the machine path must keep working — deliberately cross-tenant."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")
    resp = client.post("/api/videos", json={"url": "https://youtu.be/ddddddddddd"},
                       headers={"Authorization": "Bearer test-admin-token",
                                "X-User-Id": "benchtenant"})
    assert resp.status_code == 202
    try:
        assert db.get_video("yt_ddddddddddd")["user_id"] == "benchtenant"
    finally:
        db.delete_video("yt_ddddddddddd")


def test_mutation_accepts_a_user_jwt_without_the_admin_token(client, private_pem):
    resp = client.post("/admin/documents",
                       json={"uri": "https://arxiv.org/pdf/1706.03762", "kind": "paper"},
                       headers={"Authorization": f"Bearer {make_token(private_pem)}"})
    assert resp.status_code == 202
    with db.pool().connection() as c:
        c.execute("DELETE FROM ms_documents WHERE uri = %s",
                  ("https://arxiv.org/pdf/1706.03762",))


def test_mutation_rejects_an_invalid_jwt(client, private_pem):
    resp = client.post("/admin/documents", json={"uri": "https://x/p.pdf", "kind": "paper"},
                       headers={"Authorization": f"Bearer {make_token(private_pem, exp_delta=-60)}"})
    assert resp.status_code == 401


def test_search_stays_public(client):
    """README's graded requirement: the deployed public UI answers cross-source
    with no credentials. Login gates mutations only."""
    assert client.get("/api/videos").status_code == 200
    assert client.get("/admin/sources").status_code == 200


def test_api_config_exposes_public_auth0_values(client):
    """The SPA self-configures from these. Domain / client id / audience are
    public by design in a PKCE flow; the client SECRET must never appear."""
    body = client.get("/api/config").json()
    assert body["auth0"]["domain"] == DOMAIN
    assert body["auth0"]["client_id"] == "test-client-id"
    assert body["auth0"]["audience"] == AUDIENCE
    assert "secret" not in json.dumps(body).lower()
