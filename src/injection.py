"""Indirect prompt-injection guardrail — DESIGN.md §3h component 49.

This is a RAG system whose evidence is *user-registered documents*, so the
corpus is an untrusted input channel that reaches three LLM prompts verbatim:
the answer prompt (`src/llm.py`), the query-enhancement prompt
(`src/rag/query_enhance.py`), and — the one that is easy to forget — the LLM
judge in `benchmark/answer_quality.py` that produces our own eval numbers.

Everything is enforced HERE, at the prompt boundary, and nowhere else. Two
reasons, both deliberate:

  * Sanitizing at ingest would corrupt what we store, and would leave every
    already-indexed chunk unprotected. The prompt boundary is the only place
    all callers converge.
  * One module means one place to audit — the same contract `src/cache.py`
    has for fail-open behaviour.

**What this does and does not do.** `sanitize_evidence()` is STRUCTURAL: it
flattens newlines, defangs *our own* moment-label grammar, strips chat control
tokens, and caps length. It does NOT rewrite instruction-shaped English, and
that restraint is the design, not a gap. This corpus is ML research papers; a
paper about prompt injection legitimately contains "ignore all previous
instructions", and a rewriter would corrupt the evidence behind every honest
answer about that literature. Instruction-shaped text is instead **fenced**
(so the model is told where untrusted input begins) and **flagged** by
`scan()` (so it is visible in traces).

The load-bearing fix is the newline flattening. `llm._label()` renders one
moment per line, so a chunk containing a lookalike `[7] @ 00:00 from "…" —
excerpt: "…"` line forges a moment that retrieval never returned — and because
the forged number lands inside `_validate_citations()`'s `1..n_frames` bound,
it renders as a real citation with a working deep-link. One moment in, one line
out kills that class outright.
"""
from __future__ import annotations

import re

# Fence markers. Deliberately long and unlikely to occur in real text, and
# always stripped from the untrusted span before it is wrapped — a fence a
# caller can close by pasting the marker is decoration, not a boundary.
QUESTION_OPEN = "<<<USER_QUESTION>>>"
QUESTION_CLOSE = "<<<END_USER_QUESTION>>>"
EVIDENCE_OPEN = "<<<RETRIEVED_EVIDENCE>>>"
EVIDENCE_CLOSE = "<<<END_RETRIEVED_EVIDENCE>>>"

# Default cap for one excerpt. Chunks are already bounded by the chunker, so
# this is a backstop against a pathological title or a future parser change,
# not the primary control.
EVIDENCE_LIMIT = 4_000
TITLE_LIMIT = 300

_TRUNCATED = "…[truncated]"

# Anything that ends a line — including the Unicode separators, which some
# PDF extractors emit and which several tokenizers treat as newlines.
_LINEBREAKS = re.compile(r"[\r\n  \v\f]+")

# What a newline becomes. NOT a plain space: component 14 renders paper tables
# as " | "-joined cells and "\n"-joined ROWS (src/ingest/paper.py) so the
# structure survives as embeddable text. Flattening those to spaces silently
# undid that work at the prompt boundary — verified:
#   'Method  Acc\nBERT  88.4\nT5  91.2' -> 'Method Acc BERT 88.4 T5 91.2'
# with the row boundaries gone. A visible pilcrow keeps rows distinguishable to
# the model while still being a single physical line, so T1 stays dead.
_ROW_SEP = " ¶ "

# C0 control characters (keep tab, which is only whitespace). C1 (U+0080-U+009F)
# is deliberately NOT deleted: cp1252 mojibake from PDF extraction (mis-decoded
# smart quotes and dashes) lands in that range, and deleting it silently loses
# real characters — the same "information destroyed, sentence still grammatical"
# failure the <s>/</s> bug had. Those are escaped by _escape_c1 instead.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f]")
_C1 = re.compile(r"[\x7f-\x9f]")

# Chat-template / control tokens. `<\|…\|>` covers the whole ChatML family in
# one rule; the rest are literal tokens from Llama/Mistral-style templates.
_CONTROL_TOKENS = re.compile(
    r"<\|[^|<>]{0,40}\|>"
    r"|</?s>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    r"|</?system>"
    r"|</?assistant>"
    r"|</?user>",
    re.IGNORECASE,
)

# OUR moment-label grammar, the thing a forgery has to imitate:
#   [n] @ <locator> from "<source>" — excerpt: "<text>"
#
# Both patterns are deliberately NARROW, because the alternative is worse than
# the attack. This corpus is ML papers and talk transcripts, so text like
# "Rows [1] @ 5 epochs, [2] @ 10 epochs" and "the loss - excerpt: we log it"
# occurs honestly; a loose pattern rewrites real evidence on every answer. An
# audit against realistic paper strings caught exactly those two false
# positives (EVIDENCE.md), which is what narrowed these:
#
#   * `_LABEL_PREFIX` requires `@` to be followed by a locator shape we
#     ACTUALLY emit — `_where()` in src/rag/search.py returns only a timestamp
#     (`m:ss`/`h:mm:ss`), `page N`, or `slide N`. "@ 5 epochs" is therefore
#     not a label and is left alone.
#   * `_LABEL_SEP` requires the opening double-quote that the real separator
#     always has (`— excerpt: "`), so prose that merely contains the word
#     "excerpt:" is untouched.
#
# Disclosed limit: a forgery that uses a locator shape we never emit (`[9] @
# nowhere`) is not rewritten by `_LABEL_PREFIX`. It is also not a convincing
# label for that reason, it is still confined to one line, and SYSTEM rule 6
# sits behind it — a narrower pattern that never corrupts real evidence is the
# better trade here, stated rather than hidden.
_LOCATOR = r"(?:\d{1,2}:\d{2}(?::\d{2})?|p\.\s*\d|page\s+\d|slide\s+\d)"
_LABEL_PREFIX = re.compile(rf"\[\s*(\d{{1,4}})\s*\]\s*@\s*(?={_LOCATOR})", re.IGNORECASE)
_LABEL_SEP = re.compile(r"([—–-])\s*excerpt\s*:\s*(\")", re.IGNORECASE)

# Detection only — never used to rewrite. Phrases that indicate someone is
# addressing the model rather than describing a result.
_INSTRUCTION_PATTERNS = (
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|preceding)\s+"
    r"(?:instructions?|prompts?|rules?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding|"
    r"citation|system)\s*\w*\s*(?:instructions?|rules?|prompts?)?",
    r"note\s+to\s+(?:the\s+)?(?:evaluator|judge|grader|assistant|model)",
    r"you\s+(?:must|should|will)\s+(?:never|always|now)\b",
    r"system\s+prompt",
    r"developer\s+mode",
    r"regardless\s+of\s+the\s+(?:answer|content|evidence)",
    r"new\s+instructions?\s*:",
    r"act\s+as\s+(?:a|an|the)\b",
)
_INSTRUCTION_RE = re.compile("|".join(_INSTRUCTION_PATTERNS), re.IGNORECASE)


_BRACKET_ESCAPES = str.maketrans({"<": "⟨", ">": "⟩", "[": "⟦", "]": "⟧"})


def _escape_token(m: re.Match) -> str:
    """`<|im_start|>` -> `⟨|im_start|⟩`, `[INST]` -> `⟦INST⟧`. The delimiters
    become Unicode lookalikes, so no chat template can match the token while a
    reader (and the model) still sees exactly which token the document named.
    Only ever applied to a matched control token, never to ordinary prose — so
    a real bracketed citation like `[4]` is untouched."""
    return m.group(0).translate(_BRACKET_ESCAPES)


def sanitize_evidence(text, limit: int = EVIDENCE_LIMIT) -> str:
    """Make one untrusted span safe to interpolate into a prompt line.

    Structural only — see the module docstring. Benign text is returned
    byte-identical, which is a hard requirement: this runs over the evidence
    behind every honest answer, so any gratuitous rewriting is data corruption
    with extra steps.
    """
    if not text:
        return ""
    out = str(text)
    out = _LINEBREAKS.sub(_ROW_SEP, out)
    out = _CONTROLS.sub("", out)
    out = _C1.sub(lambda m: f"\\x{ord(m.group(0)):02x}", out)
    # ESCAPE control tokens rather than delete them. Deleting was the first
    # cut and it silently destroyed information: an NLP paper explaining that
    # "<s> marks sequence start and </s> the end" came out as "marks sequence
    # start and the end" — the sentence survived, its meaning did not. Swapping
    # the angle brackets for their Unicode lookalikes keeps the text readable
    # and faithful while making it un-matchable by any chat template.
    out = _CONTROL_TOKENS.sub(_escape_token, out)
    # Defang the label grammar without destroying the content it wraps: the
    # numbers, names and claims all survive, only the shape that would let the
    # text pass as a *new* moment is broken.
    out = _LABEL_PREFIX.sub(r"(\1) @ ", out)
    out = _LABEL_SEP.sub(r"\1 excerpt \2", out)
    # Collapse the runs of whitespace the substitutions above can leave behind,
    # but only when something was actually removed, so untouched text stays
    # byte-identical.
    if out != text:
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
    if limit and len(out) > limit:
        out = out[:limit].rstrip() + _TRUNCATED
    return out


def scan(text) -> list[str]:
    """Which injection signatures this text matches. Never mutates, never
    raises. Used for span attributes and the `/ask` payload — detection is
    observability here, not a block (DESIGN.md §3h: abstaining on detection
    would let any user disable their own search by registering a document)."""
    if not text:
        return []
    try:
        raw = str(text)
    except Exception:
        return []
    found: list[str] = []
    if _LABEL_PREFIX.search(raw) or _LABEL_SEP.search(raw):
        found.append("forged_label")
    if _CONTROL_TOKENS.search(raw):
        found.append("control_token")
    if _INSTRUCTION_RE.search(raw):
        found.append("instruction_override")
    return found


def scan_all(texts) -> list[str]:
    """Deduped, sorted union of `scan()` over many spans. Fails closed to an
    empty list — a detector error must not break the read path."""
    try:
        return sorted({flag for t in texts for flag in scan(t)})
    except Exception:
        return []


def _fence(value: str, open_marker: str, close_marker: str) -> str:
    body = (value or "").replace(open_marker, "").replace(close_marker, "").strip()
    return f"{open_marker}\n{body}\n{close_marker}"


def fence_question(question: str) -> str:
    """Wrap the user's own question. It is NOT rewritten — a question is
    allowed to contain brackets, quotes, or the word "instructions" — but it
    is fenced so it cannot be read as part of the surrounding instructions,
    and it cannot close its own fence."""
    return _fence(question, QUESTION_OPEN, QUESTION_CLOSE)


def fence_evidence(block: str) -> str:
    """Wrap a whole retrieved-evidence block."""
    return _fence(block, EVIDENCE_OPEN, EVIDENCE_CLOSE)
