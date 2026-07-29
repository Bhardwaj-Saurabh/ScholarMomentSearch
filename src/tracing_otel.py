"""OpenTelemetry backend for the tracing facade — DESIGN.md §3g component 44.

Imported lazily by `src/tracing.py` ONLY when `OTEL_EXPORTER_OTLP_ENDPOINT` is
set. Uses a BatchSpanProcessor so export happens off the request path — this
sits in front of `/api/ask`, whose latency SLA is already red.

The facade owns span nesting, so this backend deliberately does NOT use OTel's
own context propagation: it reconstructs parentage from the record's ids. That
keeps one nesting implementation instead of two that can disagree.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger(__name__)


class OtelBackend:
    def __init__(self):
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter)
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create(
            {"service.name": config.OTEL_SERVICE_NAME}))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=config.OTEL_EXPORTER_OTLP_ENDPOINT)))
        self._tracer = trace.get_tracer(__name__, tracer_provider=provider)
        self._open: dict[str, object] = {}

    def start(self, rec: dict) -> None:
        span = self._tracer.start_span(rec["name"])
        for k, v in rec["attrs"].items():
            span.set_attribute(f"rag.{k}", v if isinstance(
                v, (str, int, float, bool)) else str(v))
        span.set_attribute("rag.trace_id", rec["trace_id"])
        if rec["parent"]:
            span.set_attribute("rag.parent_span_id", rec["parent"])
        self._open[rec["id"]] = span

    def end(self, rec: dict) -> None:
        span = self._open.pop(rec["id"], None)
        if span is None:
            return
        for k, v in rec["attrs"].items():      # attrs set after start()
            span.set_attribute(f"rag.{k}", v if isinstance(
                v, (str, int, float, bool)) else str(v))
        if rec["error"]:
            span.set_attribute("rag.error", rec["error"])
        span.end()
