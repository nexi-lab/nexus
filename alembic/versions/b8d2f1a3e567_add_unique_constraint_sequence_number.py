"""Add unique constraint to operation_log.sequence_number.

Issue #2929 Round 9: The model marks sequence_number as unique=True and the
retry logic in OperationLogger.log_operation() depends on that constraint,
but the original migration (add_seq_number_dlq) only added the column as
nullable without uniqueness. This migration adds the constraint so upgraded
databases enforce the same invariant as fresh installs.

Revision ID: b8d2f1a3e567
Revises: a7f3e2d4c891
Create Date: 2026-03-13

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d2f1a3e567"
down_revision: Union[str, Sequence[str], None] = "a7f3e2d4c891"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unique constraint to operation_log.sequence_number."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Deduplicate any existing duplicates before adding the constraint.
        # Keep the row with the smallest operation_id (deterministic tie-break).
        op.execute(
            """
            DELETE FROM operation_log
            WHERE operation_id IN (
                SELECT operation_id FROM (
                    SELECT operation_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY sequence_number
                               ORDER BY operation_id
                           ) AS rn
                    FROM operation_log
                    WHERE sequence_number IS NOT NULL
                ) sub
                WHERE sub.rn > 1
            )
            """
        )
        op.create_unique_constraint(
            "uq_operation_log_sequence_number",
            "operation_log",
            ["sequence_number"],
        )
    else:
        # SQLite: deduplicate before rebuilding the table via batch mode.
        # rowid is SQLite's implicit stable PK — used as deterministic tie-break.
        op.execute(
            """
            DELETE FROM operation_log
            WHERE sequence_number IS NOT NULL
              AND rowid NOT IN (
                SELECT MIN(rowid)
                FROM operation_log
                WHERE sequence_number IS NOT NULL
                GROUP BY sequence_number
              )
            """
        )
        # batch_alter_table is required; SQLite has no native ALTER of constraints.
        with op.batch_alter_table("operation_log") as batch_op:
            batch_op.create_unique_constraint(
                "uq_operation_log_sequence_number",
                ["sequence_number"],
            )


def downgrade() -> None:
    """Remove unique constraint from operation_log.sequence_number."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_constraint(
            "uq_operation_log_sequence_number",
            "operation_log",
            type_="unique",
        )
    else:
        with op.batch_alter_table("operation_log") as batch_op:
            batch_op.drop_constraint(
                "uq_operation_log_sequence_number",
                type_="unique",
            )
