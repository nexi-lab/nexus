#!/usr/bin/env python3
"""E2E test for wildcard (*:*) public access with real FastAPI server.

This script:
1. Starts a real nexus serve process
2. Creates users and files via HTTP API
3. Creates wildcard permission tuples
4. Verifies wildcard grants access across zones

Usage:
    PYTHONPATH=src uv run python scripts/test_wildcard_e2e.py
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, suppress
from pathlib import Path

import httpx

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

JWT_SECRET = "test-wildcard-e2e-secret-key"


def find_free_port() -> int:
    """Find a free port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=2.0, trust_env=False)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def create_jwt_token(user_id: str, zone_id: str = "default") -> str:
    """Create a JWT token for testing.

    Token format must match what authlib.jose.jwt expects for validation.
    """
    import jwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "subject_id": user_id,
        "subject_type": "user",
        "zone_id": zone_id,
        "email": f"{user_id}@test.com",
        "name": f"Test User {user_id[:8]}",
        "is_admin": False,
        "iat": now,
        "exp": now + 3600,  # 1 hour expiry
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def rpc_call(client: httpx.Client, method: str, params: dict, headers: dict):
    """Make JSON-RPC call to server."""
    response = client.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers=headers,
    )
    if response.status_code != 200:
        raise Exception(f"RPC call failed: {response.status_code} - {response.text}")
    result = response.json()
    if "error" in result:
        raise Exception(f"RPC error: {result['error']}")
    return result.get("result")


def print_result(name: str, passed: bool, message: str = ""):
    """Print test result."""
    status = "PASS" if passed else "FAIL"
    color = "\033[92m" if passed else "\033[91m"
    reset = "\033[0m"
    print(f"  [{color}{status}{reset}] {name}")
    if message:
        print(f"        {message}")


def main():
    print("\n" + "=" * 70)
    print("Wildcard Public Access E2E Test - Issue #1064")
    print("=" * 70)

    # Create temp directory
    tmp_dir = tempfile.mkdtemp(prefix="nexus_wildcard_e2e_")
    db_path = Path(tmp_dir) / "test.db"
    storage_path = Path(tmp_dir) / "storage"
    storage_path.mkdir()

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Environment for server
    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = JWT_SECRET
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")

    print(f"\n[*] Starting nexus serve on port {port}...")

    # Start server - pass JWT secret explicitly via environment
    print(f"    JWT_SECRET being set: {JWT_SECRET[:10]}...")

    # Start server
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import os
import sys
sys.path.insert(0, '{Path(__file__).parent.parent / "src"}')
# Debug: print the JWT secret being used
print(f"Server JWT_SECRET: {{os.getenv('NEXUS_JWT_SECRET', 'NOT SET')[:10]}}...", flush=True)
from nexus.cli import main
main(['serve', '--host', '127.0.0.1', '--port', '{port}', '--data-dir', '{tmp_dir}', '--auth-type', 'database', '--init'])
""",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        if not wait_for_server(base_url, timeout=30.0):
            stdout, stderr = process.communicate(timeout=5)
            print("[!] Server failed to start!")
            print(f"stdout: {stdout.decode()}")
            print(f"stderr: {stderr.decode()}")
            return 1

        print(f"[*] Server started successfully at {base_url}")

        # Read server output and extract admin API key
        import re
        import select

        admin_api_key = None

        # Read server output to get the admin API key
        for _ in range(50):  # Read up to 50 chunks
            if process.stdout and select.select([process.stdout], [], [], 0.2)[0]:
                chunk = os.read(process.stdout.fileno(), 8192).decode()
                # Look for the admin API key line
                match = re.search(r"Admin API Key:\s*(sk-[a-zA-Z0-9-_]+)", chunk)
                if match:
                    admin_api_key = match.group(1)
                    print(f"    Found admin API key: {admin_api_key[:15]}...")
                    break
                if "Press Ctrl+C" in chunk:
                    break
            else:
                break

        if not admin_api_key:
            print("[!] Failed to extract admin API key from server output")
            # Try to read from the .nexus-admin-env file that gets created
            admin_env_file = Path(tmp_dir).parent / ".nexus-admin-env"
            if admin_env_file.exists():
                content = admin_env_file.read_text()
                match = re.search(r"NEXUS_API_KEY='(sk-[^']+)'", content)
                if match:
                    admin_api_key = match.group(1)
                    print(f"    Found admin API key from env file: {admin_api_key[:15]}...")

        if not admin_api_key:
            print("[!] Could not get admin API key - tests will fail")
            return 1

        # Create test client
        client = httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)
        admin_headers = {"Authorization": f"Bearer {admin_api_key}"}

        # Create users
        user_a_id = str(uuid.uuid4())
        user_b_id = str(uuid.uuid4())
        user_a_token = create_jwt_token(user_a_id, "zone-a")
        user_b_token = create_jwt_token(user_b_id, "zone-b")
        user_a_headers = {"Authorization": f"Bearer {user_a_token}"}
        user_b_headers = {"Authorization": f"Bearer {user_b_token}"}

        print(f"\n[*] Created User A (zone-a): {user_a_id[:8]}...")
        print(f"[*] Created User B (zone-b): {user_b_id[:8]}...")

        passed = 0
        failed = 0

        # Define test file path - use /public/ which is accessible
        test_file_path = "/public/wildcard-test-doc.txt"

        # ==================================================================
        # Test 1: Setup - Create test file (using admin)
        # ==================================================================
        print("\n--- Test 1: Setup - Create test file ---")
        try:
            # Create file as admin (needed for setup)
            result = rpc_call(
                client,
                "write",
                {"path": test_file_path, "content": "Hello World"},
                admin_headers,
            )
            print(f"    Created test file: {test_file_path}")

            # Verify file exists by reading it back
            content = rpc_call(
                client,
                "read",
                {"path": test_file_path},
                admin_headers,
            )
            # RPC returns bytes as {'__type__': 'bytes', 'data': 'base64...'}
            import base64

            if isinstance(content, dict) and content.get("__type__") == "bytes":
                actual_content = base64.b64decode(content["data"]).decode("utf-8")
            else:
                actual_content = str(content)

            if actual_content == "Hello World":
                print_result("File created and readable", True)
                passed += 1
            else:
                print_result("File created and readable", False, f"Got: {actual_content}")
                failed += 1
        except Exception as e:
            print_result("File creation test", False, str(e))
            failed += 1

        # ==================================================================
        # Test 2: Without wildcard, User B should NOT have access
        # ==================================================================
        print("\n--- Test 2: Cross-zone denied without wildcard ---")
        try:
            result = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", user_b_id],
                    "permission": "read",
                    "object": ["file", test_file_path],
                    "zone_id": "zone-b",
                },
                user_b_headers,
            )
            if result is False:
                print_result("User B denied without wildcard", True)
                passed += 1
            else:
                print_result(
                    "User B denied without wildcard", False, f"Should be denied but got: {result}"
                )
                failed += 1
        except Exception as e:
            print_result("Cross-zone denial test", False, str(e))
            failed += 1

        # ==================================================================
        # Test 3: Create wildcard tuple and verify User B gets access
        # ==================================================================
        print("\n--- Test 3: Wildcard grants cross-zone access ---")
        try:
            # First, list all tuples to see what's there (as admin)
            try:
                list_result = rpc_call(
                    client,
                    "rebac_list_tuples",
                    {"object": ["file", test_file_path]},
                    admin_headers,
                )
                print(f"    Existing tuples before wildcard: {list_result}")
            except Exception as e:
                print(f"    (Could not list tuples: {e})")

            # Create wildcard tuple as admin: (*:*) -> direct_viewer -> file
            result = rpc_call(
                client,
                "rebac_create",
                {
                    "subject": ["*", "*"],  # Wildcard!
                    "relation": "direct_viewer",
                    "object": ["file", test_file_path],
                    "zone_id": "default",  # Use default zone where file was created
                },
                admin_headers,
            )
            tuple_id = (
                result.get("tuple_id", "unknown") if isinstance(result, dict) else str(result)
            )
            print(f"    Created wildcard tuple: {tuple_id[:8]}...")

            # List tuples again (as admin)
            try:
                list_result = rpc_call(
                    client,
                    "rebac_list_tuples",
                    {"object": ["file", test_file_path]},
                    admin_headers,
                )
                print(f"    Tuples after wildcard: {list_result}")
            except Exception as e:
                print(f"    (Could not list tuples after: {e})")

            # Now User B should have access via wildcard
            # Note: The wildcard is in zone-a, so we check from different zones
            print(f"    Checking permission for user {user_b_id[:8]} with zone-b...")

            # Check from zone-b (cross-zone)
            result = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", user_b_id],
                    "permission": "read",
                    "object": ["file", test_file_path],
                    "zone_id": "zone-b",
                },
                user_b_headers,
            )
            print(f"    Result from zone-b: {result}")

            # Also try checking from zone-a (same zone as wildcard)
            result_same_zone = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", user_b_id],
                    "permission": "read",
                    "object": ["file", test_file_path],
                    "zone_id": "zone-a",  # Same zone as wildcard
                },
                user_a_headers,  # Use zone-a headers
            )
            print(f"    Result from zone-a (same as wildcard): {result_same_zone}")

            # Also check with 'default' zone (where the file owner is)
            result_default = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", user_b_id],
                    "permission": "read",
                    "object": ["file", test_file_path],
                    "zone_id": "default",
                },
                user_a_headers,
            )
            print(f"    Result from default zone: {result_default}")

            if result is True or result_same_zone is True:
                print_result("User B has access via wildcard", True)
                passed += 1
            else:
                print_result(
                    "User B has access via wildcard",
                    False,
                    f"cross-zone={result}, same-zone={result_same_zone}",
                )
                failed += 1
        except Exception as e:
            print_result("Wildcard access test", False, str(e))
            failed += 1

        # ==================================================================
        # Test 4: Random user from any zone should have access
        # ==================================================================
        print("\n--- Test 4: Any random user has access via wildcard ---")
        try:
            random_user_id = str(uuid.uuid4())
            random_token = create_jwt_token(random_user_id, "random-zone")
            random_headers = {"Authorization": f"Bearer {random_token}"}

            result = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", random_user_id],
                    "permission": "read",
                    "object": ["file", test_file_path],
                    "zone_id": "random-zone",
                },
                random_headers,
            )
            if result is True:
                print_result("Random user has access via wildcard", True)
                passed += 1
            else:
                print_result("Random user has access via wildcard", False, f"Got: {result}")
                failed += 1
        except Exception as e:
            print_result("Random user access test", False, str(e))
            failed += 1

        # ==================================================================
        # Test 5: Wildcard reader should NOT grant write permission
        # ==================================================================
        print("\n--- Test 5: Wildcard reader doesn't grant write ---")
        try:
            result = rpc_call(
                client,
                "rebac_check",
                {
                    "subject": ["user", user_b_id],
                    "permission": "write",
                    "object": ["file", test_file_path],
                    "zone_id": "zone-b",
                },
                user_b_headers,
            )
            if result is False:
                print_result("Wildcard reader denied write", True)
                passed += 1
            else:
                print_result(
                    "Wildcard reader denied write", False, f"Should be denied but got: {result}"
                )
                failed += 1
        except Exception as e:
            print_result("Write permission test", False, str(e))
            failed += 1

        # ==================================================================
        # Summary
        # ==================================================================
        print("\n" + "=" * 70)
        total = passed + failed
        if failed == 0:
            print(f"\033[92mAll {total} tests passed!\033[0m")
        else:
            print(f"\033[91m{failed}/{total} tests failed\033[0m")
        print("=" * 70)

        client.close()
        return 0 if failed == 0 else 1

    finally:
        # Cleanup
        print("\n[*] Stopping server...")
        if sys.platform != "win32":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

        # Clean temp dir
        import shutil

        with suppress(Exception):
            shutil.rmtree(tmp_dir)

        print("[*] Done")


if __name__ == "__main__":
    sys.exit(main())
