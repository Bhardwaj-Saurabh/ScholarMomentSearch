"""Seeding vector-integrity verification — DESIGN.md §3j component 51.

Found on 2026-07-29: every corpus-seeded paper and deck was `status='indexed'`
in Postgres with real chunk counts while `TEXT_COLLECTION` held zero of their
vectors. Root cause: `_not_indexed_documents()` trusted the status column
alone. These tests pin the fix — a source only counts as "done" when the
status says so AND its vectors actually exist — using fakes so no live stack
is required.
"""
from __future__ import annotations

from src import seeding


class _FakeDb:
    def __init__(self, docs: dict[str, dict], videos: dict[str, dict]):
        self._docs = docs
        self._videos = videos

    def get_document(self, doc_id):
        return self._docs.get(doc_id)

    def get_video(self, video_id):
        return self._videos.get(video_id)


class _FakeVectorStore:
    """Maps source_id -> chunk count. Missing key = 0, matching a source that
    was never indexed at all."""

    def __init__(self, counts: dict[str, int], raise_for: set[str] | None = None):
        self._counts = counts
        self._raise_for = raise_for or set()

    def count_document_chunks(self, user_id, source_id):
        if source_id in self._raise_for:
            raise RuntimeError("qdrant unreachable")
        return self._counts.get(source_id, 0)

    def count_video_chunks(self, user_id, video_id):
        if video_id in self._raise_for:
            raise RuntimeError("qdrant unreachable")
        return self._counts.get(video_id, 0)


DOC = {"id": "doc_seed_x_paper", "kind": "paper",
       "uri": "storage://docs/x.pdf", "title": "X"}


def test_indexed_status_but_zero_vectors_is_still_not_indexed(monkeypatch):
    """The exact incident: status says indexed, vectors say otherwise."""
    monkeypatch.setattr(seeding, "db",
                        _FakeDb({DOC["id"]: {**DOC, "status": "indexed"}}, {}))
    monkeypatch.setattr(seeding, "vector_store", _FakeVectorStore({}))
    monkeypatch.setattr(seeding, "_corpus_documents", lambda: [DOC])
    monkeypatch.setattr(seeding.config, "SEED_CORPUS", True)

    assert seeding._not_indexed_documents() == [DOC]


def test_indexed_status_with_real_vectors_is_skipped(monkeypatch):
    """The common case must not regress: an already-correct source is not
    needlessly re-embedded on every boot."""
    monkeypatch.setattr(seeding, "db",
                        _FakeDb({DOC["id"]: {**DOC, "status": "indexed"}}, {}))
    monkeypatch.setattr(seeding, "vector_store",
                        _FakeVectorStore({DOC["id"]: 42}))
    monkeypatch.setattr(seeding, "_corpus_documents", lambda: [DOC])
    monkeypatch.setattr(seeding.config, "SEED_CORPUS", True)

    assert seeding._not_indexed_documents() == []


def test_non_indexed_status_is_selected_regardless_of_vectors(monkeypatch):
    """A `pending`/`failed` row must still be selected even if some stray
    vectors happen to exist — status is still part of the check, not replaced
    by it."""
    monkeypatch.setattr(seeding, "db",
                        _FakeDb({DOC["id"]: {**DOC, "status": "pending"}}, {}))
    monkeypatch.setattr(seeding, "vector_store",
                        _FakeVectorStore({DOC["id"]: 5}))
    monkeypatch.setattr(seeding, "_corpus_documents", lambda: [DOC])
    monkeypatch.setattr(seeding.config, "SEED_CORPUS", True)

    assert seeding._not_indexed_documents() == [DOC]


def test_qdrant_error_fails_open_toward_reseeding(monkeypatch):
    """Fail-open direction matters here: an UNCERTAIN 'maybe missing' must
    re-seed, not silently trust a possibly-stale status. Re-seeding an
    already-correct source is idempotent (upsert deletes-then-inserts); trusting
    a wrong status is what caused the incident this component exists for."""
    monkeypatch.setattr(seeding, "db",
                        _FakeDb({DOC["id"]: {**DOC, "status": "indexed"}}, {}))
    monkeypatch.setattr(seeding, "vector_store",
                        _FakeVectorStore({}, raise_for={DOC["id"]}))
    monkeypatch.setattr(seeding, "_corpus_documents", lambda: [DOC])
    monkeypatch.setattr(seeding.config, "SEED_CORPUS", True)

    assert seeding._not_indexed_documents() == [DOC]


def test_missing_row_is_still_selected(monkeypatch):
    """No row at all (never seeded) must still be selected — the new vector
    check is additive, not a replacement for the existing None-row case."""
    monkeypatch.setattr(seeding, "db", _FakeDb({}, {}))
    monkeypatch.setattr(seeding, "vector_store", _FakeVectorStore({}))
    monkeypatch.setattr(seeding, "_corpus_documents", lambda: [DOC])
    monkeypatch.setattr(seeding.config, "SEED_CORPUS", True)

    assert seeding._not_indexed_documents() == [DOC]


VIDEO = {"url": "https://youtu.be/abc123", "title": "Talk"}


def test_video_indexed_status_but_zero_frames_is_still_not_indexed(monkeypatch):
    from src.samples import sample_video_id

    vid = sample_video_id(VIDEO["url"])
    monkeypatch.setattr(seeding, "db", _FakeDb({}, {vid: {"status": "indexed"}}))
    monkeypatch.setattr(seeding, "vector_store", _FakeVectorStore({}))
    monkeypatch.setattr(seeding, "_all_videos", lambda: [VIDEO])

    assert seeding._not_indexed_videos() == [VIDEO]


def test_video_indexed_status_with_real_frames_is_skipped(monkeypatch):
    from src.samples import sample_video_id

    vid = sample_video_id(VIDEO["url"])
    monkeypatch.setattr(seeding, "db", _FakeDb({}, {vid: {"status": "indexed"}}))
    monkeypatch.setattr(seeding, "vector_store", _FakeVectorStore({vid: 30}))
    monkeypatch.setattr(seeding, "_all_videos", lambda: [VIDEO])

    assert seeding._not_indexed_videos() == []
