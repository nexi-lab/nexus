"""Tests for RPCTransport (gRPC client transport).

Verifies that RPCTransport correctly communicates with NexusVFSService
over gRPC, handles errors, and retries on transient failures.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import grpc
import pytest

from nexus.contracts.exceptions import (
    NexusFileNotFoundError,
    RemoteConnectionError,
    RemoteTimeoutError,
)
from nexus.lib.rpc_codec import encode_rpc_message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    """Create an RPCTransport with mocked gRPC channel."""
    with (
        patch("nexus.remote.rpc_transport.grpc.insecure_channel") as mock_channel_fn,
        patch("nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub") as mock_stub_cls,
    ):
        mock_channel = MagicMock()
        mock_channel_fn.return_value = mock_channel

        mock_stub = MagicMock()
        mock_stub_cls.return_value = mock_stub

        from nexus.remote.rpc_transport import RPCTransport

        t = RPCTransport(
            server_address="localhost:2028",
            auth_token="test-token",
            timeout=30.0,
            connect_timeout=5.0,
        )
        # Expose mocks for test assertions
        t._mock_channel = mock_channel
        t._mock_stub = mock_stub
        yield t


# ---------------------------------------------------------------------------
# Construction Tests
# ---------------------------------------------------------------------------


class TestRPCTransportConstruction:
    """RPCTransport initialization."""

    def test_server_address_stored(self, transport) -> None:
        assert transport.server_address == "localhost:2028"

    def test_auth_token_stored(self, transport) -> None:
        assert transport._auth_token == "test-token"

    def test_default_auth_token_empty(self) -> None:
        with (
            patch("nexus.remote.rpc_transport.grpc.insecure_channel"),
            patch("nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub"),
        ):
            from nexus.remote.rpc_transport import RPCTransport

            t = RPCTransport("localhost:2028")
            assert t._auth_token == ""


# ---------------------------------------------------------------------------
# call_rpc Tests
# ---------------------------------------------------------------------------


class TestRPCTransportCallRPC:
    """call_rpc success and error paths."""

    def test_success_returns_decoded_result(self, transport) -> None:
        """Successful call returns decoded payload."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({"key": "value"})
        transport._mock_stub.Call.return_value = mock_response

        result = transport.call_rpc("sys_read", {"path": "/file.txt"})

        assert result == {"key": "value"}
        transport._mock_stub.Call.assert_called_once()

    def test_is_error_raises_nexus_error(self, transport) -> None:
        """is_error=True should raise via _handle_rpc_error."""
        mock_response = MagicMock()
        mock_response.is_error = True
        mock_response.payload = encode_rpc_message(
            {
                "code": -32000,
                "message": "File not found: /missing.txt",
            }
        )
        transport._mock_stub.Call.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.call_rpc("sys_read", {"path": "/missing.txt"})

    def test_unavailable_raises_connection_error(self, transport) -> None:
        """UNAVAILABLE gRPC status should raise RemoteConnectionError."""
        rpc_error = grpc.RpcError()
        rpc_error.code = lambda: grpc.StatusCode.UNAVAILABLE
        rpc_error.details = lambda: "Connection refused"
        transport._mock_stub.Call.side_effect = rpc_error

        with pytest.raises(RemoteConnectionError, match="gRPC server unavailable"):
            transport.call_rpc.__wrapped__(transport, "sys_read", {"path": "/"})

    def test_deadline_exceeded_raises_timeout_error(self, transport) -> None:
        """DEADLINE_EXCEEDED gRPC status should raise RemoteTimeoutError."""
        rpc_error = grpc.RpcError()
        rpc_error.code = lambda: grpc.StatusCode.DEADLINE_EXCEEDED
        rpc_error.details = lambda: "Deadline exceeded"
        transport._mock_stub.Call.side_effect = rpc_error

        with pytest.raises(RemoteTimeoutError, match="gRPC call timed out"):
            transport.call_rpc.__wrapped__(transport, "sys_read", {"path": "/"})

    def test_read_timeout_override(self, transport) -> None:
        """read_timeout should override default timeout."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({})
        transport._mock_stub.Call.return_value = mock_response

        transport.call_rpc("sys_read", {"path": "/"}, read_timeout=120.0)

        call_kwargs = transport._mock_stub.Call.call_args
        assert call_kwargs[1]["timeout"] == 120.0

    def test_none_params_sends_empty_dict(self, transport) -> None:
        """None params should encode as empty dict."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({})
        transport._mock_stub.Call.return_value = mock_response

        transport.call_rpc("sys_access")

        # Verify the Call was made (params encoded as {})
        transport._mock_stub.Call.assert_called_once()

    def test_auth_token_in_request(self, transport) -> None:
        """Auth token should be set in CallRequest."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({})
        transport._mock_stub.Call.return_value = mock_response

        transport.call_rpc("sys_stat", {"path": "/"})

        request = transport._mock_stub.Call.call_args[0][0]
        assert request.auth_token == "test-token"
        assert request.method == "sys_stat"


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestRPCTransportLifecycle:
    """Health check and close."""

    def test_health_check_success(self, transport) -> None:
        """health_check returns True when channel is ready."""
        with patch("nexus.remote.rpc_transport.grpc.channel_ready_future") as mock_future_fn:
            mock_future = MagicMock()
            mock_future.result.return_value = None
            mock_future_fn.return_value = mock_future

            assert transport.health_check() is True

    def test_health_check_timeout(self, transport) -> None:
        """health_check raises RemoteConnectionError on timeout."""
        with patch("nexus.remote.rpc_transport.grpc.channel_ready_future") as mock_future_fn:
            mock_future = MagicMock()
            mock_future.result.side_effect = grpc.FutureTimeoutError()
            mock_future_fn.return_value = mock_future

            with pytest.raises(RemoteConnectionError, match="health check timed out"):
                transport.health_check()

    def test_close_closes_channel(self, transport) -> None:
        """close() should close the gRPC channel."""
        transport.close()
        transport._mock_channel.close.assert_called_once()
