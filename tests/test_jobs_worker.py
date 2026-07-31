"""Component 5 (DESIGN.md) — queue wiring: enqueue_document() (src/jobs.py) and
the worker serving both Prefect deployments from one process (src/worker.py).

Scope decision: src/dispatcher.py is a CLAUDE.md protected file, and DESIGN.md's
own row 5 offers an explicit alternative to unifying it — "documents ride FIFO
first, WFQ unified after". We take that path: dispatcher.py is untouched (0
diff — the last test below guards against silent scope creep); documents are
enqueued immediately via enqueue_document(), which the future admin endpoint
(component 6) will call directly at registration time.

enqueue_document mirrors enqueue_video exactly, so it's tested the same way:
mock the low-level Prefect calls at the module boundary (a real call needs a
live Prefect deployment + worker, out of scope for a unit test) and verify
the dispatch contract — deployment name, parameters, fire-and-forget shape.

Component 52 (DESIGN.md §3k) changed HOW that dispatch reaches Prefect:
`run_deployment(name=...)` re-resolves the deployment name to a UUID via a
live network round trip on every single call (confirmed by isolated timing,
EVIDENCE.md 2026-07-31: ~299ms wasted per accept) even though the id never
changes for the process's lifetime. `_deployment_id()` now caches it; the
actual dispatch goes through `_create_flow_run()` against that cached id,
falling back to one full `run_deployment()` re-resolve if the cached id turns
out stale (deployment re-registered under a new id after this process
started).

Worker deployment-building is tested via a small extracted helper
(_build_deployments) so the actual blocking serve() call itself — infra, not
business logic — never has to run in a test, matching how ingest_video.serve()
was never unit-tested before this change either.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import jobs, worker


@pytest.fixture(autouse=True)
def _reset_deployment_id_cache():
    jobs._deployment_id_cache.clear()
    yield
    jobs._deployment_id_cache.clear()


def test_enqueue_document_calls_create_flow_run_with_correct_contract(monkeypatch):
    captured = {}

    def _fake_deployment_id(name):
        captured["resolved_name"] = name
        return "dep-uuid-fixed"

    def _fake_create(deployment_id, *, parameters, name):
        captured["deployment_id"] = deployment_id
        captured["parameters"] = parameters
        captured["flow_run_name"] = name
        fake_run = MagicMock()
        fake_run.id = "flow-run-abc123"
        return fake_run

    monkeypatch.setattr(jobs, "_deployment_id", _fake_deployment_id)
    monkeypatch.setattr(jobs, "_create_flow_run_from_deployment", _fake_create)
    result = jobs.enqueue_document("doc_7f3a", "u1", "paper")

    assert result == "flow-run-abc123"
    assert captured["resolved_name"] == jobs.DOCUMENT_DEPLOYMENT == "ms-ingest-document/ingest"
    assert captured["deployment_id"] == "dep-uuid-fixed"
    assert captured["parameters"] == {"doc_id": "doc_7f3a", "user_id": "u1", "kind": "paper"}
    assert "doc_7f3a" in captured["flow_run_name"]


def test_deployment_id_resolved_once_across_two_enqueue_calls(monkeypatch):
    """The whole point of component 52: the by-name lookup must not repeat
    per accept. Two enqueue calls -> exactly one read_deployment_by_name."""
    lookups = []

    class _FakeSyncClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read_deployment_by_name(self, name):
            lookups.append(name)
            fake_dep = MagicMock()
            fake_dep.id = "dep-uuid-fixed"
            return fake_dep

        def create_flow_run_from_deployment(self, deployment_id, *, parameters, name):
            fake_run = MagicMock()
            fake_run.id = "flow-run-xyz"
            return fake_run

    monkeypatch.setattr(jobs, "_get_sync_client", lambda: _FakeSyncClient())

    jobs.enqueue_document("doc_1", "u1", "paper")
    jobs.enqueue_document("doc_2", "u1", "paper")

    assert lookups == [jobs.DOCUMENT_DEPLOYMENT]  # resolved once, reused on the 2nd call


def test_stale_cached_deployment_id_falls_back_to_one_reresolve(monkeypatch):
    """If the cached id is stale (e.g. deployment re-registered), the create
    call fails once, the cache is dropped, and run_deployment() is used as
    the fallback path (which re-resolves fresh) rather than crashing."""
    jobs._deployment_id_cache[jobs.DOCUMENT_DEPLOYMENT] = "stale-uuid"

    def _boom(deployment_id, *, parameters, name):
        raise Exception("Deployment not found")

    monkeypatch.setattr(jobs, "_create_flow_run_from_deployment", _boom)

    def _fake_run_deployment(*, name, parameters, timeout, flow_run_name):
        fake_run = MagicMock()
        fake_run.id = "flow-run-fallback"
        return fake_run

    monkeypatch.setattr(jobs, "run_deployment", _fake_run_deployment)

    result = jobs.enqueue_document("doc_9", "u1", "paper")

    assert result == "flow-run-fallback"
    assert jobs.DOCUMENT_DEPLOYMENT not in jobs._deployment_id_cache  # dropped, not left stale


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


def test_one_prefect_client_reused_across_dispatches(monkeypatch):
    """Component 56 (DESIGN.md §3m): component 52 cached the deployment ID but
    still built a brand-new Prefect client — a fresh TCP+TLS handshake to a
    US-hosted control plane — on every dispatch. One long-lived client per
    process, constructed once."""
    constructions = []

    class _FakeSyncClient:
        def __init__(self):
            constructions.append(1)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read_deployment_by_name(self, name):
            fake_dep = MagicMock()
            fake_dep.id = "dep-uuid-fixed"
            return fake_dep

        def create_flow_run_from_deployment(self, deployment_id, *, parameters, name):
            fake_run = MagicMock()
            fake_run.id = "flow-run-xyz"
            return fake_run

    import prefect.client.orchestration as orch
    monkeypatch.setattr(orch, "get_client",
                        lambda sync_client=False: _FakeSyncClient())
    monkeypatch.setattr(jobs, "_client", None, raising=False)
    jobs._deployment_id_cache.clear()

    jobs.enqueue_document("doc_a", "u1", "paper")
    jobs.enqueue_document("doc_b", "u1", "paper")
    jobs.enqueue_video("yt_c", "u1")

    assert sum(constructions) == 1, (
        f"expected ONE Prefect client for the process, got {sum(constructions)} constructions")
