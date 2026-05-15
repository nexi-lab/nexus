"""Drop reputation, dispute, and memory_configs tables (Issue #2988)

Revision ID: drop_reputation_memory_configs
Revises: update_file_namespace_shared
Create Date: 2026-03-14

These tables are orphaned after removing the reputation brick and
workspace memory registration. The reputation brick assumed a
multi-agent marketplace economy not needed for the core platform,
and memory registration was orphaned by memory brick removal (#2986).

Dropped tables:
- reputation_events (append-only event log)
- reputation_scores (materialized aggregates)
- disputes (dispute lifecycle state machine)
- memory_configs (memory directory registration)
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_reputation_memory_configs"
down_revision: Union[str, None] = "drop_a2a_tasks"
branch_labels: Sequence[str] | None = None
depends_on: Union[str, Sequence[str], None] = None

# Tables to drop (order doesn't matter — no FK relationships between them)
_TABLES = [
    "reputation_events",
    "reputation_scores",
    "disputes",
    "memory_configs",
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    for table in _TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    # Intentionally empty — these tables are removed permanently.
    # If needed, recreate them from the ORM models in the git history
    # prior to this commit.
    pass
