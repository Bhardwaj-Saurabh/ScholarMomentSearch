"""Entity-graph augmented retrieval — DESIGN.md §3i component 50.

Dense retrieval cannot tell "about entity X" from "similar to text that
discusses entity X". That is why a question naming one paper pulls in
topically-adjacent chunks from *other* papers in the same field — the
cross-triplet adjacency already diagnosed as a `precision_at_10` cause, and
the shape of both grounding violations in EVIDENCE.md's Part-0 audit. An entity
index adds a **symbolic** signal that similarity cannot supply.

Four properties, in the order they matter:

1. **Off by default** (`GRAPH_RETRIEVAL_ENABLED`). With the flag unset, nothing
   here runs and ranking is byte-identical to today's, so every number already
   recorded in EVIDENCE.md stays valid. Same rule as component 17.
2. **Boost, never filter.** The graph only ever *raises* a window's score, by a
   bounded amount (`MAX_BOOST`). It cannot remove a correct answer from the
   candidate set, so grounded-or-silent (AGENTS.md #5) stays structurally safe
   rather than argumentatively safe.
3. **Fails open.** A missing table, a Postgres error, or a junk input degrades
   to "no boost" and never raises on the read path — the same contract
   `src/cache.py` and `src/injection.py` have.
4. **Tenanted.** Every row written and every query issued carries `user_id`.

**What this is, precisely:** an entity index with co-occurrence edges, not
LLM-extracted semantic relations. Extraction is a deterministic regex pass, not
a model call, because an LLM call per chunk would be thousands of calls with
unpredictable cost and would directly threaten the `ingest_throughput` ≥ 8
chunks/s SLA. It is weaker than full GraphRAG and the name should not imply
otherwise.

**Known asymmetry, recorded rather than discovered later:** `src/ingest/
pipeline.py` (the VIDEO tasks) is CLAUDE.md-protected, so per-chunk extraction
cannot be added there. Documents get entities from title AND chunk text; videos
get **title-level entities only**, backfilled from `ms_videos.title`. This is
the same protected-file asymmetry component 46 has for tracing.
"""
from __future__ import annotations

import re

from . import config, db

# The boost ceiling. Deliberately small relative to a typical fused RRF score
# (~0.3-0.9): the graph is a hint that reorders near-ties, never an override
# that can promote an unrelated chunk over a strong retrieval match. The
# bounded-ness is unit-tested, not just asserted here.
MAX_BOOST = 0.05

# Acronyms and model names: CLIP, GPT-3, BERT, T5, LoRA, ViT-B.
_ACRONYM = re.compile(r"\b[A-Z][A-Za-z]*[A-Z0-9][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*\b")
# Capitalized multi-word phrases: "Chain of Thought", "Sparse Attention".
_PHRASE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,3}\b")

# Every sentence starts with a capital letter, so without this the graph fills
# with "The", "We", "However" and the boost becomes pure noise. Also drops the
# structural words that pepper papers ("Figure", "Table", "Appendix") and the
# citation furniture ("et al").
_STOPWORDS = {
    "the", "we", "our", "this", "these", "those", "that", "it", "its", "they",
    "he", "she", "there", "here", "however", "moreover", "therefore", "thus",
    "figure", "table", "appendix", "section", "equation", "algorithm", "et",
    "al", "in", "on", "for", "and", "or", "but", "as", "at", "by", "with",
    "from", "to", "of", "a", "an", "is", "are", "was", "were", "be", "been",
    "abstract", "introduction", "conclusion", "related", "work", "results",
    "method", "methods", "experiments", "discussion", "references", "dataset",
    "model", "models", "paper", "papers", "using", "used", "use", "show",
    "shows", "shown", "note", "see", "first", "second", "third", "next",
    "finally", "also", "both", "each", "all", "one", "two", "three", "if",
    "when", "while", "where", "which", "who", "what", "how", "why", "can",
    "may", "will", "would", "should", "could", "not", "no", "yes", "so",
}

# Generic technical vocabulary that is NOT a discriminating entity in a corpus
# of ML papers and talks. Found by backfilling the real corpus and reading the
# result: the most-shared entities came out as ai(10), gpt(7), gpus(5), qa(4),
# api(3), os(2), url(2), pt(2) — every one of which would make the boost fire
# on almost any question and reward the wrong source. Recorded here because it
# was measured, not guessed.
_GENERIC = {
    "ai", "ml", "nlp", "cv", "llm", "llms", "api", "apis", "os", "url", "urls",
    "gpu", "gpus", "cpu", "cpus", "tpu", "tpus", "ram", "io", "id", "ids",
    "json", "http", "https", "html", "pdf", "csv", "sql", "cli", "ui", "ux",
    "qa", "pt", "phd", "msc", "bsc", "usa", "uk", "eu", "youtube", "github",
    "arxiv", "acl", "emnlp", "neurips", "icml", "iclr", "cvpr", "kdd",
    "sota", "mlp", "rnn", "cnn", "lstm", "relu", "adam", "sgd", "fp16", "fp32",
}

_MAX_TEXT = 20_000        # bound the regex pass; chunks are far smaller
_MAX_ENTITIES = 40        # per chunk — a runaway page must not flood the graph
_MIN_LEN = 2

# Inverse-document-frequency guard, the principled half of the same problem: an
# entity mentioned by most of the corpus cannot tell one source from another,
# which is the ONLY thing this boost is for. Applied at query time so it adapts
# as the corpus grows, instead of needing the list above to stay current.
_IDF_MAX_FRACTION = 0.35   # an entity in >35% of sources is not discriminative
_IDF_MIN_SOURCES = 6       # below this the corpus is too small for IDF to mean
                           # anything, so the hardcoded list carries it alone


def enabled() -> bool:
    return bool(getattr(config, "GRAPH_RETRIEVAL_ENABLED", False))


def _normalize(raw: str) -> str:
    return raw.strip().strip(".,;:()[]\"'").lower()


def _short_title(title: str) -> str:
    """'CLIP (Radford et al. 2021)' -> 'CLIP'. Mirrors search.py::_short_name
    so a title-derived entity matches what the attribution backstop already
    considers the source's short form."""
    return re.split(r"[:(]", title, maxsplit=1)[0].strip()


def extract_entities(text, title: str | None = None) -> list[str]:
    """Deterministic entity extraction. Returns normalized, deduped names in
    first-seen order (stable order matters: it makes the graph reproducible and
    the tests meaningful). Never raises — junk in, empty list out."""
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        name = _normalize(candidate)
        if (len(name) < _MIN_LEN or name in seen or name in _STOPWORDS
                or name in _GENERIC or not any(c.isalpha() for c in name)):
            return
        # A multi-word phrase whose every word is a stopword is furniture.
        if all(w in _STOPWORDS for w in name.split()):
            return
        seen.add(name)
        found.append(name)

    if title:
        try:
            add(_short_title(str(title)))
        except Exception:
            pass

    try:
        body = str(text or "")[:_MAX_TEXT]
    except Exception:
        return found[:_MAX_ENTITIES]

    for pattern in (_ACRONYM, _PHRASE):
        for m in pattern.finditer(body):
            add(m.group(0))
            if len(found) >= _MAX_ENTITIES:
                return found[:_MAX_ENTITIES]
    return found[:_MAX_ENTITIES]


def record_mentions(user_id: str, source_id: str, source_kind: str,
                    entities: list[str]) -> int:
    """Persist entity -> source edges. Idempotent (primary key on the triple).
    Fails open: an ingest must not fail because the graph write did."""
    if not entities:
        return 0
    try:
        return db.graph_upsert_mentions(user_id, source_id, source_kind, entities)
    except Exception:
        return 0


def neighbours(user_id: str, entities: list[str]) -> set[str]:
    """1-hop: entities that co-occur with any of `entities` in the same source.
    This is the graph hop — `clip` reaches `imagenet`, and through it a source
    that never mentions `clip` at all."""
    if not entities:
        return set()
    try:
        return set(db.graph_neighbours(user_id, entities))
    except Exception:
        return set()


def discriminating(user_id: str, entities: list[str]) -> list[str]:
    """Drop entities that most of the corpus mentions. An entity shared by
    almost every source cannot tell one source from another, which is the only
    job this boost has — keeping it would make the graph fire on nearly every
    question and reward whichever source happened to say "AI" most often.

    Skipped on a corpus too small for the frequency to mean anything; the
    hardcoded `_GENERIC` list covers that case. Fails open to the input list
    unchanged, so a counting error degrades to today's behaviour rather than
    silently dropping every entity (which would disable the feature invisibly).
    """
    if not entities:
        return []
    try:
        total = db.graph_source_count(user_id)
        if total < _IDF_MIN_SOURCES:
            return list(entities)
        counts = db.graph_entity_source_counts(user_id, list(entities))
    except Exception:
        return list(entities)
    ceiling = max(2, int(total * _IDF_MAX_FRACTION))
    return [e for e in entities if counts.get(e, 0) <= ceiling]


def matched_sources(user_id: str, entities: list[str], hops: int = 0) -> set[str]:
    """Which source_ids mention any of these entities (optionally after one
    hop of co-occurrence expansion). Fails open to the empty set, which the
    caller treats as "no boost"."""
    if not entities:
        return set()
    terms = discriminating(user_id, entities)
    if not terms:
        return set()
    if hops >= 1:
        # Expand from the DISCRIMINATING terms only, then filter the expansion
        # too — a hop off a specific entity can easily land on a generic one,
        # and an unfiltered hop would reintroduce exactly what we just removed.
        terms = discriminating(user_id, sorted(set(terms) | neighbours(user_id, terms)))
    try:
        return set(db.graph_sources_for_entities(user_id, terms))
    except Exception:
        return set()


def backfill_from_index(user_id: str, batch: int = 512) -> dict:
    """Populate the graph for content that was indexed BEFORE this component
    existed, by reading chunk text back out of the shared text collection.

    Also the only route by which VIDEO sources get entities from their
    transcript text: `src/ingest/pipeline.py` is CLAUDE.md-protected, so no
    extraction hook can be added there. Documents get theirs at ingest time
    (`doc_pipeline.t_embed_index`) and are re-covered here harmlessly, since
    `record_mentions` is idempotent.

    Returns a summary dict; raises nothing the caller has to handle beyond
    normal Qdrant/Postgres errors (this is an operator action, not a read
    path).
    """
    from .config import TEXT_COLLECTION
    from .rag import vector_store

    per_source: dict[str, tuple[str, list[str]]] = {}
    seen_per_source: dict[str, set[str]] = {}
    offset = None
    scanned = 0
    while True:
        points, offset = vector_store.client().scroll(
            collection_name=TEXT_COLLECTION,
            scroll_filter=vector_store._user_filter(user_id),
            limit=batch, offset=offset,
            with_payload=True, with_vectors=False,
        )
        if not points:
            break
        for p in points:
            payload = p.payload or {}
            source_id = payload.get("source_id") or payload.get("video_id")
            if not source_id:
                continue
            kind = payload.get("kind") or ("video" if payload.get("video_id") else "paper")
            scanned += 1
            bucket = per_source.setdefault(source_id, (kind, []))[1]
            seen = seen_per_source.setdefault(source_id, set())
            for e in extract_entities(payload.get("text")):
                if e not in seen:
                    seen.add(e)
                    bucket.append(e)
        if offset is None:
            break

    # Titles too — high-precision, and the only entity a frame-only video has.
    titles: dict[str, str] = {}
    try:
        for row in db.list_sources(user_id):
            if row.get("id") and row.get("title"):
                titles[row["id"]] = row["title"]
    except Exception:
        pass

    written = 0
    for source_id, (kind, entities) in per_source.items():
        title = titles.get(source_id)
        if title:
            for e in extract_entities("", title=title):
                if e not in seen_per_source.get(source_id, set()):
                    entities.append(e)
        written += record_mentions(user_id, source_id, kind, entities)
    return {"chunks_scanned": scanned, "sources": len(per_source),
            "edges_written": written}


def _window_source(w: dict) -> str | None:
    text = w.get("text") or {}
    return text.get("source_id") or w.get("video_id")


def boost_windows(windows: list[dict], matched: set[str],
                  boost: float = MAX_BOOST) -> list[dict]:
    """Add a bounded boost to windows whose source is in `matched`, then
    re-sort with the SAME deterministic tie-break `_fuse` uses.

    Mutates `w["rrf"]` in place and returns the same window objects reordered —
    never adds or drops a window, which is what keeps this a ranking hint
    rather than a filter.
    """
    if not matched:
        return windows
    delta = max(0.0, min(float(boost), MAX_BOOST))
    if delta == 0.0:
        return windows
    for w in windows:
        src = _window_source(w)
        if src and src in matched:
            w["rrf"] = float(w.get("rrf") or 0.0) + delta
            w["graph_boosted"] = True
    windows.sort(key=lambda w: (-w["rrf"], str(w.get("video_id")),
                                float(w.get("t") or 0.0)))
    return windows
