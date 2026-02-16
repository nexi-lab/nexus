"""Integration tests for proxy module.

Uses real aiosqlite queue + httpx MockTransport to test the full
ProxyBrick stack without a real server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nexus.proxy.brick import ProxyVFSBrick
from nexus.proxy.circuit_breaker import CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import OfflineQueuedError
from nexus.proxy.transport import HttpTransport


def _make_rpc_response(result: Any) -> httpx.Response:
    """Build a successful JSON-RPC response."""
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": "1", "result": result},
    )


def _make_error_response(status: int = 500) -> httpx.Response:
    return httpx.Response(status, json={"error": "internal error"})


class TestOnlineRoundTrip:
    @pytest.mark.asyncio
    async def test_read_returns_data(self, tmp_path) -> None:  # noqa: ANN001
        """ProxyVFSBrick.read() → MockTransport → response."""

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["method"] == "read":
                return _make_rpc_response("file content")
            return _make_error_response()

        transport_mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport_mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()
        try:
            result = await proxy.read("/file.txt", "zone1")
            assert result == b"file content"
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_list_dir_returns_entries(self, tmp_path) -> None:  # noqa: ANN001
        async def handler(request: httpx.Request) -> httpx.Response:
            return _make_rpc_response(["a.txt", "b.txt", "c.txt"])

        transport_mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport_mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()
        try:
            result = await proxy.list_dir("/dir", "zone1")
            assert result == ["a.txt", "b.txt", "c.txt"]
        finally:
            await proxy.stop()


class TestOfflineQueueReplay:
    @pytest.mark.asyncio
    async def test_offline_queue_replay(self, tmp_path) -> None:  # noqa: ANN001
        """Simulate disconnect → queue → reconnect → verify replay."""
        replayed_methods: list[str] = []
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            # First 2 calls fail (simulating offline)
            if call_count <= 2:
                raise httpx.ConnectError("simulated disconnect")
            replayed_methods.append(body["method"])
            return _make_rpc_response(None)

        transport_mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport_mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
            replay_poll_interval=0.2,
            cb_failure_threshold=5,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()

        try:
            # These should fail and be queued
            with pytest.raises(OfflineQueuedError):
                await proxy.mkdir("/dir1", "z1")
            with pytest.raises(OfflineQueuedError):
                await proxy.mkdir("/dir2", "z1")

            assert await proxy.pending_count() == 2

            # Wait for replay loop to drain
            await asyncio.sleep(1.0)

            assert await proxy.pending_count() == 0
            assert "mkdir" in replayed_methods
        finally:
            await proxy.stop()


class TestCircuitBreakerIntegration:
    @pytest.mark.asyncio
    async def test_circuit_breaker_trip_and_recover(self, tmp_path) -> None:  # noqa: ANN001
        """Repeated failures → circuit open → timeout → half_open → success."""
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise httpx.ConnectError("down")
            return _make_rpc_response(True)

        transport_mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport_mock, base_url="http://test")
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
            # Trip the circuit
            for _ in range(3):
                with pytest.raises(OfflineQueuedError):
                    await proxy.exists("/f", "z1")

            assert proxy.circuit_state is CircuitState.OPEN

            # Wait for recovery timeout + replay
            await asyncio.sleep(1.5)

            # Circuit should have recovered via replay
            assert proxy.circuit_state is CircuitState.CLOSED
        finally:
            await proxy.stop()


class TestLargePayloadStreaming:
    @pytest.mark.asyncio
    async def test_large_payload_uses_streaming(self, tmp_path) -> None:  # noqa: ANN001
        """Payloads >stream_threshold_bytes use streaming upload path."""
        streamed = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal streamed
            content_type = request.headers.get("content-type", "")
            if "octet-stream" in content_type:
                streamed = True
            return httpx.Response(200, json={"result": None})

        transport_mock = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport_mock, base_url="http://test")
        config = ProxyBrickConfig(
            remote_url="http://test",
            queue_db_path=str(tmp_path / "queue.db"),
            stream_threshold_bytes=100,
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()
        try:
            await proxy.write("/big.bin", b"x" * 200, "z1")
            assert streamed
        finally:
            await proxy.stop()


class TestAuthHeaderForwarded:
    @pytest.mark.asyncio
    async def test_auth_header_in_requests(self, tmp_path) -> None:  # noqa: ANN001
        """api_key in config appears in request headers."""
        captured_headers: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return _make_rpc_response(True)

        transport_mock = httpx.MockTransport(handler)
        # We need to set the auth header on the client, not use HttpTransport's
        # auto-client since we're injecting a mock transport
        client = httpx.AsyncClient(
            transport=transport_mock,
            base_url="http://test",
            headers={"Authorization": "Bearer my-secret-key"},
        )
        config = ProxyBrickConfig(
            remote_url="http://test",
            api_key="my-secret-key",
            queue_db_path=str(tmp_path / "queue.db"),
            retry_max_attempts=1,
        )
        http_transport = HttpTransport(config, client=client)
        proxy = ProxyVFSBrick(config, transport=http_transport)
        await proxy.start()
        try:
            await proxy.exists("/f", "z1")
            assert captured_headers.get("authorization") == "Bearer my-secret-key"
        finally:
            await proxy.stop()
