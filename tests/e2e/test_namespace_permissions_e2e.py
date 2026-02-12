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
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "ns-e2e-test",
        # CRITICAL: Permissions ENABLED for namespace testing
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        # Use in-memory Metastore (no PostgreSQL dependency)
        "NEXUS_AUTH_TYPE": "static",  # Static auth with in-memory ReBAC
        "NEXUS_REBAC_BACKEND": "memory",  # Metastore in-memory
        # Static users (alice, bob, admin)
        "NEXUS_STATIC_USERS": "alice:pass1,bob:pass2,admin:pass3",
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
    """Headers for admin user (bypasses namespace checks)."""
    return {
        "X-Nexus-Subject": "user:admin",
        "X-Nexus-Zone-ID": "test",
        "X-Nexus-Require-Admin": "true",
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
    base_url: str, client: httpx.Client, alice_headers: dict
) -> None:
    """Subject with no grants sees nothing (fail-closed).

    Alice has no ReBAC grants → all paths return 404 (invisible).
    """
    # Try to read a file that exists in the backend but alice has no grant
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/workspace/secret.txt"},
        headers=alice_headers,
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

    # Admin grants ReBAC permissions
    # Grant alice viewer-of alice_path
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": alice_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant alice failed: {resp.text}"

    # Grant bob viewer-of bob_path
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "user",
            "subject_id": "bob",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": bob_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant bob failed: {resp.text}"

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
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Admin user bypasses namespace visibility checks.

    Admin creates a file that alice has no grant for, but admin can still access it.
    """
    secret_path = "/admin/secret.txt"

    # Admin creates a file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": secret_path, "content": "Top secret"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Alice cannot see it (no grant)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": secret_path},
        headers=alice_headers,
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
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Defense in depth: namespace visibility + ReBAC fine-grained check.

    Alice has viewer-of grant (read-only) on /workspace/shared/doc.txt.
    - GET /read should work (visible + read permission)
    - POST /write should fail with 403 (visible but no write permission)
    """
    doc_path = "/workspace/shared/doc.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "v1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants alice VIEWER (read-only)
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "direct_viewer",  # viewer = read-only
            "object_type": "file",
            "object_id": doc_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200

    time.sleep(0.5)  # Cache settle

    # Alice can READ (visible + read permission)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": doc_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "v1"

    # Alice CANNOT WRITE (visible but no write permission)
    # Should return 403 (permission denied), NOT 404 (path is visible)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": doc_path, "content": "v2"},
        headers=alice_headers,
    )
    assert resp.status_code == 403, f"Alice should get 403 (no write perm): {resp.text}"


def test_grant_revocation_makes_path_invisible(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Revoking a grant makes the path invisible (404).

    1. Admin grants alice viewer-of /workspace/project/data.txt
    2. Alice can read it (200)
    3. Admin revokes the grant
    4. Alice gets 404 (path now invisible)
    """
    project_path = "/workspace/project/data.txt"

    # Admin creates file
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": project_path, "content": "Project data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Admin grants alice viewer
    resp = client.post(
        f"{base_url}/api/rebac/tuples",
        json={
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": project_path,
            "zone_id": "test",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200
    tuple_id = resp.json().get("tuple_id")

    time.sleep(0.5)  # Cache settle

    # Alice can read
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": project_path},
        headers=alice_headers,
    )
    assert resp.status_code == 200

    # Admin revokes grant
    resp = client.delete(
        f"{base_url}/api/rebac/tuples/{tuple_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200

    time.sleep(0.5)  # Cache invalidation

    # Alice now gets 404 (path invisible)
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": project_path},
        headers=alice_headers,
    )
    assert resp.status_code == 404


# =============================================================================
# Performance Tests
# =============================================================================


def test_namespace_check_performance(
    base_url: str, client: httpx.Client, alice_headers: dict, admin_headers: dict
) -> None:
    """Validate namespace visibility check has acceptable performance.

    Expected: <10ms per visibility check (O(log m) bisect lookup).
    """
    # Admin grants alice access to 100 different paths
    paths = [f"/workspace/perf-test/file-{i:03d}.txt" for i in range(100)]

    # Create one file to test visibility against
    test_path = paths[50]  # Middle of the sorted list (worst case for bisect)
    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": test_path, "content": "perf test"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Grant alice viewer on all 100 paths (builds a 100-entry mount table)
    for p in paths:
        client.post(
            f"{base_url}/api/rebac/tuples",
            json={
                "subject_type": "user",
                "subject_id": "alice",
                "relation": "direct_viewer",
                "object_type": "file",
                "object_id": p,
                "zone_id": "test",
            },
            headers=admin_headers,
        )

    time.sleep(0.5)  # Cache rebuild

    # Measure 100 visibility checks (O(log 100) bisect each)
    start = time.perf_counter()
    for _ in range(100):
        resp = client.get(
            f"{base_url}/api/v2/files/read",
            params={"path": test_path},
            headers=alice_headers,
        )
        assert resp.status_code == 200

    elapsed_ms = (time.perf_counter() - start) * 1000
    avg_ms = elapsed_ms / 100

    print(f"\n[PERF] 100 visibility checks: {elapsed_ms:.1f}ms total, {avg_ms:.2f}ms avg")

    # Assert: Average per-check latency should be reasonable
    # This includes HTTP round-trip + namespace check + ReBAC check
    # Namespace check alone should be <1ms, total <100ms per request reasonable
    assert avg_ms < 100, f"Namespace check too slow: {avg_ms:.2f}ms avg (expected <100ms)"
