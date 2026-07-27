"""Component 5 (DESIGN.md) — queue wiring: enqueue_document() (src/jobs.py) and
the worker serving both Prefect deployments from one process (src/worker.py).

Scope decision: src/dispatcher.py is a CLAUDE.md protected file, and DESIGN.md's
own row 5 offers an explicit alternative to unifying it — "documents ride FIFO
first, WFQ unified after". We take that path: dispatcher.py is untouched (0
diff — the last test below guards against silent scope creep); documents are
enqueued immediately via enqueue_document(), which the future admin endpoint
(component 6) will call directly at registration time.

enqueue_document mirrors enqueue_video exactly, so it's tested the same way:
mock prefect.deployments.run_deployment at the module boundary (a real call
needs a live Prefect deployment + worker, out of scope for a unit test) and
verify the dispatch contract — deployment name, parameters, fire-and-forget
timeout=0.

Worker deployment-building is tested via a small extracted helper
(_build_deployments) so the actual blocking serve() call itself — infra, not
business logic — never has to run in a test, matching how ingest_video.serve()
was never unit-tested before this change either.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src import jobs, worker


def test_enqueue_document_calls_run_deployment_with_correct_contract(monkeypatch):
    captured = {}

    def _fake_run_deployment(*, name, parameters, timeout, flow_run_name):
        captured["name"] = name
        captured["parameters"] = parameters
        captured["timeout"] = timeout
        captured["flow_run_name"] = flow_run_name
        fake_run = MagicMock()
        fake_run.id = "flow-run-abc123"
        return fake_run

    monkeypatch.setattr(jobs, "run_deployment", _fake_run_deployment)
    result = jobs.enqueue_document("doc_7f3a", "u1", "paper")

    assert result == "flow-run-abc123"
    assert captured["name"] == jobs.DOCUMENT_DEPLOYMENT == "ms-ingest-document/ingest"
    assert captured["parameters"] == {"doc_id": "doc_7f3a", "user_id": "u1", "kind": "paper"}
    assert captured["timeout"] == 0  # fire-and-forget, matches enqueue_video
    assert "doc_7f3a" in captured["flow_run_name"]


def test_video_deployment_contract_unchanged():
    """enqueue_video's deployment name must survive this change untouched —
    the provided video contract is a hard invariant."""
    assert jobs.INGEST_DEPLOYMENT == "ms-ingest-video/ingest"


def test_worker_serves_both_deployments_with_correct_full_names():
    deployments = worker._build_deployments()
    full_names = {d.full_name for d in deployments}
    assert full_names == {jobs.INGEST_DEPLOYMENT, jobs.DOCUMENT_DEPLOYMENT}


def test_worker_deployments_use_module_path_entrypoints():
    """Regression test for a Part-0 finding: Prefect's default FILE_PATH
    entrypoint (e.g. 'src/ingest/doc_pipeline.py:ingest_document') makes the
    worker re-import the flow's module as a bare script when it actually
    executes a scheduled run — which breaks on `from .. import db` etc. with
    'ImportError: attempted relative import beyond top-level package'. This
    crashed EVERY real document ingestion submitted through /admin/documents
    against a live worker (seed.py never hit it — it calls ingest_video/
    ingest_document directly, bypassing Prefect scheduling entirely).
    MODULE_PATH entrypoints (e.g. 'src.ingest.doc_pipeline:ingest_document')
    load via normal importlib.import_module, preserving package context."""
    from prefect.deployments.runner import EntrypointType

    deployments = worker._build_deployments()
    for d in deployments:
        assert d.entrypoint_type == EntrypointType.MODULE_PATH, d.full_name
        assert ".py:" not in d.entrypoint, d.full_name


def test_dispatcher_module_untouched_by_this_component():
    """Guard against silent scope creep: dispatcher.py (protected) should have
    no document-related content — documents ride FIFO via enqueue_document(),
    not the WFQ claim table, per the scope decision above."""
    import inspect

    from src import dispatcher
    source = inspect.getsource(dispatcher)
    assert "document" not in source.lower()
    assert "ms_documents" not in source
