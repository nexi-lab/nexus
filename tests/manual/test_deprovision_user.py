#!/usr/bin/env python3
"""
Manual test for deprovision_user function.

This script tests the deprovision_user functionality with PostgreSQL database.
It provisions a test user, verifies the resources, then deprovisions them
and confirms complete cleanup.

Usage:
    # With default PostgreSQL:
    python3 tests/manual/test_deprovision_user.py

    # Custom database URL:
    python3 tests/manual/test_deprovision_user.py --db postgresql://user:pass@host:port/dbname
"""

import argparse
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


def main() -> None:
    """Test deprovision_user with configurable database backend."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Test deprovision_user functionality")
    parser.add_argument(
        "--db",
        default=os.getenv("NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus"),
        help="Database URL (default: from NEXUS_DATABASE_URL env or PostgreSQL)",
    )
    parser.add_argument(
        "--backend-path",
        default="./nexus-data-local",
        help="Path to backend storage (default: ./nexus-data-local)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("NEXUS_API_KEY"),
        help="Admin API key (default: from NEXUS_API_KEY env)",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("NEXUS_BASE_URL", "http://localhost:2026"),
        help="Nexus server base URL (default: from NEXUS_BASE_URL env or localhost:2026)",
    )
    args = parser.parse_args()

    # Use database URL from arguments
    db_path = args.db

    print("=" * 80)
    print("Deprovision User Test")
    print("=" * 80)
    print()
    print(f"Database: {db_path}")
    print(f"Backend path: {args.backend_path}")
    print()

    # Initialize NexusFS
    print("Initializing NexusFS...")
    # db_path accepts both PostgreSQL URLs and SQLite file paths
    record_store = SQLAlchemyRecordStore(db_path=db_path)
    nx = create_nexus_fs(
        backend=LocalBackend(args.backend_path),
        metadata_store=SQLAlchemyMetadataStore(db_path=db_path),
        record_store=record_store,
        auto_parse=False,
        enforce_permissions=True,
        allow_admin_bypass=True,
    )
    print("✓ NexusFS initialized")
    print()

    # Create admin context
    admin_context = OperationContext(
        user="admin",
        groups=[],
        zone_id="test_zone",
        is_admin=True,
    )

    # Test user details
    test_user_id = "test_deprovision_user"
    test_email = "test_deprovision@example.com"
    test_zone_id = "test_zone"

    try:
        # Step 1: Check if user already exists and clean up if needed
        print("Step 1: Checking for existing test user...")
        from nexus.storage.models import UserModel

        session = record_store.session_factory()
        try:
            existing_user = session.query(UserModel).filter_by(user_id=test_user_id).first()
            if existing_user:
                print("⚠️  User already exists, cleaning up first...")
                nx.deprovision_user(
                    user_id=test_user_id,
                    zone_id=test_zone_id,
                    delete_user_record=True,
                    force=False,
                    context=admin_context,
                )
                print("✓ Existing user cleaned up")
                print()
        finally:
            session.close()

        # Step 2: Provision a new test user
        print("Step 2: Provisioning test user...")
        provision_result = nx.provision_user(
            user_id=test_user_id,
            email=test_email,
            display_name="Test Deprovision User",
            zone_id=test_zone_id,
            create_api_key=True,
            create_agents=False,  # Skip agents for faster test
            import_skills=False,
            context=admin_context,
        )

        print("✓ User provisioned successfully!")
        print(f"  - user_id: {test_user_id}")
        print(f"  - zone_id: {test_zone_id}")
        if provision_result.get("api_key"):
            print(f"  - api_key: {provision_result['api_key'][:30]}...")
        print(f"  - directories: {len(provision_result.get('directories', []))}")
        print()

        # Step 3: Verify user resources
        print("Step 3: Verifying user resources...")
        user_base = f"/zone/{test_zone_id}/user:{test_user_id}"
        resource_types = ["workspace", "memory", "skill", "agent", "connector", "resource"]

        existing_resources = []
        for resource_type in resource_types:
            resource_path = f"{user_base}/{resource_type}"
            if nx.exists(resource_path, context=admin_context):
                existing_resources.append(resource_type)
                print(f"  ✓ {resource_type}: exists")

        print(f"\n  Total resources: {len(existing_resources)}")
        print()

        # Step 4: Verify API keys
        print("Step 4: Checking API keys...")
        from nexus.storage.models import APIKeyModel

        session = record_store.session_factory()
        try:
            keys = (
                session.query(APIKeyModel)
                .filter_by(user_id=test_user_id, subject_type="user", revoked=0)
                .all()
            )
            print(f"  Found {len(keys)} active API key(s)")
        finally:
            session.close()
        print()

        # Step 5: Deprovision the user
        print("=" * 80)
        print("Step 5: Deprovisioning user...")
        print("=" * 80)
        print()

        deprovision_result = nx.deprovision_user(
            user_id=test_user_id,
            zone_id=test_zone_id,
            delete_user_record=True,
            force=False,
            context=admin_context,
        )

        print("✓ Deprovision completed!")
        print()
        print("Results:")
        print(f"  - user_id: {deprovision_result['user_id']}")
        print(f"  - zone_id: {deprovision_result['zone_id']}")
        print(
            f"  - deleted_directories: {len(deprovision_result['deleted_directories'])} directories"
        )
        for dir_path in deprovision_result["deleted_directories"]:
            print(f"      • {dir_path}")
        print(f"  - deleted_api_keys: {deprovision_result['deleted_api_keys']} key(s)")
        print(f"  - deleted_permissions: {deprovision_result['deleted_permissions']} permission(s)")
        print(f"  - deleted_entities: {deprovision_result['deleted_entities']} entit(ies)")
        print(f"  - user_record_deleted: {deprovision_result['user_record_deleted']}")
        print()

        # Step 6: Verify user is soft-deleted
        print("Step 6: Verifying user soft-deletion...")
        session = record_store.session_factory()
        try:
            user = session.query(UserModel).filter_by(user_id=test_user_id).first()
            if user:
                print("✓ User record status:")
                print(f"  - is_active: {user.is_active} (should be 0)")
                print(f"  - deleted_at: {user.deleted_at} (should be set)")

                if user.is_active == 0 and user.deleted_at is not None:
                    print("  ✓ User is properly soft-deleted")
                else:
                    print("  ⚠️  User soft-delete may not be complete")
            else:
                print("  ✗ User record not found (hard deleted?)")
        finally:
            session.close()
        print()

        # Step 7: Verify API keys are revoked
        print("Step 7: Verifying API keys are revoked...")
        session = record_store.session_factory()
        try:
            active_keys = (
                session.query(APIKeyModel)
                .filter_by(user_id=test_user_id, subject_type="user", revoked=0)
                .count()
            )
            revoked_keys = (
                session.query(APIKeyModel)
                .filter_by(user_id=test_user_id, subject_type="user", revoked=1)
                .count()
            )
            print(f"  Active keys: {active_keys} (should be 0)")
            print(f"  Revoked keys: {revoked_keys}")

            if active_keys == 0:
                print("  ✓ All API keys revoked")
            else:
                print("  ⚠️  Some API keys still active")
        finally:
            session.close()
        print()

        # Step 8: Verify resources are deleted/empty
        print("Step 8: Verifying resources are deleted/empty...")
        all_empty = True
        for resource_type in resource_types:
            resource_path = f"{user_base}/{resource_type}"
            try:
                files = nx.list(resource_path, recursive=True, context=admin_context)
                if isinstance(files, list):
                    count = len(files)
                elif isinstance(files, dict):
                    count = len(files.get("files", []))
                else:
                    count = 0

                if count == 0:
                    print(f"  ✓ {resource_type}: empty")
                else:
                    print(f"  ⚠️  {resource_type}: still has {count} items")
                    all_empty = False
            except Exception:
                print(f"  ✓ {resource_type}: doesn't exist or empty")

        print()
        print("=" * 80)
        if all_empty and deprovision_result["user_record_deleted"]:
            print("✓ TEST PASSED!")
            print("  deprovision_user successfully removed all user data")
        else:
            print("⚠️  Test completed with warnings")
            if not all_empty:
                print("  Some resources may still exist")
            if not deprovision_result["user_record_deleted"]:
                print("  User record was not soft-deleted")
        print("=" * 80)

    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        nx.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✗ Test interrupted")
        sys.exit(1)
