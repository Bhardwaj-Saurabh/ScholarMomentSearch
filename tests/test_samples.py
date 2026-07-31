"""Component 34 (DESIGN.md §3e) — seeded corpus documents are protected from
deletion, mirroring the existing `is_sample`/SAMPLE_IDS protection for the
four base sample videos + the 8 corpus videos. Without this, an ADMIN_TOKEN
holder could delete the graded demo corpus (the seed gate would just re-add
it on next boot anyway — same rationale as the video path)."""
from __future__ import annotations

from src import samples


def test_seed_doc_id_is_deterministic():
    assert samples.seed_doc_id("triplet_1", "paper") == samples.seed_doc_id("triplet_1", "paper")


def test_seed_doc_id_differs_by_kind():
    assert samples.seed_doc_id("triplet_1", "paper") != samples.seed_doc_id("triplet_1", "deck")


def test_is_sample_document_true_for_corpus_triplets():
    if not samples.CORPUS:
        return  # benchmark/corpus.json not present in this checkout — nothing to assert
    triplet_id = samples.CORPUS[0]["id"]
    assert samples.is_sample_document(samples.seed_doc_id(triplet_id, "paper"))
    assert samples.is_sample_document(samples.seed_doc_id(triplet_id, "deck"))


def test_is_sample_document_false_for_a_regular_registration():
    assert not samples.is_sample_document("doc_abc123")
