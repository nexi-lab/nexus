"""Add agent_keys table (KYA Identity Layer, Issue #1355).

Creates the agent_keys table for Ed25519 signing keys:
- Per-agent keypairs for cryptographic identity
- Fernet-encrypted private keys at rest
- DID derived from public key (unique)
- Key rotation with grace period (multiple active keys)

Revision ID: add_agent_keys_table
Revises: add_agent_records_table
Create Date: 2026-02-11
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_agent_keys_table"
down_revision: Union[str, Sequence[str], None] = "add_agent_records_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent_keys table with composite indexes."""
    op.create_table(
        "agent_keys",
        sa.Column("key_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("algorithm", sa.String(20), nullable=False, server_default="Ed25519"),
        sa.Column("public_key_bytes", sa.LargeBinary(32), nullable=False),
        sa.Column("encrypted_private_key", sa.Text(), nullable=False),
        sa.Column("did", sa.String(255), nullable=False, unique=True),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )

    # Composite index for fast agent key lookups
    op.create_index(
        "idx_agent_keys_agent_active",
        "agent_keys",
        ["agent_id", "is_active"],
    )

    # Unique DID index
    op.create_index(
        "idx_agent_keys_did",
        "agent_keys",
        ["did"],
        unique=True,
    )


def downgrade() -> None:
    """Remove agent_keys table."""
    op.drop_index("idx_agent_keys_did", table_name="agent_keys")
    op.drop_index("idx_agent_keys_agent_active", table_name="agent_keys")
    op.drop_table("agent_keys")
