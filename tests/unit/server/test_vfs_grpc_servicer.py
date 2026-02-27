"""Tests for VFSServicer (gRPC server-side handler).

Verifies that VFSServicer correctly dispatches calls, handles auth,
and maps exceptions to error responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.exceptions import (
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.server.rpc.grpc_servicer import VFSServicer

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
        """No api_key and no auth_provider → open access (anonymous)."""
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
        """Valid api_key in request auth_token → authenticated."""
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
        """Missing auth_token when api_key is configured → auth error."""
        request = _make_request("sys_stat", {"path": "/"}, auth_token="")
        context = MagicMock()

        response = await servicer_with_key.Call(request, context)

        assert response.is_error is True
        payload = decode_rpc_message(response.payload)
        assert "Authentication required" in payload["message"]

    @pytest.mark.anyio
    async def test_wrong_api_key_rejected(self, servicer_with_key) -> None:
        """Wrong auth_token when api_key is configured → auth error."""
        request = _make_request("sys_stat", {"path": "/"}, auth_token="wrong-key")
        context = MagicMock()

        response = await servicer_with_key.Call(request, context)

        assert response.is_error is True
