#!/usr/bin/env python3
"""
Create or register an admin API key in the Nexus database.

This script handles:
- Registering the admin user in entity registry (unless NEXUS_SKIP_PERMISSIONS=true)
- Creating a new API key or registering a custom one from environment
- Proper error handling and output formatting

Usage:
    python scripts/create_admin_key.py <database_url> <admin_user> [custom_key] [skip_permissions]

Output:
    Prints "API Key: <key>" on success
"""

import hashlib
import hmac
import os
import sys
from datetime import UTC, datetime, timedelta
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


def create_admin_key(
    database_url: str,
    admin_user: str,
    custom_key: str | None = None,
    skip_permissions: bool = False,
    zone_id: str = "default",
) -> tuple[str, bool]:
    """
    Create or register admin API key.

    Args:
        database_url: Database connection URL
        admin_user: Admin user ID
        custom_key: Optional custom API key to register (if None, generates new one)
        skip_permissions: If True, skip entity registry registration
        zone_id: Zone ID (default: "default")

    Returns:
        Tuple of (api_key, success)
    """
    try:
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)

        # Register user in entity registry (for agent permission inheritance)
        # Skip if NEXUS_SKIP_PERMISSIONS is set to true
        if not skip_permissions:
            try:
                entity_registry = EntityRegistry(SessionFactory)
                entity_registry.register_entity(
                    entity_type="user",
                    entity_id=admin_user,
                    parent_type="zone",
                    parent_id=zone_id,
                )
            except Exception:
                # User might already exist, that's okay
                pass
        else:
            print("Skipping entity registry setup (NEXUS_SKIP_PERMISSIONS=true)", file=sys.stderr)

        with SessionFactory() as session:
            expires_at = datetime.now(UTC) + timedelta(days=90)

            if custom_key:
                # Use custom API key from environment
                # Hash the key for storage (same as DatabaseAPIKeyAuth does)
                # Uses HMAC-SHA256 with salt (same as nexus.server.auth.database_key)
                HMAC_SALT = "nexus-api-key-v1"
                key_hash = hmac.new(
                    HMAC_SALT.encode("utf-8"), custom_key.encode("utf-8"), hashlib.sha256
                ).hexdigest()

                # Check if this specific key already exists (by hash)
                existing = session.execute(
                    select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
                ).scalar_one_or_none()
                if existing:
                    print(f"API Key: {custom_key}", file=sys.stdout)
                    print(
                        f"Custom API key already registered for user: {admin_user}",
                        file=sys.stderr,
                    )
                    return custom_key, True
                else:
                    # Insert custom key into database
                    api_key = APIKeyModel(
                        user_id=admin_user,
                        key_hash=key_hash,
                        name="Admin key (from environment)",
                        zone_id=zone_id,
                        is_admin=1,  # PostgreSQL expects integer, not boolean
                        subject_type="user",
                        subject_id=admin_user,
                        inherit_permissions=0,
                        revoked=0,
                        created_at=datetime.now(UTC),
                        expires_at=expires_at,
                    )
                    session.add(api_key)
                    session.commit()

                    print(f"API Key: {custom_key}", file=sys.stdout)
                    print(f"Registered custom API key for user: {admin_user}", file=sys.stderr)
                    print(f"Expires: {expires_at.isoformat()}", file=sys.stderr)
                    return custom_key, True
            else:
                # Generate new API key
                key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                    session,
                    user_id=admin_user,
                    name="Admin key (Docker auto-generated)",
                    zone_id=zone_id,
                    is_admin=True,
                    expires_at=expires_at,
                )
                session.commit()

                print(f"API Key: {raw_key}", file=sys.stdout)
                print(f"Created admin API key for user: {admin_user}", file=sys.stderr)
                print(f"Expires: {expires_at.isoformat()}", file=sys.stderr)
                return raw_key, True

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return "", False


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 3:
        print(
            "Usage: python create_admin_key.py <database_url> <admin_user> [custom_key] [skip_permissions]",
            file=sys.stderr,
        )
        sys.exit(1)

    database_url = sys.argv[1]
    admin_user = sys.argv[2]
    custom_key = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    skip_permissions = (len(sys.argv) > 4 and sys.argv[4].lower() == "true") or os.getenv(
        "NEXUS_SKIP_PERMISSIONS", "false"
    ).lower() == "true"

    if not database_url or not admin_user:
        print("ERROR: database_url and admin_user are required", file=sys.stderr)
        sys.exit(1)

    api_key, success = create_admin_key(database_url, admin_user, custom_key, skip_permissions)

    if not success or not api_key:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
