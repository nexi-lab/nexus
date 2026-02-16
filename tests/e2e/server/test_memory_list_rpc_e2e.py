"""E2E test for memory.list() RPC dispatch (#1203).

Tests the full RPC path: HTTP POST /api/nfs/list_memories → FastAPI dispatch
→ _handle_list_memories → Memory.list() with correct context.

Uses Starlette TestClient with real NexusFS + SQLAlchemy metadata store
(no Rust/Raft dependency, no async fixtures needed).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def nexus_fs_local(tmp_path: Path):
    """Create a real NexusFS with RaftMetadataStore."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir()
    backend = LocalBackend(root_path=storage_path)
    raft_dir = str(tmp_path / "raft-metadata")
    metadata_store = RaftMetadataStore.embedded(raft_dir)
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{tmp_path / 'records.db'}")
    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )
    yield nx
    nx.close()


@pytest.fixture
def rpc_client(nexus_fs_local: NexusFS, tmp_path: Path, monkeypatch):
    """Create sync TestClient with real FastAPI app and NexusFS."""
    monkeypatch.setenv("NEXUS_ENFORCE_PERMISSIONS", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    from nexus.server.fastapi_server import create_app

    db_url = f"sqlite:///{tmp_path / 'records.db'}"
    app = create_app(nexus_fs=nexus_fs_local, database_url=db_url)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


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


def _rpc_post(client: TestClient, method: str, params: dict | None = None) -> dict:
    """Make RPC call and return parsed response. Asserts 200 status."""
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"RPC {method} failed: {resp.text}"
    data = resp.json()
    assert "result" in data, f"No result in RPC response: {data}"
    return data["result"]


class TestMemoryListRPCE2E:
    """E2E: memory.list() through full RPC dispatch path (#1203).

    All operations go through RPC to test the real server path.
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
        result = _rpc_post(client, "store_memory", params)
        return result["memory_id"]

    def test_list_memories_rpc_returns_stored_memories(self, rpc_client: TestClient):
        """Store memories via RPC, list them via RPC — full path test."""
        id1 = self._store_via_rpc(rpc_client, "User prefers dark mode")
        id2 = self._store_via_rpc(rpc_client, "Agent learned Python patterns", scope="agent")
        id3 = self._store_via_rpc(rpc_client, "Favorite color is blue", memory_type="preference")

        result = _rpc_post(rpc_client, "list_memories", {"limit": 50})
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

        result = _rpc_post(rpc_client, "list_memories", {"scope": "user", "limit": 50})
        memories = result["memories"]
        assert len(memories) >= 1
        assert all(m["scope"] == "user" for m in memories)

    def test_list_memories_rpc_filters_by_state(self, rpc_client: TestClient):
        """RPC list_memories with state filter works correctly."""
        active_id = self._store_via_rpc(rpc_client, "Active memory", state="active")
        inactive_id = self._store_via_rpc(rpc_client, "Inactive memory", state="inactive")

        result = _rpc_post(rpc_client, "list_memories", {"state": "active", "limit": 50})
        memories = result["memories"]
        memory_ids = {m["memory_id"] for m in memories}
        assert active_id in memory_ids
        assert inactive_id not in memory_ids

    def test_list_memories_rpc_respects_limit(self, rpc_client: TestClient):
        """RPC list_memories respects the limit parameter."""
        for i in range(5):
            self._store_via_rpc(rpc_client, f"Memory {i}")

        result = _rpc_post(rpc_client, "list_memories", {"limit": 2})
        assert len(result["memories"]) == 2
