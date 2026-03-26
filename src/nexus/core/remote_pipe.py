"""RemotePipeBackend — PipeBackend implementation that proxies to a remote node.

Installed in PipeManager._buffers at open() time when the pipe's origin
address differs from self_address. All subsequent reads/writes hit this
backend directly via the fast-path dict lookup, bypassing KernelDispatch.

Uses a persistent gRPC channel from PeerChannelPool (one channel per peer,
HTTP/2 multiplexed). Same Call RPC pattern as FederationIPCResolver.

Issue #1576: DT_PIPE/DT_STREAM federation fast-path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.grpc.channel_pool import PeerChannelPool

logger = logging.getLogger(__name__)


class RemotePipeBackend:
    """PipeBackend that proxies read/write to a remote node via gRPC.

    Implements the PipeBackend protocol (core/pipe.py) so PipeManager
    treats it identically to a local RingBuffer.
    """

    __slots__ = ("_origin", "_path", "_pool", "_closed", "_timeout")

    def __init__(
        self,
        origin: str,
        path: str,
        channel_pool: "PeerChannelPool",
        *,
        timeout: float = 30.0,
    ) -> None:
        self._origin = origin
        self._path = path
        self._pool = channel_pool
        self._closed = False
        self._timeout = timeout

    # -- sync nowait (data-path hot methods) --------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Write to remote pipe via gRPC Call RPC (method=sys_write)."""
        if self._closed:
            from nexus.core.pipe import PipeClosedError

            raise PipeClosedError("write to closed remote pipe")
        return self._rpc_write(data)

    def read_nowait(self) -> bytes:
        """Read from remote pipe via gRPC Call RPC (method=sys_read)."""
        if self._closed:
            from nexus.core.pipe import PipeClosedError

            raise PipeClosedError("read from closed remote pipe")
        return self._rpc_read()

    # -- async wrappers -----------------------------------------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async write — delegates to write_nowait (gRPC call is sync)."""
        _ = blocking  # remote pipe always does a single RPC attempt
        return await asyncio.to_thread(self.write_nowait, data)

    async def read(self, *, blocking: bool = True) -> bytes:
        """Async read — delegates to read_nowait (gRPC call is sync)."""
        _ = blocking
        return await asyncio.to_thread(self.read_nowait)

    async def wait_writable(self) -> None:
        """Remote pipes are always writable (bounded by remote buffer)."""
        return

    async def wait_readable(self) -> None:
        """Remote pipes: immediate return (caller retries on empty)."""
        return

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Mark this backend as closed. Does NOT close the pooled channel."""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict:
        return {
            "type": "remote",
            "origin": self._origin,
            "path": self._path,
            "closed": self._closed,
        }

    # -- gRPC internals -----------------------------------------------------

    def _rpc_write(self, data: bytes) -> int:
        """Sync gRPC Call(method=sys_write) to origin node."""
        import grpc

        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        params: dict[str, Any] = {
            "path": self._path,
            "buf": base64.b64encode(data).decode("ascii"),
        }
        channel = self._pool.get(self._origin)
        try:
            from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.CallRequest(
                method="sys_write",
                payload=encode_rpc_message(params),
            )
            response = stub.Call(request, timeout=self._timeout)
            if response.is_error:
                payload = decode_rpc_message(response.payload) if response.payload else {}
                msg = payload.get("message", "Remote pipe write failed")
                raise NexusFileNotFoundError(self._path, msg)
            result = decode_rpc_message(response.payload)
            return int(result.get("result", len(data)))
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                self._path, f"Remote pipe write to {self._origin} failed: {exc}"
            ) from exc

    def _rpc_read(self) -> bytes:
        """Sync gRPC Call(method=sys_read) to origin node."""
        import grpc

        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        params: dict[str, Any] = {"path": self._path}
        channel = self._pool.get(self._origin)
        try:
            from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            request = vfs_pb2.CallRequest(
                method="sys_read",
                payload=encode_rpc_message(params),
            )
            response = stub.Call(request, timeout=self._timeout)
            if response.is_error:
                payload = decode_rpc_message(response.payload) if response.payload else {}
                msg = payload.get("message", "Remote pipe read failed")
                raise NexusFileNotFoundError(self._path, msg)
            result = decode_rpc_message(response.payload)
            content = result.get("result", b"")
            if isinstance(content, str):
                content = base64.b64decode(content)
            return bytes(content)
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                self._path, f"Remote pipe read from {self._origin} failed: {exc}"
            ) from exc
