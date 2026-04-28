from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from nexus.backends.storage.remote_zone import RemoteZoneBackend
from nexus.contracts.exceptions import ZoneReadOnlyError
from nexus.contracts.types import OperationContext


@pytest.fixture
def mock_transport() -> MagicMock:
    t = MagicMock()
    t.write_file.return_value = {"etag": "abc", "size": 5}
    t.read_file.return_value = b"hello"
    return t


@pytest.fixture
def readonly_backend(mock_transport: MagicMock) -> RemoteZoneBackend:
    return RemoteZoneBackend(zone_id="company", transport=mock_transport, permission="r")


@pytest.fixture
def readwrite_backend(mock_transport: MagicMock) -> RemoteZoneBackend:
    return RemoteZoneBackend(zone_id="shared", transport=mock_transport, permission="rw")


class TestRemoteZoneBackendIdentity:
    def test_name_includes_zone_id(self, readonly_backend: RemoteZoneBackend) -> None:
        assert "company" in readonly_backend.name

    def test_zone_id_stored(self, readonly_backend: RemoteZoneBackend) -> None:
        assert readonly_backend.zone_id == "company"

    def test_permission_stored(self, readonly_backend: RemoteZoneBackend) -> None:
        assert readonly_backend.permission == "r"


class TestRemoteZoneBackendReadOnly:
    def test_write_content_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError, match="company"):
            readonly_backend.write_content(b"data")
        mock_transport.write_file.assert_not_called()

    def test_delete_content_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError, match="company"):
            readonly_backend.delete_content("some_id")
        mock_transport.delete_file.assert_not_called()

    def test_mkdir_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError):
            readonly_backend.mkdir("/some/path")
        mock_transport.call_rpc.assert_not_called()

    def test_rmdir_raises_zone_read_only(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        with pytest.raises(ZoneReadOnlyError):
            readonly_backend.rmdir("/some/path")
        mock_transport.call_rpc.assert_not_called()

    def test_read_content_passes_through(
        self, readonly_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        mock_transport.read_file.return_value = b"content"
        ctx = OperationContext(user_id="u", groups=[], backend_path="/file.txt")
        result = readonly_backend.read_content("id", context=ctx)
        assert result == b"content"
        mock_transport.read_file.assert_called_once()


class TestRemoteZoneBackendReadWrite:
    def test_write_content_delegates_to_transport(
        self, readwrite_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        ctx = OperationContext(user_id="u", groups=[], backend_path="/note.md")
        readwrite_backend.write_content(b"hello", context=ctx)
        mock_transport.write_file.assert_called_once()

    def test_delete_content_delegates_to_transport(
        self, readwrite_backend: RemoteZoneBackend, mock_transport: MagicMock
    ) -> None:
        ctx = OperationContext(user_id="u", groups=[], backend_path="/note.md")
        readwrite_backend.delete_content("id", context=ctx)
        mock_transport.delete_file.assert_called_once()

    def test_unknown_permission_treated_as_read_only(self, mock_transport: MagicMock) -> None:
        # mypy would catch this at type-check time, but verify runtime behavior:
        # invalid permission values (outside Literal["r", "rw"]) are treated as read-only
        backend = RemoteZoneBackend(
            zone_id="z", transport=mock_transport, permission=cast(str, "rwx")
        )
        with pytest.raises(ZoneReadOnlyError):
            backend.write_content(b"data")
