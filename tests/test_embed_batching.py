"""Component 57 follow-up (DESIGN.md §3m) — embed-service protection.

Found live: `embed_docs` sent EVERY chunk of a document to the clip service
in ONE request (a 189-chunk paper = one giant payload embedded in one go).
Six concurrent document ingests OOM-killed the 4GB clip machine
(exit_code=137, oom_killed=true — Fly event log, 2026-07-31), and during its
restart window `_post` didn't retry `RemoteDisconnected`/connection-reset
errors (only URLError), so in-flight ingests hard-failed instead of riding
out the ~60s restart the retry loop was built for.
"""
from __future__ import annotations

import pytest

from src import config
from src.rag import embeddings


@pytest.fixture(autouse=True)
def _remote_mode(monkeypatch):
    monkeypatch.setattr(config, "CLIP_SERVICE_URL", "http://clip:8001")


def test_embed_docs_splits_large_batches(monkeypatch):
    calls = []

    def _fake_post(path, payload, timeout=600):
        calls.append(len(payload["texts"]))
        return {"vectors": [[0.0] * 4 for _ in payload["texts"]]}

    monkeypatch.setattr(embeddings, "_post", _fake_post)
    texts = [f"chunk {i}" for i in range(189)]
    out = embeddings.embed_docs(texts)
    assert out.shape[0] == 189, "all chunks embedded, order preserved end-to-end"
    assert len(calls) > 1, "one giant request is what OOM-killed the clip machine"
    assert max(calls) <= embeddings.EMBED_DOCS_BATCH
    assert sum(calls) == 189


def test_embed_docs_small_batch_stays_one_call(monkeypatch):
    calls = []

    def _fake_post(path, payload, timeout=600):
        calls.append(len(payload["texts"]))
        return {"vectors": [[0.0] * 4 for _ in payload["texts"]]}

    monkeypatch.setattr(embeddings, "_post", _fake_post)
    embeddings.embed_docs(["a", "b", "c"])
    assert calls == [3]


def test_embed_docs_preserves_order_across_batches(monkeypatch):
    def _fake_post(path, payload, timeout=600):
        # Encode each text's index into its vector so order survives review.
        return {"vectors": [[float(t.split()[-1])] * 4 for t in payload["texts"]]}

    monkeypatch.setattr(embeddings, "_post", _fake_post)
    n = embeddings.EMBED_DOCS_BATCH + 7
    out = embeddings.embed_docs([f"chunk {i}" for i in range(n)])
    assert [int(v[0]) for v in out] == list(range(n))


def test_post_retries_connection_reset_during_service_restart(monkeypatch):
    """RemoteDisconnected is what callers actually saw while the OOM-killed
    clip machine rebooted — it must get the same ride-it-out retry treatment
    as URLError, not an instant task failure."""
    from http.client import RemoteDisconnected

    monkeypatch.setattr(embeddings.time, "sleep", lambda s: None)
    attempts = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=0):
        attempts.append(1)
        if len(attempts) < 3:
            raise RemoteDisconnected("Remote end closed connection without response")
        return _FakeResp()

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", _fake_urlopen)
    assert embeddings._post("/embed/docs", {"texts": ["x"]}) == {"ok": True}
    assert len(attempts) == 3
