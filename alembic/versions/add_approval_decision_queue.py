"""Add approval decision queue tables (Issue #3790).

Postgres-only: uses JSONB and a partial unique index. The index
`approval_requests_pending_coalesce` is load-bearing for request
coalescing semantics — it ensures only one pending row exists for
any (zone_id, kind, subject) tuple at a time. Do not modify or drop
without coordinating with the approvals brick.

Revision ID: add_approval_decision_queue
Revises: 3b2a1c5d7e8f
Create Date: 2026-04-28
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "add_approval_decision_queue"
down_revision: Union[str, Sequence[str], None] = "3b2a1c5d7e8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("token_id", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(512), nullable=True),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decision_scope", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_approval_requests_status_expires",
        "approval_requests",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_approval_requests_zone_status",
        "approval_requests",
        ["zone_id", "status"],
    )
    op.create_index(
        "approval_requests_pending_coalesce",
        "approval_requests",
        ["zone_id", "kind", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            sa.String(64),
            sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_approval_decisions_request",
        "approval_decisions",
        ["request_id"],
    )

    op.create_table(
        "approval_session_allow",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(512), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column(
            "request_id",
            sa.String(64),
            sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "session_id",
            "zone_id",
            "kind",
            "subject",
            name="uq_approval_session_allow",
        ),
    )
    op.create_index(
        "ix_approval_session_allow_session",
        "approval_session_allow",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_table("approval_session_allow")
    op.drop_table("approval_decisions")
    op.drop_table("approval_requests")
