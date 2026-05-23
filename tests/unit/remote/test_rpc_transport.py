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
                "code": -32007,
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
            {"code": -32007, "message": "File not found: /missing.txt"}
        )
        transport._mock_stub.Read.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.read_file("/missing.txt")

    def test_write_file_success(self, transport) -> None:
        """write_file returns content_id/size/gen dict from WriteResponse."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.content_id = "sha256-abc"
        mock_response.size = 100
        mock_response.gen = 7
        transport._mock_stub.Write.return_value = mock_response

        result = transport.write_file("/file.txt", b"x" * 100)

        assert result == {"content_id": "sha256-abc", "size": 100, "gen": 7}
        request = transport._mock_stub.Write.call_args[0][0]
        assert request.path == "/file.txt"
        assert request.content == b"x" * 100

    def test_write_file_with_content_id(self, transport) -> None:
        """write_file passes content_id for conditional write."""
        mock_response = MagicMock()
        mock_response.is_error = False
        mock_response.content_id = "sha256-new"
        mock_response.size = 5
        mock_response.gen = 8
        transport._mock_stub.Write.return_value = mock_response

        transport.write_file("/file.txt", b"hello", content_id="sha256-old")

        request = transport._mock_stub.Write.call_args[0][0]
        assert request.content_id == "sha256-old"

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

    def test_batch_read_success(self, transport) -> None:
        """batch_read issues one BatchRead RPC and returns results in order."""
        item_a = MagicMock(is_error=False, content=b"aaa", content_id="cid-a", gen=1)
        item_b = MagicMock(is_error=False, content=b"bb", content_id="cid-b", gen=2)
        mock_response = MagicMock()
        mock_response.results = [item_a, item_b]
        transport._mock_stub.BatchRead.return_value = mock_response

        results = transport.batch_read([("/a.txt", 0, None), ("/b.txt", 4, 2)])

        assert results == [item_a, item_b]
        transport._mock_stub.BatchRead.assert_called_once()
        request = transport._mock_stub.BatchRead.call_args[0][0]
        assert request.auth_token == "test-token"
        assert len(request.items) == 2
        assert request.items[0].path == "/a.txt"
        assert request.items[0].offset == 0
        # length omitted when None — proto3 `optional` reports unset.
        assert not request.items[0].HasField("length")
        assert request.items[1].offset == 4
        assert request.items[1].length == 2

    def test_batch_read_per_item_error_in_band(self, transport) -> None:
        """batch_read surfaces per-item errors in-band — it never raises."""
        ok = MagicMock(is_error=False, content=b"x", content_id="", gen=0)
        bad = MagicMock(is_error=True, error_payload=b"{}", content=b"")
        mock_response = MagicMock()
        mock_response.results = [ok, bad]
        transport._mock_stub.BatchRead.return_value = mock_response

        results = transport.batch_read([("/ok", 0, None), ("/bad", 0, None)])

        assert results == [ok, bad]

    def test_batch_write_success(self, transport) -> None:
        """batch_write issues one BatchWrite RPC and returns success items."""
        item_a = MagicMock(is_error=False, content_id="cid-a", size=5, gen=1, version=2)
        item_b = MagicMock(is_error=False, content_id="cid-b", size=6, gen=1, version=1)
        mock_response = MagicMock()
        mock_response.results = [item_a, item_b]
        transport._mock_stub.BatchWrite.return_value = mock_response

        results = transport.batch_write([("/a.txt", b"alpha"), ("/b.txt", b"bravo!")])

        assert results == [item_a, item_b]
        transport._mock_stub.BatchWrite.assert_called_once()
        request = transport._mock_stub.BatchWrite.call_args[0][0]
        assert request.auth_token == "test-token"
        assert len(request.items) == 2
        assert request.items[0].path == "/a.txt"
        assert request.items[0].content == b"alpha"
        assert request.items[1].content == b"bravo!"

    def test_batch_write_per_item_error_raises(self, transport) -> None:
        """batch_write raises the first per-item failure (all-or-nothing)."""
        ok = MagicMock(is_error=False, content_id="c", size=1, gen=0, version=1)
        bad = MagicMock(
            is_error=True,
            error_payload=encode_rpc_message({"code": -32007, "message": "nope"}),
        )
        mock_response = MagicMock()
        mock_response.results = [ok, bad]
        transport._mock_stub.BatchWrite.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.batch_write([("/ok", b"x"), ("/bad", b"y")])

    def test_readdir_success(self, transport) -> None:
        """readdir returns the ReaddirEntry list from the response."""
        e1 = MagicMock(entry_type=1)
        e1.name = "/a"
        e2 = MagicMock(entry_type=2)
        e2.name = "/b"
        mock_response = MagicMock(is_error=False, entries=[e1, e2])
        transport._mock_stub.Readdir.return_value = mock_response

        result = transport.readdir("/")

        assert result == [e1, e2]
        request = transport._mock_stub.Readdir.call_args[0][0]
        assert request.path == "/"
        assert request.auth_token == "test-token"
        assert request.zone_id == ""

    def test_readdir_error_raises(self, transport) -> None:
        """readdir raises on is_error rather than returning an empty list."""
        mock_response = MagicMock(
            is_error=True,
            entries=[],
            error_payload=encode_rpc_message({"code": -32007, "message": "nope"}),
        )
        transport._mock_stub.Readdir.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.readdir("/")

    def test_batch_stat_returns_per_item_found_flag(self, transport) -> None:
        """batch_stat returns the BatchStatItem list in input order."""
        i1 = MagicMock(found=True, size=10)
        i1.path = "/a"
        i2 = MagicMock(found=False)
        i2.path = ""
        mock_response = MagicMock(results=[i1, i2])
        transport._mock_stub.BatchStat.return_value = mock_response

        result = transport.batch_stat(["/a", "/missing"])

        assert result == [i1, i2]
        request = transport._mock_stub.BatchStat.call_args[0][0]
        assert list(request.paths) == ["/a", "/missing"]
        assert request.auth_token == "test-token"

    def test_stat_found(self, transport) -> None:
        """stat returns the StatResponse message for an existing path."""
        mock_response = MagicMock(is_error=False, found=True, path="/x.txt", size=10)
        transport._mock_stub.Stat.return_value = mock_response

        result = transport.stat("/x.txt")

        assert result is mock_response
        request = transport._mock_stub.Stat.call_args[0][0]
        assert request.path == "/x.txt"
        assert request.auth_token == "test-token"

    def test_stat_not_found_returns_none(self, transport) -> None:
        """stat returns None when the path does not exist (found=false)."""
        mock_response = MagicMock(is_error=False, found=False)
        transport._mock_stub.Stat.return_value = mock_response

        assert transport.stat("/missing.txt") is None

    def test_stat_error_raises(self, transport) -> None:
        """stat raises on is_error rather than returning None."""
        mock_response = MagicMock(
            is_error=True,
            found=False,
            error_payload=encode_rpc_message({"code": -32007, "message": "nope"}),
        )
        transport._mock_stub.Stat.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.stat("/x.txt")

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
