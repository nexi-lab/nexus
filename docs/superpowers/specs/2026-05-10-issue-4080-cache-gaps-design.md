# Issue #4080 — Cache Split: Gap-Closing Follow-On

**Date:** 2026-05-10
**Issue:** [#4080](https://github.com/nexi-lab/nexus/issues/4080)
**Predecessor spec:** [2026-05-08-issue-4080-cache-split-design.md](2026-05-08-issue-4080-cache-split-design.md)
**Status:** Approved design, pending implementation plan

## Context

The original 2026-05-08 spec proposed splitting cache into `IndexCache` (TTL) and `FileCache` (fingerprint) with parent-only invalidation. Most of that work has landed:

- `src/nexus/cache/index_store.py` — `MemoryIndexCache` with `invalidate_parent_listing`
- `src/nexus/cache/file_store.py` — `MemoryFileCache` with per-key `asyncio.Lock`, fingerprint validation
- `src/nexus/cache/policy.py` — per-backend TTL defaults matching issue text
- `rust/kernel/src/cache/{file_cache,index_cache,invalidation}.rs` — kernel-side mirrors with striped fill locks
- `src/nexus/fuse/cache.py` — `FUSECacheManager` orchestration, entry-count LRU, metrics
- `S3`, `GCS`, `GitHub`, `delegating` backends implement `fingerprint()`
- Issue #4053 (foyer hybrid DRAM+NVMe) shipped via PR #4097 → `nexus-fuse/src/cache.rs`

The issue's acceptance criteria are not yet fully met. This spec covers the remaining gaps.

## Decision

Close five unmet acceptance items in a focused follow-on:

1. **Byte-size LRU** replacing entry-count LRU in Python `MemoryFileCache` and Rust kernel `FileCache`.
2. **`max_drain_bytes`** cap on streamed reads — bytes still flow to the caller, but the cache skips storing oversize entries.
3. **Per-zone TTL knobs** exposed via `nexus.yaml` → `CacheConfig.index_ttl_overrides`, threaded through `policy.index_ttl_for_backend`.
4. **Hit-rate benchmark** proving ≥90% hit rate on a Zipf re-read workload; fingerprint-cost benchmark; 100-way singleflight Python test.
5. **Singleflight lock lifecycle fix** — replace `WeakValueDictionary[FileKey, asyncio.Lock]` with a bounded `dict` + LRU eviction of unused locks, to prevent rare singleflight bypass when no strong refs exist between two waiters.

## Goals

1. Meet all remaining acceptance criteria from issue #4080 without re-litigating settled design choices.
2. Keep the `MemoryFileCache` / `MemoryIndexCache` public API stable; internals add byte accounting + tighter LRU.
3. Mirror behavior between Python `MemoryFileCache` and Rust kernel `FileCache`.
4. Expose tuning knobs through `CacheConfig` → `nexus.yaml` without breaking existing config.

## Non-goals

1. RedisIndexStore. Index TTLs are 60–600s; multi-process consistency value is small relative to operational cost. Revisit if a workload demands it.
2. Wiring into `core/nexus_fs.py`. `CacheStoreABC` on `NexusFS` is the unrelated fourth-pillar contract (tokens/OAuth/etc), not the file-byte cache. `FUSECacheManager` is the orchestrator for this work.
3. Touching `nexus-fuse/src/cache.rs`. Foyer integration shipped in #4097; the disk tier already uses S3-FIFO via foyer.
4. Smarter eviction (S3-FIFO/SIEVE) on the Python RAM tier. Foyer covers the disk tier where eviction-algorithm hit-rate gains matter most.
5. Changing the parent-only invalidation rule — already implemented and correct.

## Architecture

```
                   ┌────────────────────────────────────┐
                   │  nexus.yaml CacheConfig            │
                   │   - content_cache_bytes: 512MB     │
                   │   - parsed_cache_bytes: 64MB       │
                   │   - max_drain_bytes: 16MB          │
                   │   - index_ttl_overrides: {…}       │
                   └────────────────────────────────────┘
                                  │ (load)
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │              FUSECacheManager (Python)                    │
   │                                                          │
   │   ┌─────────────────────┐    ┌──────────────────────┐    │
   │   │ MemoryIndexCache    │    │ MemoryFileCache      │    │
   │   │ - TTL-based         │    │ - byte-size LRU      │    │
   │   │ - parent-only inval │    │ - per-key asyncio    │    │
   │   │ - per-backend TTL   │    │   Lock (singleflight)│    │
   │   │ - TTL overrides     │    │ - fingerprint check  │    │
   │   └─────────────────────┘    │ - max_drain_bytes    │    │
   │                              │   advisory           │    │
   │                              └──────────────────────┘    │
   └──────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────┐
   │   Rust kernel cache (mirror)                              │
   │   rust/kernel/src/cache/file_cache.rs                     │
   │   - byte-size LRU on FileCache                            │
   │   - striped fill locks (unchanged)                        │
   └──────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────┐
   │   nexus-fuse/src/cache.rs — UNTOUCHED                     │
   │   (foyer-backed hybrid; #4053 / PR #4097)                 │
   └──────────────────────────────────────────────────────────┘
```

## Components

### `MemoryFileCache` (modified)

```python
class MemoryFileCache:
    def __init__(
        self,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        max_drain_bytes: int = 16 * 1024 * 1024,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._entries: OrderedDict[FileKey, _FileEntry] = OrderedDict()
        self._total_bytes: int = 0
        self._max_bytes = max_bytes
        self._max_drain_bytes = max_drain_bytes
        ...

    @property
    def max_drain_bytes(self) -> int:
        return self._max_drain_bytes

    def get_sync(self, key, expected_fingerprint) -> bytes | None:
        # existing TTL + fingerprint check
        # NEW: self._entries.move_to_end(key) on hit
        ...

    def put_sync(self, key, content, fingerprint, ttl_seconds=None) -> None:
        # NEW: maintain self._total_bytes, evict via popitem(last=False)
        # NEW: if len(content) > self._max_bytes → log warning, skip put
        ...

    def _evict_until_under_cap(self) -> None:
        while self._total_bytes > self._max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._total_bytes -= len(evicted.content)
```

**Invariant:** `self._total_bytes == sum(len(e.content) for e in self._entries.values())`. Enforced under existing `_entry_lock`.

### Singleflight lock lifecycle (fix)

Replace `WeakValueDictionary[FileKey, asyncio.Lock]` with a plain bounded dict:

```python
class MemoryFileCache:
    def __init__(self, ..., max_lock_entries: int = 4096):
        self._locks: OrderedDict[FileKey, asyncio.Lock] = OrderedDict()
        self._max_lock_entries = max_lock_entries

    async def lock(self, key: FileKey) -> asyncio.Lock:
        with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
                self._evict_unused_locks()
            else:
                self._locks.move_to_end(key)
            return lock

    def _evict_unused_locks(self) -> None:
        # Walk LRU order, evict the first N unlocked entries until under cap.
        # Locked entries are skipped (never evicted while a fill is in flight),
        # which preserves singleflight correctness at the cost of temporary
        # overflow when many concurrent fills are active.
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

**Why:** with `WeakValueDictionary`, a Lock can be GC'd between two waiters' `await self._locks.get(key)` calls if no strong refs exist. The second waiter then creates a *new* Lock and bypasses singleflight. Bounded dict eliminates the race; LRU keeps memory bounded.

### `FUSECacheManager` (simplified)

Drop entry-count LRU + `_index_order`/`_file_order` dicts. Replace count knobs with byte knobs:

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
    self._file_cache = MemoryFileCache(
        max_bytes=content_cache_bytes + parsed_cache_bytes,
        max_drain_bytes=max_drain_bytes,
    )
    self._index_cache = MemoryIndexCache()
    self._ttl_overrides = dict(index_ttl_overrides or {})
```

Single combined `MemoryFileCache` covers raw + parsed entries; the existing `FileKey.namespace` field separates them within one store.

### `policy.py` (modified)

```python
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

### `CacheConfig` (new fields)

```python
@dataclass
class CacheConfig:
    # existing fields...
    content_cache_bytes: int = 512 * 1024 * 1024
    parsed_cache_bytes: int = 64 * 1024 * 1024
    max_drain_bytes: int = 16 * 1024 * 1024
    index_ttl_overrides: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_drain_bytes > (self.content_cache_bytes + self.parsed_cache_bytes):
            raise ValueError(
                "max_drain_bytes must not exceed total file cache size"
            )
```

Threaded from `nexus.yaml` → `CacheConfig` → `FUSECacheManager` constructor.

### Rust `FileCache` mirror (`rust/kernel/src/cache/file_cache.rs`)

```rust
pub struct FileCache {
    entries: RwLock<LinkedHashMap<FileCacheKey, FileEntry>>,
    total_bytes: AtomicUsize,
    max_bytes: usize,
    fill_locks: Vec<Mutex<()>>,  // unchanged
}

impl FileCache {
    pub fn with_max_bytes(max_bytes: usize) -> Self { ... }

    pub fn get(&self, key, expected_fp) -> Option<Vec<u8>> {
        // existing + to_back() on hit for LRU touch
    }

    pub fn put(&self, key, content, fp, ttl) {
        // existing + evict_until_under_cap()
    }
}
```

Use `hashlink = "0.8"` (or existing workspace crate offering `LinkedHashMap`). Eviction guarded by the single `RwLock` writer; `total_bytes` lives in `AtomicUsize` but is only mutated under the write lock.

### Caller-side `max_drain_bytes` guard

The cache exposes `max_drain_bytes` as a read-only property. The fill-site caller (`FUSECacheManager` content-fetch path) drains the backend stream once, with a single buffer that is dropped once it exceeds the cap:

```python
buf: bytearray | None = bytearray()
async for chunk in backend.stream(path):
    yield chunk                                          # forward to consumer
    if buf is not None:
        buf.extend(chunk)
        if len(buf) > file_cache.max_drain_bytes:
            buf = None                                   # stop accumulating

if buf is not None:
    await file_cache.put(key, bytes(buf), fp, ttl)
```

Cache `put` always accepts what it's given; the oversize policy lives at the fill site. Consumer always receives the full stream regardless of cache disposition.

## Data flow

### Read path (cache hit)

```
client.cat("/s3/bucket/foo.parquet")
  → FUSECacheManager.get_content(path)
    → key = FileKey(backend="path_s3", scope, path, "raw")
    → fp_expected = backend.fingerprint(path)        # cheap; uses cached stat
    → MemoryFileCache.get_sync(key, fp_expected)
      → entry hit, fingerprint matches, not expired
      → entries.move_to_end(key)                     # LRU touch
    → return bytes
```

### Read path (cache miss, N-way concurrent)

```
N coroutines call get_content same path concurrently
  → each acquires MemoryFileCache.lock(key)
    → first coroutine inside lock:
        - get → None
        - stream from backend, accumulate bytes
        - len(buf) > max_drain_bytes → skip_cache = True
        - file_cache.put(key, bytes, fp, ttl) if not skip_cache
        - total_bytes += len; evict LRU while > max_bytes
    → remaining N-1 coroutines: re-check get; either hit (cached)
      or refetch (skipped due to oversize)
```

### Write path

```
client.write("/s3/bucket/foo/new.txt", data)
  → backend.write(...)
  → FUSECacheManager.invalidate_path("/s3/bucket/foo/new.txt")
    → MemoryFileCache.invalidate_path_sync(path)
    → MemoryIndexCache.invalidate_parent_listing(backend, scope, path)
  → /s3/bucket/'s listing untouched
```

### TTL config resolution

```
On FUSECacheManager construction:
  policy.index_ttl_for_backend(backend_id, self._ttl_overrides)
    1. self._ttl_overrides[backend_id]           # NEW (from nexus.yaml)
    2. INDEX_TTL_BY_BACKEND[backend_id]          # existing default
    3. 60s fallback
```

## Edge cases

| Case | Handling |
|---|---|
| Entry equal to `max_bytes` | Allowed. Replaces all others; sole entry. |
| Entry larger than `max_bytes` | Log warning, skip put. Caller should already have applied `max_drain_bytes`. |
| TTL expiry during eviction | Lazy expiry on `get` is sufficient; eviction does not inspect TTL. |
| `max_drain_bytes > max_bytes` | Misconfig. `CacheConfig.__post_init__` raises `ValueError`. |
| Rename across dirs | Caller must issue invalidation for old + new parent. `MemoryIndexCache` does not infer. Documented in helper docstring. |
| Trailing-slash path (`/a/b/`) on invalidation | `PurePosixPath("/a/b/").parent == PurePosixPath("/a")`. Test required. |
| Fingerprint returns `None` | `MemoryFileCache.put` requires `ttl_seconds is not None`; falls back to TTL on `get`. Already supported. |
| Backend `fingerprint` raises | Treat as `None`; log warning once per backend. |

## Concurrency invariants

| Scenario | Guarantee | Mechanism |
|---|---|---|
| N concurrent `get_content` same key, cold | Exactly 1 backend fetch | Per-key `asyncio.Lock` (bounded dict + LRU) |
| Concurrent `put` + `get` | No torn reads | `_entry_lock` (RLock) around dict ops |
| Concurrent `put` racing eviction | `total_bytes` consistent | Eviction inside `_entry_lock` |
| Lock-table eviction races a held lock | Held lock never evicted | `candidate_lock.locked()` check |

## Metrics

Add to `FUSECacheManager._metrics`:

- `content_bytes` — current `total_bytes`
- `content_evictions` — LRU evictions count
- `content_skipped_oversize` — puts skipped due to `> max_bytes`
- `singleflight_waiters_hit` — waiters that found cache filled after lock acquire

Exposed via existing `get_metrics()` accessor. Used by hit-rate bench to verify.

## Testing

### Unit (Python)

**`tests/unit/cache/test_file_store.py`** (extend):
- `test_byte_size_cap_evicts_lru`
- `test_get_touches_lru`
- `test_total_bytes_invariant_after_overwrite`
- `test_oversize_entry_rejected`
- `test_invalidate_decrements_total_bytes`
- `test_singleflight_100_concurrent_cold_gets`
- `test_lock_dictionary_bounds`

**`tests/unit/cache/test_index_store.py`** (extend):
- `test_parent_only_invalidation_root`
- `test_parent_only_invalidation_trailing_slash`
- `test_rename_invalidates_both_dirs` (documents caller contract)
- `test_ttl_override_takes_precedence`

**`tests/unit/cache/test_policy.py`** (new):
- `test_index_ttl_with_override`
- `test_index_ttl_unknown_backend_fallback`
- `test_negative_ttl_respects_override`

**`tests/unit/cache/test_cache_manager_byte_lru.py`** (new):
- `test_max_drain_bytes_skips_cache`
- `test_max_drain_bytes_le_total_enforced`

### Unit (Rust)

**`rust/kernel/src/cache/file_cache.rs`** (extend `mod tests`):
- `test_byte_size_cap_evicts_lru`
- `test_get_touches_lru`
- `test_total_bytes_invariant`
- Existing tests retained

### Bench

**`benches/cache_hit_rate.py`:**
- Workload: 1000 distinct files, Zipf(α=1.0) re-read distribution, 10K total ops
- Backend: synthetic 1ms-latency in-memory backend
- Cache: `MemoryFileCache(max_bytes=128MB)`, file sizes 1KB–1MB (working set ≪ cap)
- Assert: aggregate hit rate ≥ 90% after a 500-op warmup
- Assert: 100-way singleflight produces exactly 1 fetch per cold key (subtest)

**`benches/cache_fingerprint_cost.py`:**
- 1000 cached files, all hot, 10K `get_content` calls
- Measure: fingerprint time / total `get_content` time
- Assert: < 10% for S3, GitHub, GCS (else fall back to TTL policy per issue Risks)

### Integration

**`tests/integration/test_cache_parent_only_invalidation.py`:**
- Real `path_local` backend wired through `FUSECacheManager`
- list `/a/` then `/a/b/` → both cached
- write `/a/b/new.txt`
- assert `/a/b/` listing miss; `/a/` listing hit

## Acceptance criterion mapping

| Issue criterion | Covered by |
|---|---|
| `IndexCache` + `FileCache` traits with RAM impls | Already shipped; this work preserves API |
| Per-key async lock prevents N-way refetch storm | `test_singleflight_100_concurrent_cold_gets` (Python) + existing Rust test |
| Parent-only invalidation verified | `test_parent_only_invalidation_*` + integration |
| ≥3 backends implement `fingerprint(path)` | Already shipped (S3, GCS, GitHub, delegating) |
| Bench: 90%+ hit rate | `benches/cache_hit_rate.py` |

## Rollout

Single-cycle behavior cutover:

- `CacheConfig` gains four new fields with safe defaults; existing public knobs (`attr_cache_ttl`, etc.) remain compatible.
- `FUSECacheManager`'s entry-count `_index_order` / `_file_order` dicts are removed in the same change that introduces byte-size LRU. Public methods unchanged.
- Rust kernel `FileCache` gets matching byte-size LRU; signature on `FileCache::default()` retained, `FileCache::with_max_bytes(usize)` added.
- `nexus.yaml` documentation updated with the new knobs.
- No migration step required; in-memory caches reset on process restart.

## Open questions

None. All design choices ratified during 2026-05-10 brainstorming session.
