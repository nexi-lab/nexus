"""Add delivered column to operation_log (Issue #1241).

Transactional Outbox pattern: each operation_log row now tracks whether
its event has been delivered to downstream systems (EventBus, webhooks,
hooks).  A partial index on undelivered rows enables efficient polling
by the EventDeliveryWorker.

Revision ID: add_delivered_col_oplog
Revises: add_memory_evolution_fields
Create Date: 2026-02-15
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_delivered_col_oplog"
down_revision: Union[str, Sequence[str], None] = "add_memory_evolution_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add delivered column + partial index for outbox polling."""
    op.add_column(
        "operation_log",
        sa.Column(
            "delivered",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # PostgreSQL: partial index (only undelivered rows — small, fast)
    # SQLite: regular index (partial indexes not supported)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "idx_operation_log_undelivered",
            "operation_log",
            ["created_at"],
            postgresql_where=sa.text("delivered = false"),
        )
    else:
        # SQLite fallback: regular composite index
        op.create_index(
            "idx_operation_log_undelivered",
            "operation_log",
            ["delivered", "created_at"],
        )


def downgrade() -> None:
    """Remove delivered column and its index."""
    op.drop_index("idx_operation_log_undelivered", table_name="operation_log")
    op.drop_column("operation_log", "delivered")
