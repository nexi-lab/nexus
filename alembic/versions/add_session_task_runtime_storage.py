"""add_session_task_runtime_storage

P1a/P1b session & task storage (SW-20260915-002 §8.9/§8.10): seven new tables
so the session/task read surface is version-controlled instead of living only
under ``Base.metadata.create_all``.

- ``sessions`` — session record with the immutable ``home_zone_id``;
- ``session_runtime_runs`` — PID/runtime descriptors (execution zone plus
  delegation/grant/epoch references);
- ``session_zone_dependencies`` — grant/epoch → active runtime dependency
  index powering cancellation/revocation_pending (§8.9 item 4);
- ``session_data_records`` — home-zone routing ledger for record writes;
- ``task_specs`` / ``task_resolutions`` / ``task_attempts`` — the implicit
  Task/Resolution/Attempt persistence, including the CHECK constraints that
  the ORM side declares (resolution status/accepted-refs, attempt state).

Table order follows the FK graph (sessions → task_specs → task_resolutions →
task_attempts → session_runtime_runs → session_zone_dependencies →
session_data_records) so a fresh upgrade works on both PostgreSQL (FKs
enforced) and SQLite.

Revision ID: add_session_task_runtime_storage
Revises: add_zone_delegation_scope
Create Date: 2026-09-30

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_session_task_runtime_storage"
down_revision: Union[str, Sequence[str], None] = "add_zone_delegation_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GRANTEE_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("home_zone_id", sa.String(length=64), nullable=False),
        sa.Column("owner", GRANTEE_JSON, nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("agent_principal", sa.String(length=256), nullable=True),
        sa.Column("created_by", GRANTEE_JSON, nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_home_zone", "sessions", ["home_zone_id"])

    op.create_table(
        "task_specs",
        sa.Column("task_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.session_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_by", GRANTEE_JSON, nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("resource_refs", sa.JSON(), nullable=False),
        sa.Column("requested_mode", sa.String(length=16), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("vfs_path", sa.String(length=512), nullable=False),
        sa.Column("bytes_written", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_task_specs_session"),
    )
    # ix_task_specs_session intentionally NOT created: the unique
    # constraint uq_task_specs_session covers the same column

    op.create_table(
        "task_resolutions",
        sa.Column("resolution_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=64),
            sa.ForeignKey("task_specs.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("execution_zone_id", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("vfs_path", sa.String(length=512), nullable=False),
        sa.Column("bytes_written", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('accepted', 'rejected')", name="ck_task_resolution_status"),
        sa.CheckConstraint(
            "status = 'rejected' OR (attempt_id IS NOT NULL AND execution_zone_id IS NOT NULL)",
            name="ck_task_resolution_accepted_refs",
        ),
    )
    op.create_index("ix_task_resolutions_task", "task_resolutions", ["task_id", "decided_at"])

    op.create_table(
        "task_attempts",
        sa.Column("attempt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=64),
            sa.ForeignKey("task_specs.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "resolution_id",
            sa.String(length=64),
            sa.ForeignKey("task_resolutions.resolution_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.session_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("execution_zone_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("pid_history", sa.JSON(), nullable=False),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("vfs_path", sa.String(length=512), nullable=False),
        sa.Column("bytes_written", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'starting', 'running', 'awaiting_input', "
            "'output_ready', 'verifying', 'completed', 'failed', 'cancelled')",
            name="ck_task_attempt_state",
        ),
    )
    op.create_index("ix_task_attempts_task", "task_attempts", ["task_id", "created_at"])
    op.create_index("ix_task_attempts_session", "task_attempts", ["session_id", "created_at"])

    op.create_table(
        "session_runtime_runs",
        sa.Column("pid", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.session_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(length=64),
            sa.ForeignKey("task_attempts.attempt_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("execution_zone_id", sa.String(length=64), nullable=False),
        sa.Column("delegation_ref", sa.String(length=128), nullable=True),
        sa.Column("grant_ref", sa.String(length=64), nullable=True),
        sa.Column("authorization_epoch", sa.Integer(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_runs_session", "session_runtime_runs", ["session_id"])
    op.create_index("ix_runtime_runs_zone", "session_runtime_runs", ["execution_zone_id"])
    op.create_index("ix_runtime_runs_attempt", "session_runtime_runs", ["attempt_id"])

    op.create_table(
        "session_zone_dependencies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column(
            "pid",
            sa.String(length=64),
            sa.ForeignKey("session_runtime_runs.pid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("grant_ref", sa.String(length=64), nullable=True),
        sa.Column("authorization_epoch", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("zone_id", "grant_ref", "pid", name="uq_session_dep_grant_pid"),
    )
    op.create_index(
        "ix_session_dep_epoch", "session_zone_dependencies", ["zone_id", "authorization_epoch"]
    )

    op.create_table(
        "session_data_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.session_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("record_kind", sa.String(length=16), nullable=False),
        sa.Column("record_name", sa.String(length=256), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("vfs_path", sa.String(length=512), nullable=False),
        sa.Column("bytes_written", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "record_kind", "record_name", name="uq_session_record_named"
        ),
    )
    op.create_index("ix_session_records_zone", "session_data_records", ["zone_id"])


def downgrade() -> None:
    """Local-development rollback only."""
    op.drop_index("ix_session_records_zone", table_name="session_data_records")
    op.drop_table("session_data_records")
    op.drop_index("ix_session_dep_epoch", table_name="session_zone_dependencies")
    op.drop_table("session_zone_dependencies")
    op.drop_index("ix_runtime_runs_attempt", table_name="session_runtime_runs")
    op.drop_index("ix_runtime_runs_zone", table_name="session_runtime_runs")
    op.drop_index("ix_runtime_runs_session", table_name="session_runtime_runs")
    op.drop_table("session_runtime_runs")
    op.drop_index("ix_task_attempts_session", table_name="task_attempts")
    op.drop_index("ix_task_attempts_task", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_index("ix_task_resolutions_task", table_name="task_resolutions")
    op.drop_table("task_resolutions")
    # (no ix_task_specs_session to drop — see upgrade note)
    op.drop_table("task_specs")
    op.drop_index("ix_sessions_home_zone", table_name="sessions")
    op.drop_table("sessions")
