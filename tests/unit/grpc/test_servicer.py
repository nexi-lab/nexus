"""Tests for VFSServicer (gRPC server-side handler).

Verifies that VFSServicer correctly dispatches calls, handles auth,
maps exceptions to error responses, and handles typed RPCs (Phase 2).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.contracts.rpc_types import RPCErrorCode
from nexus.grpc.servicer import VFSServicer
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nexus_fs() -> MagicMock:
    fs = MagicMock()
    # NexusFS methods are now sync (Phase 7)
    fs.read = MagicMock(return_value=b"")
    fs.write = MagicMock(return_value={})
    fs.sys_stat = MagicMock(return_value=None)
    fs.sys_unlink = MagicMock(return_value=None)
    fs.rmdir = MagicMock(return_value=None)
    return fs


@pytest.fixture
def mock_exposed_methods() -> dict:
    return {}


@pytest.fixture
def servicer(mock_nexus_fs, mock_exposed_methods) -> VFSServicer:
    return VFSServicer(
        nexus_fs=mock_nexus_fs,
        exposed_methods=mock_exposed_methods,
        auth_provider=None,
        api_key=None,
        subscription_manager=None,
    )


@pytest.fixture
def servicer_with_key(mock_nexus_fs, mock_exposed_methods) -> VFSServicer:
    return VFSServicer(
        nexus_fs=mock_nexus_fs,
        exposed_methods=mock_exposed_methods,
        auth_provider=None,
        api_key="test-api-key",
        subscription_manager=None,
    )


def _make_request(method: str, params: dict | None = None, auth_token: str = "") -> MagicMock:
    """Build a mock CallRequest."""
    req = MagicMock()
    req.method = method
    req.payload = encode_rpc_message(params or {})
    req.auth_token = auth_token
    return req


# ---------------------------------------------------------------------------
# Dispatch Tests
# ---------------------------------------------------------------------------


class TestVFSServicerDispatch:
    """VFSServicer dispatches to correct handlers."""

    @pytest.mark.anyio
    async def test_success_response(self, servicer) -> None:
        """Successful dispatch returns is_error=False with result payload."""
        request = _make_request("sys_stat", {"path": "/test.txt"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            return_value={"size": 1024, "path": "/test.txt"},
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is False
        payload = decode_rpc_message(response.payload)
        assert payload["result"]["size"] == 1024

    @pytest.mark.anyio
    async def test_dispatch_called_with_correct_args(self, servicer, mock_nexus_fs) -> None:
        """dispatch_method receives nexus_fs, exposed_methods, etc."""
        request = _make_request("sys_read", {"path": "/file.txt"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            return_value=b"content",
        ) as mock_dispatch:
            await servicer.Call(request, context)

            mock_dispatch.assert_called_once()
            call_kwargs = mock_dispatch.call_args[1]
            assert call_kwargs["nexus_fs"] is mock_nexus_fs


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestVFSServicerErrors:
    """VFSServicer maps exceptions to error responses."""

    @pytest.mark.anyio
    async def test_file_not_found(self, servicer) -> None:
        request = _make_request("sys_read", {"path": "/missing.txt"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            side_effect=NexusFileNotFoundError("/missing.txt"),
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert payload["code"] == -32000  # FILE_NOT_FOUND

    @pytest.mark.anyio
    async def test_invalid_path(self, servicer) -> None:
        request = _make_request("sys_read", {"path": "../escape"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            side_effect=InvalidPathError("Path traversal"),
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert payload["code"] == -32002  # INVALID_PATH

    @pytest.mark.anyio
    async def test_permission_error(self, servicer) -> None:
        request = _make_request("access", {"path": "/protected"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            side_effect=NexusPermissionError("Access denied"),
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert payload["code"] == -32004  # PERMISSION_ERROR

    @pytest.mark.anyio
    async def test_validation_error(self, servicer) -> None:
        request = _make_request("sys_stat", {"path": "/bad"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            side_effect=ValidationError("Missing path"),
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert payload["code"] == -32005  # VALIDATION_ERROR

    @pytest.mark.anyio
    async def test_internal_error(self, servicer) -> None:
        request = _make_request("sys_read", {"path": "/"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert payload["code"] == -32603  # INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Auth Tests
# ---------------------------------------------------------------------------


class TestVFSServicerAuth:
    """VFSServicer authentication."""

    @pytest.mark.anyio
    async def test_open_access_no_key(self, servicer) -> None:
        """No api_key and no auth_provider -> open access (anonymous)."""
        request = _make_request("sys_stat", {"path": "/"})
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            return_value={},
        ):
            response = await servicer.Call(request, context)

        assert response.is_error is False

    @pytest.mark.anyio
    async def test_valid_api_key(self, servicer_with_key) -> None:
        """Valid api_key in request auth_token -> authenticated."""
        request = _make_request("sys_stat", {"path": "/"}, auth_token="test-api-key")
        context = MagicMock()

        with patch(
            "nexus.server.rpc.dispatch.dispatch_method",
            new_callable=AsyncMock,
            return_value={},
        ):
            response = await servicer_with_key.Call(request, context)

        assert response.is_error is False

    @pytest.mark.anyio
    async def test_missing_api_key_rejected(self, servicer_with_key) -> None:
        """Missing auth_token when api_key is configured -> auth error."""
        request = _make_request("sys_stat", {"path": "/"}, auth_token="")
        context = MagicMock()

        response = await servicer_with_key.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert "Authentication required" in payload["message"]

    @pytest.mark.anyio
    async def test_wrong_api_key_rejected(self, servicer_with_key) -> None:
        """Wrong auth_token when api_key is configured -> auth error."""
        request = _make_request("sys_stat", {"path": "/"}, auth_token="wrong-key")
        context = MagicMock()

        response = await servicer_with_key.Call(request, context)

        assert response.is_error is True


# ---------------------------------------------------------------------------
# Typed RPC Tests (Phase 2)
# ---------------------------------------------------------------------------


def _make_typed_request(cls_name: str, **kwargs) -> MagicMock:
    """Build a mock typed request with given attributes."""
    req = MagicMock()
    for k, v in kwargs.items():
        setattr(req, k, v)
    return req


class TestVFSServicerTypedRPCs:
    """VFSServicer typed RPC handlers: Read, Write, Delete, Ping."""

    @pytest.mark.anyio
    async def test_read_success(self, servicer, mock_nexus_fs) -> None:
        """Read returns content via sys_read (full VFS path)."""
        mock_nexus_fs.sys_read = MagicMock(return_value=b"hello world")
        request = _make_typed_request(
            "ReadRequest", path="/test.txt", auth_token="", content_id="sha256-abc"
        )
        context = MagicMock()

        response = await servicer.Read(request, context)

        assert response.is_error is False
        assert response.content == b"hello world"
        assert response.size == 11

    @pytest.mark.anyio
    async def test_read_not_found(self, servicer, mock_nexus_fs) -> None:
        """Read returns is_error=True with FILE_NOT_FOUND on missing file."""
        mock_nexus_fs.sys_read = MagicMock(side_effect=NexusFileNotFoundError("/missing.txt"))
        request = _make_typed_request(
            "ReadRequest", path="/missing.txt", auth_token="", content_id="sha256-xyz"
        )
        context = MagicMock()

        response = await servicer.Read(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.error_payload)
        assert payload["code"] == RPCErrorCode.FILE_NOT_FOUND.value

    @pytest.mark.anyio
    async def test_write_success(self, servicer, mock_nexus_fs) -> None:
        """Write returns etag and size from write() (Issue #2787)."""
        mock_nexus_fs.write = MagicMock(return_value={"etag": "sha256-xyz", "size": 4})
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag=""
        )
        context = MagicMock()

        response = await servicer.Write(request, context)

        assert response.is_error is False
        assert response.etag == "sha256-xyz"
        assert response.size == 4

    @pytest.mark.anyio
    async def test_write_calls_write_not_sys_write(self, servicer, mock_nexus_fs) -> None:
        """Write handler uses write() (returns dict) not sys_write() (returns int).

        Issue #2787: sys_write() returns int (POSIX), so etag was always empty.
        """
        mock_nexus_fs.write = MagicMock(return_value={"etag": "sha256-xyz", "size": 4})
        mock_nexus_fs.sys_write = MagicMock(return_value=4)
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag=""
        )
        context = MagicMock()

        await servicer.Write(request, context)

        # Verify write() was called (not sys_write)
        mock_nexus_fs.write.assert_called_once()
        mock_nexus_fs.sys_write.assert_not_called()

    @pytest.mark.anyio
    async def test_write_with_occ_etag(self, servicer, mock_nexus_fs) -> None:
        """Write with etag uses occ_write() for compare-and-swap (Issue #2787)."""
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag="sha256-old"
        )
        context = MagicMock()

        with patch(
            "nexus.lib.occ.occ_write",
            new_callable=AsyncMock,
            return_value={"etag": "sha256-new", "size": 4},
        ) as mock_occ:
            response = await servicer.Write(request, context)

        assert response.is_error is False
        assert response.etag == "sha256-new"
        mock_occ.assert_called_once()

    @pytest.mark.anyio
    async def test_write_conflict(self, servicer, mock_nexus_fs) -> None:
        """Write returns CONFLICT error on etag mismatch."""
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag="old"
        )
        context = MagicMock()

        with patch(
            "nexus.lib.occ.occ_write",
            new_callable=AsyncMock,
            side_effect=ConflictError("/file.txt", expected_etag="old", current_etag="new"),
        ):
            response = await servicer.Write(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.error_payload)
        assert payload["code"] == -32006

    @pytest.mark.anyio
    async def test_delete_success(self, servicer, mock_nexus_fs) -> None:
        """Delete returns success=True on sys_unlink for files."""
        request = _make_typed_request(
            "DeleteRequest", path="/file.txt", auth_token="", recursive=False
        )
        context = MagicMock()

        # metadata.get is called via asyncio.to_thread (it's sync)
        file_meta = MagicMock(mime_type="application/octet-stream")
        mock_nexus_fs.metadata.get.return_value = file_meta
        mock_nexus_fs.sys_unlink = MagicMock(return_value=None)

        response = await servicer.Delete(request, context)

        assert response.is_error is False
        assert response.success is True
        mock_nexus_fs.sys_unlink.assert_called_once()

    @pytest.mark.anyio
    async def test_delete_recursive(self, servicer, mock_nexus_fs) -> None:
        """Delete with recursive=True calls rmdir for directories."""
        request = _make_typed_request("DeleteRequest", path="/dir", auth_token="", recursive=True)
        context = MagicMock()

        dir_meta = MagicMock(mime_type="inode/directory")
        mock_nexus_fs.metadata.get.return_value = dir_meta
        mock_nexus_fs.rmdir = MagicMock(return_value=None)

        response = await servicer.Delete(request, context)

        assert response.success is True
        mock_nexus_fs.rmdir.assert_called_once()

    @pytest.mark.anyio
    async def test_ping_response(self, servicer) -> None:
        """Ping returns version and zone_id."""
        request = _make_typed_request("PingRequest", auth_token="")
        context = MagicMock()

        response = await servicer.Ping(request, context)

        assert response.version  # Non-empty version string
        assert response.zone_id == "root"
        assert response.uptime_seconds >= 0

    @pytest.mark.anyio
    async def test_read_auth_required(self, servicer_with_key) -> None:
        """Read returns PERMISSION_ERROR when auth is required but token missing."""
        request = _make_typed_request("ReadRequest", path="/file.txt", auth_token="")
        context = MagicMock()

        response = await servicer_with_key.Read(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.error_payload)
        assert payload["code"] == -32004
