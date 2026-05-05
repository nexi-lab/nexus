"""SANDBOX hybrid retrieval eval — tier5 fuzzy queries.

Loads a 240-page biographical/company prose corpus and 30 hand-authored
fuzzy queries (each tagged with a relevant slug set), indexes the pages
into a SANDBOX SqliteVecBackend (fastembed) + BM25SIndex, runs hybrid
RRF for each query, and reports P@5 / R@5 against the gold slugs.

Tunable via env vars:
  EVAL_CHUNK_TOKENS, EVAL_CHUNK_OVERLAP   chunking grain
  EVAL_EMBED_MODEL, EVAL_EMBED_DIM         fastembed model + dim
  EVAL_FUSION (rrf|rrf_weighted|weighted)  fusion algorithm
  EVAL_ALPHA                               vec weight in [0, 1]
  EVAL_TITLE_BOOST                         repeat title N times in body

Inputs (the corpus + queries live outside this repo — clone separately):
  * CORPUS_DIR = /tmp/gbrain-evals-clone/eval/data/world-v1/*.json
  * QUERIES_PATH = /tmp/tier5_queries.json
        (bun-dump from eval/runner/queries/tier5-fuzzy.ts)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

# Plug into the live source tree
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nexus.bricks.search.bm25s_search import BM25SIndex  # noqa: E402
from nexus.bricks.search.fusion import (  # noqa: E402
    FusionConfig,
    FusionMethod,
    fuse_results,
)
from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend  # noqa: E402

CORPUS_DIR = Path("/tmp/gbrain-evals-clone/eval/data/world-v1")
QUERIES_PATH = Path("/tmp/tier5_queries.json")
ZONE_ID = "world-v1"
TOP_K = 5  # gbrain's headline metric is at K=5
PER_LANE_FETCH = 100  # over-fetch per lane so chunk-level RRF has material
CHUNK_TOKENS = int(os.environ.get("EVAL_CHUNK_TOKENS", "200"))
CHUNK_OVERLAP = int(os.environ.get("EVAL_CHUNK_OVERLAP", "30"))
EMBED_MODEL = os.environ.get("EVAL_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM = int(os.environ.get("EVAL_EMBED_DIM", "384"))
FUSION_METHOD = os.environ.get("EVAL_FUSION", "rrf")  # rrf | rrf_weighted | weighted
FUSION_ALPHA = float(os.environ.get("EVAL_ALPHA", "0.5"))  # vec weight in [0,1]
TITLE_BOOST = int(os.environ.get("EVAL_TITLE_BOOST", "1"))  # repeat title N times


def _load_pages() -> list[dict]:
    pages = []
    for f in sorted(CORPUS_DIR.iterdir()):
        if f.suffix != ".json" or f.name.startswith("_"):
            continue
        d = json.loads(f.read_text())
        # gbrain's HybridNoGraphAdapter (the closest external comparator)
        # builds its body the same way: title + compiled_truth + timeline.
        body_parts = [d.get("title", ""), d.get("compiled_truth", "")]
        timeline = d.get("timeline")
        if isinstance(timeline, list):
            timeline = "\n".join(str(x) for x in timeline)
        if timeline:
            body_parts.append("Timeline:\n" + timeline)
        pages.append(
            {
                "slug": d["slug"],
                "title": d.get("title", ""),
                "body": "\n\n".join(p for p in body_parts if p),
            }
        )
    return pages


def _load_queries() -> list[dict]:
    return json.loads(QUERIES_PATH.read_text())


def _slug_to_path(slug: str, chunk: int = 0) -> str:
    """Stable mapping slug+chunk → backend path id."""
    return f"/world/{slug}#c{chunk}"


def _path_to_slug(path: str) -> str:
    # accepts /world/<slug>, /world/<slug>#c<n>, or /world/<slug>.md
    inner = path[len("/world/") :]
    inner = inner.split("#", 1)[0]
    if inner.endswith(".md"):
        inner = inner[: -len(".md")]
    return inner


def _chunk_text(text: str, max_tokens: int, overlap: int) -> list[str]:
    """Naive whitespace-token chunker. Good enough for prose corpus.

    Splits on whitespace, packs ``max_tokens`` per chunk with ``overlap``
    tokens of carry-over so phrases at chunk boundaries still appear in
    one of the chunks. Empty input returns an empty list.
    """
    toks = text.split()
    if not toks:
        return []
    chunks: list[str] = []
    step = max(1, max_tokens - overlap)
    for start in range(0, len(toks), step):
        piece = toks[start : start + max_tokens]
        if not piece:
            break
        chunks.append(" ".join(piece))
        if start + max_tokens >= len(toks):
            break
    return chunks


async def _ingest(
    pages: list[dict],
    vec: SqliteVecBackend,
    bm25: BM25SIndex,
) -> None:
    print(f"Ingesting {len(pages)} pages into sqlite-vec + BM25S ...", flush=True)
    t0 = time.monotonic()

    vec_docs: list[dict] = []
    bm25_docs: list[tuple[str, str, str]] = []
    for p in pages:
        # Optional title boost: prepend the title N times so both lanes
        # see it at increased weight (matches the trick gbrain's adapter
        # uses implicitly via YAML frontmatter parsed twice).
        body = p["body"]
        if TITLE_BOOST > 1 and p.get("title"):
            body = ((p["title"] + "\n") * (TITLE_BOOST - 1)) + body
        chunks = _chunk_text(body, CHUNK_TOKENS, CHUNK_OVERLAP) or [body]
        for i, chunk in enumerate(chunks):
            path_id = _slug_to_path(p["slug"], i)
            vec_docs.append({"path": path_id, "text": chunk, "chunk_index": i})
            bm25_docs.append((path_id, path_id, chunk))

    n_vec = await vec.upsert(vec_docs, zone_id=ZONE_ID)
    n_bm25 = await bm25.index_documents_bulk(bm25_docs)

    dt = time.monotonic() - t0
    print(
        f"  pages={len(pages)} chunks={len(vec_docs)}  vec={n_vec} bm25={n_bm25}  took {dt:.1f}s",
        flush=True,
    )


async def _hybrid_search(
    query: str,
    vec: SqliteVecBackend,
    bm25: BM25SIndex,
    k: int,
) -> list[dict]:
    """Run both lanes in parallel, fuse via RRF (matches _hybrid_search_sandbox)."""
    # Over-fetch each lane so RRF has material to merge — matches the
    # production hybrid path (SearchService._hybrid_search_sandbox uses
    # per_lane_limit = limit*3, bumped to limit*5 with permission filter).
    vec_task = asyncio.create_task(vec.search(query=query, limit=PER_LANE_FETCH, zone_id=ZONE_ID))
    bm_task = asyncio.create_task(bm25.search(query=query, limit=PER_LANE_FETCH))
    vec_results, bm_results = await asyncio.gather(vec_task, bm_task)

    # Convert BM25SSearchResult → dict shape that matches the fusion id_key.
    bm_dicts = [{"path": r.path, "score": r.score, "chunk_index": 0} for r in bm_results]
    vec_dicts = [
        {"path": r.path, "score": r.score, "chunk_index": getattr(r, "chunk_index", 0)}
        for r in vec_results
    ]
    method = {
        "rrf": FusionMethod.RRF,
        "rrf_weighted": FusionMethod.RRF_WEIGHTED,
        "weighted": FusionMethod.WEIGHTED,
    }[FUSION_METHOD]
    fused_chunks = fuse_results(
        keyword_results=bm_dicts,
        vector_results=vec_dicts,
        config=FusionConfig(method=method, alpha=FUSION_ALPHA),
        limit=PER_LANE_FETCH,  # keep wide enough for slug-level aggregation
        id_key=None,  # path:chunk_index dedup, mirrors production
    )
    # Aggregate to page-level: keep each slug's best chunk score.
    by_slug: dict[str, dict] = {}
    for r in fused_chunks:
        slug = _path_to_slug(r["path"])
        if slug not in by_slug or r["score"] > by_slug[slug]["score"]:
            r2 = dict(r)
            r2["path"] = f"/world/{slug}"  # canonicalise to page-level path
            by_slug[slug] = r2
    return sorted(by_slug.values(), key=lambda x: x["score"], reverse=True)[:k]


def _precision_at_k(predicted: Iterable[str], relevant: set[str], k: int) -> float:
    pred_k = list(predicted)[:k]
    if not pred_k:
        return 0.0
    return sum(1 for p in pred_k if p in relevant) / k


def _recall_at_k(predicted: Iterable[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    pred_k = set(list(predicted)[:k])
    return len(pred_k & relevant) / len(relevant)


async def main() -> int:
    if not CORPUS_DIR.exists():
        print(f"ERROR: corpus missing at {CORPUS_DIR}", file=sys.stderr)
        return 2
    if not QUERIES_PATH.exists():
        print(f"ERROR: queries missing at {QUERIES_PATH}", file=sys.stderr)
        return 2

    pages = _load_pages()
    queries = _load_queries()
    print(f"Loaded {len(pages)} pages, {len(queries)} queries.")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "eval.sqlite")
        vec = SqliteVecBackend(
            db_path=db_path,
            embedder="fastembed",
            embedding_model=EMBED_MODEL,
            embedding_dim=EMBED_DIM,
        )
        await vec.startup()

        bm25 = BM25SIndex(index_dir=str(Path(tmp) / "bm25s"))
        await bm25.initialize()

        await _ingest(pages, vec, bm25)

        rows = []
        t_query_start = time.monotonic()
        for q in queries:
            relevant = set(q["relevant"])
            t0 = time.monotonic()
            fused = await _hybrid_search(q["text"], vec, bm25, k=TOP_K)
            dt_ms = int((time.monotonic() - t0) * 1000)
            pred_slugs = [_path_to_slug(r["path"]) for r in fused]
            p5 = _precision_at_k(pred_slugs, relevant, TOP_K)
            r5 = _recall_at_k(pred_slugs, relevant, TOP_K)
            rows.append(
                {
                    "id": q["id"],
                    "text": q["text"],
                    "relevant": sorted(relevant),
                    "predicted": pred_slugs,
                    "p@5": p5,
                    "r@5": r5,
                    "latency_ms": dt_ms,
                }
            )
        total_q_ms = int((time.monotonic() - t_query_start) * 1000)

        await vec.shutdown()

    # Per-query table
    print("\n=== Per-query results ===")
    print(f"{'id':<10} {'P@5':>5} {'R@5':>5} {'ms':>5}  query")
    for r in rows:
        print(
            f"{r['id']:<10} {r['p@5']:>5.2f} {r['r@5']:>5.2f} "
            f"{r['latency_ms']:>5d}  {r['text'][:80]}"
        )
        if r["r@5"] == 0.0:
            print(f"           gold: {r['relevant']}")
            print(f"           top5: {r['predicted']}")

    # Aggregates
    n = len(rows)
    avg_p5 = sum(r["p@5"] for r in rows) / n
    avg_r5 = sum(r["r@5"] for r in rows) / n
    p95_ms = sorted(r["latency_ms"] for r in rows)[max(0, int(0.95 * n) - 1)]
    # R@5 over only queries with non-empty gold — fairer for tier5 fuzzy
    # where 6 prompts have gold.relevant=[] (open-ended summarisation).
    scoreable = [r for r in rows if r["relevant"]]
    avg_r5_score = sum(r["r@5"] for r in scoreable) / len(scoreable) if scoreable else 0.0

    print("\n=== Aggregate ===")
    print(
        f"config: chunk={CHUNK_TOKENS}/{CHUNK_OVERLAP} title_boost={TITLE_BOOST} "
        f"embed={EMBED_MODEL} dim={EMBED_DIM} fusion={FUSION_METHOD} alpha={FUSION_ALPHA}"
    )
    print(f"N queries:          {n}  (scoreable={len(scoreable)})")
    print(f"Mean P@5:           {avg_p5:.3f}")
    print(f"Mean R@5:           {avg_r5:.3f}   (scoreable-only: {avg_r5_score:.3f})")
    print(f"Latency p95 (ms):   {p95_ms}")
    print(f"Wall total query (ms): {total_q_ms}")
    print(
        "\ngbrain headline (per their README): "
        "P@5 0.491, R@5 0.979 (graph-on); HybridNoGraph baseline lower."
    )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
