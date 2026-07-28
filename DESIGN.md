# ScholarMomentSearch — Design

**Use case:** AI Research & Conference Knowledge Base — one searchable brain over an
ML research corpus: conference talk videos, the matching paper PDFs, and the slide
decks for the same papers. Target user: ML researchers / applied-AI engineers; framed
as an enterprise "internal research agent."

**Demo moment:** ask *"How does the attention mechanism avoid recurrence?"* → one
answer citing a talk timestamp, a paper page, and a slide.

---

## 1. What the base repo actually is (read-first findings)

The assignment README describes the concept; the shipped code differs in specifics.
Design against the **code**, satisfy the **bench contract**.

| Assignment README says | Repo reality |
|---|---|
| `POST /admin/videos`, `src/api/admin.py` | `POST /api/videos` in `src/api/videos.py` (Bearer auth + `X-User-Id` multi-tenancy) |
| `/ask_stream` SSE | `POST /api/ask` in `src/api/search.py` (no `/ask_stream` yet) |
| captions → diarize → semantic chunk → LLM enrich → embed | `fetch → sample (CLIP frames) → embed-index → transcript` — **two branches**: visual (CLIP frames) + text (bge transcript chunks) |
| one hybrid Qdrant index | **two collections**: `QDRANT_COLLECTION` (CLIP, image space) + `TEXT_COLLECTION` (bge, text space), both multi-tenant (`user_id` tenant index) |
| status: pending→parsing→chunking→enriching→embedding→indexed | status: `pending → fetching → sampling → embedding → indexed \| skipped \| failed` (Postgres is source of truth; Prefect is the operational view) |
| plain Prefect FIFO | optional **WFQ fair dispatcher** (`src/dispatcher.py`): rows wait `pending` in Postgres, a loop admits them round-robin per user, capped in-flight |

Benchmark scaffold (`benchmark/bench.py`) hardcodes: `POST /admin/documents` (202),
`GET /admin/sources`, `GET /ask_stream?q=`. → **We add these routes**; existing
`/api/*` stays untouched (contract-preserved).

## 2. Where papers & decks live in the vector space

Papers and decks are *text* sources. They join the **text collection** (bge,
`TEXT_EMBED_DIM`) alongside video transcript chunks — that is the shared semantic
space where cross-source retrieval happens. The CLIP collection remains video-visual
only. Writeup framing: "one shared index" = one shared *text* space for all three
kinds + a visual branch for video; retrieval fuses both, exactly as the base app
already fuses frames + transcript.

Chunk payloads (extends the existing transcript payload shape):

```jsonc
// video transcript (exists):  { user_id, video_id, modality:"text", t_start, t_end, ms, text }
// paper (new):  { user_id, source_id, kind:"paper", page: 4,  section: "3.1", text, embed_version }
// deck  (new):  { user_id, source_id, kind:"deck",  slide: 12, text, embed_version }
```

Deterministic point IDs — `uuid5("{source_id}:{kind}:{i}")` — so re-runs overwrite
(idempotent, same trick as video).

## 3. Components to build

| # | Component | File | Notes |
|---|-----------|------|-------|
| 1 | `documents` table + unified sources query | `src/db.py` | mirrors `videos` (id `doc_…`, user_id, kind `paper\|deck`, uri, storage_key, title, status, progress, attempts, error). Don't touch the videos table. |
| 2 | Paper parser | `src/ingest/paper.py` | pymupdf → per-page text with structure → page-aware chunks (~500–800 tokens, never crossing page boundaries without carrying `page`) |
| 3 | Deck parser | `src/ingest/deck.py` | PDF decks: pymupdf per page = slide; PPTX: python-pptx. Image-heavy slides (little text) → caption via the existing env-switched vision LLM (`src/llm.py`) before embedding |
| 4 | Document ingest flow | `src/ingest/doc_pipeline.py` | Prefect flow `ms-ingest-document` with kind branch. Lifecycle: `pending → fetching → parsing → embedding → indexed \| failed`. Per-task retries like video. **Crash-safe ordering:** status → `indexed` only *after* the Qdrant upsert returns. |
| 5 | Queue wiring | `src/jobs.py`, `src/worker.py`, `src/dispatcher.py` | `enqueue_document()`; worker serves both deployments; dispatcher claims across videos+documents (or documents ride FIFO first, WFQ unified after) |
| 6 | Admin router | `src/api/admin.py` (new) | `POST /admin/documents` → validate, insert `pending`, enqueue, **202 immediately**; `GET /admin/sources` → union of videos+documents with `kind`, `status`, `pct`; errors 400/401/502 |
| 7 | Cross-source search | `src/rag/search.py` + `GET /ask_stream` | SSE endpoint wrapping the existing ask path; retrieval over text collection now returns mixed kinds; citation carries `kind` + locator (`start_ms` \| `page` \| `slide`); grounded — empty retrieval ⇒ empty citations |
| 8 | UI citation render | `ui/` | video → seek player to `start_ms`; paper → link `uri#page=N`; deck → show slide number/thumbnail |
| 9 | Benchmark | `benchmark/bench.py` | fill the 4 TODOs: labeled queries (recall@10), concurrent-ingest load, throughput probe, worker-kill (`docker kill` the worker container mid-backfill, restart, poll `/admin/sources`) |
| 10 | Seed the triplet corpus | `src/seeding.py`, `src/samples.py` | extend the boot-time seed gate to ingest `benchmark/corpus.json` (8 papers + 8 decks + 8 talks) alongside the sample videos — a fresh deploy is cross-source queryable on first load, idempotent like today |
| 11 | Self-serve ingest tab | `ui/index.html` | the existing ingest box (YouTube URL / Upload tabs) gains a "Paper / Deck" tab → `POST /admin/documents`; the library panel shows document lifecycle + retry, tenant-scoped like videos |

Build order = the table order; each step is independently testable.

### 3a. Quality-eval gaps (added 2026-07-28, DECIDED — own scope, own gates)

A tutoring-session review of "how is this actually evaluated" surfaced three real
gaps beyond the grading rubric (`eval/rubric.json`) and SLA gate (`benchmark/sla.json`)
— **both of those stay frozen and untouched**; these three components ship their own
non-frozen gate file, `benchmark/quality_gates.json`, so tuning them later is never a
"loosen the grading threshold" move.

| # | Component | File | Notes |
|---|-----------|------|-------|
| 12 | Retrieval precision@10 (topical) | `benchmark/bench.py` (`--quality` flag) | `labeled_queries.json`'s `expect_kinds` only proves recall (right kind present); it never penalizes noise. New metric: of a query's top-10 citations, what fraction resolve (via the seeded manifest's `corpus_id`, same resolution `measure_recall` already does) to the query's own triplet vs. a different, off-topic one. Threshold lives in `quality_gates.json` (`precision_at_10_min`), not `sla.json`. |
| 13 | Answer relevancy + faithfulness (LLM-judge) | `benchmark/answer_quality.py` (new) | For each labeled query: call `/ask_stream`, then judge the returned answer with the tenant's own configured LLM (`src/llm.py`, temperature 0) on two axes — **relevancy** (1–5: does the answer address the question) and **faithfulness** (pass/fail per cited claim: is it actually supported by that citation's chunk text, reusing the same text the LLM itself was shown). Reports mean relevancy and faithfulness pass-rate against `quality_gates.json` thresholds. Disclosed limitation: an LLM-judge is inherently noisy — report it, don't oversell it as ground truth. |
| 14 | Paper table & figure extraction | `src/ingest/paper.py` (extend) | Today `paper.py` is text-only (`page.get_text()`), so a table's structure is flattened into jumbled prose and embedded figures are invisible — the deck path already captions images, papers never did. Add: (a) `page.find_tables()` → a table becomes its own chunk with row/column structure preserved (not merged into surrounding prose), tagged `section="Table"`; (b) `page.get_images()` → a substantive embedded image (area threshold, skip tiny logos/icons) gets vision-captioned via the existing `llm.caption_image` (mirrors `deck.py`'s `needs_caption` path) and appended as its own chunk on that page. Unit-tested against fixture PDFs built on the fly in `tmp_path` (one with a real ruling-line table, one with an embedded diagram) — never committed media, per CLAUDE.md hygiene — the prior test suite only proved chunking *mechanics* (page numbering, heading detection), never table/figure *content* survival. |

Primary eval per component, mirrored into `CLAUDE.md` §7:
- **12** — `bench.py --quality`: `precision_at_10` ≥ `quality_gates.json`'s threshold.
- **13** — `answer_quality.py`: mean relevancy ≥ threshold, faithfulness pass-rate ≥ threshold.
- **14** — unit: fixture-PDF table chunk keeps cell/row structure; fixture-PDF figure produces a captioned chunk.

### 3b. Retrieval-quality upgrades (added 2026-07-28, DECIDED — own scope, own gates)

Follow-up to §3a's precision@10 diagnosis (EVIDENCE.md 2026-07-28): the text
branch is pure dense (bge) with no lexical matching, retrieval scores are
rank-only (RRF has no way to tell "barely qualified" from "best possible
match" apart), and every query is embedded verbatim with no rewriting. Three
components, verified against the live Qdrant Cloud instance before being
committed to here (see EVIDENCE.md for the verification transcript):

| # | Component | File | Notes |
|---|-----------|------|-------|
| 15 | Hybrid dense+sparse text search | `src/rag/vector_store.py`, `src/rag/embeddings.py` | Qdrant's OWN native hybrid search (not hand-rolled BM25): `TEXT_COLLECTION` gains a named sparse vector (`bm25`, via fastembed's `Qdrant/bm25` `SparseTextEmbedding` — already a fastembed dependency, no new library) alongside the existing unnamed dense vector. Query-time: `qm.Prefetch` (dense + sparse) fused server-side via `qm.FusionQuery(fusion=qm.Fusion.RRF)` — one Qdrant round-trip, not two. **Verified constraint**: this Qdrant server version (1.18.3) rejects adding a NEW sparse vector config to an already-populated collection (`400: Not existing vector name error`) — sparse config must exist at collection *creation*. Migration: drop + recreate `TEXT_COLLECTION` (sparse config included from the start) + reseed — the exact same operational step `config.py`'s own comment already documents for a `TEXT_EMBED_PROVIDER` switch, not a new kind of migration burden. Pushed into `vector_store.py`'s upsert/search functions only — `src/ingest/pipeline.py` (protected) still just calls `upsert_chunks(...)` with the exact same signature/behavior from its own perspective. |
| 16 | Cross-encoder reranker | `src/rag/rerank.py` (new) | After `_fuse()`'s RRF fusion, before truncating to `TOP_K`: re-score every window that carries text (transcript or paper/deck chunk) against the raw question with a small CPU cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, via `sentence_transformers.CrossEncoder` — same library CLIP already depends on, no new heavy dependency), sort by that score. Frame-only windows (pure visual match, nothing to feed a text cross-encoder) keep their original RRF order and rank after every text-scored window — a cross-encoder can't score what it can't read. Env-flagged (`RERANK_ENABLED`, default true) so `bench.py`'s search-latency gates can be measured with it on vs. off. |
| 17 | Query enhancement (decomposition + expansion) | `src/rag/query_enhance.py` (new) | **Opt-in**, `QUERY_ENHANCEMENT_ENABLED` env flag, default **false** — an extra LLM call before retrieval starts adds real latency to every search, and `accept_latency_p95_ms` is already red; graders/reviewers see the unmodified baseline unless they turn this on. One LLM call (server-wide `llm.env_config()` only, not a tenant's BYO model — keeps `retrieve()`'s signature simple) classifies the question and returns 1 (unchanged) to 3 query strings: sub-questions for a compound question ("How does X combine A and B?" → 2 sub-queries), paraphrases for a single-topic one. Each string is retrieved independently per branch, hits are deduped by point key and re-sorted by score before `_fuse()` runs — `_fuse()`'s own RRF logic is untouched. Best-effort: any failure (no LLM configured, parse error, network error) falls back to `[question]` unchanged, never blocks retrieval. |

Primary eval per component, mirrored into `CLAUDE.md` §7:
- **15** — unit: hybrid query surfaces a lexical-only match (e.g. an exact acronym) that a dense-only query misses; live: re-run `bench.py --quality` (precision@10) and `answer_quality.py` before/after, verbatim numbers, no cherry-picking.
- **16** — unit: reranker reorders a candidate list toward the more textually-relevant one, frame-only windows never crash the reranker; live: same before/after re-run as component 15, plus `search_p95` with `RERANK_ENABLED` on vs. off (latency cost must be disclosed, not hidden).
- **17** — unit: prompt/response parsing, dedup-and-resort logic; a query classified as simple returns `[question]` unchanged; live: recall@10 with the flag on vs. off (this is the one most likely to move recall, since it's the only one that changes what gets retrieved rather than how it's ranked).

### 3c. Live metrics / observability dashboard (added 2026-07-28, DECIDED — own scope)

Not part of the assignment's grading (`eval/rubric.json`/`benchmark/sla.json` don't
gate on it) — an operator-facing addition the user asked for directly. Confirmed
with the user: (a) both new endpoints require the admin bearer token, same
`require_auth` dependency the other admin-sensitive routes already use; (b) scope
is global/admin-wide (all tenants), not per-tenant — this is an ops dashboard, not
a user-facing feature; (c) in-memory only, resets on process restart — this is a
*live* dashboard (3s auto-refresh), not a persisted analytics/BI page, so no new
DB table for ephemeral request/token counters.

| # | Component | File | Notes |
|---|-----------|------|-------|
| 18 | Metrics collection + endpoints | `src/metrics.py` (new), `src/app.py`, `src/llm.py`, `src/db.py`, `src/api/metrics.py` (new) | In-process, lock-protected counters (mirrors the `_INFLIGHT`-style dict pattern already used elsewhere): a `@app.middleware("http")` in `app.py` times every request and buckets it by ROUTE TEMPLATE (`request.scope["route"].path`, not the raw path — avoids one row per `video_id`) + HTTP status; `llm.py`'s 4 call sites (`_answer_openai/_answer_anthropic/_complete_openai/_complete_anthropic`) now read `resp.usage` (OpenAI: `prompt_tokens`/`completion_tokens`; Anthropic: `input_tokens`/`output_tokens`) instead of discarding it, tagged by `kind` ("answer"/"caption"/"complete"/"ping") so only real answer-synthesis calls count toward "LLM answers"; a small hardcoded `model -> ($/1M input, $/1M output)` pricing table estimates cost, with an explicit $0 fallback for unrecognized/self-hosted models (a tenant's BYO vLLM/Ollama endpoint has no real per-token billing) — disclosed as a best-effort estimate from a static table, not live pricing-API data. `search.py::ask()` is wrapped (not restructured) so every one of its return paths gets counted toward the grounding/abstain-rate stat. `db.py` gains `queue_status_counts()` — a `GROUP BY kind, status` rollup across `ms_videos` UNION `ms_documents`, ALL tenants (existing `list_sources()` is tenant-scoped, wrong shape for an ops view — a new function, not a repurposed one). `GET /metrics` (Prometheus text exposition format) and `GET /admin/metrics` (JSON, for the UI's own polling) both gated by `Depends(require_auth)`. |
| — | UI: Metrics page | `ui/index.html` | New sidebar nav item + `data-view="metrics"` panel: stat cards (LLM cost est., input/output tokens, LLM answers, requests, rate-limited [count of 429 *responses this API returned*, honestly 0 until/unless one ever occurs — no new rate-limiting logic was added, only passive counting], **abstain rate** [user-requested addition, beyond the pasted spec]), a per-route latency table (Route/Count/Avg/p50/p95), the live ingest-queue table (Kind/Status/Count), and a response-by-status breakdown — all polled every 3s via `GET /admin/metrics`. Since the browser UI has never sent an Authorization header for ANY call (a pre-existing gap, confirmed live: with `ADMIN_TOKEN` set, the UI's existing register/retry/delete calls already 401 today — disclosed, not fixed here, out of scope for this component), the Metrics page gets its own small admin-token entry (stored in `localStorage`, sent only on this page's polling calls) rather than retrofitting auth into the unrelated existing mutating calls. |

Primary eval:
- unit: request middleware buckets by route template not raw path; LLM usage
  capture correctly reads both provider shapes; cost table's unknown-model
  fallback is `0`, not a crash; `queue_status_counts()` aggregates across BOTH
  tables and ALL tenants; `ask()`'s wrapper counts every return path exactly
  once (no double-count, no missed path).
- contract-probe: `GET /metrics` and `GET /admin/metrics` both 401 without the
  bearer token, 200 with it; `/metrics` is valid Prometheus text exposition
  format.
- manual: the UI's Metrics page renders live, non-fabricated numbers after a
  handful of real `/ask_stream` calls, auto-refreshing every 3s.

**How content gets in (product model):** (1) seeded shared corpus at boot — day-one
value; (2) self-serve at runtime — any user pastes a YouTube/arXiv/deck URL in the
UI, tenant-scoped to them; (3) bulk backfill via the admin API. All three ride the
same queue; search never waits on any of them.

## 4. Corpus & scale plan (right-sized — DECIDED)

The product ships **pre-built with the 8 curated triplets** in `benchmark/corpus.json`
(seeded at boot, component 10). **No bulk backfill lives in the product** — users grow
the corpus themselves via the UI/API (self-serve, tenant-scoped). "Thousands of
triplets" stays a writeup ceiling, not ingested content.

- **Demo + recall set:** the 8 seeded triplets. Label 15–20 queries with the expected
  source+locator for recall@10 ≥ 0.70.
- **Benchmark load is transient test traffic, not content:** bench.py registers a
  burst of a few dozen document ingests to saturate the workers while the
  search-latency probe runs (accept p95 ≤ 300 ms, search p95 ≤ 1.3× idle,
  ≥ 8 chunks/s, worker-kill no-loss), then cleans them up. Test load ≠ corpus.

## 5. Why the queue (writeup seed)

Search is latency-critical and read-only; ingestion is bursty and heavy (OCR, vision
captioning, hundreds of embeddings). The queue is the seam that lets `POST
/admin/documents` return 202 in milliseconds while workers drain at their own pace,
retry per-stage without redoing finished stages, and scale by adding replicas. The
base repo adds a twist worth writing up: Prefect alone is FIFO, so a **WFQ dispatcher**
holds the waiting line in Postgres and admits work round-robin per user — fairness on
top of the managed queue. Managed (Prefect Cloud) vs self-hosted (Redis
Streams/RabbitMQ) trade-off + the stretch broker goes here.

## 6. Risks / open questions

- **Deck sourcing:** SlidesLive embeds are awkward to fetch; PMLR/author-site PDF
  decks are easier. Fallback: export decks to PDF and upload via presign/storage URI.
- **Vision captioning cost:** cap captioned slides per deck; only caption slides with
  < N chars of extracted text.
- **`/ask_stream` shape:** bench only checks status 200 + a `"page"` token in the
  stream; keep the SSE event format close to the README's (trace → citations → answer).
- **Dispatcher + documents:** unify `wfq_claim` over both tables or run documents
  FIFO first; decide when wiring step 5.
