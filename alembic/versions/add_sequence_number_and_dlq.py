"""Add sequence_number to operation_log and dead_letter_queue table.

Revision ID: add_seq_number_dlq
Revises: merge_token_rotation_delivered
Create Date: 2026-02-17

Issue #1138/#1139: Event Stream Export + Event Replay foundation.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_seq_number_dlq"
down_revision: Union[str, Sequence[str], None] = "merge_token_rotation_delivered"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    """Add sequence_number column and dead_letter_queue table."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Add sequence_number to operation_log
    op.add_column(
        "operation_log",
        sa.Column("sequence_number", sa.BigInteger(), nullable=True),
    )

    # 2. Backfill existing rows with monotonic sequence based on created_at + rowid
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE operation_log
            SET sequence_number = sub.rn
            FROM (
                SELECT operation_id,
                       ROW_NUMBER() OVER (ORDER BY created_at, operation_id) AS rn
                FROM operation_log
            ) sub
            WHERE operation_log.operation_id = sub.operation_id
            """
        )
    else:
        # SQLite: use rowid for ordering
        op.execute(
            """
            UPDATE operation_log
            SET sequence_number = (
                SELECT COUNT(*)
                FROM operation_log AS t2
                WHERE t2.rowid <= operation_log.rowid
            )
            """
        )

    # 3. Create BRIN index on (zone_id, sequence_number) — PostgreSQL only
    if dialect == "postgresql":
        op.create_index(
            "idx_operation_log_zone_seq_brin",
            "operation_log",
            ["zone_id", "sequence_number"],
            postgresql_using="brin",
        )
    else:
        # SQLite fallback: regular B-tree index
        op.create_index(
            "idx_operation_log_zone_seq_brin",
            "operation_log",
            ["zone_id", "sequence_number"],
        )

    # 4. Create dead_letter_queue table
    op.create_table(
        "dead_letter_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("exporter_name", sa.String(100), nullable=False),
        sa.Column("event_payload", sa.Text(), nullable=False),
        sa.Column("failure_type", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_dlq_exporter_unresolved",
        "dead_letter_queue",
        ["exporter_name", "created_at"],
    )
    op.create_index(
        "idx_dlq_operation",
        "dead_letter_queue",
        ["operation_id"],
    )

def downgrade() -> None:
    """Remove dead_letter_queue table and sequence_number column."""
    op.drop_index("idx_dlq_operation", table_name="dead_letter_queue")
    op.drop_index("idx_dlq_exporter_unresolved", table_name="dead_letter_queue")
    op.drop_table("dead_letter_queue")

    op.drop_index("idx_operation_log_zone_seq_brin", table_name="operation_log")
    op.drop_column("operation_log", "sequence_number")
