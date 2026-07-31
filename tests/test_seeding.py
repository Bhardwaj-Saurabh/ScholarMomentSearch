"""Component 10 (DESIGN.md) — extend the boot-time seed gate to ingest
benchmark/corpus.json (8 papers + 8 decks + 8 talks) ALONGSIDE the base app's
existing 4 sample videos, idempotent like today.

Scope: this tests ORCHESTRATION (which sources need indexing, retry passes,
deterministic seed IDs so a re-run targets the same row, "alongside" combining
both lists) — not the ingestion pipelines themselves, which are already
tested (ingest_video is PROVIDED; ingest_document was tested in component 4).
ingest_video/ingest_document are mocked at the seeding module boundary: a real
call would download real videos/PDFs from the internet, which a unit test
must never do (tdd skill). Real Postgres (throwaway container) for the actual
not-indexed/idempotency logic — that IS what's being proven here.
"""
from __future__ import annotations


import pytest

from src import config, db, seeding


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture(autouse=True)
def _assume_vectors_exist_once_indexed(monkeypatch):
    """Component 51 added a real Qdrant vector-count check inside
    _not_indexed_videos/_not_indexed_documents (src/seeding.py::_row_has_vectors).
    This file mocks ingest_video/ingest_document at the status-flip level, per
    its own docstring's ORCHESTRATION-only scope — no real vectors are ever
    written here, so without this the vector-count check would (correctly,
    but irrelevantly to what this file tests) treat every "indexed" row as
    unconfirmed and this suite would depend on whatever real Qdrant backend
    happens to be configured. The vector-count check itself is exercised in
    tests/test_seeding_integrity.py."""
    monkeypatch.setattr(seeding.vector_store, "count_document_chunks", lambda *a, **k: 1)
    monkeypatch.setattr(seeding.vector_store, "count_video_chunks", lambda *a, **k: 1)


@pytest.fixture
def cleanup():
    video_ids, doc_ids = [], []
    yield video_ids, doc_ids
    for v in video_ids:
        db.delete_video(v)
    for d in doc_ids:
        db.delete_document(d)


def test_seed_doc_id_is_deterministic_and_kind_specific():
    a1 = seeding.seed_doc_id("attention", "paper")
    a2 = seeding.seed_doc_id("attention", "paper")
    b = seeding.seed_doc_id("attention", "deck")
    assert a1 == a2  # same triplet+kind -> same id every time (idempotency)
    assert a1 != b   # different kind -> different id


def test_not_indexed_documents_lists_every_corpus_paper_and_deck_when_fresh():
    todo = seeding._not_indexed_documents()
    ids = {t["id"] for t in todo}
    # 8 triplets * 2 kinds = 16, none indexed yet in a fresh DB
    assert len(todo) == 16
    assert seeding.seed_doc_id("attention", "paper") in ids
    assert seeding.seed_doc_id("attention", "deck") in ids
    assert all(t["kind"] in ("paper", "deck") for t in todo)
    assert all(t["uri"].startswith("http") for t in todo)


def test_not_indexed_documents_excludes_already_indexed(cleanup):
    doc_id = seeding.seed_doc_id("attention", "paper")
    cleanup[1].append(doc_id)
    db.upsert_pending_document({"id": doc_id, "user_id": config.DEFAULT_USER_ID,
                               "kind": "paper", "uri": "https://arxiv.org/pdf/1706.03762",
                               "storage_key": None, "source_hash": None, "title": "x"})
    db.set_document_status(doc_id, "indexed")
    todo = seeding._not_indexed_documents()
    assert doc_id not in {t["id"] for t in todo}
    assert len(todo) == 15  # everything else still pending


def test_not_indexed_videos_includes_both_sample_and_corpus_videos():
    todo = seeding._not_indexed_videos()
    # 4 base sample videos + 8 corpus videos, all fresh
    assert len(todo) == 4 + 8


def test_seed_to_completion_indexes_everything_on_first_pass(monkeypatch, cleanup):
    """Every ingest call 'succeeds' (mocked) -> seed_to_completion returns True
    and every source ends up indexed, sample videos AND corpus triplets."""
    def fake_ingest_video(video_id, user_id):
        db.set_status(video_id, "indexed")

    def fake_ingest_document(doc_id, user_id, kind):
        db.set_document_status(doc_id, "indexed")

    monkeypatch.setattr(seeding, "ingest_video", fake_ingest_video)
    monkeypatch.setattr(seeding, "ingest_document", fake_ingest_document)
    monkeypatch.setattr(seeding, "wait_for_clip", lambda *a, **k: None)

    ok = seeding.seed_to_completion()
    assert ok is True
    assert seeding._not_indexed_videos() == []
    assert seeding._not_indexed_documents() == []

    for v in seeding.SAMPLE_VIDEOS:
        cleanup[0].append(seeding.sample_video_id(v["url"]))
    for t in seeding.CORPUS:
        cleanup[0].append(seeding.sample_video_id(t["video_url"]))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "paper"))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "deck"))


def test_seed_to_completion_retries_a_transient_failure_then_succeeds(monkeypatch, cleanup):
    target_doc = seeding.seed_doc_id("bert", "paper")
    attempts = {"n": 0}

    def fake_ingest_video(video_id, user_id):
        db.set_status(video_id, "indexed")

    def fake_ingest_document(doc_id, user_id, kind):
        if doc_id == target_doc and attempts["n"] == 0:
            attempts["n"] += 1
            raise RuntimeError("transient failure (simulated)")
        db.set_document_status(doc_id, "indexed")

    monkeypatch.setattr(seeding, "ingest_video", fake_ingest_video)
    monkeypatch.setattr(seeding, "ingest_document", fake_ingest_document)
    monkeypatch.setattr(seeding, "wait_for_clip", lambda *a, **k: None)

    ok = seeding.seed_to_completion()
    assert ok is True
    assert attempts["n"] == 1  # failed once, then a later pass retried and succeeded
    row = db.get_document(target_doc)
    assert row["status"] == "indexed"

    for v in seeding.SAMPLE_VIDEOS:
        cleanup[0].append(seeding.sample_video_id(v["url"]))
    for t in seeding.CORPUS:
        cleanup[0].append(seeding.sample_video_id(t["video_url"]))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "paper"))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "deck"))


def test_seed_to_completion_returns_false_but_seeds_everything_else_on_permanent_failure(monkeypatch, cleanup):
    poison_doc = seeding.seed_doc_id("gpt3", "deck")

    def fake_ingest_video(video_id, user_id):
        db.set_status(video_id, "indexed")

    def fake_ingest_document(doc_id, user_id, kind):
        if doc_id == poison_doc:
            raise RuntimeError("permanently broken (simulated)")
        db.set_document_status(doc_id, "indexed")

    monkeypatch.setattr(seeding, "ingest_video", fake_ingest_video)
    monkeypatch.setattr(seeding, "ingest_document", fake_ingest_document)
    monkeypatch.setattr(seeding, "wait_for_clip", lambda *a, **k: None)

    ok = seeding.seed_to_completion()
    assert ok is False  # one permanent failure -> overall result is honest, not a false pass

    remaining = seeding._not_indexed_documents()
    assert {t["id"] for t in remaining} == {poison_doc}  # only the poison doc is stuck

    for v in seeding.SAMPLE_VIDEOS:
        cleanup[0].append(seeding.sample_video_id(v["url"]))
    for t in seeding.CORPUS:
        cleanup[0].append(seeding.sample_video_id(t["video_url"]))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "paper"))
        cleanup[1].append(seeding.seed_doc_id(t["id"], "deck"))


def test_seed_corpus_flag_skips_documents_but_still_seeds_sample_videos(monkeypatch, cleanup):
    monkeypatch.setattr(config, "SEED_CORPUS", False)
    calls = {"video": 0, "document": 0}

    def fake_ingest_video(video_id, user_id):
        calls["video"] += 1
        db.set_status(video_id, "indexed")

    def fake_ingest_document(doc_id, user_id, kind):
        calls["document"] += 1
        db.set_document_status(doc_id, "indexed")

    monkeypatch.setattr(seeding, "ingest_video", fake_ingest_video)
    monkeypatch.setattr(seeding, "ingest_document", fake_ingest_document)
    monkeypatch.setattr(seeding, "wait_for_clip", lambda *a, **k: None)

    seeding.seed_to_completion()
    assert calls["document"] == 0
    assert calls["video"] == 4  # only the base sample videos, not the 8 corpus videos

    for v in seeding.SAMPLE_VIDEOS:
        cleanup[0].append(seeding.sample_video_id(v["url"]))
