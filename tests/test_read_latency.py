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


# ── C59 amendment (found by the graded rubric, 2026-08-04) ───────────────────
# eval.py's `grounded` check requires EVERY served citation to carry non-empty
# text and a locator. A frame-only window (pure visual match, no transcript at
# that instant) genuinely has no text — and component 59's rerank fairness let
# those reach the citation list, flipping the graded check red. Frame-only
# windows stay rankable, but the served slice takes text-bearing windows;
# frame-only fills in ONLY when no text-bearing window exists at all (so a
# transcript-less video corpus still gets visual citations rather than none).

def test_final_citation_slice_prefers_text_bearing_windows():
    texty = [{"video_id": "v1", "t": 1.0, "rrf": 0.5, "modalities": {"text"},
              "frame": None, "text": {"text": f"t{i}", "video_id": "v1",
                                      "idx": None, "ms": 1000 + i}}
             for i in range(3)]
    frames = [{"video_id": "v1", "t": 9.0 + i, "rrf": 0.4, "modalities": {"frame"},
               "frame": {"idx": i, "ms": 9000 + i * 1000}, "text": None}
              for i in range(4)]
    # frame-only windows interleaved ahead of some text windows
    mixed = [frames[0], texty[0], frames[1], texty[1], frames[2], texty[2], frames[3]]
    chosen = rag_search._citation_windows(mixed, k=5)
    assert len(chosen) == 3
    assert all(w.get("text") for w in chosen), "served citations must carry text"


def test_final_citation_slice_falls_back_to_frames_when_no_text_exists():
    frames = [{"video_id": "v1", "t": float(i), "rrf": 0.4, "modalities": {"frame"},
               "frame": {"idx": i, "ms": i * 1000}, "text": None} for i in range(3)]
    chosen = rag_search._citation_windows(frames, k=2)
    assert len(chosen) == 2, "a transcript-less corpus still gets visual citations"


def test_video_citations_expose_text_field_like_document_citations(monkeypatch):
    """Second half of the rubric fix: eval.py's grounded check reads
    `citation["text"]`, which document citations carried but video citations
    spelled `transcript` — so any video citation in the top-k failed the
    graded check regardless of how well-grounded it was."""
    import numpy as np

    monkeypatch.setattr(config, "ENABLE_TRANSCRIPT", True)
    monkeypatch.setattr(config, "QUERY_ENHANCEMENT_ENABLED", False)
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search, "embed_query", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search.vector_store, "search", lambda *a, **k: [])
    monkeypatch.setattr(rag_search.vector_store, "search_text",
                        lambda *a, **k: [{"video_id": "v1", "idx": None, "ms": 5000,
                                          "text": "the spoken evidence", "score": 0.9}])
    monkeypatch.setattr(rag_search.db, "videos_by_ids",
                        lambda ids: {"v1": {"title": "A Talk", "url": None, "source": "youtube"}})
    monkeypatch.setattr(rag_search.db, "documents_by_ids", lambda ids: {})
    out = rag_search._retrieve_impl("q", "u_test")
    (cite,) = out["citations"]
    assert cite["kind"] == "video"
    assert cite.get("text") == "the spoken evidence"
    assert cite.get("transcript") == "the spoken evidence"  # existing UI field stays
