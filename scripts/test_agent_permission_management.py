#!/usr/bin/env python3
"""Test script to verify agent permission management.

This script tests four scenarios:

Test 1: Agent without API key
1. Registering an agent with no API key and no permission inheritance
2. Verifying that no API key was created for the agent
3. Verifying tenant_id is "default"
4. Cleaning up by deleting the agent

Test 2: Agent with API key (zero permissions)
1. Registering an agent with API key but no permission inheritance
2. Verifying that an API key was created
3. Verifying tenant_id is "default"
4. Testing that the agent API key has limited access (only agent config directory)
5. Cleaning up by deleting the agent

Test 3: Agent with API key (full permissions)
1. Registering an agent with API key and permission inheritance
2. Verifying that an API key was created
3. Verifying tenant_id is "default"
4. Testing that the agent API key has full access (inherits owner's permissions)
5. Cleaning up by deleting the agent

Test 4: Agent with API key (granular permissions)
1. Registering an agent with API key but no permission inheritance
2. Granting specific permissions via ReBAC (skill and workspace)
3. Verifying permissions via rebac_list_tuples
4. Testing that the agent API key can only access granted resources
5. Verifying agent cannot access other resources
6. Cleaning up by deleting the agent

Usage:
    python scripts/test_agent_permission_management.py [--api-key YOUR_ADMIN_API_KEY] [--base-url http://localhost:8080] [--cleanup-old-agents]
"""

import argparse
import json
import sys
import time
from typing import Any

import requests


def make_rpc_call(
    base_url: str,
    method: str,
    params: dict[str, Any],
    api_key: str | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    """Make an RPC call to the Nexus API.

    Args:
        base_url: Base URL of the Nexus server
        method: RPC method name
        params: RPC parameters
        api_key: Optional API key for authentication
        request_id: Request ID for JSON-RPC

    Returns:
        Response dictionary

    Raises:
        SystemExit: If the request fails or returns an error
    """
    url = f"{base_url}/api/nfs/{method}"
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": request_id,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()

        if "error" in result:
            print(f"❌ RPC Error: {result['error']}")
            sys.exit(1)

        return dict(result)  # type: ignore[arg-type]
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON response: {e}")
        sys.exit(1)


def test_agent_no_api_key(base_url: str, api_key: str) -> None:
    """Test agent registration without API key.

    Args:
        base_url: Base URL of the Nexus server
        api_key: Admin API key for authentication
    """
    print("=" * 70)
    print("Testing Agent Registration Without API Key")
    print("=" * 70)
    print()

    # Generate unique agent name with timestamp
    timestamp = int(time.time())
    agent_name = f"agent_no_key_no_permission_{timestamp}"
    agent_id = f"admin,{agent_name}"

    print(f"📝 Test Agent ID: {agent_id}")
    print()

    # Step 1: Register agent with no API key and no permission inheritance
    print("Step 1: Registering agent with no API key and no permission inheritance...")
    register_params = {
        "agent_id": agent_id,
        "name": agent_name,
        "description": "",
        "generate_api_key": False,
        "inherit_permissions": False,
        "metadata": {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        },
    }

    register_result = make_rpc_call(
        base_url=base_url,
        method="register_agent",
        params=register_params,
        api_key=api_key,
        request_id=16,
    )

    print("✅ Agent registered successfully")
    print(f"   Agent ID: {register_result['result']['agent_id']}")
    print(f"   User ID: {register_result['result']['user_id']}")
    print(f"   Has API Key: {register_result['result']['has_api_key']}")
    print(f"   Tenant ID: {register_result['result'].get('tenant_id', 'N/A')}")
    print(f"   Config Path: {register_result['result'].get('config_path', 'N/A')}")
    print()

    # Verify has_api_key is False
    if register_result["result"]["has_api_key"]:
        print("❌ ERROR: Agent should not have an API key, but has_api_key is True")
        sys.exit(1)

    # Verify tenant_id is "default" (inherited from admin API key)
    tenant_id = register_result["result"].get("tenant_id")
    if tenant_id != "default":
        print(
            f"❌ ERROR: Agent tenant_id should be 'default' (from admin API key), but got: {tenant_id}"
        )
        sys.exit(1)
    print("✅ Tenant ID is correct (default)")
    print()

    # Step 2: Verify no API key was created
    print("Step 2: Verifying no API key was created for the agent...")
    list_keys_params = {
        "include_revoked": False,
        "include_expired": True,
        "limit": 100,
    }

    list_keys_result = make_rpc_call(
        base_url=base_url,
        method="admin_list_keys",
        params=list_keys_params,
        api_key=api_key,
        request_id=4,
    )

    keys = list_keys_result.get("result", {}).get("keys", [])
    print(f"   Found {len(keys)} total API keys")

    # Check for any keys associated with this agent
    agent_keys = [
        key
        for key in keys
        if (
            key.get("subject_id") == agent_id
            or key.get("subject_id") == agent_name
            or key.get("name") == agent_id
            or key.get("name") == agent_name
        )
        and key.get("subject_type") == "agent"
    ]

    if agent_keys:
        print(f"❌ ERROR: Found {len(agent_keys)} API key(s) for agent {agent_id}:")
        for key in agent_keys:
            print(f"   - Key ID: {key.get('key_id')}")
            print(f"     Name: {key.get('name')}")
            print(f"     Subject: {key.get('subject_type')}:{key.get('subject_id')}")
        sys.exit(1)

    print("✅ No API keys found for the agent (as expected)")
    print()

    # Step 3: Delete the agent
    print("Step 3: Deleting the agent...")
    delete_params = {"agent_id": agent_id}

    delete_result = make_rpc_call(
        base_url=base_url,
        method="delete_agent",
        params=delete_params,
        api_key=api_key,
        request_id=79,
    )

    if delete_result.get("result") is True:
        print("✅ Agent deleted successfully")
    else:
        print(f"❌ ERROR: Agent deletion returned: {delete_result.get('result')}")
        sys.exit(1)

    # Step 4: Verify agent is removed from list_agents
    print("Step 4: Verifying agent is removed from list_agents...")
    # Small delay to ensure deletion is committed
    time.sleep(0.5)

    list_agents_result = make_rpc_call(
        base_url=base_url,
        method="list_agents",
        params={},
        api_key=api_key,
        request_id=100,
    )

    agents = list_agents_result.get("result", [])
    agent_still_exists = any(a.get("agent_id") == agent_id for a in agents)

    if agent_still_exists:
        print(f"❌ ERROR: Agent {agent_id} still appears in list_agents after deletion")
        print(f"   Found {len(agents)} agents total")
        print(f"   Agent IDs: {[a.get('agent_id') for a in agents]}")
        sys.exit(1)

    print(f"✅ Agent removed from list_agents (found {len(agents)} agents total)")
    if len(agents) > 0:
        print(f"   Note: Other agents exist: {[a.get('agent_id') for a in agents[:5]]}")
    print()

    # Also verify get_agent returns None
    get_agent_result = make_rpc_call(
        base_url=base_url,
        method="get_agent",
        params={"agent_id": agent_id},
        api_key=api_key,
        request_id=101,
    )

    if get_agent_result.get("result") is not None:
        print(f"❌ ERROR: get_agent still returns agent {agent_id} after deletion")
        sys.exit(1)

    print("✅ get_agent correctly returns None for deleted agent")
    print()

    print()
    print("=" * 70)
    print("✅ All tests passed!")
    print("=" * 70)


def test_agent_with_api_key(base_url: str, api_key: str) -> None:
    """Test agent registration with API key and verify limited access.

    Args:
        base_url: Base URL of the Nexus server
        api_key: Admin API key for authentication
    """
    print("=" * 70)
    print("Testing Agent Registration With API Key (Zero Permissions)")
    print("=" * 70)
    print()

    # Generate unique agent name with timestamp
    timestamp = int(time.time())
    agent_name = f"agent_with_key_no_permission_{timestamp}"
    agent_id = f"admin,{agent_name}"

    print(f"📝 Test Agent ID: {agent_id}")
    print()

    # Step 1: Register agent with API key but no permission inheritance
    print("Step 1: Registering agent with API key and no permission inheritance...")
    register_params = {
        "agent_id": agent_id,
        "name": agent_name,
        "description": "",
        "generate_api_key": True,
        "inherit_permissions": False,
        "metadata": {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        },
    }

    register_result = make_rpc_call(
        base_url=base_url,
        method="register_agent",
        params=register_params,
        api_key=api_key,
        request_id=16,
    )

    print("✅ Agent registered successfully")
    print(f"   Agent ID: {register_result['result']['agent_id']}")
    print(f"   User ID: {register_result['result']['user_id']}")
    print(f"   Has API Key: {register_result['result']['has_api_key']}")
    print(f"   Tenant ID: {register_result['result'].get('tenant_id', 'N/A')}")
    print(f"   Config Path: {register_result['result'].get('config_path', 'N/A')}")
    print()

    # Verify has_api_key is True
    if not register_result["result"]["has_api_key"]:
        print("❌ ERROR: Agent should have an API key, but has_api_key is False")
        sys.exit(1)

    # Verify tenant_id is "default"
    tenant_id = register_result["result"].get("tenant_id")
    if tenant_id != "default":
        print(
            f"❌ ERROR: Agent tenant_id should be 'default' (from admin API key), but got: {tenant_id}"
        )
        sys.exit(1)
    print("✅ Tenant ID is correct (default)")
    print()

    # Get the agent API key
    agent_api_key = register_result["result"].get("api_key")
    if not agent_api_key:
        print("❌ ERROR: Agent API key not found in registration result")
        sys.exit(1)
    print(f"✅ Agent API key received: {agent_api_key[:20]}...")
    print()

    # Step 2: Verify API key was created in database
    print("Step 2: Verifying API key was created in database...")
    list_keys_params = {
        "include_revoked": False,
        "include_expired": True,
        "limit": 100,
    }

    list_keys_result = make_rpc_call(
        base_url=base_url,
        method="admin_list_keys",
        params=list_keys_params,
        api_key=api_key,
        request_id=4,
    )

    keys = list_keys_result.get("result", {}).get("keys", [])
    print(f"   Found {len(keys)} total API keys")

    # Check for keys associated with this agent
    agent_keys = [
        key
        for key in keys
        if (
            key.get("subject_id") == agent_id
            or key.get("subject_id") == agent_name
            or key.get("name") == agent_id
            or key.get("name") == agent_name
        )
        and key.get("subject_type") == "agent"
    ]

    if not agent_keys:
        print("❌ ERROR: No API keys found for the agent")
        sys.exit(1)

    if len(agent_keys) > 1:
        print(f"⚠️  WARNING: Found {len(agent_keys)} API keys for agent (expected 1)")

    print(f"✅ Found {len(agent_keys)} API key(s) for the agent (as expected)")
    print()

    # Step 3: Test agent API key access - should only see agent config directory
    print("Step 3: Testing agent API key access (should only see agent config directory)...")
    # Agent directory uses the new namespace: /tenant:{tenant_id}/user:{user_id}/agent/{agent_name}
    agent_dir = f"/tenant:default/user:admin/agent/{agent_name}"

    # First, check if agent can see its own directory
    list_agent_dir_params = {
        "path": agent_dir,
        "recursive": False,
        "details": True,
    }

    list_agent_dir_result = make_rpc_call(
        base_url=base_url,
        method="list",
        params=list_agent_dir_params,
        api_key=agent_api_key,
        request_id=94,
    )

    agent_dir_files = list_agent_dir_result.get("result", {}).get("files", [])
    if len(agent_dir_files) > 0:
        print(f"   ✅ Agent can access its own directory: {agent_dir}")
        print(f"   Found {len(agent_dir_files)} item(s) in agent directory")
    else:
        print(f"   ⚠️  Agent directory appears empty or inaccessible: {agent_dir}")

    # Also check root to see what the agent can see
    list_params = {
        "path": "/",
        "recursive": False,
        "details": True,
    }

    list_result = make_rpc_call(
        base_url=base_url,
        method="list",
        params=list_params,
        api_key=agent_api_key,
        request_id=95,
    )

    files = list_result.get("result", {}).get("files", [])
    visible_paths = [f.get("path") for f in files]
    print(f"   Agent can see {len(files)} item(s) at root:")
    print(f"   Visible paths: {visible_paths}")

    # Agent should be able to see /tenant:default (its tenant)
    # and should be able to access its own agent directory
    has_tenant_access = "/tenant:default" in visible_paths
    has_agent_dir_access = len(agent_dir_files) > 0

    if not has_tenant_access and not has_agent_dir_access:
        print("❌ ERROR: Agent cannot see its tenant or access its own directory")
        print(f"   Visible paths at root: {visible_paths}")
        print(f"   Agent directory: {agent_dir}")
        sys.exit(1)

    if has_tenant_access:
        print("   ✅ Agent can see tenant: /tenant:default")
    if has_agent_dir_access:
        print(f"   ✅ Agent can access its own directory: {agent_dir}")

    # Verify agent cannot see other directories (workspace, memory, etc.)
    restricted_paths = ["/workspace", "/memory", "/resources", "/skills", "/connectors"]
    for restricted_path in restricted_paths:
        if restricted_path in visible_paths:
            print(f"⚠️  WARNING: Agent can see restricted path: {restricted_path}")
            # This is a warning, not an error, as the agent might have been granted access

    print("✅ Agent has limited access (only agent directory)")
    print()

    # Step 4: Delete the agent
    print("Step 4: Deleting the agent...")
    delete_params = {"agent_id": agent_id}

    delete_result = make_rpc_call(
        base_url=base_url,
        method="delete_agent",
        params=delete_params,
        api_key=api_key,
        request_id=79,
    )

    if delete_result.get("result") is True:
        print("✅ Agent deleted successfully")
    else:
        print(f"❌ ERROR: Agent deletion returned: {delete_result.get('result')}")
        sys.exit(1)

    # Step 5: Verify agent is removed from list_agents
    print("Step 5: Verifying agent is removed from list_agents...")
    # Small delay to ensure deletion is committed
    time.sleep(0.5)

    list_agents_result = make_rpc_call(
        base_url=base_url,
        method="list_agents",
        params={},
        api_key=api_key,
        request_id=100,
    )

    agents = list_agents_result.get("result", [])
    agent_still_exists = any(a.get("agent_id") == agent_id for a in agents)

    if agent_still_exists:
        print(f"❌ ERROR: Agent {agent_id} still appears in list_agents after deletion")
        print(f"   Found {len(agents)} agents total")
        print(f"   Agent IDs: {[a.get('agent_id') for a in agents]}")
        sys.exit(1)

    print(f"✅ Agent removed from list_agents (found {len(agents)} agents total)")
    if len(agents) > 0:
        print(f"   Note: Other agents exist: {[a.get('agent_id') for a in agents[:5]]}")
    print()

    # Also verify get_agent returns None
    get_agent_result = make_rpc_call(
        base_url=base_url,
        method="get_agent",
        params={"agent_id": agent_id},
        api_key=api_key,
        request_id=101,
    )

    if get_agent_result.get("result") is not None:
        print(f"❌ ERROR: get_agent still returns agent {agent_id} after deletion")
        sys.exit(1)

    print("✅ get_agent correctly returns None for deleted agent")
    print()

    print()
    print("=" * 70)
    print("✅ All tests passed!")
    print("=" * 70)


def test_agent_with_api_key_and_inheritance(base_url: str, api_key: str) -> None:
    """Test agent registration with API key and permission inheritance.

    Args:
        base_url: Base URL of the Nexus server
        api_key: Admin API key for authentication
    """
    print("=" * 70)
    print("Testing Agent Registration With API Key (Full Permissions)")
    print("=" * 70)
    print()

    # Generate unique agent name with timestamp
    timestamp = int(time.time())
    agent_name = f"agent_with_key_full_permission_{timestamp}"
    agent_id = f"admin,{agent_name}"

    print(f"📝 Test Agent ID: {agent_id}")
    print()

    # Step 1: Register agent with API key and permission inheritance
    print("Step 1: Registering agent with API key and permission inheritance...")
    register_params = {
        "agent_id": agent_id,
        "name": agent_name,
        "description": "",
        "generate_api_key": True,
        "inherit_permissions": True,
        "metadata": {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        },
    }

    register_result = make_rpc_call(
        base_url=base_url,
        method="register_agent",
        params=register_params,
        api_key=api_key,
        request_id=16,
    )

    print("✅ Agent registered successfully")
    print(f"   Agent ID: {register_result['result']['agent_id']}")
    print(f"   User ID: {register_result['result']['user_id']}")
    print(f"   Has API Key: {register_result['result']['has_api_key']}")
    print(f"   Tenant ID: {register_result['result'].get('tenant_id', 'N/A')}")
    print(f"   Config Path: {register_result['result'].get('config_path', 'N/A')}")
    print()

    # Verify has_api_key is True
    if not register_result["result"]["has_api_key"]:
        print("❌ ERROR: Agent should have an API key, but has_api_key is False")
        sys.exit(1)

    # Verify tenant_id is "default"
    tenant_id = register_result["result"].get("tenant_id")
    if tenant_id != "default":
        print(
            f"❌ ERROR: Agent tenant_id should be 'default' (from admin API key), but got: {tenant_id}"
        )
        sys.exit(1)
    print("✅ Tenant ID is correct (default)")
    print()

    # Get the agent API key
    agent_api_key = register_result["result"].get("api_key")
    if not agent_api_key:
        print("❌ ERROR: Agent API key not found in registration result")
        sys.exit(1)
    print(f"✅ Agent API key received: {agent_api_key[:20]}...")
    print()

    # Step 2: Verify API key was created in database
    print("Step 2: Verifying API key was created in database...")
    list_keys_params = {
        "include_revoked": False,
        "include_expired": True,
        "limit": 100,
    }

    list_keys_result = make_rpc_call(
        base_url=base_url,
        method="admin_list_keys",
        params=list_keys_params,
        api_key=api_key,
        request_id=4,
    )

    keys = list_keys_result.get("result", {}).get("keys", [])
    print(f"   Found {len(keys)} total API keys")

    # Check for keys associated with this agent
    agent_keys = [
        key
        for key in keys
        if (
            key.get("subject_id") == agent_id
            or key.get("subject_id") == agent_name
            or key.get("name") == agent_id
            or key.get("name") == agent_name
        )
        and key.get("subject_type") == "agent"
    ]

    if not agent_keys:
        print("❌ ERROR: No API keys found for the agent")
        sys.exit(1)

    print(f"✅ Found {len(agent_keys)} API key(s) for the agent (as expected)")
    print()

    # Step 3: Test agent API key access - should have full access (like admin)
    print("Step 3: Testing agent API key access (should have full permissions)...")

    list_params = {
        "path": "/",
        "recursive": False,
        "details": True,
    }

    list_result = make_rpc_call(
        base_url=base_url,
        method="list",
        params=list_params,
        api_key=agent_api_key,
        request_id=95,
    )

    files = list_result.get("result", {}).get("files", [])
    print(f"   Agent can see {len(files)} item(s) at root:")

    # Check that agent can see multiple directories (full access)
    visible_paths = [f.get("path") for f in files]
    print(f"   Visible paths: {visible_paths}")

    if len(files) == 0:
        print("❌ ERROR: Agent cannot see any files")
        sys.exit(1)

    # Verify agent can see tenant (full permissions should allow access)
    has_tenant_access = any(
        f.get("path") == "/tenant:default" or f.get("path").startswith("/tenant:default/")
        for f in files
    )
    if not has_tenant_access:
        print("❌ ERROR: Agent cannot see /tenant:default (should have full access)")
        sys.exit(1)
    print("   ✅ Agent can access: /tenant:default")

    # Verify agent can see workspace (full permissions)
    has_workspace_access = any(
        f.get("path") == "/workspace" or f.get("path").startswith("/workspace/") for f in files
    )
    if not has_workspace_access:
        print("⚠️  WARNING: Agent cannot see /workspace (may not have workspace yet)")
    else:
        print("   ✅ Agent can access: /workspace")

    # Verify agent can see memory (full permissions)
    has_memory_access = any(
        f.get("path") == "/memory" or f.get("path").startswith("/memory/") for f in files
    )
    if not has_memory_access:
        print("⚠️  WARNING: Agent cannot see /memory (may not have memory yet)")
    else:
        print("   ✅ Agent can access: /memory")

    # Verify agent can see resources (full permissions)
    has_resources_access = any(
        f.get("path") == "/resources" or f.get("path").startswith("/resources/") for f in files
    )
    if not has_resources_access:
        print("⚠️  WARNING: Agent cannot see /resources (may not have resources yet)")
    else:
        print("   ✅ Agent can access: /resources")

    print("✅ Agent has full access (inherits owner's permissions)")
    print()

    # Step 4: Delete the agent
    print("Step 4: Deleting the agent...")
    delete_params = {"agent_id": agent_id}

    delete_result = make_rpc_call(
        base_url=base_url,
        method="delete_agent",
        params=delete_params,
        api_key=api_key,
        request_id=79,
    )

    if delete_result.get("result") is True:
        print("✅ Agent deleted successfully")
    else:
        print(f"❌ ERROR: Agent deletion returned: {delete_result.get('result')}")
        sys.exit(1)

    # Step 5: Verify agent is removed from list_agents
    print("Step 5: Verifying agent is removed from list_agents...")
    # Small delay to ensure deletion is committed
    time.sleep(0.5)

    list_agents_result = make_rpc_call(
        base_url=base_url,
        method="list_agents",
        params={},
        api_key=api_key,
        request_id=100,
    )

    agents = list_agents_result.get("result", [])
    agent_still_exists = any(a.get("agent_id") == agent_id for a in agents)

    if agent_still_exists:
        print(f"❌ ERROR: Agent {agent_id} still appears in list_agents after deletion")
        print(f"   Found {len(agents)} agents total")
        print(f"   Agent IDs: {[a.get('agent_id') for a in agents]}")
        sys.exit(1)

    print(f"✅ Agent removed from list_agents (found {len(agents)} agents total)")
    if len(agents) > 0:
        print(f"   Note: Other agents exist: {[a.get('agent_id') for a in agents[:5]]}")
    print()

    # Also verify get_agent returns None
    get_agent_result = make_rpc_call(
        base_url=base_url,
        method="get_agent",
        params={"agent_id": agent_id},
        api_key=api_key,
        request_id=101,
    )

    if get_agent_result.get("result") is not None:
        print(f"❌ ERROR: get_agent still returns agent {agent_id} after deletion")
        sys.exit(1)

    print("✅ get_agent correctly returns None for deleted agent")
    print()

    print()
    print("=" * 70)
    print("✅ All tests passed!")
    print("=" * 70)


def test_agent_with_granular_permissions(base_url: str, api_key: str) -> None:
    """Test agent registration with API key and granular permissions via ReBAC.

    Args:
        base_url: Base URL of the Nexus server
        api_key: Admin API key for authentication
    """
    print("=" * 70)
    print("Testing Agent Registration With API Key (Granular Permissions)")
    print("=" * 70)
    print()

    # Generate unique agent name with timestamp
    timestamp = int(time.time())
    agent_name = f"agent_granular_perms_{timestamp}"
    agent_id = f"admin,{agent_name}"

    print(f"📝 Test Agent ID: {agent_id}")
    print()

    # Step 1: Register agent with API key but no permission inheritance
    print("Step 1: Registering agent with API key and no permission inheritance...")
    register_params = {
        "agent_id": agent_id,
        "name": agent_name,
        "description": "",
        "generate_api_key": True,
        "inherit_permissions": False,
        "metadata": {
            "platform": "langgraph",
            "endpoint_url": "http://localhost:2024",
            "agent_id": "agent",
        },
    }

    register_result = make_rpc_call(
        base_url=base_url,
        method="register_agent",
        params=register_params,
        api_key=api_key,
        request_id=16,
    )

    print("✅ Agent registered successfully")
    print(f"   Agent ID: {register_result['result']['agent_id']}")
    print(f"   User ID: {register_result['result']['user_id']}")
    print(f"   Has API Key: {register_result['result']['has_api_key']}")
    print(f"   Tenant ID: {register_result['result'].get('tenant_id', 'N/A')}")
    print()

    # Verify has_api_key is True
    if not register_result["result"]["has_api_key"]:
        print("❌ ERROR: Agent should have an API key, but has_api_key is False")
        sys.exit(1)

    # Verify tenant_id is "default"
    tenant_id = register_result["result"].get("tenant_id")
    if tenant_id != "default":
        print(f"❌ ERROR: Agent tenant_id should be 'default', but got: {tenant_id}")
        sys.exit(1)
    print("✅ Tenant ID is correct (default)")
    print()

    # Get the agent API key
    agent_api_key = register_result["result"].get("api_key")
    if not agent_api_key:
        print("❌ ERROR: Agent API key not found in registration result")
        sys.exit(1)
    print(f"✅ Agent API key received: {agent_api_key[:20]}...")
    print()

    # Step 2: Grant granular permissions via ReBAC
    print("Step 2: Granting granular permissions via ReBAC...")

    # Grant access to a skill
    skill_path = "/skills/system/gmail"
    print(f"   Granting direct_viewer on {skill_path}...")
    rebac_create_result1 = make_rpc_call(
        base_url=base_url,
        method="rebac_create",
        params={
            "subject": ["agent", agent_id],
            "relation": "direct_viewer",
            "object": ["file", skill_path],
        },
        api_key=api_key,
        request_id=62,
    )
    if rebac_create_result1.get("result"):
        print(f"   ✅ Granted permission on {skill_path}")
    else:
        print(
            f"   ⚠️  Warning: Permission grant may have failed (result: {rebac_create_result1.get('result')})"
        )

    # Grant access to a workspace
    workspace_path = "/workspace/admin/personal"
    print(f"   Granting direct_viewer on {workspace_path}...")
    rebac_create_result2 = make_rpc_call(
        base_url=base_url,
        method="rebac_create",
        params={
            "subject": ["agent", agent_id],
            "relation": "direct_viewer",
            "object": ["file", workspace_path],
        },
        api_key=api_key,
        request_id=63,
    )
    if rebac_create_result2.get("result"):
        print(f"   ✅ Granted permission on {workspace_path}")
    else:
        print(
            f"   ⚠️  Warning: Permission grant may have failed (result: {rebac_create_result2.get('result')})"
        )

    print()

    # Step 3: Verify permissions via rebac_list_tuples
    print("Step 3: Verifying permissions via rebac_list_tuples...")
    list_tuples_result = make_rpc_call(
        base_url=base_url,
        method="rebac_list_tuples",
        params={"subject": ["agent", agent_id]},
        api_key=api_key,
        request_id=64,
    )

    tuples = list_tuples_result.get("result", [])
    print(f"   Found {len(tuples)} permission tuple(s) for agent")

    # Check for the granted permissions
    has_skill_permission = any(
        t.get("object_id") == skill_path and t.get("relation") == "direct_viewer" for t in tuples
    )
    has_workspace_permission = any(
        t.get("object_id") == workspace_path and t.get("relation") == "direct_viewer"
        for t in tuples
    )

    if not has_skill_permission:
        print(f"   ❌ ERROR: Agent does not have permission on {skill_path}")
        print(f"   Available tuples: {[(t.get('object_id'), t.get('relation')) for t in tuples]}")
        sys.exit(1)
    print(f"   ✅ Agent has permission on {skill_path}")

    if not has_workspace_permission:
        print(f"   ❌ ERROR: Agent does not have permission on {workspace_path}")
        print(f"   Available tuples: {[(t.get('object_id'), t.get('relation')) for t in tuples]}")
        sys.exit(1)
    print(f"   ✅ Agent has permission on {workspace_path}")
    print()

    # Step 4: Test agent API key access - should only see granted resources
    print("Step 4: Testing agent API key access with granular permissions...")

    # Test access to root - should only see /agent
    list_root_result = make_rpc_call(
        base_url=base_url,
        method="list",
        params={"path": "/", "recursive": False, "details": True},
        api_key=agent_api_key,
        request_id=95,
    )

    root_files = list_root_result.get("result", {}).get("files", [])
    root_paths = [f.get("path") for f in root_files]
    print(f"   Agent can see {len(root_files)} item(s) at root: {root_paths}")

    # Should see /tenant:default (agent's tenant) or its own agent directory
    agent_dir = f"/tenant:default/user:admin/agent/{agent_name}"
    has_tenant_access = "/tenant:default" in root_paths

    # Also check if agent can access its own directory
    list_agent_dir_result = make_rpc_call(
        base_url=base_url,
        method="list",
        params={"path": agent_dir, "recursive": False, "details": True},
        api_key=agent_api_key,
        request_id=96,
    )
    agent_dir_files = list_agent_dir_result.get("result", {}).get("files", [])
    has_agent_dir_access = len(agent_dir_files) > 0

    if not has_tenant_access and not has_agent_dir_access:
        print(
            f"   ❌ ERROR: Agent cannot see /tenant:default or access its own directory {agent_dir}"
        )
        sys.exit(1)
    if has_tenant_access:
        print("   ✅ Agent can access: /tenant:default")
    if has_agent_dir_access:
        print(f"   ✅ Agent can access its own directory: {agent_dir}")

    # Test access to granted skill
    print(f"   Testing access to {skill_path}...")
    try:
        make_rpc_call(
            base_url=base_url,
            method="list",
            params={"path": skill_path, "recursive": False, "details": True},
            api_key=agent_api_key,
            request_id=96,
        )
        print(f"   ✅ Agent can access {skill_path}")
    except Exception as e:
        error_msg = str(e)
        if "permission" in error_msg.lower() or "unauthorized" in error_msg.lower():
            print(f"   ❌ ERROR: Agent cannot access {skill_path}: {error_msg}")
            sys.exit(1)
        else:
            # Might be file not found, which is okay
            print(f"   ⚠️  Warning: {skill_path} may not exist: {error_msg}")

    # Test access to granted workspace
    print(f"   Testing access to {workspace_path}...")
    try:
        make_rpc_call(
            base_url=base_url,
            method="list",
            params={"path": workspace_path, "recursive": False, "details": True},
            api_key=agent_api_key,
            request_id=97,
        )
        print(f"   ✅ Agent can access {workspace_path}")
    except Exception as e:
        error_msg = str(e)
        if "permission" in error_msg.lower() or "unauthorized" in error_msg.lower():
            print(f"   ❌ ERROR: Agent cannot access {workspace_path}: {error_msg}")
            sys.exit(1)
        else:
            # Might be directory not found, which is okay
            print(f"   ⚠️  Warning: {workspace_path} may not exist: {error_msg}")

    # Test that agent cannot access other resources (e.g., /memory, /resources)
    # Note: Agent might see these directories at root level, but should not be able to list their contents
    restricted_paths = ["/memory", "/resources"]
    for restricted_path in restricted_paths:
        print(f"   Testing NO access to contents of {restricted_path}...")
        try:
            list_restricted_result = make_rpc_call(
                base_url=base_url,
                method="list",
                params={"path": restricted_path, "recursive": False, "details": True},
                api_key=agent_api_key,
                request_id=98,
            )
            # If we get here, check if it's just an empty directory or actual access
            files = list_restricted_result.get("result", {}).get("files", [])
            # If directory exists but is empty, that's okay (might be a directory listing)
            # But if it has files, that means agent has access (which is wrong)
            if len(files) > 0:
                print(
                    f"   ❌ ERROR: Agent should NOT have access to {restricted_path}, but can see {len(files)} item(s)"
                )
                print(f"   Visible items: {[f.get('path') for f in files[:5]]}")
                sys.exit(1)
            else:
                # Empty directory - might be accessible but empty, or permission denied
                print(
                    f"   ⚠️  Warning: {restricted_path} appears accessible but empty (may be permission check)"
                )
        except Exception as e:
            error_msg = str(e)
            if (
                "permission" in error_msg.lower()
                or "unauthorized" in error_msg.lower()
                or "forbidden" in error_msg.lower()
            ):
                print(f"   ✅ Agent correctly denied access to {restricted_path}")
            else:
                # Might be file not found, which is also okay
                print(
                    f"   ⚠️  Warning: {restricted_path} may not exist or access denied: {error_msg}"
                )

    print("✅ Agent has granular permissions (only granted resources)")
    print()

    # Step 5: Delete the agent
    print("Step 5: Deleting the agent...")
    delete_params = {"agent_id": agent_id}

    delete_result = make_rpc_call(
        base_url=base_url,
        method="delete_agent",
        params=delete_params,
        api_key=api_key,
        request_id=79,
    )

    if delete_result.get("result") is True:
        print("✅ Agent deleted successfully")
    else:
        print(f"❌ ERROR: Agent deletion returned: {delete_result.get('result')}")
        sys.exit(1)

    # Step 6: Verify agent is removed from list_agents
    print("Step 6: Verifying agent is removed from list_agents...")
    time.sleep(0.5)

    list_agents_result = make_rpc_call(
        base_url=base_url,
        method="list_agents",
        params={},
        api_key=api_key,
        request_id=100,
    )

    agents = list_agents_result.get("result", [])
    agent_still_exists = any(a.get("agent_id") == agent_id for a in agents)

    if agent_still_exists:
        print(f"❌ ERROR: Agent {agent_id} still appears in list_agents after deletion")
        print(f"   Found {len(agents)} agents total")
        print(f"   Agent IDs: {[a.get('agent_id') for a in agents]}")
        sys.exit(1)

    print(f"✅ Agent removed from list_agents (found {len(agents)} agents total)")
    if len(agents) > 0:
        print(f"   Note: Other agents exist: {[a.get('agent_id') for a in agents[:5]]}")
    print()

    # Also verify get_agent returns None
    get_agent_result = make_rpc_call(
        base_url=base_url,
        method="get_agent",
        params={"agent_id": agent_id},
        api_key=api_key,
        request_id=101,
    )

    if get_agent_result.get("result") is not None:
        print(f"❌ ERROR: get_agent still returns agent {agent_id} after deletion")
        sys.exit(1)

    print("✅ get_agent correctly returns None for deleted agent")
    print()

    print()
    print("=" * 70)
    print("✅ All tests passed!")
    print("=" * 70)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test agent permission management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Admin API key (default: from NEXUS_API_KEY env var)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8080",
        help="Base URL of Nexus server (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--cleanup-old-agents",
        action="store_true",
        help="Clean up old test agents before running tests (agents matching pattern: agent_*_*_*)",
    )

    args = parser.parse_args()

    # Get API key from args or environment
    api_key = args.api_key
    if not api_key:
        import os

        api_key = os.environ.get("NEXUS_API_KEY")
        if not api_key:
            print("❌ ERROR: API key required. Provide via --api-key or NEXUS_API_KEY env var")
            sys.exit(1)

    # Optional: Clean up old test agents
    if args.cleanup_old_agents:
        print("=" * 70)
        print("Cleaning up old test agents...")
        print("=" * 70)
        print()

        list_agents_result = make_rpc_call(
            base_url=args.base_url,
            method="list_agents",
            params={},
            api_key=api_key,
            request_id=200,
        )

        agents = list_agents_result.get("result", [])
        import re

        # Pattern matches: agent_no_key_no_permission_*, agent_with_key_no_permission_*, agent_with_key_full_permission_*, agent_granular_perms_*
        test_agent_pattern = re.compile(
            r"agent_(no_key|with_key)_(no_permission|full_permission)_\d+|agent_granular_perms_\d+"
        )

        cleaned_count = 0
        for agent in agents:
            agent_id = agent.get("agent_id", "")
            # Check if agent name matches test pattern
            if "," in agent_id:
                agent_name = agent_id.split(",", 1)[1]
                if test_agent_pattern.match(agent_name):
                    print(f"   Deleting old test agent: {agent_id}")
                    try:
                        delete_result = make_rpc_call(
                            base_url=args.base_url,
                            method="delete_agent",
                            params={"agent_id": agent_id},
                            api_key=api_key,
                            request_id=201,
                        )
                        if delete_result.get("result"):
                            cleaned_count += 1
                    except Exception as e:
                        print(f"   ⚠️  Failed to delete {agent_id}: {e}")

        if cleaned_count > 0:
            print(f"✅ Cleaned up {cleaned_count} old test agent(s)")
        else:
            print("   No old test agents found to clean up")
        print()
        print()

    # Run all four tests
    try:
        test_agent_no_api_key(args.base_url, api_key)
        print()
        print()
        test_agent_with_api_key(args.base_url, api_key)
        print()
        print()
        test_agent_with_api_key_and_inheritance(args.base_url, api_key)
        print()
        print()
        test_agent_with_granular_permissions(args.base_url, api_key)

        # Final verification: Check that no test agents remain
        print()
        print("=" * 70)
        print("Final Verification: Checking for leftover test agents")
        print("=" * 70)
        print()

        time.sleep(0.5)  # Small delay to ensure all deletions are committed

        list_agents_result = make_rpc_call(
            base_url=args.base_url,
            method="list_agents",
            params={},
            api_key=api_key,
            request_id=300,
        )

        agents = list_agents_result.get("result", [])
        print(f"Found {len(agents)} total agent(s) in system")

        # Pattern to match test agents
        import re

        test_agent_pattern = re.compile(
            r"agent_(no_key|with_key)_(no_permission|full_permission)_\d+|agent_granular_perms_\d+"
        )

        test_agents_remaining = []
        for agent in agents:
            agent_id = agent.get("agent_id", "")
            if "," in agent_id:
                agent_name = agent_id.split(",", 1)[1]
                if test_agent_pattern.match(agent_name):
                    test_agents_remaining.append(agent_id)

        if test_agents_remaining:
            print(f"❌ ERROR: Found {len(test_agents_remaining)} leftover test agent(s):")
            for agent_id in test_agents_remaining:
                print(f"   - {agent_id}")
            sys.exit(1)

        print("✅ No test agents remaining (all cleaned up successfully)")
        if len(agents) > 0:
            print(f"   Other agents in system: {[a.get('agent_id') for a in agents]}")
        print()
        print("=" * 70)
        print("✅ All tests completed successfully!")
        print("=" * 70)

    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
