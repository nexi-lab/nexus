"""IPCMixin — Pipe/stream kernel primitives (DT_PIPE, DT_STREAM).

Tier 0 internal methods: _pipe_read, _pipe_write, _pipe_destroy,
_stream_read, _stream_write, _stream_destroy.

Hot path (nowait read/write) goes through Rust kernel IPC registry.
Custom backends (SHM/remote) are kept in Python fallback dicts.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.ipc_waiter import IPCWaiter
    from nexus.core.pipe_manager import PipeManager
    from nexus.core.stream_manager import StreamManager


class IPCMixin:
    """Pipe/stream IPC: _pipe_read/_write/_destroy + _stream_read/_write/_destroy."""

    # Provided by NexusFS.__init__
    _pipe_manager: PipeManager | None
    _stream_manager: StreamManager
    _ipc_waiters: dict[str, IPCWaiter]
    _custom_pipe_backends: dict[str, Any]
    _custom_stream_backends: dict[str, Any]
    _kernel: Any

    # ------------------------------------------------------------------
    # DT_PIPE kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _pipe_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_PIPE — async blocking via IPCWaiter + Rust nowait retry."""
        _waiter = self._ipc_waiters.get(path)
        if _waiter is not None:
            while True:
                _data = self._kernel.pipe_read_nowait(path)
                if _data is not None:
                    _waiter.signal_not_full()
                    if offset or count is not None:
                        _data = (
                            _data[offset : offset + count] if count is not None else _data[offset:]
                        )
                    return bytes(_data)
                await _waiter.wait_readable()

        # Custom backend fallback (SHM/remote)
        _buf = self._custom_pipe_backends.get(path)
        if _buf is not None:
            from nexus.core.pipe import PipeClosedError, PipeEmptyError

            try:
                data: bytes = _buf.read_nowait()
            except PipeEmptyError:
                data = await _buf.read(blocking=True)
            except PipeClosedError:
                raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
            if offset or count is not None:
                data = data[offset : offset + count] if count is not None else data[offset:]
            return data

        raise NexusFileNotFoundError(path, f"Pipe not found: {path}")

    def _pipe_write(self, path: str, data: bytes) -> int:
        """Write to DT_PIPE — non-blocking via Rust kernel."""
        n: int = self._kernel.pipe_write_nowait(path, data)
        _waiter = self._ipc_waiters.get(path)
        if _waiter is not None:
            _waiter.signal_not_empty()
        return n

    def _pipe_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_PIPE — close Rust buffer + clean up Python state."""
        with contextlib.suppress(Exception):
            self._kernel.destroy_pipe(path)
        _buf = self._custom_pipe_backends.pop(path, None)
        if _buf is not None:
            _buf.close()
        if self._pipe_manager is not None:
            with contextlib.suppress(Exception):
                self._pipe_manager.destroy(path)
        self._ipc_waiters.pop(path, None)
        return {}

    # ------------------------------------------------------------------
    # DT_STREAM kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _stream_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_STREAM — async blocking via IPCWaiter + Rust nowait retry."""
        _waiter = self._ipc_waiters.get(path)
        if _waiter is not None:
            while True:
                _result = self._kernel.stream_read_at(path, offset)
                if _result is not None:
                    return bytes(_result[0])
                await _waiter.wait_readable()

        # Custom backend fallback
        _buf = self._custom_stream_backends.get(path)
        if _buf is not None:
            from nexus.core.stream import StreamClosedError, StreamEmptyError

            try:
                if count is not None and count > 1:
                    items, _ = await _buf.read_batch_blocking(offset, count, blocking=True)
                    return b"".join(items)
                data: bytes
                data, _ = await _buf.read(offset, blocking=True)
                return data
            except StreamEmptyError:
                raise NexusFileNotFoundError(path, f"Stream empty at offset {offset}") from None
            except StreamClosedError:
                raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

        raise NexusFileNotFoundError(path, f"Stream not found: {path}")

    def _stream_write(self, path: str, data: bytes) -> int:
        """Write to DT_STREAM — non-blocking via Rust kernel, returns byte offset."""
        _off: int = self._kernel.stream_write_nowait(path, data)
        _waiter = self._ipc_waiters.get(path)
        if _waiter is not None:
            _waiter.signal_not_empty()
        return _off

    def _stream_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_STREAM — close Rust buffer + clean up Python state."""
        with contextlib.suppress(Exception):
            self._kernel.destroy_stream(path)
        _buf = self._custom_stream_backends.pop(path, None)
        if _buf is not None:
            _buf.close()
        with contextlib.suppress(Exception):
            self._stream_manager.destroy(path)
        self._ipc_waiters.pop(path, None)
        return {}
