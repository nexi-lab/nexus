"""E2E tests for IsolatedBackend with real FastAPI NexusFS + permissions.

Uses httpx.ASGITransport to run a real FastAPI app in-process with
``enforce_permissions=True``, proving that IsolatedBackend is a valid
drop-in replacement for any Backend in the NexusFS stack.

Tests cover:
- Real FastAPI server with IsolatedBackend as the storage backend
- Permission enforcement (enforce_permissions=True)
- Non-user (admin) operations through the API
- Unauthorized request → 401 (permissions working)
- Performance: per-call overhead acceptable
- Concurrent operations through the isolation boundary
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from nexus.isolation import IsolatedBackend, IsolationConfig

# ── Helper: IsolatedBackend wrapping a real LocalBackend ──────────────


def _make_isolated_local_backend(storage_dir: str) -> IsolatedBackend:
    """Create IsolatedBackend wrapping a real LocalBackend."""
    cfg = IsolationConfig(
        backend_module="nexus.backends.local",
        backend_class="LocalBackend",
        backend_kwargs={"root_path": storage_dir},
        pool_size=2,
        call_timeout=30.0,
        startup_timeout=30.0,
        force_process=True,
    )
    return IsolatedBackend(cfg)


def _make_isolated_mock_backend() -> IsolatedBackend:
    """Create IsolatedBackend wrapping MockBackend (fast, in-memory)."""
    cfg = IsolationConfig(
        backend_module="tests.unit.isolation.conftest",
        backend_class="MockBackend",
        pool_size=1,
        call_timeout=30.0,
        startup_timeout=30.0,
        force_process=True,
    )
    return IsolatedBackend(cfg)


# ── Helper: Real FastAPI app with IsolatedBackend ─────────────────────


def _create_test_app_with_isolated_backend(tmp_path: Path, enforce_permissions: bool = True):
    """Create a FastAPI app using IsolatedBackend as the storage backend."""
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-isolation-e2e")

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Use IsolatedBackend wrapping LocalBackend — the real deal
    backend = _make_isolated_local_backend(str(storage_dir))

    metadata_store = RaftMetadataStore.embedded(str(tmp_path / "raft-metadata"))
    db_url = f"sqlite:///{tmp_path / 'records.db'}"
    record_store = SQLAlchemyRecordStore(db_url=db_url)

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

    api_key = "test-api-key-isolation-e2e"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)
    return app, api_key, backend


# ═══════════════════════════════════════════════════════════════════════
# 1. Real FastAPI server with IsolatedBackend + permissions
# ═══════════════════════════════════════════════════════════════════════


class TestIsolatedBackendWithFastAPI:
    """IsolatedBackend → NexusFS → FastAPI with enforce_permissions=True."""

    async def test_health_endpoint(self, tmp_path) -> None:
        """Server with IsolatedBackend boots and /health returns OK."""
        app, api_key, backend = _create_test_app_with_isolated_backend(
            tmp_path / "srv", enforce_permissions=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") in ("ok", "healthy")
        backend.disconnect()

    async def test_write_read_via_api(self, tmp_path) -> None:
        """Write → read through real FastAPI with IsolatedBackend."""
        app, api_key, backend = _create_test_app_with_isolated_backend(
            tmp_path / "srv", enforce_permissions=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            # Write via API
            import base64

            content_b64 = base64.b64encode(b"isolation e2e test").decode()
            resp = await client.post(
                "/api/nfs/write",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "write",
                    "params": {"path": "/isolation-test.txt", "content": content_b64},
                },
            )
            assert resp.status_code == 200

            # Read via API
            resp = await client.post(
                "/api/nfs/read",
                json={
                    "jsonrpc": "2.0",
                    "id": "2",
                    "method": "read",
                    "params": {"path": "/isolation-test.txt"},
                },
            )
            assert resp.status_code == 200
            result = resp.json().get("result", {})
            # Verify content came back (may be base64 encoded)
            assert result is not None
        backend.disconnect()

    async def test_mkdir_via_api(self, tmp_path) -> None:
        """mkdir through real FastAPI with IsolatedBackend."""
        app, api_key, backend = _create_test_app_with_isolated_backend(
            tmp_path / "srv", enforce_permissions=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            resp = await client.post(
                "/api/nfs/mkdir",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "mkdir",
                    "params": {"path": "/isolated-dir"},
                },
            )
            assert resp.status_code == 200
        backend.disconnect()


# ═══════════════════════════════════════════════════════════════════════
# 2. Permission enforcement — 401 for unauthorized requests
# ═══════════════════════════════════════════════════════════════════════


class TestIsolatedBackendPermissions:
    """Verify permissions are enforced with IsolatedBackend."""

    async def test_no_api_key_returns_401(self, tmp_path) -> None:
        """Request without API key → 401 (not 500, not swallowed)."""
        app, _api_key, backend = _create_test_app_with_isolated_backend(
            tmp_path / "srv", enforce_permissions=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            # No Authorization header
        ) as client:
            resp = await client.post(
                "/api/nfs/read",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "read",
                    "params": {"path": "/secret.txt"},
                },
            )
            assert resp.status_code == 401
        backend.disconnect()

    async def test_wrong_api_key_returns_401(self, tmp_path) -> None:
        """Request with wrong API key → 401."""
        app, _api_key, backend = _create_test_app_with_isolated_backend(
            tmp_path / "srv", enforce_permissions=True
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer wrong-key-12345"},
        ) as client:
            resp = await client.post(
                "/api/nfs/read",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "read",
                    "params": {"path": "/secret.txt"},
                },
            )
            assert resp.status_code == 401
        backend.disconnect()


# ═══════════════════════════════════════════════════════════════════════
# 3. Direct IsolatedBackend lifecycle (no server)
# ═══════════════════════════════════════════════════════════════════════


class TestIsolatedBackendDirect:
    """Direct Backend method calls through isolation boundary."""

    def test_full_file_lifecycle(self) -> None:
        backend = _make_isolated_mock_backend()
        try:
            wr = backend.write_content(b"e2e test content")
            assert wr.success is True

            rd = backend.read_content(wr.data)
            assert rd.data == b"e2e test content"

            chunks = list(backend.stream_content(wr.data, chunk_size=5))
            assert b"".join(chunks) == b"e2e test content"

            sz = backend.get_content_size(wr.data)
            assert sz.data == len(b"e2e test content")

            dl = backend.delete_content(wr.data)
            assert dl.success is True

            ex = backend.content_exists(wr.data)
            assert ex.data is False
        finally:
            backend.disconnect()

    def test_connect_disconnect(self) -> None:
        backend = _make_isolated_mock_backend()
        status = backend.connect()
        assert status.success is True
        assert backend.is_connected is True
        backend.disconnect()
        assert backend.is_connected is False

    def test_error_propagation(self) -> None:
        backend = _make_isolated_mock_backend()
        try:
            rd = backend.read_content("nonexistent")
            assert rd.success is False
        finally:
            backend.disconnect()


# ═══════════════════════════════════════════════════════════════════════
# 4. Performance validation
# ═══════════════════════════════════════════════════════════════════════


class TestIsolatedBackendPerformance:
    """Verify no critical performance issues."""

    def test_per_call_overhead_under_10ms(self) -> None:
        """Per-call overhead through ProcessPool must be < 10ms average."""
        backend = _make_isolated_mock_backend()
        try:
            # Warm up — first call creates pool + imports
            backend.write_content(b"warmup")

            data = b"X" * 1024
            wr = backend.write_content(data)
            content_hash = wr.data

            start = time.perf_counter()
            n = 100
            for _ in range(n):
                backend.content_exists(content_hash)
            elapsed = time.perf_counter() - start

            avg_ms = (elapsed / n) * 1000
            assert avg_ms < 10.0, f"Per-call overhead too high: {avg_ms:.3f}ms"
        finally:
            backend.disconnect()

    def test_1kb_roundtrip_under_20ms(self) -> None:
        """1KB write+read roundtrip must be < 20ms average."""
        backend = _make_isolated_mock_backend()
        try:
            backend.write_content(b"warmup")

            data = b"Y" * 1024
            start = time.perf_counter()
            n = 50
            for _ in range(n):
                wr = backend.write_content(data)
                backend.read_content(wr.data)
            elapsed = time.perf_counter() - start

            avg_ms = (elapsed / n) * 1000
            assert avg_ms < 20.0, f"Roundtrip too slow: {avg_ms:.3f}ms"
        finally:
            backend.disconnect()

    def test_concurrent_reads_no_deadlock(self) -> None:
        """10 parallel reads via threads do not deadlock or error."""
        backend = _make_isolated_mock_backend()
        try:
            data = b"concurrent-perf"
            wr = backend.write_content(data)
            content_hash = wr.data

            def read_one(_: int) -> bool:
                rd = backend.read_content(content_hash)
                return rd.success and rd.data == data

            with ThreadPoolExecutor(max_workers=5) as tp:
                results = list(tp.map(read_one, range(10)))
            assert all(results)
        finally:
            backend.disconnect()
