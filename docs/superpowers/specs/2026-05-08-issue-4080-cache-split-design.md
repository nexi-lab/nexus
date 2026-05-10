# Issue #4080 - Split Index/File Cache Design

**Date:** 2026-05-08
**Issue:** [#4080](https://github.com/nexi-lab/nexus/issues/4080) - cache: split into TTL'd index cache and fingerprint'd file cache with parent-only invalidation
**Status:** Approved design, pending implementation plan

## Context

Issue #4080 proposes a logical cache split:

- `Index cache` for listings and stat-like metadata with TTL-based freshness.
- `File cache` for file bytes with fingerprint-based freshness and per-key single-flight fills.

The current tree already has multiple cache mechanisms, but they are organized by implementation layer instead of consistency model:

- `src/nexus/fuse/cache.py` provides an attribute TTL cache, a content LRU, and a parsed-content LRU.
- `src/nexus/fuse/operations.py` also carries a separate `dir_cache` TTL cache for `readdir`.
- `src/nexus/storage/local_disk_cache.py` provides a persistent SSD-backed file-content cache keyed by content hash.
- `src/nexus/storage/file_cache.py` provides a path-addressed on-disk content cache with sidecar metadata.
- `rust/kernel/src/abc/cache_store.rs` defines a generic KV + PubSub cache pillar, but not a logical file/index cache API.

This means the Python/FUSE path, the Python storage path, and the Rust kernel path all cache different shapes of data with different invalidation rules. The issue sketch also refers to `bricks/cache/`, but the current repo organizes the relevant code under `src/nexus/cache`, `src/nexus/storage`, `src/nexus/fuse`, and `rust/kernel/src`.

## Decision

Land an atomic Python + Rust cutover with **public behavior stable, internals free to change**.

The new cache architecture has exactly two logical cache roles across Python and Rust:

1. `IndexCache` for directory listings, `stat/getattr`, and related negative metadata lookups.
2. `FileCache` for file bytes, parsed-file derivatives, optional block/range entries, and validation metadata.

Physical stores remain implementation details behind those two roles. Existing FUSE RAM caches, `LocalDiskCache`, `FileContentCache`, and future Rust physical stores all become backends or adapters behind the logical `IndexCache` or `FileCache` APIs.

The Rust `CacheStore` pillar remains the cross-process KV + PubSub transport, not the public cache-policy API.

## Goals

1. Unify Python/FUSE and Rust kernel cache behavior around the same file-vs-index split.
2. Use TTL-based freshness for index data and fingerprint-based freshness for file bytes.
3. Invalidate only the mutated path and its immediate parent listing by default.
4. Prevent thundering-herd refetches with per-key single-flight fill locks.
5. Keep current public filesystem behavior and current top-level mount/cache config knobs usable.
6. Keep the design compatible with later physical-store work such as foyer without making foyer a prerequisite for the logical split.

## Non-goals

1. Preserving existing internal cache classes as first-class architecture boundaries.
2. Recursive invalidation of transitive parents on ordinary file mutation.
3. Making cache availability part of correctness; cache failures degrade performance, not core reads and writes.
4. Redesigning all cache-related user config keys in the same change.
5. Requiring a full foyer/NVMe implementation before the logical split can land.

## Logical Model

### IndexCache

`IndexCache` owns:

- `stat/getattr` metadata
- `readdir/listing` results
- negative metadata entries such as "path not found" or "directory empty" facts

`IndexCache` entries are keyed by backend identity, scope identity, normalized path, and operation kind. Freshness is TTL-based because most backends do not expose a cheap directory fingerprint.

### FileCache

`FileCache` owns:

- raw file bytes
- optional range/block entries used by read-ahead or large reads
- parsed-file derivatives such as view-specific content keyed by source fingerprint
- validation metadata such as the last observed fingerprint and fallback TTL

`FileCache` entries are keyed by backend identity, scope identity, normalized file path, and optional derivative namespace such as a parsed view type.

### Parsed views

Parsed views are not a third cache role. They are `FileCache` derivatives keyed by:

- the source file path
- the parsed view namespace, such as `txt` or `md`
- the source file fingerprint

This keeps parsed content aligned with file-byte invalidation rules instead of letting it drift behind a separate cache policy.

## Contracts

Python and Rust should mirror the same logical shape. The exact language details can differ, but the concepts must stay aligned.

### Python contract sketch

```python
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class IndexKey:
    backend_id: str
    scope_id: str
    path: str
    kind: Literal["stat", "listing", "negative"]


@dataclass(frozen=True)
class FileKey:
    backend_id: str
    scope_id: str
    path: str
    namespace: str = "raw"


class IndexCache(Protocol):
    def get(self, key: IndexKey) -> Any | None: ...
    def put(self, key: IndexKey, value: Any, ttl_seconds: int) -> None: ...
    def invalidate_path(self, key: IndexKey) -> None: ...
    def invalidate_parent_listing(self, backend_id: str, scope_id: str, path: str) -> None: ...


class FileCache(Protocol):
    async def get(
        self,
        key: FileKey,
        expected_fingerprint: str | None,
    ) -> bytes | None: ...
    async def put(
        self,
        key: FileKey,
        content: bytes,
        fingerprint: str | None,
        ttl_seconds: int | None = None,
        complete: bool = True,
    ) -> None: ...
    async def invalidate(self, key: FileKey) -> None: ...
    def lock(self, key: FileKey): ...
```

Fingerprints are opaque `str` values rather than typed backend-specific objects. This matches the existing Python code better than a bytes-only API and keeps serialization simple for Rust/PubSub integration.

### Rust contract sketch

Rust should add a logical cache module under `rust/kernel/src/cache/` with:

- `index_cache.rs` for TTL index entries
- `file_cache.rs` for validated file entries and single-flight fill state
- `invalidation.rs` for shared invalidation message shapes

Those logical caches may use the existing `CacheStore` trait for transport or persistence, but they should not collapse back into generic KV APIs at call sites.

## Identity, Keys, And Policy

### Backend identity

Every cache key must include a stable backend identity. At minimum that identity includes:

- backend name, such as `path_s3`, `path_gcs`, or `github_connector`
- mounted bucket/prefix or connector-specific source identity
- zone/user scope when the backend is user-scoped or zone-scoped

Two different users or zones must never share file or index entries by accident.

### Path normalization

Both Python and Rust must normalize paths before key construction:

- absolute virtual path
- no trailing slash except `/`
- resolved view path for parsed derivatives only after the source file path is normalized

### TTL defaults

Issue #4080's backend-oriented defaults become the policy baseline:

- in-memory/local backend index TTL: `0s`
- local persistent disk-backed index TTL: `60s`
- S3/GCS/R2 index TTL: `600s`
- GitHub index TTL: `600s`

Negative index entries use a shorter TTL of `5s`, capped so they never exceed the positive TTL for that backend.

### Public config compatibility

Existing public knobs remain usable:

- FUSE `attr_cache_ttl` and `dir_cache_ttl` remain accepted and act as compatibility overrides or upper bounds for `IndexCache`.
- existing content-cache size knobs continue to size the RAM `FileCache` tier.
- existing local-disk-cache knobs continue to size the persistent `FileCache` tier.

The public meaning stays stable even though the internal implementation becomes logical `IndexCache` and `FileCache` adapters instead of separate ad hoc caches.

### File TTL fallback

Backends without cheap fingerprints fall back to a file-cache TTL policy instead of validation-on-read. The default fallback file TTL is `60s` unless a backend-specific override is provided.

## Data Flow

### Index reads

`stat/getattr` and `readdir/listing` compute an `IndexKey` from backend identity, scope, normalized path, and operation kind.

1. On cache hit, return the value if its TTL is still valid.
2. On cache miss, fetch from the kernel/backend path.
3. Store the positive or negative result in `IndexCache`.
4. Return the fetched value.

Negative results are cacheable, but only with the short negative TTL.

### File reads

The read path first computes a `FileKey`, then resolves the cheapest expected fingerprint already available on the path:

- cached or fresh stat metadata when present
- backend metadata lookup when needed
- `None` when the backend cannot provide a cheap fingerprint

Read behavior:

1. Call `FileCache.get(key, expected_fingerprint)`.
2. If the cached entry fingerprint matches, return the bytes.
3. If the backend is in TTL fallback mode and the file entry has not expired, return the bytes.
4. Otherwise acquire the per-key single-flight lock.
5. Re-check the cache after the lock is acquired.
6. Fetch from origin only if still cold or stale.
7. Publish a complete cache entry only after the fetch succeeds or the block/range fill is marked complete for its own namespace.

### Large reads and streaming

`FileCache` may support block or range subentries for large-file hot paths, but those are internal `FileCache` strategies, not a third cache role. A large read must never buffer unbounded content purely to make it cacheable.

If caching is capped mid-stream:

- no whole-file hit record is published, or
- only an explicitly partial block/range record is published

### Mutations

The canonical invalidation rules are:

- file write/create/unlink: invalidate the file path entry and the immediate parent listing entry
- `mkdir/rmdir`: invalidate the directory path entry and the immediate parent listing entry
- rename: invalidate old path entry, new path entry, old parent listing entry, and new parent listing entry

No ordinary file mutation recursively invalidates `/a/` when `/a/b/c.txt` changes. Parent-only invalidation is the default rule.

### Cross-process invalidation

Python and Rust must publish the same invalidation message shape. The message needs only the logical facts:

- file path changed
- parent listing changed
- namespace such as raw bytes vs parsed derivative when relevant

The existing `CacheStore` PubSub transport is the delivery mechanism for those messages, not the cache semantics.

## Backend Fingerprints

`src/nexus/backends/base/backend.py` should grow an explicit cheap fingerprint API, for example:

```python
def fingerprint(self, path: str, context: OperationContext | None = None) -> str | None:
    ...
```

Returning `None` means "no cheap fingerprint available; use file TTL fallback".

### First-wave backend implementations

#### S3

Implement in `src/nexus/backends/storage/path_s3.py`.

- preferred fingerprint: `VersionId`
- fallback fingerprint when versioning is disabled: `etag:<etag>`

This matches the current `get_file_info()` behavior.

#### GCS

Implement in `src/nexus/backends/storage/path_gcs.py`.

- fingerprint: object generation as a string

This matches the current `get_version()` and `get_file_info()` behavior.

#### GitHub

Implement in `src/nexus/backends/connectors/github/connector.py`, likely with a small extension to `src/nexus/backends/base/cli_backend.py`.

Unlike S3 and GCS, the GitHub connector is CLI-backed today and does not already expose a generic file-version seam. The cutover therefore includes a small metadata protocol addition so GitHub-backed file paths can surface a blob SHA or equivalent file fingerprint without forcing the file cache into TTL-only mode.

If a particular GitHub connector path cannot produce a stable blob SHA cheaply, that path class falls back to the file TTL policy until the metadata hook is extended.

## Integration Boundaries

### Python

Add logical cache modules under `src/nexus/cache/`:

- `src/nexus/cache/index_store.py`
- `src/nexus/cache/file_store.py`
- `src/nexus/cache/invalidation.py`
- `src/nexus/cache/policy.py`

Adapt existing components instead of letting them remain policy owners:

- `src/nexus/fuse/cache.py`
  - fold attribute cache into `IndexCache`
  - fold content and parsed caches into `FileCache`
- `src/nexus/fuse/operations.py`
  - construct logical caches instead of assembling ad hoc cache pieces
  - replace the separate `dir_cache` assembly with an `IndexCache` adapter
- `src/nexus/fuse/ops/metadata_handler.py`
  - use `IndexCache` for `getattr` and `readdir`
- `src/nexus/fuse/ops/io_handler.py`
  - use `FileCache` for read fills and write invalidation
- `src/nexus/fuse/ops/mutation_handler.py`
  - switch from manual cache clearing to canonical parent-only invalidation helpers
- `src/nexus/fuse/ops/_shared.py`
  - hold the shared invalidation and local-disk adapter logic
- `src/nexus/storage/local_disk_cache.py`
  - become a persistent `FileCache` tier, not an independent cache policy
- `src/nexus/storage/file_cache.py`
  - either become a path-addressed `FileCache` implementation or be retired behind the new logical API
- `src/nexus/fuse/readahead.py`
  - remain an optimization layer that warms `FileCache`

### Rust

Add new logical cache modules under `rust/kernel/src/cache/`:

- `mod.rs`
- `index_cache.rs`
- `file_cache.rs`
- `invalidation.rs`

Wire them into the kernel hot paths:

- `sys_read` and related file-byte paths use `FileCache`
- `readdir` and stat-adjacent metadata paths use `IndexCache`
- mutation paths publish the canonical invalidation messages

`rust/kernel/src/abc/cache_store.rs` remains the physical cache pillar and invalidation transport.

## Error Handling

- Cache unavailability degrades to backend or kernel reads, not user-visible read failures.
- A failed fingerprint lookup on a backend that normally supports fingerprints bypasses the file-cache hit and reads from origin.
- A cache entry is never treated as validated if validation was required but unavailable.
- Partial or capped stream fills do not publish a complete whole-file hit record.
- The mutating process clears its own local cache state synchronously before returning success, even if cross-process invalidation publish fails.
- Cross-process invalidation publish failures are observable and should be retried where practical, but they do not roll back a completed write.

## Testing

### Contract tests

Shared logical behavior tests for Python and Rust semantics:

1. index TTL expiry for positive entries
2. index TTL expiry for negative entries
3. file fingerprint match returns cached bytes
4. file fingerprint mismatch bypasses stale bytes
5. file TTL fallback works when fingerprint is unavailable

### Mutation tests

1. writing `/a/b/c.txt` invalidates `/a/b/` listing but not `/a/`
2. `mkdir` and `rmdir` invalidate the immediate parent listing and the directory path entry
3. rename invalidates old path, new path, old parent listing, and new parent listing

### Concurrency tests

1. `100` concurrent cold reads of the same file produce exactly one backend fetch
2. concurrent cold reads of different keys do not serialize globally
3. lock re-check prevents double fills when a waiter wakes after a completed fill

### Integration tests

1. FUSE `getattr` and `readdir` keep current public behavior while using `IndexCache`
2. FUSE reads and readahead keep current public behavior while using RAM + local-disk `FileCache` tiers
3. Rust and Python invalidation events converge on the same visible state after writes
4. parsed views are invalidated when the source file fingerprint changes
5. S3, GCS, and GitHub-backed file paths all exercise the fingerprint seam or explicitly hit the file TTL fallback path where documented

## Rollout Shape

The cutover is atomic at the behavior layer:

- Python/FUSE and Rust both adopt the same logical split in one implementation cycle.
- Existing public behavior and config knobs stay stable.
- Existing internal cache classes lose ownership of consistency policy and become implementations or adapters.

The physical storage details remain replaceable after the cutover. A later foyer-backed Rust file-cache store or additional persistent tiers can slot in behind `FileCache` without changing the logical policy or the invalidation model defined here.
