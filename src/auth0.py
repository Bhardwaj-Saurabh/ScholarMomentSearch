"""Auth0 access-token validation — DESIGN.md §3f component 43.

This is what turns tenancy from *data partitioning* into a real *security
boundary*: before this, `X-User-Id` was an unauthenticated header and any
caller could name any tenant.

What it does: validates an RS256 access token against the tenant's published
JWKS (signature, `exp`, `aud`, `iss`), then derives a stable tenant id from the
token's `sub`.

Two non-obvious decisions, both load-bearing:

**RS256 is pinned, never read from the token.** Trusting a token's own `alg`
header is the classic confusion attack — an attacker re-signs with HS256 using
the (public!) RSA key as the HMAC secret, and a verifier that honors the header
accepts it. `alg=none` is the degenerate case of the same bug. `jwt.decode` is
therefore always called with an explicit `algorithms=["RS256"]`.

**The tenant id is a hash of `sub`, not `sub` itself.** `src/api/videos.py` is
CLAUDE.md-protected and validates tenants against `^[A-Za-z0-9_-]{1,64}$`.
Auth0 subjects are `auth0|68a3…`/`google-oauth2|1234…` — the `|` fails that
regex, and long provider subjects can exceed 64 chars. `u_<sha256(sub)[:32]>`
is deterministic, collision-resistant in practice, and always valid. It is
deliberately one-way: the UI shows the signed-in email from its OWN id token,
so the server never needs to store a sub→tenant mapping.

Unset `AUTH0_DOMAIN`/`AUTH0_AUDIENCE` disables the whole feature and the app
behaves exactly as it did before it existed — the same convention `REDIS_URL`
and `CLIP_SERVICE_URL` already use.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request

from . import config

logger = logging.getLogger(__name__)

_ALGORITHMS = ["RS256"]      # pinned; NEVER taken from the token header
_JWKS_TIMEOUT_S = 5

_lock = threading.Lock()
_jwks_cache: dict | None = None


def enabled() -> bool:
    return bool(config.AUTH0_DOMAIN and config.AUTH0_AUDIENCE)


def issuer() -> str:
    return f"https://{config.AUTH0_DOMAIN}/"


def reset_cache() -> None:
    """Test hook, and the recovery path when a `kid` isn't in the cached set."""
    global _jwks_cache
    with _lock:
        _jwks_cache = None


def _fetch_jwks() -> dict:
    url = f"https://{config.AUTH0_DOMAIN}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=_JWKS_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def _jwks(refresh: bool = False) -> dict | None:
    global _jwks_cache
    with _lock:
        if _jwks_cache is not None and not refresh:
            return _jwks_cache
    try:
        fetched = _fetch_jwks()
    except Exception as exc:
        logger.warning("[auth0] could not fetch JWKS (%r) — rejecting tokens", exc)
        return None
    with _lock:
        _jwks_cache = fetched
    return fetched


def _signing_key(kid: str):
    """Public key for `kid`. A miss triggers ONE refetch — Auth0 rotates keys,
    and a stale cache must not lock every user out until a restart."""
    from jwt import PyJWK

    for refresh in (False, True):
        jwks = _jwks(refresh=refresh)
        if not jwks:
            return None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                try:
                    return PyJWK.from_dict(key).key
                except Exception as exc:
                    logger.warning("[auth0] unusable JWKS entry for kid %r: %r", kid, exc)
                    return None
    return None


def tenant_for_sub(sub: str) -> str:
    """Stable, opaque tenant id that always satisfies the protected file's
    `^[A-Za-z0-9_-]{1,64}$` (35 chars: 'u_' + 32 hex)."""
    return "u_" + hashlib.sha256(sub.encode()).hexdigest()[:32]


def claims_for_token(token: str) -> dict | None:
    """Validated claims, or None. Never raises — an invalid token is an
    ordinary outcome on a public endpoint, not an error condition."""
    if not enabled() or not token:
        return None
    import jwt

    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        return None
    kid = header.get("kid")
    if not kid:
        return None
    key = _signing_key(kid)
    if key is None:
        return None
    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=_ALGORITHMS,          # pinned — see module docstring
            audience=config.AUTH0_AUDIENCE,
            issuer=issuer(),
        )
    except Exception:
        return None


def tenant_for_token(token: str) -> str | None:
    claims = claims_for_token(token)
    if not claims:
        return None
    sub = claims.get("sub")
    return tenant_for_sub(sub) if sub else None
