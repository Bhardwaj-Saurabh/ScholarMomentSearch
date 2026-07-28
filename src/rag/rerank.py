"""Cross-encoder reranker — DESIGN.md §3b component 16.

RRF fusion (_fuse() in search.py) is rank-based, score-agnostic by design —
it never reads the actual question against the actual candidate text, only
each hit's RANK within its own branch. A cross-encoder reads (question,
passage) pairs directly, correcting exactly the ties/near-ties RRF can't
distinguish (see EVIDENCE.md's precision@10 diagnosis: RRF flattens magnitude
into rank, letting a borderline match tie a genuinely strong one).

Frame-only windows (pure visual match, no transcript/chunk text at that
instant) have nothing for a text cross-encoder to read — they keep their
original fused order and always rank after every text-scored window.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .. import config


@lru_cache
def _model():
    from sentence_transformers import CrossEncoder

    return CrossEncoder(config.RERANK_MODEL)


def _window_text(w: dict[str, Any]) -> str | None:
    """The text a window carries, if any — transcript for a video window,
    page/slide text for a document window. None for a pure frame match."""
    tx = w.get("text")
    return tx.get("text") if tx else None


def rerank(question: str, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-score every text-bearing window against the question; frame-only
    windows keep their original relative order and rank after all of them."""
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
    scores = _model().predict(pairs)
    ordered = [w for (w, _text), _score in
              sorted(zip(scored, scores), key=lambda item: item[1], reverse=True)]
    return ordered + text_free
