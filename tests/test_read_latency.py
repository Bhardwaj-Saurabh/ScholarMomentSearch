"""Component 60 (DESIGN.md §3m) — read-latency polish.

Two measured wastes on every answered request:
  * the question was embedded twice — once for the text-branch confidence
    gate, once again for the branch search itself (two real clip-service
    HTTP calls when the embed cache is cold or Redis is off);
  * the named-source attribution backstop read the tenant's ENTIRE
    videos+documents tables via two `SELECT *` scans (~920.8ms measured in a
    production trace) to use exactly two columns.
"""
from __future__ import annotations

import numpy as np

from src import config, db
from src.rag import search as rag_search


def _stub(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TRANSCRIPT", True)
    monkeypatch.setattr(config, "QUERY_ENHANCEMENT_ENABLED", False)
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(config, "GRAPH_RETRIEVAL_ENABLED", False)
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search.vector_store, "search", lambda *a, **k: [])
    monkeypatch.setattr(rag_search.vector_store, "search_text",
                        lambda *a, **k: [{"source_id": "doc_a", "kind": "paper",
                                          "page": 1, "chunk": 0, "text": "t", "score": 0.5}])


def test_question_is_embedded_exactly_once_per_retrieve(monkeypatch):
    _stub(monkeypatch)
    calls = []

    def _counting_embed(q):
        calls.append(q)
        return np.zeros(4, dtype=np.float32)

    monkeypatch.setattr(rag_search, "embed_query", _counting_embed)
    rag_search._retrieve_impl("what is attention?", "u_test")
    assert calls.count("what is attention?") == 1, (
        f"question embedded {calls.count('what is attention?')}x — gate and "
        "branch must share one embedding")


def test_list_source_titles_returns_title_and_kind_only():
    db.init_schema()
    db.upsert_pending_document({"id": "doc_c60", "user_id": "u_c60", "kind": "paper",
                                "uri": "https://example.com/x.pdf", "storage_key": None,
                                "source_hash": None, "title": "A Great Paper"})
    try:
        rows = db.list_source_titles("u_c60")
        assert {"title": "A Great Paper", "kind": "paper"} in [
            {"title": r["title"], "kind": r["kind"]} for r in rows]
        # tenancy: another tenant sees nothing
        assert db.list_source_titles("u_someone_else") == []
    finally:
        db.delete_document("doc_c60")


def test_attribution_backstop_uses_the_projected_query_not_select_star(monkeypatch):
    """The backstop needs title+kind; it must no longer pull every column of
    every row via list_sources()."""
    used = {}

    def _titles(uid):
        used["titles"] = True
        return [{"title": "Some Other Paper", "kind": "paper"}]

    def _full_scan(uid):
        raise AssertionError("attribution backstop must not call list_sources()")

    monkeypatch.setattr(rag_search.db, "list_source_titles", _titles, raising=False)
    monkeypatch.setattr(rag_search.db, "list_sources", _full_scan)
    out = rag_search._check_named_source_attribution(
        "the answer cites [1] only.", [{"title": "A Cited Paper"}], "u_test")
    assert used.get("titles"), "must read titles via the projected query"
    assert out == "the answer cites [1] only."
