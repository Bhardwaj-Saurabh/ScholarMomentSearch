---
name: tdd
description: Red/green/refactor test-first mechanics for one module or function in ScholarMomentSearch (pytest + uv). Invoked from the edd skill's step 2, or directly when writing/fixing unit tests for parsers, chunkers, db helpers, fusion logic, or API handlers.
---

# TDD — red / green / refactor for one unit of code

TDD is the inner loop of EDD: it verifies the CODE's behavior. Product-level truth
(recall, SLAs, grounding) still belongs to the evals in bench.py — passing unit tests
alone never means "done".

## Environment
- `uv venv` at the repo root (Python 3.12); `uv pip install -r requirements.txt pytest`.
- Tests live in `tests/`, named `test_<module>.py`. Run: `uv run pytest tests/ -x -q`.
- No network in unit tests: fixture PDFs/decks go in `tests/fixtures/` (tiny,
  generated, never real conference media — media files are git-ignored).
  Mock Qdrant/Postgres/LLM/storage at the module boundary (`src.db`, `src.storage`,
  `src.rag.vector_store`, `src.llm`) — the real ones are exercised by bench.py.

## The cycle
1. **RED** — write the smallest test that expresses ONE behavior the code must have.
   Run it. Watch it fail for the RIGHT reason (assertion, not ImportError typos).
2. **GREEN** — write the minimum implementation that passes. Resist adding
   speculative parameters, abstractions, or "while I'm here" changes.
3. **REFACTOR** — with tests green, clean up duplication and naming. Re-run.
4. Repeat per behavior. Commit when a coherent behavior set is green.

## What to test per component (examples, not limits)
- `paper.py`: page numbers survive chunking (every chunk carries the right `page`);
  chunk size bounds; section titles attached; empty/scanned page doesn't crash.
- `deck.py`: one slide → one-or-few chunks with correct `slide`; text-light slide is
  flagged for captioning; PPTX and PDF paths agree on slide numbering.
- Admin API: 202 + `{id, status:"pending", kind}` shape; 400 on bad kind/uri; 401
  without Bearer; NO parsing side-effects in the request path (assert enqueue mock
  called, parser mock NOT called).
- Fusion/citations: `kind` and locator ride from payload → citation dict untouched;
  citation validation strips unretrieved refs.

## Rules
- A bug fix starts with a failing test that reproduces the bug.
- Never weaken an assertion to make it pass; fix the code or escalate the design.
- Don't test PROVIDED base-repo internals you aren't changing — test YOUR seam.
