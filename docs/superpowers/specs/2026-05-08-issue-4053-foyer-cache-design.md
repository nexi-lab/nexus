# Issue #4053 - Foyer Hybrid Cache For nexus-fuse Design

**Date**: 2026-05-08
**Issue**: [#4053](https://github.com/nexi-lab/nexus/issues/4053) - Replace `nexus-fuse` SQLite cache with foyer hybrid (DRAM+NVMe)

## Context

`nexus-fuse` currently caches file content through `nexus-fuse/src/cache.rs`, where `FileCache` stores content, ETags, and timestamps in SQLite. `nexus-fuse/src/fs.rs` already treats the cache as an optional synchronous optimization through `CacheLookup`, `put`, `touch`, and `invalidate`. The read path preserves server consistency by using ETag revalidation: fresh entries return immediately, stale entries with an ETag trigger `If-None-Match`, and a `304 Not Modified` refreshes the timestamp.

Issue #4053 asks to replace SQLite with [foyer](https://github.com/foyer-rs/foyer), keeping ETag behavior while adding a DRAM tier, an NVMe tier, and flash-write admission control. Foyer's hybrid cache API is async, while the current FUSE callbacks and cache boundary are synchronous.

## Decision

Replace the SQLite implementation behind `FileCache` with a foyer hybrid cache while preserving the public `FileCache` surface consumed by `NexusFs`:

- `get(path) -> CacheLookup`
- `get_etag(path) -> Option<String>`
- `put(path, content, etag)`
- `touch(path)`
- `invalidate(path)`
- `stats() -> CacheStats`

`FileCache` will own an internal Tokio runtime used only to bridge the existing synchronous API to foyer's async operations. This keeps the change localized to `cache.rs`, cache initialization in `main.rs`, small log/comment updates in `fs.rs`, documentation, and benchmarks. It avoids converting FUSE callbacks or `NexusClient` reads to async as part of this issue.

## Non-Goals

1. Rewriting the FUSE read/write path to async.
2. Keeping SQLite as a production fallback cache.
3. Persisting exact SQLite-style row counts across process restarts.
4. Introducing a new FUSE integration harness.
5. Changing the server-side read, write, or ETag APIs.

## Cache Layout

The cache root remains under:

```text
<user-cache-dir>/nexus-fuse/
```

Each Nexus server URL maps to a stable hash. The old SQLite file path for a server remains:

```text
nexus_<hash>.db
```

The new foyer storage path is:

```text
nexus_<hash>.foyer/
```

On startup, `FileCache::new` creates the foyer directory and attempts to delete the matching old SQLite database file. SQLite deletion is the migration path. If deletion fails, the mount logs a warning and continues with the foyer cache.

## Cache Records

Foyer values store a serialized file record:

```rust
struct CacheRecord {
    content: Vec<u8>,
    etag: Option<String>,
    cached_at_secs: u64,
}
```

The cache key is the Nexus path string. The value is encoded with `bincode` so foyer stores one opaque byte value per path.

The existing `MAX_CACHE_AGE_SECS` freshness policy remains one hour. The existing max-file-size guard remains at 10 MiB: content above that size bypasses `put` and is fetched directly from Nexus.

## Foyer Configuration

Defaults:

- DRAM tier: 256 MiB
- NVMe/filesystem tier: 10 GiB
- Per-file cacheability limit: 10 MiB
- Freshness age: 1 hour

Configuration is explicit on the CLI and environment:

```text
--cache-memory-mb / NEXUS_FUSE_CACHE_MEMORY_MB
--cache-disk-gb / NEXUS_FUSE_CACHE_DISK_GB
--cache-dir / NEXUS_FUSE_CACHE_DIR
```

The mount command uses these values when creating `FileCache`. The daemon command also receives equivalent cache configuration so Python-backed Rust daemon usage gets the same behavior.

Invalid tier sizes fail before mounting. A zero value disables that tier only if foyer supports the resulting configuration cleanly; otherwise zero is rejected with a clear error. Cache initialization failure remains best-effort: log the error and continue without a file cache.

## Admission And Eviction

The DRAM tier uses foyer's in-memory eviction configuration. The disk tier uses foyer's filesystem device capacity setting and enables foyer's storage admission/filtering before flash writes. The implementation will use foyer's hybrid cache builder, memory configuration, filesystem storage device, and block-engine admission filter APIs in the current foyer release.

The design does not duplicate foyer's admission logic in `nexus-fuse`. `nexus-fuse` only applies semantic cacheability checks: path keying, max file size, ETag/timestamp record data, and explicit invalidation.

## Read Data Flow

`NexusFs::read_cached(path)` keeps the same behavior:

1. Ask `FileCache::get(path)`.
2. If the cache returns `Hit(entry)`, return cached content and ETag.
3. If the cache returns `NeedsRevalidation { etag }`, call `NexusClient::read_with_etag(path, Some(&etag))`.
4. On `ReadResponse::NotModified`, call `FileCache::touch(path)`, then fetch the cached record and return it.
5. On `ReadResponse::Content { content, etag }`, call `FileCache::put(path, &content, etag.as_deref())` and return the server content.
6. On revalidation error, try to return stale cached content. If the stale record is unavailable, return the server error.
7. On `Miss`, call `read_with_etag(path, None)`, store successful content through `put`, and return it.

`CacheLookup::NeedsRevalidation` must not deserialize the content bytes just to decide freshness. If foyer requires reading the record as one value, `FileCache` may keep a small in-memory metadata map keyed by path to answer stale ETag checks without forcing content copies on every stale lookup.

## Invalidation Data Flow

`NexusFs::invalidate_path(path)` continues to call `FileCache::invalidate(path)` after writes, creates, deletes, truncates, and renames. `invalidate` removes the foyer entry and any in-memory metadata for that path. Rename continues invalidating both the old and new paths.

Directory and attribute LRU caches remain unchanged.

## Stats

`CacheStats` remains available for startup logs and tests:

```rust
pub struct CacheStats {
    pub file_count: u64,
    pub total_size: u64,
}
```

Because foyer is the cache owner, `nexus-fuse` will maintain lightweight in-process metadata for inserted records and invalidations. Stats are exact for entries inserted during the current process and best-effort after restart. Startup logging will make this clear by avoiding wording that implies SQLite-style persistent row accounting.

## Error Handling

Cache initialization is best-effort. If the cache directory cannot be created, foyer rejects the configuration, or the runtime cannot start, `main.rs` logs the error and mounts without a file cache.

Runtime cache errors do not fail FUSE operations:

- `get` logs foyer read/deserialization errors and returns `Miss`.
- `put`, `touch`, and `invalidate` log failures and return `()`.
- stale fallback after revalidation failure still attempts to read cached content before surfacing the server error.
- old SQLite migration deletion logs warnings on failure and does not block mount startup.

Production cache code must avoid `unwrap` on runtime, filesystem, or foyer operations. Poisoned lock handling should degrade cache behavior where practical instead of crashing the mount.

## Tests

The existing cache tests move from SQLite-specific internals to public behavior:

- empty cache returns `Miss`
- `put` followed by `get` returns bytes and ETag
- `put` overwrites an existing path
- `get_etag` returns the stored ETag
- `touch` refreshes an entry
- stale entry with an ETag returns `NeedsRevalidation`
- stale entry without an ETag returns `Miss`
- files above the max-file-size guard are not cached
- file exactly at the max-file-size guard is cached
- `invalidate` removes one path and is a no-op for missing paths
- binary and empty content round trip exactly
- concurrent access does not panic
- stats update for current-process puts and invalidations
- old SQLite cache file is deleted during startup migration
- cache configuration defaults and invalid-size parsing are covered

If the existing codebase has a practical way to instantiate `NexusFs` with a mock client, add targeted read-path tests for `304 Not Modified`, `200 Content`, and stale fallback. If it does not, keep this issue focused by testing the cache boundary and avoiding a new FUSE harness.

## Benchmark

Add a cache-focused Criterion benchmark under `nexus-fuse/benches` that exercises:

- warm 1 KiB, 10 KiB, 100 KiB, and 1 MiB reads from foyer
- churn across more keys than the DRAM tier can retain
- mixed hit/miss workload with ETag-shaped records

For the acceptance comparison, keep a minimal benchmark-only SQLite baseline that mirrors the old `FileCache` hot operations. SQLite must not remain in production cache code, but it may remain as a `dev-dependency` or benchmark-only helper until the acceptance benchmark is recorded. Compare foyer warm reads and churn against that SQLite baseline and document at least one passing criterion: 2x hit-rate on an agent-shaped churn trace or 30% p99 read-latency reduction.

The benchmark result and command will be recorded in `nexus-fuse/PERFORMANCE_RESULTS.md`, including machine, OS, Rust version, foyer version, tier sizes, SQLite baseline details, and the measured hit-rate or p99 latency delta.

## Documentation

Update `nexus-fuse/ARCHITECTURE.md` to replace SQLite cache references with foyer hybrid cache references. Update `nexus-fuse/PERFORMANCE_RESULTS.md` with the benchmark command, result, and migration note:

```text
Existing SQLite cache files under <cache-dir>/nexus-fuse/nexus_<hash>.db are dropped on upgrade.
New cache content is stored under <cache-dir>/nexus-fuse/nexus_<hash>.foyer/.
```

## Acceptance Mapping

- foyer wired into nexus-fuse hot path: `FileCache` becomes foyer-backed while `NexusFs::read_cached` continues using it.
- DRAM tier configurable, default 256 MiB: CLI/env config passed to `FileCache`.
- NVMe tier configurable, default 10 GiB: CLI/env config passed to foyer filesystem device capacity.
- ETag revalidation preserved: `CacheLookup`, `touch`, and read flow remain semantically unchanged.
- Admission filter active before flash writes: foyer storage admission/filtering enabled in the disk tier.
- Benchmark evidence: Criterion benchmark plus documented p99 latency or hit-rate result.
- Migration documented: startup deletes the old SQLite file and docs explain the new foyer directory.
