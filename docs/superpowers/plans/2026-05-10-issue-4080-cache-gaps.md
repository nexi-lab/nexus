# Issue #4080 — Cache Gap-Closing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five unmet acceptance items from issue #4080 — byte-size LRU, `max_drain_bytes` cap, per-zone TTL knobs, singleflight lock lifecycle fix, hit-rate bench — without disturbing the already-shipped cache split.

**Architecture:** Modify `MemoryFileCache` (Python) and `FileCache` (Rust kernel) to use byte-size LRU instead of unbounded growth. Replace `WeakValueDictionary` for asyncio locks with a bounded plain dict + LRU eviction of unused locks. Add new byte-size knobs to `FUSECacheManager` and thread them through `fuse/operations.py` and `fuse/mount.py` (which already accept a `cache_config: dict`). Add per-backend TTL overrides in `policy.py`. Add caller-side `max_drain_bytes` check inside `FUSECacheManager.cache_content`. Two new bench scripts verify acceptance criteria.

**Tech Stack:** Python 3.12 + asyncio, pytest + pytest-asyncio, Rust (kernel crate), `hashlink = "0.8"` for ordered hashmap, existing FUSE wiring.

**Spec:** `docs/superpowers/specs/2026-05-10-issue-4080-cache-gaps-design.md`

---

## File Structure

**New files:**
- `tests/unit/cache/test_policy.py` — TTL override unit tests
- `tests/unit/cache/test_file_store_byte_lru.py` — byte-size LRU unit tests (kept separate from existing `test_file_store.py` for clear scope)
- `tests/unit/cache/test_file_store_lock_lifecycle.py` — singleflight lock lifecycle tests
- `tests/unit/cache/test_cache_manager_byte_lru.py` — FUSECacheManager byte-knob tests
- `tests/integration/test_cache_parent_only_invalidation.py` — end-to-end invalidation test
- `benches/cache_hit_rate.py` — 90% hit-rate bench
- `benches/cache_fingerprint_cost.py` — fingerprint cost bench

**Modified files:**
- `src/nexus/cache/file_store.py` — byte-size LRU + lock lifecycle fix
- `src/nexus/cache/policy.py` — TTL overrides param
- `src/nexus/fuse/cache.py` — new byte-size constructor params; `max_drain_bytes` check in `cache_content`
- `src/nexus/fuse/operations.py:140-160` — read new keys from `cache_config` dict
- `src/nexus/fuse/mount.py` — docstring updates for new keys
- `rust/kernel/src/cache/file_cache.rs` — byte-size LRU mirror
- `rust/kernel/Cargo.toml` — add `hashlink = "0.8"` dep

**Untouched:**
- `src/nexus/core/config.py` (`CacheConfig` there is for a different concern: kernel path/list/kv/exists caches)
- `src/nexus/cache/settings.py` (`CacheSettings` for Dragonfly + permission caches)
- `nexus-fuse/src/cache.rs` (foyer; #4053 shipped)
- `src/nexus/cache/index_store.py` (no changes needed; TTL overrides live in `policy.py`)

---

### Task 1: Add per-backend TTL overrides to `policy.py`

**Files:**
- Modify: `src/nexus/cache/policy.py`
- Test: `tests/unit/cache/test_policy.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/cache/test_policy.py`:

```python
from nexus.cache.policy import index_ttl_for_backend, negative_ttl_for_backend


def test_index_ttl_default_when_no_override():
    assert index_ttl_for_backend("path_s3") == 600
    assert index_ttl_for_backend("unknown") == 60


def test_index_ttl_override_takes_precedence():
    overrides = {"path_s3": 30, "github_connector": 1200}
    assert index_ttl_for_backend("path_s3", overrides) == 30
    assert index_ttl_for_backend("github_connector", overrides) == 1200


def test_index_ttl_empty_override_dict_falls_through():
    assert index_ttl_for_backend("path_s3", {}) == 600


def test_index_ttl_none_override_falls_through():
    assert index_ttl_for_backend("path_s3", None) == 600


def test_negative_ttl_respects_override():
    overrides = {"path_s3": 30}
    assert negative_ttl_for_backend("path_s3", overrides) == 5


def test_negative_ttl_capped_below_positive():
    overrides = {"local": 2}
    assert negative_ttl_for_backend("local", overrides) == 2


def test_negative_ttl_unknown_backend():
    assert negative_ttl_for_backend("unknown") == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/cache/test_policy.py -v`

Expected: 5 tests FAIL with `TypeError: index_ttl_for_backend() takes 1 positional argument but 2 were given` (only 2 will pass since current API is single-arg).

- [ ] **Step 3: Update `policy.py`**

Replace entire `src/nexus/cache/policy.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

INDEX_TTL_BY_BACKEND = {
    "local": 0,
    "path_local": 0,
    "disk": 60,
    "path_s3": 600,
    "path_gcs": 600,
    "github_connector": 600,
}


def index_ttl_for_backend(
    backend_id: str,
    overrides: Mapping[str, int] | None = None,
) -> int:
    if overrides and backend_id in overrides:
        return overrides[backend_id]
    return INDEX_TTL_BY_BACKEND.get(backend_id, 60)


def negative_ttl_for_backend(
    backend_id: str,
    overrides: Mapping[str, int] | None = None,
) -> int:
    return min(5, index_ttl_for_backend(backend_id, overrides))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/cache/test_policy.py -v`

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cache/policy.py tests/unit/cache/test_policy.py
git commit -m "feat(#4080): add per-backend TTL overrides to cache policy"
```

---

### Task 2: Add byte-size LRU to `MemoryFileCache`

**Files:**
- Modify: `src/nexus/cache/file_store.py`
- Test: `tests/unit/cache/test_file_store_byte_lru.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/cache/test_file_store_byte_lru.py`:

```python
import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


def _key(path: str) -> FileKey:
    return FileKey("test_backend", "default", path, "raw")


def _put(cache: MemoryFileCache, path: str, size: int) -> None:
    cache.put_sync(_key(path), b"x" * size, fingerprint=f"fp:{path}", ttl_seconds=60)


def test_byte_total_tracks_after_put():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/b", 200)
    assert cache.total_bytes == 300


def test_byte_total_decrements_on_invalidate():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/b", 200)
    cache.invalidate_sync(_key("/a"))
    assert cache.total_bytes == 200


def test_byte_total_handles_overwrite():
    cache = MemoryFileCache(max_bytes=1024)
    _put(cache, "/a", 100)
    _put(cache, "/a", 300)
    assert cache.total_bytes == 300


def test_evicts_lru_when_over_cap():
    cache = MemoryFileCache(max_bytes=300)
    _put(cache, "/a", 100)
    _put(cache, "/b", 100)
    _put(cache, "/c", 100)
    _put(cache, "/d", 100)  # forces eviction of /a
    assert cache.get_sync(_key("/a"), "fp:/a") is None
    assert cache.get_sync(_key("/d"), "fp:/d") == b"x" * 100
    assert cache.total_bytes == 300


def test_get_touches_lru():
    cache = MemoryFileCache(max_bytes=300)
    _put(cache, "/a", 100)
    _put(cache, "/b", 100)
    _put(cache, "/c", 100)
    # Touch /a to make it MRU
    assert cache.get_sync(_key("/a"), "fp:/a") == b"x" * 100
    _put(cache, "/d", 100)  # should evict /b, not /a
    assert cache.get_sync(_key("/a"), "fp:/a") == b"x" * 100
    assert cache.get_sync(_key("/b"), "fp:/b") is None


def test_oversize_entry_rejected(caplog):
    cache = MemoryFileCache(max_bytes=100)
    cache.put_sync(_key("/big"), b"x" * 500, fingerprint="fp:/big", ttl_seconds=60)
    assert cache.get_sync(_key("/big"), "fp:/big") is None
    assert cache.total_bytes == 0


def test_entry_equal_to_cap_allowed():
    cache = MemoryFileCache(max_bytes=100)
    _put(cache, "/a", 50)
    _put(cache, "/full", 100)  # evicts /a, fits exactly
    assert cache.get_sync(_key("/full"), "fp:/full") == b"x" * 100
    assert cache.total_bytes == 100


def test_existing_singleflight_still_works():
    """Sanity: existing fingerprint/TTL behavior preserved."""
    cache = MemoryFileCache(max_bytes=1024)
    asyncio.run(_singleflight_inner(cache))


async def _singleflight_inner(cache: MemoryFileCache) -> None:
    key = _key("/single")
    lock = await cache.lock(key)
    async with lock:
        await cache.put(key, b"payload", "fp:single", 60)
    assert await cache.get(key, "fp:single") == b"payload"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cache/test_file_store_byte_lru.py -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'max_bytes'` and `AttributeError: ... total_bytes`.

- [ ] **Step 3: Rewrite `file_store.py` with byte-size LRU**

Replace `src/nexus/cache/file_store.py`:

```python
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
        self._locks: OrderedDict[FileKey, asyncio.Lock] = OrderedDict()
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
                key, size, self._max_bytes,
            )
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

    async def lock(self, key: FileKey) -> asyncio.Lock:
        with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
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
            if not self._locks[candidate].locked():
                del self._locks[candidate]
                evicted += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cache/test_file_store_byte_lru.py -v`

Expected: 8 PASS.

- [ ] **Step 5: Run existing tests to confirm no regression**

Run: `pytest tests/unit/cache/test_file_store.py -v`

Expected: All previously-passing tests still PASS. If any fail due to constructor signature change, note them — will be addressed in Task 4 (FUSECacheManager passes new kwargs).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/cache/file_store.py tests/unit/cache/test_file_store_byte_lru.py
git commit -m "feat(#4080): byte-size LRU eviction in MemoryFileCache"
```

---

### Task 3: Bounded lock-table lifecycle test

**Files:**
- Test: `tests/unit/cache/test_file_store_lock_lifecycle.py` (new)

(Implementation is already in Task 2; this task adds focused tests for the lock bound + singleflight 100-way concurrency proof.)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/cache/test_file_store_lock_lifecycle.py`:

```python
import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


def _key(path: str) -> FileKey:
    return FileKey("test_backend", "default", path, "raw")


@pytest.mark.asyncio
async def test_lock_dict_bounded_by_max_lock_entries():
    cache = MemoryFileCache(max_bytes=1024, max_lock_entries=4)
    for i in range(10):
        await cache.lock(_key(f"/k{i}"))
    # All locks are unheld → bound enforced exactly
    assert len(cache._locks) == 4


@pytest.mark.asyncio
async def test_held_lock_not_evicted():
    cache = MemoryFileCache(max_bytes=1024, max_lock_entries=2)
    held_key = _key("/held")
    held = await cache.lock(held_key)
    await held.acquire()
    try:
        for i in range(10):
            await cache.lock(_key(f"/k{i}"))
        assert held_key in cache._locks
    finally:
        held.release()


@pytest.mark.asyncio
async def test_singleflight_100_concurrent_cold_gets():
    cache = MemoryFileCache(max_bytes=10 * 1024 * 1024)
    key = _key("/hot")
    fetch_count = 0
    fetch_lock = asyncio.Lock()

    async def get_or_fill() -> bytes:
        nonlocal fetch_count
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, "fp:hot")
            if hit is not None:
                return hit
            async with fetch_lock:
                fetch_count += 1
            await asyncio.sleep(0.01)  # simulate backend latency
            await cache.put(key, b"payload" * 1000, "fp:hot", 60)
            return b"payload" * 1000

    results = await asyncio.gather(*[get_or_fill() for _ in range(100)])
    assert all(r == b"payload" * 1000 for r in results)
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_same_key_returns_same_lock_object():
    cache = MemoryFileCache(max_bytes=1024)
    key = _key("/x")
    l1 = await cache.lock(key)
    l2 = await cache.lock(key)
    assert l1 is l2
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/unit/cache/test_file_store_lock_lifecycle.py -v`

Expected: 4 PASS (the implementation already exists from Task 2).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/cache/test_file_store_lock_lifecycle.py
git commit -m "test(#4080): singleflight 100-way + bounded lock-table tests"
```

---

### Task 4: Add byte-size knobs + `max_drain_bytes` to `FUSECacheManager`

**Files:**
- Modify: `src/nexus/fuse/cache.py`
- Test: `tests/unit/cache/test_cache_manager_byte_lru.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/cache/test_cache_manager_byte_lru.py`:

```python
import logging

import pytest

from nexus.fuse.cache import FUSECacheManager


def test_cache_manager_accepts_byte_knobs():
    mgr = FUSECacheManager(
        content_cache_bytes=64 * 1024 * 1024,
        parsed_cache_bytes=8 * 1024 * 1024,
        max_drain_bytes=1024 * 1024,
    )
    assert mgr._file_cache.max_bytes == 72 * 1024 * 1024
    assert mgr.max_drain_bytes == 1024 * 1024


def test_max_drain_bytes_default_safe():
    mgr = FUSECacheManager()
    # Defaults: 512MB content + 64MB parsed = 576MB total. drain default 16MB.
    assert mgr.max_drain_bytes == 16 * 1024 * 1024


def test_cache_content_skips_oversize(caplog):
    mgr = FUSECacheManager(
        content_cache_bytes=4 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=1024,
    )
    big = b"x" * 4096
    with caplog.at_level(logging.WARNING):
        mgr.cache_content("/big.bin", big, fingerprint="fp", ttl_seconds=60)
    assert mgr.get_content("/big.bin", expected_fingerprint="fp") is None
    assert mgr._metrics["content_skipped_oversize"] == 1


def test_cache_content_accepts_under_cap():
    mgr = FUSECacheManager(
        content_cache_bytes=4 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=2048,
    )
    mgr.cache_content("/small.bin", b"x" * 1000, fingerprint="fp", ttl_seconds=60)
    assert mgr.get_content("/small.bin", expected_fingerprint="fp") == b"x" * 1000


def test_max_drain_bytes_exceeds_total_raises():
    with pytest.raises(ValueError, match="max_drain_bytes"):
        FUSECacheManager(
            content_cache_bytes=1024,
            parsed_cache_bytes=0,
            max_drain_bytes=2048,
        )


def test_ttl_overrides_threaded_through():
    mgr = FUSECacheManager(
        index_ttl_overrides={"path_s3": 30},
    )
    assert mgr.index_ttl_for_backend("path_s3") == 30
    assert mgr.index_ttl_for_backend("path_gcs") == 600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cache/test_cache_manager_byte_lru.py -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'content_cache_bytes'` and missing methods.

- [ ] **Step 3: Modify `FUSECacheManager`**

Open `src/nexus/fuse/cache.py`. Replace the `__init__` (lines 64–110) and add `max_drain_bytes` check to `cache_content` (around line 268). Also drop `attr_cache_size`, `listing_cache_size`, `content_cache_size`, `parsed_cache_size` legacy entry-count knobs.

Replace `__init__`:

```python
def __init__(
    self,
    *,
    content_cache_bytes: int = 512 * 1024 * 1024,
    parsed_cache_bytes: int = 64 * 1024 * 1024,
    max_drain_bytes: int = 16 * 1024 * 1024,
    attr_cache_ttl: int = 60,
    listing_cache_ttl: int | None = None,
    index_ttl_overrides: Mapping[str, int] | None = None,
    enable_metrics: bool = False,
) -> None:
    total_file_bytes = content_cache_bytes + parsed_cache_bytes
    if max_drain_bytes > total_file_bytes:
        raise ValueError(
            f"max_drain_bytes ({max_drain_bytes}) must not exceed "
            f"content_cache_bytes + parsed_cache_bytes ({total_file_bytes})"
        )
    self._attr_ttl = attr_cache_ttl
    self._listing_ttl = attr_cache_ttl if listing_cache_ttl is None else listing_cache_ttl
    self._ttl_overrides: dict[str, int] = dict(index_ttl_overrides or {})

    self._index_cache = MemoryIndexCache()
    self._file_cache = MemoryFileCache(max_bytes=total_file_bytes)
    self._max_drain_bytes = max_drain_bytes

    self._index_lock = threading.RLock()
    self._file_lock = threading.RLock()

    self._enable_metrics = enable_metrics
    self._metrics = {
        "attr_hits": 0,
        "attr_misses": 0,
        "content_hits": 0,
        "content_misses": 0,
        "parsed_hits": 0,
        "parsed_misses": 0,
        "invalidations": 0,
        "content_skipped_oversize": 0,
    }
    self._metrics_lock = threading.Lock()
```

Add at the top of `src/nexus/fuse/cache.py` after existing imports:

```python
from collections.abc import Mapping

from nexus.cache.policy import index_ttl_for_backend
```

Add `max_drain_bytes` property and `index_ttl_for_backend` method on the class (place near the constructor):

```python
    @property
    def max_drain_bytes(self) -> int:
        return self._max_drain_bytes

    def index_ttl_for_backend(self, backend_id: str) -> int:
        return index_ttl_for_backend(backend_id, self._ttl_overrides)
```

Modify `cache_content` (lines 268–287) to check size:

```python
    def cache_content(
        self,
        path: str,
        content: bytes,
        *,
        fingerprint: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        if len(content) > self._max_drain_bytes:
            logger.warning(
                "FUSECacheManager skipping oversize content: path=%s size=%d max_drain=%d",
                path, len(content), self._max_drain_bytes,
            )
            if self._enable_metrics:
                with self._metrics_lock:
                    self._metrics["content_skipped_oversize"] += 1
            return
        key = _file_key(path)
        if fingerprint is None and ttl_seconds is None:
            ttl_seconds = self._attr_ttl
        with self._file_lock:
            self._file_cache.put_sync(key, content, fingerprint, ttl_seconds)
```

(Note the dropped `_remember_file_key(key)` call — entry-count LRU is removed.)

Remove the corresponding `_remember_file_key`, `_forget_file_key`, `_remember_index_key`, `_forget_index_key`, and `_index_order`/`_file_order` private state (lines ~116–130, ~226–240, ~88–93). Remove the `_max_index_entries` and `_max_file_entries` attributes. Remove all calls to these methods in `get_attr`, `get_listing`, `cache_listing`, `cache_attr`, `get_content`, `get_parsed`, `cache_parsed`, `invalidate_path`. Each removal is the deletion of a single line of the form `self._remember_*_key(key)` or `self._forget_*_key(key)`.

Also count `content_skipped_oversize` only if `enable_metrics` — guard above does this. Initialize the metrics key in the dict (already added above).

Also make sure `import logging` and `logger = logging.getLogger(__name__)` are present near the top of the file. Check by reading lines 1–10; if missing, add them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cache/test_cache_manager_byte_lru.py -v`

Expected: 6 PASS.

- [ ] **Step 5: Run existing fuse/cache tests to catch regressions**

Run: `pytest tests/unit/fuse/ tests/unit/integration/test_lease_aware_cache.py -v 2>&1 | tail -50`

Expected: Failures in tests passing `attr_cache_size=`, `content_cache_size=`, etc. These call sites will be fixed in Task 5.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/fuse/cache.py tests/unit/cache/test_cache_manager_byte_lru.py
git commit -m "feat(#4080): byte-size knobs + max_drain_bytes in FUSECacheManager"
```

---

### Task 5: Migrate call sites + tests to new knobs

**Files:**
- Modify: `src/nexus/fuse/operations.py:140-160`
- Modify: `src/nexus/fuse/mount.py` (docstrings only)
- Modify: `tests/unit/integration/test_lease_aware_cache.py`
- Modify: `tests/unit/fuse/test_fuse_lease_coherence.py`
- Modify: `tests/unit/fuse/test_metadata_handler.py`

- [ ] **Step 1: Update `fuse/operations.py`**

In `src/nexus/fuse/operations.py:140-160`, replace:

```python
        dir_cache_ttl = cache_config.get("dir_cache_ttl", 5)
        bare_cache = FUSECacheManager(
            attr_cache_size=cache_config.get("attr_cache_size", 1024),
            attr_cache_ttl=cache_config.get("attr_cache_ttl", 60),
            listing_cache_size=cache_config.get("dir_cache_size", 1024),
            listing_cache_ttl=dir_cache_ttl,
            content_cache_size=cache_config.get("content_cache_size", 10000),
            parsed_cache_size=cache_config.get("parsed_cache_size", 50),
            enable_metrics=cache_config.get("enable_metrics", False),
        )
```

With:

```python
        dir_cache_ttl = cache_config.get("dir_cache_ttl", 5)
        bare_cache = FUSECacheManager(
            content_cache_bytes=cache_config.get(
                "content_cache_bytes", 512 * 1024 * 1024
            ),
            parsed_cache_bytes=cache_config.get(
                "parsed_cache_bytes", 64 * 1024 * 1024
            ),
            max_drain_bytes=cache_config.get(
                "max_drain_bytes", 16 * 1024 * 1024
            ),
            attr_cache_ttl=cache_config.get("attr_cache_ttl", 60),
            listing_cache_ttl=dir_cache_ttl,
            index_ttl_overrides=cache_config.get("index_ttl_overrides"),
            enable_metrics=cache_config.get("enable_metrics", False),
        )
```

- [ ] **Step 2: Update `fuse/mount.py` docstring**

In `src/nexus/fuse/mount.py:79-83` (and the second copy near line 464), replace the `cache_config` keys list. Find the existing block:

```python
                         - attr_cache_size: int (default: 1024)
                         - attr_cache_ttl: int (default: 60)
                         - content_cache_size: int (default: 10000)
                         - parsed_cache_size: int (default: 50)
                         - dir_cache_size: int (default: 1024)
                         - dir_cache_ttl: int (default: 5)
```

Replace with:

```python
                         - content_cache_bytes: int (default: 512*1024*1024)
                         - parsed_cache_bytes: int (default: 64*1024*1024)
                         - max_drain_bytes: int (default: 16*1024*1024)
                         - attr_cache_ttl: int (default: 60)
                         - dir_cache_ttl: int (default: 5)
                         - index_ttl_overrides: dict[str, int] (default: {})
                         - enable_metrics: bool (default: False)
```

Apply same replacement to the second copy near line 464.

- [ ] **Step 3: Update test call sites**

In `tests/unit/integration/test_lease_aware_cache.py:52`, replace:

```python
    return FUSECacheManager(attr_cache_size=128, attr_cache_ttl=60, content_cache_size=128)
```

with:

```python
    return FUSECacheManager(
        content_cache_bytes=128 * 1024,
        parsed_cache_bytes=0,
        max_drain_bytes=64 * 1024,
        attr_cache_ttl=60,
    )
```

Apply equivalent rewrites at:
- `tests/unit/integration/test_lease_aware_cache.py:138`
- `tests/unit/fuse/test_fuse_lease_coherence.py:31`
- `tests/unit/fuse/test_metadata_handler.py:109` (and 126, 162, 180)

For each: replace `attr_cache_size=N` with the byte-size equivalent; remove `content_cache_size`/`parsed_cache_size` entirely or substitute `content_cache_bytes=<small>`. The `attr_cache_ttl` and `enable_metrics` kwargs are unchanged. Default `parsed_cache_bytes=0` and `max_drain_bytes=64*1024` for tests is fine.

For test_metadata_handler.py:126 which is the bare `FUSECacheManager()` call — that already works with new defaults.

- [ ] **Step 4: Run all touched test files**

Run:

```bash
pytest tests/unit/fuse/test_metadata_handler.py tests/unit/fuse/test_fuse_lease_coherence.py tests/unit/integration/test_lease_aware_cache.py -v 2>&1 | tail -30
```

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fuse/operations.py src/nexus/fuse/mount.py tests/unit/fuse tests/unit/integration/test_lease_aware_cache.py
git commit -m "refactor(#4080): migrate FUSECacheManager call sites to byte-size knobs"
```

---

### Task 6: Parent-only invalidation integration test

**Files:**
- Test: `tests/integration/test_cache_parent_only_invalidation.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/integration/test_cache_parent_only_invalidation.py`:

```python
"""Integration test: writing /a/b/new.txt invalidates /a/b/'s listing only."""

from __future__ import annotations

import pytest

from nexus.cache.index_store import IndexKey, MemoryIndexCache


def test_parent_only_invalidation_does_not_touch_grandparent():
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    a_b_listing = IndexKey("path_local", "default", "/a/b", "listing")

    cache.put(a_listing, ["b"], ttl_seconds=600)
    cache.put(a_b_listing, ["existing.txt"], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/a/b/new.txt")

    assert cache.get(a_b_listing) is None
    assert cache.get(a_listing) == ["b"]


def test_parent_only_invalidation_root_file():
    cache = MemoryIndexCache()
    root_listing = IndexKey("path_local", "default", "/", "listing")
    cache.put(root_listing, ["foo.txt"], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/new.txt")
    assert cache.get(root_listing) is None


def test_parent_only_invalidation_trailing_slash_dir():
    """rmdir of /a/b/ should invalidate listing of /a, not of /a/b."""
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    a_b_listing = IndexKey("path_local", "default", "/a/b", "listing")
    cache.put(a_listing, ["b"], ttl_seconds=600)
    cache.put(a_b_listing, [], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/a/b/")

    # /a/b/ → parent is /a — that's what gets cleared
    assert cache.get(a_listing) is None
    assert cache.get(a_b_listing) == []


def test_rename_documents_caller_contract():
    """Cross-dir rename must be issued as two invalidations by the caller."""
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    b_listing = IndexKey("path_local", "default", "/b", "listing")
    cache.put(a_listing, ["x.txt"], ttl_seconds=600)
    cache.put(b_listing, [], ttl_seconds=600)

    # Single invalidation only clears one — by design
    cache.invalidate_parent_listing("path_local", "default", "/a/x.txt")
    assert cache.get(a_listing) is None
    assert cache.get(b_listing) == []

    # Caller must issue second invalidation for the new parent
    cache.invalidate_parent_listing("path_local", "default", "/b/x.txt")
    assert cache.get(b_listing) is None
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/integration/test_cache_parent_only_invalidation.py -v`

Expected: 4 PASS (implementation already exists from prior PRs).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_cache_parent_only_invalidation.py
git commit -m "test(#4080): parent-only invalidation integration tests"
```

---

### Task 7: Mirror byte-size LRU in Rust kernel `FileCache`

**Files:**
- Modify: `rust/kernel/Cargo.toml`
- Modify: `rust/kernel/src/cache/file_cache.rs`

- [ ] **Step 1: Add `hashlink` dependency**

In `rust/kernel/Cargo.toml`, add to `[dependencies]`:

```toml
hashlink = "0.9"
```

(Hashlink 0.9 supports Rust 1.66+ which the workspace already uses.)

Run to verify it resolves:

```bash
cargo check -p kernel 2>&1 | tail -5
```

- [ ] **Step 2: Write the failing tests**

Append to the `mod tests` block at the end of `rust/kernel/src/cache/file_cache.rs`:

```rust
    #[test]
    fn evicts_lru_when_over_byte_cap() {
        let cache = FileCache::with_max_bytes(300);
        for path in ["/a", "/b", "/c"] {
            cache.put(
                FileCacheKey::new("root", path, "raw"),
                vec![0u8; 100],
                Some(format!("fp:{path}")),
                None,
            );
        }
        cache.put(
            FileCacheKey::new("root", "/d", "raw"),
            vec![0u8; 100],
            Some("fp:/d".into()),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a")),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/d", "raw"), Some("fp:/d")),
            Some(vec![0u8; 100]),
        );
        assert_eq!(cache.total_bytes(), 300);
    }

    #[test]
    fn get_touches_lru() {
        let cache = FileCache::with_max_bytes(300);
        for path in ["/a", "/b", "/c"] {
            cache.put(
                FileCacheKey::new("root", path, "raw"),
                vec![0u8; 100],
                Some(format!("fp:{path}")),
                None,
            );
        }
        // Touch /a → make it MRU
        let _ = cache.get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a"));
        cache.put(
            FileCacheKey::new("root", "/d", "raw"),
            vec![0u8; 100],
            Some("fp:/d".into()),
            None,
        );
        // /b should be evicted, /a survives
        assert!(cache
            .get(&FileCacheKey::new("root", "/a", "raw"), Some("fp:/a"))
            .is_some());
        assert!(cache
            .get(&FileCacheKey::new("root", "/b", "raw"), Some("fp:/b"))
            .is_none());
    }

    #[test]
    fn oversize_entry_rejected() {
        let cache = FileCache::with_max_bytes(100);
        cache.put(
            FileCacheKey::new("root", "/big", "raw"),
            vec![0u8; 500],
            Some("fp:big".into()),
            None,
        );
        assert_eq!(
            cache.get(&FileCacheKey::new("root", "/big", "raw"), Some("fp:big")),
            None,
        );
        assert_eq!(cache.total_bytes(), 0);
    }

    #[test]
    fn total_bytes_decrements_on_invalidate() {
        let cache = FileCache::with_max_bytes(1024);
        cache.put(
            FileCacheKey::new("root", "/a", "raw"),
            vec![0u8; 100],
            Some("fp:a".into()),
            None,
        );
        cache.invalidate_path("root", "/a", "raw");
        assert_eq!(cache.total_bytes(), 0);
    }
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p kernel cache::file_cache::tests -- --nocapture 2>&1 | tail -20`

Expected: FAIL with `method 'with_max_bytes' not found` and `method 'total_bytes' not found`.

- [ ] **Step 4: Rewrite `rust/kernel/src/cache/file_cache.rs`**

Replace the file contents:

```rust
use hashlink::LinkedHashMap;
use parking_lot::{Mutex, MutexGuard, RwLock};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::time::{Duration, Instant};

const FILL_LOCK_STRIPES: usize = 64;
const DEFAULT_MAX_BYTES: usize = 512 * 1024 * 1024;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct FileCacheKey {
    pub scope_id: String,
    pub path: String,
    pub namespace: String,
}

impl FileCacheKey {
    pub fn new(
        scope_id: impl Into<String>,
        path: impl Into<String>,
        namespace: impl Into<String>,
    ) -> Self {
        Self {
            scope_id: scope_id.into(),
            path: path.into(),
            namespace: namespace.into(),
        }
    }
}

#[derive(Clone)]
struct FileEntry {
    content: Vec<u8>,
    fingerprint: Option<String>,
    expires_at: Option<Instant>,
}

struct CacheInner {
    entries: LinkedHashMap<FileCacheKey, FileEntry>,
    total_bytes: usize,
}

pub struct FileCache {
    inner: RwLock<CacheInner>,
    max_bytes: usize,
    fill_locks: Vec<Mutex<()>>,
}

impl Default for FileCache {
    fn default() -> Self {
        Self::with_max_bytes(DEFAULT_MAX_BYTES)
    }
}

impl FileCache {
    pub fn with_max_bytes(max_bytes: usize) -> Self {
        Self {
            inner: RwLock::new(CacheInner {
                entries: LinkedHashMap::new(),
                total_bytes: 0,
            }),
            max_bytes,
            fill_locks: (0..FILL_LOCK_STRIPES).map(|_| Mutex::new(())).collect(),
        }
    }

    pub fn total_bytes(&self) -> usize {
        self.inner.read().total_bytes
    }

    pub fn get(&self, key: &FileCacheKey, expected_fingerprint: Option<&str>) -> Option<Vec<u8>> {
        let now = Instant::now();
        let mut inner = self.inner.write();
        let entry = inner.entries.get(key)?;
        if let Some(expires_at) = entry.expires_at {
            if expires_at <= now {
                let removed = inner.entries.remove(key).expect("entry just observed");
                inner.total_bytes -= removed.content.len();
                return None;
            }
        }
        let fp_match = match expected_fingerprint {
            Some(expected) => entry.fingerprint.as_deref() == Some(expected),
            None => entry.expires_at.is_some(),
        };
        if !fp_match {
            return None;
        }
        let content = entry.content.clone();
        inner.entries.to_back(key);
        Some(content)
    }

    pub fn put(
        &self,
        key: FileCacheKey,
        content: Vec<u8>,
        fingerprint: Option<String>,
        ttl: Option<Duration>,
    ) {
        let size = content.len();
        if size > self.max_bytes {
            tracing::warn!(
                target: "kernel::cache::file_cache",
                key = ?key,
                size,
                max = self.max_bytes,
                "rejecting oversize entry",
            );
            return;
        }
        let expires_at = ttl.map(|ttl| Instant::now() + ttl);
        let mut inner = self.inner.write();
        if let Some(existing) = inner.entries.remove(&key) {
            inner.total_bytes -= existing.content.len();
        }
        inner.entries.insert(
            key,
            FileEntry {
                content,
                fingerprint,
                expires_at,
            },
        );
        inner.total_bytes += size;
        while inner.total_bytes > self.max_bytes {
            let (_, removed) = inner
                .entries
                .pop_front()
                .expect("non-empty cache with over-cap bytes");
            inner.total_bytes -= removed.content.len();
        }
    }

    pub fn lock(&self, key: &FileCacheKey) -> FileCacheFillGuard<'_> {
        let stripe = fill_lock_stripe(key);
        FileCacheFillGuard {
            _guard: self.fill_locks[stripe].lock(),
        }
    }

    pub fn invalidate_path(&self, scope_id: &str, path: &str, namespace: &str) {
        let mut inner = self.inner.write();
        let to_remove: Vec<FileCacheKey> = inner
            .entries
            .iter()
            .filter(|(k, _)| {
                k.scope_id == scope_id && k.path == path && k.namespace == namespace
            })
            .map(|(k, _)| k.clone())
            .collect();
        for key in to_remove {
            if let Some(removed) = inner.entries.remove(&key) {
                inner.total_bytes -= removed.content.len();
            }
        }
    }
}

fn fill_lock_stripe(key: &FileCacheKey) -> usize {
    let mut hasher = DefaultHasher::new();
    key.hash(&mut hasher);
    hasher.finish() as usize % FILL_LOCK_STRIPES
}

pub struct FileCacheFillGuard<'a> {
    _guard: MutexGuard<'a, ()>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::thread;

    #[test]
    fn rejects_mismatched_fingerprint() {
        let cache = FileCache::default();
        let key = FileCacheKey::new("root", "/mnt/foo.txt", "raw");
        cache.put(key.clone(), b"old".to_vec(), Some("etag:old".into()), None);
        assert_eq!(cache.get(&key, Some("etag:new")), None);
    }

    #[test]
    fn singleflight_allows_one_fill() {
        let cache = Arc::new(FileCache::default());
        let key = FileCacheKey::new("root", "/mnt/foo.txt", "raw");
        let fills = Arc::new(AtomicUsize::new(0));
        thread::scope(|scope| {
            for _ in 0..100 {
                let cache = Arc::clone(&cache);
                let key = key.clone();
                let fills = Arc::clone(&fills);
                scope.spawn(move || {
                    let _guard = cache.lock(&key);
                    if cache.get(&key, Some("etag:1")).is_none() {
                        fills.fetch_add(1, Ordering::SeqCst);
                        cache.put(
                            key.clone(),
                            b"payload".to_vec(),
                            Some("etag:1".into()),
                            None,
                        );
                    }
                    assert_eq!(cache.get(&key, Some("etag:1")), Some(b"payload".to_vec()));
                });
            }
        });
        assert_eq!(fills.load(Ordering::SeqCst), 1);
    }

    // (NEW: byte-size LRU tests appended above)
}
```

Then re-append the 4 new tests from Step 2 immediately above the closing `}` of `mod tests`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p kernel cache::file_cache::tests 2>&1 | tail -15`

Expected: 6 PASS (2 existing + 4 new).

- [ ] **Step 6: Verify kernel/io.rs and kernel/mod.rs callers still compile**

Run: `cargo build -p kernel 2>&1 | tail -10`

Expected: no errors. The public API changes: `FileCache::default()` still works; `FileCache::with_max_bytes(n)` is new; no removed methods.

- [ ] **Step 7: Commit**

```bash
git add rust/kernel/Cargo.toml rust/kernel/src/cache/file_cache.rs
git commit -m "feat(#4080): byte-size LRU on Rust kernel FileCache"
```

---

### Task 8: Hit-rate benchmark

**Files:**
- New: `benches/cache_hit_rate.py`

- [ ] **Step 1: Write the bench**

Create `benches/cache_hit_rate.py`:

```python
"""Issue #4080 acceptance bench: 90%+ hit rate on Zipf re-read workload.

Run: pytest benches/cache_hit_rate.py -v -s
"""

from __future__ import annotations

import asyncio
import random

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


NUM_FILES = 1000
TOTAL_OPS = 10_000
WARMUP_OPS = 500
FILE_SIZES = [1024, 64 * 1024, 256 * 1024, 1024 * 1024]  # 1KB..1MB
CACHE_MAX_BYTES = 128 * 1024 * 1024  # 128 MB


class _Backend:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.calls = 0

    async def fetch(self, path: str) -> bytes:
        self.calls += 1
        await asyncio.sleep(0.001)  # simulated latency
        return self._files[path]


def _make_files(rng: random.Random) -> dict[str, bytes]:
    return {
        f"/f/{i}": b"x" * rng.choice(FILE_SIZES)
        for i in range(NUM_FILES)
    }


def _zipf_index(rng: random.Random, n: int, alpha: float = 1.0) -> int:
    # Inverse-CDF sampling for truncated Zipf
    while True:
        x = rng.random()
        # Approximate inverse using rank^(-alpha) distribution
        rank = int((1.0 / (x ** (1.0 / alpha))))
        if 0 < rank <= n:
            return rank - 1


@pytest.mark.asyncio
async def test_hit_rate_at_least_90_percent():
    rng = random.Random(42)
    files = _make_files(rng)
    backend = _Backend(files)
    cache = MemoryFileCache(max_bytes=CACHE_MAX_BYTES)

    hits = 0
    misses = 0

    for op in range(TOTAL_OPS):
        idx = _zipf_index(rng, NUM_FILES)
        path = f"/f/{idx}"
        key = FileKey("bench", "default", path, "raw")
        fp = f"fp:{path}"

        cached = await cache.get(key, fp)
        if cached is not None:
            if op >= WARMUP_OPS:
                hits += 1
            continue
        if op >= WARMUP_OPS:
            misses += 1

        lock = await cache.lock(key)
        async with lock:
            recheck = await cache.get(key, fp)
            if recheck is not None:
                continue
            content = await backend.fetch(path)
            await cache.put(key, content, fp, ttl_seconds=600)

    measured = hits + misses
    hit_rate = hits / measured if measured else 0.0
    print(f"hit_rate={hit_rate:.3f}  hits={hits}  misses={misses}  "
          f"backend_calls={backend.calls}  cache_bytes={cache.total_bytes}")
    assert hit_rate >= 0.90, f"hit rate {hit_rate:.3f} below 0.90"


@pytest.mark.asyncio
async def test_singleflight_100_concurrent():
    files = {"/hot": b"payload" * 1000}
    backend = _Backend(files)
    cache = MemoryFileCache(max_bytes=CACHE_MAX_BYTES)
    key = FileKey("bench", "default", "/hot", "raw")
    fp = "fp:/hot"

    async def fetcher() -> bytes:
        lock = await cache.lock(key)
        async with lock:
            cached = await cache.get(key, fp)
            if cached is not None:
                return cached
            content = await backend.fetch("/hot")
            await cache.put(key, content, fp, ttl_seconds=600)
            return content

    results = await asyncio.gather(*[fetcher() for _ in range(100)])
    assert all(r == b"payload" * 1000 for r in results)
    assert backend.calls == 1, f"expected 1 backend call, got {backend.calls}"
```

- [ ] **Step 2: Run the bench**

Run: `pytest benches/cache_hit_rate.py -v -s`

Expected: 2 PASS with printed hit-rate ≥ 0.90.

- [ ] **Step 3: Commit**

```bash
git add benches/cache_hit_rate.py
git commit -m "test(#4080): hit-rate + 100-way singleflight bench"
```

---

### Task 9: Fingerprint-cost benchmark

**Files:**
- New: `benches/cache_fingerprint_cost.py`

- [ ] **Step 1: Write the bench**

Create `benches/cache_fingerprint_cost.py`:

```python
"""Issue #4080: fingerprint cost should be small relative to get_content.

If fingerprint takes >10% of get_content for a backend, document a fallback
to TTL-only policy for that backend.
Run: pytest benches/cache_fingerprint_cost.py -v -s
"""

from __future__ import annotations

import time

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache

ITERATIONS = 10_000


class _FakeBackend:
    def __init__(self, fp_cost_s: float) -> None:
        self._fp_cost_s = fp_cost_s
        self.fp_calls = 0

    def fingerprint(self, path: str) -> str:
        self.fp_calls += 1
        if self._fp_cost_s > 0:
            end = time.perf_counter() + self._fp_cost_s
            while time.perf_counter() < end:
                pass
        return "fp:" + path


def _run_hot_cache(backend: _FakeBackend) -> tuple[float, float]:
    cache = MemoryFileCache(max_bytes=10 * 1024 * 1024)
    key = FileKey("bench", "default", "/hot", "raw")
    cache.put_sync(key, b"x" * 4096, "fp:/hot", ttl_seconds=600)

    fp_total = 0.0
    get_total = 0.0
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        fp = backend.fingerprint("/hot")
        t1 = time.perf_counter()
        cache.get_sync(key, fp)
        t2 = time.perf_counter()
        fp_total += t1 - t0
        get_total += t2 - t0
    return fp_total, get_total


def test_fingerprint_cost_under_10_percent_when_cheap():
    """A 5µs fingerprint (typical for cached stat) should be <10% of get_content."""
    backend = _FakeBackend(fp_cost_s=5e-6)
    fp_total, get_total = _run_hot_cache(backend)
    ratio = fp_total / get_total
    print(f"cheap fingerprint: fp={fp_total*1e3:.1f}ms get={get_total*1e3:.1f}ms "
          f"ratio={ratio:.3f}")
    assert ratio < 0.10, f"fingerprint cost ratio {ratio:.3f} >= 0.10"


def test_fingerprint_cost_breaches_threshold_when_expensive():
    """A 200µs fingerprint should exceed 10% — this documents the fallback signal."""
    backend = _FakeBackend(fp_cost_s=2e-4)
    fp_total, get_total = _run_hot_cache(backend)
    ratio = fp_total / get_total
    print(f"expensive fingerprint: fp={fp_total*1e3:.1f}ms get={get_total*1e3:.1f}ms "
          f"ratio={ratio:.3f}")
    assert ratio >= 0.10, "expected expensive fingerprint to breach 10%"
```

- [ ] **Step 2: Run the bench**

Run: `pytest benches/cache_fingerprint_cost.py -v -s`

Expected: 2 PASS with cheap < 10% and expensive ≥ 10%.

- [ ] **Step 3: Commit**

```bash
git add benches/cache_fingerprint_cost.py
git commit -m "test(#4080): fingerprint cost bench documents TTL-fallback threshold"
```

---

### Task 10: Acceptance criteria sweep + PR

**Files:**
- Modify: `src/nexus/cache/__init__.py` (re-export `MemoryFileCache.DEFAULT_MAX_BYTES` for callers who want the constant)
- Verify: full test suite

- [ ] **Step 1: Re-export the constant**

Open `src/nexus/cache/__init__.py`. Find the existing `__all__` block. Confirm `MemoryFileCache` is exported. No change required if it already is. If a constant export is desired, add `DEFAULT_FILE_CACHE_BYTES = MemoryFileCache.DEFAULT_MAX_BYTES` and add it to `__all__`. Skip if not strictly needed.

- [ ] **Step 2: Run the full unit cache suite**

Run: `pytest tests/unit/cache tests/integration/test_cache_parent_only_invalidation.py -v 2>&1 | tail -20`

Expected: All PASS.

- [ ] **Step 3: Run the benches**

Run: `pytest benches/cache_hit_rate.py benches/cache_fingerprint_cost.py -v -s 2>&1 | tail -20`

Expected: All PASS.

- [ ] **Step 4: Run the FUSE unit tests**

Run: `pytest tests/unit/fuse -v 2>&1 | tail -20`

Expected: All PASS.

- [ ] **Step 5: Run Rust kernel tests**

Run: `cargo test -p kernel cache:: 2>&1 | tail -15`

Expected: All PASS.

- [ ] **Step 6: Tick acceptance criteria on the issue**

Run:

```bash
gh issue comment 4080 --body "$(cat <<'EOF'
Gap-closing landed in branch worktree-glimmering-swimming-candle.

Acceptance criteria status:
- [x] IndexCache + FileCache traits, RAM impls (already shipped)
- [x] Per-key async lock prevents N-way refetch storm
  → tests/unit/cache/test_file_store_lock_lifecycle.py::test_singleflight_100_concurrent_cold_gets
- [x] Parent-only invalidation verified
  → tests/integration/test_cache_parent_only_invalidation.py
- [x] ≥3 backends implement fingerprint(path) (S3, GCS, GitHub)
- [x] Bench: 90%+ hit rate on a re-read workload
  → benches/cache_hit_rate.py::test_hit_rate_at_least_90_percent

Deferred:
- RedisIndexStore (TTL-bound index has low multi-process consistency value vs Dragonfly cost)

Foyer NVMe tier already shipped via #4053 → #4097.
EOF
)"
```

- [ ] **Step 7: Final commit + PR**

```bash
git add -A
git commit -m "chore(#4080): cache split acceptance criteria sweep" --allow-empty
```

Then create PR:

```bash
gh pr create --title "feat(#4080): cache split gap-closing (byte-size LRU, drain cap, TTL knobs, bench)" --body "$(cat <<'EOF'
## Summary

Closes the remaining acceptance criteria from #4080 after the 2026-05-08 split landed.

- Byte-size LRU on \`MemoryFileCache\` and Rust kernel \`FileCache\` (default 512MB content + 64MB parsed)
- \`max_drain_bytes\` cap (default 16MB) on \`FUSECacheManager.cache_content\`
- Per-backend TTL overrides in \`policy.index_ttl_for_backend\` + \`negative_ttl_for_backend\`, threaded via \`cache_config["index_ttl_overrides"]\` in \`fuse/operations.py\`
- Singleflight lock lifecycle: bounded dict + LRU eviction of unused locks (replaces \`WeakValueDictionary\` which could drop a Lock between two waiters' awaits)
- Hit-rate bench (\`benches/cache_hit_rate.py\`) — proves ≥90% on Zipf re-read
- Fingerprint cost bench (\`benches/cache_fingerprint_cost.py\`) — documents the <10% threshold for keeping fingerprint validation enabled

**Deferred:** RedisIndexStore. Index TTLs (60–600s) make multi-process consistency value small vs Dragonfly operational cost. Revisit if a workload demands it.

**Not in scope:** \`nexus-fuse/src/cache.rs\` foyer cache already shipped via #4053/#4097.

## Test plan
- [x] \`pytest tests/unit/cache tests/integration/test_cache_parent_only_invalidation.py\`
- [x] \`pytest tests/unit/fuse\`
- [x] \`pytest benches/cache_hit_rate.py benches/cache_fingerprint_cost.py\`
- [x] \`cargo test -p kernel cache::\`

Spec: docs/superpowers/specs/2026-05-10-issue-4080-cache-gaps-design.md
Plan: docs/superpowers/plans/2026-05-10-issue-4080-cache-gaps.md
EOF
)"
```

Expected: PR URL returned.

---

## Self-review

Spec coverage:
- [x] Byte-size LRU (Python + Rust) → Task 2 + Task 7
- [x] `max_drain_bytes` cap → Task 4 (FUSECacheManager) + Task 5 (call sites)
- [x] Per-backend TTL overrides → Task 1 (policy) + Task 4 (FUSECacheManager threading) + Task 5 (call sites)
- [x] Hit-rate bench (≥90%) → Task 8
- [x] Fingerprint-cost bench → Task 9
- [x] Singleflight lock lifecycle fix → Task 2 (implementation) + Task 3 (tests)
- [x] Parent-only invalidation integration test → Task 6
- [x] Mirrored Rust changes → Task 7
- [x] Acceptance criteria comment on issue → Task 10

Placeholder scan: no TBD/TODO/placeholder strings. All code blocks are complete and executable.

Type consistency: `content_cache_bytes`, `parsed_cache_bytes`, `max_drain_bytes`, `index_ttl_overrides` — same names in spec, plan, FUSECacheManager constructor, and `fuse/operations.py` keys.

Rust API: `FileCache::default()` preserved (unchanged contract for kernel/io.rs callers); `FileCache::with_max_bytes(usize)` added; `FileCache::total_bytes()` added; existing `get/put/lock/invalidate_path` signatures unchanged.
