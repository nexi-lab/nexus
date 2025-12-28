"""Add cross-tenant share index for Issue #904

This index optimizes queries for finding files shared with a user
from other tenants. Query pattern:
    WHERE subject_type=? AND subject_id=?
      AND relation IN ('shared-viewer', 'shared-editor', 'shared-owner')
      AND expires_at IS NULL

Revision ID: add_cross_tenant_share_index
Revises: 0dec0068b76e
Create Date: 2025-12-27
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_cross_tenant_share_index"
down_revision: Union[str, Sequence[str], None] = "0dec0068b76e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add partial index for cross-tenant share lookups.

    This index covers the query pattern used in _fetch_cross_tenant_shares:
    - Filter by subject (recipient of the share)
    - Filter by shared-* relations
    - Only non-expired tuples (expires_at IS NULL)

    The index is partial (only shared-* relations) to minimize storage
    since cross-tenant shares are a small fraction of all tuples.
    """
    # Create partial index for cross-tenant share lookups
    # Uses subject_type, subject_id first for equality match
    # Then relation for IN clause, then object columns for covering
    op.execute(
        text("""
            CREATE INDEX IF NOT EXISTS idx_rebac_cross_tenant_shares
            ON rebac_tuples (subject_type, subject_id, relation, object_type, object_id)
            WHERE relation IN ('shared-viewer', 'shared-editor', 'shared-owner')
              AND expires_at IS NULL
        """)
    )


def downgrade() -> None:
    """Remove cross-tenant share index."""
    op.execute(text("DROP INDEX IF EXISTS idx_rebac_cross_tenant_shares"))
