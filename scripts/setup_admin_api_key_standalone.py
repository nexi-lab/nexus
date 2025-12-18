#!/usr/bin/env python3
"""
Setup admin API key in the Nexus database.

This script ensures the admin user exists in the entity registry and creates/verifies
the admin API key. Can be used standalone or called from docker-entrypoint.sh.

Usage:
    python scripts/setup_admin_api_key_standalone.py <database_url> <api_key> [tenant_id] [user_id]
    
    Or with environment variables:
    NEXUS_DATABASE_URL=postgresql://... NEXUS_API_KEY=sk-... python scripts/setup_admin_api_key_standalone.py
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add src to path for imports
script_dir = Path(__file__).parent
src_dir = script_dir.parent / "src"
sys.path.insert(0, str(src_dir))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.core.entity_registry import EntityRegistry
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel


def create_new_admin_api_key(
    database_url: str,
    tenant_id: str = "default",
    user_id: str = "admin",
    key_name: str | None = None,
    expires_days: int = 90,
    skip_permissions: bool = False,
) -> tuple[str, bool]:
    """
    Create a new admin API key in the database.

    Args:
        database_url: Database connection URL (postgresql://, sqlite://, etc.)
        tenant_id: Tenant ID (default: "default")
        user_id: User ID (default: "admin")
        key_name: Optional name for the key (default: auto-generated)
        expires_days: Days until expiry (default: 90)
        skip_permissions: Skip entity registry registration if True

    Returns:
        Tuple of (raw_api_key, success)
    """
    try:
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)

        # Register user in entity registry
        if not skip_permissions:
            entity_registry = EntityRegistry(SessionFactory)
            try:
                entity_registry.register_entity(
                    entity_type="user",
                    entity_id=user_id,
                    parent_type="tenant",
                    parent_id=tenant_id,
                )
                print(f"✓ Registered user {user_id} in entity registry")
            except Exception as e:
                print(f"  User {user_id} already exists (or registration skipped): {str(e)[:80]}")

        # Generate new API key
        with SessionFactory() as session:
            try:
                from datetime import timedelta

                expires_at = datetime.now(UTC) + timedelta(days=expires_days)

                # Generate new API key
                key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                    session,
                    user_id=user_id,
                    name=key_name or "Admin key (Docker auto-generated)",
                    tenant_id=tenant_id,
                    is_admin=True,
                    expires_at=expires_at,
                )
                session.commit()

                print(f"API Key: {raw_key}")
                print(f"✓ Created admin API key for {user_id}")
                print(f"  Expires: {expires_at.isoformat()}")
                return raw_key, True

            except Exception as e:
                print(f"ERROR creating API key: {e}", file=sys.stderr)
                import traceback

                traceback.print_exc()
                session.rollback()
                return "", False

    except Exception as e:
        print(f"ERROR connecting to database: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return "", False


def setup_admin_api_key(
    database_url: str,
    admin_api_key: str,
    tenant_id: str = "default",
    user_id: str = "admin",
    key_name: str | None = None,
    expires_days: int | None = 90,
) -> bool:
    """
    Setup admin user and API key in the database.

    Args:
        database_url: Database connection URL (postgresql://, sqlite://, etc.)
        admin_api_key: Admin API key to create/verify
        tenant_id: Tenant ID (default: "default")
        user_id: User ID (default: "admin")
        key_name: Optional name for the key (default: auto-generated)
        expires_days: Days until expiry (default: 90, None for no expiry)

    Returns:
        True if successful, False otherwise
    """
    try:
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)

        # Register user in entity registry
        entity_registry = EntityRegistry(SessionFactory)
        try:
            entity_registry.register_entity(
                entity_type="user",
                entity_id=user_id,
                parent_type="tenant",
                parent_id=tenant_id,
            )
            print(f"✓ Registered user {user_id} in entity registry")
        except Exception as e:
            # User might already exist, that's okay
            print(f"  User {user_id} already exists (or registration skipped): {str(e)[:80]}")

        # Create/verify API key
        with SessionFactory() as session:
            try:
                # Hash the key using the same method as DatabaseAPIKeyAuth
                key_hash = DatabaseAPIKeyAuth._hash_key(admin_api_key)

                # Check if THIS SPECIFIC key already exists (by hash)
                existing = session.execute(
                    select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
                ).scalar_one_or_none()

                if existing:
                    print(f"API Key: {admin_api_key}")
                    print(f"✓ Admin API key already exists (user: {existing.user_id}, name: {existing.name})")
                    # Update user_id if it's different (in case key was registered for different user)
                    if existing.user_id != user_id:
                        existing.user_id = user_id
                        existing.is_admin = 1
                        session.commit()
                        print(f"  Updated key to use admin user: {user_id}")
                    return True

                # Delete any existing keys for this user to avoid conflicts
                deleted_count = session.query(APIKeyModel).filter_by(user_id=user_id).delete()
                if deleted_count > 0:
                    print(f"  Deleted {deleted_count} existing key(s) for user {user_id}")

                # Calculate expiry
                expires_at = None
                if expires_days is not None:
                    expires_at = datetime.now(UTC).replace(year=2099, month=12, day=31)  # Far future

                # Create new key record
                new_key = APIKeyModel(
                    key_hash=key_hash,
                    user_id=user_id,
                    subject_type="user",
                    subject_id=user_id,
                    tenant_id=tenant_id,
                    is_admin=1,  # PostgreSQL uses INTEGER for boolean
                    name=key_name or f"Admin key (from environment)",
                    created_at=datetime.now(UTC),
                    expires_at=expires_at,
                    revoked=0,
                    revoked_at=None,
                    last_used_at=None,
                    inherit_permissions=0,
                )
                session.add(new_key)
                session.commit()
                print(f"API Key: {admin_api_key}")
                print(f"✓ Admin API key created for {user_id}")
                if expires_at:
                    print(f"  Expires: {expires_at.strftime('%Y-%m-%d')}")
                return True

            except Exception as e:
                print(f"ERROR creating API key: {e}", file=sys.stderr)
                import traceback

                traceback.print_exc()
                session.rollback()
                return False

    except Exception as e:
        print(f"ERROR connecting to database: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return False


def main() -> None:
    """Main entry point for CLI usage."""
    # Support both command-line args and environment variables
    database_url = None
    admin_api_key = None
    tenant_id = "default"
    user_id = "admin"
    generate_new = False

    if len(sys.argv) >= 2:
        # Command-line mode
        database_url = sys.argv[1]
        if len(sys.argv) >= 3:
            # API key provided
            admin_api_key = sys.argv[2]
            tenant_id = sys.argv[3] if len(sys.argv) > 3 else "default"
            user_id = sys.argv[4] if len(sys.argv) > 4 else "admin"
        else:
            # No API key provided - generate new one
            generate_new = True
            tenant_id = sys.argv[2] if len(sys.argv) > 2 else "default"
            user_id = sys.argv[3] if len(sys.argv) > 3 else "admin"
    else:
        # Try environment variables
        database_url = os.getenv("NEXUS_DATABASE_URL")
        admin_api_key = os.getenv("NEXUS_API_KEY")
        tenant_id = os.getenv("NEXUS_TENANT_ID", "default")
        user_id = os.getenv("NEXUS_ADMIN_USER", "admin")
        skip_permissions = os.getenv("NEXUS_SKIP_PERMISSIONS", "false").lower() == "true"

        if not database_url:
            print(
                "Usage: python setup_admin_api_key_standalone.py <database_url> [api_key] [tenant_id] [user_id]",
                file=sys.stderr,
            )
            print("\n  If api_key is omitted, a new key will be generated.", file=sys.stderr)
            print("\nOr set environment variables:", file=sys.stderr)
            print("  NEXUS_DATABASE_URL=postgresql://...", file=sys.stderr)
            print("  NEXUS_API_KEY=sk-... (optional, omit to generate new key)", file=sys.stderr)
            print("\nExamples:", file=sys.stderr)
            print(
                '  # Register existing key:\n  python setup_admin_api_key_standalone.py "postgresql://localhost/nexus" "sk-admin_key"',
                file=sys.stderr,
            )
            print(
                '  # Generate new key:\n  python setup_admin_api_key_standalone.py "postgresql://localhost/nexus"',
                file=sys.stderr,
            )
            sys.exit(1)

        if not admin_api_key:
            generate_new = True

    if not database_url:
        print("ERROR: Database URL cannot be empty", file=sys.stderr)
        sys.exit(1)

    if generate_new:
        # Generate new API key
        skip_permissions = os.getenv("NEXUS_SKIP_PERMISSIONS", "false").lower() == "true"
        raw_key, success = create_new_admin_api_key(
            database_url, tenant_id, user_id, skip_permissions=skip_permissions
        )
        sys.exit(0 if success else 1)
    else:
        # Register existing API key
        success = setup_admin_api_key(database_url, admin_api_key, tenant_id, user_id)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
