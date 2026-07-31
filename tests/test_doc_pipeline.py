"""Component 4 (DESIGN.md) — the ms-ingest-document Prefect flow
(src/ingest/doc_pipeline.py): fetch -> parse -> caption -> embed-index, with
the crash-safe ordering DESIGN.md calls out by name: status flips to 'indexed'
only AFTER the Qdrant upsert returns.

Real where it matters, mocked at the true external boundaries:
  - Postgres: real (throwaway container, see tests/conftest.py) — the status
    lifecycle IS the thing being proven, mocking it would prove nothing.
  - Qdrant: real, embedded on-disk mode (no server/cloud key needed) — proves
    the actual upsert/payload/ID-scheme wiring, not a mock's approximation.
  - Embeddings: real fastembed (bge-small, ONNX, CPU, no API key) — first call
    downloads the model (~13s one-time; see EVIDENCE.md), cached after.
  - Object storage: real, local provider (writes under ./data/, gitignored;
    fixtures clean up their own keys).
  - Network fetch (_download) and the vision LLM (llm.py): mocked. An arbitrary
    external URL and a paid LLM API are the two genuine "don't call this for
    real in a unit test" boundaries (tdd skill).

SLA relevance: this component IS the crash-safety story (benchmark/sla.json's
"no_loss_required" gate) and feeds ingestion throughput once wired to the queue
(component 5) and recall@10 once wired to search (component 7).
"""
from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path

import fitz
import pytest

from src import db, storage
from src.ingest import doc_pipeline
from src.llm import LLMConfig
from src.rag import vector_store


@pytest.fixture(autouse=True)
def _use_prefect_harness(prefect_harness):
    pass


@pytest.fixture(autouse=True)
def _schema():
    db.init_schema()
    vector_store.ensure_text_collection()


@pytest.fixture
def cleanup():
    ids: dict[str, list[str]] = {"documents": [], "storage_keys": []}
    yield ids
    for i in ids["documents"]:
        db.delete_document(i)
        vector_store.delete_document_chunks("u_doc_test", i)
    for k in ids["storage_keys"]:
        storage.delete_key(k)


def _doc_id() -> str:
    return f"doc_{uuid.uuid4().hex[:10]}"


def _register(doc_id, kind="paper", uri="https://arxiv.org/pdf/1706.03762",
             user_id="u_doc_test", title="Attention Is All You Need"):
    return db.upsert_pending_document({
        "id": doc_id, "user_id": user_id, "kind": kind, "uri": uri,
        "storage_key": None, "source_hash": None, "title": title,
    })


# ── Fixture builders: tiny real PDFs (component 2/3 style) ──────────────────

def _build_pdf_paper(path, pages_lines):
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=10)
            y += 14
    doc.save(str(path))
    doc.close()
    return path


def _build_pdf_deck(path, slides_lines):
    doc = fitz.open()
    for lines in slides_lines:
        page = doc.new_page()
        y = 72
        for line in (lines or []):
            page.insert_text((72, y), line, fontsize=14)
            y += 18
    doc.save(str(path))
    doc.close()
    return path


# ── _download (the one real network boundary) ───────────────────────────────

def _fake_http(monkeypatch, body=b"%PDF-1.4 fake paper bytes for the download test",
               content_type="application/pdf"):
    """Stub the network at urlguard's seam (component 24 routes _download
    through it instead of calling urllib directly) and make DNS resolve to a
    public IP so the SSRF guard lets the fetch proceed."""
    import socket

    from src.ingest import urlguard

    class _FakeResponse:
        def __init__(self):
            self._buf = io.BytesIO(body)
            self.status = 200

        def getheader(self, name, default=None):
            return content_type if name == "Content-Type" else default

        def read(self, n=-1):
            return self._buf.read(n)

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResponse())


def test_download_writes_scratch_file_from_url(monkeypatch, tmp_path):
    _fake_http(monkeypatch)
    dest = doc_pipeline._download("https://example.com/probe.pdf", "doc_dl_test")
    assert dest.exists()
    assert dest.suffix == ".pdf"
    assert dest.read_bytes() == b"%PDF-1.4 fake paper bytes for the download test"
    dest.unlink(missing_ok=True)


def test_download_picks_pptx_extension_from_content_type(monkeypatch):
    """Regression guard for component 24's rewrite: deck.parse_deck dispatches
    on the file SUFFIX and raises on anything but .pdf/.pptx, so a URL with no
    usable suffix that serves a PPTX must still be named .pptx. Deriving the
    extension from the URL path alone would have silently broken this."""
    _fake_http(monkeypatch, body=b"PK\x03\x04 fake pptx",
               content_type="application/vnd.openxmlformats-officedocument."
                            "presentationml.presentation")
    dest = doc_pipeline._download("https://example.com/talk", "doc_pptx_test")
    assert dest.suffix == ".pptx"
    dest.unlink(missing_ok=True)


def test_download_refuses_internal_address(monkeypatch):
    """The SSRF guard is wired into the real ingest path, not just unit-tested
    in isolation (component 24)."""
    from src.ingest import urlguard

    with pytest.raises(urlguard.BlockedUrlError):
        doc_pipeline._download("http://169.254.169.254/latest/meta-data/", "doc_ssrf_test")


# ── t_fetch: dispatch, hash + persist, duplicate detection ──────────────────

def test_t_fetch_dispatches_by_storage_key_vs_uri(monkeypatch, cleanup):
    calls = []
    monkeypatch.setattr(doc_pipeline, "_download",
                        lambda uri, doc_id: calls.append(("download", uri)))
    monkeypatch.setattr(doc_pipeline.fetch_mod, "fetch_upload",
                        lambda key, doc_id: calls.append(("upload", key)))
    # storage_key present -> fetch_upload path, even though this raises (no
    # real key exists) we only care which branch got entered.
    doc_a = _doc_id()
    cleanup["documents"].append(doc_a)
    row = _register(doc_a, uri=None)
    db.upsert_pending_document({**row, "storage_key": "docs/u/x.pdf"})
    with pytest.raises(Exception):
        doc_pipeline.t_fetch.fn(doc_a, "u_doc_test")
    assert calls and calls[0][0] == "upload"


def test_t_fetch_persists_and_sets_hash(monkeypatch, cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    content = b"%PDF-1.4 real content for the persist test"
    _register(doc_id)

    def _fake_download(uri, did):
        dest = doc_pipeline.fetch_mod.scratch_dir() / f"{did}.pdf"
        dest.write_bytes(content)
        return dest

    monkeypatch.setattr(doc_pipeline, "_download", _fake_download)
    path = doc_pipeline.t_fetch.fn(doc_id, "u_doc_test")
    assert path  # not a duplicate

    row = db.get_document(doc_id)
    assert row["source_hash"] == hashlib.sha256(content).hexdigest()
    assert row["storage_key"]
    cleanup["storage_keys"].append(row["storage_key"])
    assert storage.get_bytes(row["storage_key"]) == content
    Path(path).unlink(missing_ok=True)


def test_t_fetch_detects_duplicate_and_skips(monkeypatch, cleanup):
    content = b"%PDF-1.4 identical content shared by both docs"
    doc_a, doc_b = _doc_id(), _doc_id()
    cleanup["documents"] += [doc_a, doc_b]

    def _fake_download(uri, did):
        dest = doc_pipeline.fetch_mod.scratch_dir() / f"{did}.pdf"
        dest.write_bytes(content)
        return dest

    monkeypatch.setattr(doc_pipeline, "_download", _fake_download)

    _register(doc_a)
    path_a = doc_pipeline.t_fetch.fn(doc_a, "u_doc_test")
    db.set_document_status(doc_a, "indexed")  # only indexed docs count as dup targets
    row_a = db.get_document(doc_a)
    cleanup["storage_keys"].append(row_a["storage_key"])
    Path(path_a).unlink(missing_ok=True)

    _register(doc_b)
    path_b = doc_pipeline.t_fetch.fn(doc_b, "u_doc_test")
    assert path_b == ""
    row_b = db.get_document(doc_b)
    assert row_b["status"] == "skipped"
    assert doc_a in row_b["error"]


# ── t_parse: kind branching, locator correctness ─────────────────────────────

def test_t_parse_paper_produces_page_locator_chunks(tmp_path, cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="paper")
    pdf = _build_pdf_paper(tmp_path / "p.pdf", [
        ["First page body text about attention mechanisms."],
        ["Second page body text about training procedure."],
    ])
    chunks = doc_pipeline.t_parse.fn(doc_id, "u_doc_test", str(pdf), "paper")
    assert all(c["locator_key"] == "page" for c in chunks)
    assert [c["locator"] for c in chunks] == [1, 2]
    assert "attention mechanisms" in chunks[0]["text"]
    assert all(c["needs_caption"] is False for c in chunks)


def test_t_parse_deck_produces_slide_locator_chunks(tmp_path, cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="deck")
    pdf = _build_pdf_deck(tmp_path / "d.pdf", [
        ["Slide one has plenty of real content, well over the thin threshold."],
        None,  # blank -> needs_caption
    ])
    chunks = doc_pipeline.t_parse.fn(doc_id, "u_doc_test", str(pdf), "deck")
    assert all(c["locator_key"] == "slide" for c in chunks)
    assert [c["locator"] for c in chunks] == [1, 2]
    assert chunks[0]["needs_caption"] is False
    assert chunks[1]["needs_caption"] is True
    assert chunks[1]["image_jpeg"]


def test_t_parse_unknown_kind_raises(tmp_path, cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="podcast")
    pdf = _build_pdf_paper(tmp_path / "x.pdf", [["some text"]])
    with pytest.raises(ValueError):
        doc_pipeline.t_parse.fn(doc_id, "u_doc_test", str(pdf), "podcast")


# ── t_caption: best-effort vision captioning ─────────────────────────────────

def test_t_caption_noop_without_llm_configured():
    chunks = [{"locator_key": "slide", "locator": 1, "text": "", "section": None,
              "needs_caption": True, "image_jpeg": b"fakejpeg"}]
    out = doc_pipeline.t_caption.fn("doc_x", "u_doc_test", chunks)
    assert out[0]["text"] == ""  # no LLM configured in this test env -> untouched


def test_t_caption_injects_caption_text_when_available(monkeypatch):
    monkeypatch.setattr(doc_pipeline.llm, "env_config", lambda: LLMConfig(model="gpt-4o-mini"))
    monkeypatch.setattr(doc_pipeline.llm, "caption_image",
                        lambda jpeg, cfg: "A bar chart showing accuracy versus epoch.")
    chunks = [{"locator_key": "slide", "locator": 1, "text": "", "section": None,
              "needs_caption": True, "image_jpeg": b"fakejpeg"}]
    out = doc_pipeline.t_caption.fn("doc_x", "u_doc_test", chunks)
    assert "bar chart" in out[0]["text"]


def test_t_caption_swallows_caption_failure(monkeypatch):
    monkeypatch.setattr(doc_pipeline.llm, "env_config", lambda: LLMConfig(model="gpt-4o-mini"))

    def _boom(jpeg, cfg):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(doc_pipeline.llm, "caption_image", _boom)
    chunks = [{"locator_key": "slide", "locator": 1, "text": "thin", "section": None,
              "needs_caption": True, "image_jpeg": b"fakejpeg"}]
    out = doc_pipeline.t_caption.fn("doc_x", "u_doc_test", chunks)  # must not raise
    assert out[0]["text"] == "thin"


# ── t_embed_index: real Qdrant round-trip + the crash-safety invariant ──────

def test_t_embed_index_real_roundtrip_and_payload_shape(cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="paper", title="Attention Is All You Need")
    chunks = [
        {"locator_key": "page", "locator": 1, "text": "Attention lets models weigh context.",
         "section": "Abstract", "needs_caption": False, "image_jpeg": None},
        {"locator_key": "page", "locator": 2, "text": "The encoder stacks self-attention layers.",
         "section": "2 Method", "needs_caption": False, "image_jpeg": None},
    ]
    n = doc_pipeline.t_embed_index.fn(doc_id, "u_doc_test", "paper", chunks)
    assert n == 2
    row = db.get_document(doc_id)
    assert row["status"] == "indexed"
    assert row["chunk_count"] == 2

    from qdrant_client.http import models as qm

    from src.config import TEXT_COLLECTION
    hits, _ = vector_store.client().scroll(
        collection_name=TEXT_COLLECTION,
        scroll_filter=qm.Filter(must=[
            qm.FieldCondition(key="source_id", match=qm.MatchValue(value=doc_id))]),
        limit=10, with_payload=True,
    )
    assert len(hits) == 2
    payloads = {h.payload["page"]: h.payload for h in hits}
    assert payloads[1]["kind"] == "paper"
    assert payloads[1]["user_id"] == "u_doc_test"
    assert payloads[1]["section"] == "Abstract"
    assert "embed_version" in payloads[1]


def test_t_embed_index_crash_safety_status_not_indexed_on_upsert_failure(monkeypatch, cleanup):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="paper")
    chunks = [{"locator_key": "page", "locator": 1, "text": "some text", "section": None,
              "needs_caption": False, "image_jpeg": None}]

    def _boom(*a, **k):
        raise RuntimeError("simulated worker crash mid-upsert")

    monkeypatch.setattr(vector_store, "upsert_document_chunks", _boom)
    with pytest.raises(RuntimeError):
        doc_pipeline.t_embed_index.fn(doc_id, "u_doc_test", "paper", chunks)

    row = db.get_document(doc_id)
    assert row["status"] == "embedding"  # never reached 'indexed'
    assert row["chunk_count"] is None


# ── ingest_document: the full flow, success and failure paths ──────────────

def test_ingest_document_flow_success_end_to_end(monkeypatch, cleanup, tmp_path):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="paper")
    pdf = _build_pdf_paper(tmp_path / "full.pdf", [
        ["This paper studies attention mechanisms in sequence models."],
    ])
    content = pdf.read_bytes()

    def _fake_download(uri, did):
        dest = doc_pipeline.fetch_mod.scratch_dir() / f"{did}.pdf"
        dest.write_bytes(content)
        return dest

    monkeypatch.setattr(doc_pipeline, "_download", _fake_download)
    result = doc_pipeline.ingest_document(doc_id, "u_doc_test", "paper")
    assert result["chunks"] >= 1

    row = db.get_document(doc_id)
    assert row["status"] == "indexed"
    assert row["storage_key"]
    cleanup["storage_keys"].append(row["storage_key"])


def test_ingest_document_flow_sets_failed_on_error(monkeypatch, cleanup, tmp_path):
    doc_id = _doc_id()
    cleanup["documents"].append(doc_id)
    _register(doc_id, kind="paper")
    pdf = _build_pdf_paper(tmp_path / "boom.pdf", [["irrelevant"]])
    content = pdf.read_bytes()

    def _fake_download(uri, did):
        dest = doc_pipeline.fetch_mod.scratch_dir() / f"{did}.pdf"
        dest.write_bytes(content)
        return dest

    def _boom_parse(path):
        raise RuntimeError("corrupt PDF (simulated)")

    monkeypatch.setattr(doc_pipeline, "_download", _fake_download)
    monkeypatch.setattr(doc_pipeline.paper_mod, "parse_pdf", _boom_parse)

    with pytest.raises(RuntimeError):
        doc_pipeline.ingest_document(doc_id, "u_doc_test", "paper")

    row = db.get_document(doc_id)
    assert row["status"] == "failed"
    assert "corrupt PDF" in row["error"]


def test_t_fetch_missing_row_is_a_clean_noop_not_a_retrying_failure():
    """Component 56 (DESIGN.md §3m): a missing manifest row means the document
    was deleted after acceptance (bench probes do this by design). The old
    ValueError made Prefect retry the task twice, 30-120s apart — pure waste
    for a row that will never come back. It must behave like the duplicate
    case instead: return "" so the flow exits as a no-op."""
    assert doc_pipeline.t_fetch.fn("doc_never_existed_xyz", "u_doc_test") == ""
