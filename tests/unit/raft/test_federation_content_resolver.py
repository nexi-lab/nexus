"""Unit tests for FederationContentResolver (#163, #1665, #1744).

Tests the PRE-DISPATCH resolver for remote content reads and deletes
using mocks for metastore, backend, and gRPC stubs (no real network
or Raft needed).

After #1665: try_read/try_write/try_delete return result or None
(single-call pattern, no matches() or match_ctx).

After #1744 Phase 2: content fetch delegated to RemoteContentFetcher
protocol — resolver is addressing-agnostic. CAS+CDC chunk logic
(including scatter-gather) is tested in test_remote_content_fetcher.py.
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

    def test_fetcher_receives_all_origins(self):
        """When RemoteContentFetcher is injected, it receives all origins at once."""
        meta = _make_meta(f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}", etag="hash123", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_fetcher = MagicMock()
        mock_fetcher.fetch_remote_content.return_value = b"fetched"

        resolver = _make_resolver(
            metastore=metastore,
            remote_content_fetcher=mock_fetcher,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"fetched"
        mock_fetcher.fetch_remote_content.assert_called_once_with(
            [REMOTE_ADDR, REMOTE_ADDR_2],
            "hash123",
        )


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


class TestDeleteLocalReplicaCleanup:
    """Tests for local replica blob cleanup on delete (#1310)."""

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_local_replica_cleaned_up_after_remote_delete(self, mock_delete):
        """After successful remote delete, local replica blob is cleaned up."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="blob_hash")
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = True

        resolver = _make_resolver(
            metastore=metastore,
            local_object_store=mock_store,
        )
        result = resolver.try_delete("/test/file.txt")

        assert result == {}
        mock_store.delete_content.assert_called_once_with("blob_hash")

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_no_local_replica_no_cleanup(self, mock_delete):
        """If local CAS doesn't have the blob, skip cleanup."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="blob_hash")
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False

        resolver = _make_resolver(
            metastore=metastore,
            local_object_store=mock_store,
        )
        result = resolver.try_delete("/test/file.txt")

        assert result == {}
        mock_store.delete_content.assert_not_called()

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_cleanup_failure_does_not_break_delete(self, mock_delete):
        """Local cleanup failure is swallowed — remote delete already succeeded."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="blob_hash")
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = True
        mock_store.delete_content.side_effect = Exception("disk error")

        resolver = _make_resolver(
            metastore=metastore,
            local_object_store=mock_store,
        )
        result = resolver.try_delete("/test/file.txt")

        assert result == {}  # Still succeeds

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_no_object_store_skips_cleanup(self, mock_delete):
        """Without local_object_store injected, cleanup is skipped."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="blob_hash")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)  # No local_object_store
        result = resolver.try_delete("/test/file.txt")

        assert result == {}


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


# =============================================================================
# RemoteContentFetcher delegation (#1744 Phase 2)
# =============================================================================


class TestRemoteContentFetcherDelegation:
    """Tests that FederationContentResolver delegates to RemoteContentFetcher."""

    def test_fetcher_delegates_with_single_origin(self):
        """Fetcher receives single origin in a list."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="hash123", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_fetcher = MagicMock()
        mock_fetcher.fetch_remote_content.return_value = b"fetched"

        resolver = _make_resolver(
            metastore=metastore,
            remote_content_fetcher=mock_fetcher,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"fetched"
        mock_fetcher.fetch_remote_content.assert_called_once_with(
            [REMOTE_ADDR],
            "hash123",
        )

    def test_fetcher_delegates_with_multi_origin(self):
        """Fetcher receives all origins at once for scatter-gather."""
        meta = _make_meta(f"local@{REMOTE_ADDR},{REMOTE_ADDR_2}", etag="hash456", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_fetcher = MagicMock()
        mock_fetcher.fetch_remote_content.return_value = b"scattered"

        resolver = _make_resolver(
            metastore=metastore,
            remote_content_fetcher=mock_fetcher,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"scattered"
        mock_fetcher.fetch_remote_content.assert_called_once_with(
            [REMOTE_ADDR, REMOTE_ADDR_2],
            "hash456",
        )

    def test_fetcher_with_return_metadata(self):
        """Fetcher result wrapped in metadata dict when return_metadata=True."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="hash789", size=50)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_fetcher = MagicMock()
        mock_fetcher.fetch_remote_content.return_value = b"data"

        resolver = _make_resolver(
            metastore=metastore,
            remote_content_fetcher=mock_fetcher,
        )
        result = resolver.try_read("/test/file.txt", return_metadata=True)

        assert isinstance(result, dict)
        assert result["content"] == b"data"
        assert result["etag"] == "hash789"
        assert result["size"] == 4

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_fallback_without_fetcher(self, mock_fetch):
        """Without RemoteContentFetcher: falls back to path-based Read RPC."""
        mock_fetch.return_value = b"fallback content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="some_hash", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        # No remote_content_fetcher
        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result == b"fallback content"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_fallback_when_no_content_hash(self, mock_fetch):
        """Fetcher present but no etag → falls back to path-based fetch."""
        mock_fetch.return_value = b"path fallback"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_fetcher = MagicMock()
        resolver = _make_resolver(
            metastore=metastore,
            remote_content_fetcher=mock_fetcher,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"path fallback"
        mock_fetcher.fetch_remote_content.assert_not_called()
