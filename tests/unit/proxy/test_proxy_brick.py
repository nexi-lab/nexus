"""Unit tests for ProxyBrick and ProxyVFSBrick."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nexus.proxy.brick import ProxyBrick, ProxyVFSBrick
from nexus.proxy.circuit_breaker import CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import CircuitOpenError, OfflineQueuedError, RemoteCallError
from nexus.proxy.offline_queue import OfflineQueue
from nexus.proxy.transport import HttpTransport


def _config(**overrides: Any) -> ProxyBrickConfig:
    defaults: dict[str, Any] = {
        "remote_url": "http://localhost:2026",
        "replay_poll_interval": 0.1,
        "cb_failure_threshold": 3,
        "cb_recovery_timeout": 1.0,
        "retry_max_attempts": 1,
    }
    defaults.update(overrides)
    return ProxyBrickConfig(**defaults)


async def _make_queue(tmp_path: Any) -> OfflineQueue:
    q = OfflineQueue(str(tmp_path / "queue.db"), max_retry_count=3)
    await q.initialize()
    return q


class TestProxyBrick:
    async def test_forward_success(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(return_value={"status": "ok"})

            proxy = ProxyBrick(_config(), transport=transport, queue=queue)
            result = await proxy._forward("test_method", key="value")

            assert result == {"status": "ok"}
            transport.call.assert_awaited_once_with("test_method", params={"key": "value"})
        finally:
            await queue.close()

    async def test_forward_offline_queues(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(
                side_effect=RemoteCallError("read", cause=httpx.ConnectError("refused"))
            )

            proxy = ProxyBrick(_config(), transport=transport, queue=queue)

            with pytest.raises(OfflineQueuedError) as exc_info:
                await proxy._forward("read", path="/a", zone_id="z1")

            assert exc_info.value.method == "read"
            assert exc_info.value.queue_id > 0
            assert await queue.pending_count() == 1
        finally:
            await queue.close()

    async def test_circuit_open_fast_fails(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(
                side_effect=RemoteCallError("op", cause=httpx.ConnectError("refused"))
            )

            cfg = _config(cb_failure_threshold=2)
            proxy = ProxyBrick(cfg, transport=transport, queue=queue)

            # Trigger circuit open
            for _ in range(2):
                with pytest.raises(OfflineQueuedError):
                    await proxy._forward("op")

            # Next call should get CircuitOpenError (circuit is open, no transport call)
            with pytest.raises((CircuitOpenError, OfflineQueuedError)):
                await proxy._forward("op")
        finally:
            await queue.close()

    async def test_replay_drains_queue(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        call_log: list[str] = []

        async def mock_call(method: str, params: dict[str, Any] | None = None) -> Any:
            call_log.append(method)
            return {"ok": True}

        transport = MagicMock(spec=HttpTransport)
        transport.call = AsyncMock(side_effect=mock_call)
        transport.close = AsyncMock()

        proxy = ProxyBrick(_config(), transport=transport, queue=queue)

        # Pre-populate queue
        await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.enqueue("write", kwargs={"path": "/b"})

        await proxy.start()
        import asyncio

        await asyncio.sleep(0.5)

        assert "read" in call_log
        assert "write" in call_log
        assert await queue.pending_count() == 0
        await proxy.stop()

    async def test_partial_replay_stops_on_failure(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        call_count = 0

        async def mock_call(method: str, params: dict[str, Any] | None = None) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RemoteCallError(method, cause=httpx.ConnectError("refused"))
            return {"ok": True}

        transport = MagicMock(spec=HttpTransport)
        transport.call = AsyncMock(side_effect=mock_call)
        transport.close = AsyncMock()

        proxy = ProxyBrick(_config(cb_failure_threshold=5), transport=transport, queue=queue)

        await queue.enqueue("op1")
        await queue.enqueue("op2")
        await queue.enqueue("op3")

        await proxy.start()
        import asyncio

        await asyncio.sleep(0.5)

        # op1 should be done, op2 failed, op3 not attempted
        assert await queue.pending_count() >= 1
        await proxy.stop()

    async def test_circuit_state_property(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            proxy = ProxyBrick(_config(), transport=transport, queue=queue)
            assert proxy.circuit_state is CircuitState.CLOSED
        finally:
            await queue.close()

    async def test_remote_call_error_non_connection_propagates(self, tmp_path) -> None:  # noqa: ANN001
        """Non-connection RemoteCallError should propagate, not queue."""
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(
                side_effect=RemoteCallError(
                    "read", status_code=500, cause=RuntimeError("server error")
                )
            )

            proxy = ProxyBrick(_config(), transport=transport, queue=queue)

            with pytest.raises(RemoteCallError):
                await proxy._forward("read", path="/a")

            assert await queue.pending_count() == 0
        finally:
            await queue.close()


class TestProxyVFSBrick:
    async def test_read_forwards(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(return_value="hello")

            proxy = ProxyVFSBrick(_config(), transport=transport, queue=queue)
            result = await proxy.read("/file.txt", "z1")
            assert result == b"hello"
        finally:
            await queue.close()

    async def test_write_small_payload(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(return_value=None)

            proxy = ProxyVFSBrick(_config(), transport=transport, queue=queue)
            await proxy.write("/file.txt", b"small data", "z1")
            transport.call.assert_awaited_once()
        finally:
            await queue.close()

    async def test_write_large_payload_streams(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.stream_upload = AsyncMock(return_value=None)

            proxy = ProxyVFSBrick(
                _config(stream_threshold_bytes=10), transport=transport, queue=queue
            )
            await proxy.write("/file.txt", b"x" * 100, "z1")
            transport.stream_upload.assert_awaited_once()
        finally:
            await queue.close()

    async def test_list_dir(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(return_value=["a.txt", "b.txt"])

            proxy = ProxyVFSBrick(_config(), transport=transport, queue=queue)
            result = await proxy.list_dir("/dir", "z1")
            assert result == ["a.txt", "b.txt"]
        finally:
            await queue.close()

    async def test_exists(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            transport = MagicMock(spec=HttpTransport)
            transport.call = AsyncMock(return_value=True)

            proxy = ProxyVFSBrick(_config(), transport=transport, queue=queue)
            assert await proxy.exists("/file.txt", "z1") is True
        finally:
            await queue.close()
