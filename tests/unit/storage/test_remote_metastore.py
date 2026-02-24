"""Tests for RemoteMetastore (Issue #844).

Verifies that RemoteMetastore correctly proxies MetastoreABC operations
to a remote Nexus server via HTTP/JSON-RPC.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC
from nexus.storage.remote_metastore import RemoteMetastore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def metastore() -> RemoteMetastore:
    """Create a RemoteMetastore with mocked httpx.Client."""
    with patch("nexus.storage.remote_metastore.httpx.Client"):
        return RemoteMetastore("http://localhost:2026")


@pytest.fixture
def metastore_with_auth() -> RemoteMetastore:
    """Create a RemoteMetastore with API key."""
    with patch("nexus.storage.remote_metastore.httpx.Client"):
        return RemoteMetastore("http://localhost:2026", api_key="test-key-123")


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

    def test_server_url_stored(self, metastore: RemoteMetastore) -> None:
        assert metastore._server_url == "http://localhost:2026"

    def test_server_url_trailing_slash_stripped(self) -> None:
        with patch("nexus.storage.remote_metastore.httpx.Client"):
            m = RemoteMetastore("http://localhost:2026/")
        assert m._server_url == "http://localhost:2026"


# ---------------------------------------------------------------------------
# RPC Dispatch Tests
# ---------------------------------------------------------------------------


class TestRemoteMetastoreRPC:
    """Each method calls _call_rpc with correct args."""

    def test_get_calls_stat(self, metastore: RemoteMetastore) -> None:
        """get() should call _call_rpc('stat', ...)."""
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

            mock_rpc.assert_called_once_with("stat", {"path": "/test.txt"})
            assert result is not None
            assert isinstance(result, FileMetadata)
            assert result.path == "/test.txt"
            assert result.size == 100

    def test_get_returns_none_on_missing(self, metastore: RemoteMetastore) -> None:
        """get() should return None when server returns None."""
        with patch.object(metastore, "_call_rpc", return_value=None):
            result = metastore.get("/nonexistent.txt")
            assert result is None

    def test_get_returns_none_on_exception(self, metastore: RemoteMetastore) -> None:
        """get() should return None when RPC raises an exception."""
        with patch.object(metastore, "_call_rpc", side_effect=Exception("boom")):
            result = metastore.get("/error.txt")
            assert result is None

    def test_put_calls_set_metadata(self, metastore: RemoteMetastore) -> None:
        """put() should call _call_rpc('set_metadata', ...)."""
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
            assert call_args[0][0] == "set_metadata"
            assert call_args[0][1]["path"] == "/test.txt"
            assert call_args[0][1]["consistency"] == "sc"

    def test_delete_calls_delete(self, metastore: RemoteMetastore) -> None:
        """delete() should call _call_rpc('delete', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={"path": "/test.txt"},
        ) as mock_rpc:
            result = metastore.delete("/test.txt")

            mock_rpc.assert_called_once_with("delete", {"path": "/test.txt", "consistency": "sc"})
            assert result is not None
            assert result["path"] == "/test.txt"

    def test_exists_calls_exists(self, metastore: RemoteMetastore) -> None:
        """exists() should call _call_rpc('exists', ...)."""
        with patch.object(
            metastore,
            "_call_rpc",
            return_value={"exists": True},
        ) as mock_rpc:
            result = metastore.exists("/test.txt")

            mock_rpc.assert_called_once_with("exists", {"path": "/test.txt"})
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
        """list() should call _call_rpc('list', ...)."""
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
                "list",
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

    def test_close_closes_session(self, metastore: RemoteMetastore) -> None:
        """close() should close the httpx session."""
        metastore.close()
        metastore._session.close.assert_called_once()
