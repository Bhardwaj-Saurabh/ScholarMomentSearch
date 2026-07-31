"""Minimal worker heartbeat — component 57 (DESIGN.md §3m).

Gives the reconciler the one bit it was missing: whether a worker is actually
polling the queue. A Prefect run sitting SCHEDULED is healthy backlog when a
worker is alive, and an orphan when none is — Prefect's own state can't tell
those apart (see reconciler._flow_run_dead), so the sweep used to restart
healthy backlog and mint duplicate flow runs under exactly the load the
throughput gate measures.

Deliberately NOT the full component-35 worker-liveness remit (SIGSTOP
detection, per-worker staleness): one TTL'd Redis key, written by the worker
process, read by the sweep. Every failure mode degrades toward today's
behavior — no Redis, an unreachable Redis, or a genuinely dead worker all
read as "not alive", and the sweep restarts like it always did. Over-admission
is a wasted duplicate run; stranding a document would be a lost one.
"""
from __future__ import annotations

import threading
import time

from . import cache

HEARTBEAT_KEY = "ms:worker:heartbeat"
HEARTBEAT_INTERVAL_S = 10
HEARTBEAT_TTL_S = 30


def beat() -> None:
    """Write one heartbeat. Fails open — cache.set_json already swallows
    Redis errors, and this guard covers everything else."""
    try:
        cache.set_json(HEARTBEAT_KEY, {"ts": time.time()}, ttl=HEARTBEAT_TTL_S)
    except Exception:
        pass


def worker_alive() -> bool:
    """True only on a POSITIVE fresh heartbeat. Cache disabled, key expired,
    or any read error => False, so the caller falls back to restart-happy
    behavior rather than trusting a signal that isn't there."""
    if not cache.enabled():
        return False
    try:
        return cache.get_json(HEARTBEAT_KEY) is not None
    except Exception:
        return False


def start_heartbeat_in_background() -> None:
    """Daemon thread beating every HEARTBEAT_INTERVAL_S; TTL is 3 intervals
    so one missed beat (GC pause, slow Redis) doesn't read as a dead worker."""
    def _loop() -> None:
        while True:
            beat()
            time.sleep(HEARTBEAT_INTERVAL_S)

    threading.Thread(target=_loop, name="worker-heartbeat", daemon=True).start()
