"""E2E test for path unscoping through FastAPI server (#1202).

Tests the full RPC path: HTTP POST /api/nfs/{method} → FastAPI dispatch
→ _handle_* → NexusFS.{method}() → path unscoping → client response.

Verifies that internal zone/tenant/user prefixes are stripped from paths
before returning them to API clients via the real FastAPI server stack.

Strategy: Write files using zone-scoped internal paths (simulating what
provision_user creates), then verify RPC responses return clean paths.

Uses Starlette TestClient with real NexusFS + RaftMetadataStore.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
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
    metadata_store = RaftMetadataStore.local(raft_dir)
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


def _assert_no_internal_prefix(path: str, label: str = "") -> None:
    """Assert a path does not have internal zone/tenant/user prefixes."""
    prefix_label = f" ({label})" if label else ""
    assert not path.startswith("/tenant:"), f"Path{prefix_label} has /tenant: prefix: {path}"
    assert not path.startswith("/zone/"), f"Path{prefix_label} has /zone/ prefix: {path}"


@pytest.mark.e2e
class TestZoneScopedPathUnscopingE2E:
    """E2E: Reproduce the actual bug (#1202) with zone-scoped internal paths.

    Writes files directly to zone/user-prefixed paths (as provision_user does),
    then verifies RPC responses strip the internal prefixes.
    """

    def _write_zone_scoped_file(
        self,
        nexus_fs: NexusFS,
        zone_id: str,
        user_id: str,
        resource_path: str,
        content: bytes,
    ) -> None:
        """Write a file using the internal zone-scoped path format.

        This simulates what happens when provision_user creates files:
        /zone/{zone_id}/user:{user_id}/{resource_path}
        """
        internal_path = f"/zone/{zone_id}/user:{user_id}/{resource_path}"
        nexus_fs.write(internal_path, content)

    def test_list_strips_zone_prefix_from_provisioned_paths(
        self, rpc_client: TestClient, nexus_fs_local: NexusFS
    ) -> None:
        """Issue #1202: list('/') must not return /zone/... prefixed paths."""
        # Write files using internal zone-scoped paths (as provision_user does)
        self._write_zone_scoped_file(
            nexus_fs_local,
            "default",
            "alice",
            "workspace/hello.txt",
            b"Hello!",
        )
        self._write_zone_scoped_file(
            nexus_fs_local,
            "default",
            "alice",
            "workspace/data.csv",
            b"a,b,c",
        )

        # List root via RPC — this is what RemoteNexusFS.list('/') calls
        result = _rpc_post(rpc_client, "list", {"path": "/", "recursive": True})
        files = result["files"]

        # Extract paths
        paths = [f["path"] if isinstance(f, dict) else f for f in files]

        # THE BUG: paths should NOT contain /zone/default/user:alice/...
        for p in paths:
            _assert_no_internal_prefix(p, "list result")

        # Verify the actual expected clean paths are present
        assert any("workspace/hello.txt" in p for p in paths), (
            f"Expected workspace/hello.txt in paths: {paths}"
        )

    def test_list_strips_zone_prefix_from_detail_dicts(
        self, rpc_client: TestClient, nexus_fs_local: NexusFS
    ) -> None:
        """list(details=True) strips /zone/ prefix from path keys in dicts."""
        self._write_zone_scoped_file(
            nexus_fs_local,
            "acme",
            "bob",
            "workspace/report.md",
            b"# Report",
        )

        result = _rpc_post(
            rpc_client,
            "list",
            {"path": "/", "recursive": True, "details": True},
        )
        files = result["files"]
        assert len(files) >= 1

        for f in files:
            if isinstance(f, dict) and "path" in f:
                _assert_no_internal_prefix(f["path"], "list detail path")

    def test_glob_strips_zone_prefix(self, rpc_client: TestClient, nexus_fs_local: NexusFS) -> None:
        """glob() strips internal prefixes from zone-scoped matches."""
        self._write_zone_scoped_file(
            nexus_fs_local,
            "default",
            "alice",
            "workspace/app.py",
            b"import os",
        )
        self._write_zone_scoped_file(
            nexus_fs_local,
            "default",
            "alice",
            "workspace/test_app.py",
            b"def test(): pass",
        )

        result = _rpc_post(rpc_client, "glob", {"pattern": "*.py", "path": "/"})
        matches = result["matches"]
        assert len(matches) >= 2

        for path in matches:
            _assert_no_internal_prefix(path, "glob match")

    def test_grep_strips_zone_prefix(self, rpc_client: TestClient, nexus_fs_local: NexusFS) -> None:
        """grep() strips internal prefixes from zone-scoped results."""
        self._write_zone_scoped_file(
            nexus_fs_local,
            "default",
            "alice",
            "workspace/search_target.py",
            b"import os\nimport sys",
        )

        result = _rpc_post(
            rpc_client,
            "grep",
            {"pattern": "import", "path": "/"},
        )
        results = result["results"]
        assert len(results) >= 1

        for r in results:
            if isinstance(r, dict):
                for key in ("file", "path"):
                    if key in r and isinstance(r[key], str):
                        _assert_no_internal_prefix(r[key], f"grep {key}")


@pytest.mark.e2e
class TestTenantPrefixUnscopingE2E:
    """E2E: Verify legacy /tenant: prefix paths are also stripped.

    Directly inserts metadata with /tenant: prefix to simulate legacy data.
    """

    def test_list_strips_tenant_prefix(
        self, rpc_client: TestClient, nexus_fs_local: NexusFS
    ) -> None:
        """Legacy /tenant:default/... paths get stripped by list()."""
        # Write using legacy tenant-prefixed path
        nexus_fs_local.write(
            "/tenant:default/connector/gcs_demo/auto-test.txt",
            b"test data",
        )

        result = _rpc_post(rpc_client, "list", {"path": "/", "recursive": True})
        files = result["files"]
        paths = [f["path"] if isinstance(f, dict) else f for f in files]

        for p in paths:
            _assert_no_internal_prefix(p, "list with tenant prefix")

        # Should see the clean path
        assert any("connector/gcs_demo/auto-test.txt" in p for p in paths), (
            f"Expected connector/gcs_demo/auto-test.txt in paths: {paths}"
        )
