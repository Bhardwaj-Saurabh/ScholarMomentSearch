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

---

## 2026-07-27 — Component 2: paper PDF parser (`src/ingest/paper.py`)

**Scope** (DESIGN.md §3, row 2): "Paper parser | `src/ingest/paper.py` | pymupdf →
per-page text with structure → page-aware chunks (~500–800 tokens, never crossing
page boundaries without carrying `page`)". Pure parsing/chunking over a local file —
no network, no DB, no Qdrant (component 4's job).

**Fixture design note**: PyMuPDF's `insert_textbox` silently drops text that
overflows its rect in the installed version (1.28.0) — confirmed by a throwaway
smoke test (negative deficit return + 0 extracted chars). Switched fixtures to
explicit per-line `insert_text` calls, which reproduce reliably (verified: 48 lines
of a repeated sentence -> 3360 extracted chars on one page, comfortably over the
~3200-char/~800-token budget).

**RED** (`uv run pytest tests/test_paper_ingest.py -q`, before implementation):
```
ERROR tests/test_paper_ingest.py
ImportError: cannot import name 'paper' from 'src.ingest'
Interrupted: 1 error during collection
```

**Implementation**: `src/ingest/paper.py` — `parse_pdf(path) -> list[PaperChunk]`.
Per-page line extraction via `get_text("dict")` (text + font size); a line whose
size is >=1.15x the page's median body-line size and <=90 chars is treated as a
section heading (excluded from body text, carried forward as `section` until the
next heading); body text is joined per page and split on sentence boundaries into
<=3200-char chunks (~800 tokens at ~4 chars/token) — a chunk never spans two pages.
Added `pymupdf>=1.24` to `requirements.txt` (new runtime dependency) and a new
`requirements-dev.txt` (`pytest>=8.0`, test-only) for reproducibility.

**GREEN**:
```
$ uv run pytest tests/ -q
15 passed, 6 warnings in 0.33s
```
(6 new paper tests + the 9 from component 1, no regressions. Warnings are
pre-existing SWIG/psycopg deprecation notices, not from this change.)

**Still red / not yet built**: components 3–11. This component has no SLA-relevant
surface standalone; it feeds throughput/recall once wired into components 4 and 7.

**spec-guardian**: PASS — no protected files touched, chunk/page invariant verified
in the code, independently re-ran the full suite and got an identical
`15 passed, 6 warnings in 0.33s`. Non-blocking note: the heading-detection median
includes heading lines themselves, which could skew on a heavily-headinged page —
acceptable, the docstring already calls this best-effort and DESIGN.md doesn't
require stronger.

**Commit**: `dd2b8de` — "Add page-aware paper PDF parser (component 2)".

---

## 2026-07-27 — Component 3: deck PDF/PPTX parser (`src/ingest/deck.py`)

**Scope** (DESIGN.md §3, row 3): "Deck parser | `src/ingest/deck.py` | PDF decks:
pymupdf per page = slide; PPTX: python-pptx. Image-heavy slides (little text) →
caption via the existing env-switched vision LLM (`src/llm.py`) before embedding".
Scope boundary (mirrors component 2 + where `t_transcript` sits in the video flow):
`deck.py` stays network-free — it parses slides and, for text-thin slides, extracts
an image; the actual vision-LLM captioning call is deferred to the Prefect flow
(component 4), since an external LLM call needs task-level retries, not a place in
a pure parser.

**API sanity checks before writing tests** (avoided repeating component 2's
fixture pitfall): confirmed `python-pptx`'s `shape.has_text_frame`/`.text_frame.text`
and `shape.shape_type == MSO_SHAPE_TYPE.PICTURE` + `shape.image.blob` work as
expected; confirmed PyMuPDF's `page.get_pixmap().tobytes("jpeg", jpg_quality=85)`
produces a PIL-decodable JPEG.

**RED** (`uv run pytest tests/test_deck_ingest.py -q`, before implementation):
```
ERROR tests/test_deck_ingest.py
ImportError: cannot import name 'deck' from 'src.ingest'
Interrupted: 1 error during collection
```

**Implementation**: `src/ingest/deck.py` — `parse_deck(path) -> list[SlideChunk]`,
dispatching on file extension. PDF: one page = one slide via PyMuPDF; text < 40
chars (`TEXT_THIN_CHARS`) flags `needs_caption=True` and renders the page to a JPEG.
PPTX: one `Slide` = one slide via python-pptx; same 40-char threshold; when thin,
the largest embedded `Picture` shape's image is extracted and normalized to JPEG
via Pillow (so the field is always real JPEG bytes regardless of the source
format). Added `python-pptx>=0.6.23` to `requirements.txt`.

**First test run surfaced 3 failures — all eval bugs, not implementation bugs**:
(1) a fixture used one long `insert_text` string, which PyMuPDF clips instead of
wrapping (the exact pitfall hit in component 2 — should have reused that lesson
immediately); (2) and (3) two "text-heavy slide" fixtures used sample sentences
under the 40-char threshold I'd chosen, so they were correctly flagged
`needs_caption=True` by the code — the tests' expectations, not the threshold,
were wrong. Fixed both fixture issues (multi-line placement; longer sample text)
without touching the 40-char threshold or any parsing logic.

**GREEN**:
```
$ uv run pytest tests/ -q
22 passed, 6 warnings in 0.54s
```
(7 new deck tests + the 15 from components 1–2, no regressions.)

**Still red / not yet built**: components 4–11. No SLA row applies to this
component standalone (no network/queue/retrieval surface) — it feeds throughput
and recall once wired into components 4 and 7.

**spec-guardian**: pending.

**Commit**: _pending._
