"""Opik backend for the tracing facade — DESIGN.md §3g component 44.

Imported lazily by `src/tracing.py` ONLY when `OPIK_API_KEY` is set, so the
`opik` package (26 transitive deps incl. litellm) never has to be installed for
the app to run. An import failure here degrades to "no Opik backend", never to
a broken app.

Opik's model is trace -> spans, which maps directly onto the facade's records.
Spans are buffered per trace and flushed when the ROOT span ends, because Opik
wants a trace to exist before its children.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger(__name__)


class OpikBackend:
    def __init__(self):
        import opik

        self._client = opik.Opik(
            project_name=config.OPIK_PROJECT_NAME or None,
            workspace=config.OPIK_WORKSPACE or None,
        )
        self._traces: dict[str, object] = {}

    def start(self, rec: dict) -> None:
        # Root span opens the Opik trace; children attach to it on end().
        if rec["parent"] is None and rec["trace_id"] not in self._traces:
            self._traces[rec["trace_id"]] = self._client.trace(
                name=rec["name"], input=dict(rec["attrs"]))

    def end(self, rec: dict) -> None:
        trace = self._traces.get(rec["trace_id"])
        if trace is None:
            return
        trace.span(name=rec["name"], input=dict(rec["attrs"]),
                   output={"error": rec["error"]} if rec["error"] else {})
        if rec["parent"] is None:
            # Root finished: close the trace and stop tracking it, otherwise
            # a long-lived process accumulates one entry per request forever.
            try:
                trace.end(output={"duration_ms": rec["duration_ms"],
                                  "error": rec["error"]})
            finally:
                self._traces.pop(rec["trace_id"], None)
