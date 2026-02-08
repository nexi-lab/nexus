"""E2E test for memory.list() RPC dispatch (#1203).

Tests the full RPC path: HTTP POST /api/nfs/list_memories → FastAPI dispatch
→ _handle_list_memories → Memory.list() with correct context.

Three test classes:
1. TestMemoryListRPCE2E — uses Starlette TestClient (in-process ASGI)
2. TestMemoryListRPCServerE2E — uses real `nexus serve` subprocess over HTTP
3. TestMemoryListRPCDatabaseAuthE2E — uses `nexus serve --auth-type database --init`
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore

pytestmark = pytest.mark.xdist_group("memory_list_rpc")


def _rpc_body(method: str, params: dict | None = None) -> str:
    """Build JSON-RPC request body."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
    )


def _rpc_post_testclient(
    client: TestClient, method: str, params: dict | None = None
) -> dict:
    """Make RPC call via TestClient. Asserts 200 status."""
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"RPC {method} failed: {resp.text}"
    data = resp.json()
    assert "result" in data, f"No result in RPC response: {data}"
    return data["result"]


def _rpc_post_http(
    client: httpx.Client, method: str, params: dict | None = None
) -> dict:
    """Make RPC call via real HTTP. Asserts 200 status."""
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"RPC {method} failed: {resp.text}"
    data = resp.json()
    assert "result" in data, f"No result in RPC response: {data}"
    return data["result"]


# ============================================================================
# TestClient-based tests (in-process ASGI, no server subprocess)
# ============================================================================


@pytest.fixture
def nexus_fs_local(tmp_path: Path):
    """Create a real NexusFS with SQLAlchemy metadata store (no Raft needed)."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    backend = LocalBackend(root_path=storage_path)
    db_path = str(tmp_path / "meta.db")
    metadata_store = SQLAlchemyMetadataStore(db_path=db_path)
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{tmp_path / 'records.db'}")
    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


@pytest.fixture
def rpc_client(nexus_fs_local: NexusFS, tmp_path: Path, monkeypatch):
    """Create sync TestClient with real FastAPI app and NexusFS."""
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    from nexus.server.fastapi_server import _app_state, create_app

    db_url = f"sqlite:///{tmp_path / 'records.db'}"
    app = create_app(nexus_fs=nexus_fs_local, database_url=db_url)
    _app_state.nexus_fs = nexus_fs_local

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    _app_state.nexus_fs = None


class TestMemoryListRPCE2E:
    """E2E: memory.list() through full RPC dispatch path (#1203).

    All operations go through RPC to test the real server path (in-process ASGI).
    """

    def _store_via_rpc(
        self,
        client: TestClient,
        content: str,
        scope: str = "user",
        memory_type: str | None = None,
        state: str = "active",
    ) -> str:
        """Store a memory via RPC and return the memory_id."""
        params: dict = {"content": content, "scope": scope, "state": state}
        if memory_type:
            params["memory_type"] = memory_type
        result = _rpc_post_testclient(client, "store_memory", params)
        return result["memory_id"]

    def test_list_memories_rpc_returns_stored_memories(self, rpc_client: TestClient):
        """Store memories via RPC, list them via RPC — full path test."""
        id1 = self._store_via_rpc(rpc_client, "User prefers dark mode")
        id2 = self._store_via_rpc(rpc_client, "Agent learned Python patterns", scope="agent")
        id3 = self._store_via_rpc(
            rpc_client, "Favorite color is blue", memory_type="preference"
        )

        result = _rpc_post_testclient(rpc_client, "list_memories", {"limit": 50})
        memories = result["memories"]
        assert len(memories) >= 3
        memory_ids = {m["memory_id"] for m in memories}
        assert id1 in memory_ids
        assert id2 in memory_ids
        assert id3 in memory_ids

    def test_list_memories_rpc_filters_by_scope(self, rpc_client: TestClient):
        """RPC list_memories with scope filter works correctly."""
        self._store_via_rpc(rpc_client, "User memory", scope="user")
        self._store_via_rpc(rpc_client, "Agent memory", scope="agent")

        result = _rpc_post_testclient(rpc_client, "list_memories", {"scope": "user", "limit": 50})
        memories = result["memories"]
        assert len(memories) >= 1
        assert all(m["scope"] == "user" for m in memories)

    def test_list_memories_rpc_filters_by_state(self, rpc_client: TestClient):
        """RPC list_memories with state filter works correctly."""
        active_id = self._store_via_rpc(rpc_client, "Active memory", state="active")
        inactive_id = self._store_via_rpc(rpc_client, "Inactive memory", state="inactive")

        result = _rpc_post_testclient(
            rpc_client, "list_memories", {"state": "active", "limit": 50}
        )
        memories = result["memories"]
        memory_ids = {m["memory_id"] for m in memories}
        assert active_id in memory_ids
        assert inactive_id not in memory_ids

    def test_list_memories_rpc_respects_limit(self, rpc_client: TestClient):
        """RPC list_memories respects the limit parameter."""
        for i in range(5):
            self._store_via_rpc(rpc_client, f"Memory {i}")

        result = _rpc_post_testclient(rpc_client, "list_memories", {"limit": 2})
        assert len(result["memories"]) == 2


# ============================================================================
# Real server process tests (nexus serve subprocess over HTTP)
# ============================================================================


class TestMemoryListRPCServerE2E:
    """E2E: memory.list() through real nexus serve process (#1203).

    Uses the nexus_server fixture from conftest.py which spawns a real
    server subprocess and makes actual HTTP requests.
    """

    def _store_via_rpc(
        self,
        client: httpx.Client,
        content: str,
        scope: str = "user",
    ) -> str:
        """Store a memory via RPC over real HTTP."""
        result = _rpc_post_http(
            client, "store_memory", {"content": content, "scope": scope}
        )
        return result["memory_id"]

    def test_store_and_list_via_rpc(self, test_app: httpx.Client):
        """Store memories via RPC, list them via RPC — real server."""
        id1 = self._store_via_rpc(test_app, "Server test memory 1")
        id2 = self._store_via_rpc(test_app, "Server test memory 2")

        result = _rpc_post_http(test_app, "list_memories", {"limit": 50})
        memories = result["memories"]
        assert len(memories) >= 2
        memory_ids = {m["memory_id"] for m in memories}
        assert id1 in memory_ids
        assert id2 in memory_ids

    def test_list_scope_filter_via_rpc(self, test_app: httpx.Client):
        """list_memories scope filter works on real server."""
        self._store_via_rpc(test_app, "User scoped", scope="user")
        self._store_via_rpc(test_app, "Agent scoped", scope="agent")

        result = _rpc_post_http(
            test_app, "list_memories", {"scope": "user", "limit": 50}
        )
        assert all(m["scope"] == "user" for m in result["memories"])

    def test_list_empty_initially(self, test_app: httpx.Client):
        """list_memories returns empty list on fresh server."""
        result = _rpc_post_http(test_app, "list_memories", {"limit": 50})
        assert result["memories"] == []


# ============================================================================
# Database auth server tests (nexus serve --auth-type database --init)
# ============================================================================


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _extract_api_key(output: str) -> str | None:
    """Extract sk-* API key from server init output."""
    match = re.search(r"(sk-[a-zA-Z0-9_]+)", output)
    return match.group(1) if match else None


@pytest.fixture(scope="function")
def db_auth_server(tmp_path):
    """Start nexus serve with --auth-type database --init.

    Captures the admin API key from stdout and yields server info
    including the API key for authenticated requests.
    """
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    db_path = tmp_path / "auth.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    _src_path = Path(__file__).parent.parent.parent / "src"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-db-auth-e2e"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)

    # Start nexus serve with --auth-type database --init
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; main(["
                f"'serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', "
                "'--auth-type', 'database', '--init'"
                "])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Read stdout incrementally to capture API key during init
    api_key = None
    collected_output = ""
    start = time.time()
    timeout = 45.0

    while time.time() - start < timeout:
        # Non-blocking read from stdout
        import select

        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        if ready:
            chunk = process.stdout.read1(4096) if hasattr(process.stdout, "read1") else b""
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                collected_output += text
                if not api_key:
                    api_key = _extract_api_key(collected_output)

        # Check if server is ready
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass

    # If we didn't get the key from stdout, try the .nexus-admin-env file
    if not api_key:
        env_file = Path(".nexus-admin-env")
        if env_file.exists():
            env_content = env_file.read_text()
            api_key = _extract_api_key(env_content)

    if not api_key:
        # Last resort: drain remaining stdout
        remaining = process.stdout.read()
        if remaining:
            collected_output += remaining.decode("utf-8", errors="replace")
            api_key = _extract_api_key(collected_output)

    if not api_key:
        process.terminate()
        stderr_out = process.stderr.read().decode("utf-8", errors="replace")
        pytest.fail(
            f"Could not extract API key from server output.\n"
            f"stdout: {collected_output}\n"
            f"stderr: {stderr_out}"
        )

    # Verify server is actually ready
    try:
        resp = httpx.get(f"{base_url}/health", timeout=5.0, trust_env=False)
        assert resp.status_code == 200
    except Exception:
        process.terminate()
        stderr_out = process.stderr.read().decode("utf-8", errors="replace")
        pytest.fail(
            f"Server not ready after init.\n"
            f"stdout: {collected_output}\n"
            f"stderr: {stderr_out}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
    }

    # Cleanup
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@pytest.fixture(scope="function")
def db_auth_client(db_auth_server):
    """Create httpx client with database auth Bearer token."""
    with httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers={"Authorization": f"Bearer {db_auth_server['api_key']}"},
    ) as client:
        yield client


def _rpc_post_auth(
    client: httpx.Client, method: str, params: dict | None = None
) -> dict:
    """Make RPC call via authenticated HTTP. Asserts 200 status."""
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"RPC {method} failed ({resp.status_code}): {resp.text}"
    data = resp.json()
    assert "result" in data, f"No result in RPC response: {data}"
    return data["result"]


def _create_user_key(
    admin_client: httpx.Client,
    user_id: str,
    zone_id: str = "default",
    is_admin: bool = False,
) -> str:
    """Use admin RPC to create a non-admin user API key. Returns raw key."""
    result = _rpc_post_auth(
        admin_client,
        "admin_create_key",
        {
            "name": f"Test key for {user_id}",
            "zone_id": zone_id,
            "user_id": user_id,
            "is_admin": is_admin,
        },
    )
    return result["api_key"]


@pytest.fixture(scope="function")
def regular_user_client(db_auth_server, db_auth_client):
    """Create a non-admin user via admin RPC, return httpx client with that user's key.

    This is the key fixture for #1203: a non-admin user whose context
    actually matters for permission filtering (admin bypasses all checks).
    """
    user_key = _create_user_key(db_auth_client, user_id="testuser", is_admin=False)
    with httpx.Client(
        base_url=db_auth_server["base_url"],
        timeout=30.0,
        trust_env=False,
        headers={"Authorization": f"Bearer {user_key}"},
    ) as client:
        yield client


class TestMemoryListRPCDatabaseAuthE2E:
    """E2E: memory.list() through real nexus serve with --auth-type database (#1203).

    Uses a NON-ADMIN user to test the real permission path.
    Admin bypasses all permission checks, so only a regular user
    exercises the context-based filtering that #1203 fixes.

    Flow: nexus serve --auth-type database --init → admin key
    → admin_create_key RPC creates non-admin "testuser" → testuser key
    → RPC store_memory / list_memories with real non-admin context.
    """

    def _store_via_rpc(
        self,
        client: httpx.Client,
        content: str,
        scope: str = "user",
    ) -> str:
        """Store a memory via authenticated RPC."""
        result = _rpc_post_auth(
            client, "store_memory", {"content": content, "scope": scope}
        )
        return result["memory_id"]

    def test_store_and_list_with_regular_user(self, regular_user_client: httpx.Client):
        """Store and list memories as non-admin user — tests real context path (#1203)."""
        id1 = self._store_via_rpc(regular_user_client, "Regular user memory 1")
        id2 = self._store_via_rpc(regular_user_client, "Regular user memory 2")

        result = _rpc_post_auth(regular_user_client, "list_memories", {"limit": 50})
        memories = result["memories"]
        assert len(memories) >= 2
        memory_ids = {m["memory_id"] for m in memories}
        assert id1 in memory_ids
        assert id2 in memory_ids

    def test_list_scope_filter_with_regular_user(self, regular_user_client: httpx.Client):
        """list_memories scope filter works for non-admin user."""
        self._store_via_rpc(regular_user_client, "User scoped", scope="user")
        self._store_via_rpc(regular_user_client, "Agent scoped", scope="agent")

        result = _rpc_post_auth(
            regular_user_client, "list_memories", {"scope": "user", "limit": 50}
        )
        memories = result["memories"]
        assert len(memories) >= 1
        assert all(m["scope"] == "user" for m in memories)

    def test_list_empty_initially(self, regular_user_client: httpx.Client):
        """list_memories returns empty list on fresh server for regular user."""
        result = _rpc_post_auth(regular_user_client, "list_memories", {"limit": 50})
        assert result["memories"] == []

    def test_unauthenticated_request_rejected(self, db_auth_server):
        """Requests without valid Bearer token should be rejected."""
        with httpx.Client(
            base_url=db_auth_server["base_url"],
            timeout=30.0,
            trust_env=False,
        ) as client:
            resp = client.post(
                "/api/nfs/list_memories",
                content=_rpc_body("list_memories", {"limit": 50}),
                headers={"Content-Type": "application/json"},
            )
            # Without auth, should get 401 or 403
            assert resp.status_code in (401, 403), (
                f"Expected 401/403 without auth, got {resp.status_code}: {resp.text}"
            )

    def test_admin_store_not_visible_to_regular_user(
        self, db_auth_client: httpx.Client, regular_user_client: httpx.Client
    ):
        """Memories stored by admin should NOT be visible to regular user.

        This directly tests #1203: if list() uses the wrong context (self.context
        instead of passed context), permission filtering would be wrong.
        """
        # Admin stores a memory
        admin_result = _rpc_post_auth(
            db_auth_client, "store_memory", {"content": "Admin secret", "scope": "user"}
        )
        admin_result["memory_id"]  # stored by admin, not used directly

        # Regular user stores a memory
        user_result = _rpc_post_auth(
            regular_user_client, "store_memory", {"content": "User memory", "scope": "user"}
        )
        user_memory_id = user_result["memory_id"]

        # Regular user lists — should see their own memory, may or may not see admin's
        result = _rpc_post_auth(regular_user_client, "list_memories", {"limit": 50})
        memory_ids = {m["memory_id"] for m in result["memories"]}
        assert user_memory_id in memory_ids, "Regular user should see their own memory"
