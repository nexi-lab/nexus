"""feat: Add subject isolation to secret_store unique constraint

Revision ID: add_secret_store_subject_isolation
Revises: add_secret_store_tables
Create Date: 2026-04-07

Changes:
1. Replace unique constraint (namespace, key) with (namespace, key, subject_id, subject_type)
   to allow different subjects to store secrets under the same namespace+key.
2. Add composite index (namespace, key, subject_id, subject_type) for query performance.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_secret_store_subject_isolation"
down_revision: Union[str, Sequence[str], None] = "add_secret_store_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace unique constraint and add composite index."""
    op.drop_constraint("uq_secret_store_namespace_key", "secret_store", type_="unique")
    op.create_unique_constraint(
        "uq_secret_store_ns_key_subject",
        "secret_store",
        ["namespace", "key", "subject_id", "subject_type"],
    )
    op.create_index(
        "idx_secret_store_ns_key_subject",
        "secret_store",
        ["namespace", "key", "subject_id", "subject_type"],
    )


def downgrade() -> None:
    """Restore original unique constraint."""
    op.drop_index("idx_secret_store_ns_key_subject", table_name="secret_store")
    op.drop_constraint("uq_secret_store_ns_key_subject", "secret_store", type_="unique")
    op.create_unique_constraint(
        "uq_secret_store_namespace_key",
        "secret_store",
        ["namespace", "key"],
    )
