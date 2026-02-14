"""Add ipc_messages table (Issue #1469).

Replaces the raw DDL previously inlined in
``PostgreSQLStorageDriver.initialize()``.  The table is now managed
by Alembic and accessed through SQLAlchemy ORM via RecordStoreABC.

Revision ID: add_ipc_messages_table
Revises: add_contextual_chunk_fields
Create Date: 2026-02-14
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_ipc_messages_table"
down_revision: Union[str, Sequence[str], None] = "add_contextual_chunk_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ipc_messages table with zone+path indexes."""
    op.create_table(
        "ipc_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("dir_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False, server_default=sa.text("''")),
        sa.Column("is_dir", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "idx_ipc_msg_zone_path",
        "ipc_messages",
        ["zone_id", "path"],
        unique=True,
    )

    op.create_index(
        "idx_ipc_msg_zone_dir",
        "ipc_messages",
        ["zone_id", "dir_path"],
        unique=False,
    )


def downgrade() -> None:
    """Remove ipc_messages table."""
    op.drop_index("idx_ipc_msg_zone_dir", table_name="ipc_messages")
    op.drop_index("idx_ipc_msg_zone_path", table_name="ipc_messages")
    op.drop_table("ipc_messages")
