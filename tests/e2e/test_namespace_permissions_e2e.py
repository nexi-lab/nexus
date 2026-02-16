"""E2E tests for namespace manager with PERMISSIONS ENABLED (Issue #1239).

Tests the per-subject namespace visibility model with a real FastAPI server:
- Each subject sees only the paths they've been granted via ReBAC
- Unmounted paths return 404 (invisible), not 403 (denied)
- Admin/system bypass namespace checks
- Performance validation for namespace visibility checks
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
import uuid
from pathlib import Path
from typing import Any

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
    return httpx.Client(timeout=60)


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    """Poll /health until the server responds or timeout."""
    deadline = time.monotonic() + timeout
    with _make_client() as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _rpc(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call a JSON-RPC method on the NFS API and return parsed response."""
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"RPC {method} returned HTTP {resp.status_code}: {resp.text[:500]}"
    )
    return resp.json()


def _rebac_grant(
    client: httpx.Client,
    base_url: str,
    subject_id: str,
    relation: str,
    object_path: str,
    zone_id: str = "test",
    headers: dict[str, str] | None = None,
) -> str | None:
    """Create a ReBAC tuple via JSON-RPC and return the tuple_id."""
    result = _rpc(
        client,
        base_url,
        "rebac_create",
        {
            "subject": ["user", subject_id],
            "relation": relation,
            "object": ["file", object_path],
            "zone_id": zone_id,
        },
        headers=headers,
    )
    rpc_result = result.get("result", {})
    return rpc_result.get("tuple_id") if isinstance(rpc_result, dict) else None


def _rebac_revoke(
    client: httpx.Client,
    base_url: str,
    tuple_id: str,
    headers: dict[str, str] | None = None,
) -> bool:
    """Delete a ReBAC tuple via JSON-RPC."""
    result = _rpc(
        client,
        base_url,
        "rebac_delete",
        {"tuple_id": tuple_id},
        headers=headers,
    )
    return result.get("error") is None


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start a real nexus serve process WITH PERMISSIONS ENABLED.

    Yields a dict with base_url and data_dir.

    Requires Metastore (Rust sled extension) for in-memory ReBAC.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_ns_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    # Build env: PERMISSIONS ENABLED
    env = {
        **os.environ,
        # Clear proxies
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        # Source code on PYTHONPATH
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        # AsyncNexusFS settings
        "NEXUS_DATABASE_URL": f"sqlite:///{os.path.join(data_dir, 'nexus.db')}",
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "ns-e2e-test",
        # CRITICAL: Permissions ENABLED for namespace testing
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        # Small revision window so namespace cache invalidates after each grant
        "NEXUS_NAMESPACE_REVISION_WINDOW": "1",
        # Use in-memory Metastore (no PostgreSQL dependency)
        "NEXUS_REBAC_BACKEND": "memory",  # Metastore in-memory
        # Admin designation for open access mode (namespace bypass)
        "NEXUS_STATIC_ADMINS": "admin",
        # Disable search daemon
        "NEXUS_SEARCH_DAEMON": "false",
        # Disable rate limiting
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    # Start nexus serve as a real subprocess
    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}'])"
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
        # Dump server output on startup failure
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
        # Graceful shutdown
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

        # Cleanup temp dir
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    """Shared httpx client."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    """Get the base URL of the running server."""
    return server["base_url"]


@pytest.fixture()
def alice_headers() -> dict[str, str]:
    """Headers for user alice in zone test."""
    return {
        "X-Nexus-Subject": "user:alice",
        "X-Nexus-Zone-ID": "test",
    }


@pytest.fixture()
def bob_headers() -> dict[str, str]:
    """Headers for user bob in zone test."""
    return {
        "X-Nexus-Subject": "user:bob",
        "X-Nexus-Zone-ID": "test",
    }


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """Headers for admin user (bypasses namespace checks via NEXUS_STATIC_ADMINS)."""
    return {
        "X-Nexus-Subject": "user:admin",
        "X-Nexus-Zone-ID": "test",
    }


# =============================================================================
# Namespace Visibility Tests (Issue #1239)
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Health endpoint responds."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_zero_grants_zero_visibility(
    base_url: str, client: httpx.Client
) -> None:
    """Subject with no grants sees nothing (fail-closed).

    A user with no ReBAC grants → all paths return 404 (invisible).
    Uses a dedicated user (charlie) to avoid polluting namespace cache
    for alice/bob who are tested with grants in later tests.
    """
    charlie_headers = {"X-Nexus-Subject": "user:charlie", "X-Nexus-Zone-ID": "test"}
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/secret.txt"},
        headers=charlie_headers,
    )
    # Should return 404 (path invisible), not 403 (permission denied)
    assert resp.status_code == 404
    assert "not found" in resp.json().get("detail", "").lower()


def test_per_subject_namespace_isolation(
    base_url: str, client: httpx.Client, alice_headers: dict, bob_headers: dict, admin_headers: dict
) -> None:
    """Each subject sees only their granted paths.

    Setup:
    - Admin creates two files: /workspace/alice-project/data.txt and /workspace/bob-project/data.txt
    - Admin grants alice viewer-of /workspace/alice-project/data.txt
    - Admin grants bob viewer-of /workspace/bob-project/data.txt

    Expected:
    - Alice sees /workspace/alice-project/*, NOT /workspace/bob-project/*
    - Bob sees /workspace/bob-project/*, NOT /workspace/alice-project/*
    - Unmounted paths return 404 (invisible), not 403
    """
    # Admin creates both files
    alice_path = "/workspace/alice-project/data.txt"
    bob_path = "/workspace/bob-project/data.txt"

    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": alice_path, "content": "Alice's data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Admin write failed: {resp.text}"

    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": bob_path, "content": "Bob's data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Admin write failed: {resp.text}"

    # Admin grants ReBAC permissions via JSON-RPC
    alice_tid = _rebac_grant(client, base_url, "alice", "direct_viewer", alice_path, headers=admin_headers)
    assert alice_tid, "Grant alice failed"

    bob_tid = _rebac_grant(client, base_url, "bob", "direct_viewer", bob_path, headers=admin_headers)
    assert bob_tid, "Grant bob failed"

    # Wait for cache to settle (namespace manager rebuild)
    time.sleep(0.5)

    # Test Alice's namespace: sees alice_path, NOT bob_path
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Alice should see {alice_path}: {resp.text}"
    assert resp.json()["content"] == "Alice's data"

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_path},
        headers=alice_headers,
    )
    # Should return 404 (path invisible to alice), not 403
    assert resp.status_code == 404, f"Alice should NOT see {bob_path}: {resp.text}"

    # Test Bob's namespace: sees bob_path, NOT alice_path
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": bob_path},
        headers=bob_headers,
    )
    assert resp.status_code == 200, f"Bob should see {bob_path}: {resp.text}"
    assert resp.json()["content"] == "Bob's data"

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": alice_path},
        headers=bob_headers,
    )
    # Should return 404 (path invisible to bob), not 403
    assert resp.status_code == 404, f"Bob should NOT see {alice_path}: {resp.text}"


def test_admin_bypasses_namespace(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """Admin user bypasses namespace visibility checks.

    Admin creates a file that frank has no grant for, but admin can still access it.
    Uses dedicated user (frank) to avoid namespace cache pollution.
    """
    frank_headers = {"X-Nexus-Subject": "user:frank", "X-Nexus-Zone-ID": "test"}
    secret_path = "/admin/secret.txt"

    # Admin creates a file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": secret_path, "content": "Top secret"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Frank cannot see it (no grant)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=frank_headers,
    )
    assert resp.status_code == 404  # Invisible

    # Admin can see it (bypass)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Top secret"


def test_fine_grained_rebac_check_after_namespace(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """Defense in depth: namespace visibility + ReBAC fine-grained check.

    User has viewer-of grant (read-only) on /workspace/shared/doc.txt.
    - GET /read should work (visible + read permission)
    - POST /write should fail with 403 (visible but no write permission)

    Uses dedicated user (dave) to avoid namespace cache pollution from prior tests.
    """
    dave_headers = {"X-Nexus-Subject": "user:dave", "X-Nexus-Zone-ID": "test"}
    doc_path = "/workspace/shared/doc.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "v1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants dave VIEWER (read-only) via JSON-RPC
    tid = _rebac_grant(client, base_url, "dave", "direct_viewer", doc_path, headers=admin_headers)
    assert tid, "Grant dave viewer failed"

    time.sleep(0.5)  # Cache settle

    # Dave can READ (visible + read permission)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": doc_path},
        headers=dave_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "v1"

    # Dave CANNOT WRITE (visible but no write permission)
    # Should return 403 (permission denied), NOT 404 (path is visible)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "v2"},
        headers=dave_headers,
    )
    assert resp.status_code == 403, f"Dave should get 403 (no write perm): {resp.text}"


def test_grant_revocation_makes_path_invisible(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """Revoking a grant makes the path invisible (404).

    1. Admin grants eve viewer-of /workspace/project/data.txt
    2. Eve can read it (200)
    3. Admin revokes the grant
    4. Eve gets 404 (path now invisible)

    Uses dedicated user (eve) to avoid namespace cache pollution from prior tests.
    """
    eve_headers = {"X-Nexus-Subject": "user:eve", "X-Nexus-Zone-ID": "test"}
    project_path = "/workspace/project/data.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": project_path, "content": "Project data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants eve viewer via JSON-RPC
    tuple_id = _rebac_grant(client, base_url, "eve", "direct_viewer", project_path, headers=admin_headers)
    assert tuple_id, "Grant eve viewer failed"

    time.sleep(0.5)  # Cache settle

    # Eve can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": project_path},
        headers=eve_headers,
    )
    assert resp.status_code == 200

    # Admin revokes grant via JSON-RPC
    assert _rebac_revoke(client, base_url, tuple_id, headers=admin_headers)

    time.sleep(0.5)  # Cache invalidation

    # Eve now gets 404 (path invisible)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": project_path},
        headers=eve_headers,
    )
    assert resp.status_code == 404


# =============================================================================
# Performance Tests
# =============================================================================


def test_namespace_check_performance(
    base_url: str, client: httpx.Client, admin_headers: dict
) -> None:
    """Validate namespace visibility check has acceptable performance.

    Expected: <10ms per visibility check (O(log m) bisect lookup).
    Uses dedicated user (grace) to avoid namespace cache pollution.
    """
    grace_headers = {"X-Nexus-Subject": "user:grace", "X-Nexus-Zone-ID": "test"}
    paths = [f"/workspace/perf-test/file-{i:03d}.txt" for i in range(20)]

    # Create one file to test visibility against
    test_path = paths[10]  # Middle of the sorted list (worst case for bisect)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": test_path, "content": "perf test"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Grant grace viewer on all 20 paths (builds a 20-entry mount table)
    for p in paths:
        _rebac_grant(client, base_url, "grace", "direct_viewer", p, headers=admin_headers)

    time.sleep(0.5)  # Cache rebuild

    # Measure 50 visibility checks (O(log 20) bisect each)
    start = time.perf_counter()
    for _ in range(50):
        resp = client.get(
            f"{base_url}/api/v2/files/read",
            params={"path": test_path},
            headers=grace_headers,
        )
        assert resp.status_code == 200

    elapsed_ms = (time.perf_counter() - start) * 1000
    avg_ms = elapsed_ms / 50

    print(f"\n[PERF] 50 visibility checks: {elapsed_ms:.1f}ms total, {avg_ms:.2f}ms avg")

    # Assert: Average per-check latency should be reasonable
    # This includes HTTP round-trip + namespace check + ReBAC check
    # Namespace check alone should be <1ms, total <100ms per request reasonable
    assert avg_ms < 100, f"Namespace check too slow: {avg_ms:.2f}ms avg (expected <100ms)"


# === Issue #1398: VFS Lock Manager active ===


def test_vfs_lock_manager_active(server: dict) -> None:
    """Verify the VFS lock manager is initialized in the running server.

    We check that the /health endpoint is accessible (server is running)
    and that the lock manager module is importable — the server logs
    'VFS lock manager initialized (...)' on startup.
    """
    base_url = server["base_url"]
    with _make_client() as client:
        resp = client.get(f"{base_url}/health")
        assert resp.status_code == 200

    # Verify the lock manager classes are importable and functional.
    from nexus.core.lock_fast import VFSLockManagerProtocol, create_vfs_lock_manager

    mgr = create_vfs_lock_manager()
    assert isinstance(mgr, VFSLockManagerProtocol)

    # Quick smoke test: acquire + release.
    h = mgr.acquire("/e2e/test", "write")
    assert h > 0
    assert mgr.release(h)
    assert not mgr.is_locked("/e2e/test")
