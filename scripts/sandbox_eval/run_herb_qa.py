"""HERB QA gate — directional check that SANDBOX hybrid clears
``scripts/test_build_perf_e2e.py`` section 6 (≥7/8 hits at top-5).

Reuses the demo HERB corpus + the published QA set from
``nexus.cli.commands.demo_data``, ingests them through the same
SqliteVecBackend (fastembed) + BM25SIndex stack the SANDBOX hybrid
production path uses, and runs the 8 demo questions through a
hand-stitched RRF that mirrors ``SearchService._hybrid_search_sandbox``.

This DOES NOT exercise the daemon / HTTP / gRPC layers the perf e2e
script needs end-to-end. It only validates the retrieval-quality gate
those sections measure, so we know SANDBOX would clear it once a
sandbox-mode HTTP entrypoint is added.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nexus.bricks.search.bm25s_search import BM25SIndex  # noqa: E402
from nexus.bricks.search.fusion import (  # noqa: E402
    FusionConfig,
    FusionMethod,
    fuse_results,
)
from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend  # noqa: E402
from nexus.cli.commands.demo_data import HERB_CORPUS, HERB_QA_SET  # noqa: E402

ZONE_ID = "herb"
TOP_K = 5
PER_LANE_FETCH = 50
PASS_THRESHOLD = 7  # mirrors `hits >= 7` in test_build_perf_e2e.py:407


async def _ingest(
    vec: SqliteVecBackend, bm25: BM25SIndex, corpus: list[tuple[str, str, str]]
) -> int:
    vec_docs = [{"path": path, "text": body, "chunk_index": 0} for (path, body, _summary) in corpus]
    bm25_docs = [(path, path, body) for (path, body, _summary) in corpus]
    n_vec = await vec.upsert(vec_docs, zone_id=ZONE_ID)
    n_bm25 = await bm25.index_documents_bulk(bm25_docs)
    return min(n_vec, n_bm25)


async def _hybrid(vec: SqliteVecBackend, bm25: BM25SIndex, query: str) -> list[dict]:
    vec_task = asyncio.create_task(vec.search(query=query, limit=PER_LANE_FETCH, zone_id=ZONE_ID))
    bm_task = asyncio.create_task(bm25.search(query=query, limit=PER_LANE_FETCH))
    vec_res, bm_res = await asyncio.gather(vec_task, bm_task)
    bm_dicts = [{"path": r.path, "score": r.score, "chunk_index": 0} for r in bm_res]
    vec_dicts = [
        {"path": r.path, "score": r.score, "chunk_index": getattr(r, "chunk_index", 0)}
        for r in vec_res
    ]
    return fuse_results(
        keyword_results=bm_dicts,
        vector_results=vec_dicts,
        config=FusionConfig(method=FusionMethod.RRF),
        limit=TOP_K,
        id_key=None,
    )


async def main() -> int:
    qa = list(HERB_QA_SET)
    print(f"HERB corpus: {len(HERB_CORPUS)} pages | QA set: {len(qa)} questions")
    print(f"Gate: hits >= {PASS_THRESHOLD} at top-{TOP_K} (mirrors test_build_perf_e2e.py)")

    with tempfile.TemporaryDirectory() as tmp:
        vec = SqliteVecBackend(db_path=str(Path(tmp) / "herb.sqlite"), embedder="fastembed")
        await vec.startup()
        bm25 = BM25SIndex(index_dir=str(Path(tmp) / "bm25s"))
        await bm25.initialize()

        t0 = time.monotonic()
        n = await _ingest(vec, bm25, HERB_CORPUS)
        ingest_ms = int((time.monotonic() - t0) * 1000)
        print(f"Ingested {n} pages in {ingest_ms} ms\n")

        rows: list[tuple[str, str, list[str], bool, int]] = []
        for entry in qa:
            q = entry["question"]
            expected = entry["expected_file"]
            t1 = time.monotonic()
            fused = await _hybrid(vec, bm25, q)
            dt_ms = int((time.monotonic() - t1) * 1000)
            paths_top = [r["path"] for r in fused]
            hit = expected in paths_top
            rows.append((q, expected, paths_top, hit, dt_ms))

        await vec.shutdown()

    print(f"{'#':>2} {'hit':>3} {'ms':>5}  question -> expected")
    print("-" * 100)
    for i, (q, expected, paths, hit, ms) in enumerate(rows, 1):
        marker = "✓" if hit else "✗"
        short_q = q if len(q) <= 60 else q[:57] + "..."
        short_e = expected.rsplit("/", 1)[-1]
        print(f"{i:>2} {marker:>3} {ms:>5}  {short_q} -> {short_e}")
        if not hit:
            print(f"        top{TOP_K}: {[p.rsplit('/', 1)[-1] for p in paths]}")

    hits = sum(1 for *_, hit, _ in rows if hit)
    latencies = sorted(ms for *_, ms in rows)
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[max(0, int(0.95 * len(latencies)) - 1)]

    print(f"\nResult: {hits}/{len(rows)} hits  | latency p50={p50}ms p95={p95}ms")
    if hits >= PASS_THRESHOLD:
        print(f"PASS — gate >= {PASS_THRESHOLD}/{len(rows)} cleared")
        return 0
    print(f"FAIL — needed >= {PASS_THRESHOLD}/{len(rows)}, got {hits}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
