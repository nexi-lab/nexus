"""feat(#2169): Add agent_spec column to agent_records

Revision ID: add_agent_spec_col
Revises: 101ea1909580
Create Date: 2026-02-19

Adds a nullable TEXT column ``agent_spec`` to the ``agent_records`` table
for storing the serialized AgentSpec JSON. Follows existing patterns
(agent_metadata, context_manifest) for JSON-in-TEXT storage.

Additive only — no existing data is modified.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_agent_spec_col"
down_revision: Union[str, Sequence[str], None] = "101ea1909580"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_records", sa.Column("agent_spec", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_records", "agent_spec")
