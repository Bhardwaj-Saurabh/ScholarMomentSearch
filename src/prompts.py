"""Prompt & data versioning — DESIGN.md §3g component 47.

The gap this closes: `benchmark/answer_quality.py` reported faithfulness 0.96
and relevancy 5.0, and those numbers were attributable to **nothing**. Edit the
answer prompt and they silently become uncomparable to any earlier run, with no
record that anything changed.

**Versions are content hashes, never declared constants.** A hand-bumped
`PROMPT_VERSION` relies on someone remembering, and a forgotten bump reports
"same version" across genuinely different prompts — worse than no versioning,
because it looks trustworthy. Hashing the text makes the version impossible to
forget and impossible to get wrong.

**The registry points at the live text, it does not copy it.** `answer` is
registered as `llm.SYSTEM` itself, so a registry that has drifted from the
prompt actually being sent is not a state this module can reach. A test asserts
the identity too, because that property is the whole basis for trusting the
version.

Data versioning extends what already exists (`EMBED_VERSION`,
`TEXT_EMBED_VERSION`) with a **chunker version** derived from the parser
sources: component 14 changed paper chunking (table + figure extraction) and
nothing recorded it, so chunks written before and after that change are
indistinguishable in the index.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

_VERSION_LEN = 12      # 48 bits of sha256 — ample for distinguishing revisions


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:_VERSION_LEN]


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str

    @property
    def version(self) -> str:
        return _hash(self.text)


# Prompts registered at runtime by code that isn't part of the serving path —
# see register(). The benchmark's judge prompt lives here when the benchmark is
# what's running.
_extra: dict[str, Prompt] = {}


@lru_cache
def _app_prompts() -> dict[str, Prompt]:
    """The SERVING path's prompts only.

    Deliberately does NOT import `benchmark.*`. The first cut registered the
    judge prompt from `benchmark/answer_quality.py`, which worked locally and
    silently produced an EMPTY registry inside the Docker image — the Dockerfile
    copies `src/`, `ui/` and `benchmark/corpus.json`, so that module isn't
    there. The broad except below it turned a missing import into "no prompt
    versions", i.e. exactly the looks-trustworthy-but-isn't failure this module
    exists to prevent. Caught live in the container, not by the unit tests.

    Built lazily, and the registered objects ARE the live prompt strings — so a
    registry that has drifted from the text actually sent is unreachable.
    """
    from . import llm
    from .rag import query_enhance

    return {
        "answer": Prompt("answer", llm.SYSTEM),
        "query_enhance": Prompt("query_enhance", query_enhance._SYSTEM),
    }


def register(name: str, text: str) -> Prompt:
    """Add a prompt owned by non-serving code (the benchmark's LLM judge).
    Called by that code, where it actually exists."""
    p = Prompt(name, text)
    _extra[name] = p
    return p


def _registry() -> dict[str, Prompt]:
    return {**_app_prompts(), **_extra}


def get(name: str) -> Prompt:
    """The registered prompt. Raises rather than inventing a version for an
    unknown name — a fabricated version is the failure this module prevents."""
    reg = _registry()
    if name not in reg:
        raise KeyError(f"unregistered prompt {name!r}; known: {sorted(reg)}")
    return reg[name]


@lru_cache
def chunker_version() -> str:
    """Version of the code that decides chunk boundaries.

    Hashed from the parser sources rather than a constant, for the same reason
    prompts are: component 14 changed `paper.py`'s chunking and no constant got
    bumped. A file that cannot be read is skipped rather than failing — a
    provenance helper must never break the read path.
    """
    here = Path(__file__).resolve().parent
    parts: list[str] = []
    for rel in ("ingest/paper.py", "ingest/deck.py", "ingest/transcript.py"):
        try:
            parts.append((here / rel).read_text())
        except Exception:
            parts.append(f"<unreadable:{rel}>")
    return _hash("\n".join(parts))


def versions() -> dict:
    """Full provenance bundle: which prompts and which data produced a result.

    Goes onto spans (component 45), into the `/ask` payload, and into Opik
    experiment metadata (component 48), so an eval score is attributable to an
    exact prompt AND exact data rather than to "whatever was checked out".
    """
    # No blanket try/except around the registry any more. The first version had
    # one, and it converted a missing module into a silently empty prompt map
    # that shipped to the container looking fine. The registry now imports only
    # `src.*`, which is always present wherever this code runs, so a failure
    # here is a genuine bug and should surface rather than be swallowed.
    prompt_versions = {n: p.version for n, p in _registry().items()}
    return {
        "prompts": prompt_versions,
        "embed_version": config.EMBED_VERSION,
        "text_embed_version": config.TEXT_EMBED_VERSION,
        "chunker_version": chunker_version(),
    }
