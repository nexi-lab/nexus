from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Literal


@dataclass(frozen=True)
class IndexKey:
    backend_id: str
    scope_id: str
    path: str
    kind: Literal["stat", "listing", "negative"]


@dataclass
class _IndexEntry:
    value: Any
    expires_at: float


class MemoryIndexCache:
    def __init__(self, now_fn: Callable[[], float] | None = None) -> None:
        self._now_fn = now_fn or time.monotonic
        self._entries: dict[IndexKey, _IndexEntry] = {}
        self._lock = RLock()

    def get(self, key: IndexKey) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._now_fn():
                self._entries.pop(key, None)
                return None
            return entry.value

    def put(self, key: IndexKey, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = _IndexEntry(
                value=value,
                expires_at=self._now_fn() + max(ttl_seconds, 0),
            )

    def invalidate_path(self, key: IndexKey) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def invalidate_parent_listing(self, backend_id: str, scope_id: str, path: str) -> None:
        parent = str(PurePosixPath(path).parent) or "/"
        listing_key = IndexKey(
            backend_id=backend_id,
            scope_id=scope_id,
            path=parent or "/",
            kind="listing",
        )
        self.invalidate_path(listing_key)
