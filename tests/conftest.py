"""Test env bootstrap. Points DATABASE_URL at a local throwaway Postgres
(see CLAUDE.md / EVIDENCE.md for how to start one) and Qdrant's embedded mode
at a throwaway on-disk path, unless already set — this must run before any
test module imports src.config/src.db/src.rag.vector_store, since config.py's
load_dotenv() never overrides an already-set env var.
"""
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://postgres:test@localhost:55432/momentsearch_test",
)
os.environ.setdefault(
    "QDRANT_LOCAL_PATH",
    str(Path(tempfile.gettempdir()) / "momentsearch_test_qdrant"),
)


@pytest.fixture(scope="session")
def prefect_harness():
    """Session-scoped: pays Prefect's ~10s local-server bootstrap once, only
    for test modules that actually request it (component 4+)."""
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        yield
