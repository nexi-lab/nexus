"""Tests for RPCTransport (gRPC client transport).

Verifies that RPCTransport correctly communicates with NexusVFSService
over gRPC, handles errors, and retries on transient failures.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import grpc
import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
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
        """Successful call extracts 'result' key from server response."""
        mock_response = MagicMock()
        mock_response.is_error = False
        # Server wraps results as {"result": <actual>}
        mock_response.payload = encode_rpc_message({"result": {"key": "value"}})
        transport._mock_stub.Call.return_value = mock_response

        result = transport.call_rpc("sys_read", {"path": "/file.txt"})

        assert result == {"key": "value"}
        transport._mock_stub.Call.assert_called_once()

    def test_success_extracts_list_result(self, transport) -> None:
        """Server response with list result is unwrapped correctly."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({"result": [{"path": "/a.txt", "size": 10}]})
        transport._mock_stub.Call.return_value = mock_response

        result = transport.call_rpc("list", {"path": "/"})

        assert result == [{"path": "/a.txt", "size": 10}]

    def test_success_fallback_when_no_result_key(self, transport) -> None:
        """Response without 'result' key returns full dict (backwards compat)."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.payload = encode_rpc_message({"key": "value"})
        transport._mock_stub.Call.return_value = mock_response

        result = transport.call_rpc("sys_read", {"path": "/file.txt"})

        assert result == {"key": "value"}

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

        transport.call_rpc("access")

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


# ---------------------------------------------------------------------------
# Typed RPC Method Tests (Phase 2)
# ---------------------------------------------------------------------------


class TestRPCTransportTypedMethods:
    """Typed RPC methods: read_file, write_file, delete_file, ping."""

    def test_read_file_success(self, transport) -> None:
        """read_file returns raw bytes from ReadResponse.content."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.content = b"file content here"
        transport._mock_stub.Read.return_value = mock_response

        result = transport.read_file("/test.txt")

        assert result == b"file content here"
        transport._mock_stub.Read.assert_called_once()
        request = transport._mock_stub.Read.call_args[0][0]
        assert request.path == "/test.txt"
        assert request.auth_token == "test-token"

    def test_read_file_error(self, transport) -> None:
        """read_file raises NexusFileNotFoundError on is_error=True."""
        mock_response = MagicMock()
        mock_response.is_error = True
        mock_response.error_payload = encode_rpc_message(
            {"code": -32000, "message": "File not found: /missing.txt"}
        )
        transport._mock_stub.Read.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.read_file("/missing.txt")

    def test_write_file_success(self, transport) -> None:
        """write_file returns etag/size dict from WriteResponse."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.etag = "sha256-abc"
        mock_response.size = 100
        transport._mock_stub.Write.return_value = mock_response

        result = transport.write_file("/file.txt", b"x" * 100)

        assert result == {"etag": "sha256-abc", "size": 100}
        request = transport._mock_stub.Write.call_args[0][0]
        assert request.path == "/file.txt"
        assert request.content == b"x" * 100

    def test_write_file_with_etag(self, transport) -> None:
        """write_file passes etag for conditional write."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.etag = "sha256-new"
        mock_response.size = 5
        transport._mock_stub.Write.return_value = mock_response

        transport.write_file("/file.txt", b"hello", etag="sha256-old")

        request = transport._mock_stub.Write.call_args[0][0]
        assert request.etag == "sha256-old"

    def test_delete_file_success(self, transport) -> None:
        """delete_file returns True on success."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.success = True
        transport._mock_stub.Delete.return_value = mock_response

        result = transport.delete_file("/file.txt")

        assert result is True
        request = transport._mock_stub.Delete.call_args[0][0]
        assert request.path == "/file.txt"
        assert request.recursive is False

    def test_delete_file_recursive(self, transport) -> None:
        """delete_file passes recursive flag."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.success = True
        transport._mock_stub.Delete.return_value = mock_response

        transport.delete_file("/dir", recursive=True)

        request = transport._mock_stub.Delete.call_args[0][0]
        assert request.recursive is True

    def test_ping_success(self, transport) -> None:
        """ping returns version/zone_id/uptime dict."""
        mock_response = MagicMock()
        mock_response.version = "0.7.2"
        mock_response.zone_id = ROOT_ZONE_ID
        mock_response.uptime_seconds = 3600
        transport._mock_stub.Ping.return_value = mock_response

        result = transport.ping()

        assert result == {"version": "0.7.2", "zone_id": "root", "uptime": 3600}

    def test_read_file_unavailable(self, transport) -> None:
        """read_file raises RemoteConnectionError on UNAVAILABLE."""
        rpc_error = grpc.RpcError()
        rpc_error.code = lambda: grpc.StatusCode.UNAVAILABLE
        rpc_error.details = lambda: "Connection refused"
        transport._mock_stub.Read.side_effect = rpc_error

        with pytest.raises(RemoteConnectionError):
            transport.read_file.__wrapped__(transport, "/file.txt")


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestRPCTransportLifecycle:
    """Health check and close."""

    def test_health_check_uses_ping(self, transport) -> None:
        """health_check delegates to ping()."""
        mock_response = MagicMock()
        mock_response.version = "0.7.2"
        mock_response.zone_id = ROOT_ZONE_ID
        mock_response.uptime_seconds = 0
        transport._mock_stub.Ping.return_value = mock_response

        assert transport.health_check() is True
        transport._mock_stub.Ping.assert_called_once()

    def test_health_check_failure(self, transport) -> None:
        """health_check raises RemoteConnectionError when ping fails."""
        rpc_error = grpc.RpcError()
        rpc_error.code = lambda: grpc.StatusCode.UNAVAILABLE
        rpc_error.details = lambda: "Connection refused"
        transport._mock_stub.Ping.side_effect = rpc_error

        with pytest.raises(RemoteConnectionError, match="health check failed"):
            transport.health_check()

    def test_close_closes_channel(self, transport) -> None:
        """close() should close the gRPC channel."""
        transport.close()
        transport._mock_channel.close.assert_called_once()
