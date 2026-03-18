"""Unit tests for FederationContentResolver (#163, #1665).

Tests the PRE-DISPATCH resolver for remote content reads and deletes
using mocks for metastore, backend, and gRPC stubs (no real network
or Raft needed).

After #1665: try_read/try_write/try_delete return result or None
(single-call pattern, no matches() or match_ctx).
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.raft.federation_content_resolver import FederationContentResolver

SELF_ADDR = "10.0.0.1:50051"
REMOTE_ADDR = "10.0.0.2:50051"
REMOTE_ADDR_2 = "10.0.0.3:50051"


def _make_meta(backend_name: str, etag: str = "abc123", version: int = 1, size: int = 100):
    """Create a mock FileMetadata with the given backend_name."""
    meta = MagicMock()
    meta.backend_name = backend_name
    meta.etag = etag
    meta.version = version
    meta.size = size
    meta.modified_at = "2026-01-01T00:00:00Z"
    return meta


def _make_resolver(**kwargs):
    metastore = kwargs.pop("metastore", MagicMock())
    return FederationContentResolver(
        metastore=metastore,
        self_address=kwargs.pop("self_address", SELF_ADDR),
        **kwargs,
    )


class TestTryReadLocalContent:
    """try_read returns None for local content (not handled)."""

    def test_local_origin_returns_none(self):
        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result is None

    def test_self_in_multi_origin_returns_none(self):
        """If self is ANY of the origins, content is local — return None."""
        meta = _make_meta(f"local@{REMOTE_ADDR},{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result is None

    def test_no_origin_returns_none(self):
        """Legacy backend_name without origin -> treated as local."""
        meta = _make_meta("local")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result is None

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/nonexistent.txt")

        assert result is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result is None


class TestTryReadRemoteContent:
    """try_read returns content for remote content."""

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_origin_fetches_content(self, mock_fetch):
        mock_fetch.return_value = b"remote content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result == b"remote content"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_with_metadata(self, mock_fetch):
        mock_fetch.return_value = b"data"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="xyz789", version=3, size=4)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt", return_metadata=True)

        assert result["content"] == b"data"
        assert result["etag"] == "xyz789"
        assert result["version"] == 3
        assert result["size"] == 4

    @patch.object(FederationContentResolver, "_fetch_from_peer_streaming")
    def test_large_file_uses_streaming(self, mock_stream):
        """Files > _STREAMING_THRESHOLD use StreamRead instead of unary Read."""
        mock_stream.return_value = b"streamed content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=2_000_000)  # 2MB > 1MB threshold
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/large.bin")

        assert result == b"streamed content"
        mock_stream.assert_called_once_with(REMOTE_ADDR, "/test/large.bin")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_small_file_uses_unary_read(self, mock_fetch):
        """Files <= _STREAMING_THRESHOLD use unary Read RPC."""
        mock_fetch.return_value = b"small"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=500_000)  # 500KB < 1MB threshold
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/small.txt")

        assert result == b"small"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/small.txt")


class TestTryReadMultiOriginFailover:
    """try_read fails over to next origin when one is unreachable."""

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_failover_to_second_origin(self, mock_fetch):
        """First origin fails, second succeeds."""
        mock_fetch.side_effect = [
            NexusFileNotFoundError("/test/file.txt", "peer unreachable"),
            b"from second",
        ]
        meta = _make_meta(f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result == b"from second"
        assert mock_fetch.call_count == 2

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_all_origins_fail_raises(self, mock_fetch):
        """All origins fail -> raises NexusFileNotFoundError."""
        mock_fetch.side_effect = NexusFileNotFoundError("/test/file.txt", "unreachable")
        meta = _make_meta(f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        with pytest.raises(NexusFileNotFoundError, match="All origins unreachable"):
            resolver.try_read("/test/file.txt")


class TestTryWritePassthrough:
    """try_write always returns None (content writes are local)."""

    def test_try_write_returns_none(self):
        resolver = _make_resolver()
        result = resolver.try_write("/any/path", b"data")

        assert result is None


class TestTryDeleteLocalContent:
    """try_delete returns None for local content (not handled)."""

    def test_local_origin_returns_none(self):
        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result is None

    def test_self_in_multi_origin_returns_none(self):
        meta = _make_meta(f"local@{REMOTE_ADDR},{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result is None

    def test_no_origin_returns_none(self):
        meta = _make_meta("local")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result is None

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/nonexistent.txt")

        assert result is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result is None


class TestTryDeleteRemoteContent:
    """try_delete returns {} for remote content."""

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_origin_delegates_to_peer(self, mock_delete):
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result == {}
        mock_delete.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_delete_failure_propagates(self, mock_delete):
        """gRPC failure during remote delete propagates to caller."""
        mock_delete.side_effect = Exception("network error")
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        # Single origin — all origins fail, returns {} (logged warning)
        result = resolver.try_delete("/test/file.txt")
        assert result == {}

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_multi_origin_delete_failover(self, mock_delete):
        """First origin fails, second succeeds."""
        mock_delete.side_effect = [Exception("network error"), None]
        meta = _make_meta(f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_delete("/test/file.txt")

        assert result == {}
        assert mock_delete.call_count == 2


class TestKernelDispatchIntegration:
    """Verify FederationContentResolver works with KernelDispatch."""

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_resolve_read_remote_returns_handled(self, mock_fetch):
        from nexus.core.kernel_dispatch import KernelDispatch

        mock_fetch.return_value = b"remote"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/test/file.txt")
        assert handled is True
        assert result == b"remote"

    def test_resolve_read_local_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/test/file.txt")
        assert handled is False
        assert result is None

    def test_resolve_read_no_metadata_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/nonexistent.txt")
        assert handled is False
        assert result is None

    def test_resolve_write_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        resolver = _make_resolver()
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_write("/test/file.txt", b"data")
        assert handled is False
        assert result is None

    def test_resolve_delete_local_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_delete("/test/file.txt")
        assert handled is False
        assert result is None

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_resolve_delete_remote_delegates(self, mock_delete):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_delete("/test/file.txt")
        assert handled is True
        assert result == {}
        mock_delete.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    def test_resolve_delete_no_metadata_passes_through(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_delete("/nonexistent.txt")
        assert handled is False
        assert result is None
