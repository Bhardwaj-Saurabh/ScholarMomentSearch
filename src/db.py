"""Postgres (Neon) access layer — the videos manifest, source of truth.

One row per (user's) video; `status` tracks the ingest lifecycle:
pending -> fetching -> sampling -> embedding -> indexed | skipped | failed
(skipped = duplicate (user_id, source_hash); indexed = searchable in Qdrant).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import cache
from .config import DATABASE_URL, INFLIGHT_STATUSES, POLL_CACHE_TTL_S


def _json_safe(row: dict) -> dict:
    """Component 20 (DESIGN.md §3d): datetimes aren't JSON-serializable, and
    the poll-read cache round-trips rows through Redis as JSON. Stringify
    them the SAME way whether a row came fresh from Postgres or a warm cache
    hit, so a caller combining rows from multiple functions (list_sources()
    sorting list_videos() + list_documents() together by created_at) never
    sees a type mismatch depending on which path served which row."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}

_pool: ConnectionPool | None = None
_pool_pid: int | None = None


def pool() -> ConnectionPool:
    """Process-local pool. Prefect runs flows in subprocesses; a child must
    never reuse the parent's SSL connections (corrupts the TLS stream), so a
    fork gets a fresh pool."""
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        # check= pings each connection before lending it out — Neon silently
        # drops idle SSL connections, which otherwise 500s the first request
        # after a quiet period.
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5,
                               check=ConnectionPool.check_connection,
                               kwargs={"row_factory": dict_row})
        _pool_pid = os.getpid()
    return _pool


SCHEMA = """
CREATE TABLE IF NOT EXISTS ms_videos (
    id           TEXT PRIMARY KEY,           -- yt_<id> | up_<uuid>
    user_id      TEXT NOT NULL,
    source       TEXT NOT NULL,              -- youtube | upload
    url          TEXT,                       -- YouTube URL (source=youtube)
    storage_key  TEXT,                       -- uploads/<user>/<id>.<ext> (source=upload)
    source_hash  TEXT,                       -- sha256 of the file / yt video id
    title        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    frame_count  INT,
    progress     REAL,                       -- 0..1 within the current stage
    attempts     INT NOT NULL DEFAULT 0,
    embed_version TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ms_videos_user_idx   ON ms_videos (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ms_videos_status_idx ON ms_videos (status);
CREATE INDEX IF NOT EXISTS ms_videos_hash_idx   ON ms_videos (user_id, source_hash);

-- Papers and decks (Assignment 3). Same shape/lifecycle as ms_videos:
-- pending -> queued -> fetching -> parsing -> embedding -> indexed | skipped | failed
CREATE TABLE IF NOT EXISTS ms_documents (
    id           TEXT PRIMARY KEY,           -- doc_<uuid>
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- paper | deck
    uri          TEXT,                       -- source URL or storage:// ref
    storage_key  TEXT,                       -- fetched/uploaded copy, once stored
    source_hash  TEXT,                       -- sha256 of the fetched bytes
    title        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    chunk_count  INT,
    page_count   INT,
    progress     REAL,                       -- 0..1 within the current stage
    attempts     INT NOT NULL DEFAULT 0,
    embed_version TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ms_documents_user_idx   ON ms_documents (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ms_documents_status_idx ON ms_documents (status);
CREATE INDEX IF NOT EXISTS ms_documents_hash_idx   ON ms_documents (user_id, source_hash);
-- Added post-launch (Part 0 resilience finding): lets src/reconciler.py ask
-- Prefect for a stuck row's OWN flow run state, instead of guessing from a
-- timestamp alone. NULL for anything seeded directly in-process (no Prefect
-- run was ever scheduled for it) — reconciler.py skips those.
ALTER TABLE ms_documents ADD COLUMN IF NOT EXISTS flow_run_id TEXT;

-- Bring-your-own-model: a tenant's hosted LLM endpoint (vLLM / Ollama / any
-- OpenAI-compatible server, NVIDIA NIM, or Anthropic). When a row exists the
-- read path answers with THIS model instead of the server's LLM_* env config.
CREATE TABLE IF NOT EXISTS ms_user_llms (
    user_id    TEXT PRIMARY KEY,
    provider   TEXT NOT NULL DEFAULT 'openai',  -- openai | nvidia | anthropic
    model      TEXT NOT NULL,
    base_url   TEXT,                            -- e.g. http://my-vllm:8000/v1
    api_key    TEXT,                            -- optional (vLLM often has none)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_schema() -> None:
    with pool().connection() as conn:
        conn.execute(SCHEMA)


def upsert_pending(video: dict[str, Any]) -> dict:
    """Insert a video as pending; re-submitting an existing id resets it."""
    with pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ms_videos (id, user_id, source, url, storage_key, source_hash, title, status)
            VALUES (%(id)s, %(user_id)s, %(source)s, %(url)s, %(storage_key)s,
                    %(source_hash)s, %(title)s, 'pending')
            ON CONFLICT (id) DO UPDATE SET
                url = COALESCE(EXCLUDED.url, ms_videos.url),
                storage_key = COALESCE(EXCLUDED.storage_key, ms_videos.storage_key),
                source_hash = COALESCE(EXCLUDED.source_hash, ms_videos.source_hash),
                title = COALESCE(EXCLUDED.title, ms_videos.title),
                status = 'pending', error = NULL, progress = NULL, updated_at = now()
            RETURNING *
            """,
            video,
        ).fetchone()
    return row


def set_status(video_id: str, status: str, *, error: str | None = None,
               title: str | None = None, frame_count: int | None = None,
               source_hash: str | None = None, embed_version: str | None = None,
               progress: float | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            """
            UPDATE ms_videos SET status = %s, error = %s,
                title = COALESCE(%s, title),
                frame_count = COALESCE(%s, frame_count),
                source_hash = COALESCE(%s, source_hash),
                embed_version = COALESCE(%s, embed_version),
                progress = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (status, error, title, frame_count, source_hash, embed_version,
             progress, video_id),
        )


def set_progress(video_id: str, progress: float) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE ms_videos SET progress = %s, updated_at = now() WHERE id = %s",
                     (round(progress, 3), video_id))


def bump_attempts(video_id: str) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE ms_videos SET attempts = attempts + 1, updated_at = now() WHERE id = %s RETURNING attempts",
            (video_id,),
        ).fetchone()
    return row["attempts"] if row else 0


def get_video(video_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM ms_videos WHERE id = %s", (video_id,)).fetchone()


def find_duplicate(user_id: str, source_hash: str, exclude_id: str) -> dict | None:
    """An already-indexed video with the same content for the same user."""
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT * FROM ms_videos
            WHERE user_id = %s AND source_hash = %s AND id <> %s AND status = 'indexed'
            LIMIT 1
            """,
            (user_id, source_hash, exclude_id),
        ).fetchone()


def list_videos(user_id: str, status: str | None = None) -> list[dict]:
    key = f"videos:{user_id}:{status or ''}"
    cached = cache.get_json(key)
    if cached is not None:
        return cached
    q = "SELECT * FROM ms_videos WHERE user_id = %s"
    params: list = [user_id]
    if status:
        q += " AND status = %s"
        params.append(status)
    q += " ORDER BY created_at DESC"
    with pool().connection() as conn:
        rows = [_json_safe(r) for r in conn.execute(q, tuple(params)).fetchall()]
    cache.set_json(key, rows, ttl=POLL_CACHE_TTL_S)
    return rows


def videos_by_ids(ids: list[str]) -> dict[str, dict]:
    """Metadata join for search citations (title/url/source live here, not in Qdrant)."""
    if not ids:
        return {}
    with pool().connection() as conn:
        rows = conn.execute("SELECT * FROM ms_videos WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: r for r in rows}


def delete_video(video_id: str) -> None:
    with pool().connection() as conn:
        conn.execute("DELETE FROM ms_videos WHERE id = %s", (video_id,))


# ── Documents (papers & decks) — mirrors the ms_videos functions above ──────

def upsert_pending_document(doc: dict[str, Any]) -> dict:
    """Insert a document as pending; re-submitting an existing id resets it."""
    with pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO ms_documents (id, user_id, kind, uri, storage_key, source_hash, title, status)
            VALUES (%(id)s, %(user_id)s, %(kind)s, %(uri)s, %(storage_key)s,
                    %(source_hash)s, %(title)s, 'pending')
            ON CONFLICT (id) DO UPDATE SET
                uri = COALESCE(EXCLUDED.uri, ms_documents.uri),
                storage_key = COALESCE(EXCLUDED.storage_key, ms_documents.storage_key),
                source_hash = COALESCE(EXCLUDED.source_hash, ms_documents.source_hash),
                title = COALESCE(EXCLUDED.title, ms_documents.title),
                status = 'pending', error = NULL, progress = NULL, updated_at = now()
            RETURNING *
            """,
            doc,
        ).fetchone()
    return row


def set_document_status(doc_id: str, status: str, *, error: str | None = None,
                        title: str | None = None, chunk_count: int | None = None,
                        page_count: int | None = None, source_hash: str | None = None,
                        embed_version: str | None = None,
                        progress: float | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            """
            UPDATE ms_documents SET status = %s, error = %s,
                title = COALESCE(%s, title),
                chunk_count = COALESCE(%s, chunk_count),
                page_count = COALESCE(%s, page_count),
                source_hash = COALESCE(%s, source_hash),
                embed_version = COALESCE(%s, embed_version),
                progress = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (status, error, title, chunk_count, page_count, source_hash,
             embed_version, progress, doc_id),
        )


def set_document_flow_run_id(doc_id: str, flow_run_id: str | None) -> None:
    """Recorded at every (re-)enqueue so src/reconciler.py can later ask
    Prefect for THIS specific run's state, not just guess from a timestamp."""
    with pool().connection() as conn:
        conn.execute("UPDATE ms_documents SET flow_run_id = %s WHERE id = %s",
                     (flow_run_id, doc_id))


def stale_documents(statuses: tuple[str, ...], older_than_s: float) -> list[dict]:
    """Documents in one of `statuses` whose row hasn't been touched in
    `older_than_s` seconds — candidates for src/reconciler.py to check
    against Prefect's own record of that row's flow run (a stale timestamp
    alone doesn't mean orphaned; it might just be waiting for a free worker
    slot, which is normal backlog, not a crash)."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ms_documents
            WHERE status = ANY(%s) AND updated_at < now() - (%s || ' seconds')::interval
            ORDER BY updated_at ASC
            """,
            (list(statuses), older_than_s),
        ).fetchall()
    return rows


def set_document_storage_key(doc_id: str, storage_key: str) -> None:
    """Record where the fetched paper/deck was persisted (docs/{user}/{id}.ext) —
    so its citation still resolves after the original uri rotates or 404s."""
    with pool().connection() as conn:
        conn.execute("UPDATE ms_documents SET storage_key = %s, updated_at = now() WHERE id = %s",
                     (storage_key, doc_id))


def set_document_progress(doc_id: str, progress: float) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE ms_documents SET progress = %s, updated_at = now() WHERE id = %s",
                     (round(progress, 3), doc_id))


def bump_document_attempts(doc_id: str) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE ms_documents SET attempts = attempts + 1, updated_at = now() WHERE id = %s RETURNING attempts",
            (doc_id,),
        ).fetchone()
    return row["attempts"] if row else 0


def get_document(doc_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM ms_documents WHERE id = %s", (doc_id,)).fetchone()


def find_duplicate_document(user_id: str, source_hash: str, exclude_id: str) -> dict | None:
    """An already-indexed document with the same content for the same user."""
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT * FROM ms_documents
            WHERE user_id = %s AND source_hash = %s AND id <> %s AND status = 'indexed'
            LIMIT 1
            """,
            (user_id, source_hash, exclude_id),
        ).fetchone()


def list_documents(user_id: str, status: str | None = None) -> list[dict]:
    key = f"documents:{user_id}:{status or ''}"
    cached = cache.get_json(key)
    if cached is not None:
        return cached
    q = "SELECT * FROM ms_documents WHERE user_id = %s"
    params: list = [user_id]
    if status:
        q += " AND status = %s"
        params.append(status)
    q += " ORDER BY created_at DESC"
    with pool().connection() as conn:
        rows = [_json_safe(r) for r in conn.execute(q, tuple(params)).fetchall()]
    cache.set_json(key, rows, ttl=POLL_CACHE_TTL_S)
    return rows


def documents_by_ids(ids: list[str]) -> dict[str, dict]:
    """Metadata join for search citations (title/uri live here, not in Qdrant)."""
    if not ids:
        return {}
    with pool().connection() as conn:
        rows = conn.execute("SELECT * FROM ms_documents WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: r for r in rows}


def delete_document(doc_id: str) -> None:
    with pool().connection() as conn:
        conn.execute("DELETE FROM ms_documents WHERE id = %s", (doc_id,))


def _pct(progress: float | None) -> int | None:
    return round(progress * 100) if progress is not None else None


def list_sources(user_id: str) -> list[dict]:
    """Unified status for GET /admin/sources: videos + documents, normalized to
    {id, kind, status, title, pct, chunk_count}, newest first. chunk_count is
    frame_count for a video, chunk_count for a document — additive field
    (benchmark/bench.py's throughput measurement needs it over the public API,
    never touching the DB directly).

    Also cached directly (component 20) — called on every LLM-generated
    answer by search.py's citation-attribution backstop, not just the
    /admin/sources endpoint, so this is a hot path worth its own short-TTL
    entry on top of list_videos()/list_documents()'s own caching."""
    cache_key = f"sources:{user_id}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached
    rows = [
        (v["created_at"], {"id": v["id"], "kind": "video", "status": v["status"],
                           "title": v["title"], "pct": _pct(v["progress"]),
                           "chunk_count": v["frame_count"]})
        for v in list_videos(user_id)
    ] + [
        (d["created_at"], {"id": d["id"], "kind": d["kind"], "status": d["status"],
                           "title": d["title"], "pct": _pct(d["progress"]),
                           "chunk_count": d["chunk_count"]})
        for d in list_documents(user_id)
    ]
    rows.sort(key=lambda r: r[0], reverse=True)
    result = [r[1] for r in rows]
    cache.set_json(cache_key, result, ttl=POLL_CACHE_TTL_S)
    return result


def queue_status_counts() -> list[dict]:
    """Global (ALL tenants) ingest queue/index rollup for the ops metrics
    dashboard (DESIGN.md §3c component 18) — unlike list_sources(), NOT
    tenant-scoped: GROUP BY kind, status across both tables."""
    with pool().connection() as conn:
        return conn.execute("""
            SELECT 'video' AS kind, status, COUNT(*) AS count
              FROM ms_videos GROUP BY status
            UNION ALL
            SELECT kind, status, COUNT(*) AS count
              FROM ms_documents GROUP BY kind, status
            ORDER BY kind, status
        """).fetchall()


# ── Fair scheduling (WFQ) ────────────────────────────────────────────────────

def count_inflight() -> int:
    """How many videos currently occupy execution capacity (scheduled/running)."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM ms_videos WHERE status = ANY(%s)",
            (list(INFLIGHT_STATUSES),),
        ).fetchone()
    return row["n"] if row else 0


def wfq_claim(limit: int) -> list[dict]:
    """Atomically claim up to `limit` pending videos in FAIR (round-robin across
    users) order, flipping them pending -> queued. Returns the claimed rows.

    Fairness: rank each user's pending videos by age (row_number partitioned by
    user_id), then order by that rank first — so we take everyone's oldest, then
    everyone's 2nd, ... A user who dumped 50 videos only gets one slot per round,
    exactly like the others. The UPDATE ... WHERE status='pending' RETURNING is
    the atomic claim: if two dispatchers race, each row is handed out once.
    """
    if limit <= 0:
        return []
    with pool().connection() as conn:
        picked = conn.execute(
            """
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY user_id ORDER BY created_at, id) AS rn
                FROM ms_videos WHERE status = 'pending'
            ) t
            ORDER BY rn, id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        ids = [r["id"] for r in picked]
        if not ids:
            return []
        return conn.execute(
            """
            UPDATE ms_videos SET status = 'queued', updated_at = now()
            WHERE id = ANY(%s) AND status = 'pending'
            RETURNING id, user_id
            """,
            (ids,),
        ).fetchall()


# ── Bring-your-own-model (per-tenant LLM endpoint) ───────────────────────────

def get_user_llm(user_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM ms_user_llms WHERE user_id = %s",
                            (user_id,)).fetchone()


def set_user_llm(user_id: str, *, provider: str, model: str,
                 base_url: str | None, api_key: str | None) -> dict:
    """Upsert a tenant's model endpoint. An empty api_key keeps the stored one
    (so users can change model/URL without re-pasting their secret)."""
    with pool().connection() as conn:
        return conn.execute(
            """
            INSERT INTO ms_user_llms (user_id, provider, model, base_url, api_key)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                base_url = EXCLUDED.base_url,
                api_key = COALESCE(NULLIF(EXCLUDED.api_key, ''), ms_user_llms.api_key),
                updated_at = now()
            RETURNING *
            """,
            (user_id, provider, model, base_url, api_key),
        ).fetchone()


def delete_user_llm(user_id: str) -> None:
    with pool().connection() as conn:
        conn.execute("DELETE FROM ms_user_llms WHERE user_id = %s", (user_id,))
