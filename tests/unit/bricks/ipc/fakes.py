"""In-memory fakes for IPC brick unit testing.

These satisfy the Protocol interfaces defined in ``nexus.bricks.ipc.protocols``
without any real I/O, enabling fast, isolated unit tests.
"""

import asyncio
from typing import Any


class InMemoryStorageDriver:
    """In-memory IPC storage driver for testing.

    Satisfies the ``VFSOperations`` protocol via structural subtyping.
    """

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self._dirs: set[tuple[str, str]] = set()

    async def sys_read(self, path: str, zone_id: str) -> bytes:
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {path}")
        return self._files[key]

    # Alias for backward compatibility
    read = sys_read

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._files[(path, zone_id)] = data

    # Alias for backward compatibility
    sys_write = write

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

    async def sys_access(self, path: str, zone_id: str) -> bool:
        return (path, zone_id) in self._files or (path, zone_id) in self._dirs

    # Alias for backward compatibility
    exists = sys_access


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


class FlakyEventSubscriber:
    """Event subscriber that fails N times before succeeding.

    Used to test reconnection logic in MessageProcessor._event_listen_loop().

    Args:
        fail_count: Number of times subscribe() will raise before succeeding.
        events: Events to yield after failures are exhausted. After yielding
            all events, the generator blocks forever (simulating a healthy
            connection waiting for more events).
    """

    def __init__(
        self,
        fail_count: int = 2,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._fail_count = fail_count
        self._call_count = 0
        self._events = events or []
        self._connected = asyncio.Event()

    async def subscribe(self, channel: str) -> Any:  # noqa: ARG002
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise ConnectionError(f"EventBus down (attempt {self._call_count})")
        self._connected.set()
        for event in self._events:
            yield event
        # Block forever after yielding events (simulates healthy connection)
        await asyncio.Event().wait()


class InMemoryWakeupNotifier:
    """In-memory wakeup notifier for testing.

    Records all notify calls and optionally raises on notify.
    """

    def __init__(self, *, should_fail: bool = False) -> None:
        self.notifications: list[str] = []
        self._should_fail = should_fail

    async def notify(self, agent_id: str) -> None:
        if self._should_fail:
            raise RuntimeError("Pipe unavailable")
        self.notifications.append(agent_id)


class InMemoryWakeupListener:
    """In-memory wakeup listener for testing.

    Wakes when ``trigger()`` is called. Supports multiple wakeups.
    Uses a queue-based approach to avoid race conditions between
    trigger() and wait_for_wakeup().
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bool] = asyncio.Queue()
        self._closed = False
        self._waiting = asyncio.Event()
        self.wait_count = 0

    def trigger(self) -> None:
        """Simulate a wakeup signal arriving."""
        self._queue.put_nowait(True)

    async def wait_for_wakeup(self) -> None:
        """Block until trigger() is called."""
        if self._closed:
            raise RuntimeError("Listener closed")
        self._waiting.set()
        await self._queue.get()
        self._waiting.clear()
        self.wait_count += 1

    def close(self) -> None:
        self._closed = True
        self._queue.put_nowait(False)  # Unblock any waiting


class InMemoryNotifyPipeFactory:
    """In-memory notify pipe factory for testing.

    Records which agents had pipes created.
    """

    def __init__(self, *, should_fail: bool = False) -> None:
        self.created: list[str] = []
        self._should_fail = should_fail

    def create_notify_pipe(self, agent_id: str) -> None:
        if self._should_fail:
            raise RuntimeError("PipeManager unavailable")
        self.created.append(agent_id)
