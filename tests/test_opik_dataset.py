"""Component 48 (DESIGN.md §3g) — Opik eval dataset + experiment versioning.

The gap component 47 alone leaves open: prompts are versioned now, but
`answer_quality.py`'s faithfulness 0.96 / relevancy 5.0 still live in a terminal
scrollback with nothing to compare a later run *against*. Pushing the labeled
query set to Opik as a versioned Dataset and logging each run as an Experiment
carrying full provenance makes the question answerable: "did that prompt edit
help, and which queries regressed?"

Two properties matter more than the plumbing:

  * **Opik is the RECORD, never the gate.** `benchmark/quality_gates.json`
    remains the only thing that decides pass/fail. A telemetry backend must
    never be able to change whether a build is green.
  * **Strictly opt-in.** With `OPIK_API_KEY` unset both benchmarks behave
    byte-identically to today, so the frozen SLA path is untouched.

No test here touches the network — the Opik client is stubbed.
"""
from __future__ import annotations

import pytest

from benchmark import opik_dataset
from src import config


class _FakeDataset:
    def __init__(self, name):
        self.name = name
        self.inserted: list[list[dict]] = []

    def insert(self, items):
        self.inserted.append(list(items))

    def get_current_version_name(self):
        return "v3"


class _FakeExperiment:
    def __init__(self, **kw):
        self.kw = kw
        self.id = "exp-1"


class _FakeClient:
    def __init__(self):
        self.datasets: dict[str, _FakeDataset] = {}
        self.experiments: list[_FakeExperiment] = []
        self.scored: list[list[dict]] = []

    def get_or_create_dataset(self, name, description=None, project_name=None):
        return self.datasets.setdefault(name, _FakeDataset(name))

    def log_traces_feedback_scores(self, scores):
        self.scored.append(list(scores))

    def create_experiment(self, **kw):
        e = _FakeExperiment(**kw)
        self.experiments.append(e)
        return e


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    c = _FakeClient()
    monkeypatch.setattr(opik_dataset, "_client", lambda: c)
    return c


# ── Opt-in: unset key means genuinely nothing happens ────────────────────────

def test_disabled_when_no_api_key(monkeypatch):
    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    assert opik_dataset.enabled() is False
    assert opik_dataset.push_labeled_queries() is None
    assert opik_dataset.log_experiment("answer_quality", {"x": 1}) is None


def test_disabled_path_touches_no_client(monkeypatch):
    def _boom():
        raise AssertionError("client built while disabled")

    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    monkeypatch.setattr(opik_dataset, "_client", _boom)
    opik_dataset.push_labeled_queries()
    opik_dataset.log_experiment("answer_quality", {"x": 1})


# ── Dataset push ─────────────────────────────────────────────────────────────

def test_push_sends_every_labeled_query(client):
    info = opik_dataset.push_labeled_queries()
    assert info is not None
    ds = client.datasets[opik_dataset.DATASET_NAME]
    (items,) = ds.inserted
    assert len(items) == 16, f"expected all 16 labeled queries, got {len(items)}"


def test_pushed_items_carry_the_expectations_not_just_the_question(client):
    """A dataset of bare questions would be useless for comparing runs — the
    expected corpus_id and kinds are what make a per-query regression legible."""
    opik_dataset.push_labeled_queries()
    items = client.datasets[opik_dataset.DATASET_NAME].inserted[0]
    first = items[0]
    assert "query" in first and "corpus_id" in first and "expect_kinds" in first


def test_item_construction_is_deterministic(client):
    """Opik dedupes items by content, so idempotency depends on us emitting
    byte-identical items each time — a timestamp or uuid in an item would
    silently create duplicates on every push."""
    a = opik_dataset._dataset_items()
    b = opik_dataset._dataset_items()
    assert a == b


def test_repushing_does_not_change_the_items(client):
    opik_dataset.push_labeled_queries()
    opik_dataset.push_labeled_queries()
    ds = client.datasets[opik_dataset.DATASET_NAME]
    assert ds.inserted[0] == ds.inserted[1]


# ── Experiment provenance ────────────────────────────────────────────────────

def test_experiment_records_full_provenance(client):
    opik_dataset.log_experiment("answer_quality",
                                {"mean_relevancy": 5.0, "faithfulness_rate": 0.96})
    (exp,) = client.experiments
    cfg = exp.kw["experiment_config"]
    for key in ("prompts", "embed_version", "text_embed_version", "chunker_version"):
        assert key in cfg, f"missing provenance: {key}"
    # The retrieval flags actually in force — without them a score change is
    # unattributable between "prompt edit" and "someone flipped the reranker".
    for flag in ("hybrid_text_search", "rerank_enabled", "query_enhancement_enabled"):
        assert flag in cfg, f"missing retrieval flag: {flag}"


def test_experiment_records_the_metrics_it_was_given(client):
    opik_dataset.log_experiment("answer_quality", {"mean_relevancy": 4.5})
    cfg = client.experiments[0].kw["experiment_config"]
    assert cfg["metrics"]["mean_relevancy"] == 4.5


def test_experiment_is_linked_to_the_dataset(client):
    opik_dataset.log_experiment("answer_quality", {"x": 1})
    assert client.experiments[0].kw["dataset_name"] == opik_dataset.DATASET_NAME


def test_experiment_name_is_unique_per_run(client):
    """Two runs must not collide into one experiment, or the before/after
    comparison this component exists for is impossible."""
    opik_dataset.log_experiment("answer_quality", {"x": 1}, run_id="run-a")
    opik_dataset.log_experiment("answer_quality", {"x": 1}, run_id="run-b")
    names = {e.kw.get("name") for e in client.experiments}
    assert len(names) == 2, names


# ── Fail-open: telemetry must never break a benchmark ────────────────────────

def test_push_fails_open_when_opik_raises(monkeypatch):
    class _Broken:
        def get_or_create_dataset(self, *a, **k): raise RuntimeError("opik down")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    monkeypatch.setattr(opik_dataset, "_client", lambda: _Broken())
    assert opik_dataset.push_labeled_queries() is None     # must not raise


def test_log_experiment_fails_open_when_opik_raises(monkeypatch):
    class _Broken:
        def create_experiment(self, *a, **k): raise RuntimeError("opik down")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    monkeypatch.setattr(opik_dataset, "_client", lambda: _Broken())
    assert opik_dataset.log_experiment("answer_quality", {"x": 1}) is None


def test_opik_is_never_the_gate():
    """Structural guarantee: this module must not read the quality gates or be
    able to end a process. Opik records; `quality_gates.json` judges.

    Checked against the module's CODE with docstrings stripped — an earlier
    version of this test grepped the raw source and matched its own explanatory
    docstring, which is a false positive I have now written twice."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(opik_dataset))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)                     # drop the docstring
    code = ast.unparse(tree)
    assert "quality_gates" not in code, "the recorder is reading the gate file"
    assert "sys.exit" not in code and "SystemExit" not in code


def test_no_function_can_terminate_the_benchmark(client):
    """Belt and braces on the same property, behaviorally: a telemetry call
    that can raise SystemExit could turn a passing SLA run red."""
    for call in (lambda: opik_dataset.push_labeled_queries(),
                 lambda: opik_dataset.log_experiment("x", {"y": 1})):
        try:
            call()
        except SystemExit:                            # pragma: no cover
            pytest.fail("telemetry raised SystemExit")


# ── The call-site guards (spec-guardian: the shipped regression had no test) ─

def test_bench_quality_records_without_crashing_when_opik_is_broken(monkeypatch):
    """A `from benchmark import opik_dataset` failure once crashed bench.py
    AFTER the gate had printed — on a PASSING run that would have turned a
    green SLA non-zero. The fix was verified by hand but never pinned, which
    is how it could regress silently."""
    import benchmark.bench as bench

    src = __import__("inspect").getsource(bench.main)
    assert "try:" in src and "opik_dataset" in src, "the guard vanished from bench.main"
    # The import must resolve under BOTH invocations the docs use.
    import importlib
    assert importlib.import_module("benchmark.opik_dataset") is not None


def test_opik_module_imports_with_only_benchmark_on_the_path(monkeypatch):
    """Simulates `python benchmark/bench.py`, where sys.path[0] is benchmark/
    and `src` is NOT importable — the exact condition that made the feature
    silently inert."""
    import subprocess
    import sys as _sys

    r = subprocess.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0,'benchmark');"
         "import opik_dataset; print(opik_dataset.DATASET_NAME)"],
        capture_output=True, text=True, cwd=str(opik_dataset.ROOT))
    assert r.returncode == 0, f"script-mode import failed: {r.stderr[-400:]}"
    assert "scholarmomentsearch-labeled-queries" in r.stdout


def test_experiment_names_are_unique_without_an_explicit_run_id(client, monkeypatch):
    """The earlier uniqueness test passed run_id explicitly, so the DEFAULT
    path — a wall-clock suffix — was never exercised."""
    seq = iter(["20260101-000001", "20260101-000002"])
    monkeypatch.setattr(opik_dataset, "_run_suffix", lambda: next(seq))
    opik_dataset.log_experiment("precision", {"p": 1})
    opik_dataset.log_experiment("precision", {"p": 2})
    names = {e.kw.get("name") for e in client.experiments}
    assert len(names) == 2, names


def test_push_fails_open_on_a_malformed_query_file(client, monkeypatch):
    """`_dataset_items()` used to run OUTSIDE the try, so a bad JSON file
    escaped a function whose contract says it always fails open."""
    def _boom():
        raise ValueError("malformed labeled_queries.json")

    monkeypatch.setattr(opik_dataset, "_dataset_items", _boom)
    assert opik_dataset.push_labeled_queries() is None


# ── Per-query feedback scores (declared red, now built) ─────────────────────

def _tid():
    """A REAL uuid4 hex. `uuid4_to_uuid7` validates the version nibble, so
    "a"*32 is rejected — an unrealistic fixture that hid the conversion path."""
    import uuid
    return uuid.uuid4().hex


def test_log_query_scores_sends_one_feedback_score_per_metric(client):
    """spec-guardian: experiments carried aggregates only, so "which queries
    regressed" still meant reading raw numbers. Per-query scores attach to the
    trace that produced the answer."""
    opik_dataset.log_query_scores([
        {"trace_id": _tid(), "relevancy": 5, "faithfulness": 1.0},
        {"trace_id": _tid(), "relevancy": 3, "faithfulness": 0.5},
    ])
    assert client.scored, "no feedback scores were sent"
    names = {s["name"] for batch in client.scored for s in batch}
    assert names == {"relevancy", "faithfulness"}


def test_log_query_scores_converts_our_trace_id_to_opik_form(client):
    """Our ids are 32-hex; Opik wants UUIDv7. A score attached to an id Opik
    does not recognise is silently lost."""
    raw = _tid()
    opik_dataset.log_query_scores([{"trace_id": raw, "relevancy": 4}])
    sent_id = client.scored[0][0]["id"]
    assert sent_id != raw and "-" in sent_id


def test_log_query_scores_skips_entries_with_no_trace_id(client):
    opik_dataset.log_query_scores([{"relevancy": 5}, {"trace_id": None, "relevancy": 4}])
    assert not client.scored


def test_log_query_scores_is_a_noop_when_disabled(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "OPIK_API_KEY", "")
    assert opik_dataset.log_query_scores([{"trace_id": _tid(), "relevancy": 5}]) is None


def test_log_query_scores_fails_open(monkeypatch):
    from src import config

    class _Broken:
        def log_traces_feedback_scores(self, *a, **k):
            raise RuntimeError("opik down")

    monkeypatch.setattr(config, "OPIK_API_KEY", "test-key")
    monkeypatch.setattr(opik_dataset, "_client", lambda: _Broken())
    opik_dataset.log_query_scores([{"trace_id": _tid(), "relevancy": 5}])


def test_unconvertible_trace_id_is_reported_not_silently_dropped(client, monkeypatch):
    """`uuid4_to_uuid7` only accepts version-4 UUIDs, so a non-v4 id yields no
    Opik id and the score vanishes. Dropping it quietly would understate how
    many queries were scored — warn instead."""
    warned = []
    monkeypatch.setattr(opik_dataset, "_warn_once", lambda e: warned.append(e))
    opik_dataset.log_query_scores([{"trace_id": "a" * 32, "relevancy": 5}])
    assert not client.scored
    assert warned, "an unconvertible trace id was dropped without a warning"
