#!/usr/bin/env python3
"""Answer relevancy + faithfulness (LLM-judge) — DESIGN.md §3a component 13.

    python -m benchmark.answer_quality

Neither relevancy (does the answer address the question) nor faithfulness
(is every cited claim actually supported by its citation's own text) had any
eval before this — recall@10 (component 9) and precision@10 (component 12)
only ever look at retrieval, never at the generated answer text itself.

For each of benchmark/labeled_queries.json's queries: call the live
/ask_stream (same as bench.py's recall/precision diagnostics), then judge the
returned answer with the server's own configured judge model (LLM_* env vars,
OpenAI-compatible Chat Completions — the same provider path src/llm.py's
default "openai" branch uses, temperature 0) on two axes:

  1. relevancy (1-5): does the answer directly and completely address the
     question, independent of correctness.
  2. faithfulness: for every bracketed [n] citation the answer actually uses,
     is that specific claim supported by citation n's own retrieved text
     (the same text/transcript the answer-generating LLM itself was shown)?

Disclosed limitations: (a) an LLM-judge is inherently noisy — this reports a
measurement, not ground truth; (b) only the "openai"-compatible provider
shape is supported for the judge call (matches this deployment's LLM_PROVIDER
default; Anthropic-judge was not built, out of scope for this pass).

Needs a live stack (BASE_URL) and a judge model configured via LLM_API_KEY/
LLM_BASE_URL/LLM_MODEL (same env vars src/config.py's server-wide LLM uses).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from benchmark.bench import QUALITY, ROOT, _labeled_queries, _req

_JUDGE_TIMEOUT_S = 60


# The judge's STATIC instructions, hoisted to a module constant so component 47
# can version it by content hash. Kept byte-identical to the original inline
# text: changing it would silently invalidate component 13's recorded
# relevancy/faithfulness numbers.
# Registered with src.prompts by the BENCHMARK, not by the app: the serving
# container has no `benchmark/` package, so an app-side import of this module
# silently yielded an empty prompt registry in production (component 47).
def _register_judge_prompt():
    try:
        from src import prompts as _p
        return _p.register("judge", JUDGE_SYSTEM).version
    except Exception:
        return None


JUDGE_SYSTEM = (
    "You are evaluating a RAG system's answer for quality. Score two things:\n"
    "1. relevancy (integer 1-5): does the ANSWER directly and completely address "
    "the QUESTION? (1 = off-topic, 5 = fully addresses it). Score relevance/"
    "completeness only, not correctness.\n"
    "2. faithfulness: for EVERY bracketed citation number [n] that appears in the "
    "ANSWER, check whether the specific claim next to it is actually supported by "
    "that SOURCE's text below. List every citation number used and whether it is "
    "supported.\n\n"
    "Respond with ONLY minified JSON, no prose, in exactly this shape:\n"
    '{"relevancy": <int 1-5>, "citations_checked": '
    '[{"n": <int>, "supported": <true|false>}, ...]}\n\n'
)

JUDGE_PROMPT_VERSION = _register_judge_prompt()


def _build_judge_prompt(question: str, answer: str, citations: list[dict]) -> str:
    """Deterministic prompt construction — pure, unit-tested. Each numbered
    source line carries whatever retrieved text the answering LLM itself saw
    (citation["text"] for a paper/deck chunk, citation["transcript"] for a
    video moment), so the judge can check a claim against the SAME evidence,
    not re-derive it from scratch."""
    lines = [f"[{c.get('n')}] {c.get('title', '')}: {c.get('text') or c.get('transcript') or ''}"
            for c in citations]
    sources = "\n".join(lines)
    return (
        JUDGE_SYSTEM
        + f"QUESTION: {question}\n\nANSWER: {answer}\n\nSOURCES:\n{sources}"
    )


def _parse_judge_response(raw: str) -> dict | None:
    """Judge output -> {"relevancy": int, "citations_checked": [...]}, or
    None on anything unparseable/malformed — a judge-call failure never
    crashes the whole run, it just drops that one query (see _aggregate)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or "relevancy" not in parsed or "citations_checked" not in parsed:
        return None
    return parsed


def _aggregate(judged: list[dict | None]) -> dict:
    """Mean relevancy + overall faithfulness pass-rate across every query that
    was successfully judged. A query whose answer cited nothing (e.g. an
    abstain) contributes 0 citations_checked, not a faithfulness penalty."""
    ok = [j for j in judged if j]
    if not ok:
        return {"mean_relevancy": 0.0, "faithfulness_rate": 0.0,
                "queries_judged": 0, "citations_checked": 0}
    relevancies = [j["relevancy"] for j in ok]
    all_checks = [c for j in ok for c in j["citations_checked"]]
    supported = sum(1 for c in all_checks if c.get("supported"))
    return {
        "mean_relevancy": sum(relevancies) / len(relevancies),
        "faithfulness_rate": (supported / len(all_checks)) if all_checks else 0.0,
        "queries_judged": len(ok),
        "citations_checked": len(all_checks),
    }


# ── Live-stack + live-judge glue (needs a running server + configured LLM) ──

def _judge_call(prompt: str) -> str:
    base_url = (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    body = json.dumps({"model": model, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if api_key:
        req.add_header("authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=_JUDGE_TIMEOUT_S) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"]


def _ask(question: str) -> tuple[str, list[dict], str | None]:
    import urllib.parse

    from benchmark.bench import _citations_from_sse, _sse_events
    st, body, _ = _req("GET", "/ask_stream?q=" + urllib.parse.quote(question))
    if st != 200:
        return "", [], None
    answer, trace_id = "", None
    for name, data in _sse_events(body):
        if name == "answer":
            answer = data.get("answer", "")
            trace_id = data.get("trace_id") or trace_id
    return answer, _citations_from_sse(body), trace_id


def measure_answer_quality() -> dict:
    labeled = _labeled_queries()
    if not labeled:
        return _aggregate([])
    judged: list[dict | None] = []
    per_query: list[dict] = []
    for q in labeled:
        answer, citations, trace_id = _ask(q["query"])
        if not answer or not citations:
            judged.append(None)
            continue
        prompt = _build_judge_prompt(q["query"], answer, citations)
        try:
            raw = _judge_call(prompt)
        except Exception as exc:  # noqa: BLE001 -- best-effort, one bad judge call must not sink the run
            print(f"[answer_quality] judge call failed for {q['query']!r}: "
                 f"{type(exc).__name__}: {exc}")
            judged.append(None)
            continue
        parsed = _parse_judge_response(raw)
        judged.append(parsed)
        if parsed and trace_id:
            checks = parsed.get("citations_checked") or []
            supported = sum(1 for c in checks if c.get("supported"))
            per_query.append({
                "trace_id": trace_id,
                "relevancy": parsed.get("relevancy"),
                "faithfulness": (supported / len(checks)) if checks else None,
            })
    result = _aggregate(judged)
    result["per_query"] = per_query
    return result


def main():
    result = measure_answer_quality()
    relevancy = round(result["mean_relevancy"], 2)
    faithfulness = round(result["faithfulness_rate"], 3)
    print(f"queries judged: {result['queries_judged']} / "
         f"{len(_labeled_queries())}, citations checked: {result['citations_checked']}")

    ok_relevancy = relevancy >= QUALITY["answer_relevancy_min"]
    ok_faithfulness = faithfulness >= QUALITY["answer_faithfulness_min"]
    print(f"[{'PASS' if ok_relevancy else 'FAIL'}] answer_relevancy: {relevancy} "
         f"(target {QUALITY['answer_relevancy_min']})")
    print(f"[{'PASS' if ok_faithfulness else 'FAIL'}] answer_faithfulness: {faithfulness} "
         f"(target {QUALITY['answer_faithfulness_min']})")

    # Component 48: record the run in Opik so this score is comparable to the
    # next one. Deliberately AFTER the pass/fail decision and unable to change
    # it — quality_gates.json is the judge, Opik is only the record. No-op
    # unless OPIK_API_KEY is set.
    try:
        from benchmark import opik_dataset              # `python -m benchmark.answer_quality`
    except ImportError:
        try:
            import opik_dataset                        # direct-script invocation
        except Exception:
            opik_dataset = None
    except Exception:      # see the note in bench.py: import itself can fail
        opik_dataset = None
    if opik_dataset is None:
        sys.exit(0 if (ok_relevancy and ok_faithfulness) else 1)
    opik_dataset.push_labeled_queries()
    # Per-query scores land on the traces that produced those answers, so a
    # regression points at the spans that caused it (component 48).
    opik_dataset.log_query_scores(result.get("per_query") or [])
    exp = opik_dataset.log_experiment("answer_quality", {
        "mean_relevancy": relevancy,
        "faithfulness_rate": faithfulness,
        "queries_judged": result["queries_judged"],
        "citations_checked": result["citations_checked"],
    })
    if exp:
        print(f"recorded in Opik: experiment {exp} (dataset {opik_dataset.DATASET_NAME})")

    sys.exit(0 if (ok_relevancy and ok_faithfulness) else 1)


if __name__ == "__main__":
    main()

