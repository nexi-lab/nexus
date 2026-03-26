"""RemoteStreamBackend — StreamBackend implementation that proxies to a remote node.

Installed in StreamManager._buffers at open() time when the stream's origin
address differs from self_address. All subsequent reads/writes hit this
backend directly via the fast-path dict lookup, bypassing KernelDispatch.

Same pattern as RemotePipeBackend. Key difference: reads return
(bytes, next_offset) tuples matching the StreamBackend protocol.

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


class RemoteStreamBackend:
    """StreamBackend that proxies read/write to a remote node via gRPC.

    Implements the StreamBackend protocol (core/stream.py) so StreamManager
    treats it identically to a local StreamBuffer.
    """

    __slots__ = ("_origin", "_path", "_pool", "_closed", "_timeout", "_tail")

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
        self._tail = 0

    # -- write (append) -----------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Write to remote stream via gRPC Call RPC (method=sys_write)."""
        if self._closed:
            from nexus.core.stream import StreamClosedError

            raise StreamClosedError("write to closed remote stream")
        offset = self._rpc_write(data)
        self._tail = offset + len(data)
        return offset

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async write — delegates to write_nowait (gRPC call is sync)."""
        _ = blocking
        return await asyncio.to_thread(self.write_nowait, data)

    # -- read (non-destructive, offset-based) --------------------------------

    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]:
        """Read one message at byte_offset from remote stream."""
        if self._closed:
            from nexus.core.stream import StreamClosedError

            raise StreamClosedError("read from closed remote stream")
        return self._rpc_read(byte_offset)

    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]:
        """Async read one message at byte_offset."""
        _ = blocking
        return await asyncio.to_thread(self.read_at, byte_offset)

    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]:
        """Read up to count messages. Proxies via repeated single reads."""
        items: list[bytes] = []
        offset = byte_offset
        for _ in range(count):
            try:
                data, offset = self.read_at(offset)
                items.append(data)
            except Exception:  # noqa: BLE001
                break
        return items, offset

    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
        """Async batch read."""
        _ = blocking
        return await asyncio.to_thread(self.read_batch, byte_offset, count)

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

    @property
    def tail(self) -> int:
        return self._tail

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
                msg = payload.get("message", "Remote stream write failed")
                raise NexusFileNotFoundError(self._path, msg)
            result = decode_rpc_message(response.payload)
            return int(result.get("result", 0))
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                self._path, f"Remote stream write to {self._origin} failed: {exc}"
            ) from exc

    def _rpc_read(self, byte_offset: int) -> tuple[bytes, int]:
        """Sync gRPC Call(method=sys_read) to origin node."""
        import grpc

        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        params: dict[str, Any] = {"path": self._path, "offset": byte_offset}
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
                msg = payload.get("message", "Remote stream read failed")
                raise NexusFileNotFoundError(self._path, msg)
            result = decode_rpc_message(response.payload)
            content = result.get("result", b"")
            if isinstance(content, str):
                content = base64.b64decode(content)
            next_offset = int(result.get("next_offset", byte_offset + len(content)))
            return bytes(content), next_offset
        except grpc.RpcError as exc:
            raise NexusFileNotFoundError(
                self._path, f"Remote stream read from {self._origin} failed: {exc}"
            ) from exc
