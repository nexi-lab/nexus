"""Drop a2a_tasks table — A2A brick removed (#2979)

Revision ID: drop_a2a_tasks
Revises: f537c8b67980
Create Date: 2026-03-14

The A2A (Agent-to-Agent) protocol brick has been removed. MCP already
covers the "external systems accessing Nexus" use case with 28 concrete
tools. The a2a_tasks table and its indexes are no longer needed.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_a2a_tasks"
down_revision: Union[str, None] = "f537c8b67980"
branch_labels: Sequence[str] | None = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("a2a_tasks")


def downgrade() -> None:
    raise NotImplementedError(
        "Cannot reverse a2a_tasks DROP TABLE — the A2A brick code that defines "
        "the schema has been deleted (#2979). Restore from a database backup or "
        "check out the commit before #2979 to recreate the table."
    )
