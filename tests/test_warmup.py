"""Component 55 (DESIGN.md §3m) — startup model warm-up.

The two lazy in-process models are loaded on the FIRST request that needs
them: the cross-encoder reranker (measured 5725.9ms cold in-process, 68s
worst-case first-ever call including the model download) and the BM25 sparse
embedder. On a `min_machines_running=0` deployment that cost lands on a real
user's query; component 55 moves it to boot — in a background thread, so a
slow model download can never block the app from serving or fail a health
check, and behind the same fail-open rule as everything else at startup:
a warm-up that raises must never take the process down.
"""
from __future__ import annotations

from src import config
from src.rag import embeddings, rerank


# ── rerank.warm() ─────────────────────────────────────────────────────────────

def test_rerank_warm_loads_the_model(monkeypatch):
    monkeypatch.setattr(config, "RERANK_ENABLED", True)
    calls = []
    monkeypatch.setattr(rerank, "_model", lambda: calls.append(1))
    rerank.warm()
    assert calls, "warm() must actually trigger the lazy model load"


def test_rerank_warm_skipped_when_disabled(monkeypatch):
    """Never load a model the read path will never use."""
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    calls = []
    monkeypatch.setattr(rerank, "_model", lambda: calls.append(1))
    rerank.warm()
    assert not calls


def test_rerank_warm_never_raises(monkeypatch):
    monkeypatch.setattr(config, "RERANK_ENABLED", True)

    def _boom():
        raise RuntimeError("model download failed")

    monkeypatch.setattr(rerank, "_model", _boom)
    rerank.warm()  # must not raise


# ── embeddings.warm_sparse() ──────────────────────────────────────────────────

def test_sparse_warm_loads_the_model(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_HYBRID_TEXT_SEARCH", True)
    calls = []
    monkeypatch.setattr(embeddings, "_sparse_model", lambda: calls.append(1))
    embeddings.warm_sparse()
    assert calls


def test_sparse_warm_skipped_when_hybrid_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_HYBRID_TEXT_SEARCH", False)
    calls = []
    monkeypatch.setattr(embeddings, "_sparse_model", lambda: calls.append(1))
    embeddings.warm_sparse()
    assert not calls


def test_sparse_warm_never_raises(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_HYBRID_TEXT_SEARCH", True)

    def _boom():
        raise RuntimeError("fastembed unavailable")

    monkeypatch.setattr(embeddings, "_sparse_model", _boom)
    embeddings.warm_sparse()  # must not raise


# ── app-level wiring ──────────────────────────────────────────────────────────

def test_app_warm_models_survives_both_warms_raising(monkeypatch):
    """The lifespan runs _warm_models in a daemon thread; even called inline
    with every underlying warm broken it must never raise."""
    from src import app as app_module

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rerank, "warm", _boom)
    monkeypatch.setattr(embeddings, "warm_sparse", _boom)
    app_module._warm_models()  # must not raise
