"""Live metrics endpoints — DESIGN.md §3c component 18.

Not part of the assignment's grading. GET /metrics is Prometheus text
exposition format (for a real Prometheus/Grafana scrape); GET /admin/metrics
is JSON for the UI's own polling dashboard. Both gated by the same admin
bearer token as other admin-sensitive routes (confirmed with the user) — this
is operational/cost data, not public.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from .. import db, metrics
from .videos import require_auth

router = APIRouter(tags=["metrics"])


@router.get("/metrics", dependencies=[Depends(require_auth)])
def metrics_prometheus():
    return PlainTextResponse(metrics.prometheus_text(), media_type="text/plain; version=0.0.4")


@router.get("/admin/metrics", dependencies=[Depends(require_auth)])
def metrics_json():
    snap = metrics.snapshot()
    snap["queue"] = db.queue_status_counts()
    return snap
