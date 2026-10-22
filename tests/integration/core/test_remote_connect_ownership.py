"""Remote SDK owns one real channel and starts no local kernel process."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest

import nexus
from nexus.remote.kernel_client import KernelClient
from nexus.remote.rpc_transport import RPCTransport


@pytest.fixture
def remote_peer(monkeypatch: pytest.MonkeyPatch) -> Iterator[KernelClient]:
    peer = KernelClient(ephemeral=True)
    peer.open()
    monkeypatch.setenv("NEXUS_GRPC_PORT", peer._server_address.rsplit(":", 1)[1])
    monkeypatch.setenv("NEXUS_GRPC_TLS", "false")
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def acquired_resources(remote_peer: KernelClient, monkeypatch: pytest.MonkeyPatch):
    transports = []

    def forbidden_local_kernel(*args, **kwargs):
        raise AssertionError("A remote SDK must not start another kernel")

    def track_transport(*args, **kwargs):
        transport = RPCTransport(*args, **kwargs)
        close = Mock(wraps=transport.close)
        monkeypatch.setattr(transport, "close", close)
        transports.append((transport, close))
        return transport

    monkeypatch.setattr(nexus, "_open_local_kernel", forbidden_local_kernel)
    monkeypatch.setattr("nexus.remote.rpc_transport.RPCTransport", track_transport)
    try:
        yield transports
    finally:
        for transport, _ in transports:
            if not transport._closed:
                transport.close()


def remote_config(peer: KernelClient) -> dict:
    return {"profile": "remote", "url": f"grpc://{peer._server_address}", "timeout": 3}


@pytest.mark.parametrize(
    "failure", [RuntimeError("Construction failed"), KeyboardInterrupt("Interrupted")]
)
def test_failed_remote_construction_closes_the_owned_channel(
    remote_peer: KernelClient,
    acquired_resources,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("nexus.remote.client.RemoteNexusFS", fail)
    with pytest.raises(type(failure)) as caught:
        nexus.connect(remote_config(remote_peer))
    assert caught.value is failure
    assert len(acquired_resources) == 1
    acquired_resources[0][1].assert_called_once()
    assert remote_peer._transport.health_check()


def test_remote_connect_transfers_ownership_and_can_reconnect(
    remote_peer: KernelClient, acquired_resources
):
    for attempt in range(2):
        filesystem = nexus.connect(remote_config(remote_peer))
        transport, close = acquired_resources[attempt]
        try:
            close.assert_not_called()
            assert not hasattr(filesystem, "_kernel")
            assert not hasattr(filesystem, "record_store")
            assert transport.health_check()
            filesystem.sys_write("/remote-ownership.txt", f"attempt {attempt}".encode())
            assert filesystem.sys_read("/remote-ownership.txt") == f"attempt {attempt}".encode()
        finally:
            filesystem.close()
            filesystem.close()
        close.assert_called_once()
        assert filesystem.closed
        assert remote_peer._transport.health_check()


@pytest.mark.parametrize("phase", ["vfs_stub", "search_stub", "error_handler"])
def test_transport_construction_failure_closes_its_channel(
    remote_peer: KernelClient, monkeypatch: pytest.MonkeyPatch, phase: str
):
    import grpc

    channel = grpc.insecure_channel(remote_peer._server_address)
    close = Mock(wraps=channel.close)
    monkeypatch.setattr(channel, "close", close)
    monkeypatch.setattr(grpc, "insecure_channel", lambda *args, **kwargs: channel)
    target = {
        "vfs_stub": "nexus.remote.rpc_transport.vfs_pb2_grpc.NexusVFSServiceStub",
        "search_stub": "nexus.remote.search_client.SearchClient",
        "error_handler": "nexus.remote.rpc_transport.BaseRemoteNexusFS",
    }[phase]
    failure = RuntimeError(f"Failed during {phase}")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(target, fail)
    try:
        with pytest.raises(RuntimeError) as caught:
            RPCTransport(remote_peer._server_address)
        assert caught.value is failure
        close.assert_called_once()
        assert remote_peer._transport.health_check()
    finally:
        channel.close()


def test_remote_filesystem_journey_uses_the_shared_typed_transport(remote_peer: KernelClient):
    filesystem = nexus.connect(remote_config(remote_peer))
    try:
        filesystem.mkdir("/remote-client/nested")
        result = filesystem.write("/remote-client/nested/a.txt", "héllo needle")
        content = "héllo needle".encode()
        assert result["bytes_written"] == len(content)
        assert result["content_id"]
        assert result["revision"] == f"/remote-client/nested/a.txt@{result['gen']}"
        assert filesystem.sys_read("/remote-client/nested/a.txt", offset=1, count=3) == content[1:4]
        assert filesystem.sys_read("/remote-client/nested/a.txt", offset=1) == content[1:]
        assert filesystem.sys_read("/remote-client/nested/a.txt", count=0) == b""
        metadata = filesystem.sys_stat("/remote-client/nested/a.txt")
        assert metadata["size"] == len(content)
        assert metadata["content_id"] == result["content_id"]
        assert filesystem.sys_stat("/remote-client/missing") is None
        assert filesystem.sys_readdir("/remote-client", recursive=False) == [
            "/remote-client/nested"
        ]
        page = filesystem.sys_readdir("/remote-client", limit=1, details=True)
        assert page.has_more and page.next_cursor == "/remote-client/nested"
        assert page.items[0]["is_directory"]
        last = filesystem.sys_readdir(
            "/remote-client", limit=1, cursor=page.next_cursor, details=True
        )
        assert not last.has_more and last.items[0]["content_id"] == result["content_id"]
        assert filesystem.sys_rename("/remote-client/nested/a.txt", "/remote-client/b.txt") == {}
        assert filesystem.sys_stat("/remote-client/nested/a.txt") is None
        assert filesystem.sys_read("/remote-client/b.txt") == content
        assert filesystem.sys_unlink("/remote-client/b.txt")["hit"]
        filesystem.rmdir("/remote-client", recursive=True)
        assert filesystem.sys_stat("/remote-client") is None
    finally:
        filesystem.close()
    assert remote_peer._transport.health_check()


@pytest.mark.parametrize(
    "operation", ["offset", "ttl", "force", "read_range", "write_range", "page"]
)
def test_unrepresentable_file_options_fail_before_mutation(
    remote_peer: KernelClient, operation: str
):
    filesystem = nexus.connect(remote_config(remote_peer))
    try:
        filesystem.write("/preserved.txt", b"durable original")
        filesystem.write("/destination.txt", b"durable destination")
        if operation == "offset":
            with pytest.raises(NotImplementedError, match="offset"):
                filesystem.sys_write("/preserved.txt", b"bad", offset=1)
        elif operation == "ttl":
            with pytest.raises(NotImplementedError, match="TTL"):
                filesystem.write("/preserved.txt", b"bad", ttl=60)
        elif operation == "force":
            with pytest.raises(NotImplementedError, match="force"):
                filesystem.sys_rename("/preserved.txt", "/destination.txt", force=True)
        elif operation == "read_range":
            with pytest.raises(ValueError, match="non-negative"):
                filesystem.sys_read("/preserved.txt", offset=-1)
        elif operation == "write_range":
            with pytest.raises(ValueError, match="non-negative"):
                filesystem.sys_write("/preserved.txt", b"bad", count=-1)
        else:
            with pytest.raises(ValueError, match="positive"):
                filesystem.sys_readdir("/", limit=0)
        assert filesystem.sys_read("/preserved.txt") == b"durable original"
        assert filesystem.sys_read("/destination.txt") == b"durable destination"
    finally:
        filesystem.close()


@pytest.mark.parametrize("entry_type", [3, 4])
def test_single_batch_read_preserves_ipc_cursor_semantics(
    remote_peer: KernelClient, entry_type: int
):
    transport = remote_peer._transport
    path = "/range-ipc"
    transport.setattr(path, entry_type=entry_type, capacity=4096, io_profile="memory")
    if entry_type == 4:
        transport.stream_write_nowait(path, b"first")
        offset = transport.stream_write_nowait(path, b"complete second frame")
    else:
        transport.write_file(path, b"complete second frame")
        offset = 1
    response = transport.batch_read([(path, offset, None)])[0]
    assert not response.is_error, response
    assert response.content == b"complete second frame"


@pytest.mark.parametrize("operation", ["rename", "delete", "attributes"])
def test_invalid_batch_or_attributes_preserve_files(remote_peer: KernelClient, operation: str):
    with nexus.connect(remote_config(remote_peer)) as filesystem:
        filesystem.write("/preserved.txt", b"original")
        with pytest.raises(ValueError):
            if operation == "rename":
                filesystem.rename_batch(
                    [("/preserved.txt", "/first.txt"), ("/preserved.txt", "/second.txt")]
                )
            elif operation == "delete":
                filesystem.delete_batch(["/preserved.txt", "/preserved.txt"])
            else:
                filesystem.sys_setattr("/preserved.txt", invented_attribute="bad")
        assert filesystem.read("/preserved.txt") == b"original"
        assert not filesystem.exists("/first.txt")
        assert not filesystem.exists("/second.txt")


@pytest.mark.parametrize("operation", ["write", "batch_write", "stream_append"])
def test_a_lost_reply_does_not_replay_a_completed_operation(
    remote_peer: KernelClient, operation: str
):
    from concurrent.futures import ThreadPoolExecutor

    import grpc

    from nexus.contracts.exceptions import RemoteConnectionError
    from nexus.grpc.vfs import vfs_pb2_grpc

    owner = remote_peer._transport
    path = "/reply-loss"
    if operation == "stream_append":
        owner.setattr(path, entry_type=4, capacity=4096, io_profile="memory")
    receipts = []

    def lose_reply(method, request, context):
        context.send_initial_metadata(())
        response = getattr(owner._stub, method)(request, timeout=3)
        receipts.append(response)
        context.abort(grpc.StatusCode.UNAVAILABLE, "Injected reply loss after owner completed")

    class LossyReplies(vfs_pb2_grpc.NexusVFSServiceServicer):
        def Write(self, request, context):
            return lose_reply("Write", request, context)

        def BatchWrite(self, request, context):
            return lose_reply("BatchWrite", request, context)

        def StreamWriteNowait(self, request, context):
            return lose_reply("StreamWriteNowait", request, context)

    with ThreadPoolExecutor(max_workers=1) as pool:
        proxy = grpc.server(pool)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(LossyReplies(), proxy)
        port = proxy.add_insecure_port("127.0.0.1:0")
        assert port
        proxy.start()
        try:
            transport = RPCTransport(f"127.0.0.1:{port}", timeout=3, connect_timeout=3)
            try:
                with pytest.raises(RemoteConnectionError):
                    if operation == "write":
                        transport.write_file(path, b"one committed write")
                    elif operation == "batch_write":
                        transport.batch_write([(path, b"one committed write")])
                    else:
                        transport.stream_write_nowait(path, b"one frame")
            finally:
                transport.close()
        finally:
            assert proxy.stop(grace=0).wait(timeout=3)

    assert len(receipts) == 1, "The client replayed an operation after the owner completed it"
    if operation == "stream_append":
        frame = owner.stream_read_at(path, 0)
        assert frame.data == b"one frame"
        following = owner.stream_read_at(path, frame.next_offset)
        assert following.eof and not following.data
    else:
        assert owner.read_file(path) == b"one committed write"
        receipt = receipts[0].results[0] if operation == "batch_write" else receipts[0]
        assert not receipt.is_error
        assert owner.stat(path).gen == receipt.gen
