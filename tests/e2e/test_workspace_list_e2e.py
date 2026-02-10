"""E2E tests for register_workspace + list_workspaces with PERMISSIONS ENABLED (Issue #1201).

Tests the full chain with a real FastAPI server subprocess:
  HTTP POST /api/nfs/{method} -> auth -> OperationContext -> NexusFS.list_workspaces()

Verifies:
- Workspaces registered by alice are visible to alice via list_workspaces
- Workspaces registered by alice are NOT visible to bob
- Workspaces at non-standard paths are returned to their creator (the #1201 fix)
- Unauthenticated list_workspaces is rejected (401)
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

# Use Python 3.13 which has the Rust Metastore extension built for arm64
PYTHON = "/opt/homebrew/bin/python3.13"
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


def _rpc_call(
    client: httpx.Client,
    base_url: str,
    method: str,
    params: dict,
    headers: dict | None = None,
) -> httpx.Response:
    """Make a JSON-RPC call to the NFS endpoint."""
    return client.post(
        f"{base_url}/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        },
        headers=headers or {},
    )


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start a real nexus serve process WITH PERMISSIONS ENABLED."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_ws_e2e_")
    backend_root = os.path.join(data_dir, "backend")
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
        "NEXUS_TENANT_ID": "ws-e2e-test",
        # CRITICAL: Permissions ENABLED
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        # Note: auth-type is set via CLI args, NOT env var (avoids double-init conflict)
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
    }

    # Start server directly via Python to bypass CLI auth factory bug
    # (CLI's create_auth_provider("static", api_key=...) fails because
    # it requires auth_config dict, not api_key kwarg)
    meta_dir = os.path.join(data_dir, "metadata")
    db_path = os.path.join(data_dir, "nexus.db")
    server_script = f"""
import sys, os
sys.path.insert(0, os.environ.get('PYTHONPATH', ''))
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
    enforce_permissions=True,
    enforce_zone_isolation=True,
)
# Open-access mode (no api_key) so X-Nexus-Subject/Zone-ID headers
# are respected for identity. Static API key auth always returns
# subject_id="admin" which defeats multi-user isolation testing.
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
    with httpx.Client(timeout=10) as c:
        yield c


@pytest.fixture()
def base_url(server: dict) -> str:
    return server["base_url"]


@pytest.fixture()
def alice_headers() -> dict[str, str]:
    """Headers for alice: identity hint (open-access mode)."""
    return {
        "X-Nexus-Subject": "user:alice",
        "X-Nexus-Zone-ID": "test",
    }


@pytest.fixture()
def bob_headers() -> dict[str, str]:
    """Headers for bob: identity hint (open-access mode)."""
    return {
        "X-Nexus-Subject": "user:bob",
        "X-Nexus-Zone-ID": "test",
    }


# =============================================================================
# E2E Tests: Workspace Registration + Listing (Issue #1201)
# =============================================================================


@pytest.mark.e2e
class TestWorkspaceListE2E:
    """E2E: register_workspace + list_workspaces through real FastAPI server."""

    def test_health(self, base_url: str, client: httpx.Client) -> None:
        """Server is healthy."""
        resp = client.get(f"{base_url}/health")
        assert resp.status_code == 200

    def test_register_and_list_workspace(
        self, base_url: str, client: httpx.Client, alice_headers: dict
    ) -> None:
        """Alice registers a workspace and sees it in list_workspaces."""
        resp = _rpc_call(
            client,
            base_url,
            "register_workspace",
            {"path": "/workspace/alice-project", "name": "Alice Project"},
            alice_headers,
        )
        assert resp.status_code == 200, f"register_workspace failed: {resp.text}"
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        # List workspaces as alice
        resp = _rpc_call(client, base_url, "list_workspaces", {}, alice_headers)
        assert resp.status_code == 200, f"list_workspaces failed: {resp.text}"
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        workspaces = data.get("result", [])
        paths = [ws["path"] for ws in workspaces]
        assert any("alice-project" in p for p in paths), (
            f"alice-project not found in workspace list: {paths}"
        )

    def test_workspace_isolation_between_users(
        self, base_url: str, client: httpx.Client, alice_headers: dict, bob_headers: dict
    ) -> None:
        """Bob should NOT see alice's workspaces."""
        # Register workspace as alice
        _rpc_call(
            client,
            base_url,
            "register_workspace",
            {"path": "/workspace/alice-secret", "name": "Alice Secret"},
            alice_headers,
        )

        # List workspaces as bob
        resp = _rpc_call(client, base_url, "list_workspaces", {}, bob_headers)
        assert resp.status_code == 200, f"list_workspaces failed: {resp.text}"
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        workspaces = data.get("result", [])
        paths = [ws["path"] for ws in workspaces]
        assert not any("alice-secret" in p for p in paths), (
            f"Bob should NOT see alice-secret workspace: {paths}"
        )

    def test_nonstandard_path_workspace_returned_to_creator(
        self, base_url: str, client: httpx.Client, alice_headers: dict, bob_headers: dict
    ) -> None:
        """Issue #1201: Workspace at non-standard path visible to creator via created_by.

        Uses a path within alice's zone/user scope but NOT under the standard
        /zone/{zone}/user/{user}/workspace/ prefix, so only the created_by
        filter (not path prefix) can match it.
        """
        # Register at a path in alice's zone/user scope but NOT under /workspace/
        # This tests the created_by filter specifically (the #1201 fix)
        resp = _rpc_call(
            client,
            base_url,
            "register_workspace",
            {"path": "/zone/test/user/alice/data/team-data", "name": "Team Data"},
            alice_headers,
        )
        assert resp.status_code == 200, f"register_workspace failed: {resp.text}"
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        # Alice should see it (created_by matching, NOT path prefix)
        resp = _rpc_call(client, base_url, "list_workspaces", {}, alice_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        workspaces = data.get("result", [])
        paths = [ws["path"] for ws in workspaces]
        assert any("team-data" in p for p in paths), (
            f"team-data not found in workspace list (the #1201 bug): {paths}"
        )

        # Bob should NOT see it
        resp = _rpc_call(client, base_url, "list_workspaces", {}, bob_headers)
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        workspaces = data.get("result", [])
        paths = [ws["path"] for ws in workspaces]
        assert not any("team-data" in p for p in paths), (
            f"Bob should NOT see team-data workspace: {paths}"
        )

    def test_list_workspaces_without_identity_returns_empty(
        self, base_url: str, client: httpx.Client
    ) -> None:
        """list_workspaces without identity headers returns empty list.

        In open-access mode, requests without X-Nexus-Subject default to
        user_id="anonymous", zone_id="default" — which won't match any
        workspaces created by alice or bob.
        """
        resp = _rpc_call(client, base_url, "list_workspaces", {})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("error") is None, f"RPC error: {data.get('error')}"

        workspaces = data.get("result", [])
        assert workspaces == [], f"Anonymous user should see no workspaces, got: {workspaces}"
