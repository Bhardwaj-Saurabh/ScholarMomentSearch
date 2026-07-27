"""Sample-corpus seeding — run to completion BEFORE the app serves.

This is the startup gate: seed.py runs it as a one-shot container that must
exit 0 before api/worker start (docker-compose depends_on ...
service_completed_successfully), so the UI is never reachable with a
half-indexed corpus. Idempotent and durable (Qdrant Cloud): once the four
talks are indexed they stay indexed, so every later start finishes in seconds.

Assignment 3 (component 10): alongside those four, also seeds the 8 aligned
research triplets in benchmark/corpus.json (SEED_CORPUS=true, the default) —
24 sources total (8 talks + 8 papers + 8 decks), so a fresh deploy answers a
cross-source question immediately. Documents get a DETERMINISTIC seed id
(doc_seed_<corpus_id>_<kind>), not the random uuid4 the admin API mints for
user registrations — a re-run must target the SAME row to stay idempotent.
"""
from __future__ import annotations

import time
import urllib.request

from . import config, db
from .ingest.doc_pipeline import ingest_document
from .ingest.pipeline import ingest_video
from .rag import vector_store
from .samples import CORPUS, SAMPLE_VIDEOS, sample_video_id

_MAX_PASSES = 3  # re-attempt sources that fail (e.g. a transient network hiccup)


def wait_for_clip(timeout: int = 600) -> None:
    """Block until the CLIP service answers /healthz — first boot downloads the
    model (~600MB). No-op when embedding is in-process (CLIP_SERVICE_URL unset)."""
    if not config.CLIP_SERVICE_URL:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(config.CLIP_SERVICE_URL + "/healthz", timeout=5) as r:
                if r.status == 200:
                    print("[seed] CLIP service ready", flush=True)
                    return
        except Exception:
            pass
        print("[seed] waiting for CLIP service to warm up…", flush=True)
        time.sleep(5)
    print("[seed] CLIP service not ready in time — attempting anyway", flush=True)


def _all_videos() -> list[dict]:
    """Base sample talks + (if enabled) the 8 corpus talks — one combined
    seeding pass, per DESIGN.md's "alongside the sample videos" instruction."""
    videos = list(SAMPLE_VIDEOS)
    if config.SEED_CORPUS:
        videos += [{"url": t["video_url"], "title": t["paper_title"]}
                  for t in CORPUS if t.get("video_url")]
    return videos


def _not_indexed_videos() -> list[dict]:
    out = []
    for v in _all_videos():
        row = db.get_video(sample_video_id(v["url"]))
        if (row or {}).get("status") != "indexed":
            out.append(v)
    return out


def _seed_doc_id(corpus_id: str, kind: str) -> str:
    """Deterministic — a re-run must target the SAME row (idempotency), unlike
    the admin API's random uuid4 for one-off user registrations."""
    return f"doc_seed_{corpus_id}_{kind}"


def _corpus_documents() -> list[dict]:
    """The 16 seed documents (paper + deck per triplet), or [] if disabled."""
    if not config.SEED_CORPUS:
        return []
    docs = []
    for t in CORPUS:
        docs.append({"id": _seed_doc_id(t["id"], "paper"), "kind": "paper",
                     "uri": t["paper_pdf"], "title": t["paper_title"]})
        docs.append({"id": _seed_doc_id(t["id"], "deck"), "kind": "deck",
                     "uri": t["deck_pdf"], "title": t["deck_note"]})
    return docs


def _not_indexed_documents() -> list[dict]:
    out = []
    for d in _corpus_documents():
        row = db.get_document(d["id"])
        if (row or {}).get("status") != "indexed":
            out.append(d)
    return out


def seed_to_completion() -> bool:
    """Index every sample video and (if enabled) every corpus paper/deck,
    retrying failures. Returns True iff EVERYTHING ends up indexed. Blocking —
    the caller (seed.py) gates the app on this."""
    if not config.SEED_SAMPLE_VIDEOS:
        print("[seed] SEED_SAMPLE_VIDEOS=false — skipping", flush=True)
        return True

    db.init_schema()
    vector_store.ensure_collection()
    if config.SEED_CORPUS:
        vector_store.ensure_text_collection()

    # Light sampling for the demo corpus so videos finish quickly on CPU (the
    # Karpathy talk is 1h). User uploads run in separate Prefect subprocesses
    # that re-read config, so they keep full quality.
    config.MAX_FRAMES = min(config.MAX_FRAMES, 60)
    config.FRAME_INTERVAL_SEC = max(config.FRAME_INTERVAL_SEC, 5.0)

    wait_for_clip()

    if not _not_indexed_videos() and not _not_indexed_documents():
        print("[seed] everything already indexed — ready", flush=True)
        return True

    for attempt in range(1, _MAX_PASSES + 1):
        todo_v, todo_d = _not_indexed_videos(), _not_indexed_documents()
        if not todo_v and not todo_d:
            break
        print(f"[seed] pass {attempt}/{_MAX_PASSES}: "
             f"{len(todo_v)} video(s), {len(todo_d)} document(s)", flush=True)
        for v in todo_v:
            vid = sample_video_id(v["url"])
            print(f"[seed] -> {vid}: {v['title']}", flush=True)
            db.upsert_pending({"id": vid, "user_id": config.DEFAULT_USER_ID,
                               "source": "youtube", "url": v["url"],
                               "storage_key": None, "source_hash": vid,
                               "title": v["title"]})
            try:
                ingest_video(video_id=vid, user_id=config.DEFAULT_USER_ID)
            except Exception as exc:
                print(f"[seed] {vid} failed ({type(exc).__name__}: {exc})", flush=True)
        for d in todo_d:
            print(f"[seed] -> {d['id']} ({d['kind']}): {d['title']}", flush=True)
            db.upsert_pending_document({"id": d["id"], "user_id": config.DEFAULT_USER_ID,
                                       "kind": d["kind"], "uri": d["uri"],
                                       "storage_key": None, "source_hash": None,
                                       "title": d["title"]})
            try:
                ingest_document(doc_id=d["id"], user_id=config.DEFAULT_USER_ID, kind=d["kind"])
            except Exception as exc:
                print(f"[seed] {d['id']} failed ({type(exc).__name__}: {exc})", flush=True)

    remaining_v, remaining_d = _not_indexed_videos(), _not_indexed_documents()
    if remaining_v or remaining_d:
        names = ", ".join([sample_video_id(v["url"]) for v in remaining_v]
                          + [d["id"] for d in remaining_d])
        print(f"[seed] STILL not indexed after {_MAX_PASSES} passes: {names}", flush=True)
        return False
    print("[seed] corpus complete — everything indexed", flush=True)
    return True
