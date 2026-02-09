"""E2E tests for async file endpoints with a real running Nexus server.

Starts a real `nexus serve` process (uvicorn + FastAPI) and makes real HTTP
requests to verify all 9 /api/v2/files/* endpoints work through the full
server stack, including:
- Real FastAPI lifespan (AsyncNexusFS initialized via lifespan)
- Real PostgreSQL database (shared between sync NexusFS and AsyncNexusFS)
- Real HTTP network I/O (not ASGI test transport)
- User context via X-Nexus-Subject and X-Nexus-Zone-ID headers

Issue #940: Full async migration for MetadataStore and NexusFS.
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

# === Helpers ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30  # seconds

# Clear proxy env vars so localhost connections work (must happen before httpx)
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
    """Start a real nexus serve process for the test module.

    Yields a dict with base_url and data_dir.

    Requires PostgreSQL. Override with NEXUS_TEST_DATABASE_URL env var if needed.
    """
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    # PostgreSQL required — set NEXUS_TEST_DATABASE_URL or use default local instance
    database_url = os.environ.get(
        "NEXUS_TEST_DATABASE_URL",
        "postgresql://scorpio:scorpio@127.0.0.1:5432/nexus_e2e_test",
    )

    base_url = f"http://127.0.0.1:{port}"

    # Build env: inherit PATH for nexus CLI, clear proxy for localhost
    env = {
        **os.environ,
        # Clear proxies so localhost connections work
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        # Source code on PYTHONPATH for dev mode
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        # PostgreSQL: used by BOTH sync NexusFS and async lifespan
        "NEXUS_DATABASE_URL": database_url,
        # AsyncNexusFS settings
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "e2e-test",
        # Permissions disabled for happy-path E2E (no ReBAC setup needed)
        "NEXUS_ENFORCE_PERMISSIONS": "false",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
        # Disable search daemon (not needed for file endpoint tests)
        "NEXUS_SEARCH_DAEMON": "false",
        # Disable rate limiting for tests
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    # Start nexus serve as a real subprocess using CLI entry point
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
    """Shared httpx client that bypasses proxy."""
    with _make_client() as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    """Get the base URL of the running server."""
    return server["base_url"]


@pytest.fixture()
def user_headers() -> dict[str, str]:
    """Headers that set user context in open-access mode."""
    return {
        "X-Nexus-Subject": "user:e2e_tester",
        "X-Nexus-Zone-ID": "e2e-test",
    }


# =============================================================================
# All 9 endpoints — happy path through real HTTP server
# =============================================================================


def test_health(base_url: str, client: httpx.Client) -> None:
    """Health endpoint responds."""
    resp = client.get(f"{base_url}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_write(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """POST /api/v2/files/write creates a file."""
    # Clean up if exists from previous run
    client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/hello.txt"},
        headers=user_headers,
    )

    resp = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/hello.txt", "content": "Hello E2E!"},
        headers=user_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 1
    assert data["size"] == len("Hello E2E!")
    assert "etag" in data


def test_read(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """GET /api/v2/files/read returns file content."""
    client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/read.txt", "content": "readable"},
        headers=user_headers,
    )
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/e2e/read.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "readable"


def test_delete(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """DELETE /api/v2/files/delete removes a file."""
    client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/del.txt", "content": "bye"},
        headers=user_headers,
    )
    resp = client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/del.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True


def test_exists(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """GET /api/v2/files/exists checks file existence."""
    client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/exists.txt", "content": "here"},
        headers=user_headers,
    )
    resp = client.get(
        f"{base_url}/api/v2/files/exists",
        params={"path": "/e2e/exists.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["exists"] is True

    # Check non-existent file
    resp2 = client.get(
        f"{base_url}/api/v2/files/exists",
        params={"path": "/e2e/nope.txt"},
        headers=user_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["exists"] is False


def test_list(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """GET /api/v2/files/list lists directory contents."""
    for name in ["alpha.txt", "beta.txt", "gamma.txt"]:
        client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": f"/e2e/listdir/{name}", "content": name},
            headers=user_headers,
        )
    resp = client.get(
        f"{base_url}/api/v2/files/list",
        params={"path": "/e2e/listdir"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert "alpha.txt" in items
    assert "beta.txt" in items
    assert "gamma.txt" in items


def test_mkdir(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """POST /api/v2/files/mkdir creates a directory."""
    # Clean up if exists from previous run
    client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/newdir"},
        headers=user_headers,
    )

    resp = client.post(
        f"{base_url}/api/v2/files/mkdir",
        json={"path": "/e2e/newdir", "parents": True},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is True


def test_metadata(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """GET /api/v2/files/metadata returns file metadata."""
    # Clean up if exists from previous run
    client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/meta.txt"},
        headers=user_headers,
    )

    client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/meta.txt", "content": "metadata test"},
        headers=user_headers,
    )
    resp = client.get(
        f"{base_url}/api/v2/files/metadata",
        params={"path": "/e2e/meta.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "/e2e/meta.txt"
    assert data["size"] == len("metadata test")
    assert data["version"] == 1
    assert data["is_directory"] is False


def test_batch_read(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """POST /api/v2/files/batch-read reads multiple files."""
    paths = ["/e2e/batch1.txt", "/e2e/batch2.txt"]
    for p in paths:
        client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": p, "content": f"content-{p}"},
            headers=user_headers,
        )
    resp = client.post(
        f"{base_url}/api/v2/files/batch-read",
        json={"paths": paths},
        headers=user_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    for p in paths:
        assert data[p]["content"] == f"content-{p}"


def test_stream(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """GET /api/v2/files/stream streams file content."""
    content = "S" * 5000
    client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/stream.txt", "content": content},
        headers=user_headers,
    )
    resp = client.get(
        f"{base_url}/api/v2/files/stream",
        params={"path": "/e2e/stream.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert resp.content.decode() == content


# =============================================================================
# Version + ETag tests
# =============================================================================


def test_version_bumps(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """Version increments on each write."""
    # Clean up if exists from previous run
    client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/versioned.txt"},
        headers=user_headers,
    )

    for i in range(1, 4):
        resp = client.post(
            f"{base_url}/api/v2/files/write",
            json={"path": "/e2e/versioned.txt", "content": f"v{i}"},
            headers=user_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == i

    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/e2e/versioned.txt"},
        headers=user_headers,
    )
    assert resp.json()["content"] == "v3"


def test_404_on_missing(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """Non-existent files return 404."""
    resp = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/e2e/nonexistent.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 404

    resp = client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/nonexistent.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 404

    resp = client.get(
        f"{base_url}/api/v2/files/metadata",
        params={"path": "/e2e/nonexistent.txt"},
        headers=user_headers,
    )
    assert resp.status_code == 404


# =============================================================================
# Full CRUD workflow with user context
# =============================================================================


def test_full_crud_workflow(base_url: str, client: httpx.Client, user_headers: dict) -> None:
    """Full create-read-update-delete workflow via real HTTP."""
    # 1. Write
    w = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/workflow.txt", "content": "v1"},
        headers=user_headers,
    )
    assert w.status_code == 200
    assert w.json()["version"] == 1

    # 2. Read
    r = client.get(
        f"{base_url}/api/v2/files/read",
        params={"path": "/e2e/workflow.txt"},
        headers=user_headers,
    )
    assert r.json()["content"] == "v1"

    # 3. Update
    u = client.post(
        f"{base_url}/api/v2/files/write",
        json={"path": "/e2e/workflow.txt", "content": "v2"},
        headers=user_headers,
    )
    assert u.json()["version"] == 2

    # 4. Metadata
    m = client.get(
        f"{base_url}/api/v2/files/metadata",
        params={"path": "/e2e/workflow.txt"},
        headers=user_headers,
    )
    assert m.json()["version"] == 2

    # 5. Delete
    d = client.delete(
        f"{base_url}/api/v2/files/delete",
        params={"path": "/e2e/workflow.txt"},
        headers=user_headers,
    )
    assert d.json()["deleted"] is True

    # 6. Verify gone
    e = client.get(
        f"{base_url}/api/v2/files/exists",
        params={"path": "/e2e/workflow.txt"},
        headers=user_headers,
    )
    assert e.json()["exists"] is False


def test_user_context_passed(base_url: str, client: httpx.Client) -> None:
    """Verify user context headers are recognized by the server."""
    resp = client.get(
        f"{base_url}/api/auth/whoami",
        headers={
            "X-Nexus-Subject": "user:alice",
            "X-Nexus-Zone-ID": "zone-42",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["subject_id"] == "alice"
    assert data["zone_id"] == "zone-42"
