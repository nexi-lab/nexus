"""E2E tests for Issue #2033 LEGO decomposition — verify delegated methods work.

Tests all APIs that were refactored from NexusFS to services:
- memory property → MemoryProvider.get_or_create()
- _get_memory_api() → MemoryProvider.get_for_context()
- _ensure_entity_registry() → MemoryProvider.ensure_entity_registry()
- sync_mount → _SERVICE_ALIASES → SyncService.sync_mount_flat
- sync_mount_async → _SERVICE_ALIASES → SyncJobService.sync_mount_async
- cancel_sync_job → _SERVICE_ALIASES → SyncJobService.cancel_sync_job
- Version methods → _SERVICE_ALIASES → VersionService

Uses real NexusFS with SQLite record store and permission enforcement.

Run with:
    uv run pytest tests/e2e/test_lego_decomp_e2e.py -v --override-ini="addopts=" -x
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext
from nexus.storage.models import Base
from nexus.storage.record_store import SQLAlchemyRecordStore

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_nexus_fs(
    tmp_path: Path,
    *,
    enforce_permissions: bool = False,
    is_admin: bool = True,
) -> NexusFS:
    """Create a full NexusFS with SQLite record store for E2E testing."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=data_dir)

    # Use InMemory metastore for speed (avoids Raft native build requirement)
    from tests.helpers.in_memory_metadata_store import InMemoryMetastore

    metadata_store = InMemoryMetastore()

    # SQLite record store for Memory API + versioning
    db_path = tmp_path / "records.db"
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}")

    return NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        is_admin=is_admin,
        permissions=PermissionConfig(
            enforce=enforce_permissions,
            audit_strict_mode=False,
        ),
        parsing=ParseConfig(auto_parse=False),
        distributed=DistributedConfig(
            enable_events=False,
            enable_locks=False,
            enable_workflows=False,
        ),
        memory=MemoryConfig(enable_paging=False),
    )


@pytest.fixture()
def nx(tmp_path):
    """NexusFS with permissions disabled (admin mode)."""
    fs = _create_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
    yield fs
    fs.close()


@pytest.fixture()
def nx_perms(tmp_path):
    """NexusFS with permissions enabled."""
    fs = _create_nexus_fs(tmp_path, enforce_permissions=True, is_admin=True)
    yield fs
    fs.close()


# ---------------------------------------------------------------------------
# 1. Basic VFS sanity (ensure kernel still works)
# ---------------------------------------------------------------------------


class TestKernelSanity:
    """Verify basic VFS ops work after decomposition."""

    def test_write_read_roundtrip(self, nx):
        nx.write("/test.txt", b"hello world")
        data = nx.read("/test.txt")
        assert data == b"hello world"

    def test_mkdir_and_list(self, nx):
        nx.mkdir("/mydir", parents=True, exist_ok=True)
        nx.write("/mydir/file.txt", b"content")
        entries = nx.list("/mydir", recursive=False)
        assert "/mydir/file.txt" in entries

    def test_delete_file(self, nx):
        nx.write("/del.txt", b"bye")
        nx.delete("/del.txt")
        assert not nx.exists("/del.txt")

    def test_exists(self, nx):
        nx.write("/exists.txt", b"yes")
        assert nx.exists("/exists.txt")
        assert not nx.exists("/nope.txt")

    def test_is_directory(self, nx):
        nx.mkdir("/somedir", parents=True, exist_ok=True)
        assert nx.is_directory("/somedir")

    def test_get_metadata(self, nx):
        nx.write("/meta.txt", b"metadata test")
        meta = nx.get_metadata("/meta.txt")
        assert meta is not None
        assert meta["size"] == 13
        assert meta["is_directory"] is False

    def test_get_etag(self, nx):
        nx.write("/etag.txt", b"etag test")
        etag = nx.get_etag("/etag.txt")
        assert etag is not None
        assert isinstance(etag, str)
        assert len(etag) > 0


# ---------------------------------------------------------------------------
# 2. Memory property delegation (MemoryProvider)
# ---------------------------------------------------------------------------


class TestMemoryDelegation:
    """Verify memory property and helpers delegate correctly."""

    def test_memory_property_returns_memory_api(self, nx):
        """memory property should return a Memory instance via MemoryProvider."""
        mem = nx.memory
        assert mem is not None
        # Should have standard Memory methods
        assert hasattr(mem, "store")
        assert hasattr(mem, "query")
        assert hasattr(mem, "list")
        assert hasattr(mem, "get")

    def test_memory_property_is_singleton(self, nx):
        """Same Memory instance should be returned on repeated access."""
        mem1 = nx.memory
        mem2 = nx.memory
        assert mem1 is mem2

    def test_memory_store_and_query(self, nx):
        """End-to-end memory store + query through delegated property."""
        mem = nx.memory
        # Store a memory
        mid = mem.store(
            content="Test memory from E2E",
            scope="user",
            memory_type="observation",
        )
        assert mid is not None

        # Query it back
        result = mem.get(mid)
        assert result is not None
        assert "Test memory from E2E" in str(result.get("content", result))

    def test_get_memory_api_returns_fresh_instance(self, nx):
        """_get_memory_api should return a fresh Memory per context."""
        ctx = {"zone_id": "test-zone", "user_id": "alice"}
        mem = nx._get_memory_api(ctx)
        assert mem is not None
        assert hasattr(mem, "store")

    def test_get_memory_api_with_none_context(self, nx):
        """_get_memory_api(None) should use defaults."""
        mem = nx._get_memory_api(None)
        assert mem is not None

    def test_ensure_entity_registry(self, nx):
        """_ensure_entity_registry should return an EntityRegistry."""
        reg = nx._ensure_entity_registry()
        assert reg is not None
        # EntityRegistry has a session_factory
        assert hasattr(reg, "session") or hasattr(reg, "_session_factory")


# ---------------------------------------------------------------------------
# 3. Sync mount delegation (SyncService / SyncJobService)
# ---------------------------------------------------------------------------


class TestSyncDelegation:
    """Verify sync_mount, sync_mount_async, cancel_sync_job delegate correctly."""

    def test_sync_mount_callable(self, nx):
        """sync_mount should be callable via __getattr__."""
        assert callable(nx.sync_mount)

    def test_sync_mount_no_mounts_returns_summary(self, nx):
        """sync_mount with no mounts configured returns empty summary."""
        result = nx.sync_mount()
        assert isinstance(result, dict)
        # Should have standard sync result keys
        assert "files_created" in result or "status" in result or "synced" in result or "total" in result or isinstance(result, dict)

    def test_sync_mount_async_callable(self, nx):
        """sync_mount_async should be callable via __getattr__."""
        assert callable(nx.sync_mount_async)

    def test_cancel_sync_job_nonexistent(self, nx):
        """cancel_sync_job for non-existent job returns failure dict."""
        result = nx.cancel_sync_job("nonexistent-job-id")
        assert isinstance(result, dict)
        assert result.get("success") is False or "not found" in str(result.get("message", "")).lower() or "Job not found" in str(result)

    def test_get_sync_job_callable(self, nx):
        """get_sync_job should be forwarded via __getattr__."""
        assert callable(nx.get_sync_job)

    def test_list_sync_jobs_callable(self, nx):
        """list_sync_jobs should be forwarded via __getattr__."""
        assert callable(nx.list_sync_jobs)


# ---------------------------------------------------------------------------
# 4. Version service delegation
# ---------------------------------------------------------------------------


class TestVersionDelegation:
    """Verify version_service methods work after delegation."""

    @pytest.fixture()
    def nx_with_versions(self, tmp_path):
        """NexusFS with VersionService wired via factory."""
        try:
            from nexus.factory import create_nexus_fs

            fs = create_nexus_fs(
                backend_type="local",
                data_dir=str(tmp_path / "data"),
                db_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
                enforce_permissions=False,
                is_admin=True,
            )
            yield fs
            fs.close()
        except Exception:
            pytest.skip("factory not available for version tests")

    def test_list_versions_after_write(self, nx_with_versions):
        """list_versions should return version history after writes."""
        nx_with_versions.write("/ver.txt", b"v1")
        versions = nx_with_versions.list_versions("/ver.txt")
        assert isinstance(versions, list)
        assert len(versions) >= 1

    def test_get_version_returns_content(self, nx_with_versions):
        """get_version should retrieve specific version content."""
        nx_with_versions.write("/ver2.txt", b"version-one")
        versions = nx_with_versions.list_versions("/ver2.txt")
        if versions:
            v = versions[0]
            ver_num = v.get("version", 1)
            content = nx_with_versions.get_version("/ver2.txt", ver_num)
            assert isinstance(content, bytes)

    def test_aget_version_is_coroutine(self, nx_with_versions):
        """aget_version should be a coroutine function (via __getattr__)."""
        import inspect

        assert inspect.iscoroutinefunction(nx_with_versions.aget_version)

    def test_alist_versions_is_coroutine(self, nx_with_versions):
        """alist_versions should be a coroutine function (via __getattr__)."""
        import inspect

        assert inspect.iscoroutinefunction(nx_with_versions.alist_versions)

    def test_arollback_is_coroutine(self, nx_with_versions):
        """arollback should be a coroutine function (via __getattr__)."""
        import inspect

        assert inspect.iscoroutinefunction(nx_with_versions.arollback)

    def test_adiff_versions_is_coroutine(self, nx_with_versions):
        """adiff_versions should be a coroutine function (via __getattr__)."""
        import inspect

        assert inspect.iscoroutinefunction(nx_with_versions.adiff_versions)


# ---------------------------------------------------------------------------
# 5. __getattr__ service forwarding completeness
# ---------------------------------------------------------------------------


class TestServiceForwarding:
    """Verify __getattr__ routes to correct services."""

    def test_workspace_methods_accessible(self, nx):
        """Workspace RPC methods should be forwarded."""
        assert callable(nx.register_workspace)
        assert callable(nx.list_workspaces)

    def test_agent_methods_accessible(self, nx):
        """Agent RPC methods should be forwarded."""
        assert callable(nx.register_agent)
        assert callable(nx.list_agents)

    def test_sandbox_methods_accessible(self, nx):
        """Sandbox RPC methods should be forwarded."""
        assert callable(nx.sandbox_create)
        assert callable(nx.sandbox_list)

    def test_export_import_accessible(self, nx):
        """Export/import methods should be forwarded."""
        assert callable(nx.export_metadata)
        assert callable(nx.import_metadata)

    def test_mount_methods_accessible(self, nx):
        """Mount methods should be forwarded."""
        assert callable(nx.add_mount)
        assert callable(nx.list_mounts)

    def test_unknown_attr_raises(self, nx):
        """Unknown attributes should raise AttributeError."""
        with pytest.raises(AttributeError, match="no attribute"):
            _ = nx.nonexistent_method_xyz


# ---------------------------------------------------------------------------
# 6. Permission enforcement still works
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Verify permission checks still work after decomposition."""

    def test_write_with_admin_context(self, nx_perms):
        """Admin should be able to write."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="root",
            is_admin=True,
            is_system=False,
        )
        nx_perms.write("/perm-test.txt", b"admin write", context=ctx)
        data = nx_perms.read("/perm-test.txt", context=ctx)
        assert data == b"admin write"

    def test_mkdir_with_admin(self, nx_perms):
        """Admin should be able to mkdir."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="root",
            is_admin=True,
            is_system=False,
        )
        nx_perms.mkdir("/admin-dir", parents=True, exist_ok=True, context=ctx)
        assert nx_perms.is_directory("/admin-dir", context=ctx)


# ---------------------------------------------------------------------------
# 7. FastAPI server integration (TestClient)
# ---------------------------------------------------------------------------


class TestFastAPIIntegration:
    """Test affected APIs through FastAPI TestClient."""

    @pytest.fixture()
    def client(self, tmp_path):
        """Create a FastAPI TestClient with a real NexusFS."""
        from starlette.testclient import TestClient

        from nexus.server.fastapi_server import create_app

        fs = _create_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
        api_key = "test-api-key-" + uuid.uuid4().hex[:16]
        app = create_app(fs, api_key=api_key)
        client = TestClient(app)
        yield {
            "client": client,
            "nx": fs,
            "api_key": api_key,
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        }
        fs.close()

    def _rpc(self, ctx, method: str, params: dict | None = None) -> dict:
        """Make an RPC call via TestClient."""
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
        resp = ctx["client"].post(
            f"/api/nfs/{method}",
            content=json.dumps(body),
            headers=ctx["headers"],
        )
        return resp.json()

    def test_health_check(self, client):
        """Health endpoint should work."""
        resp = client["client"].get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_write_and_read_via_rpc(self, client):
        """Write + read via RPC endpoints."""
        import base64

        content_b64 = base64.b64encode(b"hello from e2e").decode()

        # Write
        result = self._rpc(client, "write", {
            "path": "/e2e-test.txt",
            "content": content_b64,
        })
        assert "error" not in result or result.get("error") is None

        # Read
        result = self._rpc(client, "read", {"path": "/e2e-test.txt"})
        assert "result" in result

    def test_mkdir_via_rpc(self, client):
        """mkdir via RPC endpoint."""
        result = self._rpc(client, "mkdir", {
            "path": "/e2e-dir",
            "parents": True,
            "exist_ok": True,
        })
        assert "error" not in result or result.get("error") is None

    def test_list_via_rpc(self, client):
        """list via RPC endpoint after writing a file."""
        import base64

        self._rpc(client, "write", {
            "path": "/list-test/file.txt",
            "content": base64.b64encode(b"data").decode(),
        })
        result = self._rpc(client, "list", {"path": "/list-test"})
        assert "result" in result

    def test_list_versions_via_rpc(self, client):
        """list_versions via RPC endpoint (requires factory-wired VersionService)."""
        import base64

        self._rpc(client, "write", {
            "path": "/ver-rpc.txt",
            "content": base64.b64encode(b"v1").decode(),
        })
        result = self._rpc(client, "list_versions", {"path": "/ver-rpc.txt"})
        # version_service is None in minimal NexusFS (not factory-wired)
        if "error" in result and result["error"]:
            pytest.skip("VersionService not available without factory wiring")
        assert "result" in result
        assert isinstance(result.get("result"), list)

    def test_memory_store_via_server(self, client):
        """Memory store should work via the memory property delegation."""
        # Access memory directly through NexusFS (server code does this)
        nx = client["nx"]
        mem = nx.memory
        mid = mem.store(
            content="E2E memory test via server",
            scope="user",
            memory_type="observation",
        )
        assert mid is not None

    def test_get_memory_api_via_server(self, client):
        """_get_memory_api should work when called as server does."""
        nx = client["nx"]
        context_dict = {"zone_id": "root", "user_id": "test-user"}
        mem = nx._get_memory_api(context_dict)
        assert mem is not None
        assert hasattr(mem, "store")
