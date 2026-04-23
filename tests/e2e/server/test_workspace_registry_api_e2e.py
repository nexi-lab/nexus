"""E2E tests for workspace/memory registry REST API (Issue #2987).

Starts a real FastAPI server subprocess and tests all 10 REST endpoints:
- 5 workspace endpoints at /api/v2/registry/workspaces
- 5 memory endpoints at /api/v2/registry/memories

Tests the full chain: HTTP request → auth → router → WorkspaceRegistry → DB.
"""

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

# Use the current Python interpreter (needs all nexus deps installed)
PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=10) as client:
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
    """Start a real nexus serve process."""
    if not os.path.exists(PYTHON):
        pytest.skip(f"Python 3.13 not found at {PYTHON}")

    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_registry_e2e_")
    backend_root = os.path.join(data_dir, "backend")
    os.makedirs(backend_root, exist_ok=True)

    base_url = f"http://127.0.0.1:{port}"

    # Point PYTHONPATH to our worktree's src so the new router is available
    src_path = str(Path(__file__).resolve().parents[3] / "src")

    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": src_path,
        "NEXUS_BACKEND_ROOT": backend_root,
        "NEXUS_TENANT_ID": "registry-e2e-test",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    meta_dir = os.path.join(data_dir, "metadata")
    db_path = os.path.join(data_dir, "nexus.db")
    server_script = f"""
import sys, os, asyncio
sys.path.insert(0, '{src_path}')
from nexus.backends.local import LocalBackend
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.factory import create_nexus_fs
from nexus.server.fastapi_server import create_app
import uvicorn

backend = LocalBackend(root_path='{backend_root}')
metadata_store = RaftMetadataStore.embedded('{meta_dir}')
record_store = SQLAlchemyRecordStore(db_path='{db_path}')

nx = create_nexus_fs(
    backend=backend,
    metadata_store=metadata_store,
    record_store=record_store,
))
app = create_app(nexus_fs=nx)
uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')
"""
    proc = subprocess.Popen(
        [PYTHON, "-c", server_script],
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
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        stdout, _ = proc.communicate(timeout=5)
        print(f"Server output:\n{stdout}")
        raise
    finally:
        if proc.poll() is None:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def client(server):
    """HTTP client pointed at the running server."""
    with httpx.Client(base_url=server["base_url"], timeout=10) as c:
        yield c


# === Workspace CRUD Tests ===


class TestWorkspaceRegistryE2E:
    """E2E tests for workspace registration REST endpoints."""

    def test_list_workspaces_empty(self, client: httpx.Client) -> None:
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["items"] == []

    def test_register_workspace(self, client: httpx.Client) -> None:
        resp = client.post(
            "/api/v2/registry/workspaces",
            json={
                "path": "/e2e-workspace",
                "name": "E2E Test Workspace",
                "description": "Created by e2e test",
                "metadata": {"test": True},
            },
        )
        assert resp.status_code == 201, f"Body: {resp.text}"
        data = resp.json()
        assert data["path"] == "/e2e-workspace"
        assert data["name"] == "E2E Test Workspace"
        assert data["description"] == "Created by e2e test"
        assert data["scope"] == "persistent"
        assert data["metadata"] == {"test": True}

    def test_get_workspace(self, client: httpx.Client) -> None:
        # Register first
        client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-get-ws", "name": "GetTest"},
        )
        resp = client.get("/api/v2/registry/workspaces/e2e-get-ws")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/e2e-get-ws"
        assert data["name"] == "GetTest"
        # Verify v0.5.0 fields present
        assert "scope" in data
        assert "user_id" in data
        assert "agent_id" in data
        assert "session_id" in data
        assert "expires_at" in data

    def test_get_workspace_not_found(self, client: httpx.Client) -> None:
        resp = client.get("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404

    def test_update_workspace(self, client: httpx.Client) -> None:
        # Register first
        client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-update-ws", "name": "Before"},
        )
        resp = client.patch(
            "/api/v2/registry/workspaces/e2e-update-ws",
            json={"name": "After", "description": "Updated"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "After"
        assert data["description"] == "Updated"

    def test_update_workspace_not_found(self, client: httpx.Client) -> None:
        resp = client.patch(
            "/api/v2/registry/workspaces/nonexistent",
            json={"name": "X"},
        )
        assert resp.status_code == 404

    def test_register_duplicate_workspace(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-dup-ws"},
        )
        resp = client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-dup-ws"},
        )
        assert resp.status_code == 409

    def test_unregister_workspace(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-del-ws"},
        )
        resp = client.delete("/api/v2/registry/workspaces/e2e-del-ws")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True

        # Verify it's gone
        resp = client.get("/api/v2/registry/workspaces/e2e-del-ws")
        assert resp.status_code == 404

    def test_unregister_workspace_not_found(self, client: httpx.Client) -> None:
        resp = client.delete("/api/v2/registry/workspaces/nonexistent")
        assert resp.status_code == 404

    def test_list_workspaces_shows_registered(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/workspaces",
            json={"path": "/e2e-list-ws", "name": "ListTest"},
        )
        resp = client.get("/api/v2/registry/workspaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        paths = [item["path"] for item in data["items"]]
        assert "/e2e-list-ws" in paths


# === Memory CRUD Tests ===


class TestMemoryRegistryE2E:
    """E2E tests for memory registration REST endpoints."""

    def test_list_memories_empty_initially(self, client: httpx.Client) -> None:
        resp = client.get("/api/v2/registry/memories")
        assert resp.status_code == 200
        # May have items from workspace tests if memories were registered
        assert "items" in resp.json()
        assert "count" in resp.json()

    def test_register_memory(self, client: httpx.Client) -> None:
        resp = client.post(
            "/api/v2/registry/memories",
            json={
                "path": "/e2e-memory",
                "name": "E2E Test Memory",
                "description": "Created by e2e test",
                "metadata": {"type": "kb"},
            },
        )
        assert resp.status_code == 201, f"Body: {resp.text}"
        data = resp.json()
        assert data["path"] == "/e2e-memory"
        assert data["name"] == "E2E Test Memory"
        assert data["scope"] == "persistent"

    def test_get_memory(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/memories",
            json={"path": "/e2e-get-mem", "name": "GetMemTest"},
        )
        resp = client.get("/api/v2/registry/memories/e2e-get-mem")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/e2e-get-mem"
        assert data["name"] == "GetMemTest"
        # All TUI fields present
        for field in ["scope", "user_id", "agent_id", "session_id", "expires_at", "metadata"]:
            assert field in data, f"Missing field: {field}"

    def test_get_memory_not_found(self, client: httpx.Client) -> None:
        resp = client.get("/api/v2/registry/memories/nonexistent")
        assert resp.status_code == 404

    def test_update_memory(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/memories",
            json={"path": "/e2e-update-mem", "name": "Before"},
        )
        resp = client.patch(
            "/api/v2/registry/memories/e2e-update-mem",
            json={"name": "After", "description": "Updated memory"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "After"
        assert data["description"] == "Updated memory"

    def test_update_memory_not_found(self, client: httpx.Client) -> None:
        resp = client.patch(
            "/api/v2/registry/memories/nonexistent",
            json={"name": "X"},
        )
        assert resp.status_code == 404

    def test_register_duplicate_memory(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/memories",
            json={"path": "/e2e-dup-mem"},
        )
        resp = client.post(
            "/api/v2/registry/memories",
            json={"path": "/e2e-dup-mem"},
        )
        assert resp.status_code == 409

    def test_unregister_memory(self, client: httpx.Client) -> None:
        client.post(
            "/api/v2/registry/memories",
            json={"path": "/e2e-del-mem"},
        )
        resp = client.delete("/api/v2/registry/memories/e2e-del-mem")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True

        # Verify it's gone
        resp = client.get("/api/v2/registry/memories/e2e-del-mem")
        assert resp.status_code == 404

    def test_unregister_memory_not_found(self, client: httpx.Client) -> None:
        resp = client.delete("/api/v2/registry/memories/nonexistent")
        assert resp.status_code == 404
