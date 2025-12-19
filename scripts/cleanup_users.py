"""Clean up all users from the database for testing.

This script removes all users, OAuth accounts, API keys, and tenants from the database.
USE WITH CAUTION - this will delete all user data!

Usage:
    python scripts/cleanup_users.py
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.storage.models import (
    APIKeyModel,
    TenantModel,
    UserModel,
    UserOAuthAccountModel,
)


def cleanup_database():
    """Remove all users and related data from database."""
    # Get database URL from environment or use default
    database_url = os.environ.get(
        "NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus"
    )

    print(f"Connecting to: {database_url}")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        # Count before deletion
        user_count = session.query(UserModel).count()
        oauth_count = session.query(UserOAuthAccountModel).count()
        api_key_count = session.query(APIKeyModel).count()
        tenant_count = session.query(TenantModel).count()

        print(f"\nBefore cleanup:")
        print(f"  Users: {user_count}")
        print(f"  OAuth accounts: {oauth_count}")
        print(f"  API keys: {api_key_count}")
        print(f"  Tenants: {tenant_count}")

        if user_count == 0:
            print("\nNo users to delete. Database is already clean.")
            return

        # Ask for confirmation
        response = input(
            f"\nAre you sure you want to delete ALL {user_count} users? (yes/no): "
        )
        if response.lower() != "yes":
            print("Cancelled.")
            return

        try:
            # Delete in correct order (respecting foreign keys)
            print("\nDeleting data...")

            # 1. Delete OAuth accounts (depends on users)
            deleted = session.query(UserOAuthAccountModel).delete()
            print(f"  Deleted {deleted} OAuth accounts")

            # 2. Delete API keys (depends on users)
            deleted = session.query(APIKeyModel).delete()
            print(f"  Deleted {deleted} API keys")

            # 3. Delete ReBAC tuples (via raw SQL since we don't have the model)
            result = session.execute(text("DELETE FROM rebac_tuples"))
            print(f"  Deleted {result.rowcount} ReBAC tuples")

            # 4. Delete entity registry entries
            result = session.execute(text("DELETE FROM entity_registry"))
            print(f"  Deleted {result.rowcount} entity registry entries")

            # 5. Delete users
            deleted = session.query(UserModel).delete()
            print(f"  Deleted {deleted} users")

            # 6. Delete tenants
            deleted = session.query(TenantModel).delete()
            print(f"  Deleted {deleted} tenants")

            session.commit()
            print("\n✅ Database cleaned successfully!")

        except Exception as e:
            session.rollback()
            print(f"\n❌ Error during cleanup: {e}")
            raise


if __name__ == "__main__":
    cleanup_database()
