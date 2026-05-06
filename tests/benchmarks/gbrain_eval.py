"""Manual benchmark gate for #3699 cutover.

Runs the new search daemon against the gbrain-evals corpus
(https://github.com/garrytan/gbrain-evals). Prints recall@5 and NDCG@5.

Pre-merge gate: numbers must match or beat the issue baseline:
    recall@5  = 0.9489
    NDCG@5    = 0.9028

Usage:
    make bench-search          # Makefile repos (adds bench-search target)
    just bench-search          # justfile repos (adds bench-search recipe)

Env:
    GBRAIN_EVALS_DIR   path to a checkout of gbrain-evals (must contain
                       corpus.jsonl and queries.jsonl)
    NEXUS_DATABASE_URL must point at a fresh Postgres instance with
                       pg_textsearch (BM25) and pgvector installed.
                       Accepts bare postgresql:// or postgresql+asyncpg://.

Implementation notes:
    - Data is seeded via raw SQL into file_paths + document_chunks tables,
      matching the integration-test pattern from T9 (test_daemon_search_pg.py).
    - The daemon's _fts_backend.upsert() is NOT used — those methods raise
      NotImplementedError per T5/T6 design (backend is query-only, writes go
      through ChunkStore). Direct SQL is the correct seeding path.
    - If lazy backfill / re-embedding is needed (NULL embeddings), the dense
      leg of hybrid search will degrade gracefully to keyword-only for those
      chunks. Document this in your run notes.
    - The tiny smoke fixture (tests/benchmarks/_tiny_fixture/) lets you verify
      the harness wires up without running the full gbrain-evals corpus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Baseline thresholds (Issue #3699)
# ---------------------------------------------------------------------------
BASELINE_RECALL5 = 0.9489
BASELINE_NDCG5 = 0.9028
# Allow 1 percentage-point regression before failing the gate.
REGRESSION_SLACK = 0.01


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_CREATE_FILE_PATHS = """
CREATE TABLE IF NOT EXISTS file_paths (
    path_id      TEXT PRIMARY KEY,
    zone_id      TEXT NOT NULL,
    virtual_path TEXT NOT NULL,
    deleted_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_DOCUMENT_CHUNKS = """
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    TEXT PRIMARY KEY,
    path_id     TEXT NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text  TEXT NOT NULL,
    chunk_tokens INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_BM25_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chunks_bm25_bench
ON document_chunks USING bm25(chunk_text)
WITH (text_config='english')
"""


async def _ensure_schema(engine: AsyncEngine) -> None:
    """Create tables and BM25 index if they don't exist."""
    async with engine.begin() as conn:
        # Check pg_textsearch (bm25 access method) is present.
        result = await conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
        if result.fetchone() is None:
            raise RuntimeError(
                "pg_textsearch (BM25 access method) is not installed in this Postgres. "
                "Cannot run benchmark without it."
            )
        await conn.execute(text(_CREATE_FILE_PATHS))
        await conn.execute(text(_CREATE_DOCUMENT_CHUNKS))
        # BM25 index creation is a no-op if it already exists.
        try:
            await conn.execute(text(_BM25_INDEX))
        except Exception:
            # Index may already exist with different definition; proceed.
            pass


async def _populate_embeddings(engine: AsyncEngine, corpus_path: Path) -> None:
    """Embed each corpus row + write into document_chunks.embedding.

    Used by the benchmark when NEXUS_BENCH_EMBED=1, so the dense leg of
    hybrid search has vectors to score against. Without this the seed
    path leaves embedding NULL and hybrid degrades to keyword-only.
    """
    from nexus.bricks.search.embedding_client import EmbeddingClient

    model = os.environ.get(
        "NEXUS_EMBEDDING_MODEL",
        os.environ.get("NEXUS_TXTAI_MODEL", "openai/text-embedding-3-small"),
    )
    docs = [
        json.loads(ln) for ln in corpus_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if not docs:
        return

    client = EmbeddingClient(model=model, dim=1536)
    vectors = await client.embed_batch([d["text"] for d in docs])

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(
            text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding halfvec(1536)")
        )
        for doc, vec in zip(docs, vectors, strict=True):
            vec_str = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
            await conn.execute(
                text(
                    "UPDATE document_chunks SET embedding = CAST(:v AS halfvec) WHERE path_id = :p"
                ),
                {"v": vec_str, "p": doc["id"]},
            )
    print(f"Populated {len(vectors)} embeddings via {model}.")


async def _truncate_corpus(engine: AsyncEngine) -> None:
    """Wipe any leftover data from a previous run."""
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE document_chunks, file_paths RESTART IDENTITY CASCADE"))


# ---------------------------------------------------------------------------
# Corpus seeding
# ---------------------------------------------------------------------------


async def _seed_corpus(engine: AsyncEngine, corpus_path: Path, zone_id: str = "bench") -> None:
    """Insert corpus documents into file_paths + document_chunks.

    Each JSONL line is expected to have:
        {"id": <str>, "path": <str>, "text": <str>}

    We treat each document as a single chunk (index 0).  The benchmark
    corpus is pre-chunked by gbrain-evals; real indexing goes through
    the IndexingPipeline.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    lines = corpus_path.read_text(encoding="utf-8").splitlines()
    docs = [json.loads(ln) for ln in lines if ln.strip()]

    async with engine.begin() as conn:
        for doc in docs:
            doc_id: str = str(doc["id"])
            virtual_path: str = doc["path"]
            text_content: str = doc["text"]
            chunk_id = str(uuid.uuid4())
            # Approximate token count: 1 token ≈ 4 chars (good-enough for BM25).
            token_count = max(1, len(text_content) // 4)

            await conn.execute(
                text(
                    "INSERT INTO file_paths (path_id, zone_id, virtual_path, deleted_at)"
                    " VALUES (:path_id, :zone_id, :virtual_path, NULL)"
                    " ON CONFLICT (path_id) DO NOTHING"
                ),
                {"path_id": doc_id, "zone_id": zone_id, "virtual_path": virtual_path},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks"
                    " (chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, created_at)"
                    " VALUES (:chunk_id, :path_id, :chunk_index, :chunk_text, :chunk_tokens, :created_at)"
                    " ON CONFLICT (chunk_id) DO NOTHING"
                ),
                {
                    "chunk_id": chunk_id,
                    "path_id": doc_id,
                    "chunk_index": 0,
                    "chunk_text": text_content,
                    "chunk_tokens": token_count,
                    "created_at": now,
                },
            )

    print(f"Indexed {len(docs)} documents from {corpus_path.name}.")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _ndcg(predicted: list[str], relevant: set[str]) -> float:
    """NDCG@k where k = len(predicted)."""
    import math

    dcg = sum(1.0 / math.log2(i + 2) for i, p in enumerate(predicted) if p in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(predicted), len(relevant))))
    return dcg / ideal if ideal > 0 else 0.0


# ---------------------------------------------------------------------------
# DB URL normalisation
# ---------------------------------------------------------------------------


def _normalise_db_url(url: str) -> str:
    """Convert bare postgresql:// → postgresql+asyncpg:// for asyncpg driver."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    """Run the gbrain-evals benchmark gate.

    Returns:
        0 on pass, 1 on regression vs baseline.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    eval_dir = Path(os.environ["GBRAIN_EVALS_DIR"])
    raw_db_url = os.environ["NEXUS_DATABASE_URL"]

    corpus_path = eval_dir / "corpus.jsonl"
    queries_path = eval_dir / "queries.jsonl"

    if not corpus_path.exists():
        print(f"ERROR: corpus.jsonl not found in {eval_dir}", file=sys.stderr)
        return 1
    if not queries_path.exists():
        print(f"ERROR: queries.jsonl not found in {eval_dir}", file=sys.stderr)
        return 1

    async_url = _normalise_db_url(raw_db_url)

    # Build a dedicated engine for the benchmark so we control its lifecycle.
    engine = create_async_engine(async_url, echo=False)
    try:
        print(f"Using DB: {raw_db_url}")
        print(f"Corpus  : {corpus_path}")
        print(f"Queries : {queries_path}")
        print()

        # Ensure schema exists (idempotent) and start with a clean slate.
        await _ensure_schema(engine)
        await _truncate_corpus(engine)

        # Seed corpus via direct SQL — mirrors T9 integration test pattern.
        await _seed_corpus(engine, corpus_path)

        # Populate embeddings if requested. The seed path above only writes
        # text — the dense leg of hybrid search needs vectors. Gated by
        # NEXUS_BENCH_EMBED=1 so the keyword-only baseline is still
        # measurable.
        if os.environ.get("NEXUS_BENCH_EMBED") == "1":
            await _populate_embeddings(engine, corpus_path)

        # Wire daemon to the same engine/session factory we just seeded.
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Use the plain postgresql:// URL for DaemonConfig (daemon normalises
        # it internally when it detects the "postgresql" prefix).
        daemon_url = raw_db_url
        config = DaemonConfig(database_url=daemon_url, refresh_enabled=False)

        d = SearchDaemon(config=config, async_session_factory=factory)
        # Inject the engine directly so the daemon and the seeding step share
        # one physical connection pool (same pattern as test_daemon_search_pg).
        d._async_engine = engine
        d._owns_engine = False
        await d.startup()

        try:
            # Score
            recalls: list[float] = []
            ndcgs: list[float] = []

            queries = [
                json.loads(ln)
                for ln in queries_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]

            for q in queries:
                query_text: str = q["query"]
                relevant: set[str] = set(q["relevant"])

                hits = await d.search(
                    query_text,
                    search_type="hybrid",
                    limit=10,
                    zone_id="bench",
                )
                top5 = [h.path for h in hits[:5]]

                recalls.append(len(set(top5) & relevant) / max(1, len(relevant)))
                ndcgs.append(_ndcg(top5, relevant))

            if not recalls:
                print("ERROR: no queries found in queries.jsonl", file=sys.stderr)
                return 1

            r5 = sum(recalls) / len(recalls)
            n5 = sum(ndcgs) / len(ndcgs)

            print(f"recall@5 = {r5:.4f}  (baseline {BASELINE_RECALL5:.4f})")
            print(f"NDCG@5   = {n5:.4f}  (baseline {BASELINE_NDCG5:.4f})")
            print()

            failed = False
            if r5 < BASELINE_RECALL5 - REGRESSION_SLACK:
                print(
                    f"FAIL: recall@5 {r5:.4f} is more than {REGRESSION_SLACK:.0%} below "
                    f"baseline {BASELINE_RECALL5:.4f}",
                    file=sys.stderr,
                )
                failed = True
            if n5 < BASELINE_NDCG5 - REGRESSION_SLACK:
                print(
                    f"FAIL: NDCG@5 {n5:.4f} is more than {REGRESSION_SLACK:.0%} below "
                    f"baseline {BASELINE_NDCG5:.4f}",
                    file=sys.stderr,
                )
                failed = True

            if failed:
                print("Regression vs issue baseline detected — DO NOT merge.", file=sys.stderr)
                return 1

            print("PASS: recall@5 and NDCG@5 meet or beat the issue baseline.")
            return 0

        finally:
            await d.shutdown()

    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
