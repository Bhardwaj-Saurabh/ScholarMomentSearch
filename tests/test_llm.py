"""Grounding hardening (Part 0 finding) — AGENTS.md non-negotiable #5:
"Grounded citations only... Empty retrieval -> empty results, not a fabricated
one." The grounding-auditor agent found two real violations against the live
stack: (1) a query naming a nonexistent "Mamba paper" got a confidently
fabricated answer built on an unrelated real citation instead of abstaining;
(2) a GPT-3 benchmark question got a real, correctly-numbered citation but a
statistic that doesn't match what that citation's text actually says.

Root cause: the LLM never saw WHICH source (video/paper/deck title) each
numbered moment came from — only a bare timestamp/locator and excerpt text —
so it had no structural signal to check a named-source question ("the Mamba
paper") against what's actually cited. src/rag/search.py's citations already
carry a `title` field (used by the UI); this wires it through to the LLM and
strengthens the system prompt's grounding rules to use it.

Pure logic only: _build_moments()/_label() with no network calls (frame
fetch is skipped entirely for citations with no video_id/idx, i.e. any
document citation), and prompt-text guard tests. LLM synthesis quality itself
was verified live against the real stack (see EVIDENCE.md) — that is not
something a unit test can assert on non-deterministic model output.
"""
from __future__ import annotations

from src import llm, metrics
from src.rag import search


def test_build_moments_includes_the_source_title():
    """Without a source title, the LLM has no way to check a question naming
    a specific paper/video/deck against what's actually been retrieved —
    this is the root cause the Mamba-paper fabrication traced back to."""
    citations = [
        {"n": 1, "kind": "paper", "title": "Attention Is All You Need",
         "locator": {"page": 6}, "text": "excerpt text"},
        {"n": 2, "kind": "video", "title": "3Blue1Brown — Attention",
         "video_id": "yt_x", "idx": None, "timestamp": "01:00",
         "transcript": "said text"},
    ]
    moments = search._build_moments("default", citations)
    assert moments[0]["source"] == "Attention Is All You Need"
    assert moments[1]["source"] == "3Blue1Brown — Attention"


def test_label_includes_the_source_title():
    line = llm._label(1, {"source": "Attention Is All You Need",
                          "timestamp": "page 6", "transcript": "excerpt"})
    assert "Attention Is All You Need" in line


def test_label_does_not_crash_without_a_source():
    line = llm._label(1, {"timestamp": "00:10", "transcript": "said"})
    assert "[1]" in line


def test_system_prompt_forbids_attributing_content_to_an_uncited_source():
    """Regression guard: the exact failure mode was answering about a named
    source ('the Mamba paper') using a moment from a DIFFERENT, unrelated
    source. Superseded by the stronger "cite by [n] only, never name a
    source" rule (see test_system_prompt_forbids_naming_sources_in_prose) —
    a prompt-only "cross-check the named source" instruction was tried
    first and found NOT to generalize under adversarial re-check (the CLIP/
    LoRA case). Still lock in that moments carry a source title at all,
    since src/rag/search.py's mechanical backstop depends on it."""
    assert "source title" in llm.SYSTEM


def test_system_prompt_requires_genuine_topical_relevance_not_adjacency():
    """Regression guard for the abstain-gate weakness: 'topically related'
    (e.g. both are ML papers) must not count as 'relevant' on its own."""
    assert "topically adjacent" in llm.SYSTEM or "adjacent" in llm.SYSTEM


def test_system_prompt_still_allows_answering_from_a_genuine_partial_match():
    """Don't overcorrect into over-abstaining: a real, on-topic partial match
    must still get answered (this is what protects recall_at_10)."""
    assert "don't refuse" in llm.SYSTEM or "do not refuse" in llm.SYSTEM


def test_system_prompt_forbids_naming_sources_in_prose():
    """A second adversarial re-check found the source-title fix above
    doesn't generalize: 'the CLIP paper recommends...' (0 CLIP citations
    retrieved, all 6 were LoRA content) reproduced the identical bug under a
    different name. Relying on the model to correctly cross-check a named
    source against what it actually has is unreliable — the more mechanical,
    easier-to-follow constraint is to forbid naming sources in prose at all,
    citing by [n] only (src/rag/search.py's _check_named_source_attribution
    is the code-level backstop for when even this doesn't hold)."""
    assert "only by" in llm.SYSTEM.lower()


# ── Component 18 (DESIGN.md §3c): token usage capture, previously discarded ──

class _FakeUsage:
    def __init__(self, a, b):
        self.prompt_tokens, self.completion_tokens = a, b
        self.input_tokens, self.output_tokens = a, b


class _FakeMessage:
    def __init__(self, text):
        self.content = text


class _FakeChoice:
    def __init__(self, text):
        self.message = _FakeMessage(text)


class _FakeOpenAIResponse:
    def __init__(self, text, in_tok, out_tok):
        self.choices = [_FakeChoice(text)]
        self.usage = _FakeUsage(in_tok, out_tok)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeOpenAIClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


def _patch_openai(monkeypatch, text, in_tok, out_tok):
    response = _FakeOpenAIResponse(text, in_tok, out_tok)
    # Component 58 made clients process-cached; without clearing, the first
    # test's fake would be served to every later test with the same config.
    llm._openai_client.cache_clear()
    monkeypatch.setattr("openai.OpenAI", lambda **k: _FakeOpenAIClient(response))


def test_answer_openai_records_llm_usage_as_answer_kind(monkeypatch):
    metrics.reset()
    # Large enough that the estimated cost survives snapshot()'s 4-decimal
    # rounding (a real small call, e.g. 123/45 tokens, correctly rounds to
    # $0.0000 -- that's not a bug, it matches the product's own "$0.0000"
    # empty-state display; this test needs a volume that's visibly non-zero).
    _patch_openai(monkeypatch, "a real answer [1].", 100_000, 50_000)

    cfg = llm.LLMConfig(model="gpt-4o-mini")
    out = llm.answer("q", [], cfg)
    assert out == "a real answer [1]."
    snap = metrics.snapshot()
    assert snap["input_tokens"] == 100_000
    assert snap["output_tokens"] == 50_000
    assert snap["llm_answers"] == 1
    assert snap["cost_usd"] > 0


def _tiny_jpeg() -> bytes:
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def test_caption_image_records_llm_usage_as_caption_kind_not_answer(monkeypatch):
    metrics.reset()
    _patch_openai(monkeypatch, "a caption.", 10, 5)

    cfg = llm.LLMConfig(model="gpt-4o-mini")
    llm.caption_image(_tiny_jpeg(), cfg)
    snap = metrics.snapshot()
    assert snap["input_tokens"] == 10
    assert snap["llm_answers"] == 0  # captions never count as an "LLM answer"


def test_complete_records_llm_usage_as_complete_kind(monkeypatch):
    metrics.reset()
    _patch_openai(monkeypatch, '{"queries": ["q"]}', 20, 8)

    cfg = llm.LLMConfig(model="gpt-4o-mini")
    out = llm.complete("system", "prompt", cfg)
    assert out == '{"queries": ["q"]}'
    snap = metrics.snapshot()
    assert snap["input_tokens"] == 20
    assert snap["output_tokens"] == 8
    assert snap["llm_answers"] == 0
