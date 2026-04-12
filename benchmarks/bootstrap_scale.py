#!/usr/bin/env python3
"""Benchmark: daemon bootstrap time vs file count (Issue #3704).

Measures how ``_bootstrap_txtai_backend`` scales as ``document_chunks``
grows.  The bottleneck under investigation is the unbounded ``fetchall()``
that materialises ALL rows into Python memory before pushing to txtai.

Usage::

    python benchmarks/bootstrap_scale.py            # default: 1x 5x 10x 25x
    python benchmarks/bootstrap_scale.py --quick    # 1x only (CI-safe, <5s)
    python benchmarks/bootstrap_scale.py --json     # emit JSON for further analysis
    python benchmarks/bootstrap_scale.py --scales 1,5,10

HERB corpus baseline: 2,113 files, realistic enterprise content.
Scale factors multiply that file count; chunk rows scale proportionally.

Expected output (example, numbers vary)::

     Scale     Files        Rows    Wall (s)  Peak mem (MB)
    -------------------------------------------------------
        1x     2,113       6,339       0.142            5.1
        5x    10,565      31,695       0.691           25.2
       10x    21,130      63,390       1.381           50.4
       25x    52,825     158,475       3.451          126.1
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
# Schema (minimal subset needed by _bootstrap_txtai_backend)
# ---------------------------------------------------------------------------

_DDL_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS file_paths (
        path_id       TEXT    PRIMARY KEY,
        zone_id       TEXT    NOT NULL DEFAULT 'root',
        virtual_path  TEXT    NOT NULL,
        backend_id    TEXT    NOT NULL DEFAULT '',
        physical_path TEXT    NOT NULL DEFAULT '',
        size_bytes    INTEGER NOT NULL DEFAULT 0,
        deleted_at    DATETIME         NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id     TEXT    PRIMARY KEY,
        path_id      TEXT    NOT NULL,
        chunk_index  INTEGER NOT NULL,
        chunk_text   TEXT    NOT NULL,
        chunk_tokens INTEGER NOT NULL DEFAULT 0,
        created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (path_id) REFERENCES file_paths(path_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dc_path_id ON document_chunks(path_id)",
]

# ---------------------------------------------------------------------------
# Corpus constants
# ---------------------------------------------------------------------------

# HERB enterprise-context corpus = 2,113 files (see benchmarks/herb/README.md)
HERB_FILES: int = 2_113

# Conservative average: ~3 chunks per file across mixed JSONL / Markdown / JSON.
# Real production deployments often have 5-10+ chunks per file.
CHUNKS_PER_FILE: int = 3

# Synthetic chunk body (~80 words ≈ typical paragraph).
_CHUNK_BODY: str = (
    "enterprise context chunk text simulating realistic document content "
    "from the herb corpus including product specifications meeting notes "
    "slack messages and technical documentation "
) * 2  # ~160 chars

DEFAULT_SCALES: list[int] = [1, 5, 10, 25]

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def _create_schema(engine: Any) -> None:
    from sqlalchemy import text as sa_text

    async with engine.begin() as conn:
        await conn.execute(sa_text("PRAGMA journal_mode=WAL"))
        await conn.execute(sa_text("PRAGMA synchronous=OFF"))
        for stmt in _DDL_STMTS:
            await conn.execute(sa_text(stmt))


async def _populate(engine: Any, n_files: int, chunks_per_file: int, n_zones: int = 1) -> int:
    """Insert ``n_files`` file rows + ``n_files * chunks_per_file`` chunk rows.

    When ``n_zones > 1``, files are distributed round-robin across zones
    named ``zone_0000`` … ``zone_NNNN``.  This exercises the multi-zone
    bootstrap path where every zone has far fewer than ``_UPSERT_BATCH``
    documents, which was the failure mode the per-zone flush fixes.
    """
    from sqlalchemy import text as sa_text

    file_rows = [
        {
            "path_id": str(uuid.uuid4()),
            "zone_id": f"zone_{i % n_zones:04d}" if n_zones > 1 else "root",
            "virtual_path": f"/herb/run_{n_files:07d}/file_{i:06d}.md",
        }
        for i in range(n_files)
    ]

    chunk_rows = [
        {
            "chunk_id": str(uuid.uuid4()),
            "path_id": fp["path_id"],
            "chunk_index": ci,
            "chunk_text": f"[chunk {ci}] {_CHUNK_BODY}",
            "chunk_tokens": 40,
        }
        for fp in file_rows
        for ci in range(chunks_per_file)
    ]

    async with engine.begin() as conn:
        await conn.execute(
            sa_text(
                "INSERT INTO file_paths"
                " (path_id, zone_id, virtual_path, backend_id, physical_path, size_bytes)"
                " VALUES (:path_id, :zone_id, :virtual_path, '', '', 0)"
            ),
            file_rows,
        )
        # Batch in chunks of 500 to stay well under SQLite's per-statement
        # variable limit (999 by default; each row uses 5 variables here).
        batch_size = 500
        for start in range(0, len(chunk_rows), batch_size):
            await conn.execute(
                sa_text(
                    "INSERT INTO document_chunks"
                    " (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens)"
                    " VALUES (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens)"
                ),
                chunk_rows[start : start + batch_size],
            )

    return len(chunk_rows)


# ---------------------------------------------------------------------------
# Null txtai backend — absorbs upsert calls so we measure SQL+Python only
# ---------------------------------------------------------------------------


class _NullBackend:
    async def upsert(self, docs: list, *, zone_id: str) -> int:
        return len(docs)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


async def measure_one(
    n_files: int,
    chunks_per_file: int = CHUNKS_PER_FILE,
    n_zones: int = 1,
) -> dict[str, Any]:
    """Return a timing/memory dict for a bootstrap run at *n_files* scale.

    ``n_zones`` distributes files round-robin across that many zone IDs.
    When ``n_zones`` is large and every zone stays well under
    ``_UPSERT_BATCH``, the only flush that fires is the per-zone-boundary
    flush added by Issue #3704.  This exercises the adversarial path.

    Memory is reported as the **true peak** heap bytes allocated during
    bootstrap, using ``tracemalloc.get_traced_memory()[1]`` (the high-water
    mark since ``tracemalloc.start()``).  This captures transient spikes
    that are freed before the final snapshot, unlike a before/after diff.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nexus.bricks.search.daemon import SearchDaemon

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    await _create_schema(engine)
    total_rows = await _populate(engine, n_files, chunks_per_file, n_zones=n_zones)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Build a minimally-wired daemon — same technique as test_bootstrap_filter_shape.py
    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon._async_session = session_factory
    daemon._backend = _NullBackend()
    daemon._zone_indexing_modes = {}  # no scoped zones → fast path
    daemon._indexed_directories = {}
    daemon._txtai_bootstrapped = False

    class _Stats:
        last_index_refresh: float | None = None

    daemon.stats = _Stats()

    # Warm the SQLite page cache so we measure CPU + Python cost, not cold I/O.
    from sqlalchemy import text as sa_text

    async with session_factory() as sess:
        await sess.execute(sa_text("SELECT COUNT(*) FROM document_chunks"))

    # ---- timed section ----
    # Use get_traced_memory()[1] for the true high-water mark (captures
    # transient spikes freed before the run ends, unlike a before/after diff).
    tracemalloc.start()
    t0 = time.perf_counter()

    await daemon._bootstrap_txtai_backend()

    wall_s = time.perf_counter() - t0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    await engine.dispose()

    return {
        "scale": round(n_files / HERB_FILES, 1),
        "n_files": n_files,
        "n_zones": n_zones,
        "total_rows": total_rows,
        "wall_s": round(wall_s, 3),
        "peak_mb": round(peak_bytes / 1_048_576, 2),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_header(label: str = "") -> None:
    if label:
        print(f"\n{label}")
    print(
        f"{'Scale':>7}  {'Files':>8}  {'Zones':>6}  {'Chunk rows':>11}"
        f"  {'Wall (s)':>9}  {'Peak heap (MB)':>15}"
    )
    print("-" * 71)


def _print_row(r: dict[str, Any]) -> None:
    scale_label = f"{r['scale']}x"
    print(
        f"{scale_label:>7}  {r['n_files']:>8,}  {r['n_zones']:>6,}  {r['total_rows']:>11,}"
        f"  {r['wall_s']:>9.3f}  {r['peak_mb']:>14.1f}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run(scales: list[int], emit_json: bool, chunks_per_file: int) -> None:
    results = []
    if not emit_json:
        print(f"\nBootstrap scale benchmark  (HERB baseline = {HERB_FILES:,} files)\n")
        _print_header("Single-zone (all files in 'root')")

    for scale in scales:
        n_files = HERB_FILES * scale
        r = await measure_one(n_files, chunks_per_file=chunks_per_file, n_zones=1)
        results.append(r)
        if not emit_json:
            _print_row(r)

    # Multi-zone scenario: many small zones, each well below _UPSERT_BATCH.
    # This exercises the zone-boundary flush path.  Use 500 zones so each
    # zone has ~4 files × 3 chunks = ~4 docs at 1x — always sub-threshold.
    multi_results = []
    multi_zones = 500
    if not emit_json:
        _print_header(
            f"Multi-zone ({multi_zones} zones — adversarial: each zone << _UPSERT_BATCH)"
        )
    for scale in scales:
        n_files = HERB_FILES * scale
        r = await measure_one(n_files, chunks_per_file=chunks_per_file, n_zones=multi_zones)
        multi_results.append(r)
        if not emit_json:
            _print_row(r)

    all_results = results + multi_results

    if not emit_json and len(results) > 1:
        base = results[0]["wall_s"]
        print()
        print("Single-zone linearity check (wall_s / wall_s@1x):")
        for r in results:
            ratio = r["wall_s"] / base if base > 0 else float("nan")
            print(f"  {r['scale']}x → {ratio:.2f}x  (expected ≈ {r['scale']:.1f}x if linear)")

    if emit_json:
        print(json.dumps(all_results, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark _bootstrap_txtai_backend scale (Issue #3704)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run 1x only (<5 s, safe for CI)",
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
        help="Comma-separated scale multipliers (default: 1,5,10,25)",
    )
    parser.add_argument(
        "--chunks-per-file",
        metavar="N",
        type=int,
        default=CHUNKS_PER_FILE,
        help=f"Average chunks per file (default: {CHUNKS_PER_FILE})",
    )
    args = parser.parse_args()

    if args.quick:
        scales = [1]
    elif args.scales:
        scales = args.scales
    else:
        scales = DEFAULT_SCALES

    asyncio.run(_run(scales, args.emit_json, args.chunks_per_file))


if __name__ == "__main__":
    main()
