"""Retype operation_log.snapshot_hash VARCHAR(64) → TEXT (Issue #4645).

The column was sized for a CAS hex digest, but path-addressed backends
use their storage key (``zone/<zone>/<path>``) as the content_id — any
rename of a path whose key exceeds 64 chars overflowed the INSERT and
the oplog row was silently lost while the operation reported success
(audit-trail gap; operation_log replays miss those ops).

``schema_invariants._ensure_operation_log_snapshot_hash_text`` applies
the same repair on boot for stores initialised from ORM metadata.

Revision ID: oplog_snapshot_hash_text
Revises: add_path_context_weight
Create Date: 2026-08-11
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "oplog_snapshot_hash_text"
down_revision: Union[str, Sequence[str], None] = "add_path_context_weight"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Widen snapshot_hash to TEXT so storage keys can't overflow it."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "operation_log",
            "snapshot_hash",
            existing_type=sa.String(64),
            type_=sa.Text(),
            existing_nullable=True,
        )
    # SQLite stores TEXT regardless of declared VARCHAR length and does
    # not enforce it — no rewrite needed (ALTER COLUMN TYPE is also
    # unsupported there without a table rebuild).


def downgrade() -> None:
    """Narrow back to VARCHAR(64) — truncates nothing by itself, but
    values longer than 64 chars will make PostgreSQL reject the ALTER;
    that is intentional (the data is why the column was widened)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "operation_log",
            "snapshot_hash",
            existing_type=sa.Text(),
            type_=sa.String(64),
            existing_nullable=True,
        )
