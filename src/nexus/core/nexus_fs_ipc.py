"""IPCMixin — Pipe/stream kernel primitives (DT_PIPE, DT_STREAM).

Tier 0 internal methods: _pipe_read, _pipe_write, _pipe_destroy,
_stream_read, _stream_write, _stream_destroy.

Delegates to kernel PipeManager / StreamManager primitives.
Only handles local IPC — remote pipes/streams are intercepted by
FederationIPCResolver in the PRE-DISPATCH phase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.pipe_manager import PipeManager
    from nexus.core.stream_manager import StreamManager


class IPCMixin:
    """Pipe/stream IPC: _pipe_read/_write/_destroy + _stream_read/_write/_destroy."""

    # Provided by NexusFS.__init__
    _pipe_manager: PipeManager | None
    _stream_manager: StreamManager

    # ------------------------------------------------------------------
    # DT_PIPE kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _pipe_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_PIPE — async blocking, waits until data is available.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            data = await self._pipe_manager.pipe_read(path, blocking=True)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
        if offset or count is not None:
            data = data[offset : offset + count] if count is not None else data[offset:]
        return data

    def _pipe_write(self, path: str, data: bytes) -> int:
        """Write to DT_PIPE — non-blocking, PipeFullError propagates.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            return self._pipe_manager.pipe_write_nowait(path, data)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None

    def _pipe_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_PIPE — close buffer + delete inode.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            self._pipe_manager.destroy(path)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        return {}

    # ------------------------------------------------------------------
    # DT_STREAM kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _stream_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_STREAM — async blocking, waits until data at offset is available.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamClosedError, StreamNotFoundError

        try:
            if count is not None and count > 1:
                items, _ = await self._stream_manager.stream_read_batch_blocking(
                    path, offset, count, blocking=True
                )
                return b"".join(items)
            data, _ = await self._stream_manager.stream_read(path, offset, blocking=True)
            return data
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        except StreamClosedError:
            raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

    def _stream_write(self, path: str, data: bytes) -> int:
        """Write to DT_STREAM — non-blocking append, returns byte offset.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamClosedError, StreamNotFoundError

        try:
            return self._stream_manager.stream_write_nowait(path, data)
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        except StreamClosedError:
            raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

    def _stream_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_STREAM — close buffer + delete inode.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamNotFoundError

        try:
            self._stream_manager.destroy(path)
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        return {}
