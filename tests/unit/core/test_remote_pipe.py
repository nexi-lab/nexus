"""Unit tests for RemotePipeBackend."""

import base64
from unittest.mock import MagicMock

import pytest

from nexus.core.pipe import PipeClosedError
from nexus.core.remote_pipe import RemotePipeBackend


class TestRemotePipeBackend:
    def _make_backend(self) -> tuple[RemotePipeBackend, MagicMock]:
        transport = MagicMock()
        backend = RemotePipeBackend(
            origin="10.0.0.2:50051",
            path="/nexus/pipes/test",
            transport=transport,
        )
        return backend, transport

    def test_stats(self) -> None:
        backend, _ = self._make_backend()
        stats = backend.stats
        assert stats["type"] == "remote"
        assert stats["origin"] == "10.0.0.2:50051"
        assert stats["path"] == "/nexus/pipes/test"
        assert stats["closed"] is False

    def test_close(self) -> None:
        backend, _ = self._make_backend()
        assert backend.closed is False
        backend.close()
        assert backend.closed is True

    def test_write_nowait_closed_raises(self) -> None:
        backend, _ = self._make_backend()
        backend.close()
        with pytest.raises(PipeClosedError, match="closed remote pipe"):
            backend.write_nowait(b"data")

    def test_read_nowait_closed_raises(self) -> None:
        backend, _ = self._make_backend()
        backend.close()
        with pytest.raises(PipeClosedError, match="closed remote pipe"):
            backend.read_nowait()

    def test_write_nowait_calls_rpc(self) -> None:
        backend, transport = self._make_backend()
        transport.call_rpc.return_value = 5

        result = backend.write_nowait(b"hello")

        assert result == 5
        transport.call_rpc.assert_called_once_with(
            "sys_write",
            {
                "path": "/nexus/pipes/test",
                "buf": base64.b64encode(b"hello").decode("ascii"),
            },
        )

    def test_read_nowait_calls_rpc(self) -> None:
        backend, transport = self._make_backend()
        encoded_data = base64.b64encode(b"hello").decode("ascii")
        transport.call_rpc.return_value = encoded_data

        result = backend.read_nowait()

        assert result == b"hello"
        transport.call_rpc.assert_called_once_with(
            "sys_read",
            {"path": "/nexus/pipes/test"},
        )

    @pytest.mark.asyncio
    async def test_async_wait_writable(self) -> None:
        backend, _ = self._make_backend()
        await backend.wait_writable()

    @pytest.mark.asyncio
    async def test_async_wait_readable(self) -> None:
        backend, _ = self._make_backend()
        await backend.wait_readable()
