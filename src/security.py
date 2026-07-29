"""Auth enforcement, applied as app-level middleware — DESIGN.md §3e
component 25.

`src/api/videos.py::require_auth` is the inherited check and is
CLAUDE.md-protected, so it cannot be edited. It has three problems:

  1. **Fails open.** `if not ADMIN_TOKEN: return` — an unset or empty token
     makes every "protected" route fully public. That is a reasonable dev
     convenience and an unacceptable production posture, and nothing in the
     code distinguished the two.
  2. **Not constant-time.** A plain `!=` on the token string.
  3. It only runs where a route remembered to declare
     `Depends(require_auth)`. `GET /api/llm` never did, and it hands back
     provider, model, base_url and a key hint for whatever tenant the
     (unauthenticated, spoofable) X-User-Id header names.

Fixing all three additively means enforcing here, ahead of routing. That also
turns "a new route forgot its Depends" from a latent vulnerability into a
structural impossibility for anything under a protected prefix. The existing
route-level dependencies stay exactly as they are — redundant now, harmless,
and it keeps the protected file untouched.

Config is read through the `config` module at call time (not imported as
constants at module load) so a running process picks up monkeypatched values
in tests, matching how `src/cache.py` reads `REDIS_URL`.

SCOPE BOUNDARY, deliberate: this does NOT gate the read endpoints
`GET /api/videos` and `GET /admin/sources`, nor `/api/ask`. They are public
today and stay public, because the browser UI sends no Authorization header on
anything — wiring that up is component 27, which depends on this. Tenancy also
remains `X-User-Id`, i.e. data partitioning rather than a security boundary
(DESIGN.md §3e records that as an accepted, documented limitation).
"""
from __future__ import annotations

import hmac

from . import config

# Any non-safe method under these prefixes needs the admin token.
_PROTECTED_PREFIXES = ("/api/videos", "/admin", "/api/llm")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reads that expose operational or credential-shaped data and therefore need
# the token despite being GETs.
_PROTECTED_READS = frozenset({
    "/api/llm",          # provider/model/base_url + api-key hint, per tenant
    "/metrics",          # Prometheus: cost, tokens, traffic
    "/admin/metrics",    # same, as JSON
})

_BEARER = "Bearer "


def token_ok(authorization: str | None) -> bool:
    """Constant-time bearer check. The scheme prefix is matched exactly —
    `bearer `/`Token `/`Basic ` are all rejected rather than normalized, since
    nothing in this system legitimately sends them."""
    expected = config.ADMIN_TOKEN
    if not expected or not authorization or not authorization.startswith(_BEARER):
        return False
    return hmac.compare_digest(authorization[len(_BEARER):], expected)


def requires_auth(method: str, path: str) -> bool:
    path = path.rstrip("/") or "/"
    if path in _PROTECTED_READS:
        return True
    if method.upper() in _SAFE_METHODS:
        return False
    return any(path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES)


def auth_failure(method: str, path: str, authorization: str | None) -> tuple[int, str] | None:
    """(status, detail) when the request must be refused, else None."""
    if not requires_auth(method, path):
        return None
    if not config.ADMIN_TOKEN:
        if config.ENV == "production":
            # Deliberately 503, not 401: the client did nothing wrong — the
            # server is missing a secret it requires. Failing closed here is
            # the entire point; silently serving would be the old behavior.
            return (503, "Server is missing ADMIN_TOKEN — refusing to serve "
                         "protected routes in production.")
        return None      # dev convenience, matching the inherited behavior
    if not token_ok(authorization):
        return (401, "Missing or invalid bearer token.")
    return None
