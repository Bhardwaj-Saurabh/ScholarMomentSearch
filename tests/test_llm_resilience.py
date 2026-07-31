"""Component 58 (DESIGN.md §3m) — LLM call resilience.

Every provider path used to call the API bare: no timeout (SDK default 600s —
longer than the whole request budget), no deliberate retry policy, a fresh
client (new TLS handshake) per call, and any provider exception surfaced as a
raw 500. The two HTTP-0 zeros in the last recall@10 run trace exactly here.

Contract (CLAUDE.md row 32): mocked 429-then-success => one answer; provider
failure => 502, not raw 500; /ask_stream emits a terminal error event.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config, llm
from src.rag import search as rag_search


class _FakeResponse:
    class _Msg:
        def __init__(self, text):
            self.content = text

    class _Choice:
        def __init__(self, text):
            self.message = _FakeResponse._Msg(text)

    def __init__(self, text="grounded answer [1]"):
        self.choices = [self._Choice(text)]
        self.usage = None


class _RetryableError(Exception):
    status_code = 429


class _FatalError(Exception):
    status_code = 401


class _FlakyClient:
    """Raises `fail_times` retryable errors, then succeeds."""

    def __init__(self, fail_times, exc_factory=_RetryableError):
        self.calls = 0
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory("upstream unhappy")
        return _FakeResponse()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


CFG = llm.LLMConfig(provider="openai", model="test-model", api_key="k")
MOMENTS = [{"image": None, "transcript": "the excerpt", "timestamp": "00:01",
            "source": "A Paper"}]


# ── Retry policy ─────────────────────────────────────────────────────────────

def test_429_then_success_returns_one_answer(monkeypatch):
    client = _FlakyClient(fail_times=1)
    monkeypatch.setattr(llm, "_openai_client", lambda *a, **k: client)
    out = llm.answer("q?", MOMENTS, CFG)
    assert out == "grounded answer [1]"
    assert client.calls == 2  # one failure, one retry, ONE answer


def test_retries_are_bounded_and_exhaustion_raises_llm_unavailable(monkeypatch):
    client = _FlakyClient(fail_times=99)
    monkeypatch.setattr(llm, "_openai_client", lambda *a, **k: client)
    with pytest.raises(llm.LLMUnavailable):
        llm.answer("q?", MOMENTS, CFG)
    assert client.calls == 1 + len(llm._RETRY_DELAYS_S)


def test_non_retryable_provider_error_fails_fast(monkeypatch):
    """A 401 (bad key) will not get better on retry — one call, raised as-is
    so the settings ping can show the provider's real message."""
    client = _FlakyClient(fail_times=99, exc_factory=_FatalError)
    monkeypatch.setattr(llm, "_openai_client", lambda *a, **k: client)
    with pytest.raises(_FatalError):
        llm.answer("q?", MOMENTS, CFG)
    assert client.calls == 1


# ── Client caching + timeout ─────────────────────────────────────────────────

def test_openai_client_is_cached_per_config(monkeypatch):
    import openai

    constructions = []

    def _counting(**kwargs):
        constructions.append(kwargs)

        class _C:
            pass

        return _C()

    monkeypatch.setattr(openai, "OpenAI", _counting)
    llm._openai_client.cache_clear()
    llm._openai_client("key-1", None)
    llm._openai_client("key-1", None)
    assert len(constructions) == 1, "same config must reuse one client"
    llm._openai_client("key-2", None)
    assert len(constructions) == 2, "a different config gets its own client"
    llm._openai_client.cache_clear()


def test_openai_client_sets_an_explicit_timeout(monkeypatch):
    import openai

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)

        class _C:
            pass

        return _C()

    monkeypatch.setattr(openai, "OpenAI", _capture)
    llm._openai_client.cache_clear()
    llm._openai_client("key-1", None)
    assert seen.get("timeout") == config.LLM_TIMEOUT_S
    assert seen.get("max_retries") == 0, "retries live in OUR wrapper, not the SDK"
    llm._openai_client.cache_clear()


# ── API mapping: 502 + terminal SSE error event ──────────────────────────────

@pytest.fixture
def api_client():
    from src.app import app
    return TestClient(app, raise_server_exceptions=False)


def test_api_ask_maps_llm_unavailable_to_502(api_client, monkeypatch):
    from src.api import search as search_api
    monkeypatch.setattr(search_api.rag_search, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(
                            llm.LLMUnavailable("provider melted")))
    resp = api_client.post("/api/ask", json={"question": "what is attention?"})
    assert resp.status_code == 502
    assert "detail" in resp.json()


def test_ask_stream_emits_a_terminal_error_event(api_client, monkeypatch):
    from src.api import search as search_api
    monkeypatch.setattr(search_api.rag_search, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(
                            llm.LLMUnavailable("provider melted")))
    resp = api_client.get("/ask_stream", params={"q": "what is attention?"})
    assert resp.status_code == 200  # headers were already sent — SSE contract
    body = resp.text
    assert "event: error" in body
    assert body.rstrip().endswith("data: {}") or "event: done" not in body.split("event: error")[1]


def test_rag_layer_wraps_any_provider_exception_as_llm_unavailable(monkeypatch):
    """The read path maps EVERY llm.answer failure to the typed error, so the
    API layer has exactly one thing to catch."""
    import numpy as np

    monkeypatch.setattr(config, "ENABLE_TRANSCRIPT", True)
    monkeypatch.setattr(config, "QUERY_ENHANCEMENT_ENABLED", False)
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search, "embed_query", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search.vector_store, "search",
                        lambda *a, **k: [{"video_id": "v1", "idx": 3, "ms": 1000, "score": 0.9}])
    monkeypatch.setattr(rag_search.vector_store, "search_text",
                        lambda *a, **k: [{"source_id": "doc_a", "kind": "paper", "page": 7,
                                          "text": "self-attention", "score": 0.9}])
    monkeypatch.setattr(rag_search.db, "videos_by_ids", lambda ids: {})
    monkeypatch.setattr(rag_search.db, "documents_by_ids",
                        lambda ids: {"doc_a": {"title": "Attention"}})
    monkeypatch.setattr(rag_search.storage, "get_bytes", lambda k: None)
    monkeypatch.setattr(rag_search, "resolve_llm",
                        lambda uid: (llm.LLMConfig(model="m", api_key="k"), "env"))
    monkeypatch.setattr(rag_search.llm, "answer",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("weird SDK crash")))
    with pytest.raises(llm.LLMUnavailable):
        rag_search.ask("what is attention?", "u_test")
