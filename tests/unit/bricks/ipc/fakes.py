"""In-memory fakes for IPC brick unit testing.

These provide NexusFS-compatible interfaces for the IPC brick
without any real I/O, enabling fast, isolated unit tests.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any


class _MetadataStub:
    """Stub metadata accessor for InMemoryStorageDriver.metadata property."""

    def __init__(self, driver: "InMemoryStorageDriver") -> None:
        self._driver = driver

    def get(self, path: str) -> Any:
        """Return a mock with modified_at from any zone."""
        for (p, _z), mtime in self._driver._mtimes.items():
            if p == path:
                return type("Meta", (), {"modified_at": mtime})()
        return None


class InMemoryStorageDriver:
    """In-memory NexusFS-compatible storage driver for testing."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self._dirs: set[tuple[str, str]] = set()
        self._mtimes: dict[tuple[str, str], datetime] = {}
        self._metadata_stub = _MetadataStub(self)

    def _zone_id(self, context: Any) -> str:
        return getattr(context, "zone_id", "root") if context is not None else "root"

    @property
    def metadata(self) -> Any:
        return self._metadata_stub

    def sys_read(
        self, path: str, zone_id_compat: str | None = None, *, context: Any = None
    ) -> bytes:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {path}")
        return self._files[key]

    # Alias for backward compatibility
    read = sys_read

    def write(
        self, path: str, data: bytes, zone_id_compat: str | None = None, *, context: Any = None
    ) -> None:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        self._files[(path, zone_id)] = data
        self._mtimes[(path, zone_id)] = datetime.now(UTC)

    # Alias for backward compatibility
    sys_write = write

    def sys_readdir(
        self,
        path: str,
        zone_id_compat: str | None = None,
        *,
        recursive: bool = True,
        context: Any = None,
    ) -> list[str]:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        if (path, zone_id) not in self._dirs:
            raise FileNotFoundError(f"No such directory: {path}")
        prefix = path.rstrip("/") + "/"
        results: list[str] = []
        # Check files
        for (fpath, fzone), _ in self._files.items():
            if fzone == zone_id and fpath.startswith(prefix):
                rest = fpath[len(prefix) :]
                if not recursive:
                    if "/" not in rest:  # direct child only
                        results.append(rest)
                else:
                    results.append(rest)
        # Check subdirectories
        for dpath, dzone in self._dirs:
            if dzone == zone_id and dpath.startswith(prefix):
                rest = dpath[len(prefix) :]
                if not recursive:
                    if "/" not in rest and rest:  # direct child only
                        results.append(rest)
                else:
                    if rest:
                        results.append(rest)
        return sorted(set(results))

    # Alias for backward compatibility
    def list_dir(self, path: str, zone_id: str) -> list[str]:
        ctx = type("Ctx", (), {"zone_id": zone_id})()
        return self.sys_readdir(path, recursive=False, context=ctx)

    def sys_rename(
        self, src: str, dst: str, zone_id_compat: str | None = None, *, context: Any = None
    ) -> None:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        key = (src, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {src}")
        data = self._files.pop(key)
        self._files[(dst, zone_id)] = data
        # Rename resets mtime to now (mirrors NexusFS which sets modified_at=now
        # on rename). This is important for _recover_claimed_files: a fresh claim
        # has mtime=now so it won't be treated as a stale orphan.
        self._mtimes.pop(key, None)
        self._mtimes[(dst, zone_id)] = datetime.now(UTC)

    # Alias for backward compatibility
    def rename(self, src: str, dst: str, zone_id: str) -> None:
        ctx = type("Ctx", (), {"zone_id": zone_id})()
        self.sys_rename(src, dst, context=ctx)

    def mkdir(
        self,
        path: str,
        zone_id_compat: str | None = None,
        *,
        parents: bool = True,
        exist_ok: bool = True,
        context: Any = None,
    ) -> None:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        self._dirs.add((path, zone_id))
        # Also create all parent directories
        if parents:
            parts = path.strip("/").split("/")
            for i in range(1, len(parts)):
                parent = "/" + "/".join(parts[:i])
                self._dirs.add((parent, zone_id))

    def access(self, path: str, zone_id_compat: str | None = None, *, context: Any = None) -> bool:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        return (path, zone_id) in self._files or (path, zone_id) in self._dirs

    # Alias for backward compatibility
    exists = access

    def sys_unlink(
        self, path: str, zone_id_compat: str | None = None, *, context: Any = None
    ) -> None:
        zone_id = zone_id_compat if zone_id_compat is not None else self._zone_id(context)
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(f"No such file: {path}")
        del self._files[key]
        self._mtimes.pop(key, None)

    def file_mtime(self, path: str, zone_id: str) -> datetime | None:
        """Return the mtime of a file, or None if it doesn't exist."""
        return self._mtimes.get((path, zone_id))

    def set_mtime(self, path: str, zone_id: str, mtime: datetime) -> None:
        """Test helper: override the mtime of an existing file."""
        self._mtimes[(path, zone_id)] = mtime


# Alias for backward compatibility — tests that imported InMemoryVFS
InMemoryVFS = InMemoryStorageDriver


class InMemoryEventPublisher:
    """In-memory event publisher fake for testing."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._should_fail = should_fail

    def publish(self, channel: str, data: dict[str, Any]) -> None:
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
