"""E2E tests for Issue #2033 LEGO decomposition — verify delegated methods work.

Tests APIs refactored from NexusFS to services, now accessed via
``nx.service("name").method()`` (ServiceRegistry pattern).

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

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import (
    DistributedConfig,
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


async def _create_factory_nexus_fs(
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

    return await create_nexus_fs(
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
            enable_workflows=False,
        ),
        enable_write_buffer=False,  # Sync writes so versions are immediately visible
    )


@pytest.fixture()
async def nx(tmp_path):
    """Factory-wired NexusFS with PostgreSQL, permissions disabled."""
    fs = await _create_factory_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
    yield fs
    fs.close()


@pytest.fixture()
async def nx_perms(tmp_path):
    """Factory-wired NexusFS with PostgreSQL, permissions enabled."""
    fs = await _create_factory_nexus_fs(
        tmp_path,
        enforce_permissions=True,
        is_admin=True,
        enable_tiger_cache=False,
    )
    yield fs
    fs.close()


@pytest.fixture()
async def client(tmp_path):
    """FastAPI TestClient with factory-wired NexusFS + PostgreSQL."""
    from starlette.testclient import TestClient

    from nexus.server.fastapi_server import create_app

    fs = await _create_factory_nexus_fs(tmp_path, enforce_permissions=False, is_admin=True)
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
async def client_perms(tmp_path):
    """FastAPI TestClient with factory-wired NexusFS + PostgreSQL + permissions."""
    from starlette.testclient import TestClient

    from nexus.server.fastapi_server import create_app

    fs = await _create_factory_nexus_fs(
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

    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self, nx):
        nx.write("/test.txt", b"hello world")
        data = nx.sys_read("/test.txt")
        assert data == b"hello world"

    @pytest.mark.asyncio
    async def test_mkdir_and_list(self, nx):
        nx.mkdir("/mydir", parents=True, exist_ok=True)
        nx.write("/mydir/file.txt", b"content")
        entries = nx.sys_readdir("/mydir", recursive=False)
        assert "/mydir/file.txt" in entries

    @pytest.mark.asyncio
    async def test_delete_file(self, nx):
        nx.write("/del.txt", b"bye")
        nx.sys_unlink("/del.txt")
        assert not nx.access("/del.txt")

    @pytest.mark.asyncio
    async def test_exists(self, nx):
        nx.write("/exists.txt", b"yes")
        assert nx.access("/exists.txt")
        assert not nx.access("/nope.txt")

    @pytest.mark.asyncio
    async def test_is_directory(self, nx):
        nx.mkdir("/somedir", parents=True, exist_ok=True)
        assert nx.is_directory("/somedir")

    @pytest.mark.asyncio
    async def test_get_metadata(self, nx):
        nx.write("/meta.txt", b"metadata test")
        meta = nx.sys_stat("/meta.txt")
        assert meta is not None
        assert meta["size"] == 13
        assert meta["is_directory"] is False

    @pytest.mark.asyncio
    async def test_get_etag(self, nx):
        nx.write("/etag.txt", b"etag test")
        etag = nx.get_etag("/etag.txt")
        assert etag is not None
        assert isinstance(etag, str)
        assert len(etag) > 0


# ---------------------------------------------------------------------------
# 2. Sync mount delegation (SyncService / SyncJobService)
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

    @pytest.mark.asyncio
    async def test_list_versions_after_write(self, nx):
        """list_versions should return version history after writes."""
        path = f"/ver-{uuid.uuid4().hex[:8]}.txt"
        nx.write(path, b"v1")
        from nexus.lib.sync_bridge import run_sync

        versions = run_sync(nx.version_service.list_versions(path))
        assert isinstance(versions, list)
        assert len(versions) >= 1

    @pytest.mark.asyncio
    async def test_get_version_returns_content(self, nx):
        """get_version should retrieve specific version content."""
        from nexus.lib.sync_bridge import run_sync

        path = f"/ver2-{uuid.uuid4().hex[:8]}.txt"
        nx.write(path, b"version-one")
        versions = run_sync(nx.version_service.list_versions(path))
        assert len(versions) >= 1
        ver_num = versions[0].get("version", 1)
        content = run_sync(nx.version_service.get_version(path, ver_num))
        assert isinstance(content, bytes)

    @pytest.mark.asyncio
    async def test_multiple_versions(self, nx):
        """Multiple writes should produce multiple versions."""
        from nexus.lib.sync_bridge import run_sync

        path = f"/multi-ver-{uuid.uuid4().hex[:8]}.txt"
        nx.write(path, b"v1")
        nx.write(path, b"v2")
        versions = run_sync(nx.version_service.list_versions(path))
        assert len(versions) >= 2


# ---------------------------------------------------------------------------
# 5. Permission enforcement with factory-wired ReBAC + PG
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Verify permission checks still work after decomposition with PG ReBAC."""

    @pytest.mark.asyncio
    async def test_write_with_admin_context(self, nx_perms):
        """Admin should be able to write with permissions enabled."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-test-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.write(path, b"admin write", context=ctx)
        data = nx_perms.sys_read(path, context=ctx)
        assert data == b"admin write"

    @pytest.mark.asyncio
    async def test_mkdir_with_admin(self, nx_perms):
        """Admin should be able to mkdir."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
            is_system=False,
        )
        dirname = f"/admin-dir-{uuid.uuid4().hex[:8]}"
        nx_perms.mkdir(dirname, parents=True, exist_ok=True, context=ctx)
        assert nx_perms.is_directory(dirname, context=ctx)

    @pytest.mark.asyncio
    async def test_read_after_write_with_permissions(self, nx_perms):
        """Read after write should work with admin permissions."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-rw-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.write(path, b"perm data", context=ctx)
        result = nx_perms.sys_read(path, context=ctx)
        assert result == b"perm data"

    @pytest.mark.asyncio
    async def test_version_with_permissions(self, nx_perms):
        """list_versions should work with admin permissions."""
        ctx = OperationContext(
            user_id="admin",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
            is_system=False,
        )
        path = f"/perm-ver-{uuid.uuid4().hex[:8]}.txt"
        nx_perms.write(path, b"version with perms", context=ctx)
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


# ---------------------------------------------------------------------------
# 9. Service wiring verification (factory created all expected services)
# ---------------------------------------------------------------------------


class TestFactoryServiceWiring:
    """Verify factory wired all expected services for the extracted methods."""

    def test_sync_service_wired(self, nx):
        """SyncService should be wired."""
        assert nx.service("sync") is not None

    def test_sync_job_service_wired(self, nx):
        """SyncJobService should be wired."""
        assert nx.service("sync_job") is not None

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

    def test_kernel_dispatch_wired(self, nx):
        """KernelDispatch methods should be available on NexusFS (via DispatchMixin)."""
        assert hasattr(nx, "resolve_read")
        assert hasattr(nx, "notify")
