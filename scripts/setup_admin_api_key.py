#!/usr/bin/env python3
"""
Setup admin API key in the Nexus database.

This script ensures the admin user exists in the entity registry and creates/verifies
the admin API key. Extracted from local-demo.sh for better testability and reuse.

Usage:
    python scripts/setup_admin_api_key.py <database_url> <api_key>
    python scripts/setup_admin_api_key.py "$POSTGRES_URL" "$ADMIN_KEY"
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

# Add src to path for imports
script_dir = Path(__file__).parent
src_dir = script_dir.parent / "src"
sys.path.insert(0, str(src_dir))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from nexus.core.entity_registry import EntityRegistry  # noqa: E402
from nexus.server.auth.database_key import DatabaseAPIKeyAuth  # noqa: E402
from nexus.storage.models import APIKeyModel  # noqa: E402


def setup_admin_api_key(
    database_url: str, admin_api_key: str, tenant_id: str = "default", user_id: str = "admin"
) -> bool:
    """
    Setup admin user and API key in the database.

    Args:
        database_url: Database connection URL (postgresql://, sqlite://, etc.)
        admin_api_key: Admin API key to create/verify
        tenant_id: Tenant ID (default: "default")
        user_id: User ID (default: "admin")

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
        except Exception:
            # User might already exist, that's okay
            print(f"  User {user_id} already exists (or registration skipped)")

        # Create/verify API key
        with SessionFactory() as session:
            try:
                # Hash the key using the same method as DatabaseAPIKeyAuth
                key_hash = DatabaseAPIKeyAuth._hash_key(admin_api_key)

                # Check if key already exists
                existing = session.execute(
                    select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
                ).scalar_one_or_none()

                if existing:
                    print("✓ Admin API key already exists")
                    return True

                # Create new key record
                new_key = APIKeyModel(
                    key_hash=key_hash,
                    user_id=user_id,
                    subject_type="user",
                    subject_id=user_id,
                    tenant_id=tenant_id,
                    is_admin=1,  # PostgreSQL uses INTEGER for boolean
                    name=f"{user_id.capitalize()} Bootstrap Key",
                    created_at=datetime.now(UTC),
                    expires_at=None,
                    revoked=0,
                    revoked_at=None,
                    last_used_at=None,
                    inherit_permissions=0,
                )
                session.add(new_key)
                session.commit()
                print(f"✓ Admin API key created for {user_id}")
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
    if len(sys.argv) < 3:
        print(
            "Usage: python setup_admin_api_key.py <database_url> <api_key> [tenant_id] [user_id]",
            file=sys.stderr,
        )
        print("\nExample:", file=sys.stderr)
        print(
            '  python setup_admin_api_key.py "postgresql://localhost/nexus" "sk-admin_key" "default" "admin"',
            file=sys.stderr,
        )
        sys.exit(1)

    database_url = sys.argv[1]
    admin_api_key = sys.argv[2]
    tenant_id = sys.argv[3] if len(sys.argv) > 3 else "default"
    user_id = sys.argv[4] if len(sys.argv) > 4 else "admin"

    if not database_url:
        print("ERROR: Database URL cannot be empty", file=sys.stderr)
        sys.exit(1)

    if not admin_api_key:
        print("ERROR: API key cannot be empty", file=sys.stderr)
        sys.exit(1)

    success = setup_admin_api_key(database_url, admin_api_key, tenant_id, user_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
