"""Component 26 (DESIGN.md §3e) — request bounds + rate limiting.

Two problems, one component.

**Unbounded request parameters.** `AskRequest.top_k` was a client-controlled
`int | None` with no ceiling. It flows into `retrieve()` -> `_build_moments()`,
which fetches one frame per citation from object storage and then puts every
one of them into a SINGLE multimodal LLM call. So `top_k: 10000` is a
request-amplification primitive that costs the operator real money on an
endpoint that needs no credentials. `question` and `video_ids` were likewise
unbounded.

**No rate limiting anywhere.** `/api/ask` and `/ask_stream` are public, do
retrieval + cross-encoder reranking + an LLM call, and nothing throttled them.
The metrics dashboard already had a "rate limited" counter wired to 429s that
the app could never actually emit (DESIGN.md §3c said so explicitly) — this is
the component that makes that counter mean something.

Rate limiting lives in app-level middleware because `src/api/videos.py` is
CLAUDE.md-protected and can't take a decorator, and it rides `src/cache.py`, so
it inherits that module's fail-open guarantee: no Redis (or a broken Redis)
means no limiting rather than a broken API. That trade-off is deliberate and
disclosed — availability over enforcement, matching every other degrade
decision in this codebase.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import cache, config, db


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl, nx=False):
        if nx and key in self.expiries:
            return False
        self.expiries[key] = ttl
        return True

    def pipeline(self):
        return _FakePipeline(self)

    def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v).encode()

    def set(self, key, value, ex=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class _FakePipeline:
    def __init__(self, client):
        self._c = client
        self._ops: list = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, ttl, nx=False):
        self._ops.append(("expire", key, ttl, nx))
        return self

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "incr":
                out.append(self._c.incr(op[1]))
            else:
                out.append(self._c.expire(op[1], op[2], nx=op[3]))
        self._ops.clear()
        return out


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX", 100)
    monkeypatch.setattr(config, "RATE_LIMIT_ASK_MAX", 3)
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_client", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    """Stub the whole ask path — this component is about admission control, not
    retrieval, and a real ask would need Qdrant + an LLM."""
    from src.rag import search as rag_search

    monkeypatch.setattr(rag_search, "ask", lambda *a, **k: {
        "question": "q", "citations": [], "answer": "stub",
        "llm_used": False, "abstained": True})
    from src.app import app
    return TestClient(app)


# ── Request bounds ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("top_k", [0, -1, 21, 1000, 10000])
def test_rejects_out_of_range_top_k(client, top_k):
    """The amplification primitive: each unit of top_k is one more storage
    fetch and one more image in a single LLM call."""
    resp = client.post("/api/ask", json={"question": "hi", "top_k": top_k})
    assert resp.status_code == 422, f"top_k={top_k} was accepted"


@pytest.mark.parametrize("top_k", [1, 6, 20])
def test_accepts_in_range_top_k(client, top_k):
    assert client.post("/api/ask", json={"question": "hi", "top_k": top_k}).status_code == 200


def test_accepts_omitted_top_k(client):
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 200


def test_rejects_overlong_question(client):
    resp = client.post("/api/ask", json={"question": "x" * 5000})
    assert resp.status_code == 422


def test_accepts_normal_length_question(client):
    assert client.post("/api/ask", json={"question": "x" * 500}).status_code == 200


def test_rejects_too_many_video_ids(client):
    resp = client.post("/api/ask", json={"question": "hi",
                                         "video_ids": [f"v{i}" for i in range(200)]})
    assert resp.status_code == 422


def test_rejects_overlong_question_on_ask_stream(client):
    assert client.get("/ask_stream", params={"q": "x" * 5000}).status_code == 422


# ── Rate limiting ────────────────────────────────────────────────────────────

def test_ask_burst_gets_429_with_retry_after(client):
    codes = [client.post("/api/ask", json={"question": "hi"}).status_code
             for _ in range(5)]           # RATE_LIMIT_ASK_MAX == 3
    assert codes[:3] == [200, 200, 200], codes
    assert codes[3] == 429 and codes[4] == 429, codes
    resp = client.post("/api/ask", json={"question": "hi"})
    assert resp.headers.get("Retry-After"), "429 must tell the client when to retry"


def test_ask_stream_shares_the_ask_budget(client):
    """Both ask endpoints hit the same expensive path, so one budget covers
    them — otherwise the cheaper limit is trivially doubled."""
    for _ in range(3):
        client.post("/api/ask", json={"question": "hi"})
    assert client.get("/ask_stream", params={"q": "hi"}).status_code == 429


def test_rotating_the_tenant_header_does_not_mint_a_fresh_budget(client):
    """spec-guardian finding: the first cut keyed on IP **and** tenant. But
    /api/ask needs no credentials and X-User-Id is unvalidated, so rotating
    that header handed out unlimited buckets — throttling honest clients while
    stopping no deliberate attacker. IP is the only dimension a caller can't
    trivially rotate, so it is the only one keyed on."""
    for i in range(3):
        client.post("/api/ask", json={"question": "hi"}, headers={"X-User-Id": f"t{i}"})
    assert client.post("/api/ask", json={"question": "hi"},
                       headers={"X-User-Id": "brand-new-tenant"}).status_code == 429


def test_forwarded_headers_separate_callers_behind_a_proxy(client, monkeypatch):
    """spec-guardian finding, and an availability regression rather than a
    security one: behind Fly's proxy `request.client.host` is the PROXY, so
    keying on it collapsed every anonymous caller into ONE bucket — 20
    requests from anybody would have starved the entire deployment."""
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    for _ in range(3):
        assert client.post("/api/ask", json={"question": "hi"},
                           headers={"Fly-Client-IP": "203.0.113.1"}).status_code == 200
    assert client.post("/api/ask", json={"question": "hi"},
                       headers={"Fly-Client-IP": "203.0.113.1"}).status_code == 429
    # A different real client must be unaffected by the first one's burst.
    assert client.post("/api/ask", json={"question": "hi"},
                       headers={"Fly-Client-IP": "203.0.113.9"}).status_code == 200


def test_forwarded_headers_ignored_when_not_behind_a_proxy(client, monkeypatch):
    """Trusting these unconditionally would be worse than the bug it fixes:
    any client could mint a fresh bucket per request by varying the header."""
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    codes = [client.post("/api/ask", json={"question": "hi"},
                         headers={"Fly-Client-IP": f"203.0.113.{i}"}).status_code
             for i in range(5)]
    assert codes[3] == 429 and codes[4] == 429, codes


def test_x_forwarded_for_uses_the_leftmost_entry(client, monkeypatch):
    """The left-most entry is the original client; everything after it is a
    proxy hop that must not be mistaken for the caller."""
    from src import security

    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    assert security.client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1, 10.0.0.2"},
                              "10.0.0.9") == "203.0.113.7"


def test_fly_client_ip_wins_over_x_forwarded_for(client, monkeypatch):
    """Fly sets Fly-Client-IP itself and a client cannot append to it, unlike
    X-Forwarded-For."""
    from src import security

    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    assert security.client_ip({"fly-client-ip": "203.0.113.5",
                               "x-forwarded-for": "198.51.100.1"}, "10.0.0.9") == "203.0.113.5"


def test_rejects_oversized_video_id_element(client):
    """max_length on a list bounds the COUNT, not each element — one 200KB
    string previously sailed through (spec-guardian)."""
    resp = client.post("/api/ask", json={"question": "hi", "video_ids": ["a" * 200000]})
    assert resp.status_code == 422


def test_cheap_endpoints_use_the_general_budget(client):
    """A health check must not be throttled out by the ask budget."""
    for _ in range(10):
        assert client.get("/api/health").status_code == 200


def test_no_limiting_when_redis_url_unset(client, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "")
    codes = [client.post("/api/ask", json={"question": "hi"}).status_code for _ in range(6)]
    assert codes == [200] * 6, codes


def test_no_limiting_when_disabled_by_flag(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    codes = [client.post("/api/ask", json={"question": "hi"}).status_code for _ in range(6)]
    assert codes == [200] * 6, codes


def test_fails_open_when_redis_errors(client, monkeypatch):
    """A Redis outage must degrade to unlimited, never to a broken API — the
    same trade-off src/cache.py makes everywhere else."""
    class _Broken:
        def __getattr__(self, name):
            def _boom(*a, **k):
                import redis
                raise redis.RedisError("down")
            return _boom

    monkeypatch.setattr(cache, "_client", lambda: _Broken())
    codes = [client.post("/api/ask", json={"question": "hi"}).status_code for _ in range(6)]
    assert codes == [200] * 6, codes


def test_window_gets_an_expiry_so_buckets_reset(_limits, client):
    """Without a TTL the counter would never reset and the first burst would
    ban that caller permanently."""
    client.post("/api/ask", json={"question": "hi"})
    assert _limits.expiries, "the rate-limit key was created with no expiry"
    assert all(t == config.RATE_LIMIT_WINDOW_S for t in _limits.expiries.values())
