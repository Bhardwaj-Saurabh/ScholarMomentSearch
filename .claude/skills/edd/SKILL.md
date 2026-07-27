---
name: edd
description: Evaluation-Driven Development loop for ScholarMomentSearch. MUST be invoked before starting ANY component from DESIGN.md (new feature, pipeline, endpoint, UI change). Defines the evaluation first, proves it fails, gates implementation on it, and records real evidence. Use when the user says "build component N", "implement X", "start on the paper/deck/admin/search work", or any coding task bigger than a typo.
---

# EDD — Evaluation-Driven Development (the master loop)

You are about to build a component. Do NOT write implementation code yet.
EDD wraps TDD: TDD proves the code does what the developer intended;
EDD proves the system does what the PRODUCT requires (SLAs, recall, grounding).

## The loop (no step may be skipped or reordered)

### 1. SCOPE — pin the component
- Find the component in DESIGN.md §3 (the 11-component table). Quote its row.
- Read the matching contract lines in README.md and ARCHITECTURE.md.
- If the work isn't in DESIGN.md, STOP: update DESIGN.md first (one commit), then continue.

### 2. DEFINE EVALS FIRST — what does "working" mean, measurably?
Write ALL that apply before any implementation:
- **Unit/behavior tests** (TDD layer): create `tests/test_<component>.py` — invoke the
  `tdd` skill for the red/green mechanics.
- **Contract probes**: if the component touches an endpoint, add/extend a probe in
  `tests/test_contract.py` (status codes, 202-before-work, response shape, locators).
- **Product evals**: if the component affects retrieval/answers, add labeled queries to
  `benchmark/labeled_queries.json` (query → expected source_id + kind + locator) —
  these feed recall@10 in bench.py.
- **SLA relevance**: state which `benchmark/sla.json` gates this component can affect.

### 3. RED — prove the evals fail
Run the new tests/probes. They MUST fail (or error) against current code.
If something passes before implementation, the eval is too weak — fix the eval.

### 4. IMPLEMENT — smallest code that satisfies the evals
- Follow the file plan in DESIGN.md. Mirror existing patterns (the video flow is the
  template for document flows; reuse `src/` helpers before writing new ones).
- Never modify PROVIDED files listed in CLAUDE.md "Hard invariants" unless DESIGN.md
  explicitly says to extend them.

### 5. GREEN — run everything relevant
- `uv run pytest tests/ -x -q` — all tests pass.
- Contract probes pass against a running stack when the component is API-facing.
- If the component touches queue/pipeline/index: run the `sla-gate` skill
  (bench.py, plus `--resilience` for pipeline/status changes).

### 6. EVIDENCE — record reality
Append to `EVIDENCE.md` (create if missing): date, component #, commands run,
verbatim key numbers/exit codes, and what remains red. NEVER write a number that
did not come from a run in this session. Fabricated numbers = automatic fail.

### 7. SHIP — commit
Short single-line message, no co-author trailer. Push to origin main.
Then spawn the `spec-guardian` agent on the diff before declaring the component done.

## Refusals this skill enforces
- Asked to "just implement quickly, tests later" → decline; steps 2–3 are the assignment.
- Asked to relax `sla.json` / `rubric.json` thresholds to pass → decline. Fix the system.
- An eval can't be written because the requirement is vague → stop and clarify with the
  user BEFORE coding, then encode the answer in DESIGN.md.
