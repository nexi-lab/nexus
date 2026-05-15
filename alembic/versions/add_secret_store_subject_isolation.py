"""feat: Add subject isolation to secret_store unique constraint

Revision ID: a7ss_subj_iso
Revises: a7ss_create
Create Date: 2026-04-07

Changes:
1. Replace unique constraint (namespace, key) with (namespace, key, subject_id, subject_type)
   to allow different subjects to store secrets under the same namespace+key.
2. Add composite index (namespace, key, subject_id, subject_type) for query performance.

Uses batch_alter_table for SQLite compatibility (SQLite does not support
ALTER of constraints natively — batch mode uses copy-and-move strategy).
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7ss_subj_iso"
down_revision: Union[str, Sequence[str], None] = "a7ss_create"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace unique constraint and add composite index."""
    with op.batch_alter_table("secret_store") as batch_op:
        batch_op.drop_constraint("uq_secret_store_namespace_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_secret_store_ns_key_subject",
            ["namespace", "key", "subject_id", "subject_type"],
        )
        batch_op.create_index(
            "idx_secret_store_ns_key_subject",
            ["namespace", "key", "subject_id", "subject_type"],
        )


def downgrade() -> None:
    """Restore original unique constraint."""
    with op.batch_alter_table("secret_store") as batch_op:
        batch_op.drop_index("idx_secret_store_ns_key_subject")
        batch_op.drop_constraint("uq_secret_store_ns_key_subject", type_="unique")
        batch_op.create_unique_constraint(
            "uq_secret_store_namespace_key",
            ["namespace", "key"],
        )
