"""The curated sample corpus — "A Deep Dive into LLMs" (the read-only / page)
— plus (Assignment 3) the 8 aligned research triplets in benchmark/corpus.json.

Four visually-rich LLM talks. The worker auto-ingests any that aren't indexed
when it boots (SEED_SAMPLE_VIDEOS=true, the default), so a fresh clone is
queryable the moment the stack comes up; examples/quickstart.py uses the same
list for the in-process route.
"""
from __future__ import annotations

import json
import re

from .config import ROOT

SAMPLE_VIDEOS = [
    {
        "url": "https://youtu.be/LPZh9BOjkQs",
        "title": "3Blue1Brown — LLMs explained briefly (8m)",
    },
    {
        "url": "https://youtu.be/wjZofJX0v4M",
        "title": "3Blue1Brown — Transformers, the tech behind LLMs (27m)",
    },
    {
        "url": "https://youtu.be/eMlx5fFNoYc",
        "title": "3Blue1Brown — Attention in transformers, step-by-step (26m)",
    },
    {
        "url": "https://youtu.be/zjkBMFhNj_g",
        "title": "Andrej Karpathy — [1hr Talk] Intro to Large Language Models (60m)",
    },
]


def sample_video_id(url: str) -> str:
    m = re.search(r"(?:youtu\.be/|v=)([\w-]{11})", url)
    return f"yt_{m.group(1)}" if m else url


def _load_corpus() -> list[dict]:
    """The 8 aligned research triplets (benchmark/corpus.json) — video + paper
    + deck for each. Read from the single source of truth the benchmark also
    uses, so seeding and grading never drift apart. Missing file -> empty
    list (dev convenience, matches how the app degrades elsewhere)."""
    path = ROOT / "benchmark" / "corpus.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("triplets", [])


CORPUS = _load_corpus()

# The four base sample ids + the 8 corpus videos — protected: they can be
# unselected from a query but never deleted (the seed gate would just re-add
# them anyway).
SAMPLE_IDS = frozenset(sample_video_id(v["url"]) for v in SAMPLE_VIDEOS) | frozenset(
    sample_video_id(t["video_url"]) for t in CORPUS if t.get("video_url"))


def is_sample(video_id: str) -> bool:
    return video_id in SAMPLE_IDS


def seed_doc_id(corpus_id: str, kind: str) -> str:
    """Deterministic — a re-run must target the SAME row (idempotency), unlike
    the admin API's random uuid4 for one-off user registrations. Single source
    of truth for src/seeding.py and the sample-protection set below."""
    return f"doc_seed_{corpus_id}_{kind}"


# Component 34 (DESIGN.md §3e): the 16 seeded paper/deck documents (8 corpus
# triplets x 2 kinds), protected the same way SAMPLE_IDS protects videos —
# deletable-looking but the seed gate would just re-add them on next boot.
SAMPLE_DOCUMENT_IDS = frozenset(
    seed_doc_id(t["id"], kind) for t in CORPUS for kind in ("paper", "deck"))


def is_sample_document(doc_id: str) -> bool:
    return doc_id in SAMPLE_DOCUMENT_IDS
