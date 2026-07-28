"""Component 15 (DESIGN.md §3b) — hybrid dense+sparse text search.

Qdrant's OWN native hybrid search (Prefetch + FusionQuery(fusion=RRF)), not
hand-rolled BM25: the TEXT_COLLECTION gains a named sparse vector ("bm25", via
fastembed's Qdrant/bm25 SparseTextEmbedding) alongside the existing unnamed
dense vector, fused server-side in one query_points call.

CRITICAL, found before any of this was implemented (see EVIDENCE.md): setting
the tenant filter ONLY at query_points' top-level query_filter does NOT scope
Prefetch legs at all — verified empirically against a throwaway collection,
both tenants' points came back. The fix is `filter=` on EACH Prefetch. The
tenant-scoping test below is the regression lock for that finding, not an
afterthought.

Real where it matters (mirrors tests/test_cross_source_search.py's own stated
policy): tenant scoping and the end-to-end hybrid query use the real embedded
Qdrant + real fastembed dense/sparse models. The point-vector SHAPE helper
(_point_vectors) is unit-tested with a mocked sparse embedder — pure logic,
no need for a real model just to check a dict got built correctly.
"""
from __future__ import annotations

import uuid

import numpy as np
import pytest
from qdrant_client.http import models as qm

from src import db
from src.rag import search as rag_search
from src.rag import vector_store


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()
    vector_store.ensure_text_collection()


class _FakeSparse:
    def __init__(self, indices, values):
        self.indices = np.array(indices)
        self.values = np.array(values)


# ── _point_vectors: pure shape logic, mocked sparse embedder ────────────────

def test_point_vectors_dict_shape_when_hybrid_enabled(monkeypatch):
    monkeypatch.setattr(vector_store, "ENABLE_HYBRID_TEXT_SEARCH", True)
    monkeypatch.setattr("src.rag.embeddings.embed_sparse_docs",
                        lambda texts: [_FakeSparse([1, 2], [0.5, 0.5])])
    dense = np.array([[0.1, 0.2]], dtype=np.float32)
    out = vector_store._point_vectors(dense, ["some text"])
    assert out[0][""] == [pytest.approx(0.1), pytest.approx(0.2)]
    sparse = out[0][vector_store.SPARSE_VECTOR_NAME]
    assert isinstance(sparse, qm.SparseVector)
    assert sparse.indices == [1, 2]
    assert sparse.values == [0.5, 0.5]


def test_point_vectors_plain_list_when_hybrid_disabled(monkeypatch):
    monkeypatch.setattr(vector_store, "ENABLE_HYBRID_TEXT_SEARCH", False)
    dense = np.array([[0.1, 0.2]], dtype=np.float32)
    out = vector_store._point_vectors(dense, ["some text"])
    assert out[0] == [pytest.approx(0.1), pytest.approx(0.2)]


def test_point_vectors_empty_input_returns_empty_list(monkeypatch):
    monkeypatch.setattr(vector_store, "ENABLE_HYBRID_TEXT_SEARCH", True)
    dense = np.zeros((0, 2), dtype=np.float32)
    assert vector_store._point_vectors(dense, []) == []


# ── ensure_text_collection: sparse config actually gets created ────────────

def test_ensure_text_collection_creates_sparse_vector_config():
    info = vector_store.client().get_collection(vector_store.TEXT_COLLECTION)
    assert vector_store.SPARSE_VECTOR_NAME in (info.config.params.sparse_vectors or {})


# ── Real embedded Qdrant: upsert stores both dense + sparse ────────────────

def test_upsert_chunks_stores_both_dense_and_sparse_vectors():
    user = f"u_hybrid_{uuid.uuid4().hex[:8]}"
    video_id = f"yt_{uuid.uuid4().hex[:11]}"
    from src.rag.embeddings import embed_docs
    text = "LoRA freezes the pretrained weights and injects low-rank matrices."
    vecs = embed_docs([text])

    vector_store.upsert_chunks(user, video_id, vecs, payloads=[
        {"user_id": user, "video_id": video_id, "modality": "text",
         "t_start": 1.0, "ms": 1000, "text": text}])

    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{video_id}:text:0"))
    pt = vector_store.client().retrieve(vector_store.TEXT_COLLECTION, ids=[point_id],
                                        with_vectors=True)[0]
    assert "" in pt.vector
    assert vector_store.SPARSE_VECTOR_NAME in pt.vector

    vector_store.delete_video(user, video_id)


# ── CRITICAL regression: tenant scoping must hold under the hybrid path ────

def test_search_text_hybrid_scopes_by_tenant():
    """The exact finding from EVIDENCE.md: IDENTICAL text for two tenants
    maximizes leak-detection sensitivity — if the tenant filter isn't applied
    to BOTH Prefetch legs, the other tenant's identical-scoring point comes
    back too."""
    alice, bob = f"u_alice_{uuid.uuid4().hex[:8]}", f"u_bob_{uuid.uuid4().hex[:8]}"
    v_alice, v_bob = f"yt_{uuid.uuid4().hex[:11]}", f"yt_{uuid.uuid4().hex[:11]}"
    text = "Retrieval-augmented generation combines a retriever with a generator."
    from src.rag.embeddings import embed_docs, embed_query
    vec = embed_docs([text])

    vector_store.upsert_chunks(alice, v_alice, vec, payloads=[
        {"user_id": alice, "video_id": v_alice, "modality": "text",
         "t_start": 1.0, "ms": 1000, "text": text}])
    vector_store.upsert_chunks(bob, v_bob, vec, payloads=[
        {"user_id": bob, "video_id": v_bob, "modality": "text",
         "t_start": 1.0, "ms": 1000, "text": text}])

    qvec = embed_query("how does retrieval augmented generation work")
    hits = vector_store.search_text(qvec, alice, top_k=10,
                                    query_text="how does retrieval augmented generation work")
    users_seen = {h.get("user_id") for h in hits}
    assert users_seen == {alice}, f"tenant leak: expected only {{alice}}, got {users_seen}"

    vector_store.delete_video(alice, v_alice)
    vector_store.delete_video(bob, v_bob)


# ── End-to-end: hybrid query returns real, correct results ─────────────────

def test_search_text_hybrid_end_to_end_finds_the_right_chunk():
    user = f"u_hybridE2E_{uuid.uuid4().hex[:8]}"
    doc_a, doc_b = f"doc_{uuid.uuid4().hex[:8]}", f"doc_{uuid.uuid4().hex[:8]}"
    texts = [
        "LoRA freezes the pretrained weights and injects trainable low-rank "
        "matrices into each transformer layer to adapt the model efficiently.",
        "The rank hyperparameter r=8 was used for most LoRA experiments in "
        "the paper, balancing parameter count and downstream accuracy.",
    ]
    from src.rag.embeddings import embed_docs, embed_query
    vecs = embed_docs(texts)
    vector_store.upsert_document_chunks(user, doc_a, "paper", vecs[0:1], payloads=[
        {"user_id": user, "source_id": doc_a, "kind": "paper", "page": 1, "text": texts[0]}])
    vector_store.upsert_document_chunks(user, doc_b, "paper", vecs[1:2], payloads=[
        {"user_id": user, "source_id": doc_b, "kind": "paper", "page": 2, "text": texts[1]}])

    query = "what value of r was used for the LoRA rank hyperparameter"
    qvec = embed_query(query)
    hits = vector_store.search_text(qvec, user, top_k=5, query_text=query)
    assert hits, "expected at least one hit"
    assert hits[0]["source_id"] == doc_b, "the chunk with the exact rank r=8 should rank first"

    vector_store.delete_document_chunks(user, doc_a)
    vector_store.delete_document_chunks(user, doc_b)


def test_search_text_without_query_text_still_works_dense_only():
    """Backward compatibility: existing callers that never pass query_text
    (e.g. the CLIP-branch-only tests elsewhere) must keep working exactly as
    before — this is the plain dense-only path, untouched."""
    user = f"u_denseonly_{uuid.uuid4().hex[:8]}"
    video_id = f"yt_{uuid.uuid4().hex[:11]}"
    text = "a plain transcript chunk with no special content"
    from src.rag.embeddings import embed_docs
    vec = embed_docs([text])
    vector_store.upsert_chunks(user, video_id, vec, payloads=[
        {"user_id": user, "video_id": video_id, "modality": "text",
         "t_start": 1.0, "ms": 1000, "text": text}])

    hits = vector_store.search_text(vec[0], user, top_k=5)  # no query_text
    assert any(h.get("video_id") == video_id for h in hits)

    vector_store.delete_video(user, video_id)


# ── Confidence-gate correctness under hybrid fusion ─────────────────────────

def test_retrieve_confidence_gate_uses_dense_only_score_not_hybrid_fusion_score(monkeypatch):
    """Found empirically before shipping component 15 (see EVIDENCE.md):
    Qdrant's native RRF fusion score is rank-quantized, not magnitude-based —
    an off-topic query's top score can land nearly as high as an on-topic
    one's (0.5 vs 1.0, versus dense-only's 0.41 vs 0.64 on the same probe).
    retrieve()'s confidence-gate score (best_text, feeds CONFIDENCE_THRESHOLD/
    TEXT_CONFIDENCE_THRESHOLD) MUST come from a plain dense-only call, never
    the hybrid (query_text=...) one, or the abstain gate loses its
    discriminating signal entirely."""
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(512, dtype=np.float32))
    calls = []

    def fake_search_text(vec, user_id, *, top_k, video_id=None, video_ids=None, query_text=None):
        calls.append({"query_text": query_text})
        score = 0.999 if query_text is not None else 0.111
        return [{"score": score, "video_id": "yt_gate", "ms": 0, "text": "x"}]

    monkeypatch.setattr(vector_store, "search", lambda *a, **k: [])
    monkeypatch.setattr(vector_store, "search_text", fake_search_text)

    result = rag_search.retrieve("some question", "u_gate_test")
    assert result["best_text"] == pytest.approx(0.111), (
        "confidence gate must use the dense-only score, not the hybrid-fused one")
    assert any(c["query_text"] is None for c in calls), "expected a plain dense-only call"
    assert any(c["query_text"] is not None for c in calls), "expected a hybrid call for citations"
