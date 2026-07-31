"""Component 57 (DESIGN.md §3m) — minimal worker heartbeat.

The reconciler's over-admission problem: with dispatch deferred and a real
backlog (bench submits 16 documents against 2-6 worker slots), later waves sit
in a healthy SCHEDULED state well past the 90s stale window, and the sweep —
which treats any non-COMPLETED state as restart-worthy — re-enqueues them,
minting duplicate flow runs exactly while throughput is being measured.

The discriminator: a SCHEDULED run is healthy backlog IF some worker is
actually polling. This module gives the worker a TTL'd Redis heartbeat and the
reconciler a read side. Deliberately NOT component 35 (full worker liveness /
SIGSTOP detection) — just enough signal for the sweep, failing toward today's
behavior: no Redis, no heartbeat, any error => worker_alive() is False, the
sweep restarts as it always did (over-admission, never stranding).
"""
from __future__ import annotations

from src import cache, liveness


def test_beat_writes_a_ttl_key(monkeypatch):
    written = {}

    def _fake_set_json(key, value, ttl):
        written["key"], written["ttl"] = key, ttl

    monkeypatch.setattr(cache, "set_json", _fake_set_json)
    liveness.beat()
    assert written["key"] == liveness.HEARTBEAT_KEY
    assert 0 < written["ttl"] <= 60, "heartbeat must expire on its own"


def test_worker_alive_true_only_on_a_fresh_heartbeat(monkeypatch):
    monkeypatch.setattr(cache, "enabled", lambda: True)
    monkeypatch.setattr(cache, "get_json", lambda key: {"ts": 123.0})
    assert liveness.worker_alive() is True


def test_worker_alive_false_when_heartbeat_missing(monkeypatch):
    monkeypatch.setattr(cache, "enabled", lambda: True)
    monkeypatch.setattr(cache, "get_json", lambda key: None)
    assert liveness.worker_alive() is False


def test_worker_alive_false_when_cache_disabled(monkeypatch):
    """No Redis => no signal => the reconciler keeps today's behavior."""
    monkeypatch.setattr(cache, "enabled", lambda: False)
    assert liveness.worker_alive() is False


def test_beat_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cache, "set_json", _boom)
    liveness.beat()  # must not raise
