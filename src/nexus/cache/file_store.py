from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileKey:
    backend_id: str
    scope_id: str
    path: str
    namespace: str = "raw"


@dataclass
class _FileEntry:
    content: bytes
    fingerprint: str | None
    expires_at: float | None


class MemoryFileCache:
    DEFAULT_MAX_BYTES = 512 * 1024 * 1024
    DEFAULT_MAX_LOCK_ENTRIES = 4096

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_lock_entries: int = DEFAULT_MAX_LOCK_ENTRIES,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._now_fn = now_fn or time.monotonic
        self._max_bytes = max_bytes
        self._entries: OrderedDict[FileKey, _FileEntry] = OrderedDict()
        self._total_bytes: int = 0
        self._entry_lock = RLock()
        # Lock lifecycle: bounded dict (not WeakValueDictionary) to prevent
        # singleflight bypass when GC drops a Lock between two waiters' awaits.
        self._max_lock_entries = max_lock_entries
        self._locks: OrderedDict[FileKey, _SingleflightLock] = OrderedDict()
        self._lock_guard = RLock()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def get_sync(self, key: FileKey, expected_fingerprint: str | None) -> bytes | None:
        with self._entry_lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= self._now_fn():
                self._discard_locked(key)
                return None
            if expected_fingerprint is not None:
                if entry.fingerprint != expected_fingerprint:
                    return None
                self._entries.move_to_end(key)
                return entry.content
            if entry.expires_at is None:
                return None
            self._entries.move_to_end(key)
            return entry.content

    async def get(self, key: FileKey, expected_fingerprint: str | None) -> bytes | None:
        return self.get_sync(key, expected_fingerprint)

    def put_sync(
        self,
        key: FileKey,
        content: bytes,
        fingerprint: str | None,
        ttl_seconds: int | None = None,
    ) -> None:
        size = len(content)
        if size > self._max_bytes:
            logger.warning(
                "MemoryFileCache rejecting oversize entry: key=%s size=%d max=%d",
                key,
                size,
                self._max_bytes,
            )
            # Don't leave a stale prior entry behind after a rejected replacement.
            with self._entry_lock:
                self._discard_locked(key)
            return
        expires_at = None if ttl_seconds is None else self._now_fn() + max(ttl_seconds, 0)
        with self._entry_lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._total_bytes -= len(existing.content)
            self._entries[key] = _FileEntry(
                content=content,
                fingerprint=fingerprint,
                expires_at=expires_at,
            )
            self._entries.move_to_end(key)
            self._total_bytes += size
            self._evict_until_under_cap_locked()

    async def put(
        self,
        key: FileKey,
        content: bytes,
        fingerprint: str | None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.put_sync(key, content, fingerprint, ttl_seconds)

    def invalidate_sync(self, key: FileKey) -> None:
        with self._entry_lock:
            self._discard_locked(key)

    async def invalidate(self, key: FileKey) -> None:
        self.invalidate_sync(key)

    def invalidate_path_sync(self, path: str, namespace: str | None = None) -> None:
        with self._entry_lock:
            keys = [
                key
                for key in self._entries
                if key.path == path and (namespace is None or key.namespace == namespace)
            ]
            for key in keys:
                self._discard_locked(key)

    async def lock(self, key: FileKey) -> _SingleflightLock:
        with self._lock_guard:
            lock: _SingleflightLock | None = self._locks.get(key)
            if lock is None:
                lock = _SingleflightLock()
                self._locks[key] = lock
                self._evict_unused_locks_locked()
            else:
                self._locks.move_to_end(key)
        return lock

    def _discard_locked(self, key: FileKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total_bytes -= len(entry.content)

    def _evict_until_under_cap_locked(self) -> None:
        while self._total_bytes > self._max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._total_bytes -= len(evicted.content)

    def _evict_unused_locks_locked(self) -> None:
        if len(self._locks) <= self._max_lock_entries:
            return
        target = len(self._locks) - self._max_lock_entries
        evicted = 0
        for candidate in list(self._locks):
            if evicted >= target:
                return
            # Skip in-use locks: any task that holds a reference
            # (holders > 0) or is contending on the underlying lock
            # (locked() True or waiters pending). Evicting any of those
            # would break singleflight by letting a fresh lock be created
            # for the same key while the old one still has live users.
            if self._locks[candidate]._is_in_use():
                continue
            del self._locks[candidate]
            evicted += 1


class _SingleflightLock:
    """asyncio.Lock-compatible context manager with usage tracking.

    `holders` counts callers that have received this lock from
    `MemoryFileCache.lock(key)` but have not yet exited their critical section.
    `_is_in_use()` is true while any holder exists or while the inner lock has
    waiters/is held — preventing the eviction-with-queued-waiters race that an
    `asyncio.Lock.locked()`-only check misses.
    """

    __slots__ = ("_lock", "holders")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.holders = 0

    async def __aenter__(self) -> "_SingleflightLock":
        self.holders += 1
        try:
            await self._lock.acquire()
        except BaseException:
            self.holders = max(0, self.holders - 1)
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self._lock.release()
        finally:
            self.holders = max(0, self.holders - 1)

    async def acquire(self) -> bool:
        self.holders += 1
        try:
            return await self._lock.acquire()
        except BaseException:
            self.holders = max(0, self.holders - 1)
            raise

    def release(self) -> None:
        try:
            self._lock.release()
        finally:
            self.holders = max(0, self.holders - 1)

    def locked(self) -> bool:
        return self._lock.locked()

    def _is_in_use(self) -> bool:
        if self.holders > 0 or self._lock.locked():
            return True
        waiters = getattr(self._lock, "_waiters", None)
        return bool(waiters)
