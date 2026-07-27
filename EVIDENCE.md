# EVIDENCE — dated log of real runs (EDD step 6)

Every entry below is copy-pasted from an actual command run in this repo. No number
here was estimated or guessed — per CLAUDE.md rule E4, fabrication is an automatic fail.

---

## 2026-07-27 — Component 1: `ms_documents` table + unified `list_sources` query

**Scope** (DESIGN.md §3, row 1): `documents` table + unified sources query, `src/db.py`.
Mirrors `ms_videos`; does not touch it.

**Environment setup** (Part 0 was never completed, so a throwaway local Postgres was
used for this DB-layer-only component — no Qdrant/Prefect/LLM needed):
```
uv venv --python 3.12
uv pip install "psycopg[binary,pool]>=3.1" "python-dotenv>=1.0" pytest
docker run -d --name ms-test-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=momentsearch_test -p 55432:5432 postgres:16-alpine
```
`tests/conftest.py` points `DATABASE_URL` at this container by default.

**RED** (`uv run pytest tests/test_db_documents.py -q`, before implementation):
```
FAILED tests/test_db_documents.py::test_upsert_pending_creates_row - AttributeError: module 'src.db' has no attribute 'upsert_pending_document'
... (9 failed, 8 errors — all AttributeError, same cause)
```

**Implementation**: added `ms_documents` table to `SCHEMA` in `src/db.py`
(`id doc_<uuid>`, `user_id`, `kind paper|deck`, `uri`, `storage_key`, `source_hash`,
`title`, `status`, `error`, `chunk_count`, `page_count`, `progress`, `attempts`,
`embed_version`, timestamps — same shape/indexes as `ms_videos`) plus 9 functions
mirroring the video ones (`upsert_pending_document`, `set_document_status`,
`set_document_progress`, `bump_document_attempts`, `get_document`,
`find_duplicate_document`, `list_documents`, `documents_by_ids`, `delete_document`)
and `list_sources(user_id)` — the unified `{id, kind, status, title, pct}` query
`GET /admin/sources` (component 6) will call.

**GREEN**:
```
$ uv run pytest tests/test_db_documents.py -q
.........                                                                [100%]
9 passed, 1 warning in 0.18s
```
(warning is a pre-existing psycopg_pool deprecation notice in `pool()`, unrelated to
this change — not touched, out of scope.)

**Schema verified** (`psql \d ms_documents` against the test container): all 16
columns present with correct types/defaults, matching ARCHITECTURE.md §4.1.

**Still red / not yet built**: components 2–11 (parsers, Prefect flow, admin routes,
search, UI, benchmark, seeding). No SLA rows apply yet — this component has no
network/queue/retrieval surface.

**spec-guardian**: PASS — additive-only, `ms_videos` untouched (0 deletions), no
protected files touched, `list_sources` shape matches README/ARCHITECTURE, RED/GREEN
counts consistent (9 tests).

**Commit**: `1afb14e` — "Add documents table and unified sources query (component 1)".
