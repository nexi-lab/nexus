"""Thin async coordination for Rust IPC buffers.

Rust kernel owns the data (DashMap<String, Arc<RingBufferCore/StreamBufferCore>>).
Python only holds asyncio.Event pairs for blocking wait/retry.

Hot path (nowait): pure Rust, no Python.
Cold path (blocking): Python waits on Event, retries Rust nowait call.
"""

from __future__ import annotations

import asyncio


class IPCWaiter:
    """Async signaling for one pipe or stream buffer."""

    __slots__ = ("_not_empty", "_not_full")

    def __init__(self) -> None:
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially writable

    def signal_not_empty(self) -> None:
        """Called after a successful Rust write — wake blocked readers."""
        self._not_empty.set()

    def signal_not_full(self) -> None:
        """Called after a successful Rust read — wake blocked writers."""
        self._not_full.set()

    async def wait_readable(self) -> None:
        """Block until data may be available."""
        self._not_empty.clear()
        await self._not_empty.wait()

    async def wait_writable(self) -> None:
        """Block until space may be available."""
        self._not_full.clear()
        await self._not_full.wait()
