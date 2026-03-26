"""Unit tests for RemotePipeBackend."""

from unittest.mock import MagicMock, patch

import pytest

from nexus.core.pipe import PipeClosedError
from nexus.core.remote_pipe import RemotePipeBackend


class MockChannelPool:
    """Minimal PeerChannelPool duck-type for tests."""

    def __init__(self) -> None:
        self.channel = MagicMock()

    def get(self, address: str) -> object:
        return self.channel

    def set_tls_config(self, config: object) -> None:
        pass

    def close_all(self) -> None:
        pass


def _patch_grpc_vfs(mock_stub_instance):
    """Context manager that patches nexus.grpc.vfs with mock stubs."""
    mock_pb2 = MagicMock()
    mock_pb2_grpc = MagicMock()
    mock_pb2_grpc.NexusVFSServiceStub.return_value = mock_stub_instance

    mock_vfs = MagicMock()
    mock_vfs.vfs_pb2 = mock_pb2
    mock_vfs.vfs_pb2_grpc = mock_pb2_grpc

    return patch.dict(
        "sys.modules",
        {
            "nexus.grpc.vfs": mock_vfs,
            "nexus.grpc.vfs.vfs_pb2": mock_pb2,
            "nexus.grpc.vfs.vfs_pb2_grpc": mock_pb2_grpc,
        },
    )


class TestRemotePipeBackend:
    def _make_backend(self) -> tuple[RemotePipeBackend, MockChannelPool]:
        pool = MockChannelPool()
        backend = RemotePipeBackend(
            origin="10.0.0.2:50051",
            path="/nexus/pipes/test",
            channel_pool=pool,
        )
        return backend, pool

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
        backend, _ = self._make_backend()
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = b'{"result": 5}'

        mock_stub = MagicMock()
        mock_stub.Call.return_value = mock_response

        with _patch_grpc_vfs(mock_stub):
            result = backend.write_nowait(b"hello")

        assert result == 5
        mock_stub.Call.assert_called_once()

    def test_read_nowait_calls_rpc(self) -> None:
        backend, _ = self._make_backend()
        import base64

        encoded_data = base64.b64encode(b"hello").decode("ascii")
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = f'{{"result": "{encoded_data}"}}'.encode()

        mock_stub = MagicMock()
        mock_stub.Call.return_value = mock_response

        with _patch_grpc_vfs(mock_stub):
            result = backend.read_nowait()

        assert result == b"hello"

    @pytest.mark.asyncio
    async def test_async_wait_writable(self) -> None:
        backend, _ = self._make_backend()
        await backend.wait_writable()

    @pytest.mark.asyncio
    async def test_async_wait_readable(self) -> None:
        backend, _ = self._make_backend()
        await backend.wait_readable()
