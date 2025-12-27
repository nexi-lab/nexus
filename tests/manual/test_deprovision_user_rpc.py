#!/usr/bin/env python3
"""
Integration test for deprovision_user via RPC API.

This script tests the deprovision_user RPC endpoint using the HTTP/RPC API,
consistent with other docker-integration tests.

Usage:
    python3 tests/manual/test_deprovision_user_rpc.py \
        --api-key YOUR_ADMIN_API_KEY \
        --base-url http://localhost:2026
"""

import argparse
import json
import sys
from typing import Any

import requests


def make_rpc_call(
    base_url: str,
    method: str,
    params: dict[str, Any],
    api_key: str | None = None,
) -> dict[str, Any]:
    """Make an RPC call to the Nexus server.

    Args:
        base_url: Base URL of the Nexus server
        method: RPC method name
        params: Method parameters
        api_key: Optional API key for authentication

    Returns:
        Response dictionary

    Raises:
        RuntimeError: If the RPC call fails
    """
    url = f"{base_url}/api/nfs/{method}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"params": params}

    print(f"→ RPC: {method}")
    print(f"  Params: {json.dumps(params, indent=2)}")

    response = requests.post(url, json=payload, headers=headers, timeout=120)

    if response.status_code != 200:
        print(f"✗ HTTP {response.status_code}: {response.text}")
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

    data = response.json()
    if "error" in data:
        error_msg = data["error"]
        print(f"✗ RPC Error: {error_msg}")
        raise RuntimeError(f"RPC Error: {error_msg}")

    print("✓ Success")
    return data.get("result", {})


def main() -> int:
    """Test deprovision_user RPC endpoint."""
    parser = argparse.ArgumentParser(description="Test deprovision_user via RPC")
    parser.add_argument(
        "--api-key",
        required=True,
        help="Admin API key",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:2026",
        help="Nexus server base URL (default: http://localhost:2026)",
    )
    args = parser.parse_args()

    print("=" * 80)
    print("Deprovision User Integration Test (RPC)")
    print("=" * 80)
    print()
    print(f"Server: {args.base_url}")
    print(f"API Key: {args.api_key[:20]}...")
    print()

    test_user_id = "test_deprovision_user"
    test_email = "test_deprovision@example.com"
    test_tenant_id = "test_tenant"

    try:
        # Step 1: Provision user
        print("Step 1: Provisioning test user...")
        provision_result = make_rpc_call(
            args.base_url,
            "provision_user",
            {
                "user_id": test_user_id,
                "email": test_email,
                "display_name": "Test Deprovision User",
                "tenant_id": test_tenant_id,
                "create_api_key": True,
                "create_agents": False,  # Skip agents for faster test
                "import_skills": False,
            },
            args.api_key,
        )

        print("✓ User provisioned successfully!")
        print(f"  - user_id: {provision_result.get('user_id')}")
        print(f"  - tenant_id: {provision_result.get('tenant_id')}")
        api_key = provision_result.get("api_key")
        if api_key:
            print(f"  - api_key: {api_key[:30]}...")
        print(f"  - directories: {len(provision_result.get('directories', []))}")
        print()

        # Step 2: Deprovision user
        print("=" * 80)
        print("Step 2: Deprovisioning user...")
        print("=" * 80)
        print()

        deprovision_result = make_rpc_call(
            args.base_url,
            "deprovision_user",
            {
                "user_id": test_user_id,
                "tenant_id": test_tenant_id,
                "delete_user_record": True,
                "force": False,
            },
            args.api_key,
        )

        print("✓ Deprovision completed!")
        print()
        print("Results:")
        print(f"  - user_id: {deprovision_result['user_id']}")
        print(f"  - tenant_id: {deprovision_result['tenant_id']}")
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

        print("=" * 80)
        print("✓ TEST PASSED!")
        print("  deprovision_user RPC endpoint is working correctly")
        print("=" * 80)
        return 0

    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
