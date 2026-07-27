#!/usr/bin/env python3
"""Benchmark + SLA gate for Assignment 3 — Moment Search at Scale.

    python benchmark/bench.py                 # accept-latency, ingest-vs-search, recall
    python benchmark/bench.py --resilience    # kill a worker mid-ingest, assert no loss
    python benchmark/bench.py --json out.json # also write machine-readable results

Exits non-zero if ANY target in sla.json is missed, so it doubles as your grading
gate and a CI check.

Needs a live stack: BASE_URL reachable, ADMIN_TOKEN set, and (for --resilience)
Docker Compose running the `worker` service in this directory (uses
`docker compose ps -q worker` + `docker kill` — no hardcoded container name).
The concurrent-ingest load and throughput probe submit REAL papers/decks from
benchmark/corpus.json (never fabricated example.com URLs), so they exercise
genuine fetch/parse/embed work, not a fast-failing HTTP GET.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLA = json.loads((ROOT / "benchmark" / "sla.json").read_text())
BASE = os.getenv("BASE_URL", "http://localhost:8100").rstrip("/")
ADMIN = os.getenv("ADMIN_TOKEN", "")


def _req(method, path, body=None, token=None, timeout=30):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), (time.perf_counter() - t0) * 1000
    except Exception as e:  # noqa: BLE001
        return 0, str(e), (time.perf_counter() - t0) * 1000


def p95(xs):
    return statistics.quantiles(xs, n=100)[94] if len(xs) >= 20 else (max(xs) if xs else 0.0)


def measure_accept_latency(n=30):
    """POST /admin/documents should enqueue-and-return fast (no parsing in-request)."""
    lat = []
    for i in range(n):
        st, _, ms = _req("POST", "/admin/documents", token=ADMIN,
                         body={"uri": f"https://example.com/probe_{i}.pdf",
                               "kind": "paper", "title": f"probe {i}"})
        if st == 202:
            lat.append(ms)
    return p95(lat) if lat else float("inf")


def measure_search_p95(n=40):
    q = "what does the survey say about hybrid retrieval"
    lat = []
    for _ in range(n):
        st, _, ms = _req("GET", "/ask_stream?q=" + urllib.parse.quote(q))
        if st == 200:
            lat.append(ms)
    return p95(lat) if lat else float("inf")


# ── Pure helpers (unit-tested in tests/test_bench.py, no network needed) ────

def _sse_events(body: str) -> list[tuple[str, dict]]:
    """Raw SSE text -> [(event_name, data_dict), ...]."""
    events = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if name is not None:
            events.append((name, data or {}))
    return events


def _citations_from_sse(body: str) -> list[dict]:
    for name, data in _sse_events(body):
        if name == "citations":
            return data.get("citations", [])
    return []


def _score_recall(labeled: list[dict], citations_by_query: dict[str, list[dict]]) -> float:
    """Fraction of a query's expect_kinds present in its top-10 citations,
    averaged across all labeled queries. A proxy for true per-source
    recall@10 — document ids are non-deterministic (assigned at registration
    time), so labeled_queries.json expects kinds per corpus_id, not exact ids
    (see that file's own comment)."""
    if not labeled:
        return 0.0
    scores = []
    for q in labeled:
        expect = set(q.get("expect_kinds", []))
        if not expect:
            continue
        got = {c.get("kind") for c in citations_by_query.get(q["query"], [])[:10]}
        scores.append(len(expect & got) / len(expect))
    return sum(scores) / len(scores) if scores else 0.0


def _load_corpus_uris() -> list[dict]:
    """Real, small arXiv paper + deck PDFs from benchmark/corpus.json — used
    as transient load/backfill content for this benchmark ONLY. Never the
    product's shipped corpus (that's seeded once, component 10) and never
    fabricated example.com URLs — real fetch/parse/embed work is what
    actually stresses ingestion capacity."""
    corpus = json.loads((ROOT / "benchmark" / "corpus.json").read_text())
    uris = []
    for t in corpus["triplets"]:
        uris.append({"uri": t["paper_pdf"], "kind": "paper", "title": t["paper_title"]})
        uris.append({"uri": t["deck_pdf"], "kind": "deck", "title": t["deck_note"]})
    return uris


def _cycle_to_n(items: list[dict], n: int) -> list[dict]:
    """Repeat `items` (cycling) until there are exactly n — a corpus of 16
    URIs can still generate a 50-document load batch."""
    if not items:
        return []
    out = []
    i = 0
    while len(out) < n:
        out.append(items[i % len(items)])
        i += 1
    return out


# ── Live-stack glue (needs a running server + worker; not unit-testable) ────

def measure_recall() -> float:
    path = ROOT / "benchmark" / "labeled_queries.json"
    if not path.exists():
        return 0.0
    labeled = json.loads(path.read_text())["queries"]
    by_query = {}
    for q in labeled:
        st, body, _ = _req("GET", "/ask_stream?q=" + urllib.parse.quote(q["query"]))
        by_query[q["query"]] = _citations_from_sse(body) if st == 200 else []
    return _score_recall(labeled, by_query)


def _submit_documents(uris: list[dict]) -> list[str]:
    """POST /admin/documents for each, concurrently (real load, not
    serialized) — returns the accepted ids."""
    from concurrent.futures import ThreadPoolExecutor

    def submit(u):
        st, body, _ = _req("POST", "/admin/documents", token=ADMIN, body=u)
        return json.loads(body)["id"] if st == 202 else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(submit, uris))
    return [i for i in ids if i]


_TERMINAL = {"indexed", "skipped", "failed"}


def _poll_sources_until_terminal(ids: set[str], timeout_s: float,
                                 interval_s: float = 3.0) -> dict[str, str]:
    """Poll GET /admin/sources until every id reaches a terminal status or the
    timeout elapses. Returns the final {id: status} map for the caller to
    judge — a missing id after the timeout is reported as 'missing'."""
    deadline = time.time() + timeout_s
    last: dict[str, str] = {}
    while time.time() < deadline:
        st, body, _ = _req("GET", "/admin/sources")
        if st == 200:
            rows = {s["id"]: s["status"] for s in json.loads(body)["sources"]}
            last = {i: rows.get(i, "missing") for i in ids}
            if all(v in _TERMINAL for v in last.values()):
                break
        time.sleep(interval_s)
    return last


def run_concurrent_ingest_load(n: int = 20) -> None:
    """Fire n REAL document registrations (real arXiv PDFs, not throwaway
    URLs) so search-during-ingest measures genuine fetch/parse/embed
    contention, not just a fast-failing HTTP GET."""
    _submit_documents(_cycle_to_n(_load_corpus_uris(), n))


def measure_throughput(n: int = 16, timeout_s: float = 600.0) -> float:
    """Submit n documents, poll until all reach a terminal state, return
    total indexed chunk_count / elapsed seconds."""
    t0 = time.perf_counter()
    ids = _submit_documents(_cycle_to_n(_load_corpus_uris(), n))
    if not ids:
        return 0.0
    final = _poll_sources_until_terminal(set(ids), timeout_s)
    elapsed = time.perf_counter() - t0

    st, body, _ = _req("GET", "/admin/sources")
    counts = {}
    if st == 200:
        counts = {s["id"]: (s.get("chunk_count") or 0) for s in json.loads(body)["sources"]}
    total = sum(counts.get(i, 0) for i in ids if final.get(i) == "indexed")
    return (total / elapsed) if elapsed > 0 else 0.0


def _worker_container_id() -> str | None:
    import subprocess
    try:
        out = subprocess.run(["docker", "compose", "ps", "-q", "worker"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15)
        lines = out.stdout.strip().splitlines()
        return lines[0] if lines else None
    except Exception:
        return None


def run_resilience_check(n: int = 10, kill_after_s: float = 8.0,
                         timeout_s: float = 300.0) -> bool:
    """Submit a batch, let ingestion actually start, kill the worker mid-
    stream, and assert every source still reaches a terminal state. Docker
    Compose's `restart: unless-stopped` on the worker service brings it back
    automatically; Prefect redelivers the interrupted run once a worker is
    polling again — this asserts that promise holds, it doesn't manufacture it."""
    import subprocess

    ids = _submit_documents(_cycle_to_n(_load_corpus_uris(), n))
    if not ids:
        print("[resilience] no documents were accepted — is the stack up?")
        return False
    time.sleep(kill_after_s)  # let some runs reach fetching/parsing/embedding

    cid = _worker_container_id()
    if not cid:
        print("[resilience] could not find the worker container via "
             "'docker compose ps -q worker' — is the stack up?")
        return False
    subprocess.run(["docker", "kill", cid], timeout=15)
    print(f"[resilience] killed worker container {cid[:12]} mid-ingest")

    final = _poll_sources_until_terminal(set(ids), timeout_s)
    lost = [i for i, s in final.items() if s not in _TERMINAL]
    if lost:
        print(f"[resilience] {len(lost)} source(s) never reached a terminal state: {lost}")
    return not lost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resilience", action="store_true")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    results, failures = {}, []

    def gate(name, value, ok, target):
        results[name] = {"value": value, "target": target, "pass": bool(ok)}
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {value} (target {target})")
        if not ok:
            failures.append(name)

    if args.resilience:
        no_loss = run_resilience_check()
        gate("no_loss_under_crash", no_loss, no_loss and SLA["no_loss_required"], "0 dropped, all indexed")
        return sys.exit(1 if failures else 0)

    # 1. accept latency
    a = measure_accept_latency()
    gate("accept_latency_p95_ms", round(a, 1), a <= SLA["accept_latency_p95_ms"], SLA["accept_latency_p95_ms"])

    # 2. search stays fast during a big ingest — run search-p95 concurrently
    # with a real background ingest load, not sequentially.
    from concurrent.futures import ThreadPoolExecutor
    idle = measure_search_p95()
    with ThreadPoolExecutor(max_workers=1) as ex:
        load_fut = ex.submit(run_concurrent_ingest_load, 20)
        during = measure_search_p95()
        load_fut.result()
    ratio = (during / idle) if idle else float("inf")
    gate("search_p95_during_ingest_ratio", round(ratio, 2),
         ratio <= SLA["search_p95_during_ingest_ratio_max"], SLA["search_p95_during_ingest_ratio_max"])

    # 3. recall@10 on labeled queries (benchmark/labeled_queries.json)
    recall = round(measure_recall(), 3)
    gate("recall_at_10", recall, recall >= SLA["recall_at_10_min"], SLA["recall_at_10_min"])

    # 4. ingestion throughput — a fresh, known backfill, chunks indexed / elapsed
    throughput = round(measure_throughput(), 2)
    gate("ingest_throughput_chunks_per_s", throughput,
         throughput >= SLA["ingest_throughput_min_chunks_per_s"], SLA["ingest_throughput_min_chunks_per_s"])

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json_out}")

    print(f"\n{'ALL SLAs PASS' if not failures else 'SLA FAILURES: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
