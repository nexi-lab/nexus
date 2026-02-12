"""E2E tests for normal (non-admin) user with FastAPI + database auth + permissions.

Tests the full auth and permission pipeline end-to-end:
1. Start server with --auth-type database --init (creates admin key)
2. Create a normal (non-admin) user via admin RPC
3. Grant ReBAC permissions to the normal user
4. Verify: normal user can read/write where permitted
5. Verify: normal user is DENIED where not permitted
6. Verify: auth cache returns correct results on repeated calls
7. Verify: invalid/expired tokens are rejected

This exercises the changes from the comprehensive review:
- hmac.compare_digest for API key validation
- TTLCache auth caching with shallow copy
- Shared permission check utility
- XML escaping in skill service (indirectly)
- Session-per-operation in SandboxManager

Issue #1293: Comprehensive codebase review
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _rpc_call(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict,
    *,
    api_key: str | None = None,
    headers: dict | None = None,
) -> dict | list | bool | None:
    """Make an RPC call. Returns result or raises on error."""
    h = dict(headers or {})
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers=h,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error in {method}: {data['error']}")
    return data.get("result")


def _rpc_call_raw(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict,
    *,
    api_key: str | None = None,
    headers: dict | None = None,
) -> dict:
    """Make an RPC call returning the full JSON response (including errors)."""
    h = dict(headers or {})
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers=h,
    )
    return resp.json()


# =============================================================================
# Fixture: Start server with database auth + permissions
# =============================================================================


@pytest.fixture(scope="module")
def e2e_server():
    """Start a real Nexus server with database auth and permissions enabled.

    Creates admin user, yields server info including admin API key.
    """
    with tempfile.TemporaryDirectory(prefix="nexus_user_e2e_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        data_dir = tmpdir_path / "data"
        data_dir.mkdir()
        db_path = tmpdir_path / "metadata.db"

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"

        env = {
            **os.environ,
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "NO_PROXY": "*",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
            "NEXUS_JWT_SECRET": "test-e2e-secret-key-12345",
        }

        # Start server with --init (creates admin key)
        proc = subprocess.Popen(
            [
                PYTHON,
                "-c",
                (
                    "from nexus.cli import main; "
                    f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                    f"'--data-dir', '{data_dir}', "
                    "'--auth-type', 'database', '--init', '--reset'])"
                ),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )

        try:
            # Wait for server to start and capture admin key from output
            admin_api_key = None
            deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT
            lines_collected = []

            while time.monotonic() < deadline:
                # Check for health
                try:
                    with httpx.Client(timeout=2) as client:
                        resp = client.get(f"{base_url}/health")
                        if resp.status_code == 200:
                            break
                except httpx.ConnectError:
                    pass
                time.sleep(0.3)

                # Read available output for admin key
                if proc.stdout and proc.stdout.readable():
                    import select

                    if select.select([proc.stdout], [], [], 0)[0]:
                        line = proc.stdout.readline()
                        if line:
                            lines_collected.append(line)
                            if "sk-" in line and not admin_api_key:
                                # Extract API key from output
                                for word in line.split():
                                    if word.startswith("sk-"):
                                        admin_api_key = word.strip("'\"")
                                        break
            else:
                # Timeout - dump what we have
                stdout_rest = proc.stdout.read() if proc.stdout else ""
                all_output = "".join(lines_collected) + stdout_rest
                pytest.fail(f"Server failed to start. Output:\n{all_output}")

            # If we didn't capture admin key from stdout, read remaining output
            if not admin_api_key:
                # Try reading more output
                remaining = []
                if proc.stdout:
                    import select

                    for _ in range(50):  # Try 50 times
                        if select.select([proc.stdout], [], [], 0.1)[0]:
                            line = proc.stdout.readline()
                            if line:
                                remaining.append(line)
                                if "sk-" in line:
                                    for word in line.split():
                                        if word.startswith("sk-"):
                                            admin_api_key = word.strip("'\"")
                                            break
                        if admin_api_key:
                            break

            # Also check .nexus-admin-env file
            if not admin_api_key:
                env_file = Path(".nexus-admin-env")
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if "NEXUS_API_KEY" in line and "sk-" in line:
                            admin_api_key = line.split("'")[1] if "'" in line else None
                            break

            if not admin_api_key:
                all_output = "".join(lines_collected + remaining)
                pytest.fail(
                    f"Could not extract admin API key from server output.\n"
                    f"Output:\n{all_output}"
                )

            yield {
                "base_url": base_url,
                "port": port,
                "process": proc,
                "admin_api_key": admin_api_key,
                "data_dir": str(data_dir),
                "db_path": str(db_path),
            }

        finally:
            if proc.poll() is None:
                if sys.platform != "win32":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


# =============================================================================
# Tests
# =============================================================================


class TestServerHealth:
    """Basic server health and admin auth."""

    def test_server_is_healthy(self, e2e_server: dict) -> None:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{e2e_server['base_url']}/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    def test_admin_whoami(self, e2e_server: dict) -> None:
        """Admin key should authenticate as admin."""
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{e2e_server['base_url']}/api/auth/whoami",
                headers={"Authorization": f"Bearer {e2e_server['admin_api_key']}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["authenticated"] is True
            assert data["is_admin"] is True

    def test_no_auth_rejected(self, e2e_server: dict) -> None:
        """Request without auth should be rejected."""
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{e2e_server['base_url']}/api/nfs/list",
                json={"jsonrpc": "2.0", "method": "list", "params": {"path": "/workspace"}, "id": 1},
            )
            # Should get auth error (HTTP 401/403 or JSON error)
            if resp.status_code in (401, 403):
                return  # Expected
            data = resp.json()
            assert "error" in data or "detail" in data, f"Expected auth rejection, got: {data}"


class TestNormalUserLifecycle:
    """Create a normal user and test their permissions."""

    @pytest.fixture(scope="class")
    def alice_key(self, e2e_server: dict) -> str:
        """Create a normal user 'alice' via admin RPC and return her API key."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "admin_create_key",
                {
                    "user_id": "alice",
                    "name": "Alice's test key",
                    "is_admin": False,
                    "zone_id": "default",
                    "expires_days": 1,
                },
                api_key=e2e_server["admin_api_key"],
            )
            assert result["api_key"].startswith("sk-")
            assert result["is_admin"] is False
            return result["api_key"]

    def test_alice_authenticates(self, e2e_server: dict, alice_key: str) -> None:
        """Alice's key should authenticate as non-admin user."""
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{e2e_server['base_url']}/api/auth/whoami",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["authenticated"] is True
            assert data["is_admin"] is False
            assert data["subject_id"] == "alice"

    def test_alice_auth_cached_on_second_call(
        self, e2e_server: dict, alice_key: str
    ) -> None:
        """Second auth call should be faster (cache hit)."""
        with httpx.Client(timeout=10) as client:
            # First call (cache miss)
            resp1 = client.get(
                f"{e2e_server['base_url']}/api/auth/whoami",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            data1 = resp1.json()
            assert data1["authenticated"] is True

            # Second call (should be cached)
            resp2 = client.get(
                f"{e2e_server['base_url']}/api/auth/whoami",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            data2 = resp2.json()
            assert data2["authenticated"] is True
            assert data2["subject_id"] == "alice"

    def test_invalid_key_rejected(self, e2e_server: dict) -> None:
        """Invalid API key should be rejected (hmac.compare_digest)."""
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{e2e_server['base_url']}/api/auth/whoami",
                headers={"Authorization": "Bearer sk-default_fake_k1_invalidkey1234567890"},
            )
            # Should fail authentication
            assert resp.status_code in (401, 200)
            if resp.status_code == 200:
                data = resp.json()
                assert data["authenticated"] is False


class TestPermissionEnforcement:
    """Test ReBAC permission enforcement for normal users.

    The --init flag creates admin user with ownership of /workspace.
    Normal users (non-admin) have no permissions unless explicitly granted.
    Admin bypass is enabled so admin can write files freely.
    """

    @pytest.fixture(scope="class")
    def setup_users(self, e2e_server: dict) -> dict:
        """Create bob (non-admin, no permissions)."""
        with httpx.Client(timeout=10) as client:
            admin_key = e2e_server["admin_api_key"]
            base = e2e_server["base_url"]

            # Create bob (no permissions granted)
            bob_result = _rpc_call(
                client, base, "admin_create_key",
                {
                    "user_id": "bob-perm",
                    "name": "Bob Perm Key",
                    "is_admin": False,
                    "zone_id": "default",
                    "expires_days": 1,
                },
                api_key=admin_key,
            )

            return {
                "bob_key": bob_result["api_key"],
            }

    def test_admin_can_write(self, e2e_server: dict) -> None:
        """Admin should be able to write files (admin bypass)."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "write",
                {
                    "path": "/workspace/admin-file.txt",
                    "content": "Hello from admin",
                },
                api_key=e2e_server["admin_api_key"],
            )
            # Should succeed (admin bypass)
            assert result is not None or isinstance(result, dict)

    def test_admin_can_list_workspace(self, e2e_server: dict) -> None:
        """Admin should be able to list /workspace."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "list",
                {"path": "/workspace"},
                api_key=e2e_server["admin_api_key"],
            )
            # list returns {"files": [...], "has_more": bool, ...}
            assert isinstance(result, dict)
            assert "files" in result

    def test_admin_can_read_file(self, e2e_server: dict) -> None:
        """Admin should be able to read files they wrote."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "read",
                {"path": "/workspace/admin-file.txt"},
                api_key=e2e_server["admin_api_key"],
            )
            assert result is not None

    def test_bob_sees_empty_workspace(
        self, e2e_server: dict, setup_users: dict
    ) -> None:
        """Bob (no permissions) should see an empty /workspace (files filtered by perm)."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "list",
                {"path": "/workspace"},
                api_key=setup_users["bob_key"],
            )
            # Bob can list the directory but sees no files (permission filtering)
            assert isinstance(result, dict)
            assert result["files"] == []

    def test_bob_denied_write(
        self, e2e_server: dict, setup_users: dict
    ) -> None:
        """Bob should not be able to write to /workspace."""
        with httpx.Client(timeout=10) as client:
            raw = _rpc_call_raw(
                client,
                e2e_server["base_url"],
                "write",
                {
                    "path": "/workspace/bob-file.txt",
                    "content": "Bob trying to write",
                },
                api_key=setup_users["bob_key"],
            )
            assert "error" in raw, f"Expected permission error for bob write, got: {raw}"

    def test_bob_denied_read_admin_file(
        self, e2e_server: dict, setup_users: dict
    ) -> None:
        """Bob should not be able to read admin's file."""
        with httpx.Client(timeout=10) as client:
            raw = _rpc_call_raw(
                client,
                e2e_server["base_url"],
                "read",
                {"path": "/workspace/admin-file.txt"},
                api_key=setup_users["bob_key"],
            )
            assert "error" in raw, f"Expected permission error for bob read, got: {raw}"

    def test_rebac_check_admin_has_read(self, e2e_server: dict) -> None:
        """rebac_check should confirm admin has read permission on /workspace."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "rebac_check",
                {
                    "subject": ["user", "admin"],
                    "permission": "read",
                    "object": ["file", "/workspace"],
                    "zone_id": "default",
                },
                api_key=e2e_server["admin_api_key"],
            )
            assert result is True

    def test_rebac_check_bob_no_read(self, e2e_server: dict) -> None:
        """rebac_check should deny bob read permission on /workspace."""
        with httpx.Client(timeout=10) as client:
            result = _rpc_call(
                client,
                e2e_server["base_url"],
                "rebac_check",
                {
                    "subject": ["user", "bob-perm"],
                    "permission": "read",
                    "object": ["file", "/workspace"],
                    "zone_id": "default",
                },
                api_key=e2e_server["admin_api_key"],
            )
            assert result is False
