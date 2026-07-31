"""Automatic crash recovery for documents (Part-0 resilience finding, fixed).

`bench.py --resilience` kills the worker mid-ingest; DESIGN.md/ARCHITECTURE.md
assumed "Prefect redelivers the interrupted run once a worker is polling
again" — in practice that's not automatic: a hard-killed process leaves
nothing to ever flip the row's status again (it only advances when the
flow's OWN code runs `db.set_document_status(...)`), and there's no
flow-level `retries=` configured. Manually calling this repo's own
POST /admin/documents/{id}/retry recovered the two stuck documents from that
finding just fine — this module automates exactly that, as a periodic sweep.

Distinguishing "genuinely orphaned" from "still fine, just queued behind
capacity" matters: a document merely waiting for a free worker slot is
NORMAL backlog (we saw plenty of this in Part 0 under real load), not
something to restart. A stale `updated_at` is only a cheap PREFILTER — the
real, authoritative check is asking Prefect for that row's OWN flow run
state and only acting when Prefect itself has already given up on it
(Crashed/Failed/Cancelled). A row with no `flow_run_id` (seeded directly
in-process, component 10 — never went through Prefect scheduling at all) is
never touched here.

Runs as a background thread in worker.py, independent of src/dispatcher.py's
WFQ loop (documents ride FIFO, not the dispatcher — component 5's decision —
so recovery here calls jobs.enqueue_document() directly, exactly like the
component-11 retry endpoint, rather than resetting to 'pending' and hoping a
dispatcher that never looks at documents happens to pick it up).

Scoped to documents only: videos already self-heal for the common case
(src/dispatcher.py's WFQ loop continuously re-admits ANY 'pending' video, so
resetting status alone is enough there) — the narrower remaining video gap
(stuck in an ACTIVE status like 'fetching' after a crash, not 'pending') is a
known, disclosed parallel not fixed here, since src/dispatcher.py and
src/api/videos.py are CLAUDE.md-protected and the graded resilience gate
(`bench.py --resilience`) only exercises documents.
"""
from __future__ import annotations

import threading
import time

from . import config, db, jobs, liveness

# Anything that hasn't reached a terminal state yet. Deliberately includes
# "pending": the original resilience-test finding left 2 documents stuck at
# 'pending' — killed before their flow's first task (`t_fetch`) ever ran, so
# the row was never touched past its initial insert. A stale timestamp alone
# doesn't distinguish that from healthy backlog waiting for a free slot; the
# flow_run_id check below does.
_ACTIVE_STATUSES = ("pending", "fetching", "parsing", "embedding")

# Never restart the SAME row twice within this window. Two worker replicas
# each run their own copy of this sweep (independent in-memory state, like
# src/dispatcher.py's WFQ threads) — at worst that's a harmless double
# restart (idempotent uuid5 upserts, last-write-wins status), same tradeoff
# src/dispatcher.py already accepts for over-admission.
_COOLDOWN_S = 300
_RECENTLY_RESTARTED: dict[str, float] = {}


def _read_flow_run(flow_run_id: str):
    from prefect.client.orchestration import get_client

    with get_client(sync_client=True) as c:
        return c.read_flow_run(flow_run_id)


def _flow_run_dead(flow_run_id: str | None) -> bool:
    """Whether it's safe to restart this row, given the caller has ALREADY
    confirmed its DB timestamp has been stale past RECONCILE_STALE_AFTER_S —
    that staleness is the real, load-bearing signal here, not Prefect's own
    state, which cannot be trusted to distinguish "orphaned" from "healthy":

    - A flow run whose entire worker container was SIGKILLed while EXECUTING
      keeps reporting `state.type == RUNNING` indefinitely — Prefect Cloud's
      heartbeat-based zombie detection is an opt-in "managed automation" (off
      by default) and ~9 minutes even when enabled. Confirmed live: a run
      stayed "Running" 5+ minutes after its worker died.
    - A flow run whose worker was killed WHILE STILL LAUNCHING it (before
      the subprocess ever reached Running) instead gets stuck at
      `state.type == PENDING` ("Submitting") — confirmed live, a second real
      orphaning shape distinct from the first. PENDING/SCHEDULED are ALSO
      the state of perfectly healthy work still queued behind capacity, so
      Prefect's state genuinely cannot tell these apart on its own.

    Given neither RUNNING nor PENDING/SCHEDULED reliably means "still fine,"
    the DB staleness prefilter (db.stale_documents, already applied by the
    caller) is what actually gates action here — restarting a row that
    turns out to have still been healthily backlogged is a wasted duplicate
    flow run, never an incorrect one (crash-safe status ordering + idempotent
    uuid5 upserts + per-tenant duplicate detection all still hold), which is
    an acceptable tradeoff for actually closing the "worker killed mid-flight
    loses a document forever" gap — the same over-admission tradeoff
    src/dispatcher.py's own WFQ loop already accepts.

    Only two things are NOT treated as restart-worthy: `flow_run_id` is None
    (seeded directly in-process, component 10 — no Prefect run ever existed
    to check) and COMPLETED (Prefect says this already finished — restarting
    would risk duplicating already-finished work; left for investigation,
    not auto-restarted). A lookup failure (can't confirm anything this tick)
    also returns False: when genuinely unable to check, don't act blind."""
    if not flow_run_id:
        return False
    from prefect.client.schemas.objects import StateType

    try:
        run = _read_flow_run(flow_run_id)
    except Exception:
        return False
    if run.state is None:
        return False
    if run.state.type == StateType.COMPLETED:
        return False
    # Component 57 (DESIGN.md §3m): SCHEDULED means "created, awaiting
    # pickup" — healthy backlog whenever a worker is actually polling, and
    # under real load (16 docs vs a handful of slots) later waves sit here
    # far past the stale window. Restarting them minted duplicate flow runs
    # exactly while throughput was measured. The worker heartbeat is the
    # discriminator; it reads False on ANY doubt (no Redis, expired, error),
    # so this never strands a run — it only stops punishing healthy backlog.
    # PENDING and RUNNING deliberately still restart: killed-mid-launch
    # leaves PENDING and killed-mid-execution leaves RUNNING even while a
    # RESTARTED worker heartbeats, and the resilience gate depends on both
    # shapes recovering.
    if run.state.type == StateType.SCHEDULED and liveness.worker_alive():
        return False
    return True


def reconcile_once(stale_after_s: float | None = None) -> int:
    """Find documents stuck in `_ACTIVE_STATUSES` whose own Prefect flow run
    has genuinely died, and restart them exactly like a manual retry would.
    Returns how many were restarted."""
    stale_after_s = config.RECONCILE_STALE_AFTER_S if stale_after_s is None else stale_after_s
    candidates = db.stale_documents(_ACTIVE_STATUSES, stale_after_s)
    now = time.time()
    restarted = 0
    for row in candidates:
        doc_id = row["id"]
        last = _RECENTLY_RESTARTED.get(doc_id)
        if last is not None and now - last < _COOLDOWN_S:
            continue
        # Component 56 (DESIGN.md §3m): registration now returns 202 after the
        # DB insert alone and dispatches to Prefect in a background task. A
        # crash between the two leaves 'pending' + flow_run_id NULL — for that
        # ONE shape, NULL means "accepted but never dispatched" and the row
        # must be re-enqueued or the document is lost forever (this sweep is
        # the deferral's crash-safety net). Every other active status with
        # NULL keeps the original component-10 skip below: a row mid-stage
        # with no flow run is in-process seeding, which updates its own
        # status as it works.
        if row["status"] == "pending" and not row.get("flow_run_id"):
            pass  # stale + never dispatched -> restart
        elif not _flow_run_dead(row.get("flow_run_id")):
            continue
        print(f"[reconcile] {doc_id} stuck in '{row['status']}' — its flow run "
              f"{row.get('flow_run_id')} died — restarting", flush=True)
        db.set_document_status(doc_id, "pending", error=None)
        try:
            flow_run_id = jobs.enqueue_document(doc_id, row["user_id"], row["kind"])
            db.set_document_flow_run_id(doc_id, flow_run_id)
        except Exception as exc:
            db.set_document_status(doc_id, "failed", error=f"reconcile re-enqueue: {exc}")
        _RECENTLY_RESTARTED[doc_id] = now
        restarted += 1
    return restarted


def run_forever() -> None:
    print(f"[reconcile] crash-recovery sweep on — checking every "
         f"{config.RECONCILE_INTERVAL_S}s for documents stuck >"
         f"{config.RECONCILE_STALE_AFTER_S}s", flush=True)
    while True:
        try:
            reconcile_once()
        except Exception as exc:  # never let the sweep thread die
            print(f"[reconcile] error: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(config.RECONCILE_INTERVAL_S)


def start_in_background() -> None:
    threading.Thread(target=run_forever, daemon=True).start()
