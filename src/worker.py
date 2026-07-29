"""Ingest worker entrypoint — serves both Prefect flow deployments (video +
document, DESIGN.md component 5) from one process.

    python -m src.worker

Registers "ms-ingest-video/ingest" and "ms-ingest-document/ingest" in Prefect
Cloud (idempotent) and long-polls both for scheduled runs — outbound HTTPS
only, no ports. Scale horizontally by running more replicas of this process;
WORKER_CONCURRENCY caps how many runs of EITHER kind this process executes at
once — one shared pool, not one per flow, since a VM's CPU/memory doesn't care
which pipeline is using it.

Sample seeding is NOT done here — it's a one-shot startup gate (seed.py /
src/seeding.py) that the whole stack waits on, so the app never serves a
half-indexed corpus. This worker only handles user uploads + YouTube/paper/
deck adds.

Embedding goes to the warm CLIP service when CLIP_SERVICE_URL is set
(docker-compose default); unset, each run loads the model in-process.
"""
import os
import time

from prefect import serve
from prefect.deployments.runner import EntrypointType

from .db import init_schema
from .ingest.doc_pipeline import ingest_document
from . import tracing
from .ingest.pipeline import ingest_video


def _build_deployments():
    """Both flows' deployments, sharing the "ingest" deployment name — the
    full identity (flow_name/ingest) is what jobs.py's INGEST_DEPLOYMENT and
    DOCUMENT_DEPLOYMENT constants point at.

    entrypoint_type=MODULE_PATH (not Prefect's default FILE_PATH): a worker
    executing a SCHEDULED run re-imports the flow fresh via the deployment's
    stored entrypoint. FILE_PATH loads the module as a bare script
    (importlib spec_from_file_location, no parent package) — both pipeline
    modules use relative imports (`from .. import db`), which then raise
    "ImportError: attempted relative import beyond top-level package". This
    silently never showed up before: seed.py calls ingest_video/
    ingest_document as plain in-process function calls, bypassing Prefect
    scheduling entirely, so this path was never exercised until a real
    worker executed a real scheduled run (Part 0). MODULE_PATH loads via
    normal importlib.import_module("src.ingest.doc_pipeline"), which keeps
    the module inside its real package and resolves relative imports fine.
    """
    # Component 46: video ingest gets a FLOW-LEVEL span only. Its tasks live in
    # CLAUDE.md-protected `src/ingest/pipeline.py`, so they cannot be wrapped
    # the way doc_pipeline's are — but the run itself is observable from a
    # Prefect state hook here, which is extendable. Reported with Prefect's own
    # start/end times via tracing.record(). Uncorrelated by design: the
    # registering endpoint is `videos.py`, also protected, so there is nowhere
    # additive to stash a trace context for the video path.
    traced_video = ingest_video.with_options(
        on_completion=[_video_flow_span], on_failure=[_video_flow_span])
    return [
        traced_video.to_deployment(name="ingest", entrypoint_type=EntrypointType.MODULE_PATH),
        ingest_document.to_deployment(name="ingest", entrypoint_type=EntrypointType.MODULE_PATH),
    ]


def _video_flow_span(flow, flow_run, state) -> None:
    """Prefect state hook -> one flow-level span for a video ingest run.
    Best-effort: a telemetry hook must never affect the run's outcome."""
    try:
        start = flow_run.start_time.timestamp() if flow_run.start_time else time.time()
        end = time.time()
        tracing.record(
            "ingest_video", start_ts=start, end_ts=end,
            error=None if state.is_completed() else str(state.type),
            flow_run_id=str(flow_run.id),
            video_id=(flow_run.parameters or {}).get("video_id"),
            tenant=(flow_run.parameters or {}).get("user_id"),
            state=str(state.type),
        )
    except Exception as exc:
        print(f"[worker] video flow span failed ({type(exc).__name__}: {exc}) — ignored")


def main():
    init_schema()  # make sure migrations ran before consuming runs
    from .rag import vector_store
    vector_store.ensure_collection()       # visual (CLIP) — video frames
    vector_store.ensure_text_collection()  # shared text — transcripts + papers + decks
    # Fair scheduler (WFQ): admits pending videos round-robin across users so
    # one bulk uploader can't starve everyone else (src/dispatcher.py). Videos
    # only — documents ride FIFO (DESIGN.md component 5's documented choice).
    from . import dispatcher
    dispatcher.start_in_background()
    # Crash recovery (Part 0 finding): a worker killed mid-flight leaves a
    # document stuck forever otherwise — see src/reconciler.py.
    from . import reconciler
    reconciler.start_in_background()
    limit = int(os.getenv("WORKER_CONCURRENCY", "2"))
    deployments = _build_deployments()
    # serve() talks to Prefect Cloud on startup; a transient outage (e.g. a 503)
    # used to crash the worker permanently and stop the machine. Self-heal:
    # retry forever so a blip pauses ingest instead of killing the worker.
    while True:
        try:
            names = ", ".join(f"'{d.full_name}'" for d in deployments)
            print(f"[worker] serving {names} (shared concurrency {limit})")
            serve(*deployments, limit=limit)
            break  # clean shutdown
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[worker] serve crashed: {type(exc).__name__}: {exc} — retrying in 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
