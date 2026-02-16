"""E2E tests for ProxyBrick with in-process FastAPI + permissions.

Uses httpx.ASGITransport to call a real FastAPI app in-process,
validating the full proxy → transport → server → VFS chain.

Tests cover:
- Authenticated proxy operations through real FastAPI with enforce_permissions=True
- Unauthenticated/no-API-key requests → 401 propagated as RemoteCallError
- Wrong API key → 401 propagated (not queued offline)
- Offline queue replay after simulated disconnect
- Circuit breaker state transitions observable in logs
- Dead-letter on max retries
- Performance: _forward() overhead < 5ms, enqueue < 5ms
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
import pytest

from nexus.proxy.brick import ProxyVFSBrick
from nexus.proxy.circuit_breaker import CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import CircuitOpenError, OfflineQueuedError, RemoteCallError
from nexus.proxy.transport import HttpTransport

logger = logging.getLogger(__name__)


def _create_test_app(tmp_path: Path, enforce_permissions: bool = True):
    """Create a FastAPI app with real NexusFS for testing."""
    from nexus.backends.local import LocalBackend
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    os.environ.setdefault("NEXUS_JWT_SECRET", "test-secret-12345")

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    backend = LocalBackend(root_path=str(storage_dir))
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

    api_key = "test-api-key-proxy-e2e"
    app = create_app(nexus_fs=nx, api_key=api_key, database_url=db_url)
    return app, api_key


def _make_rpc_response(result):  # noqa: ANN001
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})


# ======================================================================
# 1. Real FastAPI server with permissions=True — authenticated user
# ======================================================================


class TestProxyWithRealFastAPIPermissions:
    """Proxy → HttpTransport → real FastAPI (enforce_permissions=True) → VFS.

    These tests exercise the FULL stack including the server's
    require_auth dependency, JSON-RPC parsing, and NexusFS dispatch.
    """

    async def test_health_through_real_server(self, tmp_path) -> None:  # noqa: ANN001
        """Verify transport can reach the real FastAPI health endpoint."""
        app, api_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(
            transport=asgi,
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") in ("ok", "healthy")
        finally:
            await client.aclose()

    async def test_proxy_write_read_via_server_api(self, tmp_path) -> None:  # noqa: ANN001
        """Write via proxy → read via proxy — full VFS round-trip.

        This proves the proxy's JSON-RPC format (including base64 content
        encoding) is accepted by the real FastAPI server's /api/nfs/{method}
        endpoint with auth enabled.
        """
        app, api_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(
            transport=asgi,
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        config = ProxyBrickConfig(
            remote_url="http://test",
            api_key=api_key,
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            # Write through proxy — exercises base64 encoding + _forward
            await proxy.write("/proxy-e2e.txt", b"hello world", "default")

            # Read through proxy — exercises _forward + response decoding
            data = await proxy.read("/proxy-e2e.txt", "default")
            assert data == b"hello world" or data == b"aGVsbG8gd29ybGQ="
        finally:
            await proxy.stop()
            await client.aclose()

    async def test_proxy_exists_through_real_server(self, tmp_path) -> None:  # noqa: ANN001
        """exists() through proxy → real server returns correct result."""
        app, api_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(
            transport=asgi,
            base_url="http://test",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        config = ProxyBrickConfig(
            remote_url="http://test",
            api_key=api_key,
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            # First write a file via direct API
            resp = await client.post(
                "/api/nfs/write",
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "write",
                    "params": {"path": "/e2e-check.txt", "content": "dGVzdA=="},
                },
            )
            assert resp.status_code == 200

            # Now call exists() through the proxy's _forward() path
            result = await proxy.exists("/e2e-check.txt", "default")
            # Result type varies — could be bool or truthy value
            assert result, "exists() should return truthy for existing file"
        finally:
            await proxy.stop()
            await client.aclose()


# ======================================================================
# 2. Permission denied — unauthenticated / wrong key via real server
# ======================================================================


class TestProxyPermissionDeniedRealServer:
    """Non-authenticated or wrong-key requests through proxy → real FastAPI.

    The real server raises HTTP 401 via require_auth dependency.
    Proxy must propagate this as RemoteCallError, NOT queue it offline.
    """

    async def test_no_api_key_real_server_401(self, tmp_path) -> None:  # noqa: ANN001
        """Proxy with no api_key → real server returns 401 → RemoteCallError."""
        app, _correct_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        # No Authorization header
        client = httpx.AsyncClient(transport=asgi, base_url="http://test")

        config = ProxyBrickConfig(
            remote_url="http://test",
            # No api_key — unauthenticated
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            with pytest.raises(RemoteCallError) as exc_info:
                await proxy.exists("/file.txt", "default")

            # Real server returns 401 for missing auth
            assert exc_info.value.status_code == 401
            # Should NOT be queued — auth failures are NOT connectivity errors
            assert await proxy.pending_count() == 0
            # Circuit should still be CLOSED
            assert proxy.circuit_state is CircuitState.CLOSED
        finally:
            await proxy.stop()
            await client.aclose()

    async def test_wrong_api_key_real_server_401(self, tmp_path) -> None:  # noqa: ANN001
        """Wrong API key → real server returns 401 → RemoteCallError (not queued)."""
        app, _correct_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        # Wrong key
        client = httpx.AsyncClient(
            transport=asgi,
            base_url="http://test",
            headers={"Authorization": "Bearer wrong-key-12345"},
        )

        config = ProxyBrickConfig(
            remote_url="http://test",
            api_key="wrong-key-12345",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            with pytest.raises(RemoteCallError) as exc_info:
                await proxy.exists("/file.txt", "default")

            assert exc_info.value.status_code == 401
            assert await proxy.pending_count() == 0
        finally:
            await proxy.stop()
            await client.aclose()

    async def test_repeated_auth_failures_dont_trip_circuit(self, tmp_path) -> None:  # noqa: ANN001
        """Multiple 401s from real server do NOT trip the circuit breaker."""
        app, _correct_key = _create_test_app(tmp_path / "srv", enforce_permissions=True)
        asgi = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=asgi, base_url="http://test")

        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            cb_failure_threshold=3,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            # 5 auth failures — more than cb_failure_threshold
            for _ in range(5):
                with pytest.raises(RemoteCallError):
                    await proxy.mkdir("/denied", "default")

            # Circuit must still be CLOSED — auth errors are NOT connectivity failures
            assert proxy.circuit_state is CircuitState.CLOSED
            assert await proxy.pending_count() == 0
        finally:
            await proxy.stop()
            await client.aclose()


# ======================================================================
# 3. Mock-based permission tests (independent of server wiring)
# ======================================================================


class TestProxyPermissionDeniedMock:
    """Permission denial with mock transport — validates proxy error handling."""

    async def test_403_propagates_not_queued(self, tmp_path) -> None:  # noqa: ANN001
        """403 from remote → RemoteCallError, NOT queued."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "Forbidden"})

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            with pytest.raises(RemoteCallError) as exc_info:
                await proxy.mkdir("/denied", "z1")
            assert exc_info.value.status_code == 403
            assert await proxy.pending_count() == 0
            assert proxy.circuit_state is CircuitState.CLOSED
        finally:
            await proxy.stop()


# ======================================================================
# 4. Offline queue replay E2E
# ======================================================================


class TestProxyOfflineQueueReplayE2E:
    """Queue operations while 'offline' → replay → verify."""

    async def test_offline_queue_replay_e2e(self, tmp_path, caplog) -> None:  # noqa: ANN001
        """Queue ops during simulated offline → come back online → replay succeeds."""
        call_count = 0
        replayed: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            method = body.get("method", "")

            if call_count <= 2:
                raise httpx.ConnectError("simulated offline")

            replayed.append(method)
            return _make_rpc_response(None)

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            replay_poll_interval=0.2,
            cb_failure_threshold=5,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)

        with caplog.at_level(logging.WARNING, logger="nexus.proxy"):
            await proxy.start()
            try:
                with pytest.raises(OfflineQueuedError):
                    await proxy.mkdir("/queued_dir1", "z1")
                with pytest.raises(OfflineQueuedError):
                    await proxy.mkdir("/queued_dir2", "z1")

                assert await proxy.pending_count() == 2

                await asyncio.sleep(1.5)

                assert await proxy.pending_count() == 0
                assert len(replayed) >= 2
            finally:
                await proxy.stop()

        assert any("queued for offline replay" in r.message for r in caplog.records)

    async def test_circuit_open_logged(self, tmp_path, caplog) -> None:  # noqa: ANN001
        """Circuit breaker opens after threshold failures — logged and observable."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            cb_failure_threshold=2,
            replay_poll_interval=10.0,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)

        with caplog.at_level(logging.WARNING, logger="nexus.proxy"):
            await proxy.start()
            try:
                for _ in range(2):
                    with pytest.raises(OfflineQueuedError):
                        await proxy.exists("/f", "z1")

                assert proxy.circuit_state is CircuitState.OPEN

                with pytest.raises((CircuitOpenError, OfflineQueuedError)):
                    await proxy.exists("/f", "z1")

                assert any("Circuit open" in r.message for r in caplog.records)
            finally:
                await proxy.stop()

    async def test_dead_letter_on_max_retries(self, tmp_path) -> None:  # noqa: ANN001
        """Operations exceeding max retries → dead-lettered (0 pending)."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("permanently down")

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            max_retry_count=2,
            replay_poll_interval=0.1,
            cb_failure_threshold=100,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()
        try:
            with pytest.raises(OfflineQueuedError):
                await proxy.mkdir("/will_fail", "z1")

            await asyncio.sleep(1.5)
            assert await proxy.pending_count() == 0
        finally:
            await proxy.stop()

    async def test_circuit_recovery_replays_queue(self, tmp_path) -> None:  # noqa: ANN001
        """Circuit trips → recovers after timeout → replay drains queue."""
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise httpx.ConnectError("offline")
            return _make_rpc_response(True)

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            cb_failure_threshold=3,
            cb_recovery_timeout=0.5,
            replay_poll_interval=0.2,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            for _ in range(3):
                with pytest.raises(OfflineQueuedError):
                    await proxy.exists("/f", "z1")

            assert proxy.circuit_state is CircuitState.OPEN

            await asyncio.sleep(2.0)

            assert proxy.circuit_state is CircuitState.CLOSED
            assert await proxy.pending_count() == 0
        finally:
            await proxy.stop()


# ======================================================================
# 5. Performance validation
# ======================================================================


class TestProxyPerformance:
    """Verify no performance regressions in the proxy layer."""

    async def test_forward_overhead_under_5ms(self, tmp_path) -> None:  # noqa: ANN001
        """_forward() adds < 5ms overhead on top of transport call."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return _make_rpc_response(True)

        mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            await proxy.exists("/f", "z1")  # warm up

            start = time.monotonic()
            for _ in range(100):
                await proxy.exists("/f", "z1")
            elapsed = time.monotonic() - start

            avg_ms = (elapsed / 100) * 1000
            logger.info("Average _forward() latency: %.3f ms", avg_ms)
            assert avg_ms < 5.0, f"_forward() too slow: {avg_ms:.3f}ms avg"
        finally:
            await proxy.stop()

    async def test_enqueue_under_5ms(self, tmp_path) -> None:  # noqa: ANN001
        """Offline queue enqueue < 5ms per operation."""
        from nexus.proxy.offline_queue import OfflineQueue

        queue = OfflineQueue(str(tmp_path / "perf.db"), max_retry_count=10)
        await queue.initialize()

        try:
            await queue.enqueue("warmup", kwargs={"k": "v"})  # warm up

            start = time.monotonic()
            for i in range(100):
                await queue.enqueue(f"op_{i}", kwargs={"path": f"/f_{i}.txt"})
            elapsed = time.monotonic() - start

            avg_ms = (elapsed / 100) * 1000
            logger.info("Average enqueue latency: %.3f ms", avg_ms)
            assert avg_ms < 5.0, f"enqueue too slow: {avg_ms:.3f}ms avg"
        finally:
            await queue.close()

    async def test_no_n_plus_1_in_batch_dequeue(self, tmp_path) -> None:  # noqa: ANN001
        """Batch dequeue is a single SQL query, not N+1."""
        from nexus.proxy.offline_queue import OfflineQueue

        queue = OfflineQueue(str(tmp_path / "batch.db"), max_retry_count=10)
        await queue.initialize()

        try:
            for i in range(50):
                await queue.enqueue(f"op_{i}")

            start = time.monotonic()
            batch = await queue.dequeue_batch(limit=50)
            elapsed = time.monotonic() - start

            assert len(batch) == 50
            dequeue_ms = elapsed * 1000
            logger.info("Batch dequeue (50 items): %.3f ms", dequeue_ms)
            # Single query should be well under 10ms
            assert dequeue_ms < 10.0, f"Batch dequeue too slow: {dequeue_ms:.3f}ms"
        finally:
            await queue.close()
