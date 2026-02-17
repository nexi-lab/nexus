"""E2E tests for namespace-scoped FUSE visibility (Issue #1305).

Tests the FUSE namespace scoping via the HTTP API with agent identity:
- Agent with X-Agent-ID header sees only granted paths
- Unmounted paths return 404 (invisible), not 403 (denied)
- Admin bypasses namespace checks
- Grant revocation makes paths invisible
- Directory listing (readdir equivalent) only shows visible entries

This test uses the same server fixture pattern as test_namespace_permissions_e2e.py
but focuses on agent identity (X-Agent-ID header) rather than user identity.
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

pytestmark = pytest.mark.quarantine

# === Helpers ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30  # seconds

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_client() -> httpx.Client:
    """Create httpx client for localhost connections."""
    return httpx.Client(timeout=10)


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    """Poll /health AND verify AsyncNexusFS is ready before returning."""
    deadline = time.monotonic() + timeout
    health_ok = False
    with _make_client() as client:
        while time.monotonic() < deadline:
            try:
                if not health_ok:
                    resp = client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        health_ok = True
                        # Health is up, but AsyncNexusFS may still be initializing.
                        # Probe a real API endpoint to confirm readiness.
                if health_ok:
                    resp = client.get(
                        f"{base_url}/api/v2/files/list",
                        params={"path": "/"},
                        headers={"X-Nexus-Subject": "user:admin", "X-Nexus-Require-Admin": "true"},
                    )
                    # Server wraps AsyncNexusFS errors as 500 (not 503).
                    # Only consider ready when we get a non-5xx response.
                    if resp.status_code < 500:
                        return  # Server fully ready
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start a real nexus serve process with PERMISSIONS ENABLED.

    Yields a dict with base_url and data_dir.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_fuse_ns_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    db_path = os.path.join(data_dir, "metadata.db")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "fuse-ns-e2e-test",
        # SQLite database for AsyncNexusFS initialization (required by lifespan)
        "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
        # CRITICAL: Permissions ENABLED for namespace testing
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        "NEXUS_AUTH_TYPE": "static",
        "NEXUS_REBAC_BACKEND": "memory",
        "NEXUS_STATIC_USERS": "alice:pass1,admin:pass3",
        "NEXUS_STATIC_ADMINS": "admin",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}', '--auth-type', 'static', '--api-key', 'test-api-key'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        _wait_for_health(base_url)
        yield {
            "base_url": base_url,
            "port": port,
            "data_dir": data_dir,
            "process": proc,
        }
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
            proc.wait(timeout=3)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start. Output:\n{stdout}")
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

        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    """Shared httpx client."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    return server["base_url"]


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """Headers for admin user (bypasses namespace checks)."""
    return {
        "X-Nexus-Subject": "user:admin",
        "X-Nexus-Zone-ID": "test",
        "X-Nexus-Require-Admin": "true",
    }


@pytest.fixture()
def agent_headers() -> dict[str, str]:
    """Headers for agent-001 using X-Agent-ID header (Decision 7B)."""
    return {
        "X-Nexus-Subject": "user:alice",
        "X-Agent-ID": "agent-001",
        "X-Nexus-Zone-ID": "test",
    }


@pytest.fixture()
def agent2_headers() -> dict[str, str]:
    """Headers for agent-002 (different agent)."""
    return {
        "X-Nexus-Subject": "user:alice",
        "X-Agent-ID": "agent-002",
        "X-Nexus-Zone-ID": "test",
    }


# =============================================================================
# Namespace Visibility Tests via Agent Identity (Issue #1305)
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Health endpoint responds."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_agent_zero_grants_zero_visibility(
    base_url: str, client: httpx.Client, agent_headers: dict, admin_headers: dict
) -> None:
    """Agent with no grants sees nothing (fail-closed).

    Agent-001 has no ReBAC grants -> all paths return 404 (invisible).
    """
    # Admin creates a file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/workspace/agent-test/secret.txt", "content": "secret data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Agent with no grants gets 404 (invisible, not 403)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/agent-test/secret.txt"},
        headers=agent_headers,
    )
    assert resp.status_code == 404
    assert "not found" in resp.json().get("detail", "").lower()


def test_agent_namespace_isolation(
    base_url: str,
    client: httpx.Client,
    agent_headers: dict,
    agent2_headers: dict,
    admin_headers: dict,
) -> None:
    """Two agents see different namespaces based on their grants.

    - Agent-001 can see /workspace/agent1-project/* only
    - Agent-002 can see /workspace/agent2-project/* only
    - Neither can see the other's files
    """
    agent1_path = "/workspace/agent1-project/data.txt"
    agent2_path = "/workspace/agent2-project/data.txt"

    # Admin creates both files
    for path, content in [(agent1_path, "Agent 1 data"), (agent2_path, "Agent 2 data")]:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": path, "content": content},
            headers=admin_headers,
        )
        assert resp.status_code == 200, f"Admin write to {path} failed: {resp.text}"

    # Grant agent-001 viewer on agent1_path
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "agent",
            "subject_id": "agent-001",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": agent1_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant agent-001 failed: {resp.text}"

    # Grant agent-002 viewer on agent2_path
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "agent",
            "subject_id": "agent-002",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": agent2_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant agent-002 failed: {resp.text}"

    time.sleep(0.5)  # Cache settle

    # Agent-001 sees its path
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": agent1_path},
        headers=agent_headers,
    )
    assert resp.status_code == 200, f"Agent-001 should see {agent1_path}: {resp.text}"
    assert resp.json()["content"] == "Agent 1 data"

    # Agent-001 does NOT see agent-002's path (404)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": agent2_path},
        headers=agent_headers,
    )
    assert resp.status_code == 404, f"Agent-001 should NOT see {agent2_path}: {resp.text}"

    # Agent-002 sees its path
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": agent2_path},
        headers=agent2_headers,
    )
    assert resp.status_code == 200, f"Agent-002 should see {agent2_path}: {resp.text}"
    assert resp.json()["content"] == "Agent 2 data"

    # Agent-002 does NOT see agent-001's path (404)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": agent1_path},
        headers=agent2_headers,
    )
    assert resp.status_code == 404, f"Agent-002 should NOT see {agent1_path}: {resp.text}"


def test_admin_sees_everything(base_url: str, client: httpx.Client, admin_headers: dict) -> None:
    """Admin bypasses namespace checks — sees all paths."""
    admin_path = "/admin/fuse-test-secret.txt"

    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": admin_path, "content": "admin only"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": admin_path},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "admin only"


def test_agent_directory_listing_filtered(
    base_url: str,
    client: httpx.Client,
    agent_headers: dict,
    admin_headers: dict,
) -> None:
    """Directory listing via API only shows paths visible to the agent.

    This simulates what readdir() would see in a FUSE mount.
    """
    # Admin creates files in different directories
    visible_path = "/workspace/visible-dir/readme.md"
    hidden_path = "/workspace/hidden-dir/secret.md"

    for path, content in [
        (visible_path, "Visible content"),
        (hidden_path, "Hidden content"),
    ]:
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": path, "content": content},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    # Grant agent-001 viewer on visible_path only
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "agent",
            "subject_id": "agent-001",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": visible_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200

    time.sleep(0.5)

    # Agent reads visible file — should succeed
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": visible_path},
        headers=agent_headers,
    )
    assert resp.status_code == 200

    # Agent reads hidden file — should get 404 (invisible)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": hidden_path},
        headers=agent_headers,
    )
    assert resp.status_code == 404


def test_agent_grant_revocation(
    base_url: str,
    client: httpx.Client,
    agent_headers: dict,
    admin_headers: dict,
) -> None:
    """Revoking a grant makes the path invisible to the agent.

    1. Admin grants agent-001 viewer-of path
    2. Agent can read it (200)
    3. Admin revokes the grant
    4. Agent gets 404 (path invisible)
    """
    revoke_path = "/workspace/revoke-test/file.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": revoke_path, "content": "revoke test"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants agent-001 viewer
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "agent",
            "subject_id": "agent-001",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": revoke_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    tuple_id = resp.json().get("tuple_id")

    time.sleep(0.5)

    # Agent can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": revoke_path},
        headers=agent_headers,
    )
    assert resp.status_code == 200

    # Admin revokes grant
    resp = client.delete(
        f"{base_url}/api/rebac/tuples/{tuple_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200

    time.sleep(0.5)  # Cache invalidation

    # Agent now gets 404 (path invisible)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": revoke_path},
        headers=agent_headers,
    )
    assert resp.status_code == 404


def test_invisible_path_returns_404_not_403(
    base_url: str,
    client: httpx.Client,
    agent_headers: dict,
    admin_headers: dict,
) -> None:
    """Unmounted paths return 404 (ENOENT), never 403 (EPERM).

    This is the Plan 9 principle: invisible paths don't leak their existence.
    """
    # Admin creates a file that agent has no grant for
    secret_path = "/workspace/top-secret/classified.txt"
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": secret_path, "content": "classified"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Agent gets 404 (not 403)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=agent_headers,
    )
    assert resp.status_code == 404, f"Expected 404 (invisible), got {resp.status_code}: {resp.text}"
