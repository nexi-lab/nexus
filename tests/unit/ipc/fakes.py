"""In-memory fakes for IPC brick unit testing.

These satisfy the Protocol interfaces defined in ``nexus.ipc.protocols``
without any real I/O, enabling fast, isolated unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class InMemoryStorageDriver:
    """In-memory IPC storage driver for testing.

    Satisfies both the ``IPCStorageDriver`` and ``VFSOperations`` protocols
    via structural subtyping. Single implementation for all test fakes.
    """

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self._dirs: set[tuple[str, str]] = set()

    async def read(self, path: str, zone_id: str) -> bytes:
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {path}")
        return self._files[key]

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._files[(path, zone_id)] = data

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        if (path, zone_id) not in self._dirs:
            raise FileNotFoundError(f"No such directory: {path}")
        prefix = path.rstrip("/") + "/"
        results: list[str] = []
        # Check files
        for (fpath, fzone), _ in self._files.items():
            if fzone == zone_id and fpath.startswith(prefix):
                rest = fpath[len(prefix) :]
                if "/" not in rest:  # direct child only
                    results.append(rest)
        # Check subdirectories
        for dpath, dzone in self._dirs:
            if dzone == zone_id and dpath.startswith(prefix):
                rest = dpath[len(prefix) :]
                if "/" not in rest and rest:  # direct child only
                    results.append(rest)
        return sorted(set(results))

    async def count_dir(self, path: str, zone_id: str) -> int:
        if (path, zone_id) not in self._dirs:
            raise FileNotFoundError(f"No such directory: {path}")
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        key = (src, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {src}")
        data = self._files.pop(key)
        self._files[(dst, zone_id)] = data

    async def mkdir(self, path: str, zone_id: str) -> None:
        self._dirs.add((path, zone_id))
        # Also create all parent directories
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            parent = "/" + "/".join(parts[:i])
            self._dirs.add((parent, zone_id))

    async def exists(self, path: str, zone_id: str) -> bool:
        return (path, zone_id) in self._files or (path, zone_id) in self._dirs


# Alias for backward compatibility — tests that imported InMemoryVFS
InMemoryVFS = InMemoryStorageDriver


class InMemoryEventPublisher:
    """In-memory event publisher fake for testing."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._should_fail = should_fail

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        if self._should_fail:
            raise ConnectionError("EventBus unavailable")
        self.published.append((channel, data))


class InMemoryHotPathPublisher:
    """Captures published hot-path messages for assertion."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._should_fail = should_fail

    async def publish(self, subject: str, data: bytes) -> None:
        if self._should_fail:
            raise ConnectionError("NATS unavailable")
        self.published.append((subject, data))


class InMemoryHotPathSubscriber:
    """Feeds messages to hot listener for testing.

    Use ``inject()`` to push a message that will be yielded by
    ``subscribe()``.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[bytes]] = {}

    async def subscribe(self, subject: str) -> AsyncIterator[bytes]:
        q = self._queues.setdefault(subject, asyncio.Queue())
        while True:
            yield await q.get()

    async def inject(self, subject: str, data: bytes) -> None:
        q = self._queues.setdefault(subject, asyncio.Queue())
        await q.put(data)
