"""Add retry_count column to operation_log table.

Revision ID: add_retry_count_oplog
Revises: add_delivered_col_oplog
Create Date: 2026-03-06

Issue #2751: Persist retry counts for EventDeliveryWorker across restarts.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_retry_count_oplog"
down_revision: Union[str, Sequence[str], None] = "add_delivered_col_oplog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operation_log",
        sa.Column("retry_count", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("operation_log", "retry_count")
