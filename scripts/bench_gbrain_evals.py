#!/usr/bin/env python3
"""Run tier5-fuzzy retrieval queries against Nexus and report metrics.

Reads:
  - /tmp/eval-corpus/world-v1/*.json   (240 corpus pages)
  - /tmp/tier5_fuzzy.json              (30 fuzzy queries)

Writes corpus to a Nexus eval zone, runs each query through
``/api/v2/search/query`` (Nexus pipeline: BM25 via pgtext + dense vectors via
pgvector + RRF fusion + page-level max-pool aggregation), and prints
P@5 / R@5 / MRR / hits alongside reference numbers from a public
multi-adapter benchmark on the same corpus.

Embedding cache: pgvector storage in the Nexus container survives stack
restarts. ``--skip-index`` reuses the existing zone (no re-embedding) —
same effect as a content-hash cache for our read-mostly benchmark loop.

Env:
  NEXUS_URL, NEXUS_API_KEY  — from ``eval $(nexus env)``
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CORPUS_DIR = Path(
    os.environ.get(
        "BENCH_CORPUS_DIR",
        "/tmp/eval-corpus/world-v1",
    )
)
QUERIES_FILE = Path(
    os.environ.get(
        "BENCH_QUERIES_FILE",
        "/tmp/tier5_fuzzy.json",
    )
)
ZONE_PREFIX = os.environ.get("BENCH_ZONE_PREFIX", "/workspace/eval-corpus")
NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:14250").rstrip("/")
API_KEY = os.environ.get("NEXUS_API_KEY", "")

# Reference numbers from a public multi-adapter benchmark on the same corpus
# (145 relational queries, not tier5-fuzzy — directional anchors only).
REFERENCE_BASELINE = [
    ("knowledge-graph (full)", 49.1, 97.9),
    ("vector + grep + RRF", 17.8, 65.1),
    ("grep-only", 17.1, 62.4),
    ("vector-only", 10.8, 40.7),
]


def page_to_text(page: dict) -> str:
    """Flatten a world-v1 JSON page into a single text body for indexing."""
    parts = [page.get("title", ""), "", page.get("compiled_truth", "")]
    timeline = page.get("timeline")
    if timeline:
        parts += ["", "## Timeline"]
        if isinstance(timeline, list):
            parts += [str(item) for item in timeline]
        else:
            parts.append(str(timeline))
    return "\n".join(p for p in parts if p is not None)


def slug_to_path(slug: str) -> str:
    return f"{ZONE_PREFIX}/{slug}.md"


def path_to_slug(path: str) -> str:
    if not path.startswith(f"{ZONE_PREFIX}/"):
        return path
    return path[len(ZONE_PREFIX) + 1 :].removesuffix(".md")


def http(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: int = 60,
) -> dict:
    url = f"{NEXUS_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_corpus() -> list[dict]:
    pages = []
    for f in sorted(CORPUS_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        page = json.loads(f.read_text())
        pages.append(page)
    return pages


def index_corpus(pages: list[dict], batch_size: int = 50) -> None:
    """Push every page directly into the search index via /search/index.

    Bypasses the file-write auto-index path which is currently broken on
    legacy-key auth (UNAUTHORIZED warnings + skipped indexing despite 200 OK).
    /search/index is the explicit indexing endpoint and triggers OpenAI
    embeddings immediately.
    """
    print(
        f"  indexing {len(pages)} pages directly via /search/index (batches of {batch_size})...",
        flush=True,
    )
    t0 = time.perf_counter()
    for i in range(0, len(pages), batch_size):
        chunk = pages[i : i + batch_size]
        docs = []
        for p in chunk:
            docs.append(
                {
                    "id": slug_to_path(p["slug"]),
                    "text": page_to_text(p),
                    "path": slug_to_path(p["slug"]),
                }
            )
        http("POST", "/api/v2/search/index", json_body={"documents": docs}, timeout=300)
        sys.stdout.write(
            f"    batch {i // batch_size + 1}: +{len(chunk)} pages "
            f"[{time.perf_counter() - t0:.1f}s]\n"
        )
        sys.stdout.flush()
    print(f"  index calls complete in {time.perf_counter() - t0:.1f}s", flush=True)


def wait_for_index(expected: int, marker_slug: str, max_wait: int = 600) -> None:
    """Poll a known-content query until the search daemon has indexed at
    least ``expected`` pages. Indexing in Nexus is asynchronous; for OpenAI
    embeddings on 240 pages it can take a few minutes."""
    print(f"  waiting for daemon to index >= {expected} pages...", flush=True)
    t0 = time.perf_counter()
    last_count = -1
    while time.perf_counter() - t0 < max_wait:
        try:
            r = http(
                "GET",
                "/api/v2/search/query",
                params={
                    "q": marker_slug.split("/")[-1],
                    "path": ZONE_PREFIX,
                    "limit": 1,
                    "type": "keyword",
                },
                timeout=30,
            )
            results = r.get("results", [])
            if results:
                # Heuristic: query the corpus stats endpoint if it exists,
                # otherwise rely on a marker hit + a settle period.
                pass
            # Use stats endpoint for an accurate count.
            stats = http("GET", "/api/v2/search/stats", timeout=15)
            count = int(stats.get("indexed_chunks", stats.get("total_chunks", 0)))
            if count != last_count:
                print(f"    indexed_chunks={count}  [{time.perf_counter() - t0:.1f}s]", flush=True)
                last_count = count
            if count >= expected:
                print(
                    f"  done waiting (count={count}, {time.perf_counter() - t0:.1f}s)", flush=True
                )
                return
        except Exception as e:
            print(f"    poll error: {e}", flush=True)
        time.sleep(5)
    raise SystemExit(f"  TIMEOUT: corpus did not reach {expected} indexed chunks in {max_wait}s")


def search(q: str, limit: int = 20) -> list[dict]:
    r = http(
        "GET",
        "/api/v2/search/query",
        params={
            "q": q,
            "path": ZONE_PREFIX,
            "limit": limit,
            "type": "hybrid",
        },
        timeout=30,
    )
    return r.get("results", [])


def aggregate_to_slugs(results: list[dict]) -> list[tuple[str, float]]:
    """Collapse chunk-level results to slug-level (max chunk score per slug),
    sorted desc. Page aggregation in Nexus already does this on the server
    side — this is a defensive double-check."""
    by_slug: dict[str, float] = {}
    for r in results:
        slug = path_to_slug(r.get("path", ""))
        if not slug:
            continue
        score = float(r.get("score", 0.0))
        if slug not in by_slug or score > by_slug[slug]:
            by_slug[slug] = score
    return sorted(by_slug.items(), key=lambda kv: kv[1], reverse=True)


def evaluate(queries: list[dict], k: int = 5) -> dict:
    p_at_k = []
    r_at_k = []
    mrr_terms = []
    hits_total = 0
    per_query = []
    latencies_ms = []
    for q in queries:
        relevant = set(q["gold"]["relevant"])
        if not relevant:
            continue
        t0 = time.perf_counter()
        raw = search(q["text"], limit=k * 4)
        latencies_ms.append((time.perf_counter() - t0) * 1000)
        ranked = aggregate_to_slugs(raw)[:k]
        ranked_slugs = [s for s, _ in ranked]
        hits = sum(1 for s in ranked_slugs if s in relevant)
        p_at_k.append(hits / k)
        r_at_k.append(hits / len(relevant))
        # MRR: 1 / rank of first relevant hit (0 if none)
        mrr = 0.0
        for idx, s in enumerate(ranked_slugs, start=1):
            if s in relevant:
                mrr = 1.0 / idx
                break
        mrr_terms.append(mrr)
        if hits > 0:
            hits_total += 1
        per_query.append(
            {
                "id": q["id"],
                "text": q["text"][:80],
                "gold": list(relevant),
                "got": ranked_slugs,
                "hits": hits,
                "mrr": mrr,
            }
        )
    return {
        "p_at_5": statistics.mean(p_at_k) if p_at_k else 0.0,
        "r_at_5": statistics.mean(r_at_k) if r_at_k else 0.0,
        "mrr": statistics.mean(mrr_terms) if mrr_terms else 0.0,
        "hits_any": f"{hits_total}/{len(p_at_k)}",
        "queries": len(p_at_k),
        "latency_p50_ms": statistics.median(latencies_ms) if latencies_ms else 0.0,
        "latency_p95_ms": (
            statistics.quantiles(latencies_ms, n=20)[-1]
            if len(latencies_ms) >= 20
            else max(latencies_ms or [0.0])
        ),
        "per_query": per_query,
    }


def print_report(metrics: dict, n_queries: int) -> None:
    print()
    print("=" * 72)
    print(f"NEXUS RESULTS — {n_queries} tier5-fuzzy queries (text-embedding-3-large@1536d)")
    print("=" * 72)
    print(f"  P@5:        {metrics['p_at_5'] * 100:5.1f}%")
    print(f"  R@5:        {metrics['r_at_5'] * 100:5.1f}%")
    print(f"  MRR:        {metrics['mrr']:5.3f}")
    print(f"  Hits (≥1 relevant in top-5): {metrics['hits_any']}")
    print(
        f"  Latency:    p50={metrics['latency_p50_ms']:.0f}ms  "
        f"p95={metrics['latency_p95_ms']:.0f}ms"
    )
    print()
    print("REFERENCE BASELINES (145 relational queries, public report):")
    print(f"  {'Adapter':<28}  {'P@5':>6}  {'R@5':>6}")
    for name, p, r in REFERENCE_BASELINE:
        print(f"  {name:<28}  {p:5.1f}%  {r:5.1f}%")
    print()
    print("PER-QUERY MISSES (gold not in top-5):")
    for pq in metrics["per_query"]:
        if pq["hits"] == 0:
            print(f"  ✗ {pq['id']}: {pq['text']}")
            print(f"      gold: {pq['gold']}")
            print(f"      got:  {pq['got'][:5]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--skip-index",
        action="store_true",
        help="Reuse existing corpus in the zone (skip re-upload + re-embed)",
    )
    ap.add_argument("--save-results", default="/tmp/nexus_bench_results.json")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("NEXUS_API_KEY not set — run `eval $(nexus env)` first")

    queries = json.loads(QUERIES_FILE.read_text())
    print(f"Loaded {len(queries)} tier5-fuzzy queries", flush=True)

    pages = load_corpus()
    print(f"Loaded {len(pages)} world-v1 pages", flush=True)

    # 6 queries expect abstention (no gold to find) — split them out so they
    # don't pollute P@5/R@5; they need a different metric (false-positive rate).
    abstention_queries = [q for q in queries if q.get("gold", {}).get("expected_abstention")]
    queries = [q for q in queries if "relevant" in q.get("gold", {})]
    print(
        f"  {len(abstention_queries)} abstention queries excluded; "
        f"{len(queries)} retrieval queries kept",
        flush=True,
    )

    # Verify all gold slugs exist in corpus (catch query-corpus mismatch).
    corpus_slugs = {p["slug"] for p in pages}
    missing_gold = []
    for q in queries:
        for slug in q["gold"]["relevant"]:
            if slug not in corpus_slugs:
                missing_gold.append((q["id"], slug))
    if missing_gold:
        print(
            f"  WARN: {len(missing_gold)} gold slugs not in corpus (queries may be unwinnable):",
            flush=True,
        )
        for qid, slug in missing_gold[:5]:
            print(f"    {qid} → {slug}", flush=True)

    if not args.skip_index:
        index_corpus(pages)
        # /search/index returns when the index call finishes. With OpenAI
        # embeddings the API call is synchronous inside the request, so by the
        # time index_corpus() returns, embeddings are persisted in pgvector.
        # Brief settle for the BM25 / FTS sidecars.
        time.sleep(3)

    metrics = evaluate(queries, k=5)
    print_report(metrics, n_queries=len(queries))

    Path(args.save_results).write_text(json.dumps(metrics, indent=2))
    print(f"\nResults saved to {args.save_results}")


if __name__ == "__main__":
    main()
