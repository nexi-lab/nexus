#!/usr/bin/env python3
"""
Script to reproduce the database corruption crash by making concurrent API calls.

This simulates the sequence of API calls that the frontend makes, which triggers
the SQLite database corruption and segmentation fault.
"""

import asyncio
import aiohttp
import sys
from typing import List, Dict, Any

# Configuration
BASE_URL = "http://localhost:8080"
API_KEY = "sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


async def make_request(session: aiohttp.ClientSession, method: str, endpoint: str,
                      data: Dict[str, Any] = None, label: str = "") -> tuple:
    """Make an API request and return the response."""
    url = f"{BASE_URL}{endpoint}"
    try:
        if method == "GET":
            async with session.get(url, headers=HEADERS) as response:
                result = await response.json()
                print(f"✓ {label or endpoint} - Status: {response.status}")
                return response.status, result
        else:  # POST
            async with session.post(url, headers=HEADERS, json=data) as response:
                result = await response.json()
                print(f"✓ {label or endpoint} - Status: {response.status}")
                return response.status, result
    except Exception as e:
        print(f"✗ {label or endpoint} - Error: {e}")
        return None, None


async def wave_1_initial_loads(session: aiohttp.ClientSession):
    """Wave 1: Initial page load - concurrent list operations."""
    print("\n=== Wave 1: Initial page load (concurrent list operations) ===\n")

    tasks = [
        make_request(session, "POST", "/api/nfs/list_mounts",
                    {"jsonrpc": "2.0", "method": "list_mounts", "params": {}, "id": 1},
                    "list_mounts"),
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}, "id": 2},
                    "list /"),
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list", "params": {"path": "/tenant:default/"}, "id": 3},
                    "list /tenant:default/"),
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list", "params": {"path": "/tenant:default/user:admin/"}, "id": 4},
                    "list /tenant:default/user:admin/"),
    ]

    await asyncio.gather(*tasks)


async def wave_2_agents_and_permissions(session: aiohttp.ClientSession):
    """Wave 2: Load agents and permissions."""
    print("\n=== Wave 2: Load agents and permissions ===\n")

    tasks = [
        make_request(session, "POST", "/api/nfs/list_agents",
                    {"jsonrpc": "2.0", "method": "list_agents", "params": {}, "id": 5},
                    "list_agents"),
        make_request(session, "POST", "/api/nfs/rebac_list_tuples",
                    {"jsonrpc": "2.0", "method": "rebac_list_tuples",
                     "params": {"subject_type": "agent", "subject_id": "admin"}, "id": 6},
                    "rebac_list_tuples"),
        make_request(session, "POST", "/api/nfs/list_workspaces",
                    {"jsonrpc": "2.0", "method": "list_workspaces", "params": {}, "id": 7},
                    "list_workspaces"),
    ]

    await asyncio.gather(*tasks)


async def wave_3_skills_concurrent(session: aiohttp.ClientSession):
    """Wave 3: Concurrent skills_list calls - THIS IS WHERE THE CRASH HAPPENS."""
    print("\n=== Wave 3: CONCURRENT skills_list calls (CRASH TRIGGER) ===\n")

    # Make multiple concurrent skills_list calls
    # This triggers the SQLite database corruption
    tasks = [
        make_request(session, "POST", "/api/nfs/skills_list",
                    {"jsonrpc": "2.0", "method": "skills_list", "params": {}, "id": 10 + i},
                    f"skills_list #{i+1}")
        for i in range(5)  # 5 concurrent calls
    ]

    await asyncio.gather(*tasks)


async def wave_4_file_operations(session: aiohttp.ClientSession):
    """Wave 4: File operations and more list calls."""
    print("\n=== Wave 4: File operations and more lists ===\n")

    tasks = [
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list",
                     "params": {"path": "/tenant:default/user:admin/skill/"}, "id": 20},
                    "list /tenant:default/user:admin/skill/"),
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list",
                     "params": {"path": "/tenant:default/user:admin/agent/"}, "id": 21},
                    "list /tenant:default/user:admin/agent/"),
        make_request(session, "POST", "/api/nfs/list",
                    {"jsonrpc": "2.0", "method": "list",
                     "params": {"path": "/tenant:default/user:admin/workspace/"}, "id": 22},
                    "list /tenant:default/user:admin/workspace/"),
    ]

    await asyncio.gather(*tasks)


async def wave_5_more_skills(session: aiohttp.ClientSession):
    """Wave 5: More concurrent skills_list calls."""
    print("\n=== Wave 5: MORE concurrent skills_list calls ===\n")

    # More concurrent skills_list calls
    tasks = [
        make_request(session, "POST", "/api/nfs/skills_list",
                    {"jsonrpc": "2.0", "method": "skills_list", "params": {}, "id": 30 + i},
                    f"skills_list (wave 5) #{i+1}")
        for i in range(3)  # 3 concurrent calls
    ]

    await asyncio.gather(*tasks)


async def wave_6_sandbox_operations(session: aiohttp.ClientSession):
    """Wave 6: Sandbox creation operations."""
    print("\n=== Wave 6: Sandbox operations ===\n")

    tasks = [
        make_request(session, "POST", "/api/nfs/sandbox_get_or_create",
                    {"jsonrpc": "2.0", "method": "sandbox_get_or_create",
                     "params": {"name": "admin,ImpersonatedUser"}, "id": 40},
                    "sandbox_get_or_create (ImpersonatedUser)"),
        make_request(session, "POST", "/api/nfs/sandbox_get_or_create",
                    {"jsonrpc": "2.0", "method": "sandbox_get_or_create",
                     "params": {"name": "admin,UntrustedAgent"}, "id": 41},
                    "sandbox_get_or_create (UntrustedAgent)"),
    ]

    await asyncio.gather(*tasks)


async def wave_7_final_skills_barrage(session: aiohttp.ClientSession):
    """Wave 7: Final barrage of concurrent skills operations."""
    print("\n=== Wave 7: FINAL BARRAGE - Maximum concurrent skills_list ===\n")

    # Maximum concurrent skills_list calls to ensure crash
    tasks = [
        make_request(session, "POST", "/api/nfs/skills_list",
                    {"jsonrpc": "2.0", "method": "skills_list", "params": {}, "id": 50 + i},
                    f"skills_list (BARRAGE) #{i+1}")
        for i in range(10)  # 10 concurrent calls
    ]

    await asyncio.gather(*tasks)


async def main():
    """Main function to run all waves of API calls."""
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║  Nexus Crash Reproduction - Concurrent API Calls         ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print()
    print(f"Target: {BASE_URL}")
    print(f"API Key: {API_KEY[:30]}...")
    print()

    # Check server health first
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/health") as response:
                if response.status != 200:
                    print(f"✗ Server health check failed: {response.status}")
                    sys.exit(1)
                print("✓ Server is healthy\n")
    except Exception as e:
        print(f"✗ Cannot connect to server: {e}")
        sys.exit(1)

    # Run all waves
    async with aiohttp.ClientSession() as session:
        try:
            await wave_1_initial_loads(session)
            await asyncio.sleep(0.5)

            await wave_2_agents_and_permissions(session)
            await asyncio.sleep(0.5)

            # This wave should trigger the crash
            await wave_3_skills_concurrent(session)
            await asyncio.sleep(0.5)

            await wave_4_file_operations(session)
            await asyncio.sleep(0.5)

            await wave_5_more_skills(session)
            await asyncio.sleep(0.5)

            await wave_6_sandbox_operations(session)
            await asyncio.sleep(0.5)

            # Final barrage to ensure crash
            await wave_7_final_skills_barrage(session)

            print("\n" + "="*60)
            print("All waves completed successfully!")
            print("="*60)
            print()
            print("If the server didn't crash, the bug may have been fixed.")
            print("Check the server logs for any warnings or errors.")

        except Exception as e:
            print(f"\n✗ Exception during API calls: {e}")
            print("This might indicate the server crashed.")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
