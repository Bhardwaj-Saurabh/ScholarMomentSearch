"""Qdrant — one shared multi-tenant collection, one point per kept frame.

Multi-tenancy: every point carries user_id; the field has a tenant payload
index and every search / upsert / delete is user_id-filtered. NOT
collection-per-user (collection explosion); a huge tenant can graduate to a
dedicated collection later.

Memory profile (the frame-scale levers, all env flags, default ON):
  QDRANT_ON_DISK        original float vectors live on disk
  QDRANT_QUANTIZATION   int8 copies pinned in RAM (~4x smaller) do the search;
                        queries rescore the top candidates from the originals
  QDRANT_HNSW_ON_DISK   the HNSW graph lives on disk too

Point IDs are uuid5 of "{video_id}:{frame_idx}" — deterministic, so re-runs
overwrite instead of duplicating. Payloads are trimmed to filter/display
fields (user_id, video_id, ms, idx, embed_version); titles and URLs live in
Postgres and are joined at answer time.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import (
    CLIP_DIM,
    CLIP_MODEL,
    ENABLE_HYBRID_TEXT_SEARCH,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_HNSW_ON_DISK,
    QDRANT_LOCAL_PATH,
    QDRANT_ON_DISK,
    QDRANT_QUANTIZATION,
    QDRANT_URL,
    SPARSE_VECTOR_NAME,
    TEXT_COLLECTION,
    TEXT_EMBED_DIM,
)

_client: QdrantClient | None = None

# Dimensions of the stock sentence-transformers CLIP checkpoints — lets the
# API create the collection at boot without pulling in torch or downloading
# the model. Unknown/custom models: set CLIP_DIM, or the model gets loaded.
_KNOWN_DIMS = {
    "clip-ViT-B-32": 512,
    "clip-ViT-B-16": 512,
    "clip-ViT-L-14": 768,
    "clip-ViT-L-14-336": 768,
}


def _dim() -> int:
    if CLIP_DIM:
        return CLIP_DIM
    if CLIP_MODEL in _KNOWN_DIMS:
        return _KNOWN_DIMS[CLIP_MODEL]
    from .embeddings import embedding_dim  # last resort — loads the model

    return embedding_dim()


def client() -> QdrantClient:
    global _client
    if _client is None:
        if QDRANT_URL:
            _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None,
                                   timeout=60)
        else:  # embedded local instance — dev only, single-process
            _client = QdrantClient(path=QDRANT_LOCAL_PATH)
    return _client


def point_id(video_id: str, frame_idx: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{video_id}:{frame_idx}"))


def _user_filter(user_id: str, video_id: str | None = None,
                 video_ids: list[str] | None = None) -> qm.Filter:
    must: list[qm.FieldCondition | qm.Filter] = [
        qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id))]
    scope = None
    if video_id:  # single-video scope (kept for /transcript-style calls)
        scope = qm.FieldCondition(key="video_id", match=qm.MatchValue(value=video_id))
    elif video_ids:  # multi-select scope — query only the chosen videos
        scope = qm.FieldCondition(key="video_id", match=qm.MatchAny(any=video_ids))
    if scope is not None:
        # video_id/video_ids scopes the VIDEO branch only. Paper/deck chunks
        # (TEXT_COLLECTION, shared with video transcripts) carry no video_id
        # field at all — a plain `must` on that field silently excluded every
        # document from any video-scoped query, which meant the UI's search
        # box (which always passes video_ids) could never surface a paper/
        # deck citation at all. A document chunk (video_id absent) must
        # always pass this condition, regardless of which videos are
        # selected — the CLIP visual collection never has document points,
        # so this is a no-op there (is_empty just never matches).
        must.append(qm.Filter(should=[
            scope, qm.IsEmptyCondition(is_empty=qm.PayloadField(key="video_id"))]))
    return qm.Filter(must=must)


# Component 57 (DESIGN.md §3m): ensure_* used to fire ~5 Qdrant round trips
# per call — and the document flow calls it once per RUN, in a fresh Prefect
# subprocess each time, so every single ingest paid for index-creation calls
# that have been no-ops since the collection was first created. Two-level fix:
# a process-local memo (repeat calls in one process are free), and payload
# indexes created only WITH the collection (an existing collection already has
# them — every deployment of this code creates the two together).
_ensured: set[str] = set()


def _ensure(collection: str, dim: int, sparse_vector_name: str | None = None) -> None:
    """Create a collection (low-RAM profile) + tenant/video payload indexes.

    sparse_vector_name (component 15): the ONLY point this can be set — this
    Qdrant server version rejects adding a sparse vector config to an
    already-populated collection (verified live, EVIDENCE.md), so a sparse
    config missing here can't be retrofitted later without a drop+recreate."""
    if collection in _ensured:
        return
    c = client()
    if c.collection_exists(collection):
        # Already provisioned (by app startup, worker boot, or a previous
        # deployment of this same code) — its payload indexes were created
        # together with it below, so there is nothing left to do.
        _ensured.add(collection)
        return
    c.create_collection(
        collection_name=collection,
        vectors_config=qm.VectorParams(
            size=dim,
            distance=qm.Distance.COSINE,
            on_disk=QDRANT_ON_DISK,
        ),
        hnsw_config=qm.HnswConfigDiff(on_disk=QDRANT_HNSW_ON_DISK),
        quantization_config=(
            qm.ScalarQuantization(scalar=qm.ScalarQuantizationConfig(
                type=qm.ScalarType.INT8, always_ram=True))
            if QDRANT_QUANTIZATION else None
        ),
        sparse_vectors_config=(
            {sparse_vector_name: qm.SparseVectorParams()} if sparse_vector_name else None
        ),
    )
    # Tenant index on user_id: co-locates a tenant's points so per-user
    # searches touch a small slice of the index. video_id for delete/filter.
    try:
        c.create_payload_index(
            collection_name=collection, field_name="user_id",
            field_schema=qm.KeywordIndexParams(type=qm.KeywordIndexType.KEYWORD,
                                               is_tenant=True))
    except Exception:  # older server without is_tenant, or index already exists
        try:
            c.create_payload_index(collection_name=collection, field_name="user_id",
                                   field_schema=qm.PayloadSchemaType.KEYWORD)
        except Exception:
            pass
    try:
        c.create_payload_index(collection_name=collection, field_name="video_id",
                               field_schema=qm.PayloadSchemaType.KEYWORD)
    except Exception:
        pass
    _ensured.add(collection)


def ensure_collection() -> None:
    """Visual (CLIP frame) collection."""
    _ensure(QDRANT_COLLECTION, _dim())


def ensure_text_collection() -> None:
    """Transcript (bge text) collection — the second branch, now also home to
    paper/deck chunks. source_id gets its own index attempt here (not in
    _ensure): frame payloads in the visual collection never carry that field.
    Same component-57 rule as _ensure: the index attempt only happens when
    this process hasn't already confirmed the collection."""
    if TEXT_COLLECTION in _ensured:
        return
    _ensure(TEXT_COLLECTION, TEXT_EMBED_DIM,
           sparse_vector_name=SPARSE_VECTOR_NAME if ENABLE_HYBRID_TEXT_SEARCH else None)
    try:
        client().create_payload_index(collection_name=TEXT_COLLECTION, field_name="source_id",
                                      field_schema=qm.PayloadSchemaType.KEYWORD)
    except Exception:
        pass


def upsert_frames(user_id: str, video_id: str, ids: Iterable[int],
                  vectors: np.ndarray, payloads: list[dict[str, Any]]) -> None:
    points = [
        qm.PointStruct(id=point_id(video_id, idx), vector=vec.tolist(), payload=payload)
        for idx, vec, payload in zip(ids, vectors, payloads)
    ]
    if points:
        client().upsert(collection_name=QDRANT_COLLECTION, points=points, wait=True)


def search(vector: np.ndarray, user_id: str, *, top_k: int,
           video_id: str | None = None,
           video_ids: list[str] | None = None) -> list[dict[str, Any]]:
    try:
        hits = client().query_points(
            collection_name=QDRANT_COLLECTION,
            query=vector.tolist(),
            limit=top_k,
            query_filter=_user_filter(user_id, video_id, video_ids),
            with_payload=True,
            search_params=qm.SearchParams(
                # Quantized search is lossy; rescore re-reads the full-precision
                # vectors from disk for the top candidates.
                quantization=qm.QuantizationSearchParams(rescore=True)
                if QDRANT_QUANTIZATION else None,
            ),
        ).points
    except Exception as exc:
        # Empty deployment (collection not created yet) is a "no results"
        # situation, not a 500 — the UI shows "no moments found".
        if ("doesn't exist" in str(exc).lower() or "not found" in str(exc).lower()):
            # (case-insensitive: server mode says "Not found", embedded local
            # mode says "Collection X not found" — both mean the same thing)
            return []
        raise
    return [{"score": float(h.score), **(h.payload or {})} for h in hits]


# ── Transcript (text) branch ─────────────────────────────────────────────────

def _point_vectors(vectors: np.ndarray, texts: list[str]) -> list[Any]:
    """Component 15: each point's dense vector, plus a BM25 sparse vector
    derived from its own text, when hybrid search is enabled — a bare dense
    list (unchanged, pre-component-15 shape) otherwise. Lazy-imports
    embeddings (mirrors _dim()'s existing lazy import) so a plain video-only
    deploy with hybrid disabled never pays for loading the sparse model."""
    if not ENABLE_HYBRID_TEXT_SEARCH or not texts:
        return [vec.tolist() for vec in vectors]
    from .embeddings import embed_sparse_docs

    sparse_vecs = embed_sparse_docs(texts)
    return [
        {"": vec.tolist(), SPARSE_VECTOR_NAME: qm.SparseVector(
            indices=sparse.indices.tolist(), values=sparse.values.tolist())}
        for vec, sparse in zip(vectors, sparse_vecs)
    ]


def upsert_chunks(user_id: str, video_id: str, vectors: np.ndarray,
                  payloads: list[dict[str, Any]]) -> None:
    """Transcript chunks into the text collection. IDs are uuid5 of
    '<video_id>:text:<i>' so re-runs overwrite, and never collide with frame ids."""
    point_vectors = _point_vectors(vectors, [p.get("text", "") for p in payloads])
    points = [
        qm.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{video_id}:text:{i}")),
                       vector=vec, payload=payload)
        for i, (vec, payload) in enumerate(zip(point_vectors, payloads))
    ]
    if points:
        client().upsert(collection_name=TEXT_COLLECTION, points=points, wait=True)


def search_text(vector: np.ndarray, user_id: str, *, top_k: int,
                video_id: str | None = None,
                video_ids: list[str] | None = None,
                query_text: str | None = None) -> list[dict[str, Any]]:
    """query_text (component 15): when given (and hybrid enabled), fuses the
    dense vector search with a BM25 sparse search of the same query, natively
    in Qdrant (Prefetch + FusionQuery). Omitted -> the original dense-only
    path, unchanged, for backward compatibility with every existing caller.

    CRITICAL (verified live, EVIDENCE.md): the tenant filter is applied to
    EACH Prefetch individually, not only passed once at the top level — a
    top-level-only filter does NOT scope Qdrant's prefetch legs at all, which
    would silently leak another tenant's data through the hybrid path."""
    qfilter = _user_filter(user_id, video_id, video_ids)
    try:
        if ENABLE_HYBRID_TEXT_SEARCH and query_text:
            from .embeddings import embed_sparse_query

            sparse = embed_sparse_query(query_text)
            hits = client().query_points(
                collection_name=TEXT_COLLECTION,
                prefetch=[
                    qm.Prefetch(
                        query=vector.tolist(), using="", filter=qfilter, limit=top_k,
                        params=qm.SearchParams(
                            quantization=qm.QuantizationSearchParams(rescore=True)
                            if QDRANT_QUANTIZATION else None),
                    ),
                    qm.Prefetch(
                        query=qm.SparseVector(indices=sparse.indices.tolist(),
                                              values=sparse.values.tolist()),
                        using=SPARSE_VECTOR_NAME, filter=qfilter, limit=top_k,
                    ),
                ],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                limit=top_k,
                with_payload=True,
            ).points
        else:
            hits = client().query_points(
                collection_name=TEXT_COLLECTION,
                query=vector.tolist(),
                limit=top_k,
                query_filter=qfilter,
                with_payload=True,
                search_params=qm.SearchParams(
                    quantization=qm.QuantizationSearchParams(rescore=True)
                    if QDRANT_QUANTIZATION else None,
                ),
            ).points
    except Exception as exc:
        if ("doesn't exist" in str(exc).lower() or "not found" in str(exc).lower()):
            # (case-insensitive: server mode says "Not found", embedded local
            # mode says "Collection X not found" — both mean the same thing)
            return []
        raise
    return [{"score": float(h.score), **(h.payload or {})} for h in hits]


def upsert_document_chunks(user_id: str, source_id: str, kind: str, vectors: np.ndarray,
                           payloads: list[dict[str, Any]]) -> None:
    """Paper/deck chunks into the SAME text collection video transcripts use —
    the shared cross-source semantic space (DESIGN.md component 4). IDs are
    uuid5 of '<source_id>:<kind>:<i>' so re-runs overwrite, and never collide
    with video ids ('<video_id>:text:<i>')."""
    point_vectors = _point_vectors(vectors, [p.get("text", "") for p in payloads])
    points = [
        qm.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{kind}:{i}")),
                       vector=vec, payload=payload)
        for i, (vec, payload) in enumerate(zip(point_vectors, payloads))
    ]
    if points:
        client().upsert(collection_name=TEXT_COLLECTION, points=points, wait=True)


def count_document_chunks(user_id: str, source_id: str) -> int:
    """DESIGN.md §3j component 51: how many chunks a document ACTUALLY has in
    Qdrant, independent of what Postgres's `status` column claims. This is the
    check that closes the incident where every seeded paper/deck read
    `status='indexed'` with zero real vectors after `TEXT_COLLECTION` was
    dropped and recreated for component 15's migration. Raises on a Qdrant
    error rather than swallowing it — the caller (seeding.py) must fail OPEN
    toward re-seeding on an uncertain answer, and doing that here would hide
    the distinction between 'confirmed present' and 'unknown'."""
    f = qm.Filter(must=[
        qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
        qm.FieldCondition(key="source_id", match=qm.MatchValue(value=source_id)),
    ])
    return client().count(collection_name=TEXT_COLLECTION, count_filter=f, exact=True).count


def count_video_chunks(user_id: str, video_id: str) -> int:
    """Same check for the visual branch — frame vectors in QDRANT_COLLECTION.
    Video transcript text also lives in TEXT_COLLECTION, but the frame count
    alone is sufficient signal: a video losing its frames but keeping its
    transcript is not a case this incident produced or this check needs to
    distinguish."""
    f = _user_filter(user_id, video_id)
    return client().count(collection_name=QDRANT_COLLECTION, count_filter=f, exact=True).count


def delete_document_chunks(user_id: str, source_id: str, raise_on_error: bool = False) -> None:
    """Purge a document's chunks before re-embedding, mirroring delete_video's
    role for the video branches — keeps a re-run idempotent. Fails open
    (swallows) by default, since a transient Qdrant hiccup during a re-embed
    shouldn't crash the whole ingest flow. Component 34 (DESIGN.md §3e) needs
    the opposite for the admin DELETE route: a purge failure there must be
    visible, so the row can be left in place rather than deleted out from
    under vectors that are still searchable — raise_on_error=True does that
    without duplicating this function."""
    sel = qm.FilterSelector(filter=qm.Filter(must=[
        qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
        qm.FieldCondition(key="source_id", match=qm.MatchValue(value=source_id)),
    ]))
    try:
        client().delete(collection_name=TEXT_COLLECTION, points_selector=sel, wait=True)
    except Exception:
        if raise_on_error:
            raise


def delete_video(user_id: str, video_id: str) -> None:
    """Purge a video from BOTH branches (frames + transcript)."""
    sel = qm.FilterSelector(filter=_user_filter(user_id, video_id))
    for coll in (QDRANT_COLLECTION, TEXT_COLLECTION):
        try:
            client().delete(collection_name=coll, points_selector=sel, wait=True)
        except Exception:
            pass  # text collection may not exist if transcript is disabled


def collection_ready() -> bool:
    try:
        return client().collection_exists(QDRANT_COLLECTION)
    except Exception:
        return False
