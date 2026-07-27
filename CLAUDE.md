# CLAUDE.md — ScholarMomentSearch engineering rules

This file governs ALL engineering work in this repo. It exists so we deliver **exactly**
what the design says — no more, no less — with every claim backed by an evaluation.

---

## 1. Source of truth (read before coding; never contradict)

| Doc | Authority over |
|---|---|
| `README.md` | the assignment: API contract, Definition of Done, grading, SLAs |
| `DESIGN.md` | WHAT we build: the 11 components, build order, decided scope |
| `ARCHITECTURE.md` | HOW it fits: payloads, lifecycles, crash-safety, NFRs |
| `benchmark/sla.json` | the hard numbers (frozen — never edit to pass) |
| `benchmark/corpus.json` | the 8 seeded triplets (decided: no bulk backfill) |
| `AGENTS.md` | the grader's non-negotiables |

Scope change (add/drop/alter a component) ⇒ update `DESIGN.md` first, in its own
commit, with the user's agreement. Code that isn't traceable to a DESIGN.md component
does not get written.

## 2. Methodology: EDD, with TDD inside it — ENFORCED

**Evaluation-Driven Development**: no implementation exists until an evaluation defines
what "working" means, and nothing is "done" until that evaluation passes on a real run.

```
SCOPE → DEFINE EVALS → RED → IMPLEMENT → GREEN → EVIDENCE → SHIP
  (1)       (2)         (3)      (4)        (5)       (6)       (7)
```

Enforcement rules (these are hard, not aspirational):
- **E1** — Starting any DESIGN.md component, or any coding task bigger than a typo:
  invoke the `edd` skill FIRST. If asked to skip it ("just code it quickly"), decline
  and explain: the evals ARE the assignment.
- **E2** — Evals precede code. Step 2 artifacts (unit tests, contract probes, labeled
  queries) must exist AND fail (step 3) before implementation begins (step 4).
- **E3** — Three layers of eval, all required where applicable:
  unit tests (`tests/`, TDD) → contract probes (`tests/test_contract.py` + live curl)
  → product evals (`benchmark/bench.py`: SLAs, recall@10; `--resilience` for pipeline
  changes).
- **E4** — Numbers are sacred. Every metric reported anywhere (EVIDENCE.md, PRODUCT_EVAL,
  chat) comes verbatim from a run in the current session. Fabrication = automatic fail.
- **E5** — Red gate = stop. A failing SLA row or test blocks moving to the next
  component. Fix the system, never the threshold (`sla.json`/`rubric.json` are frozen).
- **E6** — Every completed component appends a dated entry to `EVIDENCE.md`
  (commands, verbatim numbers, exit codes, what's still red).

## 3. Skills — invoke at these moments

| Moment | Skill |
|---|---|
| Starting ANY component / feature / fix beyond a typo | `edd` (the master loop) |
| Writing unit tests inside EDD step 2, or fixing a bug (repro test first) | `tdd` |
| Touched queue/pipeline/index/search; pre-deploy; "are we fast enough?" | `sla-gate` |
| Wired an endpoint; touched `src/api/`; post-`docker compose up`; post-deploy smoke | `contract-probe` |
| Producing the final submission report | `fde-momentsearch-scaled-eval` |

## 4. Agents — spawn at these moments

| Moment | Agent | Mode |
|---|---|---|
| EDD steps 3 & 5, SLA runs, pre-deploy suites | `eval-runner` | runs suites, reports verbatim; never edits |
| EDD step 7 (before declaring a component done), any push touching `src/` | `spec-guardian` | read-only diff review vs the source-of-truth docs |
| After retrieval/fusion/citation/prompt changes; pre-deploy | `grounding-auditor` | adversarial: tries to catch invented citations, tenant leaks |

A component is DONE only when: its tests are green, relevant SLA rows pass,
`spec-guardian` returns PASS, and EVIDENCE.md is updated. All four.

## 5. Hard invariants (violating any = the change is wrong)

- **Provided surface unchanged**: `/api/videos*`, `/api/ask`, the video pipeline
  (`src/ingest/pipeline.py`, `fetch.py`, `frames.py`, `dedup.py`, `transcript.py`),
  `src/api/videos.py`, `src/dispatcher.py`. Extend around them; never edit their
  behavior. (`search.py`, `db.py`, `jobs.py`, `worker.py`, `seeding.py` may be
  extended additively where DESIGN.md says so.)
- **202-before-work**: registration endpoints insert a row + schedule a run + return.
  Parsing/embedding in a request path is an architecture bug even if fast.
- **One shared text space**: paper/deck/transcript chunks all land in `moments_text`
  with `user_id`, `kind`, locator (`page`/`slide`/`t_start`), `embed_version`,
  deterministic uuid5 IDs.
- **Crash-safe ordering**: status flips to `indexed` only AFTER the Qdrant upsert
  returns. Retries must not redo finished stages.
- **Grounded or silent**: citations only from retrieval; empty retrieval ⇒ abstain.
  No invented page/slide/timestamp, ever.
- **Tenancy everywhere**: every new row, vector payload, and storage key carries
  `user_id`; every query filters by it.
- **Hygiene**: `.env`, `.venv/`, `__pycache__/`, model caches, media (`.pdf`, `.pptx`,
  `.mp4`) are never committed. Check `git status --porcelain` before every commit.

## 6. Working rules

- **Python**: `uv` only (never `python -m venv` / bare `pip`); venv at repo root.
  Tests: `uv run pytest tests/ -x -q`.
- **Commits**: short single-line message, no co-author trailers, no emoji prefixes.
  Push to `origin main` after each green component.
- **Run it real**: local stack via `docker compose up`; SLA/bench runs need the stack
  up and `ADMIN_TOKEN` exported. Don't benchmark a stack that isn't running (health
  check first).
- **Reuse before writing**: the video flow is the template — mirror its status
  lifecycle, retry policy, and upsert idempotency for documents. Search `src/` for an
  existing helper before adding a new one.
- **Plain reporting**: failures reported with output, skipped steps named as skipped.

## 7. Component → primary eval map (DESIGN.md §3)

| # | Component | Primary eval that proves it |
|---|---|---|
| 1 | `documents` table + unified status | unit: status transitions; probe: `/admin/sources` shape |
| 2 | `paper.py` parser | unit: page survives chunking; fixture PDF in `tests/fixtures/` |
| 3 | `deck.py` parser | unit: slide numbering PDF/PPTX; caption-flag for image slides |
| 4 | `ms-ingest-document` flow | `bench.py --resilience` (no loss, no stage re-run) |
| 5 | queue wiring (`jobs`, dispatcher) | accept-latency p95 ≤ 300 ms; WFQ fairness unit test |
| 6 | `POST /admin/documents` + `GET /admin/sources` | contract-probe checklist 2–5 |
| 7 | cross-source search + `/ask_stream` | recall@10 ≥ 0.70 on `labeled_queries.json`; SSE probe 7 |
| 8 | UI citation render + Paper/Deck tab | manual demo script + probe 7 (locators present) |
| 9 | benchmark TODOs filled | `bench.py` exit 0 end-to-end |
| 10 | corpus seeding | fresh-boot test: seed exits 0, cross-source query answers |
| 11 | self-serve tab | probe: register via UI path → indexed → queryable |

Cross-cutting, always: `grounding-auditor` after 7/8/10; search-during-ingest ratio
≤ 1.3× after 4/5; provided-endpoint regression (probe 6) after everything.

## 8. Definition of done for the whole assignment

`python benchmark/bench.py` exit 0 · `--resilience` exit 0 · all contract probes pass
locally AND on Fly · one query cites video+paper+deck with working deep-links ·
`spec-guardian` PASS on the final diff · `PRODUCT_EVAL.md` generated by the eval skill
from real runs · README "How I ran it" section added · no hygiene violations in
`git log --stat`.
