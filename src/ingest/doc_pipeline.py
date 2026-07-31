"""Per-document ingest pipeline — a Prefect flow of stage-tasks, mirroring
src/ingest/pipeline.py's ingest_video (DESIGN.md component 4).

pending -> fetching -> parsing -> embedding -> indexed | skipped | failed

Stages:
  1. fetch    acquire the source into worker scratch (HTTP download for a
              uri, bucket download for a storage:// ref), hash it, skip
              duplicates, then persist a durable copy to object storage so
              the citation's uri still resolves after the original rotates
              or 404s (papers/decks are far less stable than YouTube).
  2. parse    paper.parse_pdf or deck.parse_deck by kind -> normalized chunks
              carrying the citation locator (page or slide).
  3. caption  deck slides too text-thin to embed alone get a vision-LLM
              caption. Best-effort: a caption failure never fails the flow,
              same philosophy as the video pipeline's transcript branch.
  4. embed    bge-embed each chunk's text -> idempotent Qdrant upsert into the
              SAME text collection video transcripts use (moments_text),
              tagged kind + locator -> the shared cross-source text space.

Orchestration: Prefect, exactly like ingest_video — same queue, same per-task
retry policy. Crash-safe ordering: status flips to 'indexed' only AFTER the
Qdrant upsert returns, never before — a worker killed mid-run leaves the row
in a non-terminal state instead of a false positive (DESIGN.md component 4).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from prefect import flow, task

from .. import db, graph, llm, storage, trace_link, tracing
from ..config import TEXT_EMBED_VERSION
from ..rag import vector_store
from ..rag.embeddings import embed_docs
from . import deck as deck_mod
from . import fetch as fetch_mod
from . import paper as paper_mod
from . import urlguard

_DOWNLOAD_TIMEOUT_S = 60
_ALLOWED_EXTS = (".pdf", ".pptx")
# Explicit, platform-independent Content-Type -> extension map (see _download).
_CTYPE_EXT = {
    "application/pdf": ".pdf",
    "application/x-pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".pptx",
}


def _download(uri: str, doc_id: str) -> Path:
    """A paper/deck named by an http(s) URL -> worker scratch file.

    The URI comes straight from a `POST /admin/documents` caller, so the fetch
    goes through `urlguard` (DESIGN.md §3e component 24) rather than a bare
    urlopen: scheme + resolved-IP allowlisting, per-hop redirect re-validation,
    a streaming size cap, and content-type enforcement. Without it this is an
    SSRF whose response body gets parsed, embedded into the caller's tenant,
    and read back via /api/ask.

    Extension selection keeps the ORIGINAL precedence (Content-Type first,
    URL suffix second, .pdf last) because `deck.parse_deck` dispatches on the
    file suffix and raises on anything but .pdf/.pptx — a suffix-less URL
    serving a PPTX would break if we only looked at the path. The mapping is
    an explicit table rather than `mimetypes.guess_extension`, whose result
    for the OOXML presentation type depends on the platform's MIME database.
    """
    tmp = fetch_mod.scratch_dir() / f"{doc_id}.part"
    fetched = urlguard.download_to(uri, tmp, timeout=_DOWNLOAD_TIMEOUT_S)

    ctype = (fetched.content_type or "").split(";")[0].strip().lower()
    ext = _CTYPE_EXT.get(ctype, "")
    if ext not in _ALLOWED_EXTS:
        ext = Path(urlparse(uri).path).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        ext = ".pdf"

    dest = fetch_mod.scratch_dir() / f"{doc_id}{ext}"
    fetched.path.replace(dest)
    return dest


@task(name="doc-fetch", retries=2, retry_delay_seconds=[30, 120])
def t_fetch(doc_id: str, user_id: str) -> str:
    """Source document -> worker scratch file; duplicate check via
    source_hash; a persisted copy in object storage on success.

    Returns "" when the content is a duplicate of an already-indexed document
    for this user (row marked 'skipped' — a plain outcome, not a retryable
    error, mirroring the video pipeline's t_fetch)."""
    db.set_document_status(doc_id, "fetching")
    row = db.get_document(doc_id)
    if row is None:
        # The document was deleted after acceptance (bench's accept-latency
        # probes DELETE immediately after their 202; real users can too). The
        # row will never come back, so raising here only made Prefect retry a
        # hopeless task twice, 30-120s apart. Same clean no-op exit as the
        # duplicate case (component 56, DESIGN.md §3m).
        print(f"[ingest] {doc_id} manifest row gone (deleted after accept) — nothing to do")
        return ""

    if row.get("storage_key"):
        path = fetch_mod.fetch_upload(row["storage_key"], doc_id)
    else:
        path = _download(row["uri"], doc_id)
    source_hash = fetch_mod.sha256_file(path)

    dup = db.find_duplicate_document(user_id, source_hash, exclude_id=doc_id)
    if dup:
        path.unlink(missing_ok=True)
        db.set_document_status(doc_id, "skipped", error=f"duplicate of {dup['id']}",
                               source_hash=source_hash)
        return ""

    key = storage.doc_key(user_id, doc_id, path.suffix)
    storage.upload_file(path, key, "application/pdf" if path.suffix == ".pdf" else
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    # One write, not two (component 57): storage_key and source_hash land in
    # the same UPDATE.
    db.set_document_status(doc_id, "fetching", source_hash=source_hash, storage_key=key)
    return str(path)


@task(name="doc-parse", retries=1, retry_delay_seconds=30)
def t_parse(doc_id: str, user_id: str, path: str, kind: str) -> list[dict]:
    """PDF/PPTX -> normalized chunk dicts: {locator_key, locator, text,
    section, needs_caption, image_jpeg}. locator_key is 'page' for papers,
    'slide' for decks — the exact field name the citation payload carries."""
    # Component 57: the old progress=0.0 write here was a separate round trip
    # bracketing seconds of work — the per-kind write below already flips the
    # row to 'parsing', so the lifecycle (component 1) is unchanged.
    if kind == "paper":
        raw = paper_mod.parse_pdf(Path(path))
        chunks = [{"locator_key": "page", "locator": c.page, "text": c.text,
                  "section": c.section, "needs_caption": c.needs_caption,
                  "image_jpeg": c.image_jpeg}
                 for c in raw]
        db.set_document_status(doc_id, "parsing", page_count=raw[-1].page if raw else 0,
                               progress=1.0)
    elif kind == "deck":
        raw = deck_mod.parse_deck(Path(path))
        chunks = [{"locator_key": "slide", "locator": c.slide, "text": c.text,
                  "section": None, "needs_caption": c.needs_caption,
                  "image_jpeg": c.image_jpeg}
                 for c in raw]
        db.set_document_status(doc_id, "parsing", progress=1.0)
    else:
        raise ValueError(f"Unknown document kind: {kind!r} (expected paper or deck)")
    if not chunks:
        raise RuntimeError("No content could be extracted from the document.")
    return chunks


@task(name="doc-caption", retries=1, retry_delay_seconds=30)
def t_caption(doc_id: str, user_id: str, chunks: list[dict]) -> list[dict]:
    """Vision-caption slides too text-thin to embed alone. Best-effort: no LLM
    configured, or a provider error, never fails the flow — the chunk falls
    back to whatever thin text it had, same philosophy as t_transcript."""
    cfg = llm.env_config()
    if cfg is None:
        return chunks
    todo = [c for c in chunks if c["needs_caption"] and c.get("image_jpeg")]
    if not todo:
        return chunks

    def _caption_one(c: dict) -> None:
        try:
            caption = llm.caption_image(c["image_jpeg"], cfg)
            c["text"] = f"{c['text']} {caption}".strip()
        except Exception as exc:
            print(f"[doc-caption] {doc_id} {c['locator_key']} {c['locator']}: "
                 f"caption failed ({type(exc).__name__}: {exc}) — using extracted text only")

    # Component 57 (DESIGN.md §3m): the captions were the only genuinely
    # per-chunk network work in the flow, and they ran strictly serially — an
    # image-heavy deck paid one full vision round trip per slide. Bounded
    # fan-out; each caption still fails independently (same best-effort
    # contract as before, now proven by a dedicated test).
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_caption_one, todo))
    return chunks


@task(name="doc-embed-index", retries=2, retry_delay_seconds=60)
def t_embed_index(doc_id: str, user_id: str, kind: str, chunks: list[dict]) -> int:
    """Batched bge embeddings -> idempotent upsert into the shared text
    collection. Status flips to 'indexed' only AFTER the upsert returns —
    the crash-safety invariant DESIGN.md calls out by name."""
    db.set_document_status(doc_id, "embedding", progress=0.0)
    vector_store.ensure_text_collection()
    vector_store.delete_document_chunks(user_id, doc_id)  # drop stale points from prior runs

    texts = [c["text"] for c in chunks]
    vectors = embed_docs(texts)
    payloads = [
        {"user_id": user_id, "source_id": doc_id, "kind": kind,
         c["locator_key"]: c["locator"], "section": c.get("section"),
         "text": c["text"], "embed_version": TEXT_EMBED_VERSION}
        for c in chunks
    ]
    vector_store.upsert_document_chunks(user_id, doc_id, kind, vectors, payloads)
    db.set_document_status(doc_id, "indexed", chunk_count=len(chunks),
                           embed_version=TEXT_EMBED_VERSION, progress=1.0)

    # Component 50 (DESIGN.md §3i): index this document's entities for the
    # graph boost. Deliberately AFTER the status flip to 'indexed' — the
    # crash-safety invariant above is what the whole pipeline is built around,
    # and a graph write is not allowed to sit between the upsert and the flip.
    #
    # The WHOLE block is guarded, not just the graph write. `record_mentions`
    # swallows its own errors, but `db.get_document` does not — and this task
    # carries `retries=2`, so an exception raised here (i.e. AFTER the status is
    # already 'indexed') would make the retry redo the embed + upsert. That is
    # exactly the "retries must not redo finished stages" invariant in
    # CLAUDE.md §5. Found by spec-guardian; the earlier comment here asserted
    # this block could not fail an ingest, which was simply wrong.
    try:
        row = db.get_document(doc_id) or {}
        entities: list[str] = []
        seen: set[str] = set()
        for c in chunks:
            for e in graph.extract_entities(c.get("text"), title=row.get("title")):
                if e not in seen:
                    seen.add(e)
                    entities.append(e)
                if len(entities) >= graph.MAX_ENTITIES_PER_SOURCE:
                    break
            if len(entities) >= graph.MAX_ENTITIES_PER_SOURCE:
                break
        graph.record_mentions(user_id, doc_id, kind, entities)
    except Exception as exc:  # noqa: BLE001 -- see the invariant note above
        print(f"[ingest] {doc_id} graph indexing skipped: {type(exc).__name__}: {exc}")
    return len(chunks)


@flow(name="ms-ingest-document", log_prints=True, timeout_seconds=3600)
def ingest_document(doc_id: str, user_id: str, kind: str) -> dict:
    attempt = db.bump_document_attempts(doc_id)
    path: str | None = None
    # Component 46: join the trace the API started at registration, so
    # "registered -> fetched -> parsed -> captioned -> indexed" is ONE trace
    # across two processes. Absent (no Redis, expired, or a retry that already
    # consumed it) => an uncorrelated trace, never a failed ingest.
    linked = trace_link.pop(doc_id)
    try:
        with tracing.span("ingest_document", trace_id=linked, doc_id=doc_id,
                          tenant=user_id, kind=kind, attempt=attempt,
                          correlated=bool(linked)) as _sf:
            with tracing.span("doc_fetch") as _s1:
                path = t_fetch(doc_id, user_id)
                _s1.set_attrs(duplicate=not path)
            if not path:  # duplicate — already marked 'skipped' by t_fetch
                print(f"[ingest] {doc_id} skipped (duplicate content)")
                _sf.set_attrs(skipped=True)
                return {"doc_id": doc_id, "skipped": True}
            with tracing.span("doc_parse") as _s2:
                chunks = t_parse(doc_id, user_id, path, kind)
                _s2.set_attrs(chunks=len(chunks))
            with tracing.span("doc_caption") as _s3:
                chunks = t_caption(doc_id, user_id, chunks)
                _s3.set_attrs(chunks=len(chunks),
                              captioned=sum(1 for c in chunks
                                            if c.get("needs_caption")))
            with tracing.span("doc_embed_index") as _s4:
                n = t_embed_index(doc_id, user_id, kind, chunks)
                _s4.set_attrs(indexed=n)
            _sf.set_attrs(indexed=n)
            print(f"[ingest] {doc_id} indexed: {n} chunks (attempt {attempt})")
            return {"doc_id": doc_id, "chunks": n}
    except Exception as exc:
        db.set_document_status(doc_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise  # Prefect marks the run Failed; full trace in the Cloud UI
    finally:
        if path:  # scratch only — durable copy already persisted by t_fetch
            Path(path).unlink(missing_ok=True)
