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

    def test_setattr_known_kwargs_map_to_request_fields(self, transport) -> None:
        """setattr maps known kwargs to typed proto fields; optionals are HasField-tracked."""
        mock_response = MagicMock(is_error=False, path="/x", created=True, entry_type=0)
        transport._mock_stub.Setattr.return_value = mock_response

        result = transport.setattr(
            "/x",
            entry_type=0,
            zone_id="root",
            mime_type="text/plain",
            size=42,
            unknown_kwarg="dropped",
        )

        assert result is mock_response
        request = transport._mock_stub.Setattr.call_args[0][0]
        assert request.path == "/x"
        assert request.entry_type == 0
        assert request.zone_id == "root"
        assert request.HasField("mime_type")
        assert request.mime_type == "text/plain"
        assert request.HasField("size")
        assert request.size == 42
        # Unset optionals stay unset.
        assert not request.HasField("content_id")
        assert not request.HasField("version")

    def test_setattr_forwards_s3_backend_params(self, transport) -> None:
        """DT_MOUNT S3 params (#4262) are forwarded onto SetattrRequest.

        Bridge-2: ``_extract_rust_backend_params`` produces these so the Rust
        gRPC handler can build the backend via ``ObjectStoreProvider`` instead
        of synthetically acking. S3-compatible stores (Cloudflare R2, MinIO)
        ride the same fields via ``s3_endpoint`` + ``aws_region="auto"``.
        """
        mock_response = MagicMock(is_error=False, path="/mnt/r2", created=True, entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        transport.setattr(
            "/mnt/r2",
            entry_type=2,
            backend_name="r2",
            backend_type="s3",
            s3_bucket="nexus-test",
            s3_prefix="p/",
            aws_region="auto",
            aws_access_key="AKID",
            aws_secret_key="SECRET",
            s3_endpoint="https://acct.r2.cloudflarestorage.com",
        )

        request = transport._mock_stub.Setattr.call_args[0][0]
        assert request.backend_type == "s3"
        assert request.HasField("s3_bucket") and request.s3_bucket == "nexus-test"
        assert request.HasField("s3_prefix") and request.s3_prefix == "p/"
        assert request.HasField("aws_region") and request.aws_region == "auto"
        assert request.HasField("aws_access_key") and request.aws_access_key == "AKID"
        assert request.HasField("aws_secret_key") and request.aws_secret_key == "SECRET"
        assert request.HasField("s3_endpoint")
        assert request.s3_endpoint == "https://acct.r2.cloudflarestorage.com"
        # Unrelated backend-family params stay unset across the wire.
        assert not request.HasField("gcs_bucket")
        assert not request.HasField("server_address")

    def test_setattr_forwards_remote_backend_params(self, transport) -> None:
        """DT_MOUNT remote params forward; str PEM material is encoded to bytes."""
        mock_response = MagicMock(is_error=False, path="/zone/x", created=True, entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        transport.setattr(
            "/zone/x",
            entry_type=2,
            backend_type="remote",
            server_address="grpcs://hub:443",
            remote_auth_token="tok",
            remote_ca_pem="-----BEGIN CERT-----",  # str → encoded to bytes on the wire
            remote_timeout=12.5,
        )

        request = transport._mock_stub.Setattr.call_args[0][0]
        assert request.backend_type == "remote"
        assert request.server_address == "grpcs://hub:443"
        assert request.remote_auth_token == "tok"
        assert request.HasField("remote_ca_pem")
        assert request.remote_ca_pem == b"-----BEGIN CERT-----"
        assert request.HasField("remote_timeout") and request.remote_timeout == 12.5
        assert not request.HasField("s3_bucket")

    def test_setattr_local_backend_type_forwarded_verbatim(self, transport) -> None:
        """A local backend_type rides ``backend_type`` (the handler acks it)."""
        mock_response = MagicMock(is_error=False, path="/", created=False, entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        transport.setattr("/", entry_type=2, backend_type="cas-local")

        request = transport._mock_stub.Setattr.call_args[0][0]
        assert request.backend_type == "cas-local"
        assert not request.HasField("s3_bucket")

    def test_setattr_provider_built_uninstalled_raises(self, transport) -> None:
        """Version-skew (#4262): a provider-built DT_MOUNT acked without being
        installed (created=false, no error — e.g. an old server ignoring the
        new fields) fails closed instead of reporting phantom success."""
        from nexus.contracts.exceptions import BackendError

        mock_response = MagicMock(is_error=False, created=False, path="/r2", entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        with pytest.raises(BackendError, match="not installed"):
            transport.setattr(
                "/r2",
                entry_type=2,
                backend_type="s3",
                s3_bucket="b",
                aws_region="auto",
                aws_access_key="k",
                aws_secret_key="s",
            )

    def test_setattr_provider_built_created_ok(self, transport) -> None:
        """A provider-built DT_MOUNT with created=true is accepted (the server
        built the backend)."""
        mock_response = MagicMock(is_error=False, created=True, path="/r2", entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        result = transport.setattr(
            "/r2", entry_type=2, backend_type="s3", s3_bucket="b", aws_region="auto"
        )
        assert result is mock_response

    def test_setattr_local_created_false_not_rejected(self, transport) -> None:
        """A non-provider-built type (local/empty) with created=false is the
        normal synthetic ack — it must NOT trip the version-skew guard."""
        mock_response = MagicMock(is_error=False, created=False, path="/", entry_type=2)
        transport._mock_stub.Setattr.return_value = mock_response

        result = transport.setattr("/", entry_type=2, backend_type="cas-local")
        assert result is mock_response

    def test_setattr_error_raises(self, transport) -> None:
        """setattr raises on is_error."""
        mock_response = MagicMock(
            is_error=True,
            error_payload=encode_rpc_message({"code": -32007, "message": "nope"}),
        )
        transport._mock_stub.Setattr.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.setattr("/x", entry_type=0)

    def test_rename_success(self, transport) -> None:
        """rename returns the RenameResponse."""
        mock_response = MagicMock(is_error=False, hit=True, success=True)
        transport._mock_stub.Rename.return_value = mock_response

        result = transport.rename("/a", "/b")

        assert result is mock_response
        request = transport._mock_stub.Rename.call_args[0][0]
        assert request.path == "/a"
        assert request.new_path == "/b"
        assert request.auth_token == "test-token"

    def test_rename_error_raises(self, transport) -> None:
        """rename raises on is_error."""
        mock_response = MagicMock(
            is_error=True,
            error_payload=encode_rpc_message({"code": -32007, "message": "nope"}),
        )
        transport._mock_stub.Rename.return_value = mock_response

        with pytest.raises(NexusFileNotFoundError):
            transport.rename("/a", "/b")

    def test_copy_success(self, transport) -> None:
        """copy returns the CopyResponse."""
        mock_response = MagicMock(is_error=False, hit=True, size=42)
        mock_response.dst_path = "/dst"
        transport._mock_stub.Copy.return_value = mock_response

        result = transport.copy("/src", "/dst")

        assert result is mock_response
        request = transport._mock_stub.Copy.call_args[0][0]
        assert request.src == "/src"
        assert request.dst == "/dst"
        assert request.auth_token == "test-token"

    def test_lock_acquired(self, transport) -> None:
        """lock returns the LockResponse with acquired=true on success."""
        mock_response = MagicMock(is_error=False, acquired=True, lock_id="L1")
        transport._mock_stub.Lock.return_value = mock_response

        result = transport.lock("/p", "", 5000)

        assert result is mock_response
        request = transport._mock_stub.Lock.call_args[0][0]
        assert request.path == "/p"
        assert request.timeout_ms == 5000

    def test_lock_contention_returns_response(self, transport) -> None:
        """Lock contention (acquired=false) is in-band, not a raise."""
        mock_response = MagicMock(is_error=False, acquired=False, lock_id="")
        transport._mock_stub.Lock.return_value = mock_response

        result = transport.lock("/p", "", 5000)

        assert result.acquired is False

    def test_unlock_returns_released_flag(self, transport) -> None:
        mock_response = MagicMock(is_error=False, released=True)
        transport._mock_stub.Unlock.return_value = mock_response

        result = transport.unlock("/p", "L1")

        assert result is mock_response
        request = transport._mock_stub.Unlock.call_args[0][0]
        assert request.path == "/p"
        assert request.lock_id == "L1"

    def test_watch_matched_and_timeout(self, transport) -> None:
        """watch returns matched=true with event; timeout yields matched=false."""
        hit = MagicMock(is_error=False, matched=True, event_type="FileWrite")
        hit.path = "/x"
        miss = MagicMock(is_error=False, matched=False, event_type="")
        miss.path = ""
        transport._mock_stub.Watch.side_effect = [hit, miss]

        assert transport.watch("/x", 1000) is hit
        assert transport.watch("/x", 1000).matched is False

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

    # ── B8 — xattr ─────────────────────────────────────────────────

    def test_get_xattr_found(self, transport) -> None:
        """get_xattr returns the response with found=true + value."""
        mock_response = MagicMock(is_error=False, found=True, value="hello")
        transport._mock_stub.GetXattr.return_value = mock_response

        result = transport.get_xattr("/p", "k")

        assert result is mock_response
        request = transport._mock_stub.GetXattr.call_args[0][0]
        assert request.path == "/p"
        assert request.key == "k"
        assert request.auth_token == "test-token"

    def test_get_xattr_not_found(self, transport) -> None:
        """get_xattr with found=false is in-band, not an error."""
        mock_response = MagicMock(is_error=False, found=False, value="")
        transport._mock_stub.GetXattr.return_value = mock_response

        result = transport.get_xattr("/p", "missing")

        assert result.found is False

    def test_set_xattr_sends_request(self, transport) -> None:
        """set_xattr returns nothing; verifies path/key/value land on the request."""
        mock_response = MagicMock(is_error=False)
        transport._mock_stub.SetXattr.return_value = mock_response

        transport.set_xattr("/p", "k", "v")

        request = transport._mock_stub.SetXattr.call_args[0][0]
        assert request.path == "/p"
        assert request.key == "k"
        assert request.value == "v"

    def test_get_xattr_bulk_returns_items(self, transport) -> None:
        """get_xattr_bulk returns the per-item list in input order."""
        i1 = MagicMock(found=True, value="a")
        i1.path = "/x"
        i2 = MagicMock(found=False, value="")
        i2.path = "/y"
        mock_response = MagicMock(is_error=False, items=[i1, i2])
        transport._mock_stub.GetXattrBulk.return_value = mock_response

        result = transport.get_xattr_bulk(["/x", "/y"], "k")

        assert result == [i1, i2]
        request = transport._mock_stub.GetXattrBulk.call_args[0][0]
        assert list(request.paths) == ["/x", "/y"]
        assert request.key == "k"

    # ── B9 — IPC pipe / stream ─────────────────────────────────────

    def test_close_pipe_no_op_on_success(self, transport) -> None:
        mock_response = MagicMock(is_error=False)
        transport._mock_stub.ClosePipe.return_value = mock_response

        transport.close_pipe("/pipe")

        request = transport._mock_stub.ClosePipe.call_args[0][0]
        assert request.path == "/pipe"

    def test_has_pipe_returns_bool(self, transport) -> None:
        mock_response = MagicMock(is_error=False, present=True)
        transport._mock_stub.HasPipe.return_value = mock_response

        assert transport.has_pipe("/pipe") is True

    def test_close_all_pipes_no_op_on_success(self, transport) -> None:
        mock_response = MagicMock(is_error=False)
        transport._mock_stub.CloseAllPipes.return_value = mock_response

        transport.close_all_pipes()

        request = transport._mock_stub.CloseAllPipes.call_args[0][0]
        assert request.auth_token == "test-token"

    def test_close_stream_no_op_on_success(self, transport) -> None:
        mock_response = MagicMock(is_error=False)
        transport._mock_stub.CloseStream.return_value = mock_response

        transport.close_stream("/stream")

    def test_has_stream_returns_bool(self, transport) -> None:
        mock_response = MagicMock(is_error=False, present=False)
        transport._mock_stub.HasStream.return_value = mock_response

        assert transport.has_stream("/stream") is False

    def test_stream_write_nowait_returns_offset(self, transport) -> None:
        mock_response = MagicMock(is_error=False, offset=42)
        transport._mock_stub.StreamWriteNowait.return_value = mock_response

        assert transport.stream_write_nowait("/s", b"hi") == 42

        request = transport._mock_stub.StreamWriteNowait.call_args[0][0]
        assert request.path == "/s"
        assert request.data == b"hi"

    def test_stream_read_at_non_blocking_eof(self, transport) -> None:
        """Non-blocking read with eof=true surfaces in-band."""
        mock_response = MagicMock(is_error=False, eof=True, data=b"", next_offset=7)
        transport._mock_stub.StreamReadAt.return_value = mock_response

        result = transport.stream_read_at("/s", 7, blocking=False)

        assert result.eof is True
        request = transport._mock_stub.StreamReadAt.call_args[0][0]
        assert request.blocking is False
        assert request.offset == 7

    def test_stream_read_at_blocking_uses_timeout_for_deadline(self, transport) -> None:
        """Blocking read sizes the RPC deadline to timeout_ms + slack."""
        mock_response = MagicMock(is_error=False, eof=False, data=b"abc", next_offset=10)
        transport._mock_stub.StreamReadAt.return_value = mock_response

        transport.stream_read_at("/s", 7, blocking=True, timeout_ms=60000)

        # Should pass timeout=65.0 (60s + 5s slack) > the 30s default _timeout.
        timeout = transport._mock_stub.StreamReadAt.call_args[1]["timeout"]
        assert timeout >= 60.0

    def test_stream_collect_all_returns_bytes(self, transport) -> None:
        mock_response = MagicMock(is_error=False, data=b"everything")
        transport._mock_stub.StreamCollectAll.return_value = mock_response

        assert transport.stream_collect_all("/s") == b"everything"

    # ── B10 — Mkdir + richer Delete ────────────────────────────────

    def test_mkdir_success(self, transport) -> None:
        mock_response = MagicMock(is_error=False, hit=True)
        transport._mock_stub.Mkdir.return_value = mock_response

        result = transport.mkdir("/d", parents=True, exist_ok=True)

        assert result is mock_response
        request = transport._mock_stub.Mkdir.call_args[0][0]
        assert request.path == "/d"
        assert request.parents is True
        assert request.exist_ok is True

    def test_mkdir_error_raises(self, transport) -> None:
        mock_response = MagicMock(
            is_error=True,
            error_payload=encode_rpc_message({"code": -32004, "message": "bad path"}),
        )
        transport._mock_stub.Mkdir.return_value = mock_response

        with pytest.raises(Exception):  # noqa: B017,PT011 — code -32004 → InvalidPathError
            transport.mkdir("/bad")

    def test_delete_returns_full_response(self, transport) -> None:
        """delete() returns the richer response (entry_type / path / etc.)."""
        mock_response = MagicMock(
            is_error=False, success=True, entry_type=1, content_id="cid", size=99
        )
        mock_response.path = "/x"
        transport._mock_stub.Delete.return_value = mock_response

        result = transport.delete("/x")

        assert result is mock_response
        assert result.entry_type == 1
        assert result.size == 99
        request = transport._mock_stub.Delete.call_args[0][0]
        assert request.path == "/x"

    def test_delete_file_backcompat_bool(self, transport) -> None:
        """delete_file() still returns a bool for legacy callers."""
        mock_response = MagicMock(is_error=False, success=True)
        transport._mock_stub.Delete.return_value = mock_response

        assert transport.delete_file("/x") is True

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
