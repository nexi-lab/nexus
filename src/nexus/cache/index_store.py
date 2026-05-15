from __future__ import annotations

import time
from collections import OrderedDict
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
    DEFAULT_MAX_ENTRIES = 16384

    def __init__(
        self,
        now_fn: Callable[[], float] | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._now_fn = now_fn or time.monotonic
        # OrderedDict gives both TTL freshness on read and LRU eviction when
        # the bound is hit. The bound is a safety net for workloads that scan
        # many unique paths between TTL expiries; positive-path correctness
        # remains driven by TTL freshness, not eviction.
        self._entries: OrderedDict[IndexKey, _IndexEntry] = OrderedDict()
        self._max_entries = max_entries
        self._lock = RLock()

    def get(self, key: IndexKey) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._now_fn():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def put(self, key: IndexKey, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = _IndexEntry(
                value=value,
                expires_at=self._now_fn() + max(ttl_seconds, 0),
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

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
