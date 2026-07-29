"""Component 45 (DESIGN.md §3g) — spans on the RAG read path.

The point is NOT timing. `src/metrics.py` already reports that /ask_stream
averages 13.6s. What it cannot answer is "why was THIS answer wrong", and that
question is answered by **decisions**: which candidates came back at what
scores, how the cross-encoder reordered them, which score the confidence gate
saw and whether it abstained, and which grounding backstop stripped what.

So these tests assert the presence and content of decision attributes, not
durations. A span tree that records only latency would pass a naive test and
be useless in practice.

Retrieval and the LLM are stubbed — this is about instrumentation, and a real
ask needs Qdrant plus a paid model.
"""
from __future__ import annotations

import numpy as np
import pytest

from src import config, tracing
from src.rag import search as rag_search


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    s = tracing.MemoryBackend()
    tracing.set_backends([s])
    yield s
    tracing.reset()


def _spans(sink):
    return {r["name"]: r for r in sink.spans}


@pytest.fixture
def stub_retrieval(monkeypatch):
    """A deterministic two-hit corpus: one video frame, one paper chunk."""
    monkeypatch.setattr(config, "ENABLE_TRANSCRIPT", True)
    monkeypatch.setattr(config, "QUERY_ENHANCEMENT_ENABLED", False)
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search, "embed_query", lambda q: np.zeros(4, dtype=np.float32))

    vhits = [{"video_id": "v1", "idx": 3, "ms": 1000, "score": 0.41}]
    thits = [{"source_id": "doc_a", "kind": "paper", "page": 7,
              "text": "self-attention connects all positions", "score": 0.64}]

    monkeypatch.setattr(rag_search.vector_store, "search", lambda *a, **k: list(vhits))

    def _search_text(vec, uid, *, top_k, video_id=None, video_ids=None, query_text=None):
        return list(thits)[:top_k]

    monkeypatch.setattr(rag_search.vector_store, "search_text", _search_text)
    monkeypatch.setattr(rag_search.db, "videos_by_ids", lambda ids: {})
    monkeypatch.setattr(rag_search.db, "documents_by_ids",
                        lambda ids: {"doc_a": {"title": "Attention Is All You Need"}})
    monkeypatch.setattr(rag_search.db, "list_sources", lambda uid: [])
    monkeypatch.setattr(rag_search.storage, "get_bytes", lambda k: None)
    return vhits, thits


# ── The trace exists and has the right shape ─────────────────────────────────

def test_ask_emits_a_root_span_with_question_and_tenant(sink, stub_retrieval, monkeypatch):
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    rag_search.ask("how does attention avoid recurrence?", "u_test")
    s = _spans(sink)
    assert "ask" in s
    assert s["ask"]["attrs"]["question"] == "how does attention avoid recurrence?"
    assert s["ask"]["attrs"]["tenant"] == "u_test"
    assert s["ask"]["parent"] is None


def test_retrieval_steps_each_get_their_own_span(sink, stub_retrieval, monkeypatch):
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    rag_search.ask("q", "u_test")
    s = _spans(sink)
    for name in ("retrieve", "search_visual", "search_text", "fuse"):
        assert name in s, f"missing span: {name}"
    # One trace, and retrieval hangs off the root.
    assert len({r["trace_id"] for r in sink.spans}) == 1
    assert s["retrieve"]["parent"] == s["ask"]["id"]


# ── Decision attributes: the actual reason this component exists ─────────────

def test_search_spans_record_candidate_counts_and_best_score(sink, stub_retrieval, monkeypatch):
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    rag_search.ask("q", "u_test")
    s = _spans(sink)
    assert s["search_text"]["attrs"]["candidates"] == 1
    assert s["search_text"]["attrs"]["best_score"] == pytest.approx(0.64)
    assert s["search_visual"]["attrs"]["best_score"] == pytest.approx(0.41)


def test_confidence_gate_span_records_the_scores_it_judged(sink, stub_retrieval, monkeypatch):
    """When an answer is wrong, the first question is 'should it have
    abstained?'. That needs the two branch scores AND the thresholds they were
    compared against, in the trace."""
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    rag_search.ask("q", "u_test")
    gate = _spans(sink)["confidence_gate"]
    assert gate["attrs"]["best_visual"] == pytest.approx(0.41)
    assert gate["attrs"]["best_text"] == pytest.approx(0.64)
    assert "visual_threshold" in gate["attrs"]
    assert "text_threshold" in gate["attrs"]
    assert gate["attrs"]["abstained"] is False


def test_an_abstaining_ask_still_emits_a_complete_trace(sink, stub_retrieval, monkeypatch):
    """The abstain paths are early returns. A trace that vanishes exactly when
    the system declined to answer would be missing the most interesting case."""
    monkeypatch.setattr(config, "CONFIDENCE_THRESHOLD", 0.99)
    monkeypatch.setattr(rag_search, "CONFIDENCE_THRESHOLD", 0.99)
    monkeypatch.setattr(rag_search, "TEXT_CONFIDENCE_THRESHOLD", 0.99)
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    result = rag_search.ask("q", "u_test")
    assert result["abstained"] is True
    s = _spans(sink)
    assert "ask" in s and "confidence_gate" in s
    assert s["confidence_gate"]["attrs"]["abstained"] is True
    assert s["ask"]["attrs"]["abstained"] is True


def test_empty_retrieval_still_emits_the_root_span(sink, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TRANSCRIPT", False)
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search.vector_store, "search", lambda *a, **k: [])
    monkeypatch.setattr(rag_search.db, "videos_by_ids", lambda ids: {})
    monkeypatch.setattr(rag_search.db, "documents_by_ids", lambda ids: {})
    rag_search.ask("nothing matches this", "u_test")
    s = _spans(sink)
    assert "ask" in s
    assert s["ask"]["attrs"]["citations"] == 0


def test_rerank_span_records_that_it_reordered(sink, stub_retrieval, monkeypatch):
    """The reranker silently moving the right chunk down is a real failure mode
    (component 16). The trace has to show the before/after ordering."""
    monkeypatch.setattr(config, "RERANK_ENABLED", True)
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))

    def _reversing_rerank(question, windows):
        return list(reversed(windows))

    import src.rag.rerank as rr_mod
    monkeypatch.setattr(rr_mod, "rerank", _reversing_rerank)
    rag_search.ask("q", "u_test")
    rr = _spans(sink)["rerank"]
    assert rr["attrs"]["enabled"] is True
    assert rr["attrs"]["reordered"] is True
    assert rr["attrs"]["windows_in"] == rr["attrs"]["windows_out"]


def test_llm_answer_span_records_model_and_grounding_outcome(sink, stub_retrieval, monkeypatch):
    from src.llm import LLMConfig

    cfg = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="x",
                    base_url="", max_tokens=256)
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (cfg, "server"))
    monkeypatch.setattr(rag_search.llm, "answer", lambda *a, **k: "Attention avoids recurrence [1].")
    rag_search.ask("q", "u_test")
    s = _spans(sink)
    assert s["llm_answer"]["attrs"]["model"] == "gpt-4o-mini"
    assert s["llm_answer"]["attrs"]["llm_source"] == "server"
    assert "grounding_check" in s


# ── Instrumentation must not change behavior ─────────────────────────────────

def test_results_are_identical_with_tracing_off(stub_retrieval, monkeypatch):
    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(config, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    tracing.reset()
    off = rag_search.ask("q", "u_test")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([tracing.MemoryBackend()])
    on = rag_search.ask("q", "u_test")
    tracing.reset()
    assert off["answer"] == on["answer"]
    assert off["citations"] == on["citations"]


def test_a_failing_backend_does_not_break_ask(stub_retrieval, monkeypatch):
    class _Exploding:
        def start(self, *a, **k): raise RuntimeError("down")
        def end(self, *a, **k): raise RuntimeError("down")

    monkeypatch.setattr(rag_search, "resolve_llm", lambda uid: (None, "none"))
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    tracing.reset()
    tracing.set_backends([_Exploding()])
    try:
        result = rag_search.ask("q", "u_test")      # must not raise
        assert result["citations"]
    finally:
        tracing.reset()
