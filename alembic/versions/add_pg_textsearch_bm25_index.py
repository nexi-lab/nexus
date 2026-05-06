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
    """Add a BM25 index for native ranking.

    Issue #3699 update: paradedb/paradedb (the bundled image post-#3699)
    ships ``pg_search``; Tiger Data's ``pg_textsearch`` is a separate
    fork with the same ``@@@`` operator but different ``CREATE INDEX``
    syntax. Probe for whichever is available and skip cleanly otherwise
    so the rest of the alembic chain can still apply.
    """
    conn = op.get_bind()

    if conn.dialect.name != "postgresql":
        return

    # Detect already-installed BM25 extension first (paradedb image
    # enables pg_search via the init SQL, so a fresh stack hits this
    # branch and we just create the index).
    have = {
        row[0]
        for row in conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('pg_search', 'pg_textsearch')")
        ).fetchall()
    }

    # If neither installed, try to install pg_search first (paradedb)
    # then pg_textsearch (Tiger Data). Each attempt runs in a
    # SAVEPOINT so a failure does NOT poison the outer migration
    # transaction — without this, the next migration would fail with
    # "current transaction is aborted, commands ignored ..." (Issue #3699).
    if not have:
        for ext in ("pg_search", "pg_textsearch"):
            sp = "try_install_" + ext
            try:
                conn.execute(text(f"SAVEPOINT {sp}"))
                conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
                conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
                have.add(ext)
                break
            except Exception:
                conn.execute(text(f"ROLLBACK TO SAVEPOINT {sp}"))
                conn.execute(text(f"RELEASE SAVEPOINT {sp}"))

    if not have:
        import warnings

        warnings.warn(
            "Neither pg_search (paradedb) nor pg_textsearch (Tiger Data) "
            "is installed. Skipping BM25 index creation; keyword search "
            "will fail at query time. Use the bundled paradedb image or "
            "install one of these extensions manually.",
            stacklevel=2,
        )
        return

    # Index DDL differs between the two extensions; both are queried
    # via the same ``@@@`` operator so the runtime backend doesn't care.
    if "pg_search" in have:
        # paradedb pg_search: requires explicit key_field + text_fields JSON.
        chunks_ddl = """
            CREATE INDEX IF NOT EXISTS idx_chunks_bm25
            ON document_chunks
            USING bm25 (chunk_id, chunk_text)
            WITH (key_field='chunk_id', text_fields='{"chunk_text": {}}')
        """
        paths_ddl = """
            CREATE INDEX IF NOT EXISTS idx_file_paths_bm25
            ON file_paths
            USING bm25 (path_id, virtual_path)
            WITH (key_field='path_id', text_fields='{"virtual_path": {}}')
        """
    else:
        # Tiger Data pg_textsearch: text_config-only DDL.
        chunks_ddl = """
            CREATE INDEX IF NOT EXISTS idx_chunks_bm25
            ON document_chunks USING bm25(chunk_text)
            WITH (text_config='english')
        """
        paths_ddl = """
            CREATE INDEX IF NOT EXISTS idx_file_paths_bm25
            ON file_paths USING bm25(virtual_path)
            WITH (text_config='simple')
        """

    # Wrap the index creation too — index syntax errors must not poison
    # the outer transaction either.
    for ddl in (chunks_ddl, paths_ddl):
        sp = "try_idx_bm25"
        try:
            conn.execute(text(f"SAVEPOINT {sp}"))
            conn.execute(text(ddl))
            conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
        except Exception as exc:
            conn.execute(text(f"ROLLBACK TO SAVEPOINT {sp}"))
            conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
            import warnings

            warnings.warn(
                f"BM25 index creation failed: {exc}. "
                "Keyword search may degrade to ts_rank fallback at runtime.",
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
