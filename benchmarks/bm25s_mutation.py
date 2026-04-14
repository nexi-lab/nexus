#!/usr/bin/env python3
"""Benchmark: BM25S rebuild cost and memory under mutation load (Issue #3707).

Measures three hotspots in ``BM25SIndex``:

1. **_merge_delta** — retokenizes the *entire* corpus every 100 delta
   updates, not just the delta.  CPU spike is proportional to total
   corpus size.
2. **delete_document** — rebuilds the entire BM25 retriever on every
   single delete (full retokenization + re-index).
3. **Corpus RAM** — ``self._corpus`` keeps the full text of every
   document in memory alongside the BM25 sparse structures.

Usage::

    python benchmarks/bm25s_mutation.py            # default: 1x 5x scales
    python benchmarks/bm25s_mutation.py --quick     # 1x only (CI-safe)
    python benchmarks/bm25s_mutation.py --json      # emit JSON for analysis
    python benchmarks/bm25s_mutation.py --scales 1,5,10

HERB corpus baseline: 2,113 files, realistic enterprise content.

Expected output (example, numbers vary)::

     Scale   Files  Ingest(s)  Corpus RAM(MB)  Merge 100(s)  Delete 1(s)
    ----------------------------------------------------------------------
        1x   2,113      1.234          12.3         1.056        1.012
        5x  10,565      6.789          61.5         5.280        5.040
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the repo ``src/`` tree is importable when running as a script
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HERB enterprise-context corpus = 2,113 files (see benchmarks/herb/README.md)
HERB_FILES: int = 2_113

# Number of mutations that trigger _merge_delta (matches BM25SIndex._delta_threshold)
DELTA_THRESHOLD: int = 100

DEFAULT_SCALES: list[int] = [1, 5]

# Synthetic document body (~500 chars, realistic enterprise paragraph).
_DOC_BODY: str = (
    "The authentication middleware validates JWT tokens against the identity "
    "provider before forwarding requests to downstream microservices. Each "
    "service maintains its own rate limiter configured via environment variables. "
    "When the circuit breaker trips, requests are shed to a fallback handler "
    "that returns cached responses from the Redis layer. Metrics are exported "
    "via OpenTelemetry to the Grafana stack for real-time alerting. "
    "Configuration is managed through a central etcd cluster with versioned "
    "keys and automatic rollback on validation failure. "
)


def _make_doc(idx: int) -> tuple[str, str, str]:
    """Generate a synthetic (path_id, path, content) tuple."""
    path_id = str(uuid.uuid4())
    path = f"/workspace/herb/project_{idx // 500:03d}/file_{idx:06d}.md"
    # Vary content slightly so tokenization isn't trivially cached
    content = f"# Document {idx}\n\n{_DOC_BODY}\n\nIndex: {idx}, ID: {path_id}\n"
    return (path_id, path, content)


def _make_update_doc(idx: int, original_path_id: str, original_path: str) -> tuple[str, str, str]:
    """Generate an updated version of an existing document."""
    content = (
        f"# Updated Document {idx}\n\n"
        f"REVISION 2 — This file was modified during the mutation benchmark.\n\n"
        f"{_DOC_BODY}\n\nUpdated index: {idx}, ID: {original_path_id}\n"
    )
    return (original_path_id, original_path, content)


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _measure_ram_snapshot() -> float:
    """Return current traced memory in MB (requires tracemalloc active)."""
    current, _ = tracemalloc.get_traced_memory()
    return current / 1_048_576


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------


async def measure_one(n_files: int) -> dict[str, Any]:
    """Run the full benchmark at *n_files* scale and return metrics."""
    try:
        from nexus.bricks.search.bm25s_search import BM25SIndex, is_bm25s_available
    except ImportError:
        return {"error": "bm25s_search module not found"}

    if not is_bm25s_available():
        return {"error": "bm25s package not installed (pip install bm25s)"}

    import tempfile

    with tempfile.TemporaryDirectory(prefix="bm25s_bench_") as tmpdir:
        index = BM25SIndex(index_dir=tmpdir)
        ok = await index.initialize()
        if not ok:
            return {"error": "BM25SIndex.initialize() failed"}

        # ---------------------------------------------------------------
        # Phase 1: Bulk ingest
        # ---------------------------------------------------------------
        docs = [_make_doc(i) for i in range(n_files)]

        tracemalloc.start()
        t0 = time.perf_counter()

        count = await index.index_documents_bulk(docs)

        ingest_wall_s = time.perf_counter() - t0
        _, ingest_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # ---------------------------------------------------------------
        # Phase 2: Steady-state corpus RAM
        # ---------------------------------------------------------------
        tracemalloc.start()
        # Force a snapshot that includes the corpus list
        _ = len(index._corpus)
        corpus_current, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Approximate corpus RAM: sum of string sizes
        corpus_ram_bytes = sum(sys.getsizeof(s) for s in index._corpus)
        metadata_ram_bytes = (
            sum(sys.getsizeof(s) for s in index._path_ids)
            + sum(sys.getsizeof(s) for s in index._paths)
            + sys.getsizeof(index._path_to_idx)
        )

        # ---------------------------------------------------------------
        # Phase 3: 100 updates → triggers _merge_delta
        # ---------------------------------------------------------------
        # Pick 100 existing documents to update (simulates file modifications)
        update_indices = list(range(min(DELTA_THRESHOLD, n_files)))
        update_docs = [
            _make_update_doc(i, docs[i][0], docs[i][1])
            for i in update_indices
        ]

        tracemalloc.start()
        t0 = time.perf_counter()

        for path_id, path, content in update_docs:
            await index.index_document(path_id, path, content)

        merge_wall_s = time.perf_counter() - t0
        _, merge_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # ---------------------------------------------------------------
        # Phase 4: 1 delete → full rebuild
        # ---------------------------------------------------------------
        # Delete the first document
        delete_path_id = docs[0][0]

        tracemalloc.start()
        t0 = time.perf_counter()

        await index.delete_document(delete_path_id)

        delete_wall_s = time.perf_counter() - t0
        _, delete_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        return {
            "scale": round(n_files / HERB_FILES, 1),
            "n_files": n_files,
            "ingest_wall_s": round(ingest_wall_s, 3),
            "ingest_peak_mb": round(ingest_peak_bytes / 1_048_576, 2),
            "corpus_ram_mb": round(corpus_ram_bytes / 1_048_576, 2),
            "metadata_ram_mb": round(metadata_ram_bytes / 1_048_576, 2),
            "merge_100_wall_s": round(merge_wall_s, 3),
            "merge_100_peak_mb": round(merge_peak_bytes / 1_048_576, 2),
            "delete_1_wall_s": round(delete_wall_s, 3),
            "delete_1_peak_mb": round(delete_peak_bytes / 1_048_576, 2),
        }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


_HEADER = (
    f"{'Scale':>7}  {'Files':>7}  {'Ingest(s)':>10}  {'Corpus RAM':>11}"
    f"  {'Meta RAM':>9}  {'Merge 100(s)':>13}  {'Delete 1(s)':>12}"
)
_SEP = "-" * len(_HEADER)


def _print_row(r: dict[str, Any]) -> None:
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        return
    scale_label = f"{r['scale']}x"
    print(
        f"{scale_label:>7}  {r['n_files']:>7,}  {r['ingest_wall_s']:>10.3f}"
        f"  {r['corpus_ram_mb']:>9.1f} MB  {r['metadata_ram_mb']:>6.1f} MB"
        f"  {r['merge_100_wall_s']:>13.3f}  {r['delete_1_wall_s']:>12.3f}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run(scales: list[int], emit_json: bool) -> None:
    results: list[dict[str, Any]] = []

    if not emit_json:
        print(f"\nBM25S mutation benchmark  (HERB baseline = {HERB_FILES:,} files)")
        print(f"Delta threshold = {DELTA_THRESHOLD} (merge fires on 100th update)\n")
        print(_HEADER)
        print(_SEP)

    for scale in scales:
        n_files = HERB_FILES * scale
        r = await measure_one(n_files)
        results.append(r)
        if not emit_json:
            _print_row(r)

    if not emit_json and len(results) > 1:
        base = results[0]
        if "error" not in base:
            print()
            print("Scaling analysis (value / value@1x):")
            for r in results:
                if "error" in r:
                    continue
                s = r["scale"]
                merge_ratio = r["merge_100_wall_s"] / base["merge_100_wall_s"] if base["merge_100_wall_s"] > 0 else 0
                delete_ratio = r["delete_1_wall_s"] / base["delete_1_wall_s"] if base["delete_1_wall_s"] > 0 else 0
                ram_ratio = r["corpus_ram_mb"] / base["corpus_ram_mb"] if base["corpus_ram_mb"] > 0 else 0
                print(
                    f"  {s}x → merge: {merge_ratio:.2f}x  delete: {delete_ratio:.2f}x"
                    f"  RAM: {ram_ratio:.2f}x"
                    f"  (expected ≈ {s:.1f}x if proportional to corpus)"
                )

        print()
        print("Key findings:")
        print("  • _merge_delta retokenizes ALL documents, not just the 100 changed")
        print("  • delete_document rebuilds entire retriever for a single removal")
        print("  • self._corpus holds full document text in RAM alongside BM25 structures")

    if emit_json:
        print(json.dumps(results, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark BM25S rebuild cost and memory under mutation load (Issue #3707)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run 1x only (CI-safe)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit results as JSON (one array, stdout)",
    )
    parser.add_argument(
        "--scales",
        metavar="N,N,...",
        type=lambda s: [int(x) for x in s.split(",")],
        default=None,
        help="Comma-separated scale multipliers (default: 1,5)",
    )
    args = parser.parse_args()

    if args.quick:
        scales = [1]
    elif args.scales:
        scales = args.scales
    else:
        scales = DEFAULT_SCALES

    asyncio.run(_run(scales, args.emit_json))


if __name__ == "__main__":
    main()
