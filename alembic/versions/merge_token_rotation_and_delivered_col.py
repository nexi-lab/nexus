"""Merge token rotation and delivered column migrations.

Revision ID: merge_token_rotation_delivered
Revises: add_delivered_col_oplog, add_token_rotation_secrets_audit
Create Date: 2026-02-15
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_token_rotation_delivered"
down_revision: Union[str, Sequence[str], None] = (
    "add_delivered_col_oplog",
    "add_token_rotation_secrets_audit",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge heads — no schema changes."""


def downgrade() -> None:
    """Merge heads — no schema changes."""
