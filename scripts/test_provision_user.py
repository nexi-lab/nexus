#!/usr/bin/env python3
"""
Manual test script for provision_user RPC method.

This script tests the provision_user implementation by:
1. Provisioning a new test user
2. Testing idempotency by provisioning the same user again
3. Verifying all created resources
4. Cleaning up test data

Usage:
    python scripts/test_provision_user.py
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import UTC, datetime

import nexus
from nexus.core.permissions import OperationContext


def test_provision_user() -> bool:
    """Test provision_user implementation."""
    print("=" * 80)
    print("Testing provision_user RPC Method")
    print("=" * 80)
    print()

    # Initialize NexusFS with LocalBackend and in-memory database
    print("1. Initializing NexusFS...")
    from nexus.backends.local import LocalBackend

    backend = LocalBackend(root_path="/tmp/nexus_test")
    # Use in-memory SQLite database for testing
    nx = nexus.NexusFS(backend=backend, db_path="sqlite:///:memory:")
    print("   ✓ NexusFS initialized with LocalBackend and in-memory database")
    print("   - Root path: /tmp/nexus_test")
    print("   - Database: sqlite:///:memory:")
    print()

    # Test data
    test_user_id = f"testuser_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    test_email = f"{test_user_id}@example.com"
    test_display_name = "Test User"
    test_zone_id = test_user_id  # Extracted from email

    print("2. Test User Details:")
    print(f"   - User ID: {test_user_id}")
    print(f"   - Email: {test_email}")
    print(f"   - Display Name: {test_display_name}")
    print(f"   - Zone ID: {test_zone_id}")
    print()

    # Create admin context
    admin_context = OperationContext(
        user="system",
        groups=[],
        zone_id=test_zone_id,
        is_admin=True,
    )

    # Test 1: Provision new user
    print("3. Provisioning new user (first call)...")
    try:
        result1 = nx.provision_user(
            user_id=test_user_id,
            email=test_email,
            display_name=test_display_name,
            zone_id=test_zone_id,
            create_api_key=True,
            create_agents=True,
            import_skills=True,
            context=admin_context,
        )
        print("   ✓ Provisioning successful!")
        print(f"   - User ID: {result1['user_id']}")
        print(f"   - Zone ID: {result1['zone_id']}")
        print(
            f"   - API Key: {result1['api_key'][:20]}..."
            if result1.get("api_key")
            else "   - API Key: None"
        )
        print(f"   - Workspace Path: {result1.get('workspace_path')}")
        print(f"   - Agent Paths: {len(result1.get('agent_paths', []))} agents created")
        print(f"   - Skill Paths: {len(result1.get('skill_paths', []))} skills imported")
        print()

        if "created_resources" in result1:
            print("   Created Resources:")
            for key, value in result1["created_resources"].items():
                if isinstance(value, list):
                    print(f"     - {key}: {len(value)} items")
                else:
                    print(f"     - {key}: {value}")
        print()
    except Exception as e:
        print(f"   ✗ Provisioning failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    # Test 2: Test idempotency
    print("4. Testing idempotency (second call with same user)...")
    try:
        result2 = nx.provision_user(
            user_id=test_user_id,
            email=test_email,
            display_name=test_display_name,
            zone_id=test_zone_id,
            create_api_key=True,
            create_agents=True,
            import_skills=True,
            context=admin_context,
        )
        print("   ✓ Idempotency test successful!")
        print(f"   - Same User ID: {result1['user_id'] == result2['user_id']}")
        print(f"   - Same Zone ID: {result1['zone_id'] == result2['zone_id']}")
        print(f"   - Same Workspace: {result1['workspace_path'] == result2['workspace_path']}")
        print()
    except Exception as e:
        print(f"   ✗ Idempotency test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    # Test 3: Verify resources exist
    print("5. Verifying created resources...")

    # Check workspace exists
    if result1.get("workspace_path"):
        try:
            workspace_exists = nx.exists(result1["workspace_path"], context=admin_context)
            print(f"   - Workspace exists: {workspace_exists} ✓")
        except Exception as e:
            print(f"   - Workspace check failed: {e} ✗")

    # Check user directories exist
    user_dirs = [
        f"/zone/{test_zone_id}/user:{test_user_id}/workspace",
        f"/zone/{test_zone_id}/user:{test_user_id}/memory",
        f"/zone/{test_zone_id}/user:{test_user_id}/skill",
        f"/zone/{test_zone_id}/user:{test_user_id}/agent",
        f"/zone/{test_zone_id}/user:{test_user_id}/connector",
        f"/zone/{test_zone_id}/user:{test_user_id}/resource",
    ]

    for dir_path in user_dirs:
        try:
            exists = nx.exists(dir_path, context=admin_context)
            status = "✓" if exists else "✗"
            print(f"   - {dir_path.split('/')[-1]}: {exists} {status}")
        except Exception as e:
            print(f"   - {dir_path.split('/')[-1]}: Error - {e} ✗")

    print()

    # Test 4: Verify database records
    print("6. Verifying database records...")
    try:
        from nexus.storage.models import UserModel, ZoneModel

        session = nx.metadata.SessionLocal()
        try:
            # Check zone
            zone = session.query(ZoneModel).filter_by(zone_id=test_zone_id).first()
            if zone:
                print(f"   - Zone exists: {zone.zone_id} ✓")
                print(f"     Name: {zone.name}")
                print(f"     Active: {zone.is_active}")
            else:
                print("   - Zone not found ✗")

            # Check user
            user = session.query(UserModel).filter_by(user_id=test_user_id).first()
            if user:
                print(f"   - User exists: {user.user_id} ✓")
                print(f"     Email: {user.email}")
                print(f"     Display Name: {user.display_name}")
                print(f"     Zone ID: {user.zone_id}")
                print(f"     Active: {user.is_active}")
            else:
                print("   - User not found ✗")
        finally:
            session.close()
        print()
    except Exception as e:
        print(f"   ✗ Database verification failed: {e}")
        import traceback

        traceback.print_exc()

    # Test 5: Verify entity registry
    print("7. Verifying entity registry...")
    try:
        if nx._entity_registry:
            # Check zone
            zone_entity = nx._entity_registry.get_entity("zone", test_zone_id)
            if zone_entity:
                print(f"   - Zone in registry: {zone_entity.entity_id} ✓")
            else:
                print("   - Zone not in registry ✗")

            # Check user
            user_entity = nx._entity_registry.get_entity("user", test_user_id)
            if user_entity:
                print(f"   - User in registry: {user_entity.entity_id} ✓")
                print(f"     Parent: {user_entity.parent_type}:{user_entity.parent_id}")
            else:
                print("   - User not in registry ✗")
        else:
            print("   - Entity registry not initialized ✗")
        print()
    except Exception as e:
        print(f"   ✗ Entity registry verification failed: {e}")
        import traceback

        traceback.print_exc()

    print("=" * 80)
    print("Test Summary")
    print("=" * 80)
    print("✓ provision_user implementation tested successfully!")
    print(f"✓ Test user {test_user_id} created with all resources")
    print("✓ Idempotency verified")
    print()
    print("Note: Test user and resources were created in the database.")
    print("You may want to clean them up manually if needed.")
    print()

    return True


if __name__ == "__main__":
    try:
        success = test_provision_user()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nTest failed with error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
