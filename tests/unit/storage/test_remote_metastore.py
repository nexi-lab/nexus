"""Tests for RemoteMetastore (Issue #844, #1133).

Verifies that RemoteMetastore correctly proxies MetastoreABC operations
to a remote Nexus server via RPCTransport (gRPC).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC
from nexus.storage.remote_metastore import RemoteMetastore

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
def metastore(mock_transport) -> RemoteMetastore:
    """Create a RemoteMetastore with mocked RPCTransport."""
    return RemoteMetastore(mock_transport)


# ---------------------------------------------------------------------------
# ABC Conformance
# ---------------------------------------------------------------------------


class TestRemoteMetastoreConformance:
    """RemoteMetastore satisfies MetastoreABC."""

    def test_is_metastore_subclass(self) -> None:
        assert issubclass(RemoteMetastore, MetastoreABC)

    def test_instance_check(self, metastore: RemoteMetastore) -> None:
        assert isinstance(metastore, MetastoreABC)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestRemoteMetastoreProperties:
    """RemoteMetastore configuration."""

    def test_transport_stored(self, metastore: RemoteMetastore, mock_transport) -> None:
        assert metastore._transport is mock_transport


# ---------------------------------------------------------------------------
# RPC Dispatch Tests
# ---------------------------------------------------------------------------


class TestRemoteMetastoreRPC:
    """Each method calls _call_rpc with correct args."""

    def test_get_calls_stat(self, metastore: RemoteMetastore) -> None:
        """get() should call _call_rpc('sys_stat', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={
                "path": "/test.txt",
                "backend_name": "local",
                "physical_path": "/data/test.txt",
                "size": 100,
            },
        ) as mock_rpc:
            result = metastore.get("/test.txt")

            mock_rpc.assert_called_once_with("sys_stat", {"path": "/test.txt"})
            assert result is not None
            assert isinstance(result, FileMetadata)
            assert result.path == "/test.txt"
            assert result.size == 100

    def test_get_returns_none_on_missing(self, metastore: RemoteMetastore) -> None:
        """get() should return None when server returns None."""
        with patch.object(metastore, "_call_rpc", return_value=None):
            result = metastore.get("/nonexistent.txt")
            assert result is None

    def test_get_propagates_exception(self, metastore: RemoteMetastore) -> None:
        """get() should propagate RPC exceptions so callers can distinguish errors from not-found."""
        with (
            patch.object(metastore, "_call_rpc", side_effect=Exception("boom")),
            pytest.raises(Exception, match="boom"),
        ):
            metastore.get("/error.txt")

    def test_put_calls_sys_setattr(self, metastore: RemoteMetastore) -> None:
        """put() should call _call_rpc('sys_setattr', ...)."""
        metadata = FileMetadata(
            path="/test.txt",
            backend_name="local",
            physical_path="/data/test.txt",
            size=100,
        )
        with patch.object(metastore, "_call_rpc") as mock_rpc:
            metastore.put(metadata)

            mock_rpc.assert_called_once()
            call_args = mock_rpc.call_args
            assert call_args[0][0] == "sys_setattr"
            assert call_args[0][1]["path"] == "/test.txt"
            assert call_args[0][1]["consistency"] == "sc"

    def test_delete_calls_delete(self, metastore: RemoteMetastore) -> None:
        """delete() should call _call_rpc('sys_unlink', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={"path": "/test.txt"},
        ) as mock_rpc:
            result = metastore.delete("/test.txt")

            mock_rpc.assert_called_once_with(
                "sys_unlink", {"path": "/test.txt", "consistency": "sc"}
            )
            assert result is not None
            assert result["path"] == "/test.txt"

    def test_exists_calls_exists(self, metastore: RemoteMetastore) -> None:
        """exists() should call _call_rpc('sys_access', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={"exists": True},
        ) as mock_rpc:
            result = metastore.exists("/test.txt")

            mock_rpc.assert_called_once_with("sys_access", {"path": "/test.txt"})
            assert result is True

    def test_exists_returns_false_on_missing(self, metastore: RemoteMetastore) -> None:
        """exists() should return False when server says not exists."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={"exists": False},
        ):
            result = metastore.exists("/nonexistent.txt")
            assert result is False

    def test_list_calls_list(self, metastore: RemoteMetastore) -> None:
        """list() should call _call_rpc('sys_readdir', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value=[
                {"path": "/a.txt", "backend_name": "local", "physical_path": "/a.txt", "size": 10},
                {"path": "/b.txt", "backend_name": "local", "physical_path": "/b.txt", "size": 20},
            ],
        ) as mock_rpc:
            result = metastore.list("/", recursive=True)

            mock_rpc.assert_called_once_with(
                "sys_readdir",
                {"path": "/", "recursive": True},
            )
            assert len(result) == 2
            assert all(isinstance(m, FileMetadata) for m in result)
            assert result[0].path == "/a.txt"

    def test_list_returns_empty_on_none(self, metastore: RemoteMetastore) -> None:
        """list() should return empty list when server returns None."""
        with patch.object(metastore, "_call_rpc", return_value=None):
            result = metastore.list("/empty")
            assert result == []

    def test_list_handles_string_items(self, metastore: RemoteMetastore) -> None:
        """list() should handle string items in response."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value=["/path/a.txt", "/path/b.txt"],
        ):
            result = metastore.list("/path")
            assert len(result) == 2
            assert result[0].path == "/path/a.txt"

    def test_list_handles_dict_with_items(self, metastore: RemoteMetastore) -> None:
        """list() should handle dict response with 'items' key."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={
                "items": [
                    {"path": "/x.txt", "backend_name": "r", "physical_path": "/x", "size": 5},
                ]
            },
        ):
            result = metastore.list("/")
            assert len(result) == 1
            assert result[0].path == "/x.txt"


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestRemoteMetastoreLifecycle:
    """Connection lifecycle operations."""

    def test_close_is_noop(self, metastore: RemoteMetastore) -> None:
        """close() should be a no-op (transport lifecycle managed by factory)."""
        metastore.close()  # Should not raise
