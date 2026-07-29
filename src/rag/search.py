"""Read path: question -> retrieve -> gate -> cited answer (or honest abstain).

Retrieval is milliseconds; the multimodal LLM call is seconds and dominates
cost. So the shape is a confidence funnel: fetch KNN_K candidates, collapse
temporal near-duplicates, trim to TOP_K, and — Gate 1 — if even the best
score is below CONFIDENCE_THRESHOLD, abstain WITHOUT calling the LLM. That
one free check kills most hallucination risk. Generated answers get their
[n] citations validated; invented references are stripped.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import cache, config, db, llm, metrics, prompts, storage, tracing
from ..config import (BRANCH_TOP_K, CONFIDENCE_THRESHOLD, CROSS_MODAL_BOOST,
                      FUSION_WINDOW_S, RRF_K, TEXT_CONFIDENCE_THRESHOLD, TOP_K)
from . import vector_store
from .embeddings import embed_query, embed_text

ABSTAIN = ("I couldn't find that in your videos — nothing indexed looks "
           "related to the question (neither what's on screen nor what's said).")


def _seconds(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _hit_key(h: dict[str, Any]) -> tuple:
    """Point identity for dedup across possibly-multiple sub-query result
    sets (component 17) — a video frame (video_id+idx), a video transcript
    chunk (video_id+ms), or a document chunk (source_id+page/slide)."""
    return (h.get("video_id"), h.get("idx"), h.get("ms"),
           h.get("source_id"), h.get("page"), h.get("slide"))


def _merge_hits(hit_lists: list[list[dict]]) -> list[dict]:
    """Dedup by point identity across N sub-query result lists (keeping the
    best-scoring instance of each), then re-sort by score descending so
    _fuse()'s rank-based RRF still sees a properly ordered list regardless of
    which sub-query surfaced a hit. A single list in -> that same list right
    back out, sorted (a verified no-op when query enhancement is disabled,
    since Qdrant already returns hits best-first)."""
    best: dict[tuple, dict] = {}
    for hits in hit_lists:
        for h in hits:
            key = _hit_key(h)
            if key not in best or h["score"] > best[key]["score"]:
                best[key] = h
    return sorted(best.values(), key=lambda h: h["score"], reverse=True)


def _fuse(visual_hits: list[dict], text_hits: list[dict]) -> list[dict]:
    """Reciprocal-Rank-Fusion of the two branches into time windows.

    Raw scores are incomparable (CLIP ~0.3 vs bge ~0.7), so we rank each branch
    on its own and score by rank: rrf = 1/(RRF_K + rank). Then we bucket hits
    within FUSION_WINDOW_S seconds of each other (same video) into one 'moment',
    sum their rrf, and boost windows where BOTH modalities agree — two
    independent signals pointing at the same instant is the strongest evidence.

    Paper/deck chunks (Assignment 3) carry no video_id and no timestamp — a
    page or slide IS already the precise citation unit, so each becomes its
    own window directly rather than being time-windowed (which wouldn't mean
    anything for them) or merged with anything else.
    """
    def ranked(hits, modality):
        out = []
        for rank, h in enumerate(hits):
            t = float(h.get("t_start", h.get("ms", 0) / 1000.0))
            out.append({**h, "modality": modality, "rrf": 1.0 / (RRF_K + rank), "t": t})
        return out

    video_text_hits = [h for h in text_hits if h.get("video_id")]
    doc_text_hits = [h for h in text_hits if not h.get("video_id")]

    windows: list[dict] = []
    # Hits arrive best-first (rrf desc), so the first hit landing in a window for
    # a given modality is that modality's best hit there.
    for h in sorted(ranked(visual_hits, "frame") + ranked(video_text_hits, "text"),
                    key=lambda x: x["rrf"], reverse=True):
        w = next((w for w in windows if w.get("video_id") == h["video_id"]
                  and abs(w["t"] - h["t"]) <= FUSION_WINDOW_S), None)
        if w is None:
            w = {"video_id": h["video_id"], "t": h["t"], "rrf": 0.0,
                 "modalities": set(), "frame": None, "text": None}
            windows.append(w)
        w["modalities"].add(h["modality"])
        slot = "frame" if h["modality"] == "frame" else "text"
        # Keep only the BEST hit per modality. Summing every hit would let a
        # burst of near-identical frames clustered in one 15s window inflate its
        # score past a genuine frame+transcript match — the bug that ranked a
        # silent frame-burst above the moment that actually answered.
        if w[slot] is None:
            w[slot] = h
    for w in windows:
        # Score = best frame + best transcript hit; ×boost when BOTH modalities
        # agree at this instant (two independent signals = strongest evidence).
        w["rrf"] = (w["frame"]["rrf"] if w["frame"] else 0.0) + \
                   (w["text"]["rrf"] if w["text"] else 0.0)
        if {"frame", "text"} <= w["modalities"]:
            w["rrf"] *= CROSS_MODAL_BOOST

    for h in ranked(doc_text_hits, "text"):
        windows.append({"video_id": None, "t": 0.0, "rrf": h["rrf"],
                        "modalities": {"text"}, "frame": None, "text": h})

    windows.sort(key=lambda w: w["rrf"], reverse=True)
    return windows


def _deeplink(video: dict | None, video_id: str, ms: int) -> str:
    secs = ms // 1000
    if video and video.get("source") == "youtube" and video.get("url"):
        sep = "&" if "?" in video["url"] else "?"
        return f"{video['url']}{sep}t={secs}"
    return f"/api/video/{video_id}#t={secs}"


def _thumb_url(user_id: str, video_id: str, idx: int) -> str:
    """Browser-facing thumbnail URL. Presigned GET straight to the bucket when
    the provider supports it (an <img> tag can't send auth headers); the API
    serves the bytes itself only in local-dev mode."""
    if storage.presign_capable():
        return storage.presign_get(storage.frame_key(user_id, video_id, idx))
    return f"/api/frame/{video_id}/{idx:06d}.jpg?u={user_id}"


def _media_url(video: dict | None, user_id: str, video_id: str) -> str | None:
    """Playback URL for uploaded videos (YouTube plays via its own URL)."""
    if not video or video.get("source") != "upload" or not video.get("storage_key"):
        return None
    if storage.presign_capable():
        return storage.presign_get(video["storage_key"])
    return f"/api/video/{video_id}?u={user_id}"


def retrieve(question: str, user_id: str, *, top_k: int | None = None,
             video_id: str | None = None,
             video_ids: list[str] | None = None) -> dict[str, Any]:
    """Thin tracing wrapper (component 45) around _retrieve_impl — keeps the
    span boundary out of the retrieval logic itself and out of its several
    branch points."""
    with tracing.span("retrieve", top_k=top_k or TOP_K,
                      scoped=bool(video_id or video_ids)) as sp:
        r = _retrieve_impl(question, user_id, top_k=top_k,
                           video_id=video_id, video_ids=video_ids)
        sp.set_attrs(citations=len(r["citations"]),
                     best_visual=r["best_visual"], best_text=r["best_text"])
        return r


def _retrieve_impl(question: str, user_id: str, *, top_k: int | None = None,
                   video_id: str | None = None,
                   video_ids: list[str] | None = None) -> dict[str, Any]:
    """Multimodal retrieve: query BOTH branches (CLIP frames + transcript text),
    fuse by RRF into time windows, and return numbered moment-citations.

    Returns {citations, best_visual, best_text} — the two raw bests feed the
    confidence gate (RRF scores are too small to threshold on). video_ids scopes
    the search to chosen videos (UI select/unselect)."""
    k = top_k or TOP_K

    # Component 17: opt-in (QUERY_ENHANCEMENT_ENABLED, default false) — widens
    # the candidate pool with LLM-generated sub-questions/paraphrases. The
    # confidence gate below always scores the ORIGINAL question only (never a
    # sub-query), so enabling this can only add candidates, never change the
    # abstain decision. Disabled -> queries == [question] always, and
    # _merge_hits([hits]) is a verified no-op, so behavior is byte-identical
    # to before this component existed.
    queries = [question]
    if config.QUERY_ENHANCEMENT_ENABLED:
        from .query_enhance import enhance_query

        with tracing.span("query_enhance") as _qe:
            queries = enhance_query(question)
            _qe.set_attrs(sub_queries=len(queries), queries=list(queries))

    # Visual branch — CLIP text→image.
    with tracing.span("search_visual", queries=len(queries)) as _sv:
        vhits = _merge_hits([
            vector_store.search(embed_text(q), user_id, top_k=BRANCH_TOP_K,
                               video_id=video_id, video_ids=video_ids)
            for q in queries
        ])
        best_visual = vhits[0]["score"] if vhits else 0.0
        _sv.set_attrs(candidates=len(vhits), best_score=best_visual)

    # Text branch — bge query→transcript-chunk (only if transcript is enabled).
    thits: list[dict] = []
    best_text = 0.0
    if config.ENABLE_TRANSCRIPT:
        # Gate 1 needs a score on the SAME scale CONFIDENCE_THRESHOLD/
        # TEXT_CONFIDENCE_THRESHOLD were calibrated against (a continuous,
        # magnitude-based cosine similarity) — component 15's hybrid fusion
        # score is Qdrant's own RRF, which is rank-quantized, not magnitude-
        # based (verified live: an off-topic query's top RRF score can land
        # nearly as high as an on-topic one's — RRF only encodes WHICH rank a
        # hit got, never how strong the match actually was). So the plain
        # dense-only top score for the ORIGINAL question (unchanged from
        # before component 15/17) still powers the confidence gate; the
        # hybrid, possibly-multi-query calls below only change WHICH
        # candidates get returned for citations, never the gate.
        with tracing.span("search_text", queries=len(queries),
                          hybrid=config.ENABLE_HYBRID_TEXT_SEARCH) as _st:
            gate_hits = vector_store.search_text(embed_query(question), user_id, top_k=1,
                                                 video_id=video_id, video_ids=video_ids)
            best_text = gate_hits[0]["score"] if gate_hits else 0.0
            thits = _merge_hits([
                vector_store.search_text(embed_query(q), user_id, top_k=BRANCH_TOP_K,
                                         video_id=video_id, video_ids=video_ids, query_text=q)
                for q in queries
            ])
            # best_score is the DENSE-ONLY gate score, deliberately: the hybrid
            # score is rank-quantized RRF and not comparable to the thresholds
            # (see the comment above). Recording both avoids a reader assuming
            # the gate judged the fused number.
            _st.set_attrs(candidates=len(thits), best_score=best_text,
                          top_hybrid_score=thits[0]["score"] if thits else 0.0)

    with tracing.span("fuse") as _sf:
        windows = _fuse(vhits, thits)
        _sf.set_attrs(windows=len(windows),
                      top_rrf=round(windows[0]["rrf"], 6) if windows else 0.0)
    if config.RERANK_ENABLED:
        # Component 16: RRF is rank-based (score-agnostic) — a cross-encoder
        # reads the actual question against each window's actual text,
        # correcting ties/near-ties fusion alone can't distinguish. Reranks
        # the FULL fused list, THEN truncates, so top_k gets to pick from
        # every candidate fusion surfaced, not just its own top guesses.
        from .rerank import rerank

        # Record the ordering CHANGE, not just that rerank ran: the reranker
        # quietly demoting the right chunk is a real failure mode (component
        # 16), and it is invisible unless before/after are both captured.
        with tracing.span("rerank", enabled=True, model=config.RERANK_MODEL) as _sr:
            before = [_hit_key(w["text"]) if w.get("text") else ("frame", w.get("video_id"))
                      for w in windows]
            windows = rerank(question, windows)
            after = [_hit_key(w["text"]) if w.get("text") else ("frame", w.get("video_id"))
                     for w in windows]
            _sr.set_attrs(windows_in=len(before), windows_out=len(after),
                          reordered=before != after)
    else:
        with tracing.span("rerank", enabled=False) as _sr:
            _sr.set_attrs(windows_in=len(windows), windows_out=len(windows),
                          reordered=False)
    windows = windows[:k]
    videos = db.videos_by_ids(sorted({w["video_id"] for w in windows if w["video_id"]}))
    documents = db.documents_by_ids(sorted({w["text"]["source_id"] for w in windows
                                            if w["video_id"] is None}))
    citations = []
    for i, w in enumerate(windows, 1):
        if w["video_id"] is not None:
            vid = w["video_id"]
            meta = videos.get(vid)
            fr, tx = w["frame"], w["text"]
            # Anchor on the frame's exact timestamp when there is one (precise
            # visual seek); otherwise the transcript chunk's start.
            ms = int(fr["ms"]) if fr else int(w["t"] * 1000)
            idx = int(fr["idx"]) if fr else None
            citations.append({
                "n": i,
                "kind": "video",
                "video_id": vid,
                "title": (meta or {}).get("title") or vid,
                "url": (meta or {}).get("url"),
                "source": (meta or {}).get("source"),
                "ms": ms,
                "timestamp": _seconds(ms),
                "idx": idx,
                "thumbnail": _thumb_url(user_id, vid, idx) if idx is not None else None,
                "media_url": _media_url(meta, user_id, vid),
                "deeplink": _deeplink(meta, vid, ms),
                "locator": {"start_ms": ms},
                "score": round(w["rrf"], 4),
                "transcript": (tx or {}).get("text"),
                "modalities": sorted(w["modalities"]),
            })
        else:
            # Paper/deck chunk (Assignment 3) — page/slide IS the locator, no
            # timestamp/thumbnail/frame concept applies.
            tx = w["text"]
            doc_id = tx["source_id"]
            meta = documents.get(doc_id)
            kind = tx.get("kind", "document")
            locator_key = "page" if "page" in tx else "slide"
            citations.append({
                "n": i,
                "kind": kind,
                "source_id": doc_id,
                "title": (meta or {}).get("title") or doc_id,
                "uri": (meta or {}).get("uri"),
                "locator": {locator_key: tx.get(locator_key)},
                "score": round(w["rrf"], 4),
                "text": tx.get("text"),
                "modalities": sorted(w["modalities"]),
            })
    return {"citations": citations, "best_visual": best_visual, "best_text": best_text}


def _where(c: dict[str, Any]) -> str:
    """Human-readable locator label regardless of citation kind."""
    if "timestamp" in c:
        return c["timestamp"]
    loc = c.get("locator") or {}
    if "page" in loc:
        return f"page {loc['page']}"
    if "slide" in loc:
        return f"slide {loc['slide']}"
    return ""


def _fallback_answer(citations: list[dict[str, Any]]) -> str:
    """No-LLM summary: rank the closest matches across every source kind.
    Honest about being similarity, not synthesis."""
    top = citations[0]
    label = _where(top)
    where = f"{top['title']} at {label}" if top.get("title") and label else \
            (top.get("title") or label)
    others = ", ".join(f"{_where(c)} [{c['n']}]" for c in citations[1:4])
    msg = f"Closest match: {where} [{top['n']}] (similarity {top['score']})."
    if others:
        msg += f" Other relevant moments: {others}."
    return msg


_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _validate_citations(answer: str, n_frames: int) -> str:
    """Strip invented [n] references the model has no frame for."""
    def fix(m: re.Match) -> str:
        nums = [int(x) for x in re.split(r"\s*,\s*", m.group(1))]
        valid = [str(x) for x in nums if 1 <= x <= n_frames]
        return f"[{', '.join(valid)}]" if valid else ""
    return _CITE_RE.sub(fix, answer)


_NAMED_SOURCE_RE = re.compile(
    r"\bthe\s+([A-Z][\w\-]*(?:\s+[A-Z0-9][\w\-]*){0,4})\s+(?:paper|deck|talk|video|slides?)\b",
    re.IGNORECASE)


def _short_name(title: str) -> str:
    """'CLIP (Radford et al. 2021)' -> 'CLIP'; 'GPT-3: Language Models...'
    -> 'GPT-3' — the casual short form people (and the LLM) actually use."""
    return re.split(r"[:(]", title, maxsplit=1)[0].strip()


def _check_named_source_attribution(answer: str, citations: list[dict[str, Any]],
                                    user_id: str) -> str:
    """Mechanical backstop for when the system prompt's "cite by [n] only"
    rule doesn't hold (see EVIDENCE.md — a prompt-only fix was tried first
    and an adversarial re-check found a new case: "the CLIP paper
    recommends..." with zero CLIP citations retrieved, all LoRA content).
    Unlike that prompt rule, this doesn't depend on the model's compliance:
    it looks for the literal "the X paper/deck/talk/video" naming pattern in
    the generated text and checks whether X is actually a DIFFERENT source
    that exists elsewhere in this tenant's corpus but wasn't cited here —
    the exact, real, mechanical signature of the failure mode found.

    Known limitation, disclosed: this catches the specific phrasing pattern
    all 5 known violations used, not every conceivable way to misattribute
    content — a real backstop, not a full faithfulness verifier. Fails open
    (returns the answer unchanged) on any lookup error — never let a
    hardening check break the read path."""
    try:
        all_sources = db.list_sources(user_id)
    except Exception:
        return answer

    cited_short = {_short_name(c["title"]).lower() for c in citations if c.get("title")}

    def _cited(short: str) -> bool:
        # Prefix/substring, not exact equality: the model's own colloquial
        # short form ("the chain-of-thought paper") frequently doesn't
        # exactly match _short_name()'s derivation from the real title
        # ("Chain-of-Thought Prompting") — an exact-match version of this
        # check missed a real violation with this exact mismatch shape.
        return any(short in c or c in short for c in cited_short)

    uncited = [s["title"] for s in all_sources
              if s.get("title") and not _cited(_short_name(s["title"]).lower())]
    if not uncited:
        return answer

    for m in _NAMED_SOURCE_RE.finditer(answer):
        named = m.group(1).strip().lower()
        for real_title in uncited:
            short = _short_name(real_title).lower()
            if named in short or short in named:
                return (ABSTAIN + f' (The generated answer named "{m.group(1).strip()}" '
                        f"as a source, but {real_title!r} was not actually among what "
                        "was retrieved for this question — withheld rather than risk "
                        "presenting unsupported content as fact.)")
    return answer


def _build_moments(user_id: str, citations: list[dict[str, Any]]) -> list[dict]:
    """Turn citations into what the LLM sees: each moment carries its frame
    image (if any) and/or its text excerpt — transcript for video, page/slide
    text for a paper/deck (Assignment 3) — numbered to match.

    `source` (the citation's own `title`) is included so the model can check
    a question naming a specific paper/video/deck against what's ACTUALLY
    among the moments, instead of guessing from content alone — a bare
    timestamp/locator with no source name gave the LLM no way to tell "this
    excerpt is from a different, unrelated source" from "this is the paper
    you asked about" (see EVIDENCE.md's Part-0 grounding-auditor findings)."""
    def frame_bytes(c):
        if c.get("idx") is None or c.get("video_id") is None:
            return None
        key = f"frame:{user_id}:{c['video_id']}:{c['idx']}"
        cached = cache.get_bytes(key)
        if cached is not None:
            return cached
        try:
            data = storage.get_bytes(storage.frame_key(user_id, c["video_id"], c["idx"]))
        except Exception:
            return None
        cache.set_bytes(key, data, ttl=config.FRAME_CACHE_TTL_S)
        return data

    with ThreadPoolExecutor(max_workers=6) as ex:
        images = list(ex.map(frame_bytes, citations))
    return [{"image": img, "transcript": c.get("transcript") or c.get("text"),
             "timestamp": _where(c), "source": c.get("title")}
            for img, c in zip(images, citations)]


def resolve_llm(user_id: str) -> tuple[llm.LLMConfig | None, str]:
    """Which model answers for this tenant: their own hosted endpoint
    (ms_user_llms — e.g. a vLLM server) first, the server-wide LLM_* env
    config as fallback. Returns (config, source) with source in
    {"user", "server", "none"}."""
    row = db.get_user_llm(user_id)
    if row and row.get("model"):
        return llm.from_row(row), "user"
    cfg = llm.env_config()
    return (cfg, "server") if cfg else (None, "none")


def ask(question: str, user_id: str, *, top_k: int | None = None,
        video_id: str | None = None,
        video_ids: list[str] | None = None) -> dict[str, Any]:
    """Thin wrapper around _ask_impl (DESIGN.md §3c component 18): records
    every call's grounding/abstain outcome in metrics exactly once, without
    restructuring _ask_impl's several early-return paths."""
    # Full provenance on the root: which prompts and which data produced this
    # answer. Without it an eval score is attributable to "whatever was checked
    # out at the time" (component 47).
    with tracing.span("ask", question=question, tenant=user_id,
                      **prompts.versions()) as sp:
        result = _ask_impl(question, user_id, top_k=top_k,
                           video_id=video_id, video_ids=video_ids)
        # Stamped on the ROOT so a trace list is filterable by outcome without
        # opening each one — abstained traces are the interesting ones.
        sp.set_attrs(citations=len(result.get("citations") or []),
                     abstained=bool(result.get("abstained", False)),
                     llm_used=bool(result.get("llm_used", False)))
    metrics.record_ask(result)
    return result


def _ask_impl(question: str, user_id: str, *, top_k: int | None = None,
             video_id: str | None = None,
             video_ids: list[str] | None = None) -> dict[str, Any]:
    r = retrieve(question, user_id, top_k=top_k, video_id=video_id, video_ids=video_ids)
    citations = r["citations"]
    result: dict[str, Any] = {"question": question, "citations": citations}

    if not citations:
        result.update(answer="No relevant moments were found. Try ingesting a video first.",
                      llm_used=False, abstained=True)
        return result

    # Gate 1 — confidence on the RAW per-branch bests (not the RRF score).
    # Abstain only if NEITHER what's on screen nor what's said looks relevant.
    visual_ok = r["best_visual"] >= CONFIDENCE_THRESHOLD
    text_ok = r["best_text"] >= TEXT_CONFIDENCE_THRESHOLD
    gate_abstains = bool(CONFIDENCE_THRESHOLD and not visual_ok and not text_ok)
    # Both scores AND both thresholds: "should it have abstained?" is the first
    # question asked of a bad answer, and it is unanswerable from the scores
    # alone without knowing what they were compared against.
    with tracing.span("confidence_gate") as _sg:
        _sg.set_attrs(best_visual=r["best_visual"], best_text=r["best_text"],
                      visual_threshold=CONFIDENCE_THRESHOLD,
                      text_threshold=TEXT_CONFIDENCE_THRESHOLD,
                      visual_ok=visual_ok, text_ok=text_ok,
                      abstained=gate_abstains)
    if gate_abstains:
        result.update(answer=ABSTAIN, llm_used=False, abstained=True)
        return result

    cfg, source = resolve_llm(user_id)
    if cfg is None:
        # No generative model — summarize the best matches instead of inventing.
        result.update(answer=_fallback_answer(citations), llm_used=False,
                      note=("Retrieval-only results. Connect your own model "
                            "(vLLM/Ollama/API) in settings, or set LLM_API_KEY "
                            "on the server, for a synthesized, grounded answer."))
        return result

    with tracing.span("build_moments") as _sb:
        moments = _build_moments(user_id, citations)
        _sb.set_attrs(moments=len(moments),
                      with_images=sum(1 for m in moments if m.get("image")))
    with tracing.span("llm_answer", model=cfg.model, llm_source=source,
                      prompt_version=prompts.get("answer").version) as _sl:
        raw = llm.answer(question, moments, cfg)
        _sl.set_attrs(answer_chars=len(raw or ""))
    # The two backstops can silently rewrite or withhold an answer. Whether
    # they fired is exactly what you need when an answer looks wrong.
    with tracing.span("grounding_check") as _sc:
        answer = _validate_citations(raw, len(citations))
        checked = _check_named_source_attribution(answer, citations, user_id)
        _sc.set_attrs(citations_stripped=answer != raw,
                      withheld_by_attribution_check=checked != answer)
    result["answer"] = checked
    result["llm_used"] = True
    result["llm_source"] = source          # "user" = their own hosted model
    result["llm_model"] = cfg.model
    return result
