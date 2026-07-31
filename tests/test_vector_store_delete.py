"""Component 34 (DESIGN.md §3e) — delete_document_chunks(raise_on_error=).

Existing callers (re-embedding before a re-run, tests/test_hybrid_search.py's
teardown-style calls) rely on the current fail-open behavior — a transient
Qdrant hiccup during re-embed shouldn't crash the whole ingest flow. The new
admin DELETE route needs the opposite: a purge failure must be visible so the
route can refuse to delete the row and leave orphaned vectors behind a
successful-looking response. `raise_on_error` (default False) keeps both
behaviors without duplicating the function. Fully mocked client() — no real
Qdrant needed, this is pure error-handling logic.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.rag import vector_store


def test_delete_document_chunks_swallows_by_default(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("Qdrant unreachable")
    fake_client = MagicMock()
    fake_client.delete = _boom
    monkeypatch.setattr(vector_store, "client", lambda: fake_client)

    vector_store.delete_document_chunks("u1", "doc_1")  # must not raise


def test_delete_document_chunks_raises_when_raise_on_error_true(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("Qdrant unreachable")
    fake_client = MagicMock()
    fake_client.delete = _boom
    monkeypatch.setattr(vector_store, "client", lambda: fake_client)

    try:
        vector_store.delete_document_chunks("u1", "doc_1", raise_on_error=True)
        assert False, "expected the underlying error to propagate"
    except RuntimeError:
        pass


def test_delete_document_chunks_raise_on_error_true_still_succeeds_on_success(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(vector_store, "client", lambda: fake_client)

    vector_store.delete_document_chunks("u1", "doc_1", raise_on_error=True)  # must not raise
    assert fake_client.delete.called
