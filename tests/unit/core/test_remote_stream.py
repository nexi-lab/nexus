"""Unit tests for RemoteStreamBackend."""

from unittest.mock import MagicMock, patch

import pytest

from nexus.core.remote_stream import RemoteStreamBackend
from nexus.core.stream import StreamClosedError


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


class TestRemoteStreamBackend:
    def _make_backend(self) -> tuple[RemoteStreamBackend, MockChannelPool]:
        pool = MockChannelPool()
        backend = RemoteStreamBackend(
            origin="10.0.0.2:50051",
            path="/nexus/streams/test",
            channel_pool=pool,
        )
        return backend, pool

    def test_stats(self) -> None:
        backend, _ = self._make_backend()
        stats = backend.stats
        assert stats["type"] == "remote"
        assert stats["origin"] == "10.0.0.2:50051"
        assert stats["path"] == "/nexus/streams/test"
        assert stats["closed"] is False

    def test_close(self) -> None:
        backend, _ = self._make_backend()
        assert backend.closed is False
        backend.close()
        assert backend.closed is True

    def test_tail_starts_at_zero(self) -> None:
        backend, _ = self._make_backend()
        assert backend.tail == 0

    def test_write_nowait_closed_raises(self) -> None:
        backend, _ = self._make_backend()
        backend.close()
        with pytest.raises(StreamClosedError, match="closed remote stream"):
            backend.write_nowait(b"data")

    def test_read_at_closed_raises(self) -> None:
        backend, _ = self._make_backend()
        backend.close()
        with pytest.raises(StreamClosedError, match="closed remote stream"):
            backend.read_at(0)

    def test_write_nowait_calls_rpc(self) -> None:
        backend, _ = self._make_backend()
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = b'{"result": 0}'

        mock_stub = MagicMock()
        mock_stub.Call.return_value = mock_response

        with _patch_grpc_vfs(mock_stub):
            result = backend.write_nowait(b"hello")

        assert result == 0
        assert backend.tail == 5  # 0 + len(b"hello")

    def test_read_at_calls_rpc(self) -> None:
        backend, _ = self._make_backend()
        import base64

        encoded_data = base64.b64encode(b"hello").decode("ascii")
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = f'{{"result": "{encoded_data}", "next_offset": 5}}'.encode()

        mock_stub = MagicMock()
        mock_stub.Call.return_value = mock_response

        with _patch_grpc_vfs(mock_stub):
            data, next_offset = backend.read_at(0)

        assert data == b"hello"
        assert next_offset == 5

    def test_read_batch_collects_multiple_reads(self) -> None:
        """read_batch should collect multiple read_at results."""
        backend, _ = self._make_backend()

        call_count = 0

        def mock_rpc_read(self_arg, byte_offset: int) -> tuple[bytes, int]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return b"msg", byte_offset + 3
            raise Exception("no more data")

        # Patch _rpc_read via the class (slots prevent instance patching)
        with patch.object(type(backend), "_rpc_read", mock_rpc_read):
            items, next_offset = backend.read_batch(0, count=5)

        assert len(items) == 2
        assert items == [b"msg", b"msg"]
        assert next_offset == 6
