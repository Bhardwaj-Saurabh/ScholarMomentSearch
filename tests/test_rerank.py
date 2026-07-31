"""Component 16 (DESIGN.md §3b) — cross-encoder reranker.

After _fuse()'s RRF-based cross-modal fusion, before truncating to TOP_K:
re-score every window that carries actual text (transcript or paper/deck
chunk) against the raw question with a small cross-encoder, and sort by that
score — RRF is rank-based (score-agnostic) by design, so two windows that tie
or nearly tie on fused score may be very differently relevant once the actual
text is read against the actual question. Frame-only windows (nothing for a
text cross-encoder to read) keep their FUSED-RANK POSITION (component 59,
DESIGN.md §3m): the old `ordered + text_free` shape demoted every pure-visual
moment below every text window, which with 20 text candidates meant a
visual-only moment effectively could never reach the final top-k at all —
directly costing recall on queries expecting a video citation.

Most tests here mock `rerank._model()` — pure reordering logic, no need for
a real model download. One real-model test proves the actual cross-encoder
correctly favors the more relevant passage for a real question.
"""
from __future__ import annotations

from src.rag import rerank


class _FakeCrossEncoder:
    def __init__(self, scores):
        self._scores = scores

    def predict(self, pairs):
        return self._scores[: len(pairs)]


def _video_window(text, rrf=0.1):
    return {"video_id": "yt_a", "t": 0.0, "rrf": rrf, "modalities": {"text"},
            "frame": None, "text": {"text": text}}


def _doc_window(text, rrf=0.1):
    return {"video_id": None, "t": 0.0, "rrf": rrf, "modalities": {"text"},
            "frame": None, "text": {"text": text}}


def _frame_only_window(rrf=0.1):
    return {"video_id": "yt_b", "t": 5.0, "rrf": rrf, "modalities": {"frame"},
            "frame": {"idx": 1}, "text": None}


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    windows = [_video_window("mostly irrelevant filler text", rrf=0.5),
              _doc_window("the exact answer to the question", rrf=0.1)]
    # cross-encoder scores: first window low, second window high -> should swap
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([0.1, 0.9]))
    out = rerank.rerank("the question", windows)
    assert out[0]["text"]["text"] == "the exact answer to the question"
    assert out[1]["text"]["text"] == "mostly irrelevant filler text"


def test_rerank_frame_only_window_keeps_its_fused_rank_position(monkeypatch):
    """Component 59: a frame-only window that FUSION ranked first stays first —
    the cross-encoder has nothing to read for it, so it has no basis to demote
    it below text it also didn't compare against."""
    text_w = _video_window("some transcript text")
    frame_w = _frame_only_window()
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([0.5]))
    out = rerank.rerank("q", [frame_w, text_w])  # frame_w fused-ranked first
    assert out[0] is frame_w  # keeps its fused position
    assert out[1] is text_w


def test_rerank_frame_only_window_can_outrank_a_weaker_text_window(monkeypatch):
    """The discriminating fairness eval: text windows re-sort by cross-encoder
    score AMONG the positions text windows collectively held; the frame-only
    window holds its own fused position between them."""
    text_low = _doc_window("mostly irrelevant filler")
    frame_w = _frame_only_window()
    text_high = _doc_window("the exact answer")
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([0.1, 0.9]))
    out = rerank.rerank("q", [text_low, frame_w, text_high])
    assert out[0] is text_high  # best text takes the best text position
    assert out[1] is frame_w    # fused position 2 stays a frame position
    assert out[2] is text_low


def test_rerank_never_drops_or_adds_windows(monkeypatch):
    windows = [_doc_window("a"), _frame_only_window(), _doc_window("b"),
               _frame_only_window(), _video_window("c")]
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([0.3, 0.9, 0.6]))
    out = rerank.rerank("q", windows)
    assert len(out) == len(windows)
    assert {id(w) for w in out} == {id(w) for w in windows}


def test_rerank_all_frame_only_returns_windows_unchanged(monkeypatch):
    w1, w2 = _frame_only_window(rrf=0.3), _frame_only_window(rrf=0.1)

    def _boom():
        raise AssertionError("cross-encoder should never be invoked with nothing to score")
    monkeypatch.setattr(rerank, "_model", _boom)
    out = rerank.rerank("q", [w1, w2])
    assert out == [w1, w2]


def test_rerank_empty_windows_returns_empty(monkeypatch):
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([]))
    assert rerank.rerank("q", []) == []


def test_rerank_multiple_frame_only_windows_preserve_relative_order(monkeypatch):
    text_w = _doc_window("relevant text")
    frame_1, frame_2 = _frame_only_window(rrf=0.9), _frame_only_window(rrf=0.8)
    monkeypatch.setattr(rerank, "_model", lambda: _FakeCrossEncoder([0.5]))
    out = rerank.rerank("q", [frame_1, frame_2, text_w])
    assert out[0] is frame_1  # fused positions 1-2 stay frame positions,
    assert out[1] is frame_2  # in their original relative order
    assert out[2] is text_w


# ── Real cross-encoder: proves it actually favors the relevant passage ─────

def test_rerank_real_model_favors_the_relevant_passage():
    windows = [
        _doc_window("Large language models are trained on massive text corpora "
                   "using self-supervised learning objectives."),
        _doc_window("The rank hyperparameter r=8 was used for most LoRA "
                   "experiments, balancing parameter count and accuracy."),
    ]
    out = rerank.rerank("what value of r was used for the LoRA rank hyperparameter", windows)
    assert "r=8" in out[0]["text"]["text"]
