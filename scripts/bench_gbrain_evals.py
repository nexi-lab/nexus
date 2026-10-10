#!/usr/bin/env python3
"""Run retrieval queries against the Rust cluster and report quality and latency.

Writes corpus files through gRPC and indexes their VFS directory through the
Rust HTTP API. Use a dedicated BENCH_ZONE_PREFIX under the selected zone's mount.
Upload and indexing require an admin credential; --skip-index only queries.

Environment:
  NEXUS_URL: Rust cluster HTTP URL
  NEXUS_API_KEY, NEXUS_ZONE_ID: caller credential and optional zone
  NEXUS_GRPC_PORT, NEXUS_GRPC_TLS, NEXUS_TLS_* or NEXUS_DATA_DIR:
      standard SDK gRPC connection settings for uploads
  BENCH_CORPUS_DIR: world-v1 JSON pages (default /tmp/eval-corpus/world-v1)
  BENCH_QUERIES_FILE: retrieval queries (default /tmp/tier5_fuzzy.json)
  BENCH_ZONE_PREFIX: VFS corpus directory (default /workspace/eval-corpus)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

from nexus.cli.clients.search import SearchClient
from nexus.remote.grpc_target import resolve_grpc_target
from nexus.remote.rpc_transport import RPCTransport

CORPUS_DIR = Path(os.environ.get("BENCH_CORPUS_DIR", "/tmp/eval-corpus/world-v1"))
QUERIES_FILE = Path(os.environ.get("BENCH_QUERIES_FILE", "/tmp/tier5_fuzzy.json"))
ZONE_PREFIX = os.environ.get("BENCH_ZONE_PREFIX", "/workspace/eval-corpus").rstrip("/")
NEXUS_URL = os.environ.get("NEXUS_URL", "http://localhost:14250").rstrip("/")
API_KEY = os.environ.get("NEXUS_API_KEY", "")
ZONE_ID = os.environ.get("NEXUS_ZONE_ID")


def page_to_text(page: dict) -> str:
    """Flatten a world-v1 page into text for its VFS file."""
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
    if not isinstance(slug, str) or any(part in ("", ".", "..") for part in slug.split("/")):
        raise ValueError(f"Invalid corpus slug: {slug!r}")
    if "\\" in slug or "\x00" in slug:
        raise ValueError(f"Invalid corpus slug: {slug!r}")
    return f"{ZONE_PREFIX}/{slug}.md"


def path_to_slug(path: str) -> str:
    return path.removeprefix(f"{ZONE_PREFIX}/").removesuffix(".md")


def load_corpus() -> list[dict]:
    pages = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CORPUS_DIR.glob("*.json"))
        if not path.name.startswith("_")
    ]
    if not pages:
        raise ValueError(f"No corpus pages in {CORPUS_DIR}")
    paths = [slug_to_path(page["slug"]) for page in pages]
    if len(paths) != len(set(paths)):
        raise ValueError("Corpus slugs must be unique")
    return pages


def index_corpus(pages: list[dict], client: SearchClient) -> None:
    """Write durable VFS files, then consume the host's completed index result."""
    print(f"Uploading and indexing {len(pages)} corpus pages...", flush=True)
    start = time.perf_counter()
    address, _, tls = resolve_grpc_target(NEXUS_URL, trust_local_project=False)
    with closing(RPCTransport(address, auth_token=API_KEY, tls_config=tls, timeout=300)) as fs:
        directories = {str(PurePosixPath(slug_to_path(page["slug"])).parent) for page in pages}
        for directory in sorted(directories):
            fs.mkdir(directory, parents=True, exist_ok=True)
        for page in pages:
            fs.write_file(slug_to_path(page["slug"]), page_to_text(page).encode("utf-8"))
    result = client.index(ZONE_PREFIX, recursive=True)
    if result["indexed_count"] != len(pages) or result["skipped_count"]:
        raise RuntimeError(
            f"Corpus indexing was incomplete or the prefix contains other files: {result}"
        )
    print(f"Index complete in {time.perf_counter() - start:.1f}s", flush=True)


def aggregate_to_slugs(results: list[dict]) -> list[tuple[str, float]]:
    """Score retrieval at the corpus page level using each page's best hit."""
    by_slug: dict[str, float] = {}
    for result in results:
        slug = path_to_slug(result.get("path", ""))
        if not slug:
            continue
        score = float(result.get("score", 0.0))
        by_slug[slug] = max(by_slug.get(slug, score), score)
    return sorted(by_slug.items(), key=lambda item: item[1], reverse=True)


def evaluate(queries: list[dict], client: SearchClient, *, mode: str, k: int = 5) -> dict:
    precisions, recalls, reciprocals, latencies = [], [], [], []
    per_query = []
    hits_total = 0
    for query in queries:
        relevant = set(query["gold"]["relevant"])
        if not relevant:
            continue
        start = time.perf_counter()
        raw = client.query(query["text"], path_filter=ZONE_PREFIX, limit=k * 4, query_type=mode)[
            "results"
        ]
        latencies.append((time.perf_counter() - start) * 1000)
        ranked = [slug for slug, _ in aggregate_to_slugs(raw)[:k]]
        hits = sum(slug in relevant for slug in ranked)
        precisions.append(hits / k)
        recalls.append(hits / len(relevant))
        reciprocals.append(
            next((1.0 / rank for rank, slug in enumerate(ranked, 1) if slug in relevant), 0.0)
        )
        hits_total += hits > 0
        per_query.append(
            {
                "id": query["id"],
                "text": query["text"][:80],
                "gold": sorted(relevant),
                "got": ranked,
                "hits": hits,
                "mrr": reciprocals[-1],
            }
        )
    return {
        "query_type": mode,
        "p_at_5": statistics.mean(precisions) if precisions else 0.0,
        "r_at_5": statistics.mean(recalls) if recalls else 0.0,
        "mrr": statistics.mean(reciprocals) if reciprocals else 0.0,
        "hits_any": f"{hits_total}/{len(precisions)}",
        "queries": len(precisions),
        "latency_p50_ms": statistics.median(latencies) if latencies else 0.0,
        "latency_p95_ms": (
            statistics.quantiles(latencies, n=20)[-1]
            if len(latencies) >= 20
            else max(latencies or [0.0])
        ),
        "per_query": per_query,
    }


def print_report(metrics: dict) -> None:
    print(f"\nNEXUS RESULTS: {metrics['queries']} retrieval queries ({metrics['query_type']})")
    print(f"  P@5: {metrics['p_at_5'] * 100:.1f}%")
    print(f"  R@5: {metrics['r_at_5'] * 100:.1f}%")
    print(f"  MRR: {metrics['mrr']:.3f}")
    print(f"  Hits: {metrics['hits_any']}")
    print(f"  Latency: p50={metrics['latency_p50_ms']:.0f}ms p95={metrics['latency_p95_ms']:.0f}ms")
    for query in metrics["per_query"]:
        if not query["hits"]:
            print(f"  Miss {query['id']}: {query['text']}")
            print(f"    gold: {query['gold']}")
            print(f"    got: {query['got']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-index", action="store_true", help="Query the existing corpus only")
    parser.add_argument("--mode", choices=("keyword", "semantic", "hybrid"), default="hybrid")
    parser.add_argument("--save-results", default="/tmp/nexus_bench_results.json")
    args = parser.parse_args()
    if not API_KEY:
        sys.exit("NEXUS_API_KEY is required")
    if (
        not ZONE_PREFIX.startswith("/")
        or any(part in ("", ".", "..") for part in ZONE_PREFIX[1:].split("/"))
        or "\\" in ZONE_PREFIX
        or "\x00" in ZONE_PREFIX
    ):
        sys.exit("BENCH_ZONE_PREFIX must be an absolute VFS directory path")
    pages = load_corpus()
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    queries = [query for query in queries if query.get("gold", {}).get("relevant")]
    if not queries:
        sys.exit("The dataset has no retrieval queries with relevant pages")
    corpus_slugs = {page["slug"] for page in pages}
    missing = {slug for query in queries for slug in query["gold"]["relevant"]} - corpus_slugs
    if missing:
        sys.exit(f"Query gold pages are missing from the corpus: {sorted(missing)}")
    with SearchClient(NEXUS_URL, API_KEY, zone_id=ZONE_ID, timeout=300) as client:
        if not args.skip_index:
            index_corpus(pages, client)
        metrics = evaluate(queries, client, mode=args.mode)
    print_report(metrics)
    Path(args.save_results).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Results saved to {args.save_results}")


if __name__ == "__main__":
    main()
