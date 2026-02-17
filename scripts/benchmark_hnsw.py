#!/usr/bin/env python3
"""Benchmark script for pgvector HNSW parameter tuning.

This script helps find optimal HNSW parameters for your dataset by testing
different configurations and measuring:
- Index build time
- Query latency (P50, P95, P99)
- Recall@K against exact search
- Queries per second (QPS)

Usage:
    python scripts/benchmark_hnsw.py --database-url postgresql://user:pass@localhost/nexus
    python scripts/benchmark_hnsw.py --database-url postgresql://... --test-queries 100

References:
    - https://github.com/nexi-lab/nexus/issues/1004
    - https://github.com/pgvector/pgvector
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class BenchmarkConfig:
    """Configuration for HNSW benchmark."""

    m: int
    ef_construction: int
    ef_search: int
    maintenance_work_mem: str = "2GB"
    max_parallel_workers: int = 7


@dataclass
class BenchmarkResult:
    """Results from HNSW benchmark."""

    config: BenchmarkConfig
    vector_count: int
    index_build_time_seconds: float
    index_size_mb: float
    query_latency_p50_ms: float
    query_latency_p95_ms: float
    query_latency_p99_ms: float
    qps: float
    recall_at_10: float


# Configurations to test
BENCHMARK_CONFIGS = [
    # Small scale
    BenchmarkConfig(m=16, ef_construction=64, ef_search=40),
    # Medium scale (current default)
    BenchmarkConfig(m=24, ef_construction=128, ef_search=100),
    # Large scale
    BenchmarkConfig(m=32, ef_construction=200, ef_search=200),
    # High recall variant
    BenchmarkConfig(m=24, ef_construction=200, ef_search=150),
]


def get_vector_count(session: Any) -> int:
    """Get count of vectors in document_chunks."""
    result = session.execute(
        text("SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL")
    )
    return result.scalar() or 0


def get_sample_embeddings(session: Any, n: int = 100) -> list[list[float]]:
    """Get sample embeddings for test queries."""
    result = session.execute(
        text(
            """
            SELECT embedding::text
            FROM document_chunks
            WHERE embedding IS NOT NULL
            ORDER BY RANDOM()
            LIMIT :n
        """
        ),
        {"n": n},
    )
    embeddings = []
    for row in result:
        # Parse the vector string format [x,y,z,...]
        vec_str = row[0].strip("[]")
        if vec_str:
            embeddings.append([float(x) for x in vec_str.split(",")])
    return embeddings


def exact_search(session: Any, embedding: list[float], limit: int = 10) -> list[str]:
    """Perform exact (sequential) search for ground truth."""
    result = session.execute(
        text(
            """
            SELECT chunk_id
            FROM document_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT :limit
        """
        ),
        {"embedding": embedding, "limit": limit},
    )
    return [row[0] for row in result]


def hnsw_search(session: Any, embedding: list[float], ef_search: int, limit: int = 10) -> list[str]:
    """Perform HNSW search with given ef_search."""
    session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
    result = session.execute(
        text(
            """
            SELECT chunk_id
            FROM document_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT :limit
        """
        ),
        {"embedding": embedding, "limit": limit},
    )
    return [row[0] for row in result]


def calculate_recall(ground_truth: list[str], results: list[str]) -> float:
    """Calculate recall@K."""
    if not ground_truth:
        return 0.0
    return len(set(ground_truth) & set(results)) / len(ground_truth)


def drop_hnsw_index(session: Any) -> None:
    """Drop existing HNSW index."""
    session.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))
    session.commit()


def create_hnsw_index(session: Any, config: BenchmarkConfig) -> float:
    """Create HNSW index and return build time in seconds."""
    # Set build parameters
    session.execute(text(f"SET maintenance_work_mem = '{config.maintenance_work_mem}'"))
    session.execute(text(f"SET max_parallel_maintenance_workers = {config.max_parallel_workers}"))

    # Build index
    start = time.perf_counter()
    session.execute(
        text(
            f"""
            CREATE INDEX idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding halfvec_cosine_ops)
            WITH (m = {config.m}, ef_construction = {config.ef_construction})
        """
        )
    )
    session.commit()
    return time.perf_counter() - start


def get_index_size(session: Any) -> float:
    """Get HNSW index size in MB."""
    result = session.execute(
        text(
            """
            SELECT pg_size_pretty(pg_relation_size('idx_chunks_embedding_hnsw')),
                   pg_relation_size('idx_chunks_embedding_hnsw') / 1024.0 / 1024.0 as mb
        """
        )
    )
    row = result.fetchone()
    return float(row[1]) if row else 0.0


def benchmark_config(
    session: Any,
    config: BenchmarkConfig,
    test_embeddings: list[list[float]],
    vector_count: int,
) -> BenchmarkResult:
    """Run benchmark for a single configuration."""
    logger.info(
        f"Testing config: m={config.m}, ef_construction={config.ef_construction}, "
        f"ef_search={config.ef_search}"
    )

    # Drop and recreate index
    drop_hnsw_index(session)
    build_time = create_hnsw_index(session, config)
    index_size = get_index_size(session)

    logger.info(f"  Index built in {build_time:.1f}s, size: {index_size:.1f}MB")

    # Warm up
    for emb in test_embeddings[:5]:
        hnsw_search(session, emb, config.ef_search)

    # Benchmark queries
    latencies = []
    recalls = []

    for emb in test_embeddings:
        # Get ground truth (exact search)
        # Note: For large datasets, you might want to precompute this
        ground_truth = exact_search(session, emb, limit=10)

        # Measure HNSW search
        start = time.perf_counter()
        results = hnsw_search(session, emb, config.ef_search, limit=10)
        latency_ms = (time.perf_counter() - start) * 1000
        latencies.append(latency_ms)

        # Calculate recall
        recall = calculate_recall(ground_truth, results)
        recalls.append(recall)

    # Calculate statistics
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    avg_latency = statistics.mean(latencies)
    qps = 1000.0 / avg_latency if avg_latency > 0 else 0
    recall_at_10 = statistics.mean(recalls)

    logger.info(
        f"  P50: {p50:.1f}ms, P95: {p95:.1f}ms, P99: {p99:.1f}ms, "
        f"QPS: {qps:.1f}, Recall@10: {recall_at_10:.3f}"
    )

    return BenchmarkResult(
        config=config,
        vector_count=vector_count,
        index_build_time_seconds=build_time,
        index_size_mb=index_size,
        query_latency_p50_ms=p50,
        query_latency_p95_ms=p95,
        query_latency_p99_ms=p99,
        qps=qps,
        recall_at_10=recall_at_10,
    )


def run_benchmark(
    database_url: str,
    test_queries: int = 100,
    output_file: str | None = None,
) -> list[BenchmarkResult]:
    """Run full HNSW benchmark suite."""
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Check prerequisites
        vector_count = get_vector_count(session)
        if vector_count == 0:
            logger.error("No vectors found in document_chunks. Index some documents first.")
            sys.exit(1)

        logger.info(f"Found {vector_count:,} vectors in database")

        # Get test embeddings
        test_embeddings = get_sample_embeddings(session, test_queries)
        if len(test_embeddings) < test_queries:
            logger.warning(
                f"Only found {len(test_embeddings)} embeddings, requested {test_queries}"
            )

        logger.info(f"Running benchmark with {len(test_embeddings)} test queries")

        # Run benchmarks
        results = []
        for config in BENCHMARK_CONFIGS:
            try:
                result = benchmark_config(session, config, test_embeddings, vector_count)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to benchmark config {config}: {e}")

        # Restore default index (medium scale)
        logger.info("Restoring default HNSW index...")
        drop_hnsw_index(session)
        default_config = BenchmarkConfig(m=24, ef_construction=128, ef_search=100)
        create_hnsw_index(session, default_config)

    # Output results
    if output_file:
        output_data = {
            "vector_count": vector_count,
            "test_queries": len(test_embeddings),
            "results": [
                {
                    "config": asdict(r.config),
                    "index_build_time_seconds": r.index_build_time_seconds,
                    "index_size_mb": r.index_size_mb,
                    "query_latency_p50_ms": r.query_latency_p50_ms,
                    "query_latency_p95_ms": r.query_latency_p95_ms,
                    "query_latency_p99_ms": r.query_latency_p99_ms,
                    "qps": r.qps,
                    "recall_at_10": r.recall_at_10,
                }
                for r in results
            ],
        }
        with open(output_file, "w") as f:
            json.dump(output_data, f, indent=2)
        logger.info(f"Results saved to {output_file}")

    # Print summary table
    print("\n" + "=" * 80)
    print("HNSW BENCHMARK RESULTS")
    print("=" * 80)
    print(f"Vector count: {vector_count:,}")
    print(f"Test queries: {len(test_embeddings)}")
    print("-" * 80)
    print(
        f"{'m':>4} {'ef_c':>6} {'ef_s':>6} {'Build(s)':>10} {'Size(MB)':>10} "
        f"{'P50(ms)':>10} {'P99(ms)':>10} {'QPS':>8} {'Recall':>8}"
    )
    print("-" * 80)

    for r in results:
        print(
            f"{r.config.m:>4} {r.config.ef_construction:>6} {r.config.ef_search:>6} "
            f"{r.index_build_time_seconds:>10.1f} {r.index_size_mb:>10.1f} "
            f"{r.query_latency_p50_ms:>10.1f} {r.query_latency_p99_ms:>10.1f} "
            f"{r.qps:>8.1f} {r.recall_at_10:>8.3f}"
        )

    print("=" * 80)

    # Recommendation
    best = max(results, key=lambda r: r.recall_at_10 * r.qps)
    print(f"\nRecommended config for your dataset ({vector_count:,} vectors):")
    print(
        f"  m={best.config.m}, ef_construction={best.config.ef_construction}, "
        f"ef_search={best.config.ef_search}"
    )
    print(f"  Expected: {best.qps:.1f} QPS, {best.recall_at_10:.3f} recall@10")

    return results


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Benchmark pgvector HNSW parameters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/benchmark_hnsw.py --database-url postgresql://user:pass@localhost/nexus
  python scripts/benchmark_hnsw.py --database-url postgresql://... --test-queries 200 --output results.json
        """,
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="PostgreSQL database URL",
    )
    parser.add_argument(
        "--test-queries",
        type=int,
        default=100,
        help="Number of test queries to run (default: 100)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file for JSON results",
    )

    args = parser.parse_args()

    run_benchmark(
        database_url=args.database_url,
        test_queries=args.test_queries,
        output_file=args.output,
    )


if __name__ == "__main__":
    main()
