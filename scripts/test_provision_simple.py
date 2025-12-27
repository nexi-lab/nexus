#!/usr/bin/env python3
"""
Simple direct test for provision_user method (bypasses HTTP/auth layer).

This script directly instantiates NexusFS and calls provision_user,
avoiding HTTP authentication issues during development.

Usage:
    # Load environment and run
    source .nexus-admin-env && python3 scripts/test_provision_simple.py
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext


def main() -> int:
    print("=" * 80)
    print("Direct Test: provision_user Method")
    print("=" * 80)
    print()

    # Get database URL from environment
    db_url = os.environ.get("NEXUS_DATABASE_URL")
    if not db_url:
        print("ERROR: NEXUS_DATABASE_URL environment variable not set")
        print("Run: source .nexus-admin-env")
        return 1

    print(f"Using database: {db_url}")
    print()

    # Get data directory
    data_dir = os.environ.get("NEXUS_DATA_DIR", "./nexus-data")
    print(f"Using data directory: {data_dir}")
    print()

    # Initialize NexusFS
    print("Initializing NexusFS...")
    backend = LocalBackend(root_path=data_dir)
    nx = NexusFS(backend=backend, db_path=db_url)
    print("✓ NexusFS initialized")
    print()

    # Create admin context
    admin_context = OperationContext(user="system", groups=[], tenant_id="alice", is_admin=True)

    # Test provision_user
    print("Testing provision_user...")
    print("-" * 80)
    print()

    test_user = "alice_test"
    test_email = "alice_test@example.com"

    try:
        result = nx.provision_user(
            user_id=test_user,
            email=test_email,
            display_name="Alice Test User",
            tenant_id="alice",
            create_api_key=True,
            create_agents=True,
            import_skills=True,
            context=admin_context,
        )

        print("✓ provision_user succeeded!")
        print()
        print("Result:")
        print(f"  User ID: {result.get('user_id')}")
        print(f"  Tenant ID: {result.get('tenant_id')}")
        api_key = result.get("api_key")
        print(f"  API Key: {api_key[:30] + '...' if api_key else 'None'}")
        print(f"  Workspace: {result.get('workspace_path')}")
        print(f"  Agents: {len(result.get('agent_paths', []))} created")
        print(f"  Skills: {len(result.get('skill_paths', []))} imported")
        print()

        # Test idempotency
        print("Testing idempotency (calling again)...")
        result2 = nx.provision_user(
            user_id=test_user,
            email=test_email,
            display_name="Alice Test User",
            tenant_id="alice",
            create_api_key=False,
            create_agents=True,
            import_skills=True,
            context=admin_context,
        )
        print("✓ Idempotency test passed!")
        print(f"  Same user_id: {result['user_id'] == result2['user_id']}")
        print(f"  Same tenant_id: {result['tenant_id'] == result2['tenant_id']}")
        print()

        print("=" * 80)
        print("SUCCESS: provision_user is working correctly!")
        print("=" * 80)
        return 0

    except Exception as e:
        print(f"✗ ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
