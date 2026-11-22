"""Exercise the remote filesystem client's metadata receipts over a gRPC connection."""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest

from nexus.contracts.exceptions import (
    NexusError,
    RemoteConnectionError,
    RemoteMetadataVisibilityError,
)
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.vfs_client import RemoteFilesystemClient


def test_metadata_visibility_over_grpc() -> None:
    read = {
        "reason": "ReplicaBehind",
        "required_index": "18446744073709551615",
        "applied_index": "9",
    }
    committed = {"reason": "CommittedNotVisible", "applied_index": "9"}
    calls: Counter[str] = Counter()

    class Server(vfs_pb2_grpc.NexusVFSServiceServicer):
        def Stat(self, request, context):
            calls[request.path] += 1
            if request.path == "/recovered" and calls[request.path] == 2:
                return vfs_pb2.StatResponse(path=request.path, found=True, size=17)
            if request.path != "/legacy":
                context.set_trailing_metadata(
                    (("nexus-visibility-bin", json.dumps(read).encode()),)
                )
            context.abort(grpc.StatusCode.UNAVAILABLE, "read visibility deadline")

        def Mkdir(self, request, context):
            calls[request.path] += 1
            data = committed if request.path == "/commit" else {**committed, "applied_index": 9}
            return vfs_pb2.MkdirResponse(
                is_error=True,
                error_payload=json.dumps(
                    {"code": -32603, "message": "metadata step", "data": data}
                ).encode(),
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        server = grpc.server(workers)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(Server(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        transport = RPCTransport(f"127.0.0.1:{port}", timeout=5)
        try:
            client = RemoteFilesystemClient(transport)
            assert client.sys_stat("/recovered")["size"] == 17
            with pytest.raises(RemoteMetadataVisibilityError) as lag:
                client.sys_stat("/lag")
            assert lag.value.details == read
            with pytest.raises(RemoteMetadataVisibilityError) as receipt:
                client.mkdir("/commit")
            assert receipt.value.details == committed
            with pytest.raises(NexusError, match="metadata step") as malformed:
                client.mkdir("/malformed")
            assert not isinstance(malformed.value, RemoteMetadataVisibilityError)
            with pytest.raises(RemoteConnectionError):
                client.sys_stat("/legacy")
            assert calls == {
                "/recovered": 2,
                "/lag": 3,
                "/commit": 1,
                "/malformed": 1,
                "/legacy": 3,
            }
        finally:
            transport.close()
            server.stop(0).wait()
