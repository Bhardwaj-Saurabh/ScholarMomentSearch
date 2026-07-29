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

**The registry RESOLVES the live text, it does not copy it.** `answer` reads
`llm.SYSTEM` through a resolver on every access, so a registry reporting a
version for text that was never sent is not a state this module can reach.
The first cut only *claimed* that while snapshotting the string under
`@lru_cache`; spec-guardian rebound `llm.SYSTEM` and showed the registry
happily reporting the stale hash. Now proven by a test that performs exactly
that rebind.

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
from typing import Callable

from . import config

_VERSION_LEN = 12      # 48 bits of sha256 — ample for distinguishing revisions


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:_VERSION_LEN]


@dataclass(frozen=True)
class Prompt:
    """A registered prompt. `text` may be a live lookup rather than a snapshot.

    spec-guardian demonstrated that the first version's claim — "a registry
    that has drifted from the text actually sent is unreachable" — was FALSE:
    `@lru_cache` snapshotted the `str` value, so rebinding `llm.SYSTEM` left the
    registry reporting the old hash (`69f1121dc865` while the live text hashed
    `cfe4f535e436`). Reading through a resolver makes the claim true by
    construction instead of by luck.
    """
    name: str
    # Either the literal text, or a zero-arg callable resolving it live.
    source: "str | Callable[[], str]" = ""

    @property
    def text(self) -> str:
        return self.source() if callable(self.source) else self.source

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

    Built lazily. The entries hold RESOLVERS, not copies, so the `@lru_cache`
    here caches the mapping, never the prompt text — see Prompt's docstring for
    why that distinction is load-bearing.
    """
    from . import llm
    from .rag import query_enhance

    # Resolvers, not snapshots — see Prompt's docstring.
    return {
        "answer": Prompt("answer", lambda: llm.SYSTEM),
        "query_enhance": Prompt("query_enhance", lambda: query_enhance._SYSTEM),
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

    Hashed from the parser sources plus the chunk-window env knob, rather than
    a constant, for the same reason prompts are: component 14 changed
    `paper.py`'s chunking and no constant got bumped. A file that cannot be read
    degrades to a marker rather than failing — a provenance helper must never
    break the read path. Known limit, disclosed: this is `@lru_cache`d, so a
    file that is unreadable at first call pins the degraded hash for the
    process lifetime.
    """
    here = Path(__file__).resolve().parent
    # The transcript chunk window is an env knob that moves chunk boundaries
    # with no source change — hashing only the parsers would miss it.
    parts: list[str] = [f"transcript_chunk_seconds={config.TRANSCRIPT_CHUNK_SECONDS}"]
    for rel in ("ingest/paper.py", "ingest/deck.py", "ingest/transcript.py"):
        try:
            parts.append((here / rel).read_text())
        except Exception:
            parts.append(f"<unreadable:{rel}>")
    return _hash("\n".join(parts))


def _corpus_path() -> Path:
    """Seam so a test can point this elsewhere."""
    return Path(__file__).resolve().parents[1] / "benchmark" / "corpus.json"


@lru_cache
def corpus_version() -> str:
    """Revision of the seeded corpus definition.

    §3g specified this alongside the chunker version and it was not built:
    without it, two eval runs over different corpora are indistinguishable in
    an experiment record, which defeats the point of comparing them. Hashed
    from `benchmark/corpus.json` (which the Docker image does ship). A missing
    or unreadable file degrades to a marker — provenance must never break the
    read path it is attached to.
    """
    try:
        return _hash(_corpus_path().read_text())
    except Exception:
        return "unavailable"


def _opik_prompt_cls():
    """Seam for the Opik Prompt class; imported lazily so `opik` stays optional."""
    from opik import Prompt as OpikPrompt

    return OpikPrompt


def push_to_opik() -> list[str]:
    """Publish every registered prompt to Opik's prompt library.

    §3g specified this and it was not built — versions were computed and
    stamped on spans, but nothing reached the library, so a trace's
    `prompt_version` pointed at text stored nowhere. Opik versions prompts by
    content itself, so re-pushing unchanged text is a no-op on their side and
    our content hash and theirs agree by construction.

    Returns the names pushed. No-op without `OPIK_API_KEY`; fails OPEN, because
    a telemetry publish must never break startup or a benchmark.
    """
    if not config.OPIK_API_KEY:
        return []
    pushed: list[str] = []
    try:
        cls = _opik_prompt_cls()
    except Exception:
        return []
    for name, prompt in _registry().items():
        try:
            cls(name=name, prompt=prompt.text,
                metadata={"version": prompt.version,
                          "embed_version": config.EMBED_VERSION,
                          "chunker_version": chunker_version()})
            pushed.append(name)
        except Exception:
            continue      # one bad prompt must not stop the rest
    return pushed


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
        "corpus_version": corpus_version(),
    }
