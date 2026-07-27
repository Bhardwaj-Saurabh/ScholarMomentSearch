---
name: contract-probe
description: Live API contract verification for ScholarMomentSearch against a running stack. Use after wiring any endpoint, before commits that touch src/api/, and as the smoke test after docker compose up or a Fly deploy. Checks the assignment's contract exactly (202 semantics, locators, auth, error codes, provided endpoints unchanged).
---

# Contract probe — the API does what README.md §"The API contract" says

Run against `BASE_URL` (default `http://localhost:8100`; use the Fly URL post-deploy).
Every probe is a curl one-liner; paste outputs verbatim when reporting.

## The checklist (all must hold)
1. **UI up**: `curl -sf $BASE_URL/` → 200.
2. **Async accept**: `POST /admin/documents` with Bearer + valid paper body →
   HTTP **202**, body has `id`, `status:"pending"`, `kind` — and returns in <1 s even
   for a 60-page PDF URL (accept ≠ parse).
3. **Auth**: same call without/with wrong Bearer → **401** JSON body.
4. **Validation**: bad kind (`"kind":"podcast"`), malformed uri → **400** JSON body.
5. **Unified status**: `GET /admin/sources` → both videos and documents, each with
   `id`, `kind`, `status`, `title`, `pct`.
6. **Provided endpoints unchanged**: `POST /api/videos` with a YouTube URL still 202s
   with `{video_id, status}`; `POST /api/ask` still answers; `GET /api/health` 200.
7. **Cross-source citations**: after the seed corpus is indexed,
   `GET "/ask_stream?q=how+does+attention+avoid+recurrence"` streams SSE containing
   citations with a `"start_ms"` locator AND a `"page"` locator (one query, ≥2 kinds);
   every deck citation carries `"slide"`.
8. **Grounding on empty**: a nonsense query (`q=zorbulax+quantum+pickles`) must NOT
   invent citations — abstain/empty citations is the correct outcome.

## Discipline
- A failing probe is a contract bug: return to the owning component's EDD loop.
- Codify any probe you run manually into `tests/test_contract.py` so it reruns forever
  (skip marker when the stack isn't up: `pytest.mark.skipif` on a health check).
- Record pass/fail + interesting bodies in `EVIDENCE.md`.
