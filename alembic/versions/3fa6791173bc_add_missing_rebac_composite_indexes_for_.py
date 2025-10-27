"""add_missing_rebac_composite_indexes_for_performance

Revision ID: 3fa6791173bc
Revises: 2e326825392a
Create Date: 2025-10-26 13:39:21.347438

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3fa6791173bc"
down_revision: Union[str, Sequence[str], None] = "2e326825392a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add missing composite indexes for ReBAC query performance.

    These indexes optimize the most common query patterns in rebac_manager.py:
    1. _find_subject_sets() - finding group memberships by object and relation
    2. _find_direct_relation_tuple() - permission checks with tenant isolation
    3. _find_related_objects() - finding objects related via tupleToUserset
    """

    # CRITICAL: Index for _find_subject_sets() query (line 1341)
    # Query: WHERE tenant_id = ? AND relation = ? AND object_type = ? AND object_id = ?
    #        AND subject_relation IS NOT NULL AND (expires_at IS NULL OR expires_at >= ?)
    # This is used heavily for group-based permission checks
    op.create_index(
        "idx_rebac_tenant_obj_rel_subrel",
        "rebac_tuples",
        ["tenant_id", "relation", "object_type", "object_id", "subject_relation", "expires_at"],
        postgresql_where=sa.text("subject_relation IS NOT NULL"),  # Partial index for efficiency
    )

    # IMPORTANT: Tenant-aware version of the main check index (line 1169)
    # Query: WHERE tenant_id = ? AND subject_type = ? AND subject_id = ?
    #        AND relation = ? AND object_type = ? AND object_id = ?
    # Replaces idx_rebac_check for tenant-aware deployments
    op.create_index(
        "idx_rebac_tenant_full_check",
        "rebac_tuples",
        [
            "tenant_id",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "expires_at",
        ],
    )

    # OPTIMIZATION: Index for finding related objects (tupleToUserset pattern)
    # Query: WHERE tenant_id = ? AND object_type = ? AND object_id = ? AND relation = ?
    # Used when traversing permission hierarchies via indirect relations
    op.create_index(
        "idx_rebac_tenant_obj_reverse",
        "rebac_tuples",
        ["tenant_id", "object_type", "object_id", "relation", "subject_type", "subject_id"],
    )


def downgrade() -> None:
    """Remove composite indexes."""
    op.drop_index("idx_rebac_tenant_obj_reverse", table_name="rebac_tuples")
    op.drop_index("idx_rebac_tenant_full_check", table_name="rebac_tuples")
    op.drop_index("idx_rebac_tenant_obj_rel_subrel", table_name="rebac_tuples")
