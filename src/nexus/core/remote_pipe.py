"""RemotePipeBackend — PipeBackend implementation that proxies to a remote node.

Installed in PipeManager._buffers at open() time when the pipe's origin
address differs from self_address. All subsequent reads/writes hit this
backend directly via the fast-path dict lookup, bypassing KernelDispatch.

Uses RPCTransport (persistent gRPC channel with retry + auth) from the
RPCTransportPool. Same Call RPC pattern for sys_read/sys_write.

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


class RemotePipeBackend:
    """PipeBackend that proxies read/write to a remote node via gRPC.

    Implements the PipeBackend protocol (core/pipe.py) so PipeManager
    treats it identically to a local MemoryPipeBackend.
    """

    __slots__ = ("_origin", "_path", "_transport", "_closed")

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

    # -- sync nowait (data-path hot methods) --------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Write to remote pipe via gRPC Call RPC (method=sys_write)."""
        if self._closed:
            from nexus.core.pipe import PipeClosedError

            raise PipeClosedError("write to closed remote pipe")
        params: dict[str, Any] = {
            "path": self._path,
            "buf": base64.b64encode(data).decode("ascii"),
        }
        result = self._transport.call_rpc("sys_write", params)
        return int(result) if isinstance(result, int) else len(data)

    def read_nowait(self) -> bytes:
        """Read from remote pipe via gRPC Call RPC (method=sys_read)."""
        if self._closed:
            from nexus.core.pipe import PipeClosedError

            raise PipeClosedError("read from closed remote pipe")
        result = self._transport.call_rpc("sys_read", {"path": self._path})
        content = result if isinstance(result, bytes) else b""
        if isinstance(result, str):
            content = base64.b64decode(result)
        return bytes(content)

    # -- async wrappers -----------------------------------------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async write — delegates to write_nowait (gRPC call is sync)."""
        _ = blocking
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
