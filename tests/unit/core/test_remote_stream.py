"""Unit tests for RemoteStreamBackend."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from nexus.core.remote_stream import RemoteStreamBackend
from nexus.core.stream import StreamClosedError


class TestRemoteStreamBackend:
    def _make_backend(self) -> tuple[RemoteStreamBackend, MagicMock]:
        transport = MagicMock()
        backend = RemoteStreamBackend(
            origin="10.0.0.2:50051",
            path="/nexus/streams/test",
            transport=transport,
        )
        return backend, transport

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
        backend, transport = self._make_backend()
        # call_rpc returns the starting offset (0)
        transport.call_rpc.return_value = 0

        result = backend.write_nowait(b"hello")

        assert result == 0
        assert backend.tail == 5  # 0 + len(b"hello")
        transport.call_rpc.assert_called_once_with(
            "sys_write",
            {
                "path": "/nexus/streams/test",
                "buf": base64.b64encode(b"hello").decode("ascii"),
            },
        )

    def test_read_at_calls_rpc(self) -> None:
        backend, transport = self._make_backend()
        encoded_data = base64.b64encode(b"hello").decode("ascii")
        # call_rpc returns a dict with result and next_offset
        transport.call_rpc.return_value = {
            "result": encoded_data,
            "next_offset": 5,
        }

        data, next_offset = backend.read_at(0)

        assert data == b"hello"
        assert next_offset == 5
        transport.call_rpc.assert_called_once_with(
            "sys_read",
            {"path": "/nexus/streams/test", "offset": 0},
        )

    def test_read_batch_collects_multiple_reads(self) -> None:
        """read_batch should collect multiple read_at results."""
        backend, _ = self._make_backend()

        call_count = 0

        def mock_read_at(byte_offset: int) -> tuple[bytes, int]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return b"msg", byte_offset + 3
            raise Exception("no more data")

        # Patch read_at via the class (slots prevent instance patching)
        with patch.object(type(backend), "read_at", lambda self, off: mock_read_at(off)):
            items, next_offset = backend.read_batch(0, count=5)

        assert len(items) == 2
        assert items == [b"msg", b"msg"]
        assert next_offset == 6
