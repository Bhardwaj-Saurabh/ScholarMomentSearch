"""Component 20 (DESIGN.md §3d) — Tier 2 mechanical caches: exact-match
query-embedding cache (CLIP/bge/BM25), frame-bytes cache, and the db.py
poll-read cache (list_videos/list_documents/list_sources). All deterministic
and content-addressed -- these tests prove a repeat call skips the expensive
underlying work (mocked call-count), not that the cache "feels" faster.
"""
from __future__ import annotations

import uuid

import numpy as np
import pytest

from src import cache, config, db
from src.rag import embeddings, search


class _FakeRedis:
    """In-memory stand-in for redis.Redis, shared by every test in this file
    (mirrors tests/test_cache.py's fake -- kept local to avoid cross-file
    test coupling)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value if isinstance(value, bytes) else value.encode()

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, b"0")) + 1).encode()
        return int(self.store[key])

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _enable_cache(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "redis://test:6379/0")
    fake = _FakeRedis()  # ONE instance for the whole test -- a fresh lambda
    monkeypatch.setattr(cache, "_client", lambda: fake)  # per call would reset the store every time


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture
def cleanup():
    """Rows created here must not leak into the shared Postgres test DB across
    runs -- an all-tenant scan elsewhere (db.stale_documents(), used by
    tests/test_reconciler.py) would otherwise pick up a leftover 'pending'
    document once it's aged past RECONCILE_STALE_AFTER_S in wall-clock time."""
    video_ids, doc_ids = [], []
    yield video_ids, doc_ids
    for i in video_ids:
        db.delete_video(i)
    for i in doc_ids:
        db.delete_document(i)


# ── Query-embedding cache ────────────────────────────────────────────────────

def test_embed_text_skips_recompute_on_repeat(monkeypatch):
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    calls = []

    def fake_local(text):
        calls.append(text)
        return np.array([1.0, 2.0, 3.0], dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_text_local", fake_local)
    v1 = embeddings.embed_text("hello world")
    v2 = embeddings.embed_text("hello world")
    assert len(calls) == 1
    np.testing.assert_array_almost_equal(v1, v2)


def test_embed_text_different_strings_both_compute(monkeypatch):
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    calls = []
    monkeypatch.setattr(embeddings, "embed_text_local",
                        lambda t: calls.append(t) or np.array([1.0], dtype=np.float32))
    embeddings.embed_text("a")
    embeddings.embed_text("b")
    assert calls == ["a", "b"]


def test_embed_query_skips_recompute_on_repeat(monkeypatch):
    monkeypatch.setattr(config, "TEXT_EMBED_PROVIDER", "fastembed")
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    calls = []

    def fake_local(text):
        calls.append(text)
        return np.array([0.5, 0.5], dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_query_local", fake_local)
    v1 = embeddings.embed_query("what is attention?")
    v2 = embeddings.embed_query("what is attention?")
    assert len(calls) == 1
    np.testing.assert_array_almost_equal(v1, v2)


def test_embed_query_model_version_change_misses_cache(monkeypatch):
    """A model/version bump must not serve a vector from the old space."""
    monkeypatch.setattr(config, "TEXT_EMBED_PROVIDER", "fastembed")
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "")
    calls = []
    monkeypatch.setattr(embeddings, "embed_query_local",
                        lambda t: calls.append(t) or np.array([0.1], dtype=np.float32))
    monkeypatch.setattr(config, "TEXT_EMBED_VERSION", "bge-v1")
    embeddings.embed_query("q")
    monkeypatch.setattr(config, "TEXT_EMBED_VERSION", "bge-v2")
    embeddings.embed_query("q")
    assert calls == ["q", "q"]  # both computed -- second didn't hit the v1 entry


class _FakeSparseResult:
    def __init__(self, indices, values):
        self.indices = np.asarray(indices, dtype=np.int64)
        self.values = np.asarray(values, dtype=np.float32)


def test_embed_sparse_query_skips_recompute_on_repeat(monkeypatch):
    calls = []

    class _FakeSparseModel:
        def query_embed(self, texts):
            calls.append(texts[0])
            return iter([_FakeSparseResult([1, 5, 9], [0.1, 0.2, 0.3])])

    monkeypatch.setattr(embeddings, "_sparse_model", lambda: _FakeSparseModel())
    r1 = embeddings.embed_sparse_query("hello")
    r2 = embeddings.embed_sparse_query("hello")
    assert len(calls) == 1
    np.testing.assert_array_equal(r1.indices, [1, 5, 9])
    np.testing.assert_array_almost_equal(r2.values, [0.1, 0.2, 0.3])


# ── Frame-bytes cache ────────────────────────────────────────────────────────

def test_build_moments_frame_bytes_cached(monkeypatch):
    calls = []

    def fake_get_bytes(key):
        calls.append(key)
        return b"\xff\xd8fakejpeg"

    monkeypatch.setattr(search.storage, "get_bytes", fake_get_bytes)
    citations = [{"video_id": "v1", "idx": 3, "transcript": "t", "title": "T"}]
    search._build_moments("user1", citations)
    search._build_moments("user1", citations)
    assert len(calls) == 1


def test_build_moments_frame_bytes_scoped_per_user(monkeypatch):
    """Different users -- even citing the same video_id/idx -- must not
    share a cache entry (tenancy, per CLAUDE.md's hard invariant)."""
    calls = []

    def fake_get_bytes(key):
        calls.append(key)
        return b"\xff\xd8fakejpeg"

    monkeypatch.setattr(search.storage, "get_bytes", fake_get_bytes)
    citations = [{"video_id": "v1", "idx": 3, "transcript": "t", "title": "T"}]
    search._build_moments("user1", citations)
    search._build_moments("user2", citations)
    assert len(calls) == 2


# ── db.py poll-read cache ────────────────────────────────────────────────────

def _uid():
    return f"cache-test-{uuid.uuid4().hex[:8]}"


def test_list_videos_skips_recompute_on_repeat(monkeypatch, cleanup):
    video_ids, _ = cleanup
    uid = _uid()
    vid = f"vid_{uid}"
    video_ids.append(vid)
    db.upsert_pending({"id": vid, "user_id": uid, "source": "upload",
                       "url": None, "storage_key": "k", "source_hash": "h", "title": "T"})
    r1 = db.list_videos(uid)
    # Prove the SECOND call never touches Postgres at all: break the pool and
    # confirm it still returns the identical (cached) result. Undo the patch
    # before returning -- the `cleanup` fixture's own teardown needs a WORKING
    # db.pool() to delete this row afterward.
    monkeypatch.setattr(db, "pool", lambda: (_ for _ in ()).throw(RuntimeError("no DB access expected")))
    r2 = db.list_videos(uid)
    monkeypatch.undo()
    assert r1 == r2


def test_list_videos_created_at_is_json_safe(monkeypatch, cleanup):
    """Cached rows round-trip through JSON -- created_at/updated_at must not
    be raw datetime objects afterward (breaks json.dumps on the SECOND, still-
    cached call otherwise, and would mismatch types with an uncached row in
    list_sources()'s cross-table sort)."""
    video_ids, _ = cleanup
    uid = _uid()
    vid = f"vid_{uid}"
    video_ids.append(vid)
    db.upsert_pending({"id": vid, "user_id": uid, "source": "upload",
                       "url": None, "storage_key": "k", "source_hash": "h", "title": "T"})
    rows = db.list_videos(uid)
    assert isinstance(rows[0]["created_at"], str)
    # A second call (now served from cache) must return the identical shape.
    rows2 = db.list_videos(uid)
    assert isinstance(rows2[0]["created_at"], str)
    assert rows == rows2


def test_list_sources_mixes_video_and_document_without_type_crash(monkeypatch, cleanup):
    """The historical hazard this design has to avoid: list_sources() sorts
    list_videos() + list_documents() rows by created_at in ONE combined sort.
    If one side served a cached (str) row and the other a fresh (datetime)
    row, that sort would raise TypeError. Both must be json-safe consistently."""
    video_ids, doc_ids = cleanup
    uid = _uid()
    vid, doc = f"vid_{uid}", f"doc_{uid}"
    video_ids.append(vid)
    doc_ids.append(doc)
    db.upsert_pending({"id": vid, "user_id": uid, "source": "upload",
                       "url": None, "storage_key": "k", "source_hash": "h", "title": "V"})
    db.upsert_pending_document({"id": doc, "user_id": uid, "kind": "paper",
                               "uri": "http://x", "storage_key": "k", "source_hash": "h",
                               "title": "D"})
    sources = db.list_sources(uid)  # must not raise
    kinds = {s["kind"] for s in sources}
    assert "video" in kinds and "paper" in kinds


def test_list_sources_skips_recompute_on_repeat(monkeypatch, cleanup):
    video_ids, _ = cleanup
    uid = _uid()
    vid = f"vid_{uid}"
    video_ids.append(vid)
    db.upsert_pending({"id": vid, "user_id": uid, "source": "upload",
                       "url": None, "storage_key": "k", "source_hash": "h", "title": "T"})
    r1 = db.list_sources(uid)
    # Second call must come back byte-identical without needing a live DB --
    # simplest proof: break the pool and confirm it still works from cache.
    # Undo before returning -- `cleanup`'s teardown needs a WORKING db.pool().
    monkeypatch.setattr(db, "pool", lambda: (_ for _ in ()).throw(RuntimeError("no DB access expected")))
    r2 = db.list_sources(uid)
    monkeypatch.undo()
    assert r1 == r2
