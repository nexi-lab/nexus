"""Drop a2a_tasks table — A2A brick removed (#2979)

Revision ID: drop_a2a_tasks
Revises: tune_hnsw_index_for_100k_vectors
Create Date: 2026-03-14

The A2A (Agent-to-Agent) protocol brick has been removed. MCP already
covers the "external systems accessing Nexus" use case with 28 concrete
tools. The a2a_tasks table and its indexes are no longer needed.
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_a2a_tasks"
down_revision: Union[str, None] = "tune_hnsw_index_for_100k_vectors"
branch_labels: Sequence[str] | None = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS a2a_tasks CASCADE"))


def downgrade() -> None:
    # Re-creating the table is not supported — the A2A brick code that
    # defines the schema has been deleted.  Restore from a backup or
    # check out the commit before #2979 if rollback is needed.
    pass
