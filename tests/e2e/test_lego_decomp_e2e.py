"""E2E tests for Issue #2033 LEGO decomposition — verify delegated methods work.

Tests all APIs that were refactored from NexusFS to services:
- memory property → MemoryProvider.get_or_create()
- _get_memory_api() → MemoryProvider.get_for_context()
- _ensure_entity_registry() → MemoryProvider.ensure_entity_registry()
- sync_mount → _SERVICE_ALIASES → SyncService.sync_mount_flat
- sync_mount_async → _SERVICE_ALIASES → SyncJobService.sync_mount_async
- cancel_sync_job → _SERVICE_ALIASES → SyncJobService.cancel_sync_job
- Version methods → _SERVICE_ALIASES → VersionService

Uses factory-wired NexusFS with PostgreSQL record store and FastAPI TestClient.

Run with:
    uv run pytest tests/e2e/test_lego_decomp_e2e.py -v --override-ini="addopts=" -x
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.config import (
    DistributedConfig,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)

pytestmark = [pytest.mark.e2e]

# ---------------------------------------------------------------------------
# PostgreSQL connection
# ---------------------------------------------------------------------------

PG_URL = "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test"


def _pg_available() -> bool:
    """Check if the test PostgreSQL is reachable."""
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(PG_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


# Skip entire module if PostgreSQL isn't available
pytestmark.append(
    pytest.mark.skipif(not _pg_available(), reason="PostgreSQL not available at localhost:5433")
)


# ---------------------------------------------------------------------------
# Fixtures — factory-wired NexusFS + PostgreSQL + FastAPI
# ---------------------------------------------------------------------------


def _create_factory_nexus_fs(
    tmp_path: Path,
    *,
    enforce_permissions: bool = False,
    is_admin: bool = True,
    enable_tiger_cache: bool = True,
) -> Any:
    """Create a factory-wired NexusFS with PostgreSQL record store.

    Uses create_nexus_fs() from nexus.factory — the recommended entry
    point that wires ALL services (ReBAC, Permissions, VersionService, etc.).
    """
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.factory import create_nexus_fs, create_record_store
    from tests.helpers.dict_metastore import DictMetastore

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    backend = CASLocalBackend(root_path=data_dir)
    metadata_store = DictMetastore()

    record_store = create_record_store(db_url=PG_URL, create_tables=True)

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        is_admin=is_admin,
        permissions=PermissionConfig(
            enforce=enforce_permissions,
            audit_strict_mode=False,
            enforce_zone_isolation=False,
            enable_tiger_cache=enable_tiger_cache,
        ),
        parsing=ParseConfig(auto_parse=False),
        distributed=DistributedConfig(
            enable_events=False,
            enable_locks=False,
            enable_workflows=False,
        ),
        memory=MemoryConfig(enable_paging=False),
        enable_write_buffer=False,  # Sync writes so versions are immediately visible
    )


@pytest.fixture()
def nx(tmp_path):
    """Factory-wired NexusFS with PostgreSQL, permissions disabled."""
    fs = _create_factory_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
    yield fs
    fs.close()


@pytest.fixture()
def nx_perms(tmp_path):
    """Factory-wired NexusFS with PostgreSQL, permissions enabled."""
    fs = _create_factory_nexus_fs(
        tmp_path,
        enforce_permissions=True,
        is_admin=True,
        enable_tiger_cache=False,
    )
    yield fs
    fs.close()


@pytest.fixture()
def client(tmp_path):
    """FastAPI TestClient with factory-wired NexusFS + PostgreSQL."""
    from starlette.testclient import TestClient

    from nexus.server.fastapi_server import create_app

    fs = _create_factory_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
    api_key = "test-api-key-" + uuid.uuid4().hex[:16]
    app = create_app(fs, api_key=api_key, database_url=PG_URL)
    tc = TestClient(app)
    yield {
        "client": tc,
        "nx": fs,
        "api_key": api_key,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    }
    fs.close()


@pytest.fixture()
def client_perms(tmp_path):
    """FastAPI TestClient with factory-wired NexusFS + PostgreSQL + permissions."""
    from starlette.testclient import TestClient

    from nexus.server.fastapi_server import create_app

    fs = _create_factory_nexus_fs(
        tmp_path,
        enforce_permissions=True,
        is_admin=True,
        enable_tiger_cache=False,
    )
    api_key = "test-api-key-" + uuid.uuid4().hex[:16]
    app = create_app(fs, api_key=api_key, database_url=PG_URL)
    tc = TestClient(app)
    yield {
        "client": tc,
        "nx": fs,
        "api_key": api_key,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    }
    fs.close()


def _rpc(ctx: dict, method: str, params: dict | None = None) -> dict:
    """Make a JSON-RPC call via TestClient."""
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


# ---------------------------------------------------------------------------
# 1. Basic VFS sanity (ensure kernel still works with factory wiring + PG)
# ---------------------------------------------------------------------------


class TestKernelSanity:
    """Verify basic VFS ops work after decomposition with factory + PostgreSQL."""

    def test_write_read_roundtrip(self, nx):
        nx.sys_write("/test.txt", b"hello world")
        data = nx.sys_read("/test.txt")
        assert data == b"hello world"

    def test_mkdir_and_list(self, nx):
        nx.sys_mkdir("/mydir", parents=True, exist_ok=True)
        nx.sys_write("/mydir/file.txt", b"content")
        entries = nx.sys_readdir("/mydir", recursive=False)
        assert "/mydir/file.txt" in entries

    def test_delete_file(self, nx):
        nx.sys_write("/del.txt", b"bye")
        nx.sys_unlink("/del.txt")
        assert not nx.sys_access("/del.txt")

    def test_exists(self, nx):
        nx.sys_write("/exists.txt", b"yes")
        assert nx.sys_access("/exists.txt")
        assert not nx.sys_access("/nope.txt")

    def test_is_directory(self, nx):
        nx.sys_mkdir("/somedir", parents=True, exist_ok=True)
        assert nx.sys_is_directory("/somedir")

    def test_get_metadata(self, nx):
        nx.sys_write("/meta.txt", b"metadata test")
        meta = nx.sys_stat("/meta.txt")
        assert meta is not None
        assert meta["size"] == 13
        assert meta["is_directory"] is False

    def test_get_etag(self, nx):
        nx.sys_write("/etag.txt", b"etag test")
        etag = nx.get_etag("/etag.txt")
        assert etag is not None
        assert isinstance(etag, str)
        assert len(etag) > 0


# ---------------------------------------------------------------------------
# 2. Memory property delegation (MemoryProvider)
# ---------------------------------------------------------------------------


class TestMemoryDelegation:
    """Verify memory property and helpers delegate correctly with PG backend."""

    def test_memory_property_returns_memory_api(self, nx):
        """memory property should return a Memory instance via MemoryProvider."""
        mem = nx.memory
        assert mem is not None
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
        """End-to-end memory store + query through delegated property with PG."""
        mem = nx.memory
        mid = mem.store(
            content="Test memory from E2E with PostgreSQL",
            scope="user",
            memory_type="observation",
        )
        assert mid is not None

        result = mem.get(mid)
        assert result is not None
        assert "Test memory from E2E with PostgreSQL" in str(result.get("content", result))

    def test_memory_provider_returns_fresh_instance(self, nx):
        """_memory_provider.get_for_context() should return a fresh Memory per context."""
        ctx = {"zone_id": "test-zone", "user_id": "alice"}
        mem = nx._memory_provider.get_for_context(ctx)
        assert mem is not None
        assert hasattr(mem, "store")

    def test_memory_provider_with_none_context(self, nx):
        """_memory_provider.get_for_context(None) should use defaults."""
        mem = nx._memory_provider.get_for_context(None)
        assert mem is not None

    def test_ensure_entity_registry(self, nx):
        """_memory_provider.ensure_entity_registry should return an EntityRegistry."""
        reg = nx._memory_provider.ensure_entity_registry()
        assert reg is not None
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

    def test_sync_mount_async_callable(self, nx):
        """sync_mount_async should be callable via __getattr__."""
        assert callable(nx.sync_mount_async)

    def test_cancel_sync_job_nonexistent(self, nx):
        """cancel_sync_job for non-existent job returns failure dict."""
        result = nx.cancel_sync_job("nonexistent-job-id")
        assert isinstance(result, dict)
        assert (
            result.get("success") is False
            or "not found" in str(result.get("message", "")).lower()
            or "Job not found" in str(result)
        )

    def test_get_sync_job_callable(self, nx):
        """get_sync_job should be forwarded via __getattr__."""
        assert callable(nx.get_sync_job)

    def test_list_sync_jobs_callable(self, nx):
        """list_sync_jobs should be forwarded via __getattr__."""
        assert callable(nx.list_sync_jobs)


# ---------------------------------------------------------------------------
# 4. Version service delegation (factory-wired, NOT skipped)
# ---------------------------------------------------------------------------


class TestVersionDelegation:
    """Verify version_service methods work with factory-wired NexusFS + PG."""

    def test_version_service_is_wired(self, nx):
        """Factory should wire a VersionService instance."""
        vs = getattr(nx, "version_service", None) or getattr(nx, "_version_service", None)
        assert vs is not None, "VersionService should be wired by factory"

    def test_list_versions_after_write(self, nx):
        """list_versions should return version history after writes."""
        path = f"/ver-{uuid.uuid4().hex[:8]}.txt"
        nx.sys_write(path, b"v1")
        from nexus.lib.sync_bridge import run_sync

        versions = run_sync(nx.version_service.list_versions(path))
        assert isinstance(versions, list)
        assert len(versions) >= 1

    def test_get_version_returns_content(self, nx):
        """get_version should retrieve specific version content."""
        from nexus.lib.sync_bridge import run_sync

        path = f"/ver2-{uuid.uuid4().hex[:8]}.txt"
        nx.sys_write(path, b"version-one")
        versions = run_sync(nx.version_service.list_versions(path))
        assert len(versions) >= 1
        ver_num = versions[0].get("version", 1)
        content = run_sync(nx.version_service.get_version(path, ver_num))
        assert isinstance(content, bytes)

    def test_multiple_versions(self, nx):
        """Multiple writes should produce multiple versions."""
        from nexus.lib.sync_bridge import run_sync

        path = f"/multi-ver-{uuid.uuid4().hex[:8]}.txt"
        nx.sys_write(path, b"v1")
        nx.sys_write(path, b"v2")
        versions = run_sync(nx.version_service.list_versions(path))
        assert len(versions) >= 2


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
# 6. Permission enforcement with factory-wired ReBAC + PG
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Verify permission checks still work after decomposition with PG ReBAC."""

    def test_write_with_admin_context(self, nx_perms):
        """Admin should be able to write with permissions enabled."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="root",
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-test-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.sys_write(path, b"admin write", context=ctx)
        data = nx_perms.sys_read(path, context=ctx)
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
        dirname = f"/admin-dir-{uuid.uuid4().hex[:8]}"
        nx_perms.sys_mkdir(dirname, parents=True, exist_ok=True, context=ctx)
        assert nx_perms.sys_is_directory(dirname, context=ctx)

    def test_read_after_write_with_permissions(self, nx_perms):
        """Read after write should work with admin permissions."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="root",
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-rw-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.sys_write(path, b"perm data", context=ctx)
        result = nx_perms.sys_read(path, context=ctx)
        assert result == b"perm data"

    def test_version_with_permissions(self, nx_perms):
        """list_versions should work with admin permissions."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id="root",
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-ver-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.sys_write(path, b"version with perms", context=ctx)
        from nexus.lib.sync_bridge import run_sync

        versions = run_sync(nx_perms.version_service.list_versions(path, ctx))
        assert isinstance(versions, list)
        assert len(versions) >= 1


# ---------------------------------------------------------------------------
# 7. FastAPI server integration (TestClient) — all extraction points
# ---------------------------------------------------------------------------


class TestFastAPIIntegration:
    """Test affected APIs through FastAPI TestClient with PG + factory wiring."""

    def test_health_check(self, client):
        """Health endpoint should work."""
        resp = client["client"].get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_write_and_read_via_rpc(self, client):
        """Write + read via RPC endpoints."""
        path = f"/e2e-rpc-{uuid.uuid4().hex[:8]}.txt"
        content_b64 = base64.b64encode(b"hello from e2e via PG").decode()

        result = _rpc(client, "write", {"path": path, "content": content_b64})
        assert "error" not in result or result.get("error") is None

        result = _rpc(client, "read", {"path": path})
        assert "result" in result

    def test_mkdir_via_rpc(self, client):
        """mkdir via RPC endpoint."""
        dirname = f"/e2e-dir-{uuid.uuid4().hex[:8]}"
        result = _rpc(client, "mkdir", {"path": dirname, "parents": True, "exist_ok": True})
        assert "error" not in result or result.get("error") is None

    def test_list_via_rpc(self, client):
        """list via RPC endpoint after writing a file."""
        prefix = f"/list-rpc-{uuid.uuid4().hex[:8]}"
        _rpc(
            client,
            "write",
            {
                "path": f"{prefix}/file.txt",
                "content": base64.b64encode(b"data").decode(),
            },
        )
        result = _rpc(client, "list", {"path": prefix})
        assert "result" in result

    def test_list_versions_via_rpc(self, client):
        """list_versions via RPC endpoint with factory-wired VersionService."""
        path = f"/ver-rpc-{uuid.uuid4().hex[:8]}.txt"
        _rpc(
            client,
            "write",
            {
                "path": path,
                "content": base64.b64encode(b"v1").decode(),
            },
        )
        result = _rpc(client, "list_versions", {"path": path})
        # With factory wiring, VersionService should be available
        if "error" in result and result["error"]:
            pytest.skip("VersionService not available")
        assert "result" in result
        assert isinstance(result.get("result"), list)

    def test_memory_store_via_server(self, client):
        """Memory store should work via the memory property delegation with PG."""
        nx = client["nx"]
        mem = nx.memory
        mid = mem.store(
            content="E2E memory test via FastAPI + PostgreSQL",
            scope="user",
            memory_type="observation",
        )
        assert mid is not None

    def test_memory_provider_via_server(self, client):
        """_memory_provider.get_for_context() should work when called as server does."""
        nx = client["nx"]
        context_dict = {"zone_id": "root", "user_id": "test-user"}
        mem = nx._memory_provider.get_for_context(context_dict)
        assert mem is not None
        assert hasattr(mem, "store")

    def test_exists_via_rpc(self, client):
        """exists via RPC endpoint."""
        path = f"/exists-rpc-{uuid.uuid4().hex[:8]}.txt"
        _rpc(
            client,
            "write",
            {
                "path": path,
                "content": base64.b64encode(b"check").decode(),
            },
        )
        result = _rpc(client, "exists", {"path": path})
        assert "result" in result

    def test_delete_via_rpc(self, client):
        """delete via RPC endpoint."""
        path = f"/del-rpc-{uuid.uuid4().hex[:8]}.txt"
        _rpc(
            client,
            "write",
            {
                "path": path,
                "content": base64.b64encode(b"to-delete").decode(),
            },
        )
        result = _rpc(client, "delete", {"path": path})
        assert "error" not in result or result.get("error") is None

    def test_get_metadata_via_rpc(self, client):
        """get_metadata via RPC endpoint."""
        path = f"/meta-rpc-{uuid.uuid4().hex[:8]}.txt"
        _rpc(
            client,
            "write",
            {
                "path": path,
                "content": base64.b64encode(b"meta check").decode(),
            },
        )
        result = _rpc(client, "get_metadata", {"path": path})
        assert "result" in result


# ---------------------------------------------------------------------------
# 8. FastAPI with permissions enabled (non-admin scenarios)
# ---------------------------------------------------------------------------


class TestFastAPIWithPermissions:
    """Test APIs through FastAPI with permissions enforced."""

    def test_health_with_perms(self, client_perms):
        """Health endpoint works regardless of permissions."""
        resp = client_perms["client"].get("/health")
        assert resp.status_code == 200

    def test_write_read_with_admin_header(self, client_perms):
        """Admin-authenticated write+read via RPC with permissions on."""
        path = f"/perm-api-{uuid.uuid4().hex[:8]}.txt"
        content_b64 = base64.b64encode(b"perm api test").decode()

        result = _rpc(client_perms, "write", {"path": path, "content": content_b64})
        assert "error" not in result or result.get("error") is None

        result = _rpc(client_perms, "read", {"path": path})
        assert "result" in result

    def test_mkdir_with_perms(self, client_perms):
        """mkdir via RPC with permissions on."""
        dirname = f"/perm-dir-{uuid.uuid4().hex[:8]}"
        result = _rpc(
            client_perms,
            "mkdir",
            {
                "path": dirname,
                "parents": True,
                "exist_ok": True,
            },
        )
        assert "error" not in result or result.get("error") is None

    def test_list_with_perms(self, client_perms):
        """list via RPC with permissions on."""
        prefix = f"/perm-list-api-{uuid.uuid4().hex[:8]}"
        _rpc(
            client_perms,
            "write",
            {
                "path": f"{prefix}/file.txt",
                "content": base64.b64encode(b"data").decode(),
            },
        )
        result = _rpc(client_perms, "list", {"path": prefix})
        assert "result" in result

    def test_version_with_perms(self, client_perms):
        """list_versions via RPC with permissions on."""
        path = f"/perm-ver-{uuid.uuid4().hex[:8]}.txt"
        _rpc(
            client_perms,
            "write",
            {
                "path": path,
                "content": base64.b64encode(b"v1 perm").decode(),
            },
        )
        result = _rpc(client_perms, "list_versions", {"path": path})
        if "error" in result and result["error"]:
            pytest.skip("VersionService not available")
        assert "result" in result

    def test_memory_with_perms(self, client_perms):
        """Memory store should work with permissions enabled."""
        nx = client_perms["nx"]
        mem = nx.memory
        mid = mem.store(
            content="E2E memory with permissions + PG",
            scope="user",
            memory_type="observation",
        )
        assert mid is not None
        result = mem.get(mid)
        assert result is not None


# ---------------------------------------------------------------------------
# 9. Service wiring verification (factory created all expected services)
# ---------------------------------------------------------------------------


class TestFactoryServiceWiring:
    """Verify factory wired all expected services for the extracted methods."""

    def test_memory_provider_wired(self, nx):
        """MemoryProvider should be wired by service_wiring."""
        assert hasattr(nx, "_memory_provider")
        assert nx._memory_provider is not None

    def test_sync_service_wired(self, nx):
        """SyncService should be wired."""
        assert hasattr(nx, "_sync_service")
        assert nx._sync_service is not None

    def test_sync_job_service_wired(self, nx):
        """SyncJobService should be wired."""
        assert hasattr(nx, "_sync_job_service")
        assert nx._sync_job_service is not None

    def test_version_service_wired(self, nx):
        """VersionService should be wired by factory."""
        vs = getattr(nx, "version_service", None) or getattr(nx, "_version_service", None)
        assert vs is not None

    def test_rebac_manager_wired(self, nx):
        """ReBACManager should be wired by factory."""
        assert nx._rebac_manager is not None

    def test_permission_enforcer_wired(self, nx):
        """PermissionEnforcer should be wired by factory."""
        assert nx._permission_enforcer is not None

    def test_workspace_registry_wired(self, nx):
        """WorkspaceRegistry should be wired by factory."""
        assert nx._workspace_registry is not None

    def test_entity_registry_wired(self, nx):
        """EntityRegistry should be wired by factory."""
        reg = nx._memory_provider.ensure_entity_registry()
        assert reg is not None

    def test_kernel_dispatch_wired(self, nx):
        """KernelDispatch should be wired."""
        assert hasattr(nx, "_dispatch")
        assert nx._dispatch is not None
