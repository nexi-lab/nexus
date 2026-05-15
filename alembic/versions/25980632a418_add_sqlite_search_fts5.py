"""add_sqlite_search_fts5

Revision ID: 25980632a418
Revises: rename_bypass_tenant_to_zone
Create Date: 2026-05-03 21:37:29.604223

SQLite branch: create document_chunks_fts FTS5 vtable + sync triggers
mirroring document_chunks.chunk_text. Postgres branch: no-op
(HNSW on embedding halfvec(1536) and pg_textsearch BM25 on chunk_text
already provisioned by prior migrations).

document_chunks.chunk_id is a TEXT UUID. The FTS5 vtable's
content_rowid points at SQLite's implicit integer rowid via the
content='document_chunks' link; triggers use NEW.rowid / OLD.rowid.
We never query by rowid from Python — search results JOIN back through
chunk_id (text), so the rowid stays internal to the FTS sync.
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "25980632a418"
down_revision: Union[str, Sequence[str], None] = "rename_bypass_tenant_to_zone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return  # Postgres has everything already.

    op.execute(
        text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
          chunk_text,
          content='document_chunks',
          content_rowid='rowid',
          tokenize='porter unicode61'
        )
    """)
    )
    op.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_ai
        AFTER INSERT ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(rowid, chunk_text)
          VALUES (NEW.rowid, NEW.chunk_text);
        END
    """)
    )
    op.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_ad
        AFTER DELETE ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
          VALUES ('delete', OLD.rowid, OLD.chunk_text);
        END
    """)
    )
    op.execute(
        text("""
        CREATE TRIGGER IF NOT EXISTS document_chunks_fts_au
        AFTER UPDATE OF chunk_text ON document_chunks BEGIN
          INSERT INTO document_chunks_fts(document_chunks_fts, rowid, chunk_text)
          VALUES ('delete', OLD.rowid, OLD.chunk_text);
          INSERT INTO document_chunks_fts(rowid, chunk_text)
          VALUES (NEW.rowid, NEW.chunk_text);
        END
    """)
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_au"))
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_ad"))
    op.execute(text("DROP TRIGGER IF EXISTS document_chunks_fts_ai"))
    op.execute(text("DROP TABLE IF EXISTS document_chunks_fts"))
