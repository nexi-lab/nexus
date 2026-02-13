"""add_user_model_tables

Revision ID: u1234567890a
Revises: f1234567890a
Create Date: 2025-12-19 00:00:00.000000

This migration adds user model tables for authentication:
- users: Core user account model
- user_oauth_accounts: OAuth provider account linking for authentication
- external_user_services: Configuration for external user management services

Key features:
- Multi-auth method support (password, OAuth, external)
- Soft delete support (is_active, deleted_at)
- Partial unique indexes for email/username (allows reuse after soft delete)
- ReBAC-based multi-tenant support (no primary_tenant_id)
"""

from collections.abc import Sequence
from contextlib import suppress
from typing import Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "u1234567890a"
down_revision: Union[str, Sequence[str], None] = "f1234567890a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add user model tables for authentication."""

    # Create users table
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(255), primary_key=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("avatar_url", sa.Text, nullable=True),
        sa.Column("password_hash", sa.String(512), nullable=True),
        sa.Column(
            "primary_auth_method",
            sa.String(50),
            nullable=False,
            server_default="password",
        ),
        sa.Column("external_user_id", sa.String(255), nullable=True),
        sa.Column("external_user_service", sa.String(100), nullable=True),
        sa.Column("is_global_admin", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("email_verified", sa.Integer, nullable=False, server_default="0"),
        sa.Column("user_metadata", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )

    # Create standard indexes for users table
    op.create_index("idx_users_email", "users", ["email"])
    op.create_index("idx_users_username", "users", ["username"])
    op.create_index("idx_users_auth_method", "users", ["primary_auth_method"])
    op.create_index("idx_users_external", "users", ["external_user_service", "external_user_id"])
    op.create_index("idx_users_active", "users", ["is_active"])
    op.create_index("idx_users_deleted", "users", ["deleted_at"])
    op.create_index("idx_users_created", "users", ["created_at"])
    op.create_index("idx_users_last_login", "users", ["last_login_at"])
    op.create_index("idx_users_email_active_deleted", "users", ["email", "is_active", "deleted_at"])

    # Create partial unique indexes for email and username (only for active users)
    # This allows email/username reuse after soft delete
    # Check database dialect and SQLite version
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "sqlite":
        # Check SQLite version
        result = bind.execute(text("SELECT sqlite_version()"))
        version = result.scalar()
        major, minor = map(int, version.split(".")[:2])

        if major > 3 or (major == 3 and minor >= 8):
            # SQLite 3.8.0+ supports partial indexes
            op.execute(
                text(
                    """
                CREATE UNIQUE INDEX idx_users_email_active ON users(email)
                WHERE is_active=1 AND deleted_at IS NULL AND email IS NOT NULL
            """
                )
            )
            op.execute(
                text(
                    """
                CREATE UNIQUE INDEX idx_users_username_active ON users(username)
                WHERE is_active=1 AND deleted_at IS NULL AND username IS NOT NULL
            """
                )
            )
        else:
            # SQLite < 3.8.0: Partial indexes not supported
            # Application must enforce uniqueness via code
            print(
                "WARNING: SQLite < 3.8.0 detected. Partial indexes not supported. "
                "Email/username uniqueness must be enforced in application code."
            )
    elif dialect_name == "postgresql":
        # PostgreSQL fully supports partial indexes
        op.execute(
            text(
                """
            CREATE UNIQUE INDEX idx_users_email_active ON users(email)
            WHERE is_active=1 AND deleted_at IS NULL AND email IS NOT NULL
        """
            )
        )
        op.execute(
            text(
                """
            CREATE UNIQUE INDEX idx_users_username_active ON users(username)
            WHERE is_active=1 AND deleted_at IS NULL AND username IS NOT NULL
        """
            )
        )
    else:
        # Other databases: Try to create partial indexes, may fail
        print(
            f"WARNING: Database dialect '{dialect_name}' may not support partial indexes. "
            "Attempting to create, but may fail."
        )
        with suppress(Exception):
            op.execute(
                text(
                    """
                CREATE UNIQUE INDEX idx_users_email_active ON users(email)
                WHERE is_active=1 AND deleted_at IS NULL AND email IS NOT NULL
            """
                )
            )
        with suppress(Exception):
            op.execute(
                text(
                    """
                CREATE UNIQUE INDEX idx_users_username_active ON users(username)
                WHERE is_active=1 AND deleted_at IS NULL AND username IS NOT NULL
            """
                )
            )

    # Create user_oauth_accounts table
    op.create_table(
        "user_oauth_accounts",
        sa.Column("oauth_account_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(255),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=False),
        sa.Column("provider_email", sa.String(255), nullable=True),
        sa.Column("encrypted_id_token", sa.Text, nullable=True),
        sa.Column("token_expires_at", sa.DateTime, nullable=True),
        sa.Column("provider_profile", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        # Unique constraint inline for SQLite compatibility
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_provider_user"),
    )

    # Create indexes for user_oauth_accounts
    op.create_index("idx_user_oauth_user", "user_oauth_accounts", ["user_id"])
    op.create_index("idx_user_oauth_provider", "user_oauth_accounts", ["provider"])
    op.create_index(
        "idx_user_oauth_provider_user",
        "user_oauth_accounts",
        ["provider", "provider_user_id"],
    )

    # Create external_user_services table
    op.create_table(
        "external_user_services",
        sa.Column("service_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("service_name", sa.String(100), nullable=False, unique=True),
        sa.Column("auth_endpoint", sa.Text, nullable=False),
        sa.Column("user_lookup_endpoint", sa.Text, nullable=True),
        sa.Column("auth_method", sa.String(50), nullable=False),
        sa.Column("encrypted_config", sa.Text, nullable=True),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    # Create index for external_user_services
    op.create_index("idx_external_service_name", "external_user_services", ["service_name"])


def downgrade() -> None:
    """Remove user model tables."""

    # Drop tables in reverse order (respecting foreign keys)
    with suppress(Exception):
        op.drop_table("user_oauth_accounts")

    with suppress(Exception):
        op.drop_table("external_user_services")

    with suppress(Exception):
        # Drop partial unique indexes first (if they exist)
        op.execute(text("DROP INDEX IF EXISTS idx_users_email_active"))
        op.execute(text("DROP INDEX IF EXISTS idx_users_username_active"))

    with suppress(Exception):
        op.drop_table("users")
