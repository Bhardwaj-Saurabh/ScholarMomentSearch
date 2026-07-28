"""Component 13 (DESIGN.md §3a) — answer relevancy + faithfulness (LLM-judge).

Like bench.py, this is fundamentally a live-stack + live-judge-LLM black box
(it calls /ask_stream, then an LLM judge, both over HTTP) — untestable without
a running server AND a configured judge model. This file unit-tests every
PURE piece: judge-prompt construction, judge-response parsing, and score
aggregation. The HTTP-calling glue (measure_answer_quality) is implemented
for real but is integration-level; EVIDENCE.md discloses whether a live run
happened rather than claiming it's green without one.
"""
from __future__ import annotations

from benchmark import answer_quality as aq


def test_build_judge_prompt_includes_question_answer_and_numbered_sources():
    citations = [
        {"n": 1, "title": "Attention (Vaswani et al. 2017)", "text": "Self-attention relates every token."},
        {"n": 2, "title": "BERT (Devlin et al. 2019)", "transcript": "BERT uses masked language modeling."},
    ]
    prompt = aq._build_judge_prompt("How does attention work?",
                                    "It relates tokens directly [1].", citations)
    assert "How does attention work?" in prompt
    assert "It relates tokens directly [1]." in prompt
    assert "[1] Attention (Vaswani et al. 2017): Self-attention relates every token." in prompt
    assert "[2] BERT (Devlin et al. 2019): BERT uses masked language modeling." in prompt


def test_build_judge_prompt_handles_a_citation_with_no_text_or_transcript():
    citations = [{"n": 1, "title": "Some Deck"}]
    prompt = aq._build_judge_prompt("q", "a [1].", citations)
    assert "[1] Some Deck:" in prompt


def test_parse_judge_response_plain_json():
    raw = '{"relevancy": 4, "citations_checked": [{"n": 1, "supported": true}]}'
    assert aq._parse_judge_response(raw) == {
        "relevancy": 4, "citations_checked": [{"n": 1, "supported": True}]}


def test_parse_judge_response_strips_markdown_code_fence():
    raw = '```json\n{"relevancy": 3, "citations_checked": []}\n```'
    assert aq._parse_judge_response(raw) == {"relevancy": 3, "citations_checked": []}


def test_parse_judge_response_invalid_json_returns_none():
    assert aq._parse_judge_response("not json at all") is None


def test_parse_judge_response_missing_keys_returns_none():
    assert aq._parse_judge_response('{"relevancy": 4}') is None


def test_aggregate_computes_mean_relevancy_and_faithfulness_rate():
    judged = [
        {"relevancy": 4, "citations_checked": [{"n": 1, "supported": True}, {"n": 2, "supported": False}]},
        {"relevancy": 2, "citations_checked": [{"n": 1, "supported": True}]},
    ]
    result = aq._aggregate(judged)
    assert result["mean_relevancy"] == 3.0
    assert result["faithfulness_rate"] == 2 / 3
    assert result["queries_judged"] == 2
    assert result["citations_checked"] == 3


def test_aggregate_skips_failed_judge_calls():
    judged = [None, {"relevancy": 5, "citations_checked": [{"n": 1, "supported": True}]}]
    result = aq._aggregate(judged)
    assert result["mean_relevancy"] == 5.0
    assert result["queries_judged"] == 1


def test_aggregate_no_citations_checked_gives_zero_faithfulness_not_a_crash():
    judged = [{"relevancy": 3, "citations_checked": []}]
    result = aq._aggregate(judged)
    assert result["faithfulness_rate"] == 0.0
    assert result["mean_relevancy"] == 3.0


def test_aggregate_empty_list_is_zero_not_a_crash():
    assert aq._aggregate([]) == {"mean_relevancy": 0.0, "faithfulness_rate": 0.0,
                                 "queries_judged": 0, "citations_checked": 0}
