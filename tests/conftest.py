"""Test env bootstrap. Points DATABASE_URL at a local throwaway Postgres
(see CLAUDE.md / EVIDENCE.md for how to start one) unless already set —
this must run before any test module imports src.config/src.db, since
config.py's load_dotenv() never overrides an already-set env var.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://postgres:test@localhost:55432/momentsearch_test",
)
