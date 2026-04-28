"""Tests for RemoteBackend (Issue #844, #1133).

Verifies that RemoteBackend correctly proxies ObjectStoreABC operations
to a remote Nexus server via RPCTransport (gRPC).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.storage.remote import RemoteBackend
from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_transport() -> MagicMock:
    """Create a mock RPCTransport."""
    transport = MagicMock()
    transport.server_address = "localhost:2028"
    return transport


@pytest.fixture
def backend(mock_transport) -> RemoteBackend:
    """Create a RemoteBackend with mocked RPCTransport."""
    return RemoteBackend(mock_transport)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestRemoteBackendProperties:
    """RemoteBackend identity and configuration."""

    def test_name_is_remote(self, backend: RemoteBackend) -> None:
        assert backend.name == "remote"

    def test_transport_stored(self, backend: RemoteBackend, mock_transport) -> None:
        assert backend._transport is mock_transport


# ---------------------------------------------------------------------------
# RPC Dispatch Tests
# ---------------------------------------------------------------------------


class TestRemoteBackendRPC:
    """Each method calls _call_rpc with correct args.

    The kernel sets ``virtual_path`` (absolute) and ``backend_path``
    (mount-stripped) on OperationContext before calling backend methods.
    RemoteBackend prefers ``virtual_path`` for server calls.
    """

    @staticmethod
    def _make_ctx(virtual_path: str = "/file.txt", backend_path: str = "file.txt") -> MagicMock:
        ctx = MagicMock()
        ctx.virtual_path = virtual_path
        ctx.backend_path = backend_path
        return ctx

    def test_write_content_uses_typed_rpc(self, backend: RemoteBackend, mock_transport) -> None:
        """write_content should call transport.write_file() (typed RPC)."""
        mock_transport.write_file.return_value = {"content_id": "abc123", "size": 5}
        ctx = self._make_ctx("/path/to/file.txt", "path/to/file.txt")
        result = backend.write_content(b"hello", context=ctx)

        mock_transport.write_file.assert_called_once_with("/path/to/file.txt", b"hello")
        assert isinstance(result, WriteResult)
        assert result.content_id == "abc123"
        assert result.size == 5

    def test_read_content_uses_typed_rpc(self, backend: RemoteBackend, mock_transport) -> None:
        """read_content should call transport.read_file() (typed RPC)."""
        mock_transport.read_file.return_value = b"content"
        ctx = self._make_ctx("/file.txt", "file.txt")
        result = backend.read_content("hash", context=ctx)

        mock_transport.read_file.assert_called_once_with("/file.txt", content_id="hash")
        assert result == b"content"

    def test_delete_content_is_noop(self, backend: RemoteBackend) -> None:
        """delete_content is a no-op — server-side delete via RemoteMetastore."""
        with patch.object(backend, "_call_rpc") as mock_rpc:
            ctx = self._make_ctx("/file.txt", "file.txt")
            backend.delete_content("hash", context=ctx)

            mock_rpc.assert_not_called()

    def test_content_exists_calls_rpc(self, backend: RemoteBackend) -> None:
        """content_exists should call _call_rpc('exists', ...) with absolute path."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"exists": True},
        ) as mock_rpc:
            ctx = self._make_ctx("/file.txt", "file.txt")
            result = backend.content_exists("hash", context=ctx)

            mock_rpc.assert_called_once_with("access", {"path": "/file.txt"})
            assert result is True

    def test_get_content_size_calls_rpc(self, backend: RemoteBackend) -> None:
        """get_content_size should call _call_rpc('stat', ...) with absolute path."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"size": 1024},
        ) as mock_rpc:
            ctx = self._make_ctx("/file.txt", "file.txt")
            result = backend.get_content_size("hash", context=ctx)

            mock_rpc.assert_called_once_with("sys_stat", {"path": "/file.txt"})
            assert result == 1024

    def test_backend_path_fallback(self, backend: RemoteBackend) -> None:
        """When virtual_path is None, fall back to backend_path with / prefix."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"size": 512},
        ) as mock_rpc:
            ctx = MagicMock()
            ctx.virtual_path = None
            ctx.backend_path = "workspace/data.txt"
            backend.get_content_size("hash", context=ctx)

            mock_rpc.assert_called_once_with("sys_stat", {"path": "/workspace/data.txt"})

    def test_no_context_defaults_to_root(self, backend: RemoteBackend) -> None:
        """When context is None, use root path /."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"size": 0},
        ) as mock_rpc:
            backend.get_content_size("hash", context=None)

            mock_rpc.assert_called_once_with("sys_stat", {"path": "/"})

    def test_mkdir_calls_rpc(self, backend: RemoteBackend) -> None:
        """mkdir should call _call_rpc('mkdir', ...)."""
        with patch.object(backend, "_call_rpc") as mock_rpc:
            backend.mkdir("/test/dir", parents=True, exist_ok=True)

            mock_rpc.assert_called_once_with(
                "mkdir",
                {"path": "/test/dir", "parents": True, "exist_ok": True},
            )

    def test_rmdir_calls_rpc(self, backend: RemoteBackend) -> None:
        """rmdir should call _call_rpc('rmdir', ...)."""
        with patch.object(backend, "_call_rpc") as mock_rpc:
            backend.rmdir("/test/dir", recursive=True)

            mock_rpc.assert_called_once_with(
                "sys_rmdir",
                {"path": "/test/dir", "recursive": True},
            )

    def test_list_dir_calls_rpc(self, backend: RemoteBackend) -> None:
        """list_dir should call _call_rpc('list', ...)."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value=["file1.txt", "subdir"],
        ) as mock_rpc:
            result = backend.list_dir("/test/dir")

            mock_rpc.assert_called_once_with("sys_readdir", {"path": "/test/dir"})
            assert result == ["file1.txt", "subdir"]

    def test_list_dir_handles_dict_response(self, backend: RemoteBackend) -> None:
        """list_dir should handle dict response with 'items' key."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"items": [{"name": "a.txt"}, {"name": "b.txt"}]},
        ):
            result = backend.list_dir("/test")
            assert result == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestRemoteBackendLifecycle:
    """Connection lifecycle operations."""

    def test_close_is_noop(self, backend: RemoteBackend) -> None:
        """close() should be a no-op (transport lifecycle managed by factory)."""
        backend.close()  # Should not raise
