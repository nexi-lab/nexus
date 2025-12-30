"""perf(#947): Tune HNSW index parameters for 100K+ vector scale

Revision ID: tune_hnsw_index_for_100k_vectors
Revises: update_file_namespace_shared
Create Date: 2025-12-28

Recreates the HNSW index with optimized parameters for 100K+ vectors:
- m=24: More connections for high-dimensional data (1536 dims), improves recall
- ef_construction=128: Better graph quality at build time

Performance impact (based on pgvector benchmarks):
- Before (defaults m=16, ef_construction=64): ~20 QPS, 0.95 recall
- After (tuned): ~40 QPS, 0.998 recall

Note: This migration drops and recreates the index, which may take time
for large tables. We use parallel workers to speed up the build.

References:
- https://github.com/nexi-lab/nexus/issues/947
- https://github.com/pgvector/pgvector#hnsw
- https://supabase.com/blog/increase-performance-pgvector-hnsw
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "tune_hnsw_index_for_100k_vectors"
down_revision: Union[str, Sequence[str], None] = "update_file_namespace_shared"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Recreate HNSW index with tuned parameters for better performance."""
    conn = op.get_bind()

    # Only applies to PostgreSQL (pgvector)
    if conn.dialect.name != "postgresql":
        return

    # Check if pgvector extension is available
    result = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if not result.fetchone():
        # pgvector not installed, skip
        return

    # Check if the index exists
    result = conn.execute(
        text("""
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'idx_chunks_embedding_hnsw'
        """)
    )
    if not result.fetchone():
        # Index doesn't exist (table might be empty or not created yet)
        # The vector_db.py will create it with tuned params on next init
        return

    # Speed up index creation with parallel workers
    conn.execute(text("SET max_parallel_maintenance_workers = 7"))
    conn.execute(text("SET maintenance_work_mem = '1GB'"))

    # Drop existing index (uses default params)
    conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))

    # Recreate with tuned parameters
    # m=24: More connections per node for high-dimensional data
    # ef_construction=128: Better graph quality (must be >= 2*m)
    conn.execute(
        text("""
            CREATE INDEX idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 24, ef_construction = 128)
        """)
    )


def downgrade() -> None:
    """Revert to default HNSW index parameters."""
    conn = op.get_bind()

    if conn.dialect.name != "postgresql":
        return

    # Check if pgvector extension is available
    result = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if not result.fetchone():
        return

    # Check if the index exists
    result = conn.execute(
        text("""
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'idx_chunks_embedding_hnsw'
        """)
    )
    if not result.fetchone():
        return

    # Drop tuned index
    conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))

    # Recreate with default parameters
    conn.execute(
        text("""
            CREATE INDEX idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
        """)
    )
