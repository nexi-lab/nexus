#!/usr/bin/env python3
"""Create API keys for Nexus users.

Usage:
    python create-api-key.py alice "Alice's laptop" --admin
    python create-api-key.py bob "Bob's server" --days 90
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.entity_registry import EntityRegistry
from nexus.server.auth.database_key import DatabaseAPIKeyAuth


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Nexus API key")
    parser.add_argument("user_id", help="User ID (e.g., alice, bob)")
    parser.add_argument("name", help="Key name (e.g., 'Alice laptop')")
    parser.add_argument("--admin", action="store_true", help="Grant admin privileges")
    parser.add_argument("--days", type=int, help="Expiry in days (optional)")
    parser.add_argument("--tenant-id", default="default", help="Tenant ID (default: default)")

    args = parser.parse_args()

    # Get database URL from environment
    database_url = os.getenv("NEXUS_DATABASE_URL")
    if not database_url:
        print("Error: NEXUS_DATABASE_URL environment variable not set")
        print("Example: export NEXUS_DATABASE_URL='postgresql://nexus:password@localhost/nexus'")
        sys.exit(1)

    # Create engine and session
    engine = create_engine(database_url)
    SessionFactory = sessionmaker(bind=engine)

    # Calculate expiry if specified
    expires_at = None
    if args.days:
        expires_at = datetime.now(UTC) + timedelta(days=args.days)

    # Register user in entity registry (for agent permission inheritance)
    entity_registry = EntityRegistry(SessionFactory)
    entity_registry.register_entity(
        entity_type="user",
        entity_id=args.user_id,
        parent_type="tenant",
        parent_id=args.tenant_id,
    )

    # Create API key
    with SessionFactory() as session:
        try:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id=args.user_id,
                name=args.name,
                tenant_id=args.tenant_id,
                is_admin=args.admin,
                expires_at=expires_at,
            )
            session.commit()

            print(f"✓ Created API key for user '{args.user_id}'")
            print(f"  Name: {args.name}")
            print(f"  Admin: {args.admin}")
            if expires_at:
                print(f"  Expires: {expires_at.strftime('%Y-%m-%d')}")
            else:
                print("  Expires: Never")
            print()
            print("IMPORTANT: Save this key - it will not be shown again!")
            print()
            print(f"  API Key: {raw_key}")
            print()
            print("Use with:")
            print(f"  export NEXUS_API_KEY='{raw_key}'")
            print("  nexus ls /workspace --remote-url http://localhost:8080")

        except Exception as e:
            print(f"Error creating API key: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
