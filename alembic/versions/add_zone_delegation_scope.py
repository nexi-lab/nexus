"""Add purpose and capability-bound scope rules to zone delegations.

Revision ID: add_zone_delegation_scope
Revises: add_zone_v1_canonical_storage
Create Date: 2026-09-24
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_zone_delegation_scope"
down_revision: Union[str, Sequence[str], None] = "add_zone_v1_canonical_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Expand only: legacy rows remain NULL until their bounded TTL expires."""
    op.add_column("zone_delegations", sa.Column("purpose", sa.String(32), nullable=True))
    op.add_column("zone_delegations", sa.Column("scope_rules", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Local-development rollback only."""
    op.drop_column("zone_delegations", "scope_rules")
    op.drop_column("zone_delegations", "purpose")
