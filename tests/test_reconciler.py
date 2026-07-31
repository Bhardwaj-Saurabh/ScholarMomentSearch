"""Part-0 resilience finding, fixed: `bench.py --resilience` killed a worker
mid-ingest and 2 of 10 documents never reached a terminal state — Docker's
`restart: unless-stopped` didn't even fire for the killed `--scale` replica,
and even once a worker was back, the interrupted flow runs stayed stuck
forever (no flow-level retries, no automatic redelivery). Manually calling
this repo's own POST /admin/documents/{id}/retry recovered them fine — this
component automates exactly that recovery, via a background sweep
(src/reconciler.py) that finds documents stuck in an active status whose OWN
Prefect flow run has genuinely died (Crashed/Failed/Cancelled — NOT just
"hasn't updated in a while", which is also true of perfectly healthy work
still queued behind capacity) and restarts them.

Real throwaway Postgres for the actual stale-row query and end-to-end
reconcile logic (that IS what's being proven); Prefect's own flow-run-state
API is mocked (a real check needs a live Prefect Cloud deployment, out of
scope for a unit test — already exercised for real in this session against
the live stack, see EVIDENCE.md) and jobs.enqueue_document is mocked (same
reasoning as component 5/6's own tests).
"""
from __future__ import annotations

import pytest

from src import db, jobs, reconciler


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()


@pytest.fixture
def cleanup():
    ids = []
    yield ids
    for i in ids:
        db.delete_document(i)


def _make_document(doc_id, status, age_s=0, flow_run_id=None):
    db.upsert_pending_document({"id": doc_id, "user_id": "default", "kind": "paper",
                               "uri": "https://arxiv.org/pdf/1706.03762",
                               "storage_key": None, "source_hash": None, "title": "x"})
    db.set_document_status(doc_id, status)
    if flow_run_id:
        db.set_document_flow_run_id(doc_id, flow_run_id)
    if age_s:
        with db.pool().connection() as conn:
            conn.execute("UPDATE ms_documents SET updated_at = now() - (%s || ' seconds')::interval "
                        "WHERE id = %s", (age_s, doc_id))


# ── db.stale_documents() ─────────────────────────────────────────────────────

def test_stale_documents_finds_old_rows_in_the_given_statuses(cleanup):
    cleanup.append("doc_stale1")
    _make_document("doc_stale1", "fetching", age_s=200)
    found = {r["id"] for r in db.stale_documents(("fetching", "parsing", "embedding"), 90)}
    assert "doc_stale1" in found


def test_stale_documents_excludes_recently_updated_rows(cleanup):
    cleanup.append("doc_fresh1")
    _make_document("doc_fresh1", "fetching", age_s=0)
    found = {r["id"] for r in db.stale_documents(("fetching", "parsing", "embedding"), 90)}
    assert "doc_fresh1" not in found


def test_stale_documents_excludes_rows_in_other_statuses(cleanup):
    cleanup.append("doc_done1")
    _make_document("doc_done1", "indexed", age_s=200)
    found = {r["id"] for r in db.stale_documents(("fetching", "parsing", "embedding"), 90)}
    assert "doc_done1" not in found


# ── db.set_document_flow_run_id() ────────────────────────────────────────────

def test_set_document_flow_run_id_persists(cleanup):
    cleanup.append("doc_fr1")
    _make_document("doc_fr1", "fetching")
    db.set_document_flow_run_id("doc_fr1", "flow-run-abc")
    assert db.get_document("doc_fr1")["flow_run_id"] == "flow-run-abc"


# ── reconciler._flow_run_dead() ───────────────────────────────────────────────

class _FakeState:
    def __init__(self, type_):
        self.type = type_


class _FakeRun:
    def __init__(self, state_type):
        self.state = _FakeState(state_type) if state_type else None


def test_flow_run_dead_true_for_every_non_completed_state(monkeypatch):
    """Prefect's own state can't reliably distinguish orphaned from healthy
    (confirmed live, two distinct ways): a worker SIGKILLed mid-EXECUTION
    leaves its run 'Running' indefinitely (zombie-run detection is an opt-in
    Cloud automation, off by default, ~9min even when on); a worker SIGKILLed
    mid-LAUNCH (before the subprocess ever started) leaves its run stuck at
    'Pending'/'Submitting' instead — and PENDING/SCHEDULED is ALSO the state
    of perfectly healthy queued work. By the time this is called the caller
    has ALREADY confirmed DB staleness (the real signal), so every state
    except COMPLETED is treated as restart-worthy."""
    from prefect.client.schemas.objects import StateType

    # Component 57 refined SCHEDULED: it stays restart-worthy only when no
    # worker heartbeat is present (pinned False here so the original contract
    # is what's under test, regardless of any local Redis).
    monkeypatch.setattr(reconciler.liveness, "worker_alive", lambda: False)
    for bad in (StateType.CRASHED, StateType.FAILED, StateType.CANCELLED,
               StateType.RUNNING, StateType.PENDING, StateType.SCHEDULED):
        monkeypatch.setattr(reconciler, "_read_flow_run", lambda fid, _b=bad: _FakeRun(_b))
        assert reconciler._flow_run_dead("some-id") is True, bad


def test_flow_run_dead_false_when_prefect_says_it_already_completed(monkeypatch):
    """The one state that must NOT be restarted: Prefect says this run
    already finished. Restarting would risk duplicating finished work — a
    DB/Prefect desync like this is left for investigation, not auto-fixed."""
    from prefect.client.schemas.objects import StateType

    monkeypatch.setattr(reconciler, "_read_flow_run", lambda fid: _FakeRun(StateType.COMPLETED))
    assert reconciler._flow_run_dead("some-id") is False


def test_flow_run_dead_false_when_no_flow_run_id():
    assert reconciler._flow_run_dead(None) is False


def test_flow_run_dead_false_on_lookup_error(monkeypatch):
    def _boom(fid):
        raise RuntimeError("Prefect Cloud unreachable")
    monkeypatch.setattr(reconciler, "_read_flow_run", _boom)
    assert reconciler._flow_run_dead("some-id") is False  # uncertain -> don't touch it


# ── reconciler.reconcile_once() ──────────────────────────────────────────────

def test_reconcile_restarts_a_document_whose_flow_run_actually_died(monkeypatch, cleanup):
    cleanup.append("doc_dead1")
    _make_document("doc_dead1", "fetching", age_s=200, flow_run_id="dead-run-1")
    monkeypatch.setattr(reconciler, "_flow_run_dead", lambda fid: True)
    calls = {}

    def _fake_enqueue(doc_id, user_id, kind):
        calls["doc_id"] = doc_id
        return "new-run-id"
    monkeypatch.setattr(jobs, "enqueue_document", _fake_enqueue)

    n = reconciler.reconcile_once()
    assert n == 1
    assert calls["doc_id"] == "doc_dead1"
    row = db.get_document("doc_dead1")
    assert row["flow_run_id"] == "new-run-id"


def test_reconcile_leaves_a_still_in_flight_document_alone(monkeypatch, cleanup):
    cleanup.append("doc_alive1")
    _make_document("doc_alive1", "embedding", age_s=200, flow_run_id="running-1")
    monkeypatch.setattr(reconciler, "_flow_run_dead", lambda fid: False)
    calls = {"n": 0}
    monkeypatch.setattr(jobs, "enqueue_document", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    n = reconciler.reconcile_once()
    assert n == 0
    assert calls["n"] == 0
    assert db.get_document("doc_alive1")["status"] == "embedding"  # untouched


def test_reconcile_skips_a_stale_row_with_no_flow_run_id(monkeypatch, cleanup):
    """Seeded documents (component 10) call ingest_document() directly,
    in-process — never through Prefect scheduling — so they have no
    flow_run_id to check. Never guess for these; leave them alone."""
    cleanup.append("doc_noflow1")
    _make_document("doc_noflow1", "parsing", age_s=200, flow_run_id=None)
    calls = {"n": 0}
    monkeypatch.setattr(jobs, "enqueue_document", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    n = reconciler.reconcile_once()
    assert n == 0
    assert calls["n"] == 0


def test_reconcile_does_not_restart_the_same_row_twice_within_the_cooldown(monkeypatch, cleanup):
    cleanup.append("doc_cooldown1")
    _make_document("doc_cooldown1", "fetching", age_s=200, flow_run_id="dead-run-2")
    monkeypatch.setattr(reconciler, "_flow_run_dead", lambda fid: True)
    calls = {"n": 0}

    def _fake_enqueue(doc_id, user_id, kind):
        calls["n"] += 1
        return "new-run-id"
    monkeypatch.setattr(jobs, "enqueue_document", _fake_enqueue)

    reconciler._RECENTLY_RESTARTED.clear()
    first = reconciler.reconcile_once()
    # Re-backdate so it still LOOKS stale on the very next tick (as a real
    # freshly-restarted "pending" row would not, but this isolates the
    # cooldown behavior specifically from the staleness prefilter).
    with db.pool().connection() as conn:
        conn.execute("UPDATE ms_documents SET updated_at = now() - interval '200 seconds' "
                    "WHERE id = %s", ("doc_cooldown1",))
    second = reconciler.reconcile_once()

    assert first == 1
    assert second == 0
    assert calls["n"] == 1


# ── Component 56 (DESIGN.md §3m) — the 202-then-crash safety net ─────────────
# Registration now returns 202 after the DB insert alone; the Prefect dispatch
# runs in a background task. If the API process dies between the 202 and the
# dispatch, the row is left 'pending' with flow_run_id NULL — a shape the
# sweep used to skip unconditionally (it meant "seeded in-process, no Prefect
# run ever existed"). For 'pending' specifically, NULL now means "accepted but
# never dispatched", and a stale row in that shape must be re-enqueued or the
# document is lost forever. Other active statuses with NULL keep the old skip:
# a row at fetching/parsing/embedding with no flow run is in-process seeding,
# which actively updates its own status.

def test_reconcile_restarts_a_stale_pending_row_that_was_never_dispatched(monkeypatch, cleanup):
    cleanup.append("doc_nodispatch1")
    _make_document("doc_nodispatch1", "pending", age_s=200, flow_run_id=None)

    calls = {}

    def _fake_enqueue(doc_id, uid, kind):
        calls["id"] = doc_id
        return "rescued-run-1"

    monkeypatch.setattr(reconciler.jobs, "enqueue_document", _fake_enqueue)
    restarted = reconciler.reconcile_once()
    assert restarted >= 1
    assert calls.get("id") == "doc_nodispatch1"
    row = db.get_document("doc_nodispatch1")
    assert row["flow_run_id"] == "rescued-run-1"


def test_reconcile_still_skips_non_pending_rows_with_no_flow_run_id(monkeypatch, cleanup):
    """The original component-10 guard: an in-process seeded row mid-stage has
    no flow run to check and must never be guessed at."""
    cleanup.append("doc_seedlike1")
    _make_document("doc_seedlike1", "embedding", age_s=200, flow_run_id=None)

    called = []
    monkeypatch.setattr(reconciler.jobs, "enqueue_document",
                        lambda *a, **k: called.append(a) or "nope")
    reconciler.reconcile_once()
    assert not called
    assert db.get_document("doc_seedlike1")["status"] == "embedding"


# ── Component 57 (DESIGN.md §3m) — SCHEDULED backlog vs dead run ─────────────
# A SCHEDULED run is "created, waiting for pickup". With a real backlog (bench
# submits 16 docs against a handful of worker slots) later waves sit SCHEDULED
# well past the stale window, and treating that as dead re-enqueued them —
# duplicate flow runs minted exactly while throughput was being measured. The
# discriminator is the component-57 worker heartbeat: SCHEDULED + a live
# worker polling = healthy backlog, leave it. No heartbeat (worker actually
# dead, or Redis off/unreachable) = today's restart behavior. PENDING and
# RUNNING keep restarting regardless — a worker killed mid-launch leaves
# PENDING, killed mid-execution leaves RUNNING, and both MUST recover for the
# resilience gate.

def test_reconcile_leaves_scheduled_backlog_alone_when_a_worker_is_alive(monkeypatch, cleanup):
    from prefect.client.schemas.objects import StateType

    cleanup.append("doc_backlog_c57a")
    _make_document("doc_backlog_c57a", "pending", age_s=200, flow_run_id="sched-c57a")
    monkeypatch.setattr(reconciler, "_read_flow_run",
                        lambda fid: _FakeRun(StateType.SCHEDULED))
    monkeypatch.setattr(reconciler.liveness, "worker_alive", lambda: True)
    called = []
    monkeypatch.setattr(reconciler.jobs, "enqueue_document",
                        lambda *a, **k: called.append(a) or "dup-run")
    assert reconciler.reconcile_once() == 0
    assert not called, "healthy SCHEDULED backlog must not be re-enqueued"


def test_reconcile_restarts_a_scheduled_run_when_no_worker_is_alive(monkeypatch, cleanup):
    from prefect.client.schemas.objects import StateType

    cleanup.append("doc_backlog_c57b")
    _make_document("doc_backlog_c57b", "pending", age_s=200, flow_run_id="sched-c57b")
    monkeypatch.setattr(reconciler, "_read_flow_run",
                        lambda fid: _FakeRun(StateType.SCHEDULED))
    monkeypatch.setattr(reconciler.liveness, "worker_alive", lambda: False)

    def _fake_enqueue(doc_id, uid, kind):
        return "rescued-c57b"

    monkeypatch.setattr(reconciler.jobs, "enqueue_document", _fake_enqueue)
    assert reconciler.reconcile_once() >= 1
    assert db.get_document("doc_backlog_c57b")["flow_run_id"] == "rescued-c57b"


def test_reconcile_restarts_a_pending_run_even_with_a_live_worker(monkeypatch, cleanup):
    """Killed-mid-launch leaves the run stuck PENDING while the restarted
    worker heartbeats happily — the resilience gate depends on this shape
    still recovering."""
    from prefect.client.schemas.objects import StateType

    cleanup.append("doc_backlog_c57c")
    _make_document("doc_backlog_c57c", "fetching", age_s=200, flow_run_id="pend-c57c")
    monkeypatch.setattr(reconciler, "_read_flow_run",
                        lambda fid: _FakeRun(StateType.PENDING))
    monkeypatch.setattr(reconciler.liveness, "worker_alive", lambda: True)

    def _fake_enqueue(doc_id, uid, kind):
        return "rescued-c57c"

    monkeypatch.setattr(reconciler.jobs, "enqueue_document", _fake_enqueue)
    assert reconciler.reconcile_once() >= 1
