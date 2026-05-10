from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from weakref import WeakValueDictionary


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
    def __init__(self, now_fn: Callable[[], float] | None = None) -> None:
        self._now_fn = now_fn or time.monotonic
        self._entries: dict[FileKey, _FileEntry] = {}
        self._entry_lock = RLock()
        self._locks: WeakValueDictionary[FileKey, asyncio.Lock] = WeakValueDictionary()
        self._lock_guard = RLock()

    def get_sync(self, key: FileKey, expected_fingerprint: str | None) -> bytes | None:
        with self._entry_lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= self._now_fn():
                self._entries.pop(key, None)
                return None
            if expected_fingerprint is not None:
                return entry.content if entry.fingerprint == expected_fingerprint else None
            if entry.expires_at is None:
                return None
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
        expires_at = None if ttl_seconds is None else self._now_fn() + max(ttl_seconds, 0)
        with self._entry_lock:
            self._entries[key] = _FileEntry(
                content=content,
                fingerprint=fingerprint,
                expires_at=expires_at,
            )

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
            self._entries.pop(key, None)

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
                self._entries.pop(key, None)

    async def lock(self, key: FileKey) -> asyncio.Lock:
        with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
