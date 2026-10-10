"""Request identity and channel ownership through real TCP VFS and public MCP."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import patch

import grpc
import pytest
from fastmcp import Client
from google.protobuf.message_factory import GetMessageClass

from nexus.bricks.mcp.server import create_mcp_server
from nexus.contracts.exceptions import AuthenticationError
from nexus.core.nexus_fs import NexusFS
from nexus.grpc.vfs import vfs_pb2
from nexus.lib.request_credentials import request_api_key
from nexus.lib.rpc_codec import encode_rpc_message
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.vfs_client import RemoteFilesystemClient

pytestmark = pytest.mark.e2e


class CredentialHost:
    def __init__(self):
        self.calls = []
        self.keys = {"alice-key", "bob-key", "ambient-admin", "delegation-key", ""}
        self.barrier = threading.Barrier(2)

    def handler(self, method):
        response_type = GetMessageClass(method.output_type)

        def invoke(request, context):
            self.calls.append((method.name, request))
            if request.auth_token not in self.keys:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "fixture key revoked")
            if method.name == "Call":
                return response_type(payload=encode_rpc_message({"result": "ok"}))
            if method.name in ("Read", "Stat"):
                if request.path.startswith("/pair/"):
                    self.barrier.wait(timeout=5)
                owner = request.path.rsplit("/", 1)[-1]
                if request.auth_token != "ambient-admin" and request.auth_token != f"{owner}-key":
                    context.abort(grpc.StatusCode.PERMISSION_DENIED, "fixture owner denied")
                if method.name == "Read":
                    return response_type(content=owner.encode())
                return response_type(found=True, path=request.path, size=1234)
            if method.name == "Write":
                if request.path.startswith("/pair/"):
                    self.barrier.wait(timeout=5)
                return response_type(size=len(request.content))
            return response_type()

        return grpc.unary_unary_rpc_method_handler(
            invoke,
            request_deserializer=GetMessageClass(method.input_type).FromString,
            response_serializer=response_type.SerializeToString,
        )


class BorrowedFilesystem(RemoteFilesystemClient):
    def __init__(self, transport):
        super().__init__(transport)
        self.closed = False

    def service(self, name):
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def peer():
    host = CredentialHost()
    service = vfs_pb2.DESCRIPTOR.services_by_name["NexusVFSService"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(pool)
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    service.full_name,
                    {method.name: host.handler(method) for method in service.methods},
                )
            ]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        transport = RPCTransport(f"127.0.0.1:{port}", auth_token="ambient-admin", timeout=5)
        try:
            yield host, transport, BorrowedFilesystem(transport)
        finally:
            transport.close()
            server.stop(0).wait()


OPERATIONS = [
    ("Read", "read_file", ("/alice",)),
    ("Write", "write_file", ("/alice", b"hello")),
    ("Delete", "delete", ("/alice",)),
    ("Mkdir", "mkdir", ("/alice",)),
    ("BatchRead", "batch_read", ([("/alice", 0, None)],)),
    ("BatchWrite", "batch_write", ([("/alice", b"hello")],)),
    ("Readdir", "readdir", ("/",)),
    ("BatchStat", "batch_stat", (["/alice"],)),
    ("Stat", "stat", ("/alice",)),
    ("Setattr", "setattr", ("/alice",)),
    ("Rename", "rename", ("/alice", "/other")),
    ("Copy", "copy", ("/alice", "/other")),
    ("Lock", "lock", ("/alice",)),
    ("Unlock", "unlock", ("/alice",)),
    ("Watch", "watch", ("/alice", 0)),
    ("GetXattr", "get_xattr", ("/alice", "label")),
    ("SetXattr", "set_xattr", ("/alice", "label", "value")),
    ("GetXattrBulk", "get_xattr_bulk", (["/alice"], "label")),
    ("ClosePipe", "close_pipe", ("/alice",)),
    ("HasPipe", "has_pipe", ("/alice",)),
    ("CloseAllPipes", "close_all_pipes", ()),
    ("CloseStream", "close_stream", ("/alice",)),
    ("HasStream", "has_stream", ("/alice",)),
    ("StreamWriteNowait", "stream_write_nowait", ("/alice", b"hello")),
    ("StreamReadAt", "stream_read_at", ("/alice", 0)),
    ("StreamCollectAll", "stream_collect_all", ("/alice",)),
    ("Ping", "ping", ()),
    ("Call", "call_rpc", ("whoami",)),
]


@pytest.mark.parametrize("rpc,method,args", OPERATIONS, ids=[item[0] for item in OPERATIONS])
def test_every_vfs_rpc_uses_request_identity_and_restores_default(peer, rpc, method, args):
    host, transport, _ = peer
    scope = request_api_key.set("alice-key")
    try:
        getattr(transport, method)(*args)
        assert host.calls[-1][0] == rpc
        assert host.calls[-1][1].auth_token == "alice-key"
    finally:
        request_api_key.reset(scope)
    getattr(transport, method)(*args)
    assert host.calls[-1][1].auth_token == "ambient-admin"


@pytest.mark.parametrize("rpc,method,args", OPERATIONS, ids=[item[0] for item in OPERATIONS])
def test_empty_request_key_never_reaches_vfs_with_peer_identity(peer, rpc, method, args):
    host, transport, _ = peer
    scope = request_api_key.set("")
    try:
        with pytest.raises(AuthenticationError):
            getattr(transport, method)(*args)
        assert host.calls == []
    finally:
        request_api_key.reset(scope)


def test_credential_suite_covers_the_entire_vfs_protocol():
    methods = vfs_pb2.DESCRIPTOR.services_by_name["NexusVFSService"].methods
    assert {method.name for method in methods} == {item[0] for item in OPERATIONS}


def test_generic_override_is_scoped_and_explicit_empty_is_rejected(peer):
    host, transport, _ = peer
    transport.call_rpc("whoami", auth_token="delegation-key")
    assert host.calls[-1][1].auth_token == "delegation-key"
    scope = request_api_key.set("alice-key")
    try:
        transport.call_rpc("whoami", auth_token="delegation-key")
        assert host.calls[-1][1].auth_token == "alice-key"
    finally:
        request_api_key.reset(scope)
    before = len(host.calls)
    with pytest.raises(AuthenticationError):
        transport.call_rpc("whoami", auth_token="")
    assert len(host.calls) == before
    transport.call_rpc("whoami")
    assert host.calls[-1][1].auth_token == "ambient-admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_url", [None, "grpc://127.0.0.1:1"])
@pytest.mark.parametrize("method", ["read_file", "write_file", "file_info"])
async def test_public_mcp_borrows_one_channel_for_concurrent_identities(peer, remote_url, method):
    host, transport, filesystem = peer
    mcp = await create_mcp_server(nx=cast(NexusFS, filesystem), remote_url=remote_url)

    async def invoke(token, path):
        scope = request_api_key.set(token)
        try:
            async with Client(mcp) as client:
                params = {"path": path}
                if method == "write_file":
                    params["content"] = "hello"
                result = await client.call_tool(f"nexus_{method}", params)
                return result.content[0].text
        finally:
            request_api_key.reset(scope)

    with patch("nexus.connect", side_effect=AssertionError("unexpected connection")):
        results = await asyncio.gather(
            invoke("alice-key", "/pair/alice"), invoke("bob-key", "/pair/bob")
        )
        assert all(not result.startswith("Error:") for result in results)
        assert {(call.path, call.auth_token) for _, call in host.calls} == {
            ("/pair/alice", "alice-key"),
            ("/pair/bob", "bob-key"),
        }
        assert request_api_key.get() is None
        assert not filesystem.closed and not transport._closed
        if method == "read_file":
            assert results == ["alice", "bob"]
        elif method == "file_info":
            assert [json.loads(result)["size"] for result in results] == [1234, 1234]
            assert all(rpc == "Stat" for rpc, _ in host.calls)
        if method != "write_file":
            assert (await invoke("alice-key", "/bob")).startswith("Error:")
        host.keys.remove("alice-key")
        assert "Authentication required" in await invoke("alice-key", "/alice")
        before = len(host.calls)
        assert "Authentication required" in await invoke("", "/alice")
        assert len(host.calls) == before
        assert not filesystem.closed and not transport._closed
    assert await asyncio.to_thread(filesystem.sys_read, "/bob") == b"bob"
    assert host.calls[-1][1].auth_token == "ambient-admin"
