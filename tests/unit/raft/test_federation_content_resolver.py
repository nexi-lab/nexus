"""Unit tests for FederationContentResolver (#163).

Tests the PRE-DISPATCH resolver for remote content reads and deletes
using mocks for metastore, backend, and gRPC stubs (no real network
or Raft needed).
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.raft.federation_content_resolver import FederationContentResolver

SELF_ADDR = "10.0.0.1:50051"
REMOTE_ADDR = "10.0.0.2:50051"


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


class TestMatchesLocalContent:
    """matches() returns None for local content (not handled)."""

    def test_local_origin_returns_none(self):
        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        assert resolver.matches("/test/file.txt") is None

    def test_no_origin_returns_none(self):
        """Legacy backend_name without origin → treated as local."""
        meta = _make_meta("local")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        assert resolver.matches("/test/file.txt") is None

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        assert resolver.matches("/nonexistent.txt") is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        assert resolver.matches("/test/file.txt") is None


class TestMatchesRemoteContent:
    """matches() returns metadata (truthy) for remote content."""

    def test_remote_origin_returns_metadata(self):
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        result = resolver.matches("/test/file.txt")
        assert result is meta


class TestReadRemoteContent:
    """read() fetches content from remote peer using match_ctx."""

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_read_uses_match_ctx(self, mock_fetch):
        mock_fetch.return_value = b"remote content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=100)

        resolver = _make_resolver()
        result = resolver.read("/test/file.txt", match_ctx=meta)

        assert result == b"remote content"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_with_metadata(self, mock_fetch):
        mock_fetch.return_value = b"data"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="xyz789", version=3, size=4)

        resolver = _make_resolver()
        result = resolver.read("/test/file.txt", match_ctx=meta, return_metadata=True)

        assert result["content"] == b"data"
        assert result["etag"] == "xyz789"
        assert result["version"] == 3
        assert result["size"] == 4

    @patch.object(FederationContentResolver, "_fetch_from_peer_streaming")
    def test_large_file_uses_streaming(self, mock_stream):
        """Files > _STREAMING_THRESHOLD use StreamRead instead of unary Read."""
        mock_stream.return_value = b"streamed content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=2_000_000)  # 2MB > 1MB threshold

        resolver = _make_resolver()
        result = resolver.read("/test/large.bin", match_ctx=meta)

        assert result == b"streamed content"
        mock_stream.assert_called_once_with(REMOTE_ADDR, "/test/large.bin")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_small_file_uses_unary_read(self, mock_fetch):
        """Files <= _STREAMING_THRESHOLD use unary Read RPC."""
        mock_fetch.return_value = b"small"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=500_000)  # 500KB < 1MB threshold

        resolver = _make_resolver()
        result = resolver.read("/test/small.txt", match_ctx=meta)

        assert result == b"small"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/small.txt")


class TestDeleteRemoteContent:
    """delete() delegates to remote peer using match_ctx."""

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_delete_delegates_to_peer(self, mock_delete):
        meta = _make_meta(f"local@{REMOTE_ADDR}")

        resolver = _make_resolver()
        resolver.delete("/test/file.txt", match_ctx=meta)

        mock_delete.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_delete_failure_propagates(self, mock_delete):
        mock_delete.side_effect = Exception("network error")
        meta = _make_meta(f"local@{REMOTE_ADDR}")

        resolver = _make_resolver()
        with pytest.raises(Exception, match="network error"):
            resolver.delete("/test/file.txt", match_ctx=meta)


class TestKernelDispatchIntegration:
    """Verify FederationContentResolver works with new KernelDispatch matches() protocol."""

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
        assert result is None  # local content: matches() returns None

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

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_resolve_read_remote_is_handled(self, mock_fetch):
        from nexus.core.kernel_dispatch import KernelDispatch

        mock_fetch.return_value = b"remote data"
        meta = _make_meta(f"local@{REMOTE_ADDR}", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/test/file.txt")
        assert handled is True
        assert result == b"remote data"

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
