---
name: sla-gate
description: Run and interpret the SLA benchmark (benchmark/bench.py) for ScholarMomentSearch. Use after implementing anything that touches the queue, ingestion pipelines, index, or search path; before any deploy; and whenever the user asks "are we meeting the SLAs", "run the benchmark", or "is it fast enough". Includes the --resilience worker-kill gate.
---

# SLA gate — the numbers decide, not vibes

`benchmark/sla.json` holds the graded targets. bench.py exits non-zero on ANY miss.
These thresholds are FROZEN — tune the corpus/load knobs to your machine, never the gates.

## Preconditions
- Stack running: `docker compose up -d` (API on :8100, ≥1 worker, clip, seed done).
- `ADMIN_TOKEN` exported in the shell running bench.
- For throughput/resilience rows: ≥2 workers (`docker compose up -d --scale worker=2`).

## Run matrix — what you changed decides what you run
| Change touched | Required runs |
|---|---|
| admin API / registration | `python benchmark/bench.py` (accept-latency row at minimum) |
| any ingest flow, jobs.py, dispatcher, db status logic | full `bench.py` AND `bench.py --resilience` |
| retrieval / fusion / citations | full `bench.py` (recall row) + grounding-auditor agent |
| deploy prep | everything, plus `--json out.json` and keep the artifact |

## Resilience gate mechanics (worker-kill)
1. Start a batch of document ingests (bench does this).
2. `docker kill <worker-container>` mid-stream — NOT graceful stop.
3. Restart the worker. Assert: zero rows stuck in non-terminal states forever,
   every source reaches `indexed`, finished stages did NOT re-run (check timestamps/
   Prefect task states, not gut feeling).
If it fails: the bug is status/commit ordering in the pipeline (status must flip to
`indexed` only AFTER the Qdrant upsert). Fix the pipeline. NEVER fix the test.

## Interpreting results
- Report the table verbatim into `EVIDENCE.md` — numbers, pass/fail, exit code.
- A red row = the component that owns it goes back to EDD step 4. Do not proceed to
  the next DESIGN.md component on a red gate.
- p95 needs ≥20 samples to be a real quantile (bench handles this — don't shrink n).
- Search-during-ingest ratio is THE architecture proof. If it's >1.3×, suspect:
  parsing in the request path, shared process, or a starved event loop — see
  README.md Troubleshooting before touching code.

## Forbidden
- Editing sla.json thresholds, skipping `--resilience` after pipeline changes,
  reporting any number not produced by a run in this session.
