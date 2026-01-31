"""Unit tests for NexusRPCTransport."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from nexus.core.rpc_transport import NexusRPCTransport, RPCError, TransportError


class TestNexusRPCTransport:
    """Tests for NexusRPCTransport."""

    def test_init_defaults(self) -> None:
        """Test initialization with default values."""
        transport = NexusRPCTransport("http://localhost:2026")
        assert transport.endpoint == "http://localhost:2026"
        assert transport._auth_token is None
        transport.close()

    def test_init_with_auth_token(self) -> None:
        """Test initialization with auth token."""
        transport = NexusRPCTransport("http://localhost:2026", auth_token="test-token")
        assert transport._auth_token == "test-token"
        transport.close()

    def test_init_strips_trailing_slash(self) -> None:
        """Test that trailing slash is stripped from endpoint."""
        transport = NexusRPCTransport("http://localhost:2026/")
        assert transport.endpoint == "http://localhost:2026"
        transport.close()

    def test_repr(self) -> None:
        """Test string representation."""
        transport = NexusRPCTransport("http://localhost:2026")
        assert "http://localhost:2026" in repr(transport)
        transport.close()

    def test_context_manager(self) -> None:
        """Test context manager protocol."""
        with NexusRPCTransport("http://localhost:2026") as transport:
            assert transport.endpoint == "http://localhost:2026"
        # Client should be closed after exiting context

    @patch.object(httpx.Client, "post")
    def test_call_success(self, mock_post: MagicMock) -> None:
        """Test successful RPC call."""
        # Mock successful response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc": "2.0", "id": "1", "result": {"data": "test"}}'
        mock_post.return_value = mock_response

        with NexusRPCTransport("http://localhost:2026") as transport:
            result = transport.call("test_method", {"param": "value"})

        assert result == {"data": "test"}
        mock_post.assert_called_once()

    @patch.object(httpx.Client, "post")
    def test_call_with_timeout_override(self, mock_post: MagicMock) -> None:
        """Test RPC call with timeout override."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc": "2.0", "id": "1", "result": {}}'
        mock_post.return_value = mock_response

        with NexusRPCTransport("http://localhost:2026") as transport:
            transport.call("test_method", timeout=60.0)

        # Verify timeout was passed
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs.get("timeout") is not None

    @patch.object(httpx.Client, "post")
    def test_call_rpc_error(self, mock_post: MagicMock) -> None:
        """Test RPC call that returns error."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = (
            b'{"jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "File not found"}}'
        )
        mock_post.return_value = mock_response

        with (
            NexusRPCTransport("http://localhost:2026") as transport,
            pytest.raises(RPCError) as exc_info,
        ):
            transport.call("read", {"path": "/nonexistent"})

        assert exc_info.value.code == -32000
        assert "File not found" in exc_info.value.message

    @patch.object(httpx.Client, "post")
    def test_call_http_error(self, mock_post: MagicMock) -> None:
        """Test RPC call with HTTP error status."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response

        with (
            NexusRPCTransport("http://localhost:2026") as transport,
            pytest.raises(TransportError) as exc_info,
        ):
            transport.call("test_method")

        assert "500" in str(exc_info.value)

    @patch.object(httpx.Client, "post")
    def test_call_connection_error(self, mock_post: MagicMock) -> None:
        """Test RPC call with connection error."""
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with (
            NexusRPCTransport("http://localhost:2026") as transport,
            pytest.raises(TransportError) as exc_info,
        ):
            transport.call("test_method")

        assert "Connection failed" in str(exc_info.value)

    @patch.object(httpx.Client, "post")
    def test_call_timeout_error(self, mock_post: MagicMock) -> None:
        """Test RPC call with timeout error."""
        mock_post.side_effect = httpx.TimeoutException("Request timed out")

        with (
            NexusRPCTransport("http://localhost:2026") as transport,
            pytest.raises(TransportError) as exc_info,
        ):
            transport.call("test_method")

        assert "timeout" in str(exc_info.value).lower()

    @patch.object(httpx.Client, "post")
    def test_ping_success(self, mock_post: MagicMock) -> None:
        """Test successful ping."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc": "2.0", "id": "1", "result": {"status": "ok"}}'
        mock_post.return_value = mock_response

        with NexusRPCTransport("http://localhost:2026") as transport:
            assert transport.ping() is True

    @patch.object(httpx.Client, "post")
    def test_ping_failure(self, mock_post: MagicMock) -> None:
        """Test failed ping."""
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with NexusRPCTransport("http://localhost:2026") as transport:
            assert transport.ping() is False

    @patch.object(httpx.Client, "post")
    def test_ping_wrong_status(self, mock_post: MagicMock) -> None:
        """Test ping with wrong status in response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc": "2.0", "id": "1", "result": {"status": "error"}}'
        mock_post.return_value = mock_response

        with NexusRPCTransport("http://localhost:2026") as transport:
            assert transport.ping() is False


class TestRPCError:
    """Tests for RPCError exception."""

    def test_error_attributes(self) -> None:
        """Test RPCError attributes."""
        error = RPCError(code=-32000, message="File not found", data={"path": "/test"})
        assert error.code == -32000
        assert error.message == "File not found"
        assert error.data == {"path": "/test"}

    def test_error_string(self) -> None:
        """Test RPCError string representation."""
        error = RPCError(code=-32000, message="File not found")
        assert "-32000" in str(error)
        assert "File not found" in str(error)


class TestTransportError:
    """Tests for TransportError exception."""

    def test_error_message(self) -> None:
        """Test TransportError message."""
        error = TransportError("Connection failed")
        assert "Connection failed" in str(error)
