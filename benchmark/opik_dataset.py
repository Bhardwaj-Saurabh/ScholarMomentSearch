"""Opik eval dataset + experiment versioning — DESIGN.md §3g component 48.

Component 47 made prompts versionable. This makes eval RESULTS comparable.
Before it, `answer_quality.py` printed faithfulness 0.96 / relevancy 5.0 and
that was the end of it: nothing recorded which prompt, which embeddings, or
which retrieval flags produced the number, and nothing to diff a later run
against. The question you actually want answered — "did that prompt edit help,
and which queries regressed?" — was unanswerable.

So: push `benchmark/labeled_queries.json` to a named Opik **Dataset** (Opik
versions datasets itself and dedupes items by content, so re-pushing is
idempotent), and log each benchmark run as an **Experiment** whose config
carries the full provenance from `src.prompts.versions()` plus the retrieval
flags actually in force.

Two rules this module holds to:

**Opik is the RECORD, never the GATE.** `benchmark/quality_gates.json` remains
the only thing that decides pass/fail. Nothing here reads a threshold or exits a
process — a test asserts that structurally, because a telemetry backend that can
turn a build red is a liability, not an asset.

**Strictly opt-in.** `OPIK_API_KEY` unset ⇒ every function is a no-op and both
benchmarks behave byte-identically to before this existed. And every call fails
OPEN: an Opik outage must never sink a benchmark run, least of all one gating an
SLA.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `bench.py` is documented and run as a SCRIPT (`python benchmark/bench.py`),
# which puts `benchmark/` on sys.path — not the repo root — so `src` is not
# importable and this module's own import fails. The caller's fail-open guard
# then swallowed it, leaving the feature silently inert in the one invocation
# people actually use: "recorded in Opik" simply never printed. Putting the
# root on the path here fixes it at the source rather than making every caller
# compensate. Idempotent, and a no-op under `python -m` / pytest, where the
# root is already present.
if str(ROOT) not in sys.path:
    # append, not insert(0): this only needs `src` to be importable, and taking
    # precedence over site-packages is a shadowing risk for no benefit.
    sys.path.append(str(ROOT))

from src import config, prompts  # noqa: E402 - must follow the sys.path fix

logger = logging.getLogger(__name__)
DATASET_NAME = "scholarmomentsearch-labeled-queries"

_warned = False


def enabled() -> bool:
    return bool(config.OPIK_API_KEY)


def _warn_once(exc: Exception) -> None:
    global _warned
    if not _warned:
        logger.warning("[opik_dataset] Opik unavailable (%r) — continuing unrecorded", exc)
        _warned = True


def _client():
    import opik

    return opik.Opik(project_name=config.OPIK_PROJECT_NAME or None,
                     workspace=config.OPIK_WORKSPACE or None)


def _dataset_items() -> list[dict]:
    """The labeled query set as Opik dataset items.

    Deterministic by construction — no timestamp, no uuid. Opik dedupes items
    by CONTENT, so idempotency depends entirely on emitting byte-identical
    items each push; a clock or random value in here would silently create a
    duplicate of the whole set every run.
    """
    path = ROOT / "benchmark" / "labeled_queries.json"
    if not path.exists():
        return []
    queries = json.loads(path.read_text()).get("queries", [])
    return [
        {
            "query": q["query"],
            "corpus_id": q.get("corpus_id"),
            # Kept alongside the question deliberately: a dataset of bare
            # questions cannot express WHICH query regressed against WHAT
            # expectation, which is the entire point of comparing runs.
            "expect_kinds": q.get("expect_kinds"),
        }
        for q in queries
    ]


def push_labeled_queries() -> dict | None:
    """Upsert the eval set into Opik. Returns dataset info, or None when
    disabled/unavailable."""
    if not enabled():
        return None
    try:
        # Inside the try: a malformed labeled_queries.json would otherwise
        # propagate out of a function whose contract says it always fails open.
        items = _dataset_items()
        if not items:
            return None
        ds = _client().get_or_create_dataset(
            name=DATASET_NAME,
            description="Labeled queries for recall@10 / precision@10 / answer "
                        "quality (benchmark/labeled_queries.json).")
        ds.insert(items)
        version = None
        try:
            version = ds.get_current_version_name()
        except Exception:
            pass          # version reporting is a nicety, not a requirement
        return {"dataset": DATASET_NAME, "items": len(items), "version": version}
    except Exception as exc:
        _warn_once(exc)
        return None


def _provenance() -> dict:
    """Everything needed to attribute a score. The retrieval flags matter as
    much as the prompt versions: without them a score change is ambiguous
    between "the prompt was edited" and "someone turned the reranker off"."""
    v = prompts.versions()
    return {
        **v,
        "hybrid_text_search": config.ENABLE_HYBRID_TEXT_SEARCH,
        "rerank_enabled": config.RERANK_ENABLED,
        "rerank_model": config.RERANK_MODEL,
        "query_enhancement_enabled": config.QUERY_ENHANCEMENT_ENABLED,
        "top_k": config.TOP_K,
    }


def log_experiment(name: str, metrics: dict, *, run_id: str | None = None) -> str | None:
    """Record one benchmark run as an Opik experiment. Returns its id, or None
    when disabled/unavailable."""
    if not enabled():
        return None
    try:
        # A unique name per run: two runs collapsing into one experiment would
        # make the before/after comparison this exists for impossible.
        suffix = run_id or _run_suffix()
        exp = _client().create_experiment(
            dataset_name=DATASET_NAME,
            name=f"{name}-{suffix}",
            experiment_config={**_provenance(), "metrics": metrics, "run": name},
        )
        return getattr(exp, "id", None)
    except Exception as exc:
        _warn_once(exc)
        return None


# Metrics that are per-QUERY rather than per-run. Anything else in a score dict
# (trace_id, bookkeeping) is ignored rather than sent as a bogus score.
_SCORE_FIELDS = ("relevancy", "faithfulness", "precision")


def log_query_scores(rows: list[dict]) -> None:
    """Attach per-query scores to the traces that produced those answers.

    §3g specified this and it was not built: experiments carried only
    aggregates, so "did that prompt edit help, and WHICH queries regressed?"
    still meant diffing raw numbers by hand. A feedback score lands on the
    trace itself, so a regression is one click from the spans that caused it.

    Each row needs the `trace_id` the server reported for that answer. Rows
    without one are skipped — a score attached to nothing is worse than no
    score, because it inflates the count. Fails OPEN.
    """
    if not enabled():
        return None
    try:
        from src.tracing_opik import _opik_trace_id

        scores: list[dict] = []
        unconvertible = 0
        for row in rows:
            tid = row.get("trace_id")
            if not tid:
                continue          # nothing to attach to
            opik_id = _opik_trace_id(str(tid))
            if not opik_id:
                # `uuid4_to_uuid7` only accepts version-4 UUIDs. Ours are, but
                # a trace id from anywhere else would silently lose its score
                # and understate how many queries were scored.
                unconvertible += 1
                continue
            for field in _SCORE_FIELDS:
                if row.get(field) is None:
                    continue
                scores.append({"id": opik_id, "name": field,
                               "value": float(row[field])})
        if unconvertible:
            _warn_once(ValueError(
                f"{unconvertible} trace id(s) could not be converted to Opik "
                "form — those per-query scores were not recorded"))
        if not scores:
            return None
        _client().log_traces_feedback_scores(scores)
    except Exception as exc:
        _warn_once(exc)
    return None


def _run_suffix() -> str:
    """Wall-clock stamp, used ONLY for the experiment name — never inside a
    dataset item, which must stay content-stable (see _dataset_items)."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
