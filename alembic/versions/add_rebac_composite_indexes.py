"""Add composite indexes for ReBAC permission checks

Revision ID: add_rebac_indexes
Revises: add_content_cache
Create Date: 2025-12-06

Adds composite indexes to rebac_tuples table for faster permission lookups.
These indexes optimize the most common query patterns:
1. Direct permission checks (subject -> relation -> object)
2. Userset/group membership lookups
3. Object permission expansion (find all subjects with access)

See: https://github.com/nexi-lab/nexus/issues/591
Expected improvement: 2-5x faster permission lookups
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_rebac_indexes"
down_revision: Union[str, Sequence[str], None] = "add_content_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add composite indexes for ReBAC permission checks."""
    # 1. Direct permission check (most common query pattern)
    # Used in: _has_direct_relation, _get_direct_relation_tuple
    # Query: WHERE subject_type=? AND subject_id=? AND relation=? AND object_type=? AND object_id=?
    op.create_index(
        "idx_rebac_permission_check",
        "rebac_tuples",
        ["subject_type", "subject_id", "relation", "object_type", "object_id", "tenant_id"],
    )

    # 2. Userset/group membership lookups
    # Used in: _find_subject_sets
    # Query: WHERE relation=? AND object_type=? AND object_id=? AND subject_relation IS NOT NULL
    op.create_index(
        "idx_rebac_userset_lookup",
        "rebac_tuples",
        ["relation", "object_type", "object_id", "subject_relation", "tenant_id"],
    )

    # 3. Object permission expansion (find all subjects with access to an object)
    # Used in: rebac_expand, _get_direct_subjects
    # Query: WHERE object_type=? AND object_id=? AND relation=? AND tenant_id=?
    op.create_index(
        "idx_rebac_object_expand",
        "rebac_tuples",
        ["object_type", "object_id", "relation", "tenant_id"],
    )


def downgrade() -> None:
    """Remove composite indexes."""
    op.drop_index("idx_rebac_object_expand", table_name="rebac_tuples")
    op.drop_index("idx_rebac_userset_lookup", table_name="rebac_tuples")
    op.drop_index("idx_rebac_permission_check", table_name="rebac_tuples")
