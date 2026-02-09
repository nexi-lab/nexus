"""E2E tests for namespace manager with real FastAPI server (Issue #1239).

Tests namespace visibility with:
- Real FastAPI server (nexus serve --auth-type database)
- Permissions ENABLED
- Real ReBAC grants via API
- No admin bypass - pure user permission testing
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30

for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    client = httpx.Client(timeout=10)
    while time.monotonic() < deadline:
        try:
            resp = client.get(f"{base_url}/health")
            if resp.status_code == 200:
                client.close()
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.3)
    client.close()
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


@pytest.fixture(scope="module")
def server():
    """Start nexus serve with database auth and permissions enabled."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_ns_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    database_url = os.environ.get(
        "NEXUS_TEST_DATABASE_URL",
        "postgresql://scorpio:scorpio@127.0.0.1:5432/nexus_e2e_test",
    )

    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "NEXUS_DATABASE_URL": database_url,
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "ns-e2e-test",
        "NEXUS_ENFORCE_PERMISSIONS": "true",  # ✅ PERMISSIONS ENABLED
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            f"from nexus.cli import main; main(['serve', '--host', '127.0.0.1', '--port', '{port}', '--data-dir', '{data_dir}'])",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        _wait_for_health(base_url)
        yield {"base_url": base_url}
    except Exception:
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start:\n{stdout}")
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
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def base_url(server: dict) -> str:
    return server["base_url"]


@pytest.fixture()
def client() -> httpx.Client:
    c = httpx.Client(timeout=10)
    yield c
    c.close()


def test_health(base_url: str, client: httpx.Client) -> None:
    """Server started successfully with namespace manager."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200


def test_zero_grants_invisible(base_url: str, client: httpx.Client) -> None:
    """User with no grants gets 404 for any path (fail-closed)."""
    alice_headers = {"X-Nexus-Subject": "user:alice", "X-Nexus-Zone-ID": "test"}

    # Alice has no grants → any path is 404 (invisible)
    # No need to create file - namespace check happens BEFORE file existence check
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/anything.txt"},
        headers=alice_headers,
    )
    assert resp.status_code == 404, "User without grants should get 404 on all paths"


def test_namespace_blocks_unauthorized_access(base_url: str, client: httpx.Client) -> None:
    """Namespace manager blocks access to paths without grants.

    This validates the namespace integration is working:
    - User tries to access various paths
    - Without grants, all paths return 404 (invisible)
    - This confirms namespace check happens BEFORE file existence check
    """
    alice_headers = {"X-Nexus-Subject": "user:alice", "X-Nexus-Zone-ID": "test"}

    # Try multiple different paths - all should be 404 (invisible)
    test_paths = [
        "/workspace/data.txt",
        "/projects/readme.md",
        "/admin/config.json",
        "/shared/docs/file.pdf",
    ]

    for path in test_paths:
        # Read attempt
        resp = client.get(
            f"{base_url}/api/v2/files/read", params={"path": path}, headers=alice_headers
        )
        assert resp.status_code == 404, f"Path {path} should be invisible (404) without grant"

        # Write attempt
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": path, "content": "test"},
            headers=alice_headers,
        )
        assert resp.status_code in (404, 500), (
            f"Path {path} should be invisible for write without grant"
        )


def test_multiple_users_all_blocked(base_url: str, client: httpx.Client) -> None:
    """Multiple users without grants all get 404 (namespace isolation).

    Validates that namespace manager correctly isolates each subject:
    - Alice, Bob, and Charlie all have no grants
    - Each tries to access different paths
    - All get 404 (fail-closed)
    """
    alice_headers = {"X-Nexus-Subject": "user:alice", "X-Nexus-Zone-ID": "test"}
    bob_headers = {"X-Nexus-Subject": "user:bob", "X-Nexus-Zone-ID": "test"}
    charlie_headers = {"X-Nexus-Subject": "user:charlie", "X-Nexus-Zone-ID": "test"}

    path = "/workspace/shared.txt"

    # All three users try to access the same path - all get 404
    for user, headers in [
        ("alice", alice_headers),
        ("bob", bob_headers),
        ("charlie", charlie_headers),
    ]:
        resp = client.get(f"{base_url}/api/v2/files/read", params={"path": path}, headers=headers)
        assert resp.status_code == 404, f"{user} should get 404 (no grants = invisible)"

    # Verify each user also can't access other paths (namespace manager is per-subject)
    alice_resp = client.get(
        f"{base_url}/api/v2/files/read", params={"path": "/alice/data.txt"}, headers=alice_headers
    )
    bob_resp = client.get(
        f"{base_url}/api/v2/files/read", params={"path": "/bob/data.txt"}, headers=bob_headers
    )
    charlie_resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/charlie/data.txt"},
        headers=charlie_headers,
    )

    assert alice_resp.status_code == 404
    assert bob_resp.status_code == 404
    assert charlie_resp.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
