"""perf(#948): Convert vector to halfvec for 50% storage reduction

Revision ID: convert_vector_to_halfvec
Revises: add_pg_textsearch_bm25
Create Date: 2025-12-30

Converts embedding storage from float32 (vector) to float16 (halfvec) for:
- 50% storage reduction per vector (6KB -> 3KB for 1536 dims)
- 66% index size reduction
- Minimal accuracy loss (<1% recall impact)

Requires pgvector 0.7.0+ which introduced the halfvec type.

Performance impact (based on East Agile benchmarks with 1.2M vectors):
- Storage: 15GB -> 6.4GB (57% reduction)
- HNSW Index: 9.2GB -> 3.1GB (66% reduction)
- Query latency: Slightly improved due to reduced data size
- Recall accuracy: Remains consistent (<1% loss)

References:
- https://github.com/nexi-lab/nexus/issues/948
- https://github.com/pgvector/pgvector#half-precision-vectors
- https://www.eastagile.com/blogs/optimizing-vector-storage-in-postgresql-with-pgvector-halfvec
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "convert_vector_to_halfvec"
down_revision: Union[str, Sequence[str], None] = "add_pg_textsearch_bm25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert embedding column from vector to halfvec."""
    conn = op.get_bind()

    # Only applies to PostgreSQL (pgvector)
    if conn.dialect.name != "postgresql":
        return

    # Check if pgvector extension is available
    result = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    row = result.fetchone()
    if not row:
        # pgvector not installed, skip
        return

    # Check pgvector version >= 0.7.0 (halfvec support)
    version = row[0]
    version_parts = [int(x) for x in version.split(".")]
    if version_parts[0] == 0 and version_parts[1] < 7:
        import warnings

        warnings.warn(
            f"pgvector {version} does not support halfvec (requires 0.7.0+). "
            "Skipping migration. Please upgrade pgvector first.",
            stacklevel=2,
        )
        return

    # Check if embedding column exists and is vector type
    result = conn.execute(
        text("""
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_name = 'document_chunks' AND column_name = 'embedding'
        """)
    )
    col_info = result.fetchone()
    if not col_info:
        # No embedding column yet, nothing to migrate
        return

    # If already halfvec, skip
    if col_info[1] == "halfvec":
        return

    # Speed up operations with parallel workers
    conn.execute(text("SET max_parallel_maintenance_workers = 7"))
    conn.execute(text("SET maintenance_work_mem = '1GB'"))

    # Step 1: Drop existing HNSW index (uses vector_cosine_ops)
    conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))

    # Step 2: Add new halfvec column
    conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding_new halfvec(1536)"))

    # Step 3: Convert existing embeddings (this may take time for large tables)
    # The ::halfvec cast automatically converts float32 to float16
    conn.execute(
        text("""
            UPDATE document_chunks
            SET embedding_new = embedding::halfvec
            WHERE embedding IS NOT NULL
        """)
    )

    # Step 4: Drop old column and rename new one
    conn.execute(text("ALTER TABLE document_chunks DROP COLUMN embedding"))
    conn.execute(text("ALTER TABLE document_chunks RENAME COLUMN embedding_new TO embedding"))

    # Step 5: Recreate HNSW index with halfvec_cosine_ops
    # Using same tuned parameters as before (m=24, ef_construction=128)
    conn.execute(
        text("""
            CREATE INDEX idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding halfvec_cosine_ops)
            WITH (m = 24, ef_construction = 128)
        """)
    )


def downgrade() -> None:
    """Revert halfvec back to vector (float32)."""
    conn = op.get_bind()

    if conn.dialect.name != "postgresql":
        return

    # Check if pgvector extension is available
    result = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if not result.fetchone():
        return

    # Check if embedding column exists
    result = conn.execute(
        text("""
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_name = 'document_chunks' AND column_name = 'embedding'
        """)
    )
    col_info = result.fetchone()
    if not col_info or col_info[0] != "halfvec":
        # Not halfvec, nothing to downgrade
        return

    # Speed up operations
    conn.execute(text("SET max_parallel_maintenance_workers = 7"))
    conn.execute(text("SET maintenance_work_mem = '1GB'"))

    # Drop halfvec index
    conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))

    # Add vector column
    conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding_new vector(1536)"))

    # Convert halfvec back to vector (upcast from float16 to float32)
    conn.execute(
        text("""
            UPDATE document_chunks
            SET embedding_new = embedding::vector
            WHERE embedding IS NOT NULL
        """)
    )

    # Drop old and rename
    conn.execute(text("ALTER TABLE document_chunks DROP COLUMN embedding"))
    conn.execute(text("ALTER TABLE document_chunks RENAME COLUMN embedding_new TO embedding"))

    # Recreate index with vector_cosine_ops
    conn.execute(
        text("""
            CREATE INDEX idx_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 24, ef_construction = 128)
        """)
    )
