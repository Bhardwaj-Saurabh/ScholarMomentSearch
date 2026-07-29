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

from fastapi import Header, HTTPException

from . import cache, config

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

# Environments where an unset ADMIN_TOKEN is tolerated. Everything ELSE fails
# closed — the check is deliberately inverted (allowlist of dev names) rather
# than `ENV == "production"`, which spec-guardian flagged: that form silently
# failed OPEN for `prod`, `prd`, `staging`, or a typo. A safety default that
# depends on spelling one magic word correctly is not a safety default.
_DEV_ENVS = frozenset({"development", "dev", "local", "test", "testing"})


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


def bearer(authorization: str | None) -> str:
    """The raw credential out of an `Authorization: Bearer …` header."""
    if not authorization or not authorization.startswith(_BEARER):
        return ""
    return authorization[len(_BEARER):].strip()


def resolve_tenant(authorization: str | None) -> str | None:
    """The authenticated tenant for this request, or None if the caller isn't
    a logged-in user (component 43).

    Precedence is deliberately narrow: ONLY a valid Auth0 access token yields a
    tenant here. The admin token intentionally does NOT, because it must keep
    honoring a caller-supplied `X-User-Id` — `benchmark/bench.py` and
    `eval/eval.py` select tenants that way and the graded SLA gates would break
    otherwise. That makes the admin token an operator/machine credential which
    is cross-tenant by design, never a user login.
    """
    from . import auth0

    if not auth0.enabled():
        return None
    return auth0.tenant_for_token(bearer(authorization))


def require_auth_dep(authorization: str | None = Header(default=None)) -> None:
    """Route-level replacement for `src/api/videos.py::require_auth`, installed
    via FastAPI's `dependency_overrides` (component 43).

    Why this exists: that protected dependency compares the bearer against
    `ADMIN_TOKEN` specifically, so once a SECOND valid credential type (a user
    JWT) exists it rejects legitimately-signed-in users at the route — after
    the middleware has already allowed them. Component 25 called that leftover
    dependency "redundant, harmless"; it stops being harmless the moment a
    second credential type appears.

    `dependency_overrides` keys on the function object, and `admin.py` and
    `search.py` both import that same object, so one override covers every
    router without editing the protected file.

    Behavior is a strict superset of the original: a valid Auth0 token passes,
    and everything else follows the inherited admin-token rules exactly.
    """
    if resolve_tenant(authorization) is not None:
        return
    if not config.ADMIN_TOKEN:
        return          # inherited dev-convenience; middleware fails closed in prod
    if not token_ok(authorization):
        raise HTTPException(401, "Missing or invalid bearer token.")


def force_user_id(scope: dict, tenant: str) -> None:
    """Overwrite `X-User-Id` in the raw ASGI scope with the authenticated
    tenant, before routing.

    This is how a JWT identity reaches the request handlers WITHOUT editing
    `src/api/videos.py`, which is CLAUDE.md-protected. It also solves a second
    problem a dependency override would not: there are TWO independent tenancy
    implementations in this codebase — that protected `user_id()` dependency
    and `src/api/search.py::_uid()` — and both read this same header, so
    rewriting it covers both uniformly and can't drift apart later.

    Any client-supplied value is DROPPED, not merged. If the header could still
    win, the spoof this component exists to close would still be open.
    """
    headers = [(k, v) for (k, v) in scope["headers"] if k.lower() != b"x-user-id"]
    headers.append((b"x-user-id", tenant.encode()))
    scope["headers"] = headers


def auth_failure(method: str, path: str, authorization: str | None) -> tuple[int, str] | None:
    """(status, detail) when the request must be refused, else None."""
    if not requires_auth(method, path):
        return None
    # Component 43: a valid user login is sufficient for a mutation — the admin
    # token is no longer the only way in. Checked first so a signed-in user is
    # never refused just because ADMIN_TOKEN happens to be unset.
    if resolve_tenant(authorization) is not None:
        return None
    if not config.ADMIN_TOKEN:
        if config.ENV not in _DEV_ENVS:
            # Deliberately 503, not 401: the client did nothing wrong — the
            # server is missing a secret it requires. Failing closed here is
            # the entire point; silently serving would be the old behavior.
            return (503, "Server is missing ADMIN_TOKEN — refusing to serve "
                         "protected routes outside a development environment.")
        return None      # dev convenience, matching the inherited behavior
    if not token_ok(authorization):
        return (401, "Missing or invalid bearer token.")
    return None


# ── Rate limiting (DESIGN.md §3e component 26) ───────────────────────────────
# Both ask endpoints share ONE budget: they run the same expensive path
# (retrieval + rerank + an LLM call), so giving them separate counters would
# let a caller trivially double the cheaper limit by alternating between them.
_ASK_PATHS = frozenset({"/api/ask", "/ask_stream"})


def _bucket(path: str) -> tuple[str, int]:
    """(bucket name, max requests per window) for this path."""
    if path.rstrip("/") in _ASK_PATHS:
        return ("ask", config.RATE_LIMIT_ASK_MAX)
    return ("gen", config.RATE_LIMIT_MAX)


def client_ip(headers, peer: str | None) -> str:
    """The caller's real address.

    `request.client.host` is the PEER, which behind Fly's proxy is the proxy
    itself — spec-guardian caught that keying on it would collapse every
    anonymous caller on the deployment into ONE bucket, so 20 requests from
    anybody would starve all other users. That is an availability regression,
    not a security control, so the forwarded headers have to be read.

    They are only trusted when `TRUST_PROXY_HEADERS` is on (default: on
    outside the dev allowlist, since that is exactly where a proxy sits).
    Trusting them unconditionally would be worse than the bug: any client
    could then mint a fresh bucket per request just by varying the header.
    `Fly-Client-IP` is preferred over `X-Forwarded-For` because Fly sets it
    itself and it cannot be appended to by the client.
    """
    if config.TRUST_PROXY_HEADERS:
        fly = (headers.get("fly-client-ip") or "").strip()
        if fly:
            return fly
        # Left-most entry is the original client; the rest are proxy hops.
        xff = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
    return peer or "unknown"


def rate_limit_check(path: str, ip: str) -> int | None:
    """Returns Retry-After seconds when this request should be refused, else
    None.

    Keyed on IP ALONE, deliberately. The first cut also keyed on the tenant,
    which spec-guardian correctly called a free bypass: `/api/ask` needs no
    credentials and `X-User-Id` is unvalidated, so rotating that header handed
    out unlimited fresh buckets. The combination was the worst of both worlds —
    it throttled honest clients while stopping no deliberate attacker. IP is
    the only dimension a caller cannot trivially rotate, so it is the only one
    used. The cost is that distinct tenants behind one NAT share a budget,
    which is the normal trade-off of IP-based limiting and is the right way
    round: shared-but-enforced beats separate-but-bypassable.

    Fails OPEN in every uncertain case — disabled by flag, no Redis, or
    `incr_with_expiry` returning None because Redis is unreachable. A Redis
    outage degrades to unlimited rather than to a dead API, consistent with
    `src/cache.py`'s contract and the Qdrant-down-at-boot precedent.
    """
    if not config.RATE_LIMIT_ENABLED or not cache.enabled():
        return None
    name, limit = _bucket(path)
    count = cache.incr_with_expiry(f"rl:{name}:{ip}", config.RATE_LIMIT_WINDOW_S)
    if count is None:          # couldn't count -> don't limit
        return None
    return config.RATE_LIMIT_WINDOW_S if count > limit else None
