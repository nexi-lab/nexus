"""feat(#1192): Add contextual chunking fields to document_chunks

Revision ID: add_contextual_chunk_fields
Revises: convert_vector_to_halfvec
Create Date: 2026-02-13

Adds three nullable columns for Anthropic's Contextual Retrieval pattern:
- chunk_context (Text): LLM-generated situating context (JSON string)
- chunk_position (Integer): 0-based position within the source document
- source_document_id (String(36)): UUID linking all chunks from one indexing run

Plus an index on source_document_id for efficient per-document lookups.

Backwards compatible: all columns are nullable, existing rows unaffected.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_contextual_chunk_fields"
down_revision: Union[str, Sequence[str], None] = "convert_vector_to_halfvec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add contextual chunking columns and index."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chunk_context", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("chunk_position", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("source_document_id", sa.String(36), nullable=True))
        batch_op.create_index("idx_chunks_source_doc", ["source_document_id"])


def downgrade() -> None:
    """Remove contextual chunking columns and index."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.drop_index("idx_chunks_source_doc")
        batch_op.drop_column("source_document_id")
        batch_op.drop_column("chunk_position")
        batch_op.drop_column("chunk_context")
