"""Query enhancement (decomposition + expansion) — DESIGN.md §3b component 17.

**Opt-in** (`QUERY_ENHANCEMENT_ENABLED`, default false): an LLM call before
retrieval even starts adds real latency to every search, and
`accept_latency_p95_ms` is already red (EVIDENCE.md) — this must never
become the baseline latency graders/reviewers see unless explicitly turned
on.

One LLM call (the server-wide `llm.env_config()` only, never a tenant's own
BYO model — keeps `retrieve()`'s signature simple) classifies the question
and returns 1-3 query strings: sub-questions for a compound question ("How
does X combine A and B?" -> 2 sub-queries), alternate phrasings for a
single-topic one, or the question unchanged when neither would help.

Best-effort: any failure (no LLM configured, parse error, network error)
falls back to `[question]` unchanged — this never blocks retrieval.
"""
from __future__ import annotations

import json

from .. import injection, llm

_SYSTEM = (
    "You expand a search query for a retrieval system over a corpus of ML "
    "research talks, papers, and slide decks.\n"
    "If the question is COMPOUND (asks about two or more distinct things, "
    'e.g. "How does X combine A and B?"), split it into 2-3 separate '
    "sub-questions, one per distinct thing.\n"
    "If the question is about a SINGLE topic, instead write 1-2 alternate "
    "phrasings that use different wording for the same underlying question — "
    "this catches semantically-equivalent chunks the original wording might "
    "miss.\n"
    "If neither would help (the question is already simple and well-"
    "phrased), return the question UNCHANGED as the only item.\n"
    'Respond with ONLY minified JSON, no prose: {"queries": ["...", ...]}'
)

_MAX_QUERIES = 3


def _parse(raw: str) -> list[str] | None:
    """Judge-style JSON parsing (tolerant of a markdown code fence) -> a
    cleaned, capped list of query strings, or None on anything malformed."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    queries = parsed.get("queries") if isinstance(parsed, dict) else None
    if not isinstance(queries, list):
        return None
    cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    return cleaned[:_MAX_QUERIES] or None


def _build_prompt(question: str) -> str:
    """Component 49: fence the question so it reads as data, not as extra
    instructions to the expander. Kept a separate pure function so the
    construction is unit-testable without an LLM."""
    return f"QUESTION:\n{injection.fence_question(question)}"


def enhance_query(question: str) -> list[str]:
    """`[question]` unchanged on ANY failure — no LLM configured, parse
    error, network error. Never blocks retrieval; this is pure best-effort
    widening of the candidate pool fed into fusion."""
    cfg = llm.env_config()
    if cfg is None:
        return [question]
    try:
        raw = llm.complete(_SYSTEM, _build_prompt(question), cfg)
    except Exception:
        return [question]
    return _parse(raw) or [question]
