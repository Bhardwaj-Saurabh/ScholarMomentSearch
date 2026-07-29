"""Entity-graph augmented retrieval — DESIGN.md §3i component 50.

The property that protects every number already recorded in EVIDENCE.md is the
first test here: with `GRAPH_RETRIEVAL_ENABLED` off, ranking must be
byte-identical to today's. Everything else is about the boost being *bounded*
and *tenant-scoped*, because an unbounded boost is just a different way of
inventing a citation.

Most of this file exercises pure functions (extraction, boost application) so
the logic is provable without a database. The DB-backed lookups have their own
section and clean up after themselves.
"""
from __future__ import annotations

import pytest

from src import graph


# ── Extraction: deterministic, and quiet on ordinary prose ───────────────────

def test_extraction_is_deterministic():
    text = "We evaluate CLIP and GPT-3 on ImageNet using LoRA adapters."
    assert graph.extract_entities(text) == graph.extract_entities(text)


def test_extracts_acronyms_and_model_names():
    ents = graph.extract_entities(
        "We evaluate CLIP and GPT-3 on ImageNet using LoRA adapters.")
    assert "clip" in ents
    assert "gpt-3" in ents


def test_sentence_initial_words_are_not_entities():
    """The single biggest source of garbage entities: every sentence starts
    with a capital letter. Without a stopword pass the graph fills up with
    "The", "We", "Our", "However" and the boost becomes noise."""
    ents = graph.extract_entities(
        "The model works. We trained it. Our results hold. However, it is slow. "
        "This shows that. These are the facts. Figure 2 shows Table 3.")
    for junk in ("the", "we", "our", "however", "this", "these", "figure", "table"):
        assert junk not in ents


def test_title_entities_are_included():
    ents = graph.extract_entities("some body text", title="CLIP (Radford et al. 2021)")
    assert "clip" in ents


def test_extraction_never_raises_on_junk():
    for junk in (None, "", "   ", "\x00\x01", "?!?!", "a" * 20_000):
        assert isinstance(graph.extract_entities(junk), list)


def test_entities_are_normalized_and_deduped():
    ents = graph.extract_entities("CLIP clip Clip CLIP.")
    assert ents.count("clip") == 1


# ── The boost: bounded, order-preserving, and off by default ──────────────────

def _win(rrf: float, source_id: str | None = None, video_id: str | None = None):
    text = {"source_id": source_id, "page": 1} if source_id else None
    return {"video_id": video_id, "t": 0.0, "rrf": rrf,
            "modalities": {"text"}, "frame": None, "text": text}


def test_boost_promotes_a_window_whose_source_mentions_the_entity():
    """The whole point: a chunk from the source the question NAMES should beat
    a merely topically-similar chunk from a different source."""
    windows = [_win(0.50, source_id="other_paper"), _win(0.48, source_id="clip_paper")]
    out = graph.boost_windows(windows, {"clip_paper"}, boost=0.10)
    assert out[0]["text"]["source_id"] == "clip_paper"


def test_boost_is_bounded_and_cannot_invert_a_large_gap():
    """An unbounded boost would let the graph override retrieval entirely —
    the failure mode that turns a ranking hint into a fabrication engine."""
    windows = [_win(0.90, source_id="other"), _win(0.10, source_id="matched")]
    out = graph.boost_windows(windows, {"matched"}, boost=graph.MAX_BOOST)
    assert out[0]["text"]["source_id"] == "other"


def test_boost_never_removes_or_adds_windows():
    windows = [_win(0.5, source_id="a"), _win(0.4, source_id="b"), _win(0.3)]
    out = graph.boost_windows(windows, {"b"}, boost=0.1)
    assert len(out) == len(windows)
    assert {id(w) for w in out} == {id(w) for w in windows}


def test_no_matches_leaves_order_byte_identical():
    windows = [_win(0.5, source_id="a"), _win(0.4, source_id="b")]
    before = [w["rrf"] for w in windows]
    out = graph.boost_windows(windows, set(), boost=0.1)
    assert [w["rrf"] for w in out] == before


def test_boost_is_clamped_to_max():
    windows = [_win(0.10, source_id="m")]
    out = graph.boost_windows(windows, {"m"}, boost=99.0)
    assert out[0]["rrf"] <= 0.10 + graph.MAX_BOOST


def test_boost_ties_break_deterministically():
    """Same contract as _merge_hits/_fuse (test_ranking_determinism.py): equal
    scores must always order the same way, or precision@10 wobbles."""
    runs = []
    for _ in range(5):
        windows = [_win(0.4, source_id="b"), _win(0.3, source_id="a"),
                   _win(0.3, source_id="c")]
        out = graph.boost_windows(windows, {"a", "c"}, boost=0.1)
        runs.append([(w["text"] or {}).get("source_id") for w in out])
    assert all(r == runs[0] for r in runs), runs


def test_disabled_by_default(monkeypatch):
    """Component 17's rule: an opt-in retrieval change must never move the
    baseline numbers a reviewer sees unless explicitly turned on."""
    monkeypatch.delenv("GRAPH_RETRIEVAL_ENABLED", raising=False)
    from src import config
    monkeypatch.setattr(config, "GRAPH_RETRIEVAL_ENABLED", False)
    assert graph.enabled() is False


# ── Fail-open: a graph error must never break the read path ───────────────────

def test_matched_sources_fails_open_on_db_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(graph.db, "graph_sources_for_entities", boom)
    assert graph.matched_sources("u1", ["clip"]) == set()


def test_matched_sources_empty_for_no_entities():
    assert graph.matched_sources("u1", []) == set()


# ── DB-backed: tenancy and the 1-hop hop ─────────────────────────────────────

@pytest.fixture
def cleanup():
    from src import db
    yield
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM ms_graph_mentions WHERE user_id LIKE 'graphtest_%'")


def test_mentions_round_trip_and_are_tenant_scoped(cleanup):
    graph.record_mentions("graphtest_a", "src_1", "paper", ["clip", "imagenet"])
    graph.record_mentions("graphtest_b", "src_2", "paper", ["clip"])

    assert graph.matched_sources("graphtest_a", ["clip"]) == {"src_1"}
    assert graph.matched_sources("graphtest_b", ["clip"]) == {"src_2"}
    # The isolation assertion that matters: A's entity must never reach B's row.
    assert "src_2" not in graph.matched_sources("graphtest_a", ["clip"])


def test_record_mentions_is_idempotent(cleanup):
    for _ in range(3):
        graph.record_mentions("graphtest_c", "src_3", "paper", ["clip"])
    assert graph.matched_sources("graphtest_c", ["clip"]) == {"src_3"}


def test_one_hop_neighbours(cleanup):
    """`clip` and `imagenet` co-occur in src_4, so asking about `clip` reaches
    `imagenet` — and through it, src_5, which never mentions `clip` at all.
    This is the graph hop the architecture comparison found missing."""
    graph.record_mentions("graphtest_d", "src_4", "paper", ["clip", "imagenet"])
    graph.record_mentions("graphtest_d", "src_5", "paper", ["imagenet"])

    assert graph.neighbours("graphtest_d", ["clip"]) >= {"imagenet"}
    expanded = graph.matched_sources("graphtest_d", ["clip"], hops=1)
    assert expanded == {"src_4", "src_5"}


def test_neighbours_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(graph.db, "graph_neighbours", boom)
    assert graph.neighbours("u1", ["clip"]) == set()


def test_neighbours_ranks_shared_seeds_first(cleanup):
    """"elmo" co-occurs with BOTH seed entities (via src_a and src_c);
    "squad"/"glue" co-occur with only one each — elmo must rank first."""
    graph.record_mentions("graphtest_e2", "src_a", "paper", ["bert", "elmo", "glue"])
    graph.record_mentions("graphtest_e2", "src_b", "paper", ["bert", "squad"])
    graph.record_mentions("graphtest_e2", "src_c", "paper", ["language", "elmo"])
    nb = graph.db.graph_neighbours("graphtest_e2", ["bert", "language"], limit=20)
    assert nb[0] == "elmo"


def test_neighbours_cap_actually_trims_a_large_candidate_set(cleanup):
    """This is the regression that matters, not just an API-shape check
    (spec-guardian: the previous version of this test used only 3 candidates,
    well under any plausible limit, so it would have passed under the
    ORIGINAL uncapped query too). Reproduces the size regime that caused the
    real incident: one source mentioning a seed entity alongside 50 other
    terms, each shared with the seed by only that one source — i.e. real
    fan-out material, not a toy case."""
    filler = [f"term-{i}" for i in range(50)]
    graph.record_mentions("graphtest_e3", "src_big", "paper", ["clip"] + filler)
    nb = graph.db.graph_neighbours("graphtest_e3", ["clip"], limit=20)
    assert len(nb) == 20, f"expected the cap to trim 50 candidates to 20, got {len(nb)}"


def test_neighbours_wrapper_passes_the_cap_through(cleanup):
    """graph.neighbours() (the function search.py actually calls) must not
    silently drop the limit on its way to db.graph_neighbours."""
    filler = [f"term-{i}" for i in range(50)]
    graph.record_mentions("graphtest_e4", "src_big", "paper", ["clip"] + filler)
    nb = graph.neighbours("graphtest_e4", ["clip"])
    assert len(nb) <= 20


def test_deleting_a_document_purges_its_graph_rows(cleanup):
    from src import db

    graph.record_mentions("graphtest_e", "doc_purge_me", "paper", ["clip"])
    assert graph.matched_sources("graphtest_e", ["clip"]) == {"doc_purge_me"}
    db.delete_document("doc_purge_me")
    assert graph.matched_sources("graphtest_e", ["clip"]) == set()


# ── Phrase-extraction defects found by spec-guardian ─────────────────────────

def test_leading_function_words_are_trimmed_from_phrases():
    """`_PHRASE` starts on any capital, so a sentence-initial "Our" produced
    the entity `our sparse attention`, which can never match a query saying
    "sparse attention". Rejecting only ALL-stopword phrases missed this."""
    ents = graph.extract_entities(
        "Our Sparse Attention variant beats BERT on every task.")
    assert "sparse attention" in ents
    assert not any(e.startswith("our ") for e in ents)


@pytest.mark.parametrize("junk_prefix", ["however ", "interestingly ", "this ",
                                         "why this matters for"])
def test_discourse_markers_do_not_become_entity_prefixes(junk_prefix):
    text = ("However Google reported gains. Interestingly Meta did too. "
            "Why This Matters For Enterprise Search is unclear.")
    ents = graph.extract_entities(text)
    assert not any(e.startswith(junk_prefix) for e in ents), ents


def test_lowercase_joined_phrases_are_extracted():
    """"Chain of Thought" was NOT matched by `_PHRASE` (the lowercase "of"
    breaks the capitalized run) even though the code comment claimed it as the
    example — and chain-of-thought is one of the labeled query topics."""
    ents = graph.extract_entities("We study Chain of Thought prompting.")
    assert any("chain" in e and "thought" in e for e in ents), ents


def test_hyphenated_lowercase_terms_are_extracted():
    ents = graph.extract_entities("We measure zero-shot and chain-of-thought accuracy.")
    assert "zero-shot" in ents
    assert "chain-of-thought" in ents


# ── Query-side extraction: the reason the boost would never have fired ────────

def test_lowercase_question_yields_nothing_from_the_document_extractor():
    """Documents the gap that motivates extract_query_entities: real questions
    are lowercase, and the capitalization-driven extractor is blind to them."""
    assert graph.extract_entities("what does the clip paper say about transfer?") == []


def test_query_entities_resolve_against_the_tenant_vocabulary(monkeypatch):
    """A lowercase question must still find `clip` — via the entity vocabulary
    the tenant actually has, so it can never invent one."""
    monkeypatch.setattr(graph.db, "graph_match_entities",
                        lambda u, cands: {"clip"} & set(cands))
    ents = graph.extract_query_entities("u1", "what does the clip paper say?")
    assert "clip" in ents


def test_query_entities_cannot_invent_an_entity(monkeypatch):
    monkeypatch.setattr(graph.db, "graph_match_entities", lambda u, cands: set())
    assert graph.extract_query_entities("u1", "what about flibbertigibbet routing?") == []


def test_query_entities_fail_open_on_db_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(graph.db, "graph_match_entities", boom)
    # Falls back to whatever the capitalization pass found — here, the acronym.
    assert graph.extract_query_entities("u1", "How does CLIP work?") == ["clip"]


# ── Discriminating power: the generic-entity problem, found by measurement ────

@pytest.mark.parametrize("generic", ["ai", "api", "gpu", "os", "url", "nlp",
                                     "youtube", "arxiv", "neurips"])
def test_generic_technical_vocabulary_is_not_an_entity(generic):
    """Backfilling the real corpus produced ai(10), gpt(7), gpus(5), api(3),
    os(2), url(2) as the most-shared "entities" — each would fire the boost on
    almost any question. These are excluded by name."""
    assert generic not in graph.extract_entities(f"We used {generic.upper()} here.")


def test_idf_drops_entities_most_of_the_corpus_mentions(monkeypatch):
    monkeypatch.setattr(graph.db, "graph_source_count", lambda u: 20)
    monkeypatch.setattr(graph.db, "graph_entity_source_counts",
                        lambda u, e: {"transformer": 18, "mamba": 1})
    assert graph.discriminating("u1", ["transformer", "mamba"]) == ["mamba"]


def test_idf_is_skipped_on_a_tiny_corpus(monkeypatch):
    """With 3 sources, "mentioned by 2 of them" is not evidence of being
    generic — applying IDF there would delete real signal."""
    monkeypatch.setattr(graph.db, "graph_source_count", lambda u: 3)
    monkeypatch.setattr(graph.db, "graph_entity_source_counts",
                        lambda u, e: {"clip": 3})
    assert graph.discriminating("u1", ["clip"]) == ["clip"]


def test_idf_fails_open_to_the_unfiltered_list(monkeypatch):
    """A counting error must degrade to today's behaviour, NOT to dropping
    every entity — that would disable the feature invisibly."""
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(graph.db, "graph_source_count", boom)
    assert graph.discriminating("u1", ["clip"]) == ["clip"]


def test_matched_sources_returns_empty_when_all_entities_are_generic(monkeypatch):
    monkeypatch.setattr(graph.db, "graph_source_count", lambda u: 20)
    monkeypatch.setattr(graph.db, "graph_entity_source_counts",
                        lambda u, e: {"transformer": 19})
    called = []
    monkeypatch.setattr(graph.db, "graph_sources_for_entities",
                        lambda *a: called.append(1) or [])
    assert graph.matched_sources("u1", ["transformer"]) == set()
    assert called == [], "must not even query when nothing discriminates"


# ── The invariant that protects every already-recorded number ─────────────────

@pytest.mark.parametrize("flag,expect_called", [(False, False), (True, True)])
def test_graph_is_consulted_only_when_the_flag_is_on(monkeypatch, flag, expect_called):
    """The invariant that keeps every recorded precision@10 / recall@10 figure
    valid: with the flag off the read path must not call into graph.py.

    Driven through a REAL `_retrieve_impl` call with the graph functions spied,
    not a source grep — the previous version asserted `called == []` without
    ever invoking the function under test, so it passed for free
    (spec-guardian). The `True` case is what proves the `False` case means
    something.
    """
    import numpy as np

    from src import config
    from src.rag import search as rag_search

    monkeypatch.setattr(config, "GRAPH_RETRIEVAL_ENABLED", flag)
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    monkeypatch.setattr(config, "QUERY_ENHANCEMENT_ENABLED", False)

    # Keep retrieval entirely in-memory: one document hit is enough to produce
    # a window for the boost to act on.
    hit = {"source_id": "src_x", "page": 1, "score": 0.9, "text": "body",
           "kind": "paper"}
    monkeypatch.setattr(rag_search, "embed_text", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search, "embed_query", lambda q: np.zeros(4, dtype=np.float32))
    monkeypatch.setattr(rag_search.vector_store, "search", lambda *a, **k: [])
    monkeypatch.setattr(rag_search.vector_store, "search_text", lambda *a, **k: [hit])
    monkeypatch.setattr(rag_search.db, "documents_by_ids", lambda ids: {})
    monkeypatch.setattr(rag_search.db, "videos_by_ids", lambda ids: {}, raising=False)

    called: list[str] = []
    monkeypatch.setattr(rag_search.graph, "extract_query_entities",
                        lambda *a, **k: (called.append("extract"), ["clip"])[1])
    monkeypatch.setattr(rag_search.graph, "matched_sources",
                        lambda *a, **k: (called.append("matched"), {"src_x"})[1])
    monkeypatch.setattr(rag_search.graph, "boost_windows",
                        lambda w, *a, **k: (called.append("boost"), w)[1])

    rag_search._retrieve_impl("what about clip?", "u_test", top_k=5)

    assert bool(called) is expect_called, called


def test_live_path_uses_direct_matches_only(monkeypatch):
    """Pinned after a live check found hops=1 matching 18-26 of 28 real
    sources for an ordinary question, even after the neighbour cap/ranking fix
    — broader than "boost the source the question names" was meant to be
    (EVIDENCE.md, 2026-07-29). hops=1 stays implemented and unit-tested above;
    this guards that the LIVE wiring does not silently switch back to it."""
    import inspect

    from src.rag import search as rag_search

    src = inspect.getsource(rag_search._retrieve_impl)
    assert "graph.matched_sources(user_id, q_entities, hops=0)" in src
