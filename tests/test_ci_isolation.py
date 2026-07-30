"""Component 41 (DESIGN.md §3e) — the test-isolation gap.

Real repro: `.env` (developer machine / CI secret) sets a real `QDRANT_URL`
pointing at the production Qdrant Cloud cluster. `conftest.py` sets
`QDRANT_LOCAL_PATH` via `os.environ.setdefault` so tests use an embedded,
throwaway Qdrant instance instead — but it never touches `QDRANT_URL` itself.
`src/config.py`'s unconditional `load_dotenv()` (override=False) still
populates `QDRANT_URL` from `.env` the first time `src.config` is imported,
and `src/rag/vector_store.py::client()` prefers `QDRANT_URL` over
`QDRANT_LOCAL_PATH` whenever it's set — so every "real Qdrant" test,
including the tenant-isolation regression test itself, silently ran against
the live production cluster instead of an isolated instance.

Some tests (hybrid search, cross-source search) genuinely need server-mode
Qdrant — payload indexes and sparse vectors have no effect in the embedded
client — so CI points QDRANT_URL at a disposable local container
(.github/workflows/ci.yml) rather than leaving it empty. The property this
guards is narrower than "always empty": QDRANT_URL must never resolve to the
real Qdrant Cloud cluster, whether that's empty (embedded fallback, the local
dev default) or a loopback address (a throwaway container, CI's setup).
"""
from __future__ import annotations

from src import config


def test_suite_never_points_at_a_real_qdrant_url():
    url = config.QDRANT_URL
    is_safe = url == "" or "://localhost" in url or "://127.0.0.1" in url
    assert is_safe, (
        f"QDRANT_URL={url!r} looks like a real (non-loopback) Qdrant instance. "
        "conftest.py must force it empty via os.environ.setdefault before "
        "src.config is first imported, or CI must point it at a disposable "
        "local container - never at the production cluster from .env."
    )
    assert "cloud.qdrant.io" not in url
