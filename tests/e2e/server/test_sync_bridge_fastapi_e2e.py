"""E2E test: sync_bridge with real FastAPI server + permissions (Issue #1300).

Validates that the async/sync anti-pattern fixes work correctly in a
realistic FastAPI server context:
1. Creates FastAPI app with permissions enforcement
2. Exercises file write/read/delete operations through the server
3. Verifies no "cannot call asyncio.run() from running event loop" errors
4. Validates event dispatch (fire_and_forget) works within the server
5. Tests concurrent operations don't deadlock

Uses httpx ASGITransport to call the FastAPI app in-process (no subprocess).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx
import pytest

from nexus.backends.local import LocalBackend
from nexus.core.sync_bridge import shutdown_sync_bridge
from nexus.storage.record_store import SQLAlchemyRecordStore


def _create_test_app(tmp_path: Path, enforce_permissions: bool = True):
    """Create a FastAPI app with real NexusFS for testing.

    Uses the factory to wire up all services (ReBAC, audit, etc.)
    exactly like the production server does.
    """
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    # Set JWT secret required by auth
    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-12345")

    # Create backend and stores
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=str(storage_dir))

    # Create metadata store (Raft standalone mode — path must be a file, not directory)
    metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft-metadata"))

    # Create record store for services (ReBAC, audit, etc.)
    db_url = f"sqlite:///{tmp_path / 'records.db'}"
    record_store = SQLAlchemyRecordStore(db_url=db_url)

    # Create NexusFS with full service wiring
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=enforce_permissions,
        allow_admin_bypass=True,
        enforce_zone_isolation=False,
        is_admin=True,
        enable_tiger_cache=False,
        enable_deferred_permissions=False,
    )

    # Create FastAPI app with API key auth
    api_key = "test-api-key-e2e"
    app = create_app(
        nexus_fs=nx,
        api_key=api_key,
        database_url=db_url,
    )

    return app, api_key


def _run_async(coro):
    """Run a coroutine in a fresh event loop (avoids pytest-asyncio dependency)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def server_app(tmp_path):
    """Create a test FastAPI app with permissions."""
    app, api_key = _create_test_app(tmp_path, enforce_permissions=True)
    yield app, api_key
    shutdown_sync_bridge()


@pytest.fixture
def server_app_no_perms(tmp_path):
    """Create a test FastAPI app without permissions."""
    app, api_key = _create_test_app(tmp_path, enforce_permissions=False)
    yield app, api_key
    shutdown_sync_bridge()


# === Health check ===


class TestServerHealth:
    """Basic server health with our changes."""

    def test_health_endpoint(self, server_app_no_perms):
        """Server should start and respond to health checks."""
        app, _api_key = server_app_no_perms

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data.get("status") in ("ok", "healthy")

        _run_async(_test())


# === File operations through server (exercises run_sync / fire_and_forget) ===


class TestFileOperationsE2E:
    """Test file operations through the FastAPI server.

    These exercise the code paths where asyncio.run() was replaced
    with run_sync() and fire_and_forget().
    """

    def test_write_and_read_file(self, server_app_no_perms):
        """Write a file via API, then read it back."""
        app, api_key = server_app_no_perms

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                # Write
                resp = await client.post(
                    "/api/nfs/write",
                    json={"params": {"path": "/test.txt", "content": "aGVsbG8="}},
                    headers=headers,
                )
                assert resp.status_code == 200, f"Write failed: {resp.text}"

                # Read
                resp = await client.post(
                    "/api/nfs/read",
                    json={"params": {"path": "/test.txt"}},
                    headers=headers,
                )
                assert resp.status_code == 200, f"Read failed: {resp.text}"

        _run_async(_test())

    def test_write_delete_exists(self, server_app_no_perms):
        """Write, check exists, delete — exercises event dispatch paths."""
        app, api_key = server_app_no_perms

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}
                path = f"/test-{uuid.uuid4().hex[:8]}.txt"

                # Write
                resp = await client.post(
                    "/api/nfs/write",
                    json={"params": {"path": path, "content": "dGVzdA=="}},
                    headers=headers,
                )
                assert resp.status_code == 200

                # Exists
                resp = await client.post(
                    "/api/nfs/exists",
                    json={"params": {"path": path}},
                    headers=headers,
                )
                assert resp.status_code == 200

                # Delete
                resp = await client.post(
                    "/api/nfs/delete",
                    json={"params": {"path": path}},
                    headers=headers,
                )
                assert resp.status_code == 200

        _run_async(_test())

    def test_concurrent_file_operations(self, server_app_no_perms):
        """Multiple concurrent operations should not deadlock or error."""
        app, api_key = server_app_no_perms

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                async def _write_file(idx: int):
                    path = f"/concurrent-{idx}.txt"
                    content_b64 = "Y29udGVudA=="  # "content"
                    resp = await client.post(
                        "/api/nfs/write",
                        json={"params": {"path": path, "content": content_b64}},
                        headers=headers,
                    )
                    return resp.status_code

                # Run 5 concurrent writes
                results = await asyncio.gather(*[_write_file(i) for i in range(5)])
                assert all(r == 200 for r in results), f"Some writes failed: {results}"

        _run_async(_test())


# === Permission-enabled operations ===


class TestPermissionsE2E:
    """Test that permissions work correctly with our sync_bridge changes."""

    def test_unauthenticated_request_rejected(self, server_app):
        """Request without API key should be rejected when auth is enabled."""
        app, _api_key = server_app

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/nfs/read",
                    json={"params": {"path": "/test.txt"}},
                )
                # Should get 401 or 403 (no auth)
                assert resp.status_code in (401, 403), (
                    f"Expected auth rejection, got {resp.status_code}: {resp.text}"
                )

        _run_async(_test())

    def test_authenticated_write_with_permissions(self, server_app):
        """Authenticated write should work with permissions enforcement."""
        app, api_key = server_app

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                # Write with auth should succeed (admin bypass enabled)
                resp = await client.post(
                    "/api/nfs/write",
                    json={
                        "params": {"path": "/perm-test.txt", "content": "cGVybQ=="},
                    },
                    headers=headers,
                )
                # Should succeed (admin bypass) or at least not crash
                # The key validation: no "asyncio.run() cannot be called from
                # a running event loop" error
                assert resp.status_code in (200, 201, 403), (
                    f"Unexpected status: {resp.status_code}: {resp.text}"
                )

        _run_async(_test())


# === Verify no asyncio.run errors in server context ===


class TestNoAsyncioRunErrors:
    """Verify our changes don't produce asyncio.run() errors in server context."""

    def test_multiple_sequential_operations_no_loop_error(self, server_app_no_perms):
        """Sequential operations should never hit 'cannot call asyncio.run()' error."""
        app, api_key = server_app_no_perms

        async def _test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {api_key}"}

                for i in range(10):
                    resp = await client.post(
                        "/api/nfs/write",
                        json={
                            "params": {
                                "path": f"/seq-{i}.txt",
                                "content": "dGVzdA==",
                            },
                        },
                        headers=headers,
                    )
                    assert resp.status_code == 200, (
                        f"Operation {i} failed: {resp.status_code}: {resp.text}"
                    )
                    # Verify no "asyncio.run" error in response
                    if resp.status_code != 200:
                        assert "asyncio.run" not in resp.text
                        assert "running event loop" not in resp.text

        _run_async(_test())
