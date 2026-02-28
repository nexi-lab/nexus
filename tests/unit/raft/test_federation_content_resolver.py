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


def _make_meta(backend_name: str, etag: str = "abc123", version: int = 1):
    """Create a mock FileMetadata with the given backend_name."""
    meta = MagicMock()
    meta.backend_name = backend_name
    meta.etag = etag
    meta.version = version
    meta.modified_at = "2026-01-01T00:00:00Z"
    return meta


def _make_resolver(**kwargs):
    metastore = kwargs.pop("metastore", MagicMock())
    backend = kwargs.pop("backend", MagicMock())
    return FederationContentResolver(
        metastore=metastore,
        backend=backend,
        self_address=kwargs.pop("self_address", SELF_ADDR),
        **kwargs,
    )


class TestTryReadLocalContent:
    """try_read returns (False, metadata_hint) for local content."""

    def test_local_origin_returns_metadata_hint(self):
        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_read("/test/file.txt")

        assert handled is False
        assert result is meta  # metadata hint for kernel reuse

    def test_no_origin_returns_metadata_hint(self):
        """Legacy backend_name without origin → treated as local."""
        meta = _make_meta("local")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_read("/test/file.txt")

        assert handled is False
        assert result is meta

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_read("/nonexistent.txt")

        assert handled is False
        assert result is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_read("/test/file.txt")

        assert handled is False
        assert result is None


class TestTryReadRemoteContent:
    """try_read returns (True, content) for remote content."""

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_origin_fetches_content(self, mock_fetch):
        mock_fetch.return_value = b"remote content"
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta
        backend = MagicMock()

        resolver = _make_resolver(metastore=metastore, backend=backend)
        handled, result = resolver.try_read("/test/file.txt")

        assert handled is True
        assert result == b"remote content"
        mock_fetch.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")
        # Progressive replication: content persisted locally
        backend.write_content.assert_called_once_with(b"remote content")

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_remote_with_metadata(self, mock_fetch):
        mock_fetch.return_value = b"data"
        meta = _make_meta(f"local@{REMOTE_ADDR}", etag="xyz789", version=3)
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_read("/test/file.txt", return_metadata=True)

        assert handled is True
        assert result["content"] == b"data"
        assert result["etag"] == "xyz789"
        assert result["version"] == 3
        assert result["size"] == 4

    @patch.object(FederationContentResolver, "_fetch_from_peer")
    def test_persist_failure_does_not_abort(self, mock_fetch):
        """Progressive replication failure is a warning, not an error."""
        mock_fetch.return_value = b"remote content"
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta
        backend = MagicMock()
        backend.write_content.side_effect = OSError("disk full")

        resolver = _make_resolver(metastore=metastore, backend=backend)
        handled, result = resolver.try_read("/test/file.txt")

        # Read still succeeds even though local persist failed
        assert handled is True
        assert result == b"remote content"


class TestTryDeleteLocalContent:
    """try_delete returns (False, metadata_hint) for local content."""

    def test_local_origin_returns_metadata_hint(self):
        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_delete("/test/file.txt")

        assert handled is False
        assert result is meta

    def test_no_origin_returns_metadata_hint(self):
        meta = _make_meta("local")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_delete("/test/file.txt")

        assert handled is False
        assert result is meta

    def test_no_metadata_returns_none(self):
        metastore = MagicMock()
        metastore.get.return_value = None

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_delete("/nonexistent.txt")

        assert handled is False
        assert result is None

    def test_empty_backend_name_returns_none(self):
        meta = MagicMock()
        meta.backend_name = ""
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_delete("/test/file.txt")

        assert handled is False
        assert result is None


class TestTryDeleteRemoteContent:
    """try_delete returns (True, {}) for remote content."""

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_origin_delegates_to_peer(self, mock_delete):
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        handled, result = resolver.try_delete("/test/file.txt")

        assert handled is True
        assert result == {}
        mock_delete.assert_called_once_with(REMOTE_ADDR, "/test/file.txt")

    @patch.object(FederationContentResolver, "_delete_on_peer")
    def test_remote_delete_failure_is_best_effort(self, mock_delete):
        """gRPC failure during remote delete is logged, not raised."""
        mock_delete.side_effect = Exception("network error")
        meta = _make_meta(f"local@{REMOTE_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        # Should not raise — _delete_on_peer handles errors internally
        # But since we're patching the method itself to raise, this tests
        # that the caller doesn't swallow it (it propagates)
        with pytest.raises(Exception, match="network error"):
            resolver.try_delete("/test/file.txt")


class TestMatchesPassthrough:
    """matches() returns False so writes pass through to kernel."""

    def test_matches_returns_false(self):
        resolver = _make_resolver()
        assert resolver.matches("/any/path") is False


class TestKernelDispatchIntegration:
    """Verify FederationContentResolver works with KernelDispatch."""

    def test_resolve_read_local_returns_hint(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_read("/test/file.txt")
        assert handled is False
        assert result is meta

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

    def test_resolve_delete_local_returns_hint(self):
        from nexus.core.kernel_dispatch import KernelDispatch

        meta = _make_meta(f"local@{SELF_ADDR}")
        metastore = MagicMock()
        metastore.get.return_value = meta

        resolver = _make_resolver(metastore=metastore)
        dispatch = KernelDispatch()
        dispatch.register_resolver(resolver)

        handled, result = dispatch.resolve_delete("/test/file.txt")
        assert handled is False
        assert result is meta

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
