# Issue #4080 Cache Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the logical `IndexCache` / `FileCache` split across Python FUSE and the Rust kernel, with TTL metadata caching, fingerprint-aware file-byte caching, per-key single-flight fills, and parent-only invalidation while keeping public filesystem behavior stable.

**Architecture:** Add Python logical cache modules under `src/nexus/cache/` and mirror them in `rust/kernel/src/cache/`. Adapt the existing FUSE cache manager, `dir_cache`, `LocalDiskCache`, and file read/mutation handlers to use those logical caches instead of owning independent cache policies. Add a backend `fingerprint()` seam so file-cache validation can prefer cheap backend versions and fall back to TTL when unavailable.

**Tech Stack:** Python, asyncio, cachetools, pytest, Rust, dashmap, parking_lot, cargo test.

---

## File Structure

- Create `src/nexus/cache/index_store.py`: logical TTL-based cache for `stat/getattr`, `readdir/listing`, and negative metadata entries.
- Create `src/nexus/cache/file_store.py`: logical fingerprint-aware file cache with per-key async single-flight locks and TTL fallback.
- Create `src/nexus/cache/policy.py`: backend TTL policy helpers and negative-entry TTL rules.
- Create `src/nexus/cache/invalidation.py`: Python invalidation dataclasses for file-path and parent-listing events.
- Modify `src/nexus/cache/__init__.py`: export the new logical cache types.
- Modify `src/nexus/backends/base/backend.py`: add the `fingerprint()` interface.
- Modify `src/nexus/backends/storage/path_s3.py`: return `VersionId` or `etag:<etag>` as the fingerprint.
- Modify `src/nexus/backends/storage/path_gcs.py`: return object generation as the fingerprint.
- Modify `src/nexus/backends/base/cli_backend.py`: default CLI-backed connectors to TTL fallback by returning `None` for `fingerprint()`.
- Modify `src/nexus/backends/connectors/github/connector.py`: make the GitHub connector participate in the fingerprint seam and explicitly fall back to TTL when directory metadata has no blob SHA.
- Modify `src/nexus/fuse/cache.py`: turn `FUSECacheManager` into a compatibility adapter over the new logical caches.
- Modify `src/nexus/fuse/operations.py`: construct logical caches instead of separate ad hoc RAM caches plus raw `dir_cache`.
- Modify `src/nexus/fuse/ops/_shared.py`: build logical file keys, resolve fingerprints, route L2 reads through logical file-cache semantics, and replace raw `dir_cache` invalidation with canonical parent-listing invalidation.
- Modify `src/nexus/fuse/ops/metadata_handler.py`: use `IndexCache` for `getattr` and `readdir`.
- Modify `src/nexus/fuse/ops/io_handler.py`: use `FileCache` for read fills and file-entry invalidation after writes.
- Modify `src/nexus/fuse/ops/mutation_handler.py`: use canonical file-entry and parent-listing invalidation on create/unlink/mkdir/rmdir/rename.
- Modify `tests/unit/fuse/conftest.py`: expand the cache mock surface for listing/file invalidation methods.
- Modify `src/nexus/storage/local_disk_cache.py`: treat the first argument as an opaque cache key rather than assuming it is only a content hash.
- Modify `src/nexus/storage/file_cache.py`: store/read fingerprint + TTL sidecar metadata so non-FUSE paths can follow the same file-cache semantics.
- Create `rust/kernel/src/cache/mod.rs`: Rust logical cache module root.
- Create `rust/kernel/src/cache/index_cache.rs`: Rust TTL index cache.
- Create `rust/kernel/src/cache/file_cache.rs`: Rust file cache with fingerprint validation and single-flight fill guards.
- Create `rust/kernel/src/cache/invalidation.rs`: Rust invalidation message types.
- Modify `rust/kernel/src/lib.rs`: export the new `cache` module.
- Modify `rust/kernel/src/kernel/mod.rs`: add `Kernel` fields for the logical caches.
- Modify `rust/kernel/src/kernel/io.rs`: read/list hot paths consult logical caches, and mutation paths invalidate the file path plus immediate parent listing.
- Create `tests/unit/cache/test_index_store.py`: TTL and parent-only invalidation tests for the Python logical index cache.
- Create `tests/unit/cache/test_file_store.py`: fingerprint, TTL fallback, and single-flight tests for the Python logical file cache.
- Create `tests/unit/backends/test_backend_fingerprints.py`: backend fingerprint tests for S3, GCS, CLI fallback, and GitHub TTL fallback.
- Modify `tests/unit/fuse/conftest.py`: expose logical cache mocks.
- Modify `tests/unit/fuse/test_metadata_handler.py`: listing/stat cache tests use the logical index cache surface.
- Modify `tests/unit/fuse/test_io_handler.py`: file-cache invalidation and expected-fingerprint read behavior.
- Modify `tests/unit/fuse/test_mutation_handler.py`: parent-only invalidation rules for file and directory mutations.
- Modify `tests/storage/test_local_disk_cache.py`: opaque cache-key tests and logical-key persistence.
- Modify `tests/unit/storage/test_file_cache.py`: fingerprint + TTL sidecar tests for the path-addressed disk cache.
- Create `tests/unit/fuse/test_cache_split_coherence.py`: concurrent reader single-flight test and parent-only invalidation coherence test.
- Modify `tests/unit/integration/test_lease_aware_cache.py`: verify the logical cache split still cooperates with lease revocation.

## Task 1: Add Python Logical Cache Primitives And Contract Tests

**Files:**
- Create: `src/nexus/cache/index_store.py`
- Create: `src/nexus/cache/file_store.py`
- Create: `src/nexus/cache/policy.py`
- Create: `src/nexus/cache/invalidation.py`
- Modify: `src/nexus/cache/__init__.py`
- Create: `tests/unit/cache/test_index_store.py`
- Create: `tests/unit/cache/test_file_store.py`

- [ ] **Step 1: Write failing unit tests for TTL expiry, parent-only invalidation, fingerprint mismatch, and single-flight**

Add these tests:

```python
# tests/unit/cache/test_index_store.py
from nexus.cache.policy import negative_ttl_for_backend
from nexus.cache.index_store import IndexKey, MemoryIndexCache


def test_memory_index_cache_expires_positive_entry() -> None:
    now = [100.0]
    cache = MemoryIndexCache(now_fn=lambda: now[0])
    key = IndexKey("path_s3", "zone1", "/bucket/foo", "listing")

    cache.put(key, [".", "..", "a.txt"], ttl_seconds=5)
    assert cache.get(key) == [".", "..", "a.txt"]

    now[0] += 6
    assert cache.get(key) is None


def test_memory_index_cache_invalidates_only_parent_listing() -> None:
    cache = MemoryIndexCache(now_fn=lambda: 100.0)
    root_key = IndexKey("path_s3", "zone1", "/a", "listing")
    child_key = IndexKey("path_s3", "zone1", "/a/b", "listing")

    cache.put(root_key, [".", "..", "b"], ttl_seconds=60)
    cache.put(child_key, [".", "..", "c.txt"], ttl_seconds=60)

    cache.invalidate_parent_listing("path_s3", "zone1", "/a/b/c.txt")

    assert cache.get(root_key) == [".", "..", "b"]
    assert cache.get(child_key) is None


def test_memory_index_cache_expires_negative_entry_with_short_ttl() -> None:
    now = [100.0]
    cache = MemoryIndexCache(now_fn=lambda: now[0])
    key = IndexKey("path_s3", "zone1", "/bucket/missing.txt", "negative")

    cache.put(key, {"missing": True}, ttl_seconds=negative_ttl_for_backend("path_s3"))
    assert cache.get(key) == {"missing": True}

    now[0] += 6
    assert cache.get(key) is None
```

```python
# tests/unit/cache/test_file_store.py
import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache


@pytest.mark.asyncio
async def test_memory_file_cache_rejects_mismatched_fingerprint() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")

    await cache.put(key, b"old-bytes", fingerprint="etag:old")
    assert await cache.get(key, expected_fingerprint="etag:new") is None


@pytest.mark.asyncio
async def test_memory_file_cache_uses_ttl_fallback_without_fingerprint() -> None:
    now = [100.0]
    cache = MemoryFileCache(now_fn=lambda: now[0])
    key = FileKey("github_connector", "zone1", "/issues/1_test.yaml")

    await cache.put(key, b"cached", fingerprint=None, ttl_seconds=5)
    assert await cache.get(key, expected_fingerprint=None) == b"cached"

    now[0] += 6
    assert await cache.get(key, expected_fingerprint=None) is None


@pytest.mark.asyncio
async def test_memory_file_cache_singleflight_allows_one_fill() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    fill_calls = 0

    async def worker() -> bytes | None:
        nonlocal fill_calls
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, expected_fingerprint="etag:1")
            if hit is None:
                fill_calls += 1
                await asyncio.sleep(0.01)
                await cache.put(key, b"payload", fingerprint="etag:1")
        return await cache.get(key, expected_fingerprint="etag:1")

    results = await asyncio.gather(*(worker() for _ in range(25)))
    assert results == [b"payload"] * 25
    assert fill_calls == 1
```

- [ ] **Step 2: Run the new cache tests and verify they fail**

Run:

```bash
pytest tests/unit/cache/test_index_store.py tests/unit/cache/test_file_store.py -v
```

Expected: `FAILED` with `ModuleNotFoundError` or import errors for `nexus.cache.index_store` and `nexus.cache.file_store`.

- [ ] **Step 3: Implement the Python logical cache modules**

Create the modules with these contents:

```python
# src/nexus/cache/index_store.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Callable, Literal
import time


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
        listing_key = IndexKey(backend_id=backend_id, scope_id=scope_id, path=parent, kind="listing")
        self.invalidate_path(listing_key)
```

```python
# src/nexus/cache/file_store.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable


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
        self._locks: dict[FileKey, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()

    async def get(self, key: FileKey, expected_fingerprint: str | None) -> bytes | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= self._now_fn():
            self._entries.pop(key, None)
            return None
        if expected_fingerprint is not None:
            return entry.content if entry.fingerprint == expected_fingerprint else None
        return entry.content

    async def put(
        self,
        key: FileKey,
        content: bytes,
        fingerprint: str | None,
        ttl_seconds: int | None = None,
    ) -> None:
        expires_at = None if ttl_seconds is None else self._now_fn() + max(ttl_seconds, 0)
        self._entries[key] = _FileEntry(content=content, fingerprint=fingerprint, expires_at=expires_at)

    async def invalidate(self, key: FileKey) -> None:
        self._entries.pop(key, None)

    async def lock(self, key: FileKey) -> asyncio.Lock:
        async with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
```

```python
# src/nexus/cache/policy.py
from __future__ import annotations


INDEX_TTL_BY_BACKEND = {
    "local": 0,
    "path_local": 0,
    "disk": 60,
    "path_s3": 600,
    "path_gcs": 600,
    "github_connector": 600,
}


def index_ttl_for_backend(backend_id: str) -> int:
    return INDEX_TTL_BY_BACKEND.get(backend_id, 60)


def negative_ttl_for_backend(backend_id: str) -> int:
    return min(5, index_ttl_for_backend(backend_id))
```

```python
# src/nexus/cache/invalidation.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilePathInvalidation:
    backend_id: str
    scope_id: str
    path: str
    namespace: str = "raw"


@dataclass(frozen=True)
class ParentListingInvalidation:
    backend_id: str
    scope_id: str
    path: str
```

- [ ] **Step 4: Run the cache contract tests and verify they pass**

Run:

```bash
pytest tests/unit/cache/test_index_store.py tests/unit/cache/test_file_store.py -v
```

Expected: `PASSED` for all cache contract tests.

- [ ] **Step 5: Commit the logical Python cache layer**

Run:

```bash
git add src/nexus/cache/__init__.py src/nexus/cache/index_store.py src/nexus/cache/file_store.py src/nexus/cache/policy.py src/nexus/cache/invalidation.py tests/unit/cache/test_index_store.py tests/unit/cache/test_file_store.py
git commit -m "feat: add logical index and file cache primitives"
```

## Task 2: Add Backend Fingerprint Plumbing

**Files:**
- Modify: `src/nexus/backends/base/backend.py`
- Modify: `src/nexus/backends/storage/path_s3.py`
- Modify: `src/nexus/backends/storage/path_gcs.py`
- Modify: `src/nexus/backends/base/cli_backend.py`
- Modify: `src/nexus/backends/connectors/github/connector.py`
- Create: `tests/unit/backends/test_backend_fingerprints.py`

- [ ] **Step 1: Add failing tests for S3, GCS, CLI fallback, and GitHub TTL fallback**

Create `tests/unit/backends/test_backend_fingerprints.py`:

```python
from unittest.mock import MagicMock

from nexus.backends.base.cli_backend import PathCLIBackend
from nexus.backends.connectors.github.connector import GitHubConnector
from nexus.backends.storage.path_gcs import PathGCSBackend
from nexus.backends.storage.path_s3 import PathS3Backend


def test_path_s3_fingerprint_prefers_version_id_then_etag() -> None:
    backend = object.__new__(PathS3Backend)
    backend._s3_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._s3_transport.get_object_metadata.return_value = {
        "version_id": "v123",
        "etag": "abc123",
        "size": 1,
        "last_modified": None,
    }

    assert backend.fingerprint("/file.txt") == "v123"

    backend._s3_transport.get_object_metadata.return_value["version_id"] = "null"
    assert backend.fingerprint("/file.txt") == "etag:abc123"


def test_path_gcs_fingerprint_returns_generation() -> None:
    backend = object.__new__(PathGCSBackend)
    backend._gcs_transport = MagicMock()
    backend._get_key_path = lambda path: path
    backend._gcs_transport.get_generation.return_value = "456"

    assert backend.fingerprint("/file.txt") == "456"


def test_cli_backend_fingerprint_defaults_to_none() -> None:
    backend = object.__new__(PathCLIBackend)
    assert backend.fingerprint("/issues/1_test.yaml") is None


def test_github_connector_fingerprint_falls_back_to_none_without_sha() -> None:
    backend = object.__new__(GitHubConnector)
    backend.list_dir_metadata = MagicMock(
        return_value={"1_test.yaml": {"number": 1, "title": "Test issue"}}
    )

    assert backend.fingerprint("/issues/1_test.yaml") is None
```

- [ ] **Step 2: Run the fingerprint tests and verify they fail**

Run:

```bash
pytest tests/unit/backends/test_backend_fingerprints.py -v
```

Expected: `FAILED` because `fingerprint()` is not implemented on the current classes.

- [ ] **Step 3: Implement the backend fingerprint seam**

Apply these changes:

```python
# src/nexus/backends/base/backend.py
class Backend(ObjectStoreABC):
    ...
    def fingerprint(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """Return a cheap file fingerprint when the backend exposes one.

        Returning ``None`` means the caller must use a TTL fallback instead of
        validation-on-read.
        """
        return None
```

```python
# src/nexus/backends/storage/path_s3.py
def fingerprint(self, path: str, context: "OperationContext | None" = None) -> str | None:
    backend_path = context.backend_path if context and context.backend_path else path.lstrip("/")
    meta = self._s3_transport.get_object_metadata(self._get_key_path(backend_path))
    version_id = meta.get("version_id")
    if version_id and version_id != "null":
        return version_id
    etag = meta.get("etag")
    return f"etag:{etag}" if etag else None
```

```python
# src/nexus/backends/storage/path_gcs.py
def fingerprint(self, path: str, context: "OperationContext | None" = None) -> str | None:
    backend_path = context.backend_path if context and context.backend_path else path.lstrip("/")
    return self._gcs_transport.get_generation(self._get_key_path(backend_path))
```

```python
# src/nexus/backends/base/cli_backend.py
def fingerprint(
    self,
    path: str,
    context: "OperationContext | None" = None,
) -> str | None:
    return None
```

```python
# src/nexus/backends/connectors/github/connector.py
def fingerprint(self, path: str, context: Any | None = None) -> str | None:
    parent, _, name = path.rpartition("/")
    metadata = self.list_dir_metadata(parent or "/", context=context) or {}
    row = metadata.get(name) or {}
    candidate = row.get("sha") or row.get("blob_sha") or row.get("etag")
    return str(candidate) if candidate else None
```

- [ ] **Step 4: Run the fingerprint tests and verify they pass**

Run:

```bash
pytest tests/unit/backends/test_backend_fingerprints.py -v
```

Expected: `PASSED`, with S3 and GCS returning concrete fingerprints and CLI-backed connectors returning `None` when they have no cheap metadata seam.

- [ ] **Step 5: Commit the fingerprint plumbing**

Run:

```bash
git add src/nexus/backends/base/backend.py src/nexus/backends/storage/path_s3.py src/nexus/backends/storage/path_gcs.py src/nexus/backends/base/cli_backend.py src/nexus/backends/connectors/github/connector.py tests/unit/backends/test_backend_fingerprints.py
git commit -m "feat: add backend fingerprint support"
```

## Task 3: Cut FUSE `getattr` And `readdir` Over To `IndexCache`

**Files:**
- Modify: `src/nexus/fuse/cache.py`
- Modify: `src/nexus/fuse/operations.py`
- Modify: `src/nexus/fuse/ops/_shared.py`
- Modify: `src/nexus/fuse/ops/metadata_handler.py`
- Modify: `tests/unit/fuse/conftest.py`
- Modify: `tests/unit/fuse/test_metadata_handler.py`

- [ ] **Step 1: Add failing FUSE metadata tests for listing hits and parent-only invalidation**

Add these tests:

```python
# tests/unit/fuse/test_metadata_handler.py
def test_readdir_cache_hit_uses_logical_listing_cache(
    self,
    fuse_ops: Any,
    mock_nexus_fs: MagicMock,
) -> None:
    fuse_ops.cache.cache_listing("/cached", [".", "..", "a.txt"])

    entries = fuse_ops.readdir("/cached")

    assert entries == [".", "..", "a.txt"]
    mock_nexus_fs.sys_readdir.assert_not_called()


def test_parent_only_listing_invalidation_keeps_grandparent() -> None:
    cache = FUSECacheManager(attr_cache_size=8, attr_cache_ttl=60, content_cache_size=8, parsed_cache_size=8)
    cache.cache_listing("/a", [".", "..", "b"])
    cache.cache_listing("/a/b", [".", "..", "c.txt"])

    cache.invalidate_parent_listing("/a/b/c.txt")

    assert cache.get_listing("/a") == [".", "..", "b"]
    assert cache.get_listing("/a/b") is None
```

- [ ] **Step 2: Run the FUSE metadata tests and verify they fail**

Run:

```bash
pytest tests/unit/fuse/test_metadata_handler.py -v
```

Expected: `FAILED` because `FUSECacheManager` does not yet expose `get_listing`, `cache_listing`, or `invalidate_parent_listing`.

- [ ] **Step 3: Turn `FUSECacheManager` into an `IndexCache` compatibility adapter**

Apply these edits:

```python
# src/nexus/fuse/cache.py
from nexus.cache.index_store import IndexKey, MemoryIndexCache
from nexus.cache.file_store import FileKey, MemoryFileCache


class FUSECacheManager:
    def __init__(..., attr_cache_ttl: int = 60, ...):
        self._attr_ttl = attr_cache_ttl
        self._listing_ttl = attr_cache_ttl
        self._index_cache = MemoryIndexCache()
        self._file_cache = MemoryFileCache()
        ...

    def _stat_key(self, path: str) -> IndexKey:
        return IndexKey("fuse", "default", path, "stat")

    def _listing_key(self, path: str) -> IndexKey:
        return IndexKey("fuse", "default", path, "listing")

    def get_attr(self, path: str) -> dict[str, Any] | None:
        return self._index_cache.get(self._stat_key(path))

    def cache_attr(self, path: str, attrs: dict[str, Any]) -> None:
        self._index_cache.put(self._stat_key(path), attrs, ttl_seconds=self._attr_ttl)

    def get_listing(self, path: str) -> list[str] | None:
        return self._index_cache.get(self._listing_key(path))

    def cache_listing(self, path: str, entries: list[str]) -> None:
        self._index_cache.put(self._listing_key(path), entries, ttl_seconds=self._listing_ttl)

    def invalidate_parent_listing(self, path: str) -> None:
        self._index_cache.invalidate_parent_listing("fuse", "default", path)
```

```python
# src/nexus/fuse/ops/_shared.py
def invalidate_dir_cache(ctx: FUSESharedContext, path: str) -> None:
    ctx.cache.invalidate_parent_listing(path)
```

```python
# src/nexus/fuse/ops/metadata_handler.py
cache_entries = ctx.cache.get_listing(path)
if cache_entries is not None:
    return cache_entries
...
ctx.cache.cache_listing(path, entries)
```

```python
# tests/unit/fuse/conftest.py
@pytest.fixture()
def mock_cache() -> MagicMock:
    cache = MagicMock()
    cache.get_attr.return_value = None
    cache.get_listing.return_value = None
    cache.get_content.return_value = None
    cache.get_parsed.return_value = None
    cache.invalidate_parent_listing.return_value = None
    cache.invalidate_file.return_value = None
    return cache
```

- [ ] **Step 4: Run the metadata tests and verify they pass**

Run:

```bash
pytest tests/unit/fuse/test_metadata_handler.py -v
```

Expected: `PASSED`, including the logical listing-cache hit and parent-only invalidation tests.

- [ ] **Step 5: Commit the FUSE metadata cutover**

Run:

```bash
git add src/nexus/fuse/cache.py src/nexus/fuse/operations.py src/nexus/fuse/ops/_shared.py src/nexus/fuse/ops/metadata_handler.py tests/unit/fuse/conftest.py tests/unit/fuse/test_metadata_handler.py
git commit -m "feat: route fuse metadata through index cache"
```

## Task 4: Cut FUSE File Reads, L2 Storage, And Mutations Over To `FileCache`

**Files:**
- Modify: `src/nexus/storage/local_disk_cache.py`
- Modify: `src/nexus/storage/file_cache.py`
- Modify: `src/nexus/fuse/cache.py`
- Modify: `src/nexus/fuse/ops/_shared.py`
- Modify: `src/nexus/fuse/ops/io_handler.py`
- Modify: `src/nexus/fuse/ops/mutation_handler.py`
- Modify: `tests/storage/test_local_disk_cache.py`
- Modify: `tests/unit/storage/test_file_cache.py`
- Modify: `tests/unit/fuse/test_io_handler.py`
- Modify: `tests/unit/fuse/test_mutation_handler.py`
- Create: `tests/unit/fuse/test_cache_split_coherence.py`
- Modify: `tests/unit/integration/test_lease_aware_cache.py`

- [ ] **Step 1: Add failing tests for opaque L2 cache keys, expected-fingerprint reads, and parent-only mutation invalidation**

Add these tests:

```python
# tests/storage/test_local_disk_cache.py
def test_local_disk_cache_accepts_opaque_logical_cache_key(cache) -> None:
    cache_key = "path_s3:zone1:/bucket/foo.txt:etag:abc123:raw"
    assert cache.put(cache_key, b"payload") is True
    assert cache.get(cache_key) == b"payload"
```

```python
# tests/unit/fuse/test_io_handler.py
def test_write_invalidates_file_and_parent_listing(
    self,
    fuse_ops: Any,
    mock_nexus_fs: MagicMock,
    mock_cache: MagicMock,
) -> None:
    mock_nexus_fs.access.return_value = True
    mock_nexus_fs.sys_read.return_value = b""
    fd = fuse_ops.open("/dir/file.txt", os.O_RDWR)
    fuse_ops.cache = mock_cache

    fuse_ops.write("/dir/file.txt", b"data", 0, fd)

    mock_cache.invalidate_file.assert_called_once_with("/dir/file.txt")
    mock_cache.invalidate_parent_listing.assert_called_once_with("/dir/file.txt")
```

```python
# tests/unit/fuse/test_cache_split_coherence.py
import asyncio

import pytest

from nexus.cache.file_store import FileKey, MemoryFileCache
from nexus.fuse.cache import FUSECacheManager


@pytest.mark.asyncio
async def test_logical_file_cache_singleflight_keeps_one_origin_fill() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("path_s3", "zone1", "/bucket/foo.txt")
    calls = 0

    async def read_once() -> bytes | None:
        nonlocal calls
        lock = await cache.lock(key)
        async with lock:
            hit = await cache.get(key, expected_fingerprint="etag:1")
            if hit is None:
                calls += 1
                await asyncio.sleep(0.01)
                await cache.put(key, b"payload", fingerprint="etag:1")
        return await cache.get(key, expected_fingerprint="etag:1")

    assert await asyncio.gather(*(read_once() for _ in range(20))) == [b"payload"] * 20
    assert calls == 1


def test_invalidate_file_clears_parsed_views_for_source_path() -> None:
    cache = FUSECacheManager(attr_cache_size=8, attr_cache_ttl=60, content_cache_size=8, parsed_cache_size=8)
    cache.cache_content("/report.xlsx", b"raw", fingerprint="etag:1")
    cache.cache_parsed("/report.xlsx", "md", b"# parsed")

    cache.invalidate_file("/report.xlsx")

    assert cache.get_content("/report.xlsx", expected_fingerprint="etag:1") is None
    assert cache.get_parsed("/report.xlsx", "md") is None
```

- [ ] **Step 2: Run the file-cache and mutation tests and verify they fail**

Run:

```bash
pytest tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py -v
```

Expected: `FAILED` because the current code assumes content-hash L2 keys, has no `invalidate_file()` method on the compatibility cache surface, and still uses ad hoc invalidation instead of the canonical file + parent-listing split.

- [ ] **Step 3: Implement the logical file-cache cutover**

Make these changes:

```python
# src/nexus/fuse/cache.py
    def _file_key(self, path: str, namespace: str = "raw") -> FileKey:
        return FileKey("fuse", "default", path, namespace)

    def _parsed_key(self, path: str, view_type: str) -> FileKey:
        return FileKey("fuse", "default", path, f"parsed:{view_type}")

    def get_content(self, path: str, expected_fingerprint: str | None = None) -> bytes | None:
        return asyncio.run(self._file_cache.get(self._file_key(path), expected_fingerprint))

    def cache_content(
        self,
        path: str,
        content: bytes,
        *,
        fingerprint: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        asyncio.run(
            self._file_cache.put(
                self._file_key(path),
                content,
                fingerprint=fingerprint,
                ttl_seconds=ttl_seconds,
            )
        )

    def get_parsed(self, path: str, view_type: str) -> bytes | None:
        return asyncio.run(self._file_cache.get(self._parsed_key(path, view_type), None))

    def cache_parsed(self, path: str, view_type: str, content: bytes) -> None:
        asyncio.run(self._file_cache.put(self._parsed_key(path, view_type), content, fingerprint=None))

    def invalidate_file(self, path: str, namespace: str | None = None) -> None:
        if namespace is not None:
            asyncio.run(self._file_cache.invalidate(self._file_key(path, namespace)))
            return
        for key in list(self._file_cache._entries):
            if key.path == path:
                asyncio.run(self._file_cache.invalidate(key))
```

```python
# src/nexus/fuse/ops/_shared.py
def get_file_fingerprint(ctx: FUSESharedContext, path: str) -> str | None:
    metadata = asyncio.run(get_metadata(ctx, path))
    if metadata is None:
        return None
    if getattr(metadata, "content_id", None):
        return metadata.content_id
    version = getattr(metadata, "version", None)
    return str(version) if version else None


def logical_l2_key(ctx: FUSESharedContext, path: str, fingerprint: str | None, namespace: str = "raw") -> str:
    zone_id = get_zone_id(ctx) or "root"
    suffix = fingerprint or "ttl-fallback"
    return f"fuse:{zone_id}:{path}:{suffix}:{namespace}"


async def get_file_content(...):
    expected_fingerprint = get_file_fingerprint(ctx, path)
    cache_key = logical_l2_key(ctx, path, expected_fingerprint, view_type or "raw")
    cached = ctx.cache.get_content(path, expected_fingerprint=expected_fingerprint)
    if cached is not None:
        return _maybe_parse(ctx, path, view_type, cached)
    ...
    if ctx.local_disk_cache is not None:
        disk_hit = ctx.local_disk_cache.get(cache_key, zone_id=get_zone_id(ctx))
        if disk_hit is not None:
            ctx.cache.cache_content(path, disk_hit, fingerprint=expected_fingerprint)
            return _maybe_parse(ctx, path, view_type, disk_hit)
    ...
    if has_lease:
        ctx.cache.cache_content(path, content, fingerprint=expected_fingerprint)
    if ctx.local_disk_cache is not None:
        ctx.local_disk_cache.put(cache_key, content, zone_id=get_zone_id(ctx))
```

```python
# src/nexus/fuse/ops/io_handler.py
        ctx.cache.invalidate_file(original_path)
        ctx.cache.invalidate_parent_listing(original_path)
```

```python
# src/nexus/fuse/ops/mutation_handler.py
        ctx.cache.invalidate_file(original_path)
        ctx.cache.invalidate_parent_listing(original_path)
...
        ctx.cache.invalidate_file(old_path)
        ctx.cache.invalidate_file(new_path)
        ctx.cache.invalidate_parent_listing(old_path)
        ctx.cache.invalidate_parent_listing(new_path)
```

```python
# src/nexus/storage/local_disk_cache.py
def _make_cache_key(self, cache_key: str, zone_id: str | None = None) -> str:
    if zone_id:
        from nexus.lib.zone import validate_zone_id
        validate_zone_id(zone_id)
        return f"{zone_id}:{cache_key}"
    return cache_key
```

```python
# src/nexus/storage/file_cache.py
def read_if_fresh(
    self,
    zone_id: str,
    virtual_path: str,
    expected_fingerprint: str | None,
) -> bytes | None:
    meta = self.read_meta(zone_id, virtual_path)
    if meta is None:
        return None
    if expected_fingerprint is not None:
        if meta.get("fingerprint") != expected_fingerprint:
            return None
    else:
        expires_at = meta.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            return None
    return self.read(zone_id, virtual_path)
```

- [ ] **Step 4: Run the file-cache and mutation tests and verify they pass**

Run:

```bash
pytest tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py -v
```

Expected: `PASSED`, including the opaque logical L2 key path, canonical file-entry invalidation, and single-flight coherence tests.

- [ ] **Step 5: Commit the FUSE file-cache cutover**

Run:

```bash
git add src/nexus/storage/local_disk_cache.py src/nexus/storage/file_cache.py src/nexus/fuse/cache.py src/nexus/fuse/ops/_shared.py src/nexus/fuse/ops/io_handler.py src/nexus/fuse/ops/mutation_handler.py tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py
git commit -m "feat: route fuse file reads through logical file cache"
```

## Task 5: Mirror The Logical Cache Split In Rust Kernel Hot Paths

**Files:**
- Create: `rust/kernel/src/cache/mod.rs`
- Create: `rust/kernel/src/cache/index_cache.rs`
- Create: `rust/kernel/src/cache/file_cache.rs`
- Create: `rust/kernel/src/cache/invalidation.rs`
- Modify: `rust/kernel/src/lib.rs`
- Modify: `rust/kernel/src/kernel/mod.rs`
- Modify: `rust/kernel/src/kernel/io.rs`

- [ ] **Step 1: Add failing Rust tests for index TTL, file fingerprint validation, and single-flight fills**

Create these tests inside the new Rust modules:

```rust
// rust/kernel/src/cache/index_cache.rs
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn expires_listing_entry_after_ttl() {
        let now = Instant::now();
        let cache = IndexCache::new_for_tests(now);
        let key = IndexCacheKey::new("root", "/a/b", IndexKind::Listing);
        cache.put_listing(key.clone(), vec![("a.txt".into(), 1)], Duration::from_secs(1));
        assert_eq!(cache.get_listing(&key), Some(vec![("a.txt".into(), 1)]));
        cache.set_now_for_tests(now + Duration::from_secs(2));
        assert_eq!(cache.get_listing(&key), None);
    }
}
```

```rust
// rust/kernel/src/cache/file_cache.rs
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
            for _ in 0..20 {
                let cache = Arc::clone(&cache);
                let key = key.clone();
                let fills = Arc::clone(&fills);
                scope.spawn(move || {
                    let _guard = cache.lock(&key);
                    if cache.get(&key, Some("etag:1")).is_none() {
                        fills.fetch_add(1, Ordering::SeqCst);
                        cache.put(key.clone(), b"payload".to_vec(), Some("etag:1".into()), None);
                    }
                    assert_eq!(cache.get(&key, Some("etag:1")), Some(b"payload".to_vec()));
                });
            }
        });

        assert_eq!(fills.load(Ordering::SeqCst), 1);
    }
}
```

- [ ] **Step 2: Run the Rust tests and verify they fail**

Run:

```bash
cargo test --manifest-path rust/kernel/Cargo.toml --lib cache:: -q
```

Expected: `FAILED` because the `cache` module does not yet exist.

- [ ] **Step 3: Implement the Rust logical caches and wire them into `Kernel`**

Add these structures and hooks:

```rust
// rust/kernel/src/cache/mod.rs
pub mod file_cache;
pub mod index_cache;
pub mod invalidation;
```

```rust
// rust/kernel/src/lib.rs
pub mod cache;
```

```rust
// rust/kernel/src/kernel/mod.rs
pub struct Kernel {
    ...
    index_cache: crate::cache::index_cache::IndexCache,
    file_cache: crate::cache::file_cache::FileCache,
}
```

```rust
// rust/kernel/src/kernel/io.rs
pub fn readdir(&self, parent_path: &str, zone_id: &str, is_admin: bool) -> Vec<(String, u8)> {
    let key = crate::cache::index_cache::IndexCacheKey::new(zone_id, parent_path, crate::cache::index_cache::IndexKind::Listing);
    if let Some(entries) = self.index_cache.get_listing(&key) {
        return entries;
    }
    let entries = self.readdir_uncached(parent_path, zone_id, is_admin);
    let ttl = crate::cache::index_cache::ttl_for_backend("kernel");
    self.index_cache.put_listing(key, entries.clone(), ttl);
    entries
}
```

```rust
// rust/kernel/src/kernel/io.rs
let file_key = crate::cache::file_cache::FileCacheKey::new(&ctx.zone_id, path, "raw");
let expected_fingerprint = entry.content_id.clone().or_else(|| {
    if entry.version > 0 { Some(entry.version.to_string()) } else { None }
});
if let Some(bytes) = self.file_cache.get(&file_key, expected_fingerprint.as_deref()) {
    return Ok(SysReadResult {
        data: Some(bytes),
        post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
        content_id: expected_fingerprint,
        entry_type: DT_REG,
        stream_next_offset: None,
    });
}
let _guard = self.file_cache.lock(&file_key);
```

```rust
// rust/kernel/src/kernel/io.rs
self.file_cache.invalidate_path(&ctx.zone_id, path, "raw");
self.index_cache.invalidate_parent_listing(&ctx.zone_id, path);
```

- [ ] **Step 4: Run the Rust tests and verify they pass**

Run:

```bash
cargo test --manifest-path rust/kernel/Cargo.toml --lib cache:: -q
```

Expected: `PASSED` for the new logical cache modules and their kernel hot-path wiring.

- [ ] **Step 5: Commit the Rust cache split**

Run:

```bash
git add rust/kernel/src/cache/mod.rs rust/kernel/src/cache/index_cache.rs rust/kernel/src/cache/file_cache.rs rust/kernel/src/cache/invalidation.rs rust/kernel/src/lib.rs rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs
git commit -m "feat: add rust logical cache split"
```

## Task 6: Add Cross-Layer Acceptance Coverage And Final Verification

**Files:**
- Modify: `tests/unit/backends/test_backend_fingerprints.py`
- Modify: `tests/unit/fuse/test_metadata_handler.py`
- Modify: `tests/unit/fuse/test_io_handler.py`
- Modify: `tests/unit/fuse/test_mutation_handler.py`
- Modify: `tests/unit/fuse/test_cache_split_coherence.py`
- Modify: `tests/unit/integration/test_lease_aware_cache.py`

- [ ] **Step 1: Add failing acceptance tests that map directly to the approved spec**

Add these assertions:

```python
# tests/unit/fuse/test_cache_split_coherence.py
def test_parent_only_invalidation_does_not_clear_grandparent_listing() -> None:
    cache = FUSECacheManager(attr_cache_size=8, attr_cache_ttl=60, content_cache_size=8, parsed_cache_size=8)
    cache.cache_listing("/a", [".", "..", "b"])
    cache.cache_listing("/a/b", [".", "..", "c.txt"])

    cache.invalidate_parent_listing("/a/b/c.txt")

    assert cache.get_listing("/a") == [".", "..", "b"]
    assert cache.get_listing("/a/b") is None


@pytest.mark.asyncio
async def test_ttl_fallback_path_is_used_for_cli_backends_without_fingerprint() -> None:
    cache = MemoryFileCache(now_fn=lambda: 100.0)
    key = FileKey("github_connector", "zone1", "/issues/1_test.yaml")
    await cache.put(key, b"cached", fingerprint=None, ttl_seconds=5)
    assert await cache.get(key, expected_fingerprint=None) == b"cached"


def test_parsed_view_invalidation_tracks_source_path() -> None:
    cache = FUSECacheManager(attr_cache_size=8, attr_cache_ttl=60, content_cache_size=8, parsed_cache_size=8)
    cache.cache_content("/report.xlsx", b"raw", fingerprint="etag:1")
    cache.cache_parsed("/report.xlsx", "md", b"# parsed")

    cache.invalidate_file("/report.xlsx")

    assert cache.get_content("/report.xlsx", expected_fingerprint="etag:1") is None
    assert cache.get_parsed("/report.xlsx", "md") is None
```

```python
# tests/unit/integration/test_lease_aware_cache.py
def test_lease_revocation_clears_logical_file_and_listing_entries() -> None:
    cache = FUSECacheManager(attr_cache_size=8, attr_cache_ttl=60, content_cache_size=8, parsed_cache_size=8)
    cache.cache_attr(PATH, {"st_size": 11})
    cache.cache_listing("/mnt/gcs", [".", "..", "file.txt"])
    cache.cache_content(PATH, b"hello world", fingerprint="etag:1")

    cache.on_lease_revoked(PATH)
    cache.invalidate_parent_listing(PATH)

    assert cache.get_attr(PATH) is None
    assert cache.get_listing("/mnt/gcs") is None
    assert cache.get_content(PATH, expected_fingerprint="etag:1") is None
```

- [ ] **Step 2: Run the acceptance suite and verify the remaining gaps fail**

Run:

```bash
pytest tests/unit/cache/test_index_store.py tests/unit/cache/test_file_store.py tests/unit/backends/test_backend_fingerprints.py tests/unit/fuse/test_metadata_handler.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py -v
```

Expected: any remaining failures point to genuine spec gaps, not missing test files or import errors.

- [ ] **Step 3: Close the remaining gaps without widening scope**

Limit the final sweep to:

```python
# Keep the fixes scoped to these surfaces only
FILES_TO_TOUCH = [
    "src/nexus/cache/index_store.py",
    "src/nexus/cache/file_store.py",
    "src/nexus/cache/policy.py",
    "src/nexus/cache/invalidation.py",
    "src/nexus/backends/base/backend.py",
    "src/nexus/backends/storage/path_s3.py",
    "src/nexus/backends/storage/path_gcs.py",
    "src/nexus/backends/base/cli_backend.py",
    "src/nexus/backends/connectors/github/connector.py",
    "src/nexus/fuse/cache.py",
    "src/nexus/fuse/operations.py",
    "src/nexus/fuse/ops/_shared.py",
    "src/nexus/fuse/ops/metadata_handler.py",
    "src/nexus/fuse/ops/io_handler.py",
    "src/nexus/fuse/ops/mutation_handler.py",
    "src/nexus/storage/local_disk_cache.py",
    "src/nexus/storage/file_cache.py",
    "rust/kernel/src/cache/mod.rs",
    "rust/kernel/src/cache/index_cache.rs",
    "rust/kernel/src/cache/file_cache.rs",
    "rust/kernel/src/cache/invalidation.rs",
    "rust/kernel/src/lib.rs",
    "rust/kernel/src/kernel/mod.rs",
    "rust/kernel/src/kernel/io.rs",
]
```

Do not widen into unrelated cache systems such as ReBAC, auth caches, or cache-warmer refactors during this task.

- [ ] **Step 4: Run the full targeted verification set and verify it is green**

Run:

```bash
pytest tests/unit/cache/test_index_store.py tests/unit/cache/test_file_store.py tests/unit/backends/test_backend_fingerprints.py tests/unit/fuse/test_metadata_handler.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py -v
cargo test --manifest-path rust/kernel/Cargo.toml --lib cache:: -q
```

Expected:

- all targeted Python tests `PASSED`
- Rust cache tests `PASSED`
- no acceptance gap remains for TTL metadata, fingerprint validation, single-flight, or parent-only invalidation

- [ ] **Step 5: Commit the acceptance sweep**

Run:

```bash
git add tests/unit/backends/test_backend_fingerprints.py tests/unit/fuse/test_metadata_handler.py tests/unit/fuse/test_io_handler.py tests/unit/fuse/test_mutation_handler.py tests/unit/fuse/test_cache_split_coherence.py tests/unit/integration/test_lease_aware_cache.py tests/storage/test_local_disk_cache.py tests/unit/storage/test_file_cache.py
git commit -m "test: cover cache split acceptance criteria"
```
