"""Cross-encoder reranker — DESIGN.md §3b component 16.

RRF fusion (_fuse() in search.py) is rank-based, score-agnostic by design —
it never reads the actual question against the actual candidate text, only
each hit's RANK within its own branch. A cross-encoder reads (question,
passage) pairs directly, correcting exactly the ties/near-ties RRF can't
distinguish (see EVIDENCE.md's precision@10 diagnosis: RRF flattens magnitude
into rank, letting a borderline match tie a genuinely strong one).

Frame-only windows (pure visual match, no transcript/chunk text at that
instant) have nothing for a text cross-encoder to read — they keep their
FUSED-RANK POSITION (component 59, DESIGN.md §3m). The original shape
(`ordered + text_free`) demoted every pure-visual moment below every text
window; with BRANCH_TOP_K=20 text candidates that meant a visual-only moment
effectively could never reach the final top-k, directly costing recall on
queries expecting a video citation. The cross-encoder never read those
windows, so it has no basis to demote them: text windows re-sort by CE score
among the positions text windows collectively held, frame-only windows hold
their own fused positions. Reorder only — never drop, never add.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .. import config, tracing


@lru_cache
def _model():
    from sentence_transformers import CrossEncoder

    return CrossEncoder(config.RERANK_MODEL)


def warm() -> None:
    """Load the cross-encoder ahead of the first request (component 55,
    DESIGN.md §3m). Measured lazily: 5725.9ms cold in-process, 68s worst case
    when the first-ever call also downloads the model — a cost that lands on a
    real user's query unless paid at boot. Fail-open: a failed warm just means
    the first rerank pays the load, exactly as before."""
    if not config.RERANK_ENABLED:
        return
    try:
        _model()
    except Exception as exc:
        print(f"[warmup] reranker load failed ({exc!r}) — first rerank will pay it")


def _window_text(w: dict[str, Any]) -> str | None:
    """The text a window carries, if any — transcript for a video window,
    page/slide text for a document window. None for a pure frame match."""
    tx = w.get("text")
    return tx.get("text") if tx else None


def rerank(question: str, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-score every text-bearing window against the question; frame-only
    windows keep their fused-rank position (see module docstring)."""
    scored: list[tuple[dict[str, Any], str]] = []
    text_free: list[dict[str, Any]] = []
    for w in windows:
        text = _window_text(w)
        if text:
            scored.append((w, text))
        else:
            text_free.append(w)

    if not scored:
        return windows  # nothing to rerank against — never load the model for no reason

    pairs = [(question, text) for _, text in scored]
    # Its own span: the cross-encoder is the single most expensive step after
    # the LLM (measured cold at 5725.9ms, warm ~100ms — component 45), and the
    # model-load spike is invisible unless the inference is timed separately
    # from the surrounding fusion work.
    with tracing.span("rerank_model", model=config.RERANK_MODEL) as _sp:
        scores = _model().predict(pairs)
        _sp.set_attrs(scored=len(pairs),
                      frame_only=len(text_free),
                      top_score=float(max(scores)) if len(scores) else 0.0,
                      min_score=float(min(scores)) if len(scores) else 0.0)
    ordered = iter(w for (w, _text), _score in
                   sorted(zip(scored, scores), key=lambda item: item[1], reverse=True))
    # Position merge (component 59): walk the ORIGINAL fused order — a text
    # position gets the next-best text window by cross-encoder score, a
    # frame-only position keeps its own window. Same length, same identities,
    # only the order among text windows changes.
    frames = iter(text_free)
    return [next(ordered) if _window_text(w) else next(frames) for w in windows]
