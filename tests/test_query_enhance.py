"""Component 17 (DESIGN.md §3b) — query enhancement (decomposition +
expansion). Opt-in (QUERY_ENHANCEMENT_ENABLED, default false).

Pure logic only, no network: llm.env_config()/llm.complete() are
monkeypatched — a real check needs a live LLM, exactly like
answer_quality.py's judge calls."""
from __future__ import annotations

from src.llm import LLMConfig
from src.rag import query_enhance as qe


# ── _parse: pure JSON parsing ────────────────────────────────────────────────

def test_parse_plain_json():
    assert qe._parse('{"queries": ["a", "b"]}') == ["a", "b"]


def test_parse_strips_markdown_code_fence():
    raw = '```json\n{"queries": ["a"]}\n```'
    assert qe._parse(raw) == ["a"]


def test_parse_invalid_json_returns_none():
    assert qe._parse("not json") is None


def test_parse_missing_queries_key_returns_none():
    assert qe._parse('{"other": ["a"]}') is None


def test_parse_non_list_queries_returns_none():
    assert qe._parse('{"queries": "a"}') is None


def test_parse_empty_list_returns_none():
    assert qe._parse('{"queries": []}') is None


def test_parse_strips_blank_entries_and_whitespace():
    assert qe._parse('{"queries": [" a ", "", "  ", "b"]}') == ["a", "b"]


def test_parse_caps_at_max_queries():
    assert qe._parse('{"queries": ["a", "b", "c", "d", "e"]}') == ["a", "b", "c"]


# ── enhance_query: best-effort, never blocks retrieval ──────────────────────

def test_enhance_query_no_llm_configured_returns_question_unchanged(monkeypatch):
    monkeypatch.setattr(qe.llm, "env_config", lambda: None)
    assert qe.enhance_query("how does attention work") == ["how does attention work"]


def test_enhance_query_returns_parsed_queries_on_success(monkeypatch):
    monkeypatch.setattr(qe.llm, "env_config", lambda: LLMConfig(model="gpt-4o-mini"))
    monkeypatch.setattr(qe.llm, "complete",
                        lambda system, prompt, cfg: '{"queries": ["sub one", "sub two"]}')
    assert qe.enhance_query("how does X combine A and B") == ["sub one", "sub two"]


def test_enhance_query_llm_call_failure_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(qe.llm, "env_config", lambda: LLMConfig(model="gpt-4o-mini"))

    def _boom(system, prompt, cfg):
        raise RuntimeError("network error")
    monkeypatch.setattr(qe.llm, "complete", _boom)
    assert qe.enhance_query("q") == ["q"]


def test_enhance_query_unparseable_response_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(qe.llm, "env_config", lambda: LLMConfig(model="gpt-4o-mini"))
    monkeypatch.setattr(qe.llm, "complete", lambda system, prompt, cfg: "garbage, not json")
    assert qe.enhance_query("q") == ["q"]
