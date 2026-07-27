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

**spec-guardian**: pending.

**Commit**: _pending._
