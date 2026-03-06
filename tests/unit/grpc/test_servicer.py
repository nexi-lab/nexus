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
from nexus.grpc.servicer import VFSServicer
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nexus_fs() -> MagicMock:
    return MagicMock()


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
        request = _make_request("sys_access", {"path": "/protected"})
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
    """VFSServicer typed RPC handlers: Read, Write, Delete, StreamRead, Ping."""

    @pytest.mark.anyio
    async def test_read_success(self, servicer, mock_nexus_fs) -> None:
        """Read returns content, etag, size from sys_read."""
        mock_nexus_fs.sys_read.return_value = {
            "content": b"hello world",
            "etag": "sha256-abc",
            "size": 11,
        }
        request = _make_typed_request("ReadRequest", path="/test.txt", auth_token="")
        context = MagicMock()

        with patch("nexus.grpc.servicer.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"content": b"hello world", "etag": "sha256-abc", "size": 11}
            response = await servicer.Read(request, context)

        assert response.is_error is False
        assert response.content == b"hello world"
        assert response.etag == "sha256-abc"
        assert response.size == 11

    @pytest.mark.anyio
    async def test_read_not_found(self, servicer) -> None:
        """Read returns is_error=True with FILE_NOT_FOUND on missing file."""
        request = _make_typed_request("ReadRequest", path="/missing.txt", auth_token="")
        context = MagicMock()

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=NexusFileNotFoundError("/missing.txt"),
        ):
            response = await servicer.Read(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.error_payload)
        assert payload["code"] == -32000

    @pytest.mark.anyio
    async def test_write_success(self, servicer) -> None:
        """Write returns etag and size from metadata after sys_write."""
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag=""
        )
        context = MagicMock()

        # sys_write returns int; servicer looks up metadata for etag
        mock_meta = MagicMock(etag="sha256-xyz", size=4)
        servicer._nexus_fs.metadata.get.return_value = mock_meta

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=4,
        ):
            response = await servicer.Write(request, context)

        assert response.is_error is False
        assert response.etag == "sha256-xyz"
        assert response.size == 4

    @pytest.mark.anyio
    async def test_write_conflict(self, servicer) -> None:
        """Write returns CONFLICT error on etag mismatch."""
        request = _make_typed_request(
            "WriteRequest", path="/file.txt", content=b"data", auth_token="", etag="old"
        )
        context = MagicMock()

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=ConflictError("/file.txt", expected_etag="old", current_etag="new"),
        ):
            response = await servicer.Write(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.error_payload)
        assert payload["code"] == -32006

    @pytest.mark.anyio
    async def test_delete_success(self, servicer) -> None:
        """Delete returns success=True on sys_unlink."""
        request = _make_typed_request(
            "DeleteRequest", path="/file.txt", auth_token="", recursive=False
        )
        context = MagicMock()

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await servicer.Delete(request, context)

        assert response.is_error is False
        assert response.success is True

    @pytest.mark.anyio
    async def test_delete_recursive(self, servicer) -> None:
        """Delete with recursive=True calls sys_rmdir."""
        request = _make_typed_request("DeleteRequest", path="/dir", auth_token="", recursive=True)
        context = MagicMock()

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_thread:
            response = await servicer.Delete(request, context)

        assert response.success is True
        # Verify sys_rmdir was called (not sys_unlink) — check mock name
        call_args = mock_thread.call_args
        func = call_args[0][0]
        assert "sys_rmdir" in str(func)

    @pytest.mark.anyio
    async def test_stream_read_chunks(self, servicer) -> None:
        """StreamRead yields chunks for large content."""
        request = _make_typed_request(
            "StreamReadRequest", path="/big.bin", auth_token="", chunk_size=5
        )
        context = MagicMock()
        context.cancelled.return_value = False

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=b"hello world!",  # 12 bytes
        ):
            chunks = []
            async for chunk in servicer.StreamRead(request, context):
                chunks.append(chunk)

        # 12 bytes / 5 chunk_size = 3 chunks (5 + 5 + 2)
        assert len(chunks) == 3
        assert chunks[0].data == b"hello"
        assert chunks[0].offset == 0
        assert chunks[0].is_last is False
        assert chunks[1].data == b" worl"
        assert chunks[2].data == b"d!"
        assert chunks[2].is_last is True

    @pytest.mark.anyio
    async def test_stream_read_empty_file(self, servicer) -> None:
        """StreamRead yields single empty chunk for empty file."""
        request = _make_typed_request(
            "StreamReadRequest", path="/empty.txt", auth_token="", chunk_size=0
        )
        context = MagicMock()
        context.cancelled.return_value = False

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=b"",
        ):
            chunks = []
            async for chunk in servicer.StreamRead(request, context):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0].data == b""
        assert chunks[0].is_last is True

    @pytest.mark.anyio
    async def test_stream_read_error(self, servicer) -> None:
        """StreamRead yields error chunk on file not found."""
        request = _make_typed_request(
            "StreamReadRequest", path="/missing.bin", auth_token="", chunk_size=0
        )
        context = MagicMock()

        with patch(
            "nexus.grpc.servicer.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=NexusFileNotFoundError("/missing.bin"),
        ):
            chunks = []
            async for chunk in servicer.StreamRead(request, context):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0].is_error is True
        payload = decode_rpc_message(chunks[0].error_payload)
        assert payload["code"] == -32000

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
