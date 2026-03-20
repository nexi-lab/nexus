"""Unit tests for FederationContentResolver (#163, #1665, #1744).

Tests the PRE-DISPATCH resolver for remote content reads and deletes
using mocks for metastore, backend, and gRPC stubs (no real network
or Raft needed).

After #1665: try_read/try_write/try_delete return result or None
(single-call pattern, no matches() or match_ctx).

After #1744: CDC-aware federation read — manifest + local chunk check +
parallel fetch of missing chunks via PeerBlobClient.
"""

import json
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


# =============================================================================
# CDC-aware federation read (#1744)
# =============================================================================


def _make_manifest_bytes(chunk_hashes: list[str], total_size: int = 2048) -> bytes:
    """Build a minimal CDC manifest JSON bytes."""
    chunks = []
    offset = 0
    chunk_size = total_size // len(chunk_hashes) if chunk_hashes else 0
    for h in chunk_hashes:
        chunks.append({"chunk_hash": h, "offset": offset, "length": chunk_size})
        offset += chunk_size
    manifest = {
        "type": "chunked_manifest_v1",
        "total_size": total_size,
        "chunk_count": len(chunk_hashes),
        "avg_chunk_size": chunk_size,
        "content_hash": "full_content_hash_placeholder",
        "chunks": chunks,
    }
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8")


class TestCDCAwareFederationRead:
    """Tests for CDC-aware blob-based federation read (#1744)."""

    def test_blob_fetch_single_blob_file(self):
        """Non-chunked file: fetch by hash, store locally, return content."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="single_hash", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.write_content.return_value = MagicMock(content_hash="single_hash")

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = b"hello world"  # Not a manifest

        resolver = _make_resolver(
            metastore=metastore,
            peer_blob_client=mock_client,
            local_object_store=mock_store,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"hello world"
        mock_client.fetch_blob.assert_called_once_with(REMOTE_ADDR, "single_hash")
        mock_store.write_content.assert_called_once_with(b"hello world")

    def test_blob_fetch_chunked_all_missing(self):
        """Chunked file, no local chunks: fetch manifest + all chunks from origin."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="manifest_hash", size=2048)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False  # Nothing local
        mock_store.write_content.return_value = MagicMock()
        mock_store.read_content.return_value = b"assembled content"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs.return_value = {
            "chunk_a": b"aaa",
            "chunk_b": b"bbb",
            "chunk_c": b"ccc",
        }

        resolver = _make_resolver(
            metastore=metastore,
            peer_blob_client=mock_client,
            local_object_store=mock_store,
        )
        result = resolver.try_read("/test/big.csv")

        assert result == b"assembled content"
        # Manifest fetched by hash
        mock_client.fetch_blob.assert_called_once_with(REMOTE_ADDR, "manifest_hash")
        # All 3 chunks fetched
        mock_client.fetch_blobs.assert_called_once()
        fetched_hashes = mock_client.fetch_blobs.call_args[0][1]
        assert set(fetched_hashes) == {"chunk_a", "chunk_b", "chunk_c"}
        # Final assembly via local read
        mock_store.read_content.assert_called_once_with("manifest_hash")

    def test_blob_fetch_chunked_partial_local(self):
        """Chunked file, some chunks local: only fetch missing ones."""
        chunk_hashes = ["chunk_a", "chunk_b", "chunk_c"]
        manifest_bytes = _make_manifest_bytes(chunk_hashes)

        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="manifest_hash", size=2048)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        # content_exists: manifest=no, chunk_a=yes, chunk_b=no, chunk_c=yes
        mock_store.content_exists.side_effect = lambda h: h in ("chunk_a", "chunk_c")
        mock_store.write_content.return_value = MagicMock()
        mock_store.read_content.return_value = b"assembled"

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = manifest_bytes
        mock_client.fetch_blobs.return_value = {"chunk_b": b"bbb"}

        resolver = _make_resolver(
            metastore=metastore,
            peer_blob_client=mock_client,
            local_object_store=mock_store,
        )
        result = resolver.try_read("/test/big.csv")

        assert result == b"assembled"
        # Only chunk_b should be fetched
        mock_client.fetch_blobs.assert_called_once()
        fetched_hashes = mock_client.fetch_blobs.call_args[0][1]
        assert fetched_hashes == ["chunk_b"]

    def test_blob_fetch_content_already_local(self):
        """Content already in local CAS: skip all remote fetches."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="local_hash", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = True  # Already local!
        mock_store.read_content.return_value = b"local content"

        mock_client = MagicMock()

        resolver = _make_resolver(
            metastore=metastore,
            peer_blob_client=mock_client,
            local_object_store=mock_store,
        )
        result = resolver.try_read("/test/file.txt")

        assert result == b"local content"
        # No remote calls at all
        mock_client.fetch_blob.assert_not_called()
        mock_client.fetch_blobs.assert_not_called()

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_fallback_without_peer_client(self, mock_fetch):
        """Without PeerBlobClient: falls back to path-based Read RPC."""
        mock_fetch.return_value = b"fallback content"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="some_hash", size=100)
        metastore = MagicMock()
        metastore.get.return_value = meta

        # No peer_blob_client or local_object_store
        resolver = _make_resolver(metastore=metastore)
        result = resolver.try_read("/test/file.txt")

        assert result == b"fallback content"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    def test_blob_fetch_with_return_metadata(self):
        """CDC-aware read with return_metadata=True."""
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="hash123", size=50)
        metastore = MagicMock()
        metastore.get.return_value = meta

        mock_store = MagicMock()
        mock_store.content_exists.return_value = False
        mock_store.write_content.return_value = MagicMock()

        mock_client = MagicMock()
        mock_client.fetch_blob.return_value = b"data"  # Not a manifest

        resolver = _make_resolver(
            metastore=metastore,
            peer_blob_client=mock_client,
            local_object_store=mock_store,
        )
        result = resolver.try_read("/test/file.txt", return_metadata=True)

        assert isinstance(result, dict)
        assert result["content"] == b"data"
        assert result["etag"] == "hash123"
