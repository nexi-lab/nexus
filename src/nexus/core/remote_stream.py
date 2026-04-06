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
    from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


class RemoteStreamBackend:
    """StreamBackend that proxies read/write to a remote node via gRPC.

    Implements the StreamBackend protocol (core/stream.py) so StreamManager
    treats it identically to a local MemoryStreamBackend.
    """

    __slots__ = ("_origin", "_path", "_transport", "_closed", "_tail")

    def __init__(
        self,
        origin: str,
        path: str,
        transport: "RPCTransport",
    ) -> None:
        self._origin = origin
        self._path = path
        self._transport = transport
        self._closed = False
        self._tail = 0

    # -- write (append) -----------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Write to remote stream via gRPC Call RPC (method=sys_write)."""
        if self._closed:
            from nexus.core.stream import StreamClosedError

            raise StreamClosedError("write to closed remote stream")
        params: dict[str, Any] = {
            "path": self._path,
            "buf": base64.b64encode(data).decode("ascii"),
        }
        result = self._transport.call_rpc("sys_write", params)
        offset = int(result) if isinstance(result, int) else 0
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
        result = self._transport.call_rpc("sys_read", {"path": self._path, "offset": byte_offset})
        if isinstance(result, dict):
            content = result.get("result", b"")
            if isinstance(content, str):
                content = base64.b64decode(content)
            next_offset = int(result.get("next_offset", byte_offset + len(content)))
            return bytes(content), next_offset
        content = result if isinstance(result, bytes) else b""
        if isinstance(result, str):
            content = base64.b64decode(result)
        return bytes(content), byte_offset + len(content)

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
        """Mark this backend as closed. Does NOT close the transport."""
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
