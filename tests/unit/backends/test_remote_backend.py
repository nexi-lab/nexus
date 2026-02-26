"""Tests for RemoteBackend (Issue #844).

Verifies that RemoteBackend correctly proxies ObjectStoreABC operations
to a remote Nexus server via HTTP/JSON-RPC.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.remote import RemoteBackend
from nexus.contracts.exceptions import (
    RemoteConnectionError,
)
from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> RemoteBackend:
    """Create a RemoteBackend with mocked httpx.Client."""
    with patch("nexus.backends.remote.httpx.Client"):
        return RemoteBackend("http://localhost:2026")


@pytest.fixture
def backend_with_auth() -> RemoteBackend:
    """Create a RemoteBackend with API key."""
    with patch("nexus.backends.remote.httpx.Client"):
        return RemoteBackend("http://localhost:2026", api_key="test-key-123")


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestRemoteBackendProperties:
    """RemoteBackend identity and configuration."""

    def test_name_is_remote(self, backend: RemoteBackend) -> None:
        assert backend.name == "remote"

    def test_server_url_stored(self, backend: RemoteBackend) -> None:
        assert backend._server_url == "http://localhost:2026"

    def test_server_url_trailing_slash_stripped(self) -> None:
        with patch("nexus.backends.remote.httpx.Client"):
            b = RemoteBackend("http://localhost:2026/")
        assert b._server_url == "http://localhost:2026"


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

    def test_write_content_calls_rpc(self, backend: RemoteBackend) -> None:
        """write_content should call _call_rpc('write', ...) with absolute path."""
        with patch.object(
            backend,
            "_call_rpc",
            return_value={"etag": "abc123", "size": 5},
        ) as mock_rpc:
            ctx = self._make_ctx("/path/to/file.txt", "path/to/file.txt")
            result = backend.write_content(b"hello", context=ctx)

            mock_rpc.assert_called_once_with(
                "sys_write",
                {"path": "/path/to/file.txt", "content": b"hello"},
            )
            assert isinstance(result, WriteResult)
            assert result.content_hash == "abc123"
            assert result.size == 5

    def test_read_content_calls_rpc(self, backend: RemoteBackend) -> None:
        """read_content should call _call_rpc('read', ...) with absolute path."""
        with (
            patch.object(backend, "_call_rpc") as mock_rpc,
            patch.object(
                backend._error_handler,
                "_parse_read_response",
                return_value=b"content",
            ),
        ):
            ctx = self._make_ctx("/file.txt", "file.txt")
            result = backend.read_content("hash", context=ctx)

            mock_rpc.assert_called_once_with("sys_read", {"path": "/file.txt"})
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

            mock_rpc.assert_called_once_with("sys_access", {"path": "/file.txt"})
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
                "sys_mkdir",
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

    def test_connect_health_check(self, backend: RemoteBackend) -> None:
        """connect should call GET /api/health."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        backend._session.get.return_value = mock_response

        backend.connect()
        backend._session.get.assert_called_once()

    def test_connect_raises_on_bad_status(self, backend: RemoteBackend) -> None:
        """connect should raise RemoteConnectionError on non-200."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        backend._session.get.return_value = mock_response

        with pytest.raises(RemoteConnectionError):
            backend.connect()

    def test_disconnect_closes_session(self, backend: RemoteBackend) -> None:
        """disconnect should close the httpx session."""
        backend.disconnect()
        backend._session.close.assert_called_once()

    def test_close_alias(self, backend: RemoteBackend) -> None:
        """close() should close the httpx session."""
        backend.close()
        backend._session.close.assert_called_once()
