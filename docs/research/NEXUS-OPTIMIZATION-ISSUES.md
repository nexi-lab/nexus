# NEXUS PERFORMANCE OPTIMIZATION ISSUES

## Comprehensive Issue List with Implementation Guide

**Research Sources:** SeaweedFS, JuiceFS, SpiceDB, Google Zanzibar, 100+ GitHub Issues
**Date:** December 26, 2025
**Branch:** `claude/research-seaweedfs-0rFsy`

---

## EXECUTIVE SUMMARY

| Priority | Issues | Expected Impact | Timeline |
|----------|--------|-----------------|----------|
| **P0 Critical** | 5 issues | 10-100x improvement | Week 1-2 |
| **P1 High** | 6 issues | 5-50x improvement | Week 3-6 |
| **P2 Medium** | 5 issues | 2-10x improvement | Week 7-12 |
| **P3 Future** | 4 issues | Architecture evolution | Later |

### Current Rust Implementation Status

| Already in Rust ✅ | Performance |
|-------------------|-------------|
| String interning | 4x memory reduction |
| Graph caching (#862) | 90% rebuild reduction |
| Bulk permission checks | 85x speedup |
| SIMD grep/UTF-8 | 50-100x speedup |
| BLAKE3 hashing | 10x faster |
| Memory-mapped I/O | Zero-copy reads |

---

# P0 CRITICAL ISSUES (Week 1-2)

## Issue #P0-1: Timestamp Quantization for Permission Cache

**Title:** `perf: Implement timestamp quantization for 10-100x cache hit improvement`

**Priority:** P0 - Critical
**Effort:** Low (1-2 days)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - cache keys are Python-layer |
| **Python** | ✅ Implement in `rebac_cache.py` |

### Problem
Cache keys include precise timestamps → cache misses for identical checks milliseconds apart.

### Solution
```python
# File: src/nexus/core/rebac_cache.py

QUANTIZATION_INTERVAL = 5  # seconds (from SpiceDB research)

def _get_cache_key(self, subject, permission, object, tenant_id):
    quantum = int(time.time() // QUANTIZATION_INTERVAL) * QUANTIZATION_INTERVAL
    return f"{tenant_id}:{subject}:{permission}:{object}:q{quantum}"
```

### Expected Impact
- Same-tenant cache hit: 70% → 95%+
- Permission check latency: 10-100ms → <1ms (cached)

### Research Source
- SpiceDB: 5-second quantum windows achieve 60%+ hit rates
- SeaweedFS Issue #2325: Default settings beat "tuned" settings (45x improvement)

---

## Issue #P0-2: Request Deduplication for Concurrent Checks

**Title:** `perf: Implement request deduplication to prevent thundering herd`

**Priority:** P0 - Critical
**Effort:** Medium (2-3 days)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - DashMap available for future optimization |
| **Python** | ✅ Implement first in `rebac_manager.py` |

### Problem
100 concurrent requests for same file = 100 separate permission checks.

### Solution
```python
# File: src/nexus/core/rebac_manager.py

class PermissionDeduplicator:
    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def check_deduplicated(self, key: str, compute_fn):
        async with self._lock:
            if key in self._in_flight:
                return await self._in_flight[key]  # Wait for existing

            future = asyncio.get_event_loop().create_future()
            self._in_flight[key] = future

        try:
            result = await compute_fn()
            future.set_result(result)
            return result
        finally:
            async with self._lock:
                del self._in_flight[key]
```

### Optional Rust Enhancement
```rust
// File: rust/nexus_fast/src/lib.rs
// Add after line 164

use dashmap::DashMap;

/// In-flight request tracker using lock-free DashMap
static IN_FLIGHT: Lazy<DashMap<InternedMemoKey, Arc<Mutex<Option<bool>>>>> =
    Lazy::new(DashMap::new);
```

### Expected Impact
- Concurrent access: 10-50x fewer computations
- Database load during spikes: Reduced by 90%

### Research Source
- SpiceDB: Lock table pattern reduces computations by 40%
- JuiceFS Issue #132: Always cancel background operations properly

---

## Issue #P0-3: Tiger Cache for Single-File Operations

**Title:** `perf: Extend Tiger Cache to single-file permission checks`

**Priority:** P0 - Critical
**Effort:** Medium (3-5 days)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - Tiger Cache uses Python Roaring Bitmaps |
| **Python** | ✅ Extend `tiger_cache.py` and `nexus_fs_core.py` |

### Problem
Tiger Cache only used for `filter_list()`, single reads still do slow ReBAC traversal.
Permission checks = 60% of read latency.

### Solution
```python
# File: src/nexus/core/nexus_fs_core.py

async def _check_permission_fast(self, path, permission, context):
    # 1. Try Tiger Cache first (O(1) bitmap lookup)
    tiger_result = self._tiger_cache.check_cached(
        context.subject, permission, path, context.tenant_id
    )
    if tiger_result is not None:
        return tiger_result  # <1ms

    # 2. Fall back to ReBAC with cache population
    result = await self._rebac.check(context.subject, permission, path, context.tenant_id)

    # 3. Update Tiger Cache for future single-file checks
    await self._tiger_cache.update_single(
        context.subject, permission, path, context.tenant_id, result
    )

    return result
```

### Expected Impact
- Single-file read permission: 10-100ms → <1ms (after warm-up)
- Read latency: 15-20ms → 5-8ms

### Research Source
- Nexus Issue #682: Tiger Cache provides 10-100x speedup for listings
- Current gap: Not used for individual file checks

---

## Issue #P0-4: Expose Bloom Filter from Rust

**Title:** `feat: Expose Rust Bloom filter for fast cache miss detection`

**Priority:** P0 - Critical
**Effort:** Low (1 day)
**Risk:** None

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Required - expose existing bloomfilter crate |
| **Python** | ✅ Use exposed Bloom filter in cache layers |

### Problem
Bloom filter is imported in Rust (`line 4: use bloomfilter::Bloom`) but NOT exposed to Python.

### Rust Solution
```rust
// File: rust/nexus_fast/src/lib.rs
// Add after line 2000 (with other pyfunctions)

#[pyclass]
struct BloomWrapper {
    inner: Bloom<String>,
}

#[pymethods]
impl BloomWrapper {
    #[new]
    fn new(capacity: usize, fp_rate: f64) -> Self {
        BloomWrapper {
            inner: Bloom::new_for_fp_rate(capacity, fp_rate),
        }
    }

    fn check(&self, key: &str) -> bool {
        self.inner.check(&key.to_string())
    }

    fn add(&mut self, key: &str) {
        self.inner.set(&key.to_string())
    }

    fn clear(&mut self) {
        self.inner.clear()
    }
}

// Add to #[pymodule] fn nexus_fast
m.add_class::<BloomWrapper>()?;
```

### Python Usage
```python
# File: src/nexus/core/rebac_cache.py

from nexus._nexus_fast import BloomWrapper

class ReBACPermissionCache:
    def __init__(self):
        # Bloom filter for fast "definitely not cached" checks
        self._bloom = BloomWrapper(100_000, 0.01)  # 1% false positive

    def get(self, key):
        # Fast negative check
        if not self._bloom.check(key):
            return None  # Definitely not cached

        # Might be cached, do actual lookup
        return self._cache.get(key)
```

### Expected Impact
- Cache miss detection: O(1) with 99% accuracy
- Backend load reduction: ~10%

### Research Source
- SeaweedFS: Uses Bloom filters for CAS existence checks
- Already in Nexus LocalBackend but not in permission cache

---

## Issue #P0-5: Async Task/Goroutine Leak Audit

**Title:** `fix: Audit async code for task leaks preventing OOM`

**Priority:** P0 - Critical (Safety)
**Effort:** Low (1-2 days)
**Risk:** None (preventive)

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Already safe - Rust ownership model |
| **Python** | ✅ Audit all async code |

### Problem
SeaweedFS Issue #7270: Missing `defer cancel()` caused goroutine leak (146MB → 5GB → OOM).

### Audit Checklist
```python
# Files to audit:
# - src/nexus/core/async_rebac_manager.py
# - src/nexus/remote/client.py
# - src/nexus/server/*.py

# Pattern to find and fix:
# BEFORE (leak risk):
async def operation():
    task = asyncio.create_task(background_work())
    result = await main_work()
    return result  # task may still be running!

# AFTER (safe):
async def operation():
    task = asyncio.create_task(background_work())
    try:
        result = await main_work()
        return result
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```

### Expected Impact
- Prevents OOM crashes under sustained load
- Memory stability over 24h+ operation

### Research Source
- SeaweedFS Issue #7270: Goroutine leak fixed with `defer cancelFunc()`
- JuiceFS Issue #132: Prefetch not cancelled wasted bandwidth

---

# P1 HIGH PRIORITY ISSUES (Week 3-6)

## Issue #P1-1: Subproblem Caching for Permission Checks

**Title:** `perf: Implement subproblem caching for 2-3x permission speedup`

**Priority:** P1 - High
**Effort:** Medium (1-2 weeks)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Recommended - extend existing memoization |
| **Python** | ✅ Also implement for non-Rust fallback |

### Problem
Current caching only stores final result: "Can Alice read /files/report.pdf?"
But permission checks involve multiple subproblems that are reusable:
1. Is Alice member of Engineering team? (reusable across all files)
2. Does Engineering have access to /files/? (reusable for all team members)
3. Does /files/ permission inherit? (reusable for all files in folder)

### Rust Solution
```rust
// File: rust/nexus_fast/src/lib.rs
// Modify around line 106

struct MultiLevelCache {
    // Current: final permission results
    permissions: InternedMemoCache,

    // NEW: Subproblem caches
    // (tenant, subject_type, subject_id) → Vec<(group_type, group_id)>
    memberships: AHashMap<(Sym, Sym, Sym), Vec<(Sym, Sym)>>,

    // (tenant, resource_type, resource_id) → Vec<(ancestor_type, ancestor_id)>
    hierarchies: AHashMap<(Sym, Sym, Sym), Vec<(Sym, Sym)>>,

    // (tenant, group_type, group_id, permission, resource_type, resource_id) → bool
    grants: AHashMap<(Sym, Sym, Sym, Sym, Sym, Sym), bool>,
}

impl MultiLevelCache {
    fn get_memberships(&self, tenant: Sym, subj_type: Sym, subj_id: Sym) -> Option<&Vec<(Sym, Sym)>> {
        self.memberships.get(&(tenant, subj_type, subj_id))
    }

    fn set_memberships(&mut self, tenant: Sym, subj_type: Sym, subj_id: Sym, groups: Vec<(Sym, Sym)>) {
        self.memberships.insert((tenant, subj_type, subj_id), groups);
    }
}
```

### Python Fallback
```python
# File: src/nexus/core/subproblem_cache.py

class SubproblemCache:
    def __init__(self):
        self._membership = TTLCache(maxsize=10000, ttl=300)   # 5 min (stable)
        self._hierarchy = TTLCache(maxsize=5000, ttl=600)     # 10 min (very stable)
        self._grants = TTLCache(maxsize=10000, ttl=60)        # 1 min (can change)

    def get_user_groups(self, user_id: str, tenant_id: str) -> Optional[frozenset]:
        return self._membership.get(f"{tenant_id}:{user_id}")

    def set_user_groups(self, user_id: str, tenant_id: str, groups: frozenset):
        self._membership[f"{tenant_id}:{user_id}"] = groups
```

### Expected Impact
- 60%+ cache reuse across different permission checks
- Cold permission check: 50-500ms → 10-50ms

### Research Source
- SpiceDB: Subproblem decomposition enables 60%+ reuse
- Zanzibar paper: Cache intermediate results, not just finals

---

## Issue #P1-2: Leopard Transitive Closure Index

**Title:** `perf: Implement Leopard-style index for O(1) group membership`

**Priority:** P1 - High
**Effort:** High (2-3 weeks)
**Risk:** Medium (invalidation complexity)

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Highly recommended - performance critical |
| **Python** | ⚠️ Fallback only - too slow for deep hierarchies |

### Problem
Checking "Is Alice in Engineering?" requires BFS traversal:
```
Alice → Team-Frontend → Division-Engineering → Org-Company
```
Current: O(depth) database queries per check.

### Rust Solution
```rust
// File: rust/nexus_fast/src/lib.rs
// Add new module

use dashmap::DashMap;
use ahash::AHashSet;

/// Leopard-style transitive closure index for O(1) membership lookups
/// From Zanzibar paper: 1.56M QPS median, 2.22M QPS at P99
struct LeopardIndex {
    // subject → all groups transitively (for permission checks)
    closure: DashMap<(Sym, Sym), AHashSet<(Sym, Sym)>, ahash::RandomState>,

    // group → all members (for invalidation when group changes)
    reverse: DashMap<(Sym, Sym), AHashSet<(Sym, Sym)>, ahash::RandomState>,
}

impl LeopardIndex {
    fn new() -> Self {
        LeopardIndex {
            closure: DashMap::with_hasher(ahash::RandomState::new()),
            reverse: DashMap::with_hasher(ahash::RandomState::new()),
        }
    }

    /// O(1) membership check
    fn is_member(&self, subject: (Sym, Sym), group: (Sym, Sym)) -> bool {
        self.closure
            .get(&subject)
            .map(|groups| groups.contains(&group))
            .unwrap_or(false)
    }

    /// Get all groups for a subject (for permission expansion)
    fn get_all_groups(&self, subject: (Sym, Sym)) -> Vec<(Sym, Sym)> {
        self.closure
            .get(&subject)
            .map(|groups| groups.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Rebuild closure for a subject (called on membership change)
    fn rebuild_for_subject(&self, subject: (Sym, Sym), direct_memberships: &[(Sym, Sym)]) {
        let mut visited = AHashSet::new();
        let mut queue: Vec<(Sym, Sym)> = direct_memberships.to_vec();

        while let Some(group) = queue.pop() {
            if visited.contains(&group) {
                continue;
            }
            visited.insert(group);

            // Add transitive memberships
            if let Some(parent_groups) = self.closure.get(&group) {
                for parent in parent_groups.iter() {
                    queue.push(*parent);
                }
            }
        }

        self.closure.insert(subject, visited);
    }
}

#[pyfunction]
fn leopard_is_member(subject_type: &str, subject_id: &str, group_type: &str, group_id: &str) -> bool {
    // Implementation with thread-local interner
    LEOPARD_INDEX.with(|index| {
        // ... intern strings and check
    })
}
```

### Expected Impact
- Group membership check: 5-50ms → <0.1ms (500x faster)
- Deep hierarchies (5+ levels): 100-500ms → <0.1ms

### Research Source
- Zanzibar paper Section 4.3: Leopard handles 1.56M QPS median
- Single tuple change can generate 10,000+ index updates (acceptable tradeoff)

---

## Issue #P1-3: BulkCheckPermission API Enhancement

**Title:** `feat: Enhance BulkCheckPermission with subproblem sharing`

**Priority:** P1 - High
**Effort:** Medium (1 week)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Already has `compute_permissions_bulk` - enhance it |
| **Python** | ✅ Add Python wrapper with better batching |

### Current State
Rust already has bulk checks at 85x speedup, but doesn't share subproblems.

### Rust Enhancement
```rust
// File: rust/nexus_fast/src/lib.rs
// Enhance compute_permissions_bulk around line 756

#[pyfunction]
fn compute_permissions_bulk_v2<'py>(
    py: Python<'py>,
    checks: &Bound<PyList>,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
    tuple_version: u64,
) -> PyResult<Bound<'py, PyDict>> {
    // ... existing parsing ...

    // NEW: Pre-compute shared subproblems
    let subjects: AHashSet<_> = check_requests.iter().map(|c| c.1).collect();
    let objects: AHashSet<_> = check_requests.iter().map(|c| c.3).collect();

    // Batch compute all memberships (reused across checks)
    let memberships: AHashMap<_, _> = subjects
        .par_iter()
        .map(|subj| (*subj, compute_memberships(&graph, subj)))
        .collect();

    // Batch compute all hierarchies (reused across checks)
    let hierarchies: AHashMap<_, _> = objects
        .par_iter()
        .map(|obj| (*obj, compute_ancestors(&graph, obj)))
        .collect();

    // Now compute permissions using pre-computed data
    let results = check_requests
        .par_iter()
        .map(|(orig, subj, perm, obj)| {
            let subj_groups = memberships.get(subj).unwrap();
            let obj_ancestors = hierarchies.get(obj).unwrap();
            let result = check_with_precomputed(subj_groups, obj_ancestors, *perm, &graph);
            (orig.clone(), result)
        })
        .collect();

    // ... return results ...
}
```

### Expected Impact
- Directory listing (1000 files): 100ms → 25ms (4x faster than current bulk)
- Database queries: 3 instead of N

### Research Source
- SpiceDB BulkCheckPermission: Batches SQL queries, shares subproblems
- Current Nexus Rust: 85x speedup but no subproblem sharing

---

## Issue #P1-4: Watch API for Real-Time Cache Invalidation

**Title:** `feat: Implement Watch API for event-based cache invalidation`

**Priority:** P1 - High
**Effort:** Medium (1-2 weeks)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - database events are Python-layer |
| **Python** | ✅ Implement using rebac_changelog table |

### Problem
TTL-based invalidation = up to 60s stale permissions (security risk).

### Solution
```python
# File: src/nexus/core/permission_watcher.py

class PermissionWatcher:
    """Stream permission changes for real-time cache invalidation."""

    def __init__(self, metadata_store):
        self._store = metadata_store
        self._position = 0

    async def watch(self, tenant_id: str) -> AsyncIterator[PermissionChange]:
        """Stream permission changes since last position."""
        while True:
            changes = await self._store.get_changelog_since(
                tenant_id, self._position, limit=100
            )

            for change in changes:
                yield PermissionChange(
                    subject=change.subject,
                    relation=change.relation,
                    object=change.object,
                    operation=change.operation,
                    timestamp=change.timestamp,
                )
                self._position = change.id

            if not changes:
                await asyncio.sleep(0.1)


class WatchBasedInvalidator:
    """Invalidate caches based on Watch events."""

    def __init__(self, cache, tiger_cache, leopard_index, watcher):
        self._cache = cache
        self._tiger = tiger_cache
        self._leopard = leopard_index
        self._watcher = watcher

    async def run(self, tenant_id: str):
        async for change in self._watcher.watch(tenant_id):
            # Invalidate affected caches precisely
            await self._cache.invalidate_for_change(change)
            await self._tiger.queue_rebuild(change.object)

            if change.relation == "member":
                await self._leopard.rebuild_for_subject(change.subject)
```

### Expected Impact
- Cache invalidation: 60s TTL → <100ms event-based
- Stale permission window: Eliminated

### Research Source
- SpiceDB Watch API: Real-time streaming of relationship changes
- Each event includes ZedToken for consistency

---

## Issue #P1-5: Memory-Efficient Metadata (JuiceFS-Inspired)

**Title:** `perf: Implement compact metadata structures for 3-5x memory reduction`

**Priority:** P1 - High
**Effort:** High (2-3 weeks)
**Risk:** Medium

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - could add compact structs |
| **Python** | ✅ Use `dataclass(slots=True)` and struct packing |

### Problem
Current: ~200 bytes per file metadata (Python objects).
JuiceFS: 100 bytes per file.
At 100M files: 20GB vs 10GB memory.

### Python Solution
```python
# File: src/nexus/storage/compact_metadata.py

import struct
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)  # slots=True: ~40% memory reduction
class CompactFileMetadata:
    """
    64 bytes fixed (vs ~200+ bytes current Python objects)

    Layout:
    - path_hash: 8 bytes (uint64)
    - content_hash: 32 bytes (SHA-256)
    - size: 8 bytes (uint64)
    - mtime: 8 bytes (uint64 unix timestamp)
    - flags: 8 bytes (permissions, type, etc.)
    """
    path_hash: int
    content_hash: bytes
    size: int
    mtime: int
    flags: int

    _struct = struct.Struct('>Q32sQQQ')  # 64 bytes, big-endian

    def to_bytes(self) -> bytes:
        return self._struct.pack(
            self.path_hash, self.content_hash, self.size, self.mtime, self.flags
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> 'CompactFileMetadata':
        return cls(*cls._struct.unpack(data))


class PathInterner:
    """Deduplicate path prefixes to save memory."""

    def __init__(self):
        self._interned: dict[str, str] = {}

    def intern(self, path: str) -> str:
        if path in self._interned:
            return self._interned[path]

        # Reuse parent path if already interned
        if '/' in path:
            parent, name = path.rsplit('/', 1)
            interned_parent = self.intern(parent)
            interned = f"{interned_parent}/{name}"
        else:
            interned = path

        self._interned[path] = interned
        return interned
```

### Optional Rust Enhancement
```rust
// File: rust/nexus_fast/src/lib.rs

/// Compact metadata for Python export
#[pyclass]
struct CompactMetadata {
    path_hash: u64,
    content_hash: [u8; 32],
    size: u64,
    mtime: u64,
    flags: u64,
}

#[pymethods]
impl CompactMetadata {
    fn to_bytes(&self) -> PyResult<Vec<u8>> {
        let mut buf = Vec::with_capacity(64);
        buf.extend_from_slice(&self.path_hash.to_be_bytes());
        buf.extend_from_slice(&self.content_hash);
        buf.extend_from_slice(&self.size.to_be_bytes());
        buf.extend_from_slice(&self.mtime.to_be_bytes());
        buf.extend_from_slice(&self.flags.to_be_bytes());
        Ok(buf)
    }
}
```

### Expected Impact
- Memory per file: 200 bytes → 64-100 bytes (2-3x reduction)
- 100M files: 20GB → 6-10GB

### Research Source
- JuiceFS: 100 bytes/file (27% of HDFS, 3.7% of CephFS)
- Techniques: arena memory, path interning, compression

---

## Issue #P1-6: Cross-Tenant Permission Optimization

**Title:** `perf: Implement dedicated cross-tenant permission index`

**Priority:** P1 - High
**Effort:** Medium (1-2 weeks)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - could add to graph structure |
| **Python** | ✅ Implement dedicated index |

### Problem
Cross-tenant checks are 2x slower (queries both tenants).
But cross-tenant is only 5% of traffic.

### Solution
```python
# File: src/nexus/core/cross_tenant_index.py

class CrossTenantGrantIndex:
    """
    Specialized index for cross-tenant shares.
    Optimized for: "Can user from tenant A access resource in tenant B?"
    """

    def __init__(self):
        # (object_tenant, object) → [(subject_tenant, subject, relation, expires_at)]
        self._by_object: dict[tuple, list[tuple]] = {}
        # (subject_tenant, subject) → [(object_tenant, object, relation)]
        self._by_subject: dict[tuple, list[tuple]] = {}
        # Longer TTL for cross-tenant (grants are stable)
        self._cache = TTLCache(maxsize=10000, ttl=600)  # 10 minutes

    def add_grant(self, subject_tenant, subject, relation, object_tenant, object, expires_at=None):
        key_obj = (object_tenant, object)
        key_subj = (subject_tenant, subject)

        if key_obj not in self._by_object:
            self._by_object[key_obj] = []
        self._by_object[key_obj].append((subject_tenant, subject, relation, expires_at))

        if key_subj not in self._by_subject:
            self._by_subject[key_subj] = []
        self._by_subject[key_subj].append((object_tenant, object, relation))

    def has_cross_tenant_access(
        self,
        subject: str,
        subject_tenant: str,
        permission: str,
        object: str,
        object_tenant: str
    ) -> Optional[bool]:
        """O(1) cross-tenant permission check."""
        # Check cache first
        cache_key = f"{subject_tenant}:{subject}:{permission}:{object_tenant}:{object}"
        if cached := self._cache.get(cache_key):
            return cached

        # Check index
        grants = self._by_object.get((object_tenant, object), [])
        for grant_subj_tenant, grant_subj, relation, expires_at in grants:
            if grant_subj_tenant == subject_tenant and grant_subj == subject:
                if expires_at and expires_at < time.time():
                    continue  # Expired
                if self._relation_implies(relation, permission):
                    self._cache[cache_key] = True
                    return True

        self._cache[cache_key] = False
        return False
```

### Integration
```python
# File: src/nexus/core/rebac_manager.py

async def check(self, subject, permission, object, subject_tenant, object_tenant=None):
    if object_tenant is None or object_tenant == subject_tenant:
        # Same-tenant: hot path (95% of checks)
        return await self._check_same_tenant(subject, permission, object, subject_tenant)
    else:
        # Cross-tenant: dedicated path (5% of checks)
        return await self._check_cross_tenant(
            subject, subject_tenant, permission, object, object_tenant
        )
```

### Expected Impact
- Cross-tenant check (cached): 40-100ms → <5ms
- Same-tenant path: Unchanged (not affected)

### Research Source
- SpiceDB multi-tenancy patterns
- Nexus: 95% same-tenant, 5% cross-tenant traffic

---

# P2 MEDIUM PRIORITY ISSUES (Week 7-12)

## Issue #P2-1: Volume-Based Storage Backend

**Title:** `feat: Implement volume-based storage for 13x metadata efficiency`

**Priority:** P2 - Medium
**Effort:** Very High (4-6 weeks)
**Risk:** High (major architecture change)

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Recommended for performance-critical paths |
| **Python** | ✅ Backend interface and orchestration |

### Problem
Current: Each file = 1 filesystem inode (536 bytes overhead on XFS).
SeaweedFS: 40 bytes per file, 16 bytes in memory.

### Solution Overview
```python
# File: src/nexus/backends/volume_backend.py

class VolumeBackend(Backend):
    """
    Store files as needles in volume files (SeaweedFS-inspired).

    Benefits:
    - 13x metadata efficiency (40 bytes vs 536 bytes/file)
    - O(1) reads via in-memory index
    - Better sequential write performance
    """

    VOLUME_SIZE = 32 * 1024 * 1024 * 1024  # 32GB per volume

    def __init__(self, base_path: str):
        self._volumes: dict[int, VolumeFile] = {}
        self._index: dict[str, NeedleLocation] = {}  # hash → (vol_id, offset, size)
        self._current_volume_id = 0

    async def write_content(self, content: bytes) -> str:
        content_hash = self._hash(content)

        if content_hash in self._index:
            return content_hash  # Deduplication

        volume = await self._get_writable_volume()
        offset = await volume.append(content_hash, content)

        self._index[content_hash] = NeedleLocation(
            volume_id=volume.id,
            offset=offset,
            size=len(content)
        )
        return content_hash

    async def read_content(self, content_hash: str) -> bytes:
        location = self._index.get(content_hash)
        if not location:
            raise FileNotFoundError(content_hash)

        # O(1) disk read - single seek
        volume = self._volumes[location.volume_id]
        return await volume.read_at(location.offset, location.size)
```

### Rust Component (Index)
```rust
// File: rust/nexus_fast/src/volume.rs

use memmap2::Mmap;
use dashmap::DashMap;

/// In-memory needle index for O(1) lookups
struct NeedleIndex {
    // content_hash → (volume_id, offset, size)
    index: DashMap<[u8; 32], (u32, u64, u32), ahash::RandomState>,
}

impl NeedleIndex {
    fn lookup(&self, hash: &[u8; 32]) -> Option<(u32, u64, u32)> {
        self.index.get(hash).map(|r| *r)
    }

    fn insert(&self, hash: [u8; 32], volume_id: u32, offset: u64, size: u32) {
        self.index.insert(hash, (volume_id, offset, size));
    }
}

#[pyfunction]
fn volume_index_lookup(hash_hex: &str) -> Option<(u32, u64, u32)> {
    // Thread-safe lookup in global index
}
```

### Expected Impact
- Metadata per file: 536 bytes → 40 bytes (13x reduction)
- Memory index: 16 bytes per file
- Disk seeks: O(1) per read

### Research Source
- SeaweedFS: Needle-in-haystack design from Facebook Haystack paper
- Facebook: 4x read performance, 28% cost reduction

---

## Issue #P2-2: Tiered Storage with Erasure Coding

**Title:** `feat: Implement tiered storage with automatic EC for warm data`

**Priority:** P2 - Medium
**Effort:** High (3-4 weeks)
**Risk:** Medium

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - EC libraries available |
| **Python** | ✅ Orchestration and policy engine |

### Problem
All data stored with same redundancy.
Hot data needs fast access, cold data needs cost efficiency.

### Solution
```python
# File: src/nexus/storage/tiered_storage.py

class TieredStorageManager:
    """
    Automatic data tiering based on access patterns.

    Hot:  Local SSD, 3x replication, <1ms access
    Warm: Local HDD, RS(10,4) EC, <10ms access
    Cold: Cloud S3, RS(10,4) EC, <100ms access
    """

    def __init__(self):
        self._hot = LocalSSDBackend(max_size="100GB")
        self._warm = ErasureCodingBackend(data_shards=10, parity_shards=4)
        self._cold = S3Backend(bucket="nexus-archive")
        self._access_tracker = AccessTracker()
        self._tier_index = TierIndex()  # hash → tier

    async def read(self, content_hash: str) -> bytes:
        tier = self._tier_index.get(content_hash, Tier.COLD)
        self._access_tracker.record(content_hash)

        if tier == Tier.HOT:
            return await self._hot.read(content_hash)
        elif tier == Tier.WARM:
            content = await self._warm.read(content_hash)
            if self._access_tracker.should_promote(content_hash):
                await self._promote(content_hash, content, Tier.HOT)
            return content
        else:
            content = await self._cold.read(content_hash)
            await self._cache_locally(content_hash, content)
            return content

    async def background_tiering(self):
        """Periodic job to demote cold content."""
        for content_hash in self._access_tracker.get_cold_content(days=30):
            current_tier = self._tier_index.get(content_hash)
            if current_tier == Tier.HOT:
                await self._demote(content_hash, Tier.WARM)
            elif current_tier == Tier.WARM:
                await self._demote(content_hash, Tier.COLD)
```

### Expected Impact
- Storage cost: 42% reduction for warm data (3x → 1.4x overhead)
- Hot data latency: Unchanged
- Warm data latency: Acceptable (<10ms)

### Research Source
- Facebook f4: 42% storage reduction with RS(10,4)
- SeaweedFS: Erasure coding for warm/cold tiering

---

## Issue #P2-3: Database Partitioning for 100M+ Files

**Title:** `feat: Implement database partitioning for 100M+ file scale`

**Priority:** P2 - Medium
**Effort:** High (2-3 weeks)
**Risk:** Medium

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - database layer |
| **Python** | ✅ SQLAlchemy partitioning |

### Solution
```sql
-- File: migrations/partition_files.sql

-- Partition main files table by tenant
CREATE TABLE files (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    created_at TIMESTAMP
) PARTITION BY HASH (tenant_id);

-- Create 64 partitions
CREATE TABLE files_p00 PARTITION OF files FOR VALUES WITH (modulus 64, remainder 0);
CREATE TABLE files_p01 PARTITION OF files FOR VALUES WITH (modulus 64, remainder 1);
-- ... repeat for 2-63

-- Shared content index (NOT partitioned, for cross-tenant dedup)
CREATE TABLE shared_content_index (
    content_hash TEXT PRIMARY KEY,
    owner_tenant_id UUID NOT NULL,
    shared_with_tenants UUID[] DEFAULT '{}',
    ref_count INTEGER DEFAULT 1,
    created_at TIMESTAMP
);

CREATE INDEX idx_shared_tenants ON shared_content_index USING GIN (shared_with_tenants);

-- Permission tuples partitioned by tenant
CREATE TABLE rebac_tuples (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    relation TEXT,
    object_type TEXT,
    object_id TEXT,
    created_at TIMESTAMP
) PARTITION BY HASH (tenant_id);
```

### Expected Impact
- Query performance at 100M files: Maintained
- Tenant isolation: Improved
- Partition pruning: Automatic by PostgreSQL

### Research Source
- Nexus Issue #870: Database partitioning needed
- SeaweedFS: Distributed metadata across volume servers

---

## Issue #P2-4: Connection Pooling with PgBouncer

**Title:** `perf: Implement PgBouncer connection pooling`

**Priority:** P2 - Medium
**Effort:** Low (3-5 days)
**Risk:** Low

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - infrastructure |
| **Python** | ✅ SQLAlchemy pool configuration |
| **Infrastructure** | ✅ Deploy PgBouncer |

### Solution
```python
# File: src/nexus/storage/connection_pool.py

from sqlalchemy.pool import QueuePool

def create_optimized_engine(database_url: str):
    return create_async_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "server_settings": {
                "statement_timeout": "30000",
                "idle_in_transaction_session_timeout": "60000"
            }
        }
    )
```

### Expected Impact
- Connection overhead: Reduced by 80%
- Concurrent request handling: Improved

### Research Source
- SeaweedFS Issue #5794: PostgreSQL connection management
- JuiceFS: Connection pooling critical for metadata performance

---

## Issue #P2-5: Configurable Parallel Thresholds

**Title:** `feat: Make Rust parallel thresholds configurable`

**Priority:** P2 - Medium
**Effort:** Low (1 day)
**Risk:** None

### Implementation

| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Add configuration functions |
| **Python** | ✅ Expose configuration API |

### Current State
```rust
// rust/nexus_fast/src/lib.rs lines 51-52
const GLOB_PARALLEL_THRESHOLD: usize = 500;
const PERMISSION_PARALLEL_THRESHOLD: usize = 50;
```

### Solution
```rust
// File: rust/nexus_fast/src/lib.rs

use std::sync::atomic::{AtomicUsize, Ordering};

static GLOB_THRESHOLD: AtomicUsize = AtomicUsize::new(500);
static PERMISSION_THRESHOLD: AtomicUsize = AtomicUsize::new(50);

#[pyfunction]
fn set_parallel_threshold(operation: &str, threshold: usize) -> PyResult<()> {
    match operation {
        "glob" => GLOB_THRESHOLD.store(threshold, Ordering::SeqCst),
        "permission" => PERMISSION_THRESHOLD.store(threshold, Ordering::SeqCst),
        _ => return Err(PyValueError::new_err("Unknown operation")),
    }
    Ok(())
}

#[pyfunction]
fn get_parallel_threshold(operation: &str) -> PyResult<usize> {
    match operation {
        "glob" => Ok(GLOB_THRESHOLD.load(Ordering::SeqCst)),
        "permission" => Ok(PERMISSION_THRESHOLD.load(Ordering::SeqCst)),
        _ => Err(PyValueError::new_err("Unknown operation")),
    }
}
```

### Expected Impact
- Tunable performance for different hardware
- Better CPU utilization

---

# P3 FUTURE ISSUES

## Issue #P3-1: Distributed Cache with Consistent Hashing

**Title:** `feat: Implement distributed cache with consistent hashing`

**Priority:** P3 - Future
**Effort:** High
**Risk:** Medium

### Implementation
| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - could implement ring |
| **Python** | ✅ Redis cluster integration |

---

## Issue #P3-2: Materialized Views for Common Queries

**Title:** `feat: Implement materialized permission views`

**Priority:** P3 - Future
**Effort:** High
**Risk:** Medium

### Implementation
| Layer | Recommendation |
|-------|----------------|
| **Rust** | ❌ Not needed - database layer |
| **Python** | ✅ PostgreSQL materialized views |

---

## Issue #P3-3: FUSE Client Optimization

**Title:** `perf: Optimize nexus-fuse client performance`

**Priority:** P3 - Future
**Effort:** Medium
**Risk:** Low

### Implementation
| Layer | Recommendation |
|-------|----------------|
| **Rust** | ✅ Already in `nexus-fuse/` - enhance caching |
| **Python** | ❌ N/A |

---

## Issue #P3-4: Predictive Cache Warming

**Title:** `feat: Implement predictive cache warming`

**Priority:** P3 - Future
**Effort:** Medium
**Risk:** Low

### Implementation
| Layer | Recommendation |
|-------|----------------|
| **Rust** | ⚠️ Optional - ML model serving |
| **Python** | ✅ Access pattern analysis |

---

# IMPLEMENTATION SUMMARY

## By Priority

| Priority | Issues | Rust Work | Python Work |
|----------|--------|-----------|-------------|
| **P0** | 5 | 1 (Bloom filter) | 5 |
| **P1** | 6 | 3 (Subproblem, Leopard, Bulk v2) | 6 |
| **P2** | 5 | 2 (Volume index, Thresholds) | 5 |
| **P3** | 4 | 1 (FUSE) | 3 |

## By Implementation Layer

### Rust-Required Issues
| Issue | Reason |
|-------|--------|
| P0-4: Bloom Filter | Already imported, just need to expose |
| P1-1: Subproblem Cache | Performance critical, extend memoization |
| P1-2: Leopard Index | O(1) vs O(depth), must be fast |
| P1-3: Bulk v2 | Enhance existing Rust function |
| P2-1: Volume Index | In-memory index for O(1) reads |
| P2-5: Thresholds | Simple Rust change |

### Python-Only Issues
| Issue | Reason |
|-------|--------|
| P0-1: Quantization | Cache key generation |
| P0-2: Deduplication | Async coordination |
| P0-3: Tiger Single | Bitmap operations in Python |
| P0-5: Task Audit | Code review |
| P1-4: Watch API | Database events |
| P1-6: Cross-Tenant | Index management |
| P2-2: Tiered Storage | Policy engine |
| P2-3: Partitioning | Database schema |
| P2-4: PgBouncer | Infrastructure |

## Timeline

```
Week 1-2 (P0):
├─ Timestamp quantization (Python)
├─ Request deduplication (Python)
├─ Tiger Cache single ops (Python)
├─ Expose Bloom filter (Rust)
└─ Task leak audit (Python)

Week 3-4 (P1 Start):
├─ Subproblem caching (Rust + Python)
├─ Watch API (Python)
└─ Cross-tenant index (Python)

Week 5-6 (P1 Complete):
├─ Leopard index (Rust)
├─ Bulk v2 enhancement (Rust)
└─ Memory-efficient metadata (Python)

Week 7-12 (P2):
├─ Volume-based storage (Rust + Python)
├─ Tiered storage (Python)
├─ Database partitioning (Python)
├─ Connection pooling (Infrastructure)
└─ Configurable thresholds (Rust)
```

---

## REFERENCES

### Research Documents
1. `seaweedfs-deep-technical-dive.md` - Volume internals, CompactMap
2. `seaweedfs-github-issues-analysis.md` - 50+ issues, 45x LevelDB improvement
3. `juicefs-issues-deep-dive.md` - <2ms metadata requirement
4. `spicedb-deep-dive.md` - Subproblem caching, quantization
5. `zanzibar-permission-optimizations.md` - Leopard index, 1M QPS
6. `distributed-filesystem-best-practices.md` - Facebook, Netflix patterns
7. `rust-cross-reference.md` - Current Rust implementation analysis

### External Sources
- [SeaweedFS GitHub](https://github.com/seaweedfs/seaweedfs)
- [JuiceFS GitHub](https://github.com/juicedata/juicefs)
- [SpiceDB GitHub](https://github.com/authzed/spicedb)
- [Google Zanzibar Paper](https://research.google/pubs/pub48190/)
- [Facebook Haystack Paper](https://www.usenix.org/conference/osdi10/finding-needle-haystack)

### Nexus Files Analyzed
- `rust/nexus_fast/src/lib.rs` (2500+ lines)
- `src/nexus/core/rebac_*.py`
- `src/nexus/core/tiger_cache.py`
- `src/nexus/storage/*.py`
- `src/nexus/backends/*.py`
