"""Add tiger_directory_grants table for Leopard-style permission expansion

Revision ID: add_tiger_directory_grants
Revises: add_memory_hierarchy_fields
Create Date: 2026-01-18

Tracks directory-level permission grants to enable:
1. Pre-materialization: When permission granted on directory, expand to all descendants
2. New file integration: When file created, inherit permissions from ancestor directories
3. Move handling: When file moves, update permissions based on old/new ancestors

Related to: Pre-materialize directory grants optimization (100-1000x speedup)

Performance Impact:
- Read operations: O(depth) inheritance walk -> O(1) bitmap lookup
- Write operations: O(1) -> O(descendants) on directory grant (amortized)
- New file creation: O(1) -> O(ancestor_grants) (typically small)
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_tiger_directory_grants"
down_revision: Union[str, Sequence[str], None] = "add_memory_hierarchy_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create tiger_directory_grants table and related indexes."""
    bind = op.get_bind()

    # Table to track directory-level permission grants
    # Used for:
    # 1. Expanding grants to all descendants (pre-materialization)
    # 2. Adding new files to existing grants (inheritance)
    # 3. Cleaning up on grant revocation
    op.create_table(
        "tiger_directory_grants",
        sa.Column("grant_id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Subject (who has access)
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        # Permission type (read, write, execute)
        sa.Column("permission", sa.String(50), nullable=False),
        # Directory path that was granted (e.g., /workspace/project/)
        sa.Column("directory_path", sa.Text, nullable=False),
        # Tenant isolation
        sa.Column("tenant_id", sa.String(255), nullable=False),
        # Revision at time of grant (for consistency - prevents "new enemy" problem)
        # Files created after this revision are NOT automatically included
        sa.Column("grant_revision", sa.BigInteger, nullable=False, default=0),
        # Whether to include files created after the grant (user choice)
        sa.Column("include_future_files", sa.Boolean, nullable=False, default=True),
        # Expansion status: pending, in_progress, completed, failed
        sa.Column(
            "expansion_status",
            sa.String(20),
            nullable=False,
            default="pending",
        ),
        # Number of descendants expanded (for progress tracking)
        sa.Column("expanded_count", sa.Integer, nullable=False, default=0),
        # Total descendants to expand (set when expansion starts)
        sa.Column("total_count", sa.Integer, nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Error info if expansion failed
        sa.Column("error_message", sa.Text, nullable=True),
        # Unique constraint: one grant per (subject, permission, directory, tenant)
        sa.UniqueConstraint(
            "tenant_id",
            "directory_path",
            "permission",
            "subject_type",
            "subject_id",
            name="uq_tiger_directory_grants",
        ),
    )

    # Index for finding grants by path prefix (for new file integration)
    # When file /workspace/project/file.txt is created, we need to find all grants
    # on /workspace/project/, /workspace/, and /
    if bind.dialect.name == "postgresql":
        # PostgreSQL: Use text_pattern_ops for efficient prefix matching
        op.execute(
            """
            CREATE INDEX idx_tiger_dir_grants_path_prefix
            ON tiger_directory_grants (tenant_id, directory_path text_pattern_ops)
            """
        )
    else:
        # SQLite: Regular index (LIKE prefix still works but less efficient)
        op.create_index(
            "idx_tiger_dir_grants_path_prefix",
            "tiger_directory_grants",
            ["tenant_id", "directory_path"],
        )

    # Index for finding grants by subject (for cache invalidation)
    op.create_index(
        "idx_tiger_dir_grants_subject",
        "tiger_directory_grants",
        ["tenant_id", "subject_type", "subject_id"],
    )

    # Index for pending expansions (for background worker)
    op.create_index(
        "idx_tiger_dir_grants_pending",
        "tiger_directory_grants",
        ["expansion_status", "created_at"],
    )

    # Index for permission lookups (finding all grants for a directory)
    op.create_index(
        "idx_tiger_dir_grants_lookup",
        "tiger_directory_grants",
        ["tenant_id", "directory_path", "permission"],
    )


def downgrade() -> None:
    """Drop tiger_directory_grants table and indexes."""
    bind = op.get_bind()

    op.drop_index("idx_tiger_dir_grants_lookup", table_name="tiger_directory_grants")
    op.drop_index("idx_tiger_dir_grants_pending", table_name="tiger_directory_grants")
    op.drop_index("idx_tiger_dir_grants_subject", table_name="tiger_directory_grants")
    op.drop_index("idx_tiger_dir_grants_path_prefix", table_name="tiger_directory_grants")
    op.drop_table("tiger_directory_grants")
