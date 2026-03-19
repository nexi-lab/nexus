"""add_mcl_columns_to_operation_log

Issue #2929 Step 4: Extend operation_log with MCL columns.
Key Decision #2: "MCL in existing operation log, not a third event system."

Adds entity_urn, aspect_name, change_type columns to operation_log table
for metadata change log semantics.

Revision ID: a7f3e2d4c891
Revises: c4b5ef9d1ff5
Create Date: 2026-03-13

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f3e2d4c891"
down_revision: Union[str, Sequence[str], None] = "c4b5ef9d1ff5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add MCL columns to operation_log (nullable for backfill)
    op.add_column("operation_log", sa.Column("entity_urn", sa.String(255), nullable=True))
    op.add_column("operation_log", sa.Column("aspect_name", sa.String(100), nullable=True))
    op.add_column("operation_log", sa.Column("change_type", sa.String(50), nullable=True))

    # Index for efficient entity lookup
    op.create_index("idx_operation_log_entity_urn", "operation_log", ["entity_urn"])


def downgrade() -> None:
    op.drop_index("idx_operation_log_entity_urn", table_name="operation_log")
    op.drop_column("operation_log", "change_type")
    op.drop_column("operation_log", "aspect_name")
    op.drop_column("operation_log", "entity_urn")
