"""add_chunks_embedding_halfvec

Revision ID: add_chunks_embedding_halfvec
Revises: 25980632a418
Create Date: 2026-05-05 02:00:00

Issue #3699: PgVectorBackend reads ``document_chunks.embedding`` (halfvec(1536)).
Before #3699 the column + ``idx_chunks_embedding_hnsw`` were created lazily by
``vector_db.py`` on first txtai write — that path is deleted with the txtai
backend, so a fresh Postgres DB never gets the column and dense semantic
search fails with ``column "embedding" does not exist``.

This migration:
1. Ensures ``vector`` extension is enabled (no-op if already present).
2. Adds ``document_chunks.embedding halfvec(1536)`` (NULL allowed — populated
   on indexing).
3. Creates ``idx_chunks_embedding_hnsw`` HNSW cosine index.

Postgres-only. SQLite branch is a no-op (sqlite-vec uses a separate vtable).
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_chunks_embedding_halfvec"
down_revision: Union[str, Sequence[str], None] = "25980632a418"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name != "postgresql":
        return

    # Idempotent: ensure pgvector is enabled.
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Verify pgvector >= 0.7.0 (halfvec support).
    row = conn.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).fetchone()
    if not row:
        raise RuntimeError(
            "pgvector extension required by PgVectorBackend (Issue #3699) but not installed."
        )
    parts = [int(x) for x in row[0].split(".")[:2]]
    if parts[0] == 0 and parts[1] < 7:
        raise RuntimeError(
            f"pgvector {row[0]} does not support halfvec (requires 0.7.0+). "
            "Upgrade pgvector before applying this migration."
        )

    # Add the column if it isn't there yet (existing DBs may already have
    # it from the legacy lazy-create path).
    col_exists = conn.execute(
        text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document_chunks'
              AND column_name = 'embedding'
        """)
    ).fetchone()
    if not col_exists:
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding halfvec(1536)"))

    # Create HNSW cosine index if missing.
    idx_exists = conn.execute(
        text("""
            SELECT 1 FROM pg_indexes
            WHERE indexname = 'idx_chunks_embedding_hnsw'
        """)
    ).fetchone()
    if not idx_exists:
        conn.execute(
            text("""
                CREATE INDEX idx_chunks_embedding_hnsw
                ON document_chunks
                USING hnsw (embedding halfvec_cosine_ops)
                WITH (m = 24, ef_construction = 128)
            """)
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.execute(text("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw"))
    conn.execute(text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding"))
