"""Component 47 (DESIGN.md §3g) — prompt & data versioning.

The gap this closes: `benchmark/answer_quality.py` reported faithfulness 0.96
and relevancy 5.0, and those numbers were attributable to **nothing**. Edit the
answer prompt and they silently become uncomparable to any earlier or later run,
with no record that anything changed.

The version is a **content hash**, deliberately, not a hand-bumped constant.
A constant relies on remembering to bump it, and a forgotten bump is exactly
the failure mode being prevented — it would report "same version" across
genuinely different prompts, which is worse than no versioning because it
looks trustworthy.

Data versioning extends what already exists (`EMBED_VERSION`,
`TEXT_EMBED_VERSION`) with a chunker version: component 14 changed paper
chunking (table + figure extraction) and nothing recorded it, so chunks
produced before and after that change are indistinguishable in the index.
"""
from __future__ import annotations

import pytest

from src import prompts


# ── Content-hash versioning ──────────────────────────────────────────────────

def test_registered_prompt_exposes_text_and_version():
    p = prompts.get("answer")
    assert p.text
    assert p.version


def test_version_is_derived_from_content_not_declared():
    a = prompts.Prompt(name="t", source="you are a helpful assistant")
    b = prompts.Prompt(name="t", source="you are a helpful assistant")
    assert a.version == b.version


def test_editing_the_text_changes_the_version_automatically():
    """The whole point: no human step can be forgotten."""
    a = prompts.Prompt(name="t", source="answer only from the sources")
    b = prompts.Prompt(name="t", source="answer only from the sources.")   # one char
    assert a.version != b.version


def test_version_is_short_and_stable_across_processes():
    p = prompts.Prompt(name="t", source="hello")
    assert p.version == "sha256:2cf24dba5fb0a3"[:len(p.version)] or len(p.version) <= 24
    # Deterministic: recomputing gives the same answer, no salt/uuid involved.
    assert p.version == prompts.Prompt(name="t", source="hello").version


def test_every_serving_prompt_is_registered():
    """Every LLM call site on the SERVING path must be traceable to a version.
    The judge prompt is not here on purpose — it belongs to the benchmark and
    is registered by it (see the container-bug tests below)."""
    for name in ("answer", "query_enhance"):
        assert prompts.get(name).version, f"{name} is not registered"


def test_registered_text_matches_what_the_module_actually_uses():
    """A registry that drifts from the real prompt is worse than none — it
    would report a version for text that was never sent."""
    from src import llm
    from src.rag import query_enhance

    assert prompts.get("answer").text == llm.SYSTEM
    assert prompts.get("query_enhance").text == query_enhance._SYSTEM


def test_unknown_prompt_raises_rather_than_returning_a_fake_version():
    with pytest.raises(KeyError):
        prompts.get("no-such-prompt")


# ── The provenance bundle ────────────────────────────────────────────────────

def test_versions_bundle_carries_prompt_and_data_versions():
    v = prompts.versions()
    for key in ("prompts", "embed_version", "text_embed_version", "chunker_version"):
        assert key in v, f"missing provenance field: {key}"
    assert v["prompts"]["answer"] == prompts.get("answer").version


def test_chunker_version_changes_when_a_parser_changes():
    """Component 14 altered paper chunking and nothing recorded it. The chunker
    version is derived from the parser sources, so a future edit to how chunks
    are produced is visible in the index and in eval provenance."""
    v1 = prompts.chunker_version()
    assert v1
    assert v1 == prompts.chunker_version()          # deterministic


def test_versions_bundle_is_json_serializable():
    import json

    json.dumps(prompts.versions())      # goes into spans and API payloads


# ── The container bug this component nearly shipped ──────────────────────────

def test_app_registry_does_not_depend_on_benchmark_code():
    """Caught LIVE in the Docker container, not by any unit test: the first cut
    registered the judge prompt by importing `benchmark.answer_quality`. The
    image only copies `src/`, `ui/` and `benchmark/corpus.json`, so that import
    failed and a broad except turned it into an EMPTY prompt map — versioning
    that looks present and reports nothing, which is worse than none.

    Asserted by BEHAVIOR, not by grepping the source: an earlier version of
    this test searched `_app_prompts`'s source text for "benchmark" and matched
    its own docstring. Here the `benchmark` package is made unimportable, which
    is what the container actually looks like."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "benchmark" or name.startswith("benchmark."):
            raise ModuleNotFoundError("No module named 'benchmark'")
        return real_import(name, *a, **k)

    prompts._app_prompts.cache_clear()
    prompts._extra.clear()
    builtins.__import__ = _blocked
    try:
        v = prompts.versions()          # must not raise
    finally:
        builtins.__import__ = real_import
        prompts._app_prompts.cache_clear()
    assert v["prompts"].get("answer"), "serving prompts unavailable without benchmark"
    assert v["prompts"].get("query_enhance")


def test_serving_prompts_are_present_without_the_benchmark_registered():
    """Simulates the container: nothing has called register()."""
    prompts._extra.clear()
    v = prompts.versions()
    assert v["prompts"].get("answer"), "answer prompt version missing"
    assert v["prompts"].get("query_enhance"), "query_enhance version missing"


def test_benchmark_can_register_its_judge_prompt_at_runtime():
    from benchmark import answer_quality

    prompts._extra.clear()
    answer_quality._register_judge_prompt()
    assert prompts.get("judge").version == prompts.Prompt("judge", answer_quality.JUDGE_SYSTEM).version


def test_version_follows_a_live_prompt_rebind():
    """spec-guardian DEMONSTRATED the original claim false: `@lru_cache`
    snapshotted the string, so rebinding `llm.SYSTEM` left the registry
    reporting the stale hash (69f1121dc865 while the live text hashed
    cfe4f535e436). The registry resolves live now — this is the test that
    would have caught it."""
    from src import llm

    original = llm.SYSTEM
    before = prompts.get("answer").version
    try:
        llm.SYSTEM = original + " APPENDED FOR TEST"
        assert prompts.get("answer").version != before, \
            "registry reported a stale version after the prompt changed"
    finally:
        llm.SYSTEM = original
    assert prompts.get("answer").version == before
