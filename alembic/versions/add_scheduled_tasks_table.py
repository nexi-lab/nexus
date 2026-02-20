"""fix(#698): Add scheduled_tasks table via Alembic migration

Revision ID: add_scheduled_tasks_tbl
Revises: merge_agent_spec_zone_phase
Create Date: 2026-02-21

Replaces runtime DDL in server/lifespan/services.py with a proper
Alembic migration. Includes all Astraea columns (Issue #1274) and
fixes zone_id default from 'default' to 'root' (ROOT_ZONE_ID).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_scheduled_tasks_tbl"
down_revision: Union[str, Sequence[str], None] = "merge_agent_spec_zone_phase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create table only if it doesn't already exist (idempotent for deployments
    # that used the old runtime DDL).
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "scheduled_tasks" not in inspector.get_table_names():
        op.create_table(
            "scheduled_tasks",
            sa.Column(
                "id", sa.Text(), server_default=sa.text("gen_random_uuid()::text"), nullable=False
            ),
            sa.Column("agent_id", sa.Text(), nullable=False),
            sa.Column("executor_id", sa.Text(), nullable=False),
            sa.Column("task_type", sa.Text(), nullable=False),
            sa.Column(
                "payload",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default="{}",
                nullable=False,
            ),
            sa.Column("priority_tier", sa.SmallInteger(), nullable=False, server_default="2"),
            sa.Column("effective_tier", sa.SmallInteger(), nullable=False, server_default="2"),
            sa.Column(
                "enqueued_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "boost_amount",
                sa.Numeric(precision=12, scale=6),
                nullable=False,
                server_default="0",
            ),
            sa.Column("boost_tiers", sa.SmallInteger(), nullable=False, server_default="0"),
            sa.Column("boost_reservation_id", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
            sa.Column("zone_id", sa.String(length=255), nullable=False, server_default="root"),
            sa.Column("error_message", sa.Text(), nullable=True),
            # Astraea columns (Issue #1274)
            sa.Column("request_state", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("priority_class", sa.Text(), nullable=False, server_default="batch"),
            sa.Column("executor_state", sa.Text(), nullable=False, server_default="UNKNOWN"),
            sa.Column("estimated_service_time", sa.Float(), nullable=False, server_default="30.0"),
            sa.PrimaryKeyConstraint("id"),
        )

        # Indexes
        op.create_index(
            "idx_scheduled_tasks_dequeue",
            "scheduled_tasks",
            ["effective_tier", "enqueued_at"],
            postgresql_where=sa.text("status = 'queued'"),
        )
        op.create_index(
            "idx_sched_astraea_dequeue",
            "scheduled_tasks",
            ["priority_class", "enqueued_at"],
            postgresql_where=sa.text("status = 'queued'"),
        )
        op.create_index("idx_scheduled_tasks_status", "scheduled_tasks", ["status"])
        op.create_index("idx_scheduled_tasks_zone", "scheduled_tasks", ["zone_id"])
    else:
        # Table exists from runtime DDL — ensure Astraea columns + correct defaults.
        existing_cols = {c["name"] for c in inspector.get_columns("scheduled_tasks")}

        astraea_cols = [
            ("request_state", sa.Text(), "pending"),
            ("priority_class", sa.Text(), "batch"),
            ("executor_state", sa.Text(), "UNKNOWN"),
            ("estimated_service_time", sa.Float(), "30.0"),
        ]
        for col_name, col_type, default in astraea_cols:
            if col_name not in existing_cols:
                op.add_column(
                    "scheduled_tasks",
                    sa.Column(col_name, col_type, nullable=False, server_default=default),
                )

        # Fix zone_id default from 'default' to 'root' (ROOT_ZONE_ID)
        op.alter_column(
            "scheduled_tasks",
            "zone_id",
            server_default="root",
        )

        # Ensure HRRN index exists
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("scheduled_tasks")}
        if "idx_sched_astraea_dequeue" not in existing_indexes:
            op.create_index(
                "idx_sched_astraea_dequeue",
                "scheduled_tasks",
                ["priority_class", "enqueued_at"],
                postgresql_where=sa.text("status = 'queued'"),
            )
        if "idx_scheduled_tasks_status" not in existing_indexes:
            op.create_index("idx_scheduled_tasks_status", "scheduled_tasks", ["status"])
        if "idx_scheduled_tasks_zone" not in existing_indexes:
            op.create_index("idx_scheduled_tasks_zone", "scheduled_tasks", ["zone_id"])


def downgrade() -> None:
    op.drop_index("idx_scheduled_tasks_zone", table_name="scheduled_tasks")
    op.drop_index("idx_scheduled_tasks_status", table_name="scheduled_tasks")
    op.drop_index("idx_sched_astraea_dequeue", table_name="scheduled_tasks")
    op.drop_index("idx_scheduled_tasks_dequeue", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
