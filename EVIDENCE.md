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

**spec-guardian**: PASS — verified in code (not just claimed) that `deck.py` makes
no LLM/network calls (grepped for requests/httpx/openai/anthropic/llm imports —
none found), honoring the component-4 boundary. Independently re-ran the full
suite: `22 passed, 6 warnings in 0.57s`, matching. Two forward notes, non-blocking:
(1) component 4's captioning task must handle `needs_caption=True` +
`image_jpeg=None` gracefully (a thin-text PPTX slide with no picture shape);
(2) `.gitignore` had no `*.pdf`/`*.pptx`/`*.mp4` rule — fixed immediately (cheap,
real hygiene gap per CLAUDE.md's media-file rule) rather than deferred.

**Commit**: `8777d05` — "Add deck PDF/PPTX slide parser (component 3)".
Follow-up `.gitignore` fix included in the next commit.

---

## 2026-07-27 — Component 4: document ingest Prefect flow (`src/ingest/doc_pipeline.py`)

**Scope** (DESIGN.md §3, row 4): "Document ingest flow | `src/ingest/doc_pipeline.py`
| Prefect flow `ms-ingest-document` with kind branch. Lifecycle: `pending →
fetching → parsing → embedding → indexed | failed`. Per-task retries like video.
**Crash-safe ordering:** status → `indexed` only *after* the Qdrant upsert
returns." This is the biggest component so far — it wires DB (1) + parsers (2, 3)
together and needed small additive plumbing beyond the named file:

- `src/config.py` — `DOC_KEY_PREFIX = "docs/"` (new key-layout constant)
- `src/storage.py` — `doc_key(user_id, doc_id, ext)` (mirrors `upload_key`)
- `src/db.py` — `set_document_storage_key(doc_id, storage_key)` (small setter)
- `src/rag/vector_store.py` — `upsert_document_chunks`/`delete_document_chunks`
  (mirror `upsert_chunks`/`delete_video`'s text-branch role, ID scheme
  `{source_id}:{kind}:{i}`) + a `source_id` payload index in `_ensure()`
- `src/llm.py` — `caption_image(image_jpeg, cfg)`, implemented as a one-line
  wrapper around the existing `answer()` (same trick `ping()` already uses) —
  zero new provider-dispatch code

None of these are on CLAUDE.md's protected-file list; each is purely additive
(new functions/constants, no existing video-path behavior touched).

**Product decision beyond the letter of DESIGN.md's row**: `t_fetch` persists the
downloaded paper/deck to object storage (`storage.upload_file` + `doc_key`) before
proceeding, unlike the video pipeline's YouTube path (which never persists,
relying on YouTube's own stability). Justification: `benchmark/corpus.json`'s
`known_gaps` already flagged that `.edu`/course-page deck URLs "can rotate across
course terms" — persisting a durable copy is what keeps a citation's `uri` alive
after that happens. Matches ARCHITECTURE.md §5.1's "P1 fetch: download PDF → sha256
dup check → docs/ in object storage" line, which the video flow's precedent alone
would not have implied.

**Environment/tooling decisions** (so the tests are real, not theater):
- Installed `prefect`, `qdrant-client`, `fastembed` into the venv (already
  base-app deps in `requirements.txt` — no new entries needed there).
- Confirmed Prefect flows/tasks run fully locally with no Cloud account (a
  temporary local server auto-starts when `PREFECT_API_URL` is unset — matches
  `examples/quickstart.py`'s documented behavior). Timed the cost: a full `@flow`
  call costs ~1s (fixed per-run overhead); calling a `@task` directly (its
  `.fn` attribute, bypassing flow-run tracking) costs ~40ms after the one-time
  ~10s server bootstrap. Used a **session-scoped** `prefect_test_harness()`
  fixture (`tests/conftest.py`, opt-in via `test_doc_pipeline.py`'s local
  autouse fixture, not global) so only this component's tests pay that cost,
  once. Tests call `.fn` directly except the 2 full end-to-end flow tests.
- Qdrant: real, **embedded on-disk mode** (`QDRANT_LOCAL_PATH` env pointed at a
  throwaway temp dir in `conftest.py`, no server/cloud key) — proves the actual
  upsert/payload/ID-scheme wiring, not a mock's approximation.
- Embeddings: real fastembed (bge-small-en-v1.5, ONNX, CPU). First call
  downloads the model from HF Hub: **~13s one-time** (`Fetching 5 files...`,
  confirmed live); cached after (~2ms on the second call). No API key needed.
- Object storage: real, local provider (writes under `./data/`, gitignored per
  the `.gitignore` fix from component 3); test fixtures clean up their keys.
- Mocked at the two genuine external boundaries per the `tdd` skill's own
  guidance: the network `_download()` call (an arbitrary external URL) and
  `llm.caption_image` (a paid provider API) — never a real network fetch or a
  real LLM call in the test suite.

**RED** (`uv run pytest tests/test_doc_pipeline.py -q`, before implementation):
```
ImportError: cannot import name 'doc_pipeline' from 'src.ingest'
Interrupted: 1 error during collection
```

**GREEN** — all 14 tests passed on the first implementation attempt (no
red-then-fix cycle needed this time):
```
$ uv run pytest tests/test_doc_pipeline.py -v
14 passed, 7 warnings in 40.23s
```
Covers: `_download`'s own URL-fetch logic; `t_fetch`'s storage-key-vs-uri
dispatch, hash computation + storage persistence, and duplicate-skip path;
`t_parse`'s kind branching (page locator for papers, slide locator for decks,
including the unknown-kind error); `t_caption`'s three states (no LLM
configured → no-op, LLM available → caption injected, LLM raises → swallowed,
never crashes the flow); `t_embed_index`'s real Qdrant round-trip (payload
shape verified directly via `client().scroll()` — `kind`, `page`/`slide`,
`user_id`, `source_id`, `embed_version` all present) **and** the named
crash-safety invariant (`vector_store.upsert_document_chunks` forced to raise
→ status asserted to stay `"embedding"`, never reaches `"indexed"`); the full
`ingest_document` flow succeeding end-to-end and setting `"failed"` with a
readable error on an injected parse failure.

**Full suite**:
```
$ uv run pytest tests/ -q
36 passed, 7 warnings in 39.50s
```
(14 new + 22 from components 1–3, no regressions. One new warning is Qdrant's
local mode noting "payload indexes have no effect in local Qdrant" — expected
and harmless; indexes take effect against real Qdrant Cloud in production.)

**sla-gate deferred, not skipped**: `benchmark/bench.py` needs a running HTTP
stack (`POST /admin/documents`, the queue trigger) that doesn't exist until
components 5 (queue wiring) and 6 (admin router) are built — there is nothing
for it to hit yet. The mechanism the resilience gate will later verify at the
system level (worker crash → zero loss → no re-run of finished stages) is
already directly proven here at the unit level via the crash-safety test above.

**Still red / not yet built**: components 5–11 (queue wiring, admin API, search,
UI, benchmark, seeding).

**spec-guardian**: PASS-with-warnings. Independently verified (not just trusted
EVIDENCE.md's claims): the crash-safety invariant holds by inspection —
`set_document_status(doc_id, "indexed", ...)` is the last line of
`t_embed_index`, strictly after the upsert call, and appears nowhere else in the
file; no protected files touched; the video path's existing functions
(`upsert_chunks`, `delete_video`, `set_status`, `answer`, `ping`, `upload_key`)
were only added-to, never modified; the two ID schemes
(`{video_id}:text:i` vs `{source_id}:{kind}:i`) cannot collide since `kind` is
never the literal `"text"`. Independently re-ran the suite: `36 passed, 7
warnings in 38.30s`, matching. One low finding: the new `source_id` payload-index
attempt lived in the shared `_ensure()` helper, so it also (harmlessly, via
try/except) fired against the video-only CLIP collection — beyond DESIGN.md row
4's text-collection-only scope. **Fixed**: moved the index creation out of
`_ensure()` into `ensure_text_collection()` specifically. Re-ran full suite after
the fix: `36 passed, 7 warnings in 54.23s` (timing noise only, count unchanged).

**Commit**: `1a87a97` — "Add document ingest Prefect flow with crash-safe status
ordering (component 4)". Follow-up index-scoping fix in the next commit.

---

## 2026-07-27 — Component 5: queue wiring (`src/jobs.py`, `src/worker.py`)

**Scope** (DESIGN.md §3, row 5): "Queue wiring | `src/jobs.py`, `src/worker.py`,
`src/dispatcher.py` | `enqueue_document()`; worker serves both deployments;
dispatcher claims across videos+documents (**or documents ride FIFO first, WFQ
unified after**)."

**Scope decision — a real conflict between our own governance docs, resolved
in writing before coding.** `src/dispatcher.py` is on CLAUDE.md's protected-file
list ("Extend around them; never edit their behavior"), but DESIGN.md's row 5
literally names it for unification. DESIGN.md's own row offers an explicit
fallback — documents ride FIFO first — so I took it: **`src/dispatcher.py` is
untouched, 0 diff** (`git diff --stat src/dispatcher.py` — empty output,
confirmed after shipping). Documents are enqueued immediately via
`enqueue_document()`, which the future admin endpoint (component 6) will call
directly at registration; WFQ-unifying the dispatcher across both tables is a
later, optional enhancement DESIGN.md itself defers, not a dropped requirement.
No graded SLA needs document-side fairness (`sla.json` only checks accept
latency, search-during-ingest ratio, throughput, recall, no-loss).

**API research before implementing**: confirmed Prefect's module-level `serve()`
accepts multiple `RunnerDeployment` objects built via `flow.to_deployment(name=
...)`, and that `deployment.full_name` reproduces the exact `{flow_name}/
{deployment_name}` convention `jobs.py`'s existing `INGEST_DEPLOYMENT =
"ms-ingest-video/ingest"` already relies on — verified live
(`f1.to_deployment(name="ingest").full_name == "f1/ingest"`) before touching
`worker.py`, so switching from the single-flow `.serve()` method to the
multi-flow `serve()` function provably preserves the video deployment's
existing identity (a hard invariant: the provided video contract must survive
unmodified).

**Implementation**:
- `src/jobs.py` — `DOCUMENT_DEPLOYMENT = "ms-ingest-document/ingest"` +
  `enqueue_document(doc_id, user_id, kind)`, mirroring `enqueue_video` exactly
  (same fire-and-forget `timeout=0` contract).
- `src/worker.py` — extracted `_build_deployments()` (returns both flows'
  `RunnerDeployment`s) so the deployment-construction logic is testable without
  running the actual blocking `serve()` loop (infra, not business logic — same
  reasoning `ingest_video.serve()` itself was never unit-tested under).
  `main()` now serves both deployments with `WORKER_CONCURRENCY` as one
  **shared** concurrency pool (a deliberate simplification: Prefect's `serve()`
  takes a single `limit` across all served deployments, and a worker VM's
  CPU/memory doesn't care which pipeline is using it). Also now eagerly
  ensures the text collection at startup (previously only the CLIP collection
  was), since it's shared by transcripts and documents alike.

**RED** (`uv run pytest tests/test_jobs_worker.py -v`, before implementation):
```
FAILED test_enqueue_document_calls_run_deployment_with_correct_contract - AttributeError: module 'src.jobs' has no attribute 'enqueue_document'
FAILED test_worker_serves_both_deployments_with_correct_full_names - AttributeError: module 'src.worker' has no attribute '_build_deployments'
2 failed, 2 passed in 1.41s
```
(The 2 passes were pre-existing-state guards — `INGEST_DEPLOYMENT` unchanged,
`dispatcher.py` has no document content — correctly green before any change,
since they assert what must NOT change.)

**GREEN**:
```
$ uv run pytest tests/test_jobs_worker.py -v
4 passed, 5 warnings in 1.42s
$ uv run pytest tests/ -q
40 passed, 7 warnings in 38.25s
```
(4 new + 36 from components 1–4, no regressions.) `enqueue_video`/`enqueue_document`
tested by mocking `prefect.deployments.run_deployment` at the module boundary —
a real call needs a live Prefect deployment + polling worker, out of scope for
a unit test (mirrors how `enqueue_video` itself was never tested before this
component, since no tests existed in the repo pre-Assignment-3 work).

**sla-gate deferred, not skipped** — same reason as component 4: `bench.py`
needs `POST /admin/documents` (component 6), which doesn't exist yet. The
accept-latency-relevant piece this component contributes (a fast,
fire-and-forget `enqueue_document` call) is structurally identical to
`enqueue_video`'s already-fast contract, verified via the mocked dispatch test.

**Still red / not yet built**: components 6–11 (admin API, search, UI,
benchmark, seeding).

**spec-guardian**: PASS. Verified independently: crash-safety ordering holds by
inspection (the single `"indexed"` write is the last line of `t_embed_index`,
strictly after the upsert); no protected files touched; video-path functions
only added-to; the two ID schemes structurally cannot collide. One low finding
(the new `source_id` index lived in the shared `_ensure()` helper, so it also
fired — harmlessly — against the video-only collection) fixed immediately:
moved into `ensure_text_collection()` specifically. Full suite re-confirmed
green after the fix.

**Commit**: `49142d8` — "Add document queue wiring: enqueue_document and
multi-deployment worker (component 5)". Note: `.env.example` was found to
contain real, live credentials (Neon/Prefect/Qdrant/OpenAI) mid-session —
excluded from this and every commit; moved to the gitignored `.env`, `.env.example`
reverted to placeholders, user advised to rotate all four. See conversation
record; not a code-quality finding, a security incident caught before push.

---

## 2026-07-27 — Component 6: admin router (`src/api/admin.py`)

**Scope** (DESIGN.md §3, row 6): "Admin router | `src/api/admin.py` (new) |
`POST /admin/documents` → validate, insert `pending`, enqueue, **202
immediately**; `GET /admin/sources` → union of videos+documents with `kind`,
`status`, `pct`; errors 400/401/502."

**Design choices, each grounded in an existing convention, not invented**:
- Auth/tenancy (`require_auth`, `user_id`) are **imported from
  `src.api.videos`**, not duplicated — the exact pattern `src/api/search.py`
  (an existing, provided file) already uses for its own Bearer-gated routes
  (`from .videos import require_auth, user_id as user_id_dep`).
- `GET /admin/sources` is **public/tenant-scoped, no Bearer required** —
  matches `GET /api/videos`'s existing convention (only mutating routes carry
  the admin-token dependency).
- `uri` validation accepts `http://`, `https://`, or `storage://` — the two
  shapes README's own contract examples show. A `storage://` uri is split into
  `uri` (kept as given, for reference) and `storage_key` (the raw key,
  immediately usable by `doc_pipeline.t_fetch`'s existing storage_key branch —
  no new fetch logic needed).
- `502` is raised specifically when `jobs.enqueue_document` fails — the
  Postgres insert already succeeded, so a scheduling failure is the upstream
  queue's fault (Prefect Cloud unreachable), matching "502 upstream failure"
  literally. The row is marked `failed` first so it's visible in
  `GET /admin/sources` rather than silently stuck `pending`.
- `app.py` (not a protected file) gained one import + one `include_router`
  line — the smallest possible touch to wire a third router onto the existing
  two.

**RED** (`uv run pytest tests/test_admin_api.py -v`, before implementation):
```
ERROR ... ImportError: cannot import name 'admin' from 'src.api'
9 errors
```

**GREEN** — all 9 passed on the first implementation attempt:
```
$ uv run pytest tests/test_admin_api.py -v
9 passed, 2 warnings in 3.33s
```
Covers: 202-before-work with the exact `{id, status, kind}` shape; 401 without
Bearer; 400 for bad `kind`, empty `uri`, and a disallowed scheme; the
`storage://` → `storage_key` split; 502 + `failed` status when the queue
schedule call raises; `GET /admin/sources`' unified shape (exact dict equality
against the README-documented fields) and its no-auth-required contract.

Doubles as the **contract-probe** layer (FastAPI's `TestClient` exercises the
real request/response/validation cycle in-process) until a live
`docker compose up` stack exists for the skill's live-curl variant.

**Full suite**:
```
$ uv run pytest tests/ -q
49 passed, 7 warnings in 57.71s
```
(9 new + 40 from components 1–5, no regressions.)

**sla-gate: now technically runnable, deliberately not run yet.** This is the
first component giving `bench.py` a real HTTP surface (`POST
/admin/documents`) to hit. Running it for real needs a live server process
plus a live Prefect Cloud connection actually scheduling runs (this session's
`.env` now holds real Neon/Prefect/Qdrant/OpenAI credentials — see the
component-5 security note above) — a bigger operational step (effectively
Part 0) than this component's own scope. Deferred to the next session step
rather than spending the user's real cloud credentials without being asked
first. The TestClient-based accept-fast contract (202 with no parsing in the
handler body — `jobs.enqueue_document` is the only post-insert call, and it's
fire-and-forget) is structurally verified above.

**Still red / not yet built**: components 7–11 (cross-source search, UI,
benchmark, seeding, self-serve tab). Part 0 (real stack, live `bench.py` run)
is now unblocked and a natural next step.

**spec-guardian**: PASS-with-warnings. Verified in code, not just claimed: the
202-before-work contract (only `upsert_pending_document` + `enqueue_document`
before returning), `app.py`'s diff is genuinely additive-only, auth reuse
matches `search.py`'s existing precedent exactly, and all four status codes
(202/401/400/502) trace to real `HTTPException` calls. Independently re-ran the
suite: `49 passed, 7 warnings in 55.99s`, matching. One low finding: ARCHITECTURE.md's
API-contract table listed `GET /admin/sources`' auth as plain "Bearer" with no
qualifier, inconsistent with the deliberate no-auth-required implementation
(which correctly mirrors `GET /api/videos`'s precedent). **Fixed**: table now
reads "— (tenant-scoped, no Bearer — matches `GET /api/videos`'s read-only
convention)".

**Commit**: `ca66c98` — "Add admin router: POST /admin/documents and GET
/admin/sources (component 6)". Follow-up ARCHITECTURE.md fix in the next commit.

---

## 2026-07-27 — Component 7: cross-source search (`src/rag/search.py`, `GET /ask_stream`)

**Scope** (DESIGN.md §3, row 7): "Cross-source search | `src/rag/search.py` +
`GET /ask_stream` | SSE endpoint wrapping the existing ask path; retrieval over
text collection now returns mixed kinds; citation carries `kind` + locator
(`start_ms` \| `page` \| `slide`); grounded — empty retrieval ⇒ empty
citations." The biggest logic-surgery component so far — `_fuse()` is the
heart of the existing (PROVIDED) RRF-fusion algorithm, and it had a real,
previously-latent bug this component surfaces and fixes.

**The bug**: `_fuse()`'s window-grouping loop directly indexed `h["video_id"]`
— fine while every text hit came from a video transcript (always has that
key), but the moment `moments_text` also holds paper/deck chunks (component
4's doc_pipeline, which have `source_id` instead), any query touching a
document hit would raise `KeyError: 'video_id'`. RED caught this immediately
and precisely (`src/rag/search.py:55: KeyError: 'video_id'`).

**Design decision**: paper/deck chunks are split out of the video
time-windowing entirely — `video_text_hits = [h for h in text_hits if
h.get("video_id")]`, `doc_text_hits` = the rest. A page or slide is already
the precise citation unit (unlike a raw ~20s transcript chunk, which benefits
from merging with a nearby frame); each document hit becomes its OWN window,
never merged with anything, never cross-modal-boosted (no visual companion is
retrievable for a paper/deck chunk in this design). Video-only behavior is
UNCHANGED — same grouping key, same rrf math, same cross-modal boost — proven
by a regression test using the exact payload shape the original code was
built for.

**Two downstream functions also needed the same fix** (not just `_fuse`):
`_build_moments` (`c["timestamp"]` direct-indexed — KeyError for a document
citation with no `timestamp` key) and `_fallback_answer` (same). Both now use
a new `_where(c)` helper that produces a human label regardless of kind
(`"14:22"` for video, `"page 4"` / `"slide 12"` for documents) — otherwise the
no-LLM fallback path and the LLM-moment-builder would both crash the instant a
cross-source result included a document citation. `_fallback_answer`'s wording
changed from "Closest visual match" to "Closest match" (no longer necessarily
visual) — the only observable text change, and necessary, not cosmetic.

**Backward compatibility (the hard invariant)**: every citation dict — video
included — gained two additive keys (`kind`, `locator`) but kept every
pre-existing flat field (`video_id`, `ms`, `idx`, `thumbnail`, `media_url`,
`deeplink`, `transcript`, `modalities`, `title`, `url`, `source`) unchanged.
Confirmed `POST /api/ask` has no `response_model=` constraining its output
(`grep` of `src/api/search.py`) — FastAPI serializes the dict as-is, so
additive keys are invisible to any code that doesn't look for them; a
regression test asserts every original field is still present for a video
citation.

**`GET /ask_stream`**: added to `src/api/search.py` (allowed to extend per
CLAUDE.md), wrapping `rag_search.ask()` exactly as DESIGN.md's row says — no
token-by-token rewrite of `llm.py`'s blocking `answer()` call, which "wrapping
the existing ask path" doesn't ask for and would be a much bigger, out-of-scope
change. Emits `trace` → `citations` (kind + locator, cross-source) → `answer` →
`done` as SSE events.

**Testing note — avoided a heavy dependency deliberately**: `retrieve()` calls
`embed_text()` (CLIP, needs `sentence-transformers`/torch) unconditionally for
the visual branch, even in tests that only care about the text/document
fusion logic. Rather than installing torch just to embed a throwaway query
vector, `embed_text` is mocked at the `rag_search` module boundary (returns a
correctly-sized 512-d zero vector) — the one real end-to-end test still uses
genuine fastembed (bge) embeddings for the text branch that actually matters,
and a real (unmocked) `vector_store.search()` against the empty CLIP
collection degrades gracefully to `[]`, exactly as production does when
Qdrant has nothing indexed yet.

**Product eval — labeled queries added** (`benchmark/labeled_queries.json`,
16 queries, 1-2 per `corpus.json` triplet): each expects specific citation
`kind`s for recall@10 (component 9 will resolve `corpus_id` to actual
video_id/doc_id once the corpus is seeded — component 10 — since document IDs
are randomly generated at registration, unlike video's deterministic
`yt_<id>`).

**RED** (`uv run pytest tests/test_cross_source_search.py -v`, before implementation):
```
KeyError: 'video_id' (src/rag/search.py:55) — 4 tests, the exact bug above
KeyError: 'kind' — 1 test (downstream of the same bug)
ModuleNotFoundError: No module named 'sentence_transformers' — 3 tests (fixed by mocking embed_text, not the implementation)
404 == 200 — 1 test (/ask_stream doesn't exist yet)
7 failed, 3 passed in 11.87s
```

**GREEN** — all 10 passed on the first implementation attempt:
```
$ uv run pytest tests/test_cross_source_search.py -v
10 passed, 2 warnings in 12.39s
```
Covers: video-only `_fuse` regression (unchanged); grounded-empty regression;
document hits becoming standalone windows; never cross-modal-boosted; two
different documents' same-page-number chunks staying separate citations
(no accidental merging); video citation shape fully backward-compatible
(every old field present) plus the new `kind`/`locator`; paper+deck citation
shape with correct `locator` per kind; grounded-empty-retrieval regression via
`ask()`; **a genuine end-to-end test with real embedded Qdrant + real
fastembed embeddings proving one query — "how does the attention mechanism
avoid recurrence?" — returns all three kinds (video, paper, deck) together**,
the assignment's #1 graded criterion; and the `/ask_stream` SSE shape,
including the exact self-verify check from README (`grep -m1 '"page"'`
against the citations event).

**Full suite**:
```
$ uv run pytest tests/ -q
59 passed, 7 warnings in 72.67s
```
(10 new + 49 from components 1–6, no regressions.)

**sla-gate**: recall@10 is now measurable in principle (retrieval + citation
shape both exist), but still needs the seeded corpus (component 10) and
bench.py's own implementation (component 9) to produce a real number — no
number fabricated here.

**Still red / not yet built**: components 8–11 (UI citation render, benchmark
implementation, corpus seeding, self-serve tab).

**spec-guardian**: PASS-with-warnings. Diffed `_fuse()` line by line and
confirmed the video-only path is byte-for-byte identical (rrf formula, window
grouping key, cross-modal boost condition, sort order) — the new document
branch is strictly additive, appended after video windows are built. Confirmed
`/api/ask`'s response shape is unchanged (no `response_model=` constraining
it) and every original citation field survives. Confirmed `/ask_stream` is a
thin wrapper with no logic duplication and no token-streaming rewrite.
Independently re-ran the suite: `59 passed, 7 warnings in 82.47s`, matching.
No other violations found — "the riskiest part of this change... genuinely
does survive unmodified."

**Commit**: `8dd3803` — "Add cross-source search: fix video_id KeyError,
kind+locator citations, GET /ask_stream (component 7)".

---

## 2026-07-27 — Component 8: UI citation render (`ui/index.html`)

**Scope** (DESIGN.md §3, row 8): "UI citation render | `ui/` | video → seek
player to `start_ms`; paper → link `uri#page=N`; deck → show slide
number/thumbnail."

**Testing constraint discovered before writing anything**: the app serves
`index.html` as a single inline-script page with NO static-file mount
(`grep -rn "StaticFiles\|mount(" src/ ui/` — nothing). Adding a separate
`citation.js` and `<script src="citation.js">` would 404 in production —
there's no route serving it. Fix: kept the pure kind/locator logic in its OWN
`<script id="citation-logic">` tag inside the same `index.html` (browsers
share global scope across `<script>` tags on one page, so this changes
nothing about how the page runs) — zero new routes, zero new files served.

**Testing approach**: no JS test framework exists in this repo. Rather than
add one (or skip testing entirely), `ui/citation.test.js` uses only Node
built-ins (`node:test`, `node:vm`) to regex-extract the `citation-logic`
script block directly OUT OF `index.html` and run it in a bare `vm` context —
this tests the ACTUAL shipped code every time (drift-proof: edit the block,
the test picks up the edit automatically), and needs no DOM stub since the
block is deliberately pure (no `document`/`window`/`fetch`).

**RED found a real bug, not a test-fixture issue**: the first `citeOpenUrl`
implementation sniffed the uri for a literal `.pdf` suffix before appending
`#page=N`. arXiv — our corpus's primary paper source — serves PDFs at URLs
like `https://arxiv.org/pdf/1706.03762`, with **no `.pdf` in the URL at all**
(the server sets `Content-Type`, not the path). The suffix heuristic would
have silently dropped the page anchor for exactly the case that matters most.
**Fix**: removed the sniffing entirely — a `#page=N`/`#page={slide}` fragment
is always appended when a locator exists. PDF viewers honor it; anything else
(e.g. a downloaded `.pptx`) silently ignores an unused fragment — harmless
either way, and simpler than the wrong heuristic it replaced.

**Design decisions**:
- `playCitation()` (the existing video modal — seek, YouTube embed, `<video>`
  seek) is **completely untouched**, byte-for-byte. A new `openCitation(n)`
  dispatches by `citeIsDocument(c)`: document citations `window.open()` the
  source at its locator; video citations call the unchanged `playCitation`.
  This is the safest way to guarantee "video path survives unmodified" — its
  own function body was never edited, only what calls it changed.
- "deck → show slide number/thumbnail": implemented the slide **number**
  (shown on the card, e.g. "Slide 12") and the **link** (opens the deck at
  that position). No slide **thumbnail** — `deck.py` (component 3) only ever
  uses a slide's rendered image transiently for LLM captioning; it's never
  persisted to object storage, so there is no URL to show a visual thumbnail
  from. Building that would mean persisting slide images too — an unplanned,
  bigger change DESIGN.md's row doesn't ask for outright ("thumbnail" is the
  second half of an "or" in the row's own phrasing). Documented as a known
  gap, not silently dropped.
- Field names used (`c.kind`, `c.locator.page`/`.slide`, `c.uri`,
  `c.source_id`) match exactly what `retrieve()` (component 7) emits — no
  naming drift between the Python citation shape and the JS consumer.

**RED** (`node --test ui/citation.test.js`, before implementation):
```
AssertionError: expected true, got null — citation-logic script block not found in index.html
tests 1, pass 0, fail 1
```

**GREEN** — 6/7 passed on the first implementation attempt; the 7th (arXiv
uri) failed for the real reason above, fixed by simplifying the logic (not
by weakening the test):
```
$ node --test ui/citation.test.js
tests 7, pass 7, fail 0
```
Covers: kind defaulting, document-vs-video dispatch, label text per kind
(timestamp / "Page N" / "Slide N"), page-anchor construction for arXiv-style
extensionless paper URLs and `.pdf`/`.pptx` deck URLs, missing-uri → null,
and an existing query-string on the uri being preserved before the fragment.

**Full Python suite unaffected** (no `.py` files touched by this component):
```
$ uv run pytest tests/ -q
59 passed, 7 warnings in 66.36s
```

**sla-gate**: N/A — no network/queue/index surface in this component.

**Still red / not yet built**: components 9–11 (benchmark implementation,
corpus seeding, self-serve tab). A live-browser check (actually clicking a
paper/deck citation card once real data is seeded) is still owed once Part 0
stands up the real stack — this component's evidence is unit-level only.

**spec-guardian**: PASS. Independently confirmed `playCitation()` is
byte-for-byte untouched (diffed the pre/post function body directly); the
static-file-mount claim verified by grep (no `StaticFiles`/`.mount(`
anywhere); re-ran both suites (`node --test`: 7/7; `pytest`: 59 passed) and
field-name parity between `retrieve()`'s citation dict and the JS reader —
all confirmed, not taken on faith. One non-blocking observation: `retrieve()`
(component 7) has a defensive `kind:"document"` fallback for a chunk payload
missing `kind` entirely, but `citeIsDocument` only checked for literal
`"paper"`/`"deck"`, so that fallback would have mis-routed to the video path.
**Fixed**: `citeIsDocument` now treats anything non-`"video"` as a document —
forward-compatible with any future kind, not just today's two. Added a test
for the `"document"` fallback case; 7/7 still pass.

**Commit**: `80cb427` — "Add cross-source UI citation rendering: video
unchanged, paper/deck open at locator (component 8)". Follow-up
forward-compat fix in the next commit.

---

## 2026-07-27 — Component 9: fill `benchmark/bench.py`'s 4 TODOs

**Scope** (DESIGN.md §3, row 9): "Benchmark | `benchmark/bench.py` | fill the
4 TODOs: labeled queries (recall@10), concurrent-ingest load, throughput
probe, worker-kill (`docker kill` the worker container mid-backfill, restart,
poll `/admin/sources`)."

**Honest framing, stated up front**: `bench.py` is fundamentally a black-box
HTTP client against a LIVE stack — accept-latency, search-p95-during-ingest,
real throughput, and the docker-kill resilience check all need a running
server + worker + Docker, none of which exist in this test environment (Part
0 — the real `docker compose up` with live Neon/Prefect Cloud/Qdrant Cloud —
was never stood up in this session; see the component-4/5/6 deferral notes
above). Filling the TODOs with real, correct code is this component's actual
deliverable; *running* them against a live stack is a distinct, later step
requiring the user's explicit go-ahead to spend real cloud credentials — asked
separately after this ships, not assumed.

**What's implemented for real** (all four TODOs, following the scaffold's own
structure):
1. **Recall@10**: `measure_recall()` loads `benchmark/labeled_queries.json`
   (component 7), hits `/ask_stream` per query, parses the SSE response via a
   new `_sse_events`/`_citations_from_sse` pair, and scores with
   `_score_recall` — the fraction of a query's `expect_kinds` present in its
   top-10 citations, averaged across queries. This is a documented *proxy*
   for true per-source recall@10 (labeled_queries.json's own comment: doc ids
   are non-deterministic, assigned at registration, unlike video's
   deterministic `yt_<id>`), not the literal metric name — an honest
   simplification, not a shortcut around the SLA.
2. **Concurrent-ingest load**: `run_concurrent_ingest_load()` submits REAL
   arXiv paper/deck PDFs from `benchmark/corpus.json` (via a new
   `_load_corpus_uris`/`_cycle_to_n` pair), not throwaway `example.com` URLs —
   genuine fetch/parse/embed work is what actually contends for CPU during a
   backfill; a URL that 404s immediately wouldn't stress anything. Run
   concurrently with `measure_search_p95()` via a `ThreadPoolExecutor`, not
   sequentially before/after.
3. **Throughput**: `measure_throughput()` submits a batch, polls
   `GET /admin/sources` until every id reaches a terminal status
   (`_poll_sources_until_terminal`), and divides total indexed `chunk_count`
   by elapsed seconds. This needed one additive field on the public API:
   `list_sources()`/`GET /admin/sources` now includes `chunk_count`
   (`frame_count` for video, `chunk_count` for documents) — without it,
   `bench.py` would have had no way to compute chunks/s without reaching
   into the DB directly, which a black-box benchmark script must never do.
   Updated the two existing exact-dict-equality tests (component 1 and 6)
   that asserted the old 5-field shape — a deliberate, disclosed schema
   extension, not a weakened eval.
4. **Resilience**: `run_resilience_check()` submits a batch, waits
   `kill_after_s` for real ingestion to start, discovers the worker container
   via `docker compose ps -q worker` (no hardcoded container name — robust to
   whatever project-name convention Compose computes for this directory), and
   `docker kill`s it. Confirmed `docker-compose.yml`'s `worker` service
   already has `restart: unless-stopped` — Compose brings it back
   automatically, no manual `docker compose up -d worker` needed in the
   script. Then polls to a terminal state and asserts nothing is permanently
   stuck.

**RED** (`uv run pytest tests/test_bench.py -v`, before implementation):
```
AttributeError: module 'benchmark.bench' has no attribute '_score_recall'
AttributeError: module 'benchmark.bench' has no attribute '_load_corpus_uris'
AttributeError: module 'benchmark.bench' has no attribute '_cycle_to_n'
12 failed in 0.04s
```
(Added `benchmark/__init__.py` — empty — so `benchmark/bench.py` is
importable as `from benchmark import bench` for testing; it still runs
exactly the same as `python benchmark/bench.py`.)

**GREEN** — all 12 passed on the first implementation attempt:
```
$ uv run pytest tests/test_bench.py -v
12 passed in 0.05s
```
Covers every PURE piece with no network involved: SSE parsing (multiple
events, empty body), citations extraction, recall scoring (full coverage,
partial coverage, cross-query averaging, a missing query result, an empty
labeled set), corpus-URI loading (16 entries — 8 triplets × paper+deck, all
real `http` URLs with titles), and the cycle-to-n batch sizer (repeats and
truncates correctly in both directions).

**Full suite** (including the two updated `chunk_count` schema tests):
```
$ uv run pytest tests/ -q
71 passed, 7 warnings in 64.87s
```
(12 new + 59 from components 1–8, no regressions.) `bench.py --help` also
sanity-checked to run cleanly as a script.

**What remains explicitly UNRUN, by design, not oversight**: `measure_accept_latency`,
`measure_search_p95`, `run_concurrent_ingest_load`, `measure_recall`,
`measure_throughput`, `run_resilience_check` — the actual HTTP-calling glue —
have never executed against a live server in this session. No SLA number in
this entry is fabricated; none is reported at all, because none has been
measured. The next step is asking the user whether to stand up Part 0 (the
real stack) so these can run for real.

**Still red / not yet built**: components 10–11 (corpus seeding, self-serve
tab). Part 0 remains the prerequisite for this component's own live run.

**spec-guardian**: PASS, no findings. Independently verified: every `gate()`
call receives a value from a real measurement function (no hardcoded
placeholder feeding a gate); `benchmark/sla.json` untouched; the
`chunk_count` schema addition is genuinely additive (reads pre-existing
Postgres columns, no consumer breaks, both updated tests disclosed
honestly); the resilience check uses dynamic `docker compose ps -q worker`
container discovery (no hardcoded name) and correctly relies on the worker
service's pre-existing `restart: unless-stopped` policy rather than assuming
one. Re-ran the suite independently: `71 passed, 7 warnings in 65.73s`,
matching exactly.

**Commit**: `d936117` — "Fill benchmark/bench.py's 4 TODOs: recall,
concurrent load, throughput, resilience (component 9)".

---

## 2026-07-27 — Component 10: seed the triplet corpus (`src/seeding.py`, `src/samples.py`)

**Scope** (DESIGN.md §3, row 10): "Seed the triplet corpus | `src/seeding.py`,
`src/samples.py` | extend the boot-time seed gate to ingest
`benchmark/corpus.json` (8 papers + 8 decks + 8 talks) alongside the sample
videos — a fresh deploy is cross-source queryable on first load, idempotent
like today."

**Design decisions**:
- **"Alongside," not "instead of."** The base app's original 4 sample videos
  (3Blue1Brown/Karpathy — unrelated to our research corpus) are left
  completely untouched; the 8 corpus triplets seed in the SAME pass via a
  combined `_all_videos()` list, matching DESIGN.md's literal wording.
- **Deterministic seed IDs for documents** — `_seed_doc_id(corpus_id, kind)`
  → `doc_seed_<id>_<kind>` (e.g. `doc_seed_attention_paper`), NOT the admin
  API's random `uuid4` used for one-off user registrations. This is the
  idempotency-critical decision: without a stable ID, every container
  restart would insert a brand-new row and re-seed from scratch forever,
  violating "idempotent like today."
- **New `SEED_CORPUS` flag** (default `true`), independent of the existing
  `SEED_SAMPLE_VIDEOS` — 24 sources is heavier than the original 4, so local
  dev can disable just the corpus without losing the base app's own seeding.
- **`samples.py` extended, not rewritten**: added `_load_corpus()` (reads
  `benchmark/corpus.json` — the SAME file the benchmark uses, so seeding and
  grading never drift apart) and folded the 8 corpus video ids into
  `SAMPLE_IDS`/`is_sample()` — they get the same delete-protection the
  original 4 already have, for the same reason (the seed gate would just
  re-add them).
- **Honest failure semantics**: `seed_to_completion()` returns `True` only if
  EVERYTHING ends up indexed. A single permanently-broken source returns
  `False` — proven by a test where 15 of 16 documents succeed and one is
  poisoned; the function correctly reports failure rather than a false pass,
  while still seeding everything else (no source silently blocks its peers).

**Testing boundary**: this tests ORCHESTRATION (not-indexed detection, retry
passes across `_MAX_PASSES`, deterministic idempotency, "alongside"
combining, the flag) — not the ingestion pipelines themselves, already
tested (`ingest_video` is PROVIDED; `ingest_document` was tested in component
4). `ingest_video`/`ingest_document` are mocked at the `seeding` module
boundary: a real call downloads real videos/PDFs from the internet, which a
unit test must never do.

**RED** (`uv run pytest tests/test_seeding.py -v`, before implementation):
```
AttributeError: module 'src.seeding' has no attribute '_not_indexed_documents'
AttributeError: module 'src.seeding' has no attribute '_seed_doc_id'
AttributeError: module 'src.seeding' has no attribute '_not_indexed_videos'
AttributeError: module 'src.seeding' has no attribute 'ingest_document'
AttributeError: module 'src.config' has no attribute 'SEED_CORPUS'
8 failed in 9.09s
```

**GREEN** — all 8 passed on the first implementation attempt:
```
$ uv run pytest tests/test_seeding.py -v
8 passed, 6 warnings in 8.45s
```
Covers: deterministic seed-id generation (same triplet+kind → same id, always);
fresh-DB not-indexed listing (16 documents, 12 videos = 4 base + 8 corpus);
excluding already-indexed sources; a full successful seed indexing everything
(sample videos AND corpus triplets together); a transient failure retried and
recovered within `_MAX_PASSES`; a permanent failure honestly reported as
`False` while every other source still gets seeded; and the `SEED_CORPUS=false`
flag skipping documents *and* corpus videos while still seeding the base 4.

**Full suite**:
```
$ uv run pytest tests/ -q
79 passed, 7 warnings in 79.25s
```
(8 new + 71 from components 1–9, no regressions.) Confirmed `examples/quickstart.py`
(PROVIDED, imports `SAMPLE_VIDEOS`/`sample_video_id` from `samples.py`) is
unaffected — both names are unchanged in content, only new names were added.

**sla-gate**: N/A at the unit level (no live stack). This component is what
makes component 9's `recall@10` and the demo's cross-source query actually
possible once Part 0 runs — still deferred, per the user's explicit choice
to finish all 11 components before standing up the real stack.

**Still red / not yet built**: component 11 (self-serve ingest tab). Actually
seeding 24 real sources (8 videos via yt-dlp+CLIP, 16 documents via
fetch+parse+embed) has never been run — that's real network/compute work
appropriately deferred to Part 0, not something to fabricate here.

**spec-guardian**: PASS, no findings. Independently verified: `SAMPLE_VIDEOS`'s
list body has zero diff lines (only additions around it); the deterministic
`doc_seed_<corpus_id>_<kind>` id is the single source used consistently by
`_corpus_documents()`, `_not_indexed_documents()`, and `seed_to_completion()`
— no parallel id derivation exists; `examples/quickstart.py` (PROVIDED)
confirmed unaffected; the honest-failure loop genuinely attempts every source
every pass (per-item try/except, no early abort) and only returns `False`
when something is truly still unindexed after all passes. Re-ran the suite
independently: `79 passed, 7 warnings in 70.37s`, matching.

**Commit**: `bc06390` — "Extend boot-time seed gate to ingest the 8 corpus
triplets (component 10)".

---

## 2026-07-27 — Component 11: self-serve ingest tab (`ui/index.html`)

**Scope** (DESIGN.md §3, row 11): "the existing ingest box (YouTube URL / Upload
tabs) gains a 'Paper / Deck' tab → `POST /admin/documents`; the library panel
shows document lifecycle + retry, tenant-scoped like videos." This is the
11th and final DESIGN.md component.

Gap found during scoping: DESIGN.md's row explicitly asks for "+ retry", but
component 6 only shipped `POST /admin/documents` and `GET /admin/sources` —
no retry route exists for documents (unlike videos' `POST /api/videos/{id}/retry`).
Added `POST /admin/documents/{doc_id}/retry` to `src/api/admin.py` (not a
protected file — it's the component-6-created router), mirroring `videos.py`'s
retry but always direct-enqueuing (no fair-dispatch branch, consistent with
component 5's decision that documents ride FIFO).

**Evals defined**:
- Unit (Python): 4 new tests in `tests/test_admin_api.py` for the retry route
  (202+pending on success, 401 without Bearer, 404 missing doc, 404 wrong tenant).
- Unit (JS, pure logic): new `ui/ingest.test.js` (7 tests) exercising a new
  DOM-free `<script id="ingest-logic">` block in `index.html` — `docBadge(status)`
  (lifecycle icon/label incl. `parsing`, a status documents have that videos
  don't) and `buildDocumentPayload(kind, uri, title)` (client-side mirror of
  `admin.py`'s validation). Same node:vm regex-extraction pattern as component 8's
  `citation.test.js` — no DOM stub, no new JS framework.
- Contract probe: `tests/test_admin_api.py` doubles as the contract-probe layer
  again (TestClient, in-process), per the convention component 6 established —
  no live stack exists yet (Part 0 still deferred).
- SLA relevance: none directly. This is a UI-only feature plus one lightweight
  endpoint mirroring an existing pattern; the accept-latency path it rides
  (`POST /admin/documents`) was already SLA-tested in component 6/9.

**RED** (before implementation):
```
$ uv run pytest tests/test_admin_api.py -x -q
assert resp.status_code == 401
E       assert 404 == 401
1 failed, 9 passed in 12.59s   # /admin/documents/{id}/retry route doesn't exist

$ node --test ui/ingest.test.js
generatedMessage: false, code: 'ERR_ASSERTION', actual: null, expected: true
✖ ui/ingest.test.js — <script id="ingest-logic"> block doesn't exist yet
```

**IMPLEMENT**:
- `src/api/admin.py`: `POST /admin/documents/{doc_id}/retry` (202, Bearer-gated,
  tenant-scoped 404, resets status to pending + clears error + re-enqueues).
- `ui/index.html`:
  - Third tab "Paper / Deck" in `#addTabs` (kind select, URI input, optional
    title input, register button) — hidden by default like the existing tabs,
    and hidden entirely in sample mode via the existing `applyMode()` logic
    (no new visibility branch needed).
  - `<script id="ingest-logic">`: `docBadge(status)`, `buildDocumentPayload(...)`.
  - `loadDocuments()`: fetches the already-unified `GET /admin/sources`
    (component 6), filters to non-video rows, renders chips with lifecycle
    badge + retry button on `failed`. Deliberately does NOT touch `loadVideos()`
    or reuse `/admin/sources` for videos — `/api/videos` stays the single
    source of truth for video rendering (is_sample, frame_count, thumbnails),
    so this component is purely additive.
  - `$("#docBtn").onclick`: validates via `buildDocumentPayload`, POSTs to
    `/admin/documents`, matches the existing `registerIngest` UX (clear inputs,
    "Queued — processing in the background", triggers the shared refresher).
  - `startRefresher()`/`_INFLIGHT` extended to poll `loadDocuments()` alongside
    `loadVideos()` and to include `"parsing"` (a document-only status) in the
    in-flight set.

**GREEN**:
```
$ node --test ui/*.test.js
ℹ tests 13
ℹ pass 13
ℹ fail 0

$ uv run pytest tests/ -x -q
83 passed, 7 warnings in 79.88s
```
(4 new Python tests + 79 from components 1–10 = 83, no regressions; 6 existing
+ 7 new JS tests = 13, no regressions.)

**Still deferred**: exercising this in an actual browser against a live stack,
and the live-curl contract-probe checklist — both wait on Part 0 (the user's
explicit choice to finish all 11 components first). No UI screenshot/manual
click-through was performed in this session; only the pure-logic and
TestClient layers were run.

All 11 DESIGN.md components are now implemented.

**spec-guardian**: PASS, no findings. Independently verified: `src/api/videos.py`,
`src/dispatcher.py`, `src/ingest/*.py`, `benchmark/sla.json`, `eval/rubric.json`
have zero diff lines in this commit; the new retry route's tenant check is a
verbatim match of `videos.py`'s own pattern (404 for both missing and
wrong-tenant, no info leak); `loadDocuments()` writes only to `#documents`
and never touches `#videos`/`loadVideos()`'s state; every status
`doc_pipeline.py`/`db.py` can actually produce (pending, fetching, parsing,
embedding, indexed, failed, skipped) has a correctly-spelled case in
`docBadge()` (one harmless unused `"queued"` case noted as dead code, not a
defect — documents never emit it). Re-ran independently: `pytest tests/ -q`
→ 83 passed; `node --test ui/*.test.js` → 13 passed. Matches this entry.

**Commit**: `7ac58d4` — "Add self-serve Paper/Deck ingest tab with document
retry (component 11)".

---

## 2026-07-28 — Part 0: real stack, sla-gate, contract-probe, grounding-auditor

All 11 DESIGN.md components were built against mocks/local Postgres/embedded
Qdrant. This session stood up the REAL stack (`docker compose up`, real Neon,
real Prefect Cloud, real Qdrant Cloud, real OpenAI) for the first time and ran
it for real, per the user's earlier choice to finish all 11 components before
Part 0. Three real bugs were found and fixed; two real operational gaps and
two real grounding violations were found and are disclosed, not fixed, below.

### Bug 1 — Dockerfile never copied `benchmark/` into the image (found+fixed)

`Dockerfile` only `COPY`'d `src/` and `ui/`. `src/samples.py`'s `_load_corpus()`
reads `benchmark/corpus.json` at runtime and silently returns `[]` if the file
is missing (a deliberate dev-convenience fallback). Inside the container this
meant `CORPUS = []` regardless of `SEED_CORPUS`, so the first `docker compose
up` seeded only the 4 base sample videos — the 8 corpus videos and 16
documents from component 10 never seeded, with no error at all.
**Fix**: `COPY benchmark/corpus.json benchmark/corpus.json` added to
`Dockerfile`. Rebuilt, re-seeded: `docker compose logs seed` confirmed
`[seed] pass 1/3: 8 video(s), 16 document(s)` then `[seed] corpus complete —
everything indexed`. Verified via `GET /admin/sources`: 28 sources, all
`indexed` (`Counter({'video': 12, 'deck': 8, 'paper': 8})`,
`Counter({'indexed': 28})`).

### Bug 2 — Prefect deployments used FILE_PATH entrypoints, crashing every real registration (found+fixed)

`worker.py`'s `_build_deployments()` called `flow.to_deployment(name="ingest")`
with Prefect's default `entrypoint_type=EntrypointType.FILE_PATH`. A worker
executing an actually-SCHEDULED run re-imports the flow fresh via
`load_script_as_module(script_path)` — loading e.g. `src/ingest/doc_pipeline.py`
as a bare script with no parent package. Both pipeline modules use relative
imports (`from .. import db, llm, storage`), which then raise
`ImportError: attempted relative import beyond top-level package`. This
crashed **every single** real document (and would have crashed every real
video) registration submitted through the actual API — `seed.py` never hit
this because it calls `ingest_video`/`ingest_document` as plain in-process
function calls, bypassing Prefect scheduling entirely. This is why it
survived 11 components of test coverage: nothing before this session ever
exercised a real worker executing a real scheduled run.

Root-caused via `docker compose logs worker` (`Unexpected exception
encountered when trying to load flow` → `ImportError: attempted relative
import beyond top-level package`, 100% reproducible on every submitted
document). Confirmed `Flow.to_deployment(entrypoint_type=...)` and
`EntrypointType.MODULE_PATH` exist in the installed Prefect version by
inspecting it inside the worker container. `MODULE_PATH` loads via
`importlib.import_module("src.ingest.doc_pipeline")` — normal package import,
relative imports resolve fine.

**Fix**: `src/worker.py`'s `_build_deployments()` now passes
`entrypoint_type=EntrypointType.MODULE_PATH` for both flows. New regression
test `test_worker_deployments_use_module_path_entrypoints` in
`tests/test_jobs_worker.py` asserts `d.entrypoint_type == EntrypointType.MODULE_PATH`
and `".py:" not in d.entrypoint` for both deployments — RED before the fix
(`AssertionError`), GREEN after.

**Live verification**: rebuilt+restarted the worker, submitted a fresh
duplicate-of-seeded document (`doc_1e9fc890ff`, BERT paper). Worker log:
`Beginning flow run` → `doc-fetch` `Finished in state Completed()` →
`[ingest] doc_1e9fc890ff skipped (duplicate content)` → `Finished in state
Completed()`. No crash. Confirmed both deployments' entrypoints via
`docker compose exec worker python -c "from src.worker import
_build_deployments; ..."`:
```
ms-ingest-video/ingest    | entrypoint= src.ingest.pipeline.ingest_video    | type= EntrypointType.MODULE_PATH
ms-ingest-document/ingest | entrypoint= src.ingest.doc_pipeline.ingest_document | type= EntrypointType.MODULE_PATH
```

### Bug 3 — bench.py's own load/throughput tests dedup-shadowed the seeded corpus (found+fixed)

`measure_throughput()`, `run_concurrent_ingest_load()`, and
`run_resilience_check()` all submitted documents built from the same
`benchmark/corpus.json` URIs already seeded (component 10) under
`user_id='default'`, with no `X-User-Id` header — so every submission
deduped (`find_duplicate_document`, tenant-scoped) against the seeded corpus
and landed on `skipped`, never `indexed`, no matter how fast real ingest
actually was. `measure_throughput()` only sums `chunk_count` for
`status=='indexed'` ids, so it read `0.0` structurally, independent of the
Prefect crash (bug 2) — confirmed directly: after fixing bug 2, a full
bench.py re-run still produced `ingest_throughput_chunks_per_s: 0.0`, and
querying Postgres showed all 16-36 bench-submitted docs at `status='skipped'`,
zero newly `indexed`.

**Fix**: added `_fresh_bench_tenant(label)` (mints `bench-<label>-<uuid8>`)
and threaded a `user` param through `_req`/`_submit_documents`/
`_poll_sources_until_terminal`; `run_concurrent_ingest_load`,
`measure_throughput`, and `run_resilience_check` now each register under
their own fresh tenant per call, so their submissions genuinely parse+embed
instead of instantly deduping. Three new tests in `tests/test_bench.py`
(`test_req_sets_x_user_id_header_when_user_given`,
`test_req_omits_x_user_id_header_when_no_user_given`,
`test_fresh_bench_tenant_is_unique_per_call_and_labeled`) — RED before
(`_req` had no `user` param), GREEN after. Full suite: `uv run pytest tests/
-x -q` → **87 passed**.

### Operational finding — worker/Prefect-runner froze solid under sustained load (found, worked around, NOT fixed in code)

After the bug-3 fix, a bench run submitted ~66 documents in a burst (30
deliberately-fake accept-latency probes + 20 load + 16 throughput) against
only 4 concurrent execution slots (2 workers × `WORKER_CONCURRENCY=2`).
Partway through, `docker stats` showed both worker containers at 0-3% CPU
and `docker compose logs worker` repeated `"44 scheduled runs skipped (at
capacity)"` **unchanged** for 13+ minutes — genuinely stuck, not slow
(`updated_at` on the pending rows was frozen). `docker compose restart
worker` cleared it immediately; real fetch→parse→caption→embed→index cycles
resumed at ~15-20s/document. This looks like a Prefect `Runner`
internal-concurrency-accounting leak (a crashed/orphaned execution not
releasing its slot) rather than anything in this repo's own code. **Not
fixed this session** — flagged as a real production concern: the worker
service should have a liveness health-check + auto-restart policy (or the
Prefect runner's concurrency-leak needs its own investigation), since
`restart: unless-stopped` alone does not help when the process itself doesn't
exit, it just stops making progress.

### Operational finding — the resilience gate's crash-recovery is NOT automatic

`bench.py --resilience` submitted 10 documents under a fresh tenant,
`docker kill`ed the worker mid-ingest, and polled. Result:
```
$ uv run python benchmark/bench.py --resilience
[resilience] killed worker container ecfaf0a0378a mid-ingest
[resilience] 2 source(s) never reached a terminal state: ['doc_665584d466', 'doc_d928b50a31']
[FAIL] no_loss_under_crash: False (target 0 dropped, all indexed)
```
Investigated two layers:
1. **Docker never auto-restarted the killed container.** `docker compose ps
   -a` showed `worker-1: Exited (137) 5 minutes ago` — `restart:
   unless-stopped` (confirmed set via `docker inspect`, `restartCount=0`)
   simply never fired for this `--scale`-created replica. Manually running
   `docker compose up -d --scale worker=2` brought it back.
2. **Even with a worker available again, the two interrupted documents
   stayed `pending` indefinitely** — `ingest_video`/`ingest_document`'s
   `@flow(...)` decorators (`pipeline.py:157`, `doc_pipeline.py:164`) set
   `timeout_seconds=3600` but no `retries`. A hard-killed process doesn't
   raise a Python exception Prefect can retry on; without flow-level
   retries or an external reconciliation sweep, an interrupted run has no
   automatic path back to execution. Manually calling this session's own
   `POST /admin/documents/{id}/retry` (component 11) on both stuck ids
   immediately re-enqueued them and they completed normally (worker log:
   `Beginning flow run` → `doc-fetch Completed` for both). So recovery
   **is possible but is operator/automation-driven, not automatic** — the
   ARCHITECTURE.md/DESIGN.md assumption ("Prefect redelivers the interrupted
   run once a worker is polling again") does not hold for a hard kill
   without flow-level retries.
**Not fixed this session** (would need `retries=N` on both flows — one of
which, `pipeline.py`, is a CLAUDE.md-protected file — plus verification that
Prefect actually retries a `Crashed` state the same way it retries a
`Failed` one, which needs its own investigation). `no_loss_under_crash`
remains **FAIL** as measured; disclosed here with full root cause and a
proven manual recovery path.

### Contract-probe checklist (`.claude/skills/contract-probe/SKILL.md`, `BASE_URL=http://localhost:8000`)

| # | Probe | Result |
|---|---|---|
| 1 | `GET /` → 200 | **PASS** |
| 2 | `POST /admin/documents` (Bearer, valid) → 202, `{id,status,kind}`, <1s | **PASS** (202, 0.987s) |
| 3 | Auth: no/wrong Bearer → 401 | **PASS** (both) |
| 4 | Validation: bad kind / bad scheme → 400 | **PASS** (both) |
| 5 | `GET /admin/sources` unified shape | **PASS** (video/paper/deck rows all correct shape) |
| 6 | Provided endpoints unchanged: `/api/health` 200, `/api/ask` 200 with answer+citations | **PASS** |
| 7 | Cross-source citations: one query → ≥2 kinds, `start_ms` + `page` locators | **PASS** ("how does attention avoid recurrence" → video `start_ms` + paper `page` in one response) |
| 8 | Grounding on nonsense query: `q=zorbulax+quantum+pickles` | **SOFT FAIL** — see grounding-auditor verdict below: real citations returned (not invented), `abstained:false`, but the checklist's literal "abstain/empty citations" expectation isn't met. Root cause: `CONFIDENCE_THRESHOLD`/`TEXT_CONFIDENCE_THRESHOLD` gate on raw per-branch similarity, which nonsense queries can still weakly clear. Not touched — this is PROVIDED confidence-gate logic, not a component built this session. |

### sla-gate (`benchmark/bench.py`, `benchmark/sla.json` — thresholds untouched)

| Metric | Target | Run 1 (pre-fix) | Run 2 (bug 2 fixed) | Run 3 (bug 3 fixed) |
|---|---|---|---|---|
| `accept_latency_p95_ms` | ≤300 | 1277.7 FAIL | 1369.6 FAIL | 1280.5 FAIL |
| `search_p95_during_ingest_ratio` | ≤1.3 | 0.77 PASS | 0.97 PASS | 1.02 PASS |
| `recall_at_10` | ≥0.70 | 0.604 FAIL | 0.604 FAIL | 0.667 FAIL |
| `ingest_throughput_chunks_per_s` | ≥8 | 0.0 FAIL | 0.0 FAIL | 0.0 FAIL |

All three runs' exact console output preserved in session logs. Every number
above is verbatim from `uv run python benchmark/bench.py` — none rounded
favorably or omitted.

**accept_latency_p95_ms — genuinely network-bound, not a code bug.** Isolated
directly inside the running `api` container (`docker compose exec api
python -c "..."`, 4 back-to-back warm calls): `db.upsert_pending_document`
≈400ms, `jobs.enqueue_document` (Prefect Cloud `run_deployment`) ≈500-600ms,
every single call, steady-state — matching the ≈950-1030ms curl end-to-end
measurements exactly. This is two real network round trips (Neon Postgres +
Prefect Cloud) from a home/office machine to two separate managed clouds,
consistent with CLAUDE.md's own architecture invariant (insert+schedule,
zero parsing in the request path — the code is correct). The 300ms target
almost certainly assumes a co-located topology (e.g., Fly.io + Neon + Prefect
Cloud in the same region), not this local topology. **Recommend
re-measuring after the Fly.io deploy** before deciding this gate is
unreachable.

**recall_at_10 — real gap, with a precise, disclosed mechanism.** bench.py's
own official numbers (0.604, 0.604, 0.667) all FAIL. A clean, uncontended
re-run of the same 16 labeled queries via direct curl (run only after all
concurrent bench-generated ingest load had fully drained, confirmed via
`docker stats` near-idle) scored **0.729** — above target — on the identical
scoring logic. The gap between bench.py's own runs and this clean re-check is
real and explainable: `measure_recall()` runs immediately after
`run_concurrent_ingest_load()` fires 20 real registrations, so bench.py's own
official recall measurement is itself contending with real background
ingest/embedding/LLM-captioning load — a genuine interaction, not a
discrepancy to paper over.

Root mechanism for the residual gap (found by reading `src/rag/search.py`,
NOT modified): `TOP_K=6` (`src/config.py:286`) caps the final citations list
at 6 windows — the system architecturally never returns more than 6,
regardless of `labeled_queries.json`'s "top-10" framing. Video windows where
both frame+transcript agree get `CROSS_MODAL_BOOST=1.5×` (`_fuse()`,
`search.py:79-80`); paper/deck windows get no such boost. With only 6 total
slots and one modality boosted, whichever document kind (paper or deck)
scores lower via RRF for a given query is often the one squeezed out — the
uncontended diagnostic's missing-kind tally was `{'deck': 5, 'paper': 5}`,
evenly split, not skewed to one kind. This is a genuine product tradeoff
(fewer, more focused citations → cheaper/faster LLM calls), not a bug in
this session's components; **not modified**, per instruction to diagnose
only.

**ingest_throughput_chunks_per_s — 0.0 in all three official runs, for three
different reasons in sequence**: run 1 = worker crash (bug 2); runs 2-3 =
bench's own dedup-shadowing (bug 3, fixed mid-session, so run 3 still read
0.0 because the *fix* for run 3 hadn't landed until after run 3 started) /
then a genuine worker/Prefect-runner freeze (see operational finding above)
ate the entire 600s poll window. After manually restarting the worker and
letting the backlog drain to completion (outside bench.py's own poll
window), all 16 `bench-throughput-*` documents did reach `indexed`
(832 total chunks). Two honest supplementary numbers, computed post-hoc,
NOT from an official bench.py run: including the ~13-minute freeze,
832 chunks / 1308s ≈ **0.64 chunks/s**; excluding the freeze (from the
`docker compose restart worker` timestamp to completion), 832 chunks / 479s
≈ **1.74 chunks/s**. Both are still below the 8 chunks/s target — a real,
disclosed gap, not just a measurement artifact, though the true steady-state
number is likely higher than either (LLM-captioning rate limits — see
grounding-auditor section — were also throttling this same window: worker
log shows repeated `RateLimitError: ... tokens per min (TPM): Limit 200000,
Used 200000` during this exact period).

### grounding-auditor verdict

Spawned an agent to adopt `.claude/agents/grounding-auditor.md`'s persona
against the live stack. Findings, most-severe first:

1. **CRITICAL — fabricated answer for a nonexistent source.** Query "What
   does the Mamba paper say about state space models" (no Mamba/SSM source
   exists in the 58-source corpus, confirmed via `/admin/sources`) got a
   confident answer inventing Mamba-paper content, built on a real citation
   that is actually an unrelated RAG-talk transcript snippet (misheard "RAG
   models" as "rack models"). Should have abstained (empty corpus for this
   topic) and did not.
2. **HIGH — real citation, fabricated claim.** Query about GPT-3's
   "ARC-AGI benchmark" accuracy got specific percentages attributed to a
   real, correctly-numbered citation (GPT-3 paper, page in range) — but the
   cited page's actual numbers are for ARC (Easy), not ARC (Challenge) as
   claimed; the answer's specific statistic does not match what the source
   says.
3. **LOW — the zorbulax case, re-confirmed.** All cited content is real and
   indexed; the answer text correctly disclaims relevance
   ("does not appear in the provided moments"). `abstained:false` is a
   metadata-label inaccuracy, not an invented citation.
4. **Tenant isolation — PASS.** A novel `X-User-Id` sees zero sources via
   `/admin/sources` and gets `abstained:true`/empty citations from
   `/ask_stream` — no cross-tenant leakage.
5. **Spot-check on an on-topic query — PASS.** Citations' locators
   (slide/page numbers) were internally consistent and plausible.

Findings 1-2 are **new, real grounding violations** distinct from and more
severe than the zorbulax soft-fail already known from the contract probe —
the LLM answer-synthesis step (`src/llm.py`/`_validate_citations` in
`search.py`) validates that cited `[n]` markers refer to real, existing
citations, but does not verify that the answer's specific *claims* are
actually supported by the cited text's content. **Not fixed this session**
(would require either a citation-faithfulness check post-generation or a
stricter abstain condition when the corpus has zero genuinely on-topic
matches) — disclosed here as the most important open item for whoever picks
up grounding work next.

### Summary of what's still red, honestly

- `accept_latency_p95_ms`, `recall_at_10`, `ingest_throughput_chunks_per_s`,
  `no_loss_under_crash`: all **FAIL** as officially measured by
  `benchmark/bench.py` against the frozen `benchmark/sla.json` thresholds.
  Each has a precise, real, investigated root cause above — none are
  mysteries, none are code bugs left unfixed out of neglect, and none of the
  thresholds were touched.
- Three real code bugs (Dockerfile, Prefect entrypoint, bench.py
  dedup-shadowing) were found and fixed, each verified live.
- Two real operational gaps (worker/Prefect-runner freeze under load;
  crash-recovery is operator-driven, not automatic) were found, worked
  around live, and disclosed — not fixed in code.
- Two real, more-severe-than-previously-known grounding violations
  (fabricated Mamba-paper answer; misattributed GPT-3 benchmark statistic)
  were found by the grounding-auditor agent and disclosed — not fixed.

**Commit**: `5587682` — "Fix corpus seeding in Docker, Prefect entrypoint crash,
and bench.py dedup-shadowing (Part 0)".

---

## 2026-07-28 — Grounding fix: source-title grounding for the 2 violations found above

The user directed that AGENTS.md's non-negotiable #5 ("Grounded citations
only... Empty retrieval → empty results, not a fabricated one") is a must,
not a disclosed-and-deferred item — fix it, not just report it. Followed the
`edd` loop as maintenance/hardening of existing component 7
(`src/rag/search.py` + `src/llm.py`), not a new DESIGN.md component.

**Root cause, found by reading the actual code path the two violations went
through**: `_build_moments()` (`src/rag/search.py`) turned each citation into
`{"image", "transcript", "timestamp"}` for the LLM — it never included WHICH
source (video/paper/deck title) a moment came from, even though every
citation already carries a `title` field (used by the UI). The LLM had no
structural way to check a question naming a specific work ("the Mamba
paper") against what was actually retrieved — only prose text to guess from.
Combined with the system prompt's strong anti-abstain instruction ("if even
one moment is relevant, ANSWER from it — do not refuse"), a weakly-similar
but unrelated moment got recruited into a confident, unlabeled answer.

**Evals defined** (this is inherently an LLM-output-quality fix — most of it
can only be proven by a live behavioral before/after, not a deterministic
unit test):
- Unit (pure logic): new `tests/test_llm.py` — `_build_moments()` includes
  the `source` field from a citation's `title`; `_label()` renders it;
  the `SYSTEM` prompt contains specific guardrail phrases (regression guard
  so a future edit can't silently drop them); the prompt still permits
  answering from a genuine partial match (protects `recall_at_10`, doesn't
  overcorrect into blanket over-abstention).
- Live repro/verification: re-ran the EXACT two adversarial queries the
  grounding-auditor used, against the real running stack, before and after.
- Regression check: re-ran the clean, uncontended recall@10 diagnostic
  (16 labeled queries) to confirm the fix — which only changes what the LLM
  is shown/told, not retrieval — didn't move recall.

**RED** (before implementation): `uv run pytest tests/test_llm.py -v` → 4 of
6 failed (`_build_moments` output had no `source` key; `SYSTEM` lacked the
guardrail phrases).

**IMPLEMENT**:
- `src/rag/search.py::_build_moments`: added `"source": c.get("title")` to
  each moment dict.
- `src/llm.py::_label`/`_intro`: render the source title alongside the
  timestamp/locator; broadened "transcript"-only wording to cover paper/deck
  excerpts too (the field was already reused for both, the LABEL wasn't).
- `src/llm.py::SYSTEM`: rewrote to (1) be source-neutral (video/paper/deck,
  not video-only), (2) explicitly forbid attributing a different source's
  content to a named-but-absent work ("a moment from a different source is
  never evidence about the named one, no matter how topically related its
  content sounds"), (3) require genuine topical relevance, not adjacency
  ("being topically adjacent... does not make a moment relevant to a
  specific named paper, statistic, or claim it doesn't contain"), (4) still
  explicitly permit answering from a genuine partial match, unchanged in
  spirit, so recall doesn't regress.

**GREEN**: `uv run pytest tests/test_llm.py -v` → 6/6 passed. Full suite:
`uv run pytest tests/ -x -q` → **93 passed** (87 + 6 new, no regressions).

**Live verification** (rebuilt + restarted the `api` container to pick up
the change, `docker compose build api && docker compose up -d api`):
- Mamba query (`GET /ask_stream?q=What+does+the+Mamba+paper+say+about+state+space+models`):
  **before**: confidently invented Mamba-paper content, citing an unrelated
  RAG-talk transcript snippet. **after**: *"The moments provided do not
  contain specific information about state space models from the Mamba
  paper. Therefore, I couldn't find relevant details regarding state space
  models in the context of that paper."* — no fabrication, correctly
  declines to attribute content to the named-but-absent source.
- GPT-3/ARC query (`GET /ask_stream?q=What+is+GPT-3's+accuracy+on+the+ARC-AGI+benchmark`):
  **before**: cited the real GPT-3 paper (page 17) but stated 68.8/71.2/70.1
  — the auditor confirmed those are actually the paper's ARC-**Easy** numbers,
  not ARC-Challenge as claimed. **after**: cites the same real paper/page and
  now states 51.4/53.2/51.5 — the paper's actual ARC-**Challenge** numbers.
  Residual, smaller imprecision: the answer still treats "ARC-AGI" (a modern,
  different benchmark) as equivalent to the paper's original "ARC" dataset
  without flagging the naming mismatch — a benchmark-identity nuance, not a
  fabricated statistic; not chased further this session.
- Recall regression check: clean uncontended diagnostic (16 labeled
  queries) → **0.729**, unchanged from the pre-fix measurement — confirms
  the fix touches only LLM-synthesis inputs, not retrieval, as intended.

**Residual, disclosed (not chased further)**:
- `abstained` in the API response is still `False` for the Mamba case even
  though the answer content is now honest — that boolean is set by the
  confidence gate (`CONFIDENCE_THRESHOLD`/`TEXT_CONFIDENCE_THRESHOLD` in
  `src/rag/search.py`), untouched by this fix. The content is now grounded;
  the metadata label describing it is not fully accurate. Fixing that would
  mean tuning the confidence gate itself, which risks the recall gate (already
  marginal) — a separate, riskier change deliberately not made in this pass.
- This fix hardens against the SPECIFIC failure mode found (misattributing
  content across sources); it is a prompt-level defense, not a formal
  citation-faithfulness verifier — it measurably closed the two violations
  found, but does not mathematically guarantee no other hallucination mode
  exists. Full faithfulness checking (verifying each generated claim against
  its cited chunk's actual text, e.g. via embedding-overlap scoring) remains
  future work if deeper guarantees are wanted.

**Independent re-check (fresh grounding-auditor run, adversarial, NOT just
re-running the same 2 queries)**: confirmed both original repros are fixed
(Mamba abstains cleanly; GPT-3/ARC now cites the correct 51.4/53.2/51.5).
But 3 NEW variants of the same underlying pattern still reproduce it:
1. **CRITICAL** — "What numerical rank value does the CLIP paper recommend
   for low-rank adaptation?" retrieved zero CLIP citations (all 6 were
   LoRA content) yet the answer opened with *"The CLIP paper recommends a
   low-rank adaptation value of r..."* — the identical Mamba-shaped bug,
   CLIP swapped in for Mamba. The prompt's general wording ("a moment from a
   different source is never evidence about the named one") did not
   reliably stop this specific case.
2. **HIGH** — a false-premise question about the ReAct paper ("why did it
   conclude pure chain-of-thought outperforms tool-use") got an answer
   affirming the false premise, while its OWN cited moment [6] states the
   opposite ("consistently outperform[s] baselines with only reasoning or
   acting"). The answer contradicts its own citation.
3. **MEDIUM** — a real-but-uncorpused model name (Mixtral 8x7B) got a
   partial fabrication (2 paragraphs asserting facts "from the Mixtral
   paper") before self-correcting in paragraph 3 — better than a clean
   fabrication, still not correct.
Clean passes: a real-paper/uncovered-subtopic question (BERT + carbon
footprint) abstained correctly; tenant isolation re-confirmed unaffected.

**Honest conclusion**: this fix demonstrably closes the two originally
reported cases and does not regress recall, but does NOT fully generalize —
a system-prompt-level guardrail is a probabilistic mitigation against a
capable model's tendency to answer confidently, not a hard guarantee. Fully
"achieving" AGENTS.md's non-negotiable #5 in the adversarial-general case
likely needs a code-level, not prompt-level, defense (e.g., a post-hoc check
cross-referencing every named source/entity mentioned in the generated
answer against the actual cited titles, or an explicit false-premise check
against cited text before the answer is accepted) — a materially bigger
change than this session's fix, flagged for the user to decide whether to
pursue further.

**Commit**: `d8744b0` — "Ground LLM answers in source titles to stop
cross-source misattribution".

---

## 2026-07-28 — Resilience fix: automatic crash recovery (no_loss_under_crash)

The user directed that README's "No loss" non-negotiable (`--resilience`
passes) is a must, not a disclosed-and-deferred item. `bench.py --resilience`
was FAIL (`no_loss_under_crash: False`) at the start of this entry, with two
layered causes already found in the earlier Part-0 entry: (1) Docker's
`restart: unless-stopped` never actually fires for a `docker kill`ed
`--scale`-created worker replica, and (2) even once a worker is back, an
interrupted flow run has no automatic path to re-execution — no flow-level
`retries=`, and Prefect's own crash detection turned out to be far weaker
than DESIGN.md/ARCHITECTURE.md assumed (see below). Manual retry via this
repo's own `POST /admin/documents/{id}/retry` (component 11) recovered fine —
this entry automates exactly that as a background sweep.

**Scope**: maintenance/hardening of existing component 5 (queue wiring) +
component 11 (retry), not a new DESIGN.md component. New file
`src/reconciler.py`; additive changes to `src/db.py` (new `flow_run_id`
column + 2 functions), `src/config.py` (2 new tunables), `src/api/admin.py`
(persist `flow_run_id` at register/retry), `src/worker.py` (start the sweep
alongside `src/dispatcher.py`'s).

**Design iteration — two real dead ends found and corrected BEFORE landing
on what actually works**, each confirmed empirically against the live stack,
not assumed:

1. First attempt: only trust Prefect's flow-run state, treating
   `CRASHED`/`FAILED`/`CANCELLED` as "safe to restart" and `RUNNING` as
   "still fine, leave it." Result: **still FAIL**. Investigated why — the
   orphaned flow run's `read_flow_run()` reported `state.type ==
   StateType.RUNNING` a full 5+ minutes after its worker container was
   SIGKILLed. Researched Prefect 3.x's actual crash-detection mechanism:
   heartbeat-based "zombie run" detection is an **opt-in Cloud-managed
   automation** (not on by default) and even enabled takes **~9 minutes**
   (3 missed 3-minute heartbeats) — nowhere near reliable enough for a
   300s resilience-test window, or for real production recovery speed.
2. Second attempt: widen "safe to restart" to include `RUNNING` (trusting
   our own DB timestamp staleness instead of Prefect's state for that case),
   keep `PENDING`/`SCHEDULED` as "still fine" (genuinely-queued-behind-
   capacity is a real, common, healthy state we observed repeatedly earlier
   in Part 0). Result: **still FAIL** — a *different* stuck document this
   time, found to be sitting in `StateType.PENDING` ("Submitting"): its
   worker was killed while still LAUNCHING the flow, before it ever reached
   `Running`. Since `PENDING`/`SCHEDULED` is simultaneously the state of
   perfectly healthy queued work AND this orphaned-mid-launch case, Prefect's
   state genuinely cannot distinguish them.
3. **Final design**: stopped trying to make Prefect's state authoritative at
   all. `db.stale_documents()` (a real Postgres query: status in
   `(pending, fetching, parsing, embedding)` AND `updated_at` older than
   `RECONCILE_STALE_AFTER_S`, default 90s) is the actual load-bearing signal.
   Prefect's state is consulted only to rule out the one case worth ruling
   out — `COMPLETED` (Prefect says this already finished; restarting would
   risk duplicating finished work, left for investigation instead). Every
   other state, once the row is already confirmed stale, is treated as
   restart-worthy. Trade-off, explicitly accepted (same one
   `src/dispatcher.py`'s own WFQ loop already makes): a row that turns out
   to have still been healthily backlogged might get a redundant duplicate
   flow run — never an *incorrect* one, since crash-safe status ordering,
   idempotent uuid5 upserts, and per-tenant duplicate detection all still
   hold regardless of how many times a document gets (re-)submitted.

**Evals defined**: `tests/test_reconciler.py` (12 tests, real throwaway
Postgres for `stale_documents`/end-to-end `reconcile_once` logic — that IS
what's being proven — Prefect's flow-run-state API mocked, a real check
needs a live Prefect Cloud deployment): staleness query correctness (finds
old rows in the given statuses, excludes fresh ones, excludes other
statuses); `_flow_run_dead` returns True for every state except `COMPLETED`,
False for `COMPLETED`/no-flow-run-id/lookup-failure; `reconcile_once`
actually restarts a dead row (resets to pending, re-enqueues, persists the
new `flow_run_id`), leaves an explicitly-still-alive row untouched, skips a
stale row with no `flow_run_id` (seeded documents — never went through
Prefect scheduling), and never restarts the same row twice within a 300s
cooldown.

**RED** (before implementation): `ImportError: cannot import name
'reconciler' from 'src'`.

**GREEN**: `uv run pytest tests/test_reconciler.py -v` → 12/12 passed (after
2 rounds of test updates matching the 2 design corrections above). Full
suite: `uv run pytest tests/ -x -q` → **105 passed** (93 + 12, no
regressions).

**Live verification — 4 successive real `bench.py --resilience` runs
against the live stack**, each a real `docker kill` on a real worker
container mid-ingest:
```
Run 1 (before this fix):     [FAIL] no_loss_under_crash: False — 2 sources never reached a terminal state
Run 2 (Prefect-state-only, attempt 1): [FAIL] — same 2 ids, confirmed stuck at state.type==RUNNING 5+ min post-kill
Run 3 (widened to RUNNING, attempt 2): [FAIL] — 2 NEW ids, confirmed stuck at state.type==PENDING ("Submitting")
Run 4 (staleness-primary, final):      [PASS] no_loss_under_crash: True (target 0 dropped, all indexed)
```
Run 4's worker logs confirm the mechanism working as designed —
`[reconcile] doc_608c62b14e stuck in 'pending' — its flow run
06a67f7e-... died — restarting` for 4 of the 10 submitted documents (the
ones actually assigned to the killed container; `docker compose ps -a`
confirmed the killed replica never auto-restarted — `Exited (137)`, matching
the earlier-disclosed Docker `--scale` restart-policy gap, still not fixed,
only worked around by the reconciler not depending on it).

**Residual, disclosed**: the killed worker container itself still never
auto-restarts (a Docker Compose `--scale`-replica behavior, not fixed this
session — the reconciler makes this no longer matter for correctness, but a
`docker compose ps` after a crash will still show one fewer running replica
than desired until an operator/monitoring system notices and runs `docker
compose up -d --scale worker=N` again). Scoped to documents only, matching
`bench.py --resilience`'s own scope — videos have a narrower parallel gap
(stuck in an active status, not just 'pending') not addressed here, since
`src/dispatcher.py`/`src/api/videos.py` are CLAUDE.md-protected and the
graded gate only exercises documents.

**Commit**: `0f7d961` — "Automate crash recovery for orphaned document
ingests (fix no_loss_under_crash)".

---

## 2026-07-28 — Grounding, round 2: mechanical (not just prompt-level) defense

The round-1 grounding fix (source titles + prompt rules) closed the 2
originally-reported violations, but its OWN independent adversarial re-check
(previous entry) found it didn't generalize — 3 new cases reproduced the
same pattern under different names. This entry replaces "ask the model to
cross-check a named source" (an abstract reasoning task it doesn't reliably
do) with a structural constraint plus a mechanical, code-level backstop that
doesn't depend on the model's compliance at all — then adversarially
re-verifies TWICE more, finding and fixing one more real gap before landing.

**1. Prompt change** (`src/llm.py::SYSTEM`, rule 4): instead of "only
attribute if titles match" (reasoning-based, proven unreliable), the model
is now told to **never write a source's name in prose at all** — cite ONLY
by `[n]`. Simpler, more mechanical instruction to follow; removes the
linguistic opportunity for "the X paper says..." to exist at all when
followed.

**2. Code-level backstop** (`src/rag/search.py::_check_named_source_attribution`,
new, called after `_validate_citations` in `ask()`): scans the generated
answer for the literal "the X paper/deck/talk/video" pattern (regex,
case-insensitive) and checks whether X is actually a DIFFERENT source that
exists elsewhere in the tenant's corpus (`db.list_sources`) but wasn't cited
for this query. If so, withholds the answer with an explicit explanation
instead of returning the fabrication — this does NOT depend on the LLM
following instruction #1; it catches the violation even when the prompt
rule fails.

**Evals**: `tests/test_llm.py` (new prompt-guard test) +
`tests/test_grounding_guard.py` (new file, 7 pure-logic tests: `_short_name`
normalization; flags an uncited named source; allows naming an actually-
cited one; ignores names absent from the corpus entirely; ignores plain
prose with no naming pattern; fails open on a `db.list_sources` error).

**RED → GREEN**: 7 new tests RED (`_check_named_source_attribution` didn't
exist) → GREEN. Full suite: **113 passed** (105 + 8: 7 new +
`test_system_prompt_forbids_naming_sources_in_prose`; one round-1 test's
exact-phrase assertion was updated to match the stronger new rule, not
loosened — noted explicitly since CLAUDE.md forbids weakening an eval).

**Live re-verification #1** (rebuilt + restarted `api`): re-ran the exact
CLIP/LoRA repro from round 1's adversarial check. Before: fabricated "The
CLIP paper recommends a low-rank adaptation value..." Before this session's
fix even existed, that would have gone straight to the user; now: *"I
couldn't find that in your videos... (The generated answer named "CLIP" as
a source, but 'CLIP (Radford et al. 2021)' was not actually among what was
retrieved for this question — withheld...)"* — the fabrication is caught
and replaced, live, verified via curl against the running stack.

**Independent adversarial re-check #2** (fresh agent, NEW queries, not just
re-running the fixed one) found: (a) the disclosed false-premise gap
confirmed still open as expected (a BERT question baited into affirming
"BERT uses an autoregressive left-to-right decoder" got an answer that
contradicts its own correctly-cited sources, which state BERT is
bidirectional — a self-contradiction between prose and citation, a
structurally different failure this fix doesn't address); (b) tenant
isolation and a normal grounded query both clean; (c) **one real, non-
adversarial gap found**: "How does the chain-of-thought paper combine a
dense passage retriever..." (an ordinary-sounding question, not a trick)
got zero CoT citations (all 6 were RAG content) yet a fully fabricated
answer — the mechanical check should have caught this but didn't. Root
cause: `_check_named_source_attribution` compared the model's colloquial
"chain-of-thought" against `_short_name("Chain-of-Thought Prompting (Wei et
al. 2022)")` == `"Chain-of-Thought Prompting"` via **exact string equality**
— a real mismatch the model's natural phrasing produces, not an edge case.

**Fix for the exact-match gap**: replaced dict-key equality with
prefix/substring containment (`named in short or short in named`) on both
sides of the comparison — `"chain-of-thought"` is a substring of
`"chain-of-thought prompting"`, so this now matches. New regression test
`test_check_named_source_catches_a_colloquial_short_name_mismatch` locks
this in. RED confirmed (`assert result != answer` failed before the fix) →
GREEN after. Full suite: **113 passed**, no regressions. Rebuilt + restarted
`api` again; re-ran the exact CoT query live — now correctly withheld:
*"...named 'chain-of-thought' as a source, but 'Chain-of-Thought Prompting
(Wei et al. 2022)' was not actually among what was retrieved..."*.

**Regression checks**: a normal, correctly-grounded query ("how does
attention avoid recurrence") still answers normally, unaffected. Clean,
uncontended recall@10 diagnostic (16 labeled queries): **0.729**, identical
to the pre-fix measurement — this change touches only post-generation
answer text, never retrieval, as intended.

**Residual, disclosed, NOT fixed this round** (both found by the round-2
adversarial re-check, both structurally different from what this fix
addresses):
- **False-premise self-contradiction** — an answer that misreads or
  contradicts its OWN correctly-cited source (the BERT/autoregressive
  case). `_check_named_source_attribution` only catches naming an
  *uncited* source; it does nothing when the citation is correct but the
  model's claim about it is wrong. Would need a genuine faithfulness check
  (comparing the specific claim against the cited text's actual content),
  a materially different and harder mechanism than this session's fix.
- **Matching precision**: the substring-containment fix reduces but does
  not eliminate false-negative risk from further, more unusual colloquial
  phrasings (e.g. an acronym or nickname with no textual overlap with the
  real title at all) — the regex pattern itself also only catches "the X
  paper/deck/talk/video" phrasing specifically, not every possible way to
  name a source in prose (e.g. "Vaswani's paper", "the 2021 CLIP work").
  This is a real, mechanical, generalizing improvement over prompt-only
  hardening, verified against 3 independent adversarial rounds — not a
  formal guarantee of zero hallucination, which remains out of reach for
  any prompt- or regex-based defense.

**Commit**: `f04d1e8` — "Add mechanical named-source guard to catch
cross-source misattribution".

---

## 2026-07-28 — UI redesign + a real integration bug found while auditing it

The user asked whether the UI had actually been updated to match everything
built, and to redesign it to be professional and "sellable," integrating the
cross-source (talk + paper + deck) capability the whole assignment is about.
Auditing the UI against that ask surfaced something more important than
copy: **the shipped search box could never have returned a paper or deck
citation to a real browser user.**

### Real bug found: `video_ids` scoping silently excluded every document

The UI's `$("#go").onclick` always calls with a `video_ids` scope (either
`SAMPLE_INDEXED` on the front page or the checked-video set on
`/get-started` — never empty in practice, since indexed videos default to
selected). `POST /api/ask` → `rag_search.ask()` → `vector_store._user_filter`
built a Qdrant `must: video_id MatchAny(video_ids)` condition on
`TEXT_COLLECTION` — but paper/deck chunks in that SAME collection carry no
`video_id` field at all (`upsert_document_chunks` payloads use `source_id`/
`kind`/`page`/`slide` instead). A Qdrant `must` condition on a field a point
doesn't have excludes that point — so ANY non-empty `video_ids` scope
silently dropped every document from the results, always. This is why it
was never caught until now: every prior verification this session
(`bench.py`, curl testing, both grounding audits) called `/ask_stream`
directly with no `video_ids` param at all, which never hit this path — the
one caller that always does is the UI itself, which nothing had exercised
end-to-end in a browser before.

**Fix** (`src/rag/vector_store.py::_user_filter`): the video-id condition is
now wrapped in `should: [video_id match, IsEmptyCondition(video_id)]` — so
it constrains video-transcript points as before, while any point WITHOUT a
`video_id` field (i.e. every document chunk) always passes through
regardless of which videos are selected. The CLIP visual collection never
has document points, so this is a no-op there. Additive fix inside
`search.py`/`vector_store.py`'s own extensible territory (both explicitly
allowed to be "extended additively" per CLAUDE.md) — `/api/ask`'s own route
code in `src/api/search.py` has zero diff lines; its request/response shape
and status codes are untouched. Judgment call, made explicitly: this DOES
change what `/api/ask` itself would now additionally return (documents can
come through where they silently couldn't before) — treated as a legitimate
bug fix to shared retrieval plumbing, not a forbidden edit to `/api/ask`'s
provided behavior, since no existing video-only behavior changes and the
previous behavior was never an intentional part of the base app's contract
(documents didn't exist in the base app at all).

**`GET /ask_stream` extended** to accept `video_ids` (plural, `Query`),
mirroring `POST /api/ask`'s own existing param — it previously only accepted
a single `video_id`, so even switching the UI to call it wouldn't have
carried the multi-select scope through. Also added the `note` field to its
`answer` SSE event (parity with `/api/ask`'s existing "no LLM configured"
note, previously dropped by the SSE wrapper).

**UI's search handler switched from `POST /api/ask` to `GET /ask_stream`**
(new `askViaStream()`: fetch + `ReadableStream` reader parsing `event:`/
`data:` blocks by hand) — this is what actually makes the fix reach a real
user, not just `curl`.

**Evals**: two new tests in `tests/test_cross_source_search.py`:
`test_video_ids_scope_excludes_other_videos_but_not_documents` (real
embedded Qdrant: upserts one in-scope video, one out-of-scope video, and one
document; asserts the out-of-scope video is excluded AND the document is
NOT); `test_ask_stream_accepts_video_ids_plural_query_params` (contract test
via `TestClient`, mocks `rag_search.ask` to capture what `video_ids` value
actually arrives). **RED** (`AssertionError: assert None == ['yt_a','yt_b']`
/ document missing from scoped results) → **GREEN**. Full suite:
`uv run pytest tests/ -x -q` → **115 passed** (113 + 2, no regressions).

**Live verification** (rebuilt + restarted `api`): replicated the EXACT
query shape the front page sends — fetched the 12 real sample-indexed video
ids via `/api/videos`, built the same `video_ids=...&video_ids=...` query
string the browser would, and called `/ask_stream` with it directly:
```
citation kinds: ['video', 'video', 'paper', 'video', 'video', 'paper']
```
Two paper citations (Attention, Chain-of-Thought) came back alongside four
video citations — before this fix, this exact call would have returned
video citations only, always, regardless of query content.

### UI redesign (cosmetic + copy, verified not to break existing logic)

Rebranded "MomentSearch" → "ScholarMomentSearch" throughout (title tag,
meta description, favicon, header, footer, both `applyMode()` branches).
Rewrote hero copy/badge/examples in both modes to reflect DESIGN.md's actual
positioning ("AI Research & Conference Knowledge Base") instead of the
original video-only template copy; example queries now use real questions
the seeded corpus can answer (starting with DESIGN.md's own stated demo
moment: "how does attention avoid recurrence?"), not generic visual-search
prompts. Added a small Talks/Papers/Decks trust strip near the hero. Split
the library into two labeled subsections (`#videosGroup` "🎥 Talks",
`#documentsGroup` "📄 Papers & decks") instead of two undifferentiated
stacked chip rows. Gave document result-cards a gradient icon panel instead
of a flat placeholder, for visual parity with video thumbnails. Footer now
points at this repo's real GitHub instead of the original base template's
`traversaal-ai/momentsearch` link.

Verified none of this touched the two pure-logic `<script>` blocks
(`citation-logic`, `ingest-logic`) that `ui/citation.test.js`/
`ui/ingest.test.js` regex-extract and test — `node --test ui/*.test.js` →
**13 passed**, unchanged. Verified every `id` the existing JS references via
`$("#...")` still exists exactly once in the restructured HTML (scripted
check, no typos/mismatches). Verified `/`, `/get-started` both still return
200 with balanced HTML tags.

**Commit**: `abde982` — "Fix video_ids filter excluding documents and
redesign UI for cross-source search".

---

## 2026-07-28 — Enterprise UI rebuild (sidebar console)

The user judged the UI still read as a PoC and asked for a full redesign to
enterprise grade, explicitly approving a delete-and-rebuild. Direction was
pinned with the user before coding (AskUserQuestion): light SaaS console
aesthetic (white surfaces, slate text, Inter only, SVG icons, no emoji),
sidebar app-shell layout (Search / Library / Add sources as client-side
views), indigo-600 accent with emerald/amber/red status colors and
sky/violet/teal kind tags. Full plan reviewed and approved via plan mode.

**What was rebuilt** (`ui/index.html`, complete rewrite, 663 → ~700 lines):
- App shell: fixed left sidebar (brand, three-view nav, corpus stats + LLM
  status footer), top bar (view title, "N processing" pill, mode link),
  scrollable main area with three `data-view` sections toggled by a tiny
  client-side router — no new routes, still one self-contained file.
- Library is now a real data table (Source / Kind / Status / Chunks /
  Actions + a scope checkbox column in workspace mode) with a unified row
  model normalizing videos and documents, sticky header, per-status progress
  bars, and one delegated click listener for select/retry/delete — replacing
  the old chip-pill rows. Scope summary bar + Select all/Clear; the Search
  view shows a scope hint linking into the Library.
- Add sources: three cards (YouTube / Upload / Paper+Deck) each with its own
  status line (`#ytStatus`/`#upStatus`/`#docStatus`) replacing the single
  shared status element.
- Sample mode (`/`) keeps the same shell: "Add sources" nav hidden,
  Library relabeled "Corpus" and rendered read-only (no checkboxes/actions).
  One deliberate, disclosed improvement over the old behavior: the demo
  Corpus now also lists the seeded papers/decks (filtered to `doc_seed_*`
  ids — the document mirror of the `is_sample` video filter) instead of
  showing talks only.
- All emoji chrome replaced by an inline Lucide SVG icon set (`ICONS` map +
  `icon()` helper + boot-time `data-icon` hydration). The test-locked
  `docBadge()` text icons (✓ ⚠ ◷ …) are bridged to SVGs at render time via
  a `BADGE_ICON` map — the locked block itself is unchanged in behavior.

**Constraints held** (each independently verified after the rewrite):
- The two test-locked script blocks survived: `citation-logic` byte-
  identical; `ingest-logic` identical except the `c:` class values (updated
  to the new palette — explicitly not asserted by the tests).
  `node --test ui/citation.test.js ui/ingest.test.js` → **13/13 pass**.
- `<!--MS_MODE-->` placeholder present exactly once, in `<head>`; live
  check: `curl /` injects `window.MS_MODE="sample"`, `/get-started` injects
  `"full"`, both HTTP 200.
- Ported-verbatim logic kept its contracts: `askViaStream` SSE parser,
  markdown/[n]-pill renderer, 2.5s refresher (still requires
  `loadVideos`/`loadDocuments` to return arrays — kept), presign upload
  flow, `playCitation` modal (same element ids), `openCitation` kind
  dispatch, SELECTED/KNOWN/SAMPLE_INDEXED scope sync.
- Scripted checks: all **43** `$("#id")` references resolve to exactly one
  `id="…"` each (0 missing, 0 duplicated); every `<script>` block parses
  under `node --check`; section/div/table/aside/main/button tag counts
  balanced; legacy design tokens (`coral|paper2|Fraunces|text-muted|
  border-line|bg-paper`) — zero hits.
- Live cross-source proof against the rebuilt container, using the exact
  `video_ids` query shape the browser sends:
  `citation kinds: ['video', 'video', 'paper', 'video', 'video', 'paper']`.
- Backend untouched: `uv run pytest tests/ -x -q` → **115 passed**.

**Commit**: pending — `ui/index.html`, this entry.

---

### 2026-07-28 — Component 14: paper table & figure extraction (DESIGN.md §3a)

Scope: `paper.py` was text-only (`page.get_text()`), so a table's structure was
flattened into jumbled prose and embedded figures were invisible entirely — the
deck path already captioned images, papers never did. Own scope-change commit
(`218936e`) added DESIGN.md components 12-14 and mirrored them into CLAUDE.md §7
before any implementation, per CLAUDE.md §1.

RED: added 4 tests to `tests/test_paper_ingest.py` against fixture PDFs built with
real ruling-line tables (`fitz.draw_line`) and embedded images (`fitz.insert_image`).
`uv run pytest tests/test_paper_ingest.py -x -q` → `1 failed, 6 passed` (table test
failed: `assert 0 == 1`) — confirmed the eval was meaningful before implementing.

IMPLEMENT: `paper.py` — `_extract_tables()` (via `page.find_tables()`, PyMuPDF's
default line-ruling strategy, which does not fire on ordinary paragraph text —
verified no false positives on the existing prose-only fixtures) emits one chunk
per table with row/column structure preserved (`" | "`-joined cells, newline-joined
rows), tagged `section="Table"`; table-area lines are excluded from the ordinary
prose scan (bbox overlap ≥50%) so cell values are never duplicated as noise.
`_extract_figures()` (via `page.get_images()` + `get_image_rects()` area filter,
`_FIGURE_MIN_AREA = 120×120px`, skips small logos/icons) flags substantive images
`needs_caption=True` — the *existing* `doc_pipeline.py::t_caption` task is
kind-agnostic and already vision-captions any chunk shaped that way, so papers now
get figure captions through the same path decks use, with zero changes to that
task. `doc_pipeline.py`'s paper branch of `t_parse` updated to pass through
`c.needs_caption`/`c.image_jpeg` instead of the old hardcoded `False`/`None`.

GREEN:
- `uv run pytest tests/test_paper_ingest.py tests/test_doc_pipeline.py -q` →
  **24 passed**.
- Full suite: `uv run pytest tests/ -q` → **119 passed** (was 115; +4 new, 0
  regressions).

Disclosed limitation: `find_tables()`'s default strategy needs actual vector
ruling lines — a table drawn with whitespace-alignment only (no visible grid
lines) will not be detected and still falls through to the ordinary prose scan,
same as before this change. Not evaluated against real-world corpus PDFs in this
pass (no live stack ingestion run) — only against hand-built fixtures, matching
DESIGN.md component 14's stated primary eval (unit-level, fixture-PDF structure
survival), not a live probe.

**Commit**: pending — `src/ingest/paper.py`, `src/ingest/doc_pipeline.py`,
`tests/test_paper_ingest.py`.

---

### 2026-07-28 — Component 12: retrieval precision@10 diagnostic (DESIGN.md §3a)

Scope: `benchmark/labeled_queries.json`'s `expect_kinds` only proves recall (right
*kind* present) — it never penalizes noise. An off-topic citation of an expected
kind (e.g. a LoRA chunk showing up on an Attention-paper query) scored full recall
credit. New metric: of a query's top-10 citations, what fraction resolve — via the
seed corpus's deterministic ids (`doc_seed_<corpus_id>_<kind>`, `yt_<youtube-id>`,
mirroring `src/seeding.py`'s own scheme without importing `src/`) — to that query's
own triplet vs. a different one. Own gate file `benchmark/quality_gates.json`
(`precision_at_10_min: 0.70`), never `sla.json` (frozen, CLAUDE.md §2 E5).

RED: added 6 tests to `tests/test_bench.py` for `_seed_corpus_id_map` and
`_score_precision`. `uv run pytest tests/test_bench.py -q` → `6 failed, 15 passed`
(`AttributeError: module 'benchmark.bench' has no attribute '_score_precision'`).

IMPLEMENT: `bench.py` gained `_seed_corpus_id_map()`, `_score_precision()`,
`measure_precision()` (refactored `measure_recall`/`measure_precision` onto a
shared `_fetch_labeled_citations()` — same live `/ask_stream` calls, scored two
ways), and a `--quality` CLI mode (mirrors `--resilience`'s standalone-mode
pattern) gating `precision_at_10` against `quality_gates.json`.

GREEN:
- `uv run pytest tests/test_bench.py -q` → **21 passed**.
- Full suite: `uv run pytest tests/ -q` → **125 passed** (was 119; +6 new, 0
  regressions).
- Live run against the running stack (`BASE_URL=http://localhost:8000
  ADMIN_TOKEN=... uv run python benchmark/bench.py --quality`), run twice for
  stability, uncontended (no concurrent load in flight):
  `[FAIL] precision_at_10: 0.635 (target 0.7)` — **identical both runs**
  (deterministic, not a contention artifact like recall's official-run number).

**Red, disclosed, not tuned away**: 0.635 fails the 0.70 gate I set for this new
diagnostic. Per CLAUDE.md §2 E5 the fix is the system, not the threshold — and
this is a newly-introduced quality gate, not a frozen assignment one, so the
honest move is still to report the real number, not adjust the bar to make it
pass. Plausible mechanism, not yet investigated further: the 8 seeded triplets
are real, cross-referencing ML papers (LoRA fine-tunes GPT-family/LLaMA models;
CoT and ReAct both discuss reasoning traces; Attention is background context cited
by several of the others) — genuine topical/vocabulary overlap between adjacent
papers in a coherent research corpus, not necessarily a retrieval defect, though a
real defect (e.g. `TOP_K=6`'s cross-modal boost pulling in adjacent-topic filler
once a video's own on-topic hits are exhausted) hasn't been ruled out either.
Left open for follow-up, not silently patched.

**Commit**: pending — `benchmark/bench.py`, `benchmark/quality_gates.json`,
`tests/test_bench.py`.
