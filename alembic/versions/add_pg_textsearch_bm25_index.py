"""Add pg_textsearch BM25 index for native BM25 ranking

Revision ID: add_pg_textsearch_bm25
Revises: 77274f750d1f
Create Date: 2025-12-30

Adds pg_textsearch extension and BM25 index for true BM25 relevance ranking.
This replaces the limited ts_rank() which doesn't implement proper BM25.

pg_textsearch (from Tiger Data/Timescale) provides:
- True BM25 ranking with IDF, term frequency saturation, and length normalization
- Memtable architecture for efficient indexing
- Native SQL syntax with <@> operator
- 3x higher QPS than Elasticsearch (per Tiger Data benchmarks)

BM25 parameters:
- k1=1.2: Term frequency saturation (prevents keyword stuffing)
- b=0.75: Length normalization (fair comparison across doc lengths)

Performance impact (estimated):
- Before: ts_rank() degrades to 25-30s on 800K rows
- After: BM25 queries ~10ms regardless of corpus size

Requirements:
- PostgreSQL 17+ (pg_textsearch is PG17+ only)
- Falls back to ts_rank() on older PostgreSQL versions

References:
- https://github.com/timescale/pg_textsearch
- https://www.tigerdata.com/blog/introducing-pg_textsearch-true-bm25-ranking-hybrid-retrieval-postgres
- Issue #953: perf(db): Add pg_textsearch for native BM25 ranking
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_pg_textsearch_bm25"
down_revision: str | Sequence[str] | None = "77274f750d1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add pg_textsearch extension and BM25 index (PostgreSQL 17+ only)."""
    conn = op.get_bind()

    # Only create pg_textsearch index on PostgreSQL
    # SQLite doesn't support this extension
    if conn.dialect.name != "postgresql":
        return

    # Check PostgreSQL version (pg_textsearch requires PG 17+)
    result = conn.execute(text("SHOW server_version_num"))
    version_num = int(result.scalar())

    if version_num < 170000:
        # PostgreSQL < 17, skip pg_textsearch
        import warnings

        warnings.warn(
            f"pg_textsearch requires PostgreSQL 17+, but server is {version_num // 10000}.{(version_num % 10000) // 100}. "
            "Skipping BM25 index creation. Keyword search will use ts_rank() fallback.",
            stacklevel=2,
        )
        return

    # Try to enable pg_textsearch extension
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_textsearch"))

        # Create BM25 index on document_chunks.chunk_text
        # Using English text config with default BM25 parameters (k1=1.2, b=0.75)
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_chunks_bm25
            ON document_chunks USING bm25(chunk_text)
            WITH (text_config='english')
        """)
        )

        # Create BM25 index on file_paths.virtual_path for filename search
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_file_paths_bm25
            ON file_paths USING bm25(virtual_path)
            WITH (text_config='simple')
        """)
        )

    except Exception as e:
        # pg_textsearch extension not available
        import warnings

        warnings.warn(
            f"pg_textsearch extension not available: {e}. "
            "Keyword search will use ts_rank() fallback. "
            "Install pg_textsearch for true BM25 ranking: "
            "https://github.com/timescale/pg_textsearch",
            stacklevel=2,
        )


def downgrade() -> None:
    """Remove pg_textsearch indexes and extension."""
    conn = op.get_bind()

    if conn.dialect.name != "postgresql":
        return

    # Drop BM25 indexes (ignore errors if they don't exist)
    try:
        conn.execute(text("DROP INDEX IF EXISTS idx_chunks_bm25"))
        conn.execute(text("DROP INDEX IF EXISTS idx_file_paths_bm25"))
    except Exception:
        pass

    # Note: We don't drop the extension as other databases might use it
    # conn.execute(text("DROP EXTENSION IF EXISTS pg_textsearch"))
