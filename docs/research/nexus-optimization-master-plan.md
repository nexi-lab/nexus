# NEXUS OPTIMIZATION MASTER PLAN

## Cross-Referenced Research from SeaweedFS, JuiceFS, SpiceDB & Production Systems

**Date:** December 26, 2025
**Status:** Ready for Implementation
**Research Sources:** 6 deep-dive documents, 100+ GitHub issues analyzed, 40+ production system patterns

---

## EXECUTIVE SUMMARY

This document synthesizes findings from:
- **SeaweedFS**: Volume-based storage, O(1) reads, 95% memory reduction with CompactMap
- **JuiceFS**: 100 bytes/file metadata, 45μs writes, arena memory management
- **SpiceDB/Zanzibar**: 1M QPS authorization, subproblem caching, Leopard indexing
- **Production Systems**: Facebook Haystack (4x perf), Dropbox Magic Pocket (12 nines), Netflix (98% cache hit)
- **Nexus Issues**: 14 implemented optimizations, 10 proposed improvements

### Key Performance Targets

| Metric | Current | Phase 1 | Phase 2 | Phase 3 |
|--------|---------|---------|---------|---------|
| Permission check (cached) | 10-100ms | **<1ms** | <0.5ms | <0.1ms |
| Directory list (1000 files) | 500-1000ms | **<100ms** | <50ms | <20ms |
| Memory per file | ~200 bytes | **<100 bytes** | <64 bytes | <40 bytes |
| Cache hit rate | ~70% | **85%+** | 95%+ | 99%+ |
| Same-tenant read | 15-20ms | **<5ms** | <3ms | <1ms |

---

## CRITICAL FINDINGS

### From SeaweedFS (45x Performance Gains Possible)

| Finding | Source | Impact | Nexus Application |
|---------|--------|--------|-------------------|
| **LevelDB defaults beat tuning** | Issue #2325 | 45x (600→27K keys/s) | Don't over-tune caches |
| **CompactMap 95% memory reduction** | v3.88 | 20MB→1MB per 1M files | Implement compact metadata |
| **O(1) disk reads via in-memory index** | Architecture | Single seek per file | Pre-load hot path indexes |
| **Goroutine leak pattern** | Issue #7270 | OOM prevention | Audit all `defer cancelFunc()` |
| **Data resurrection bug** | Issue #7102 | Data integrity | Check metadata before restore |

### From JuiceFS (100 bytes/file Achievable)

| Finding | Source | Impact | Nexus Application |
|---------|--------|--------|-------------------|
| **<2ms metadata latency required** | Issue #145 | 200x ops/sec difference | Use local SQLite + Redis |
| **Lua script batching** | Issue #94 | 50% round-trip reduction | Batch metadata operations |
| **S3 API > native SDKs** | Issue #496 | Prevents memory leaks | Use S3-compatible APIs |
| **Quota double-counting** | Issue #5018 | Billing accuracy | Handle sustained inodes |
| **30s timeout minimum** | Issue #182 | Write stability | Increase timeouts |

### From SpiceDB/Zanzibar (1M QPS Achievable)

| Finding | Source | Impact | Nexus Application |
|---------|--------|--------|-------------------|
| **Timestamp quantization** | Zanzibar paper | 10-100x cache sharing | 5-second quantum windows |
| **Subproblem decomposition** | SpiceDB | 60%+ cache reuse | Cache intermediate results |
| **Leopard transitive closure** | Zanzibar | O(1) group lookups | Pre-compute memberships |
| **Request deduplication** | SpiceDB | 40% computation reduction | Coalesce concurrent checks |
| **Watch API invalidation** | SpiceDB | Real-time consistency | Event-based cache updates |

### From Production Systems

| System | Finding | Numbers | Nexus Application |
|--------|---------|---------|-------------------|
| **Facebook Haystack** | Aggregate files in segments | 4x read perf, 28% cheaper | Volume-based storage |
| **Facebook f4** | Erasure coding for warm | 42% storage reduction | Tiered storage |
| **Dropbox** | Sharded MySQL at scale | 12 nines durability | Simple > complex |
| **Netflix** | Two-tier caching | 98% hit rate | Client + server cache |
| **Uber Docstore** | Stateless query layer | 40M req/sec, 99.9% hits | Separate concerns |

---

## IMPLEMENTATION PLAN

### PHASE 1: QUICK WINS (Week 1-2) 🔥

#### 1.1 Timestamp Quantization [CRITICAL]

**Problem:** Cache keys include precise timestamps → cache misses for identical checks milliseconds apart

**SeaweedFS Lesson:** Default LevelDB settings gave 45x improvement over "tuned" settings

**Solution:**
```python
# File: src/nexus/core/rebac_cache.py

QUANTIZATION_INTERVAL = 5  # seconds (from SpiceDB)

def _get_cache_key(self, subject, permission, object, tenant_id):
    # Round to 5-second boundary for cache sharing
    quantum = int(time.time() // QUANTIZATION_INTERVAL) * QUANTIZATION_INTERVAL
    return f"{tenant_id}:{subject}:{permission}:{object}:q{quantum}"
```

**Expected Impact:** 10-100x cache hit improvement
**Effort:** Low (1-2 days)
**Risk:** Low

---

#### 1.2 Request Deduplication [CRITICAL]

**Problem:** 100 concurrent requests for same file = 100 separate permission checks

**JuiceFS Lesson:** Issue #132 - Always cancel prefetch/background operations properly

**Solution:**
```python
# File: src/nexus/core/rebac_manager.py

class PermissionDeduplicator:
    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def check_deduplicated(self, key, compute_fn):
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

**Expected Impact:** 10-50x fewer computations under concurrent load
**Effort:** Low (2-3 days)
**Risk:** Low

---

#### 1.3 Tiger Cache for Single Operations [HIGH PRIORITY]

**Problem:** Tiger Cache only used for `filter_list()`, single reads still slow

**Current Nexus State:** Permission checks = 60% of read latency

**Solution:**
```python
# File: src/nexus/core/nexus_fs_core.py

async def _check_permission_fast(self, path, permission, context):
    # 1. Try Tiger Cache first (O(1) bitmap lookup)
    if result := self._tiger_cache.check_cached(
        context.subject, permission, path, context.tenant_id
    ):
        return result  # <1ms

    # 2. Fall back to ReBAC with cache population
    result = await self._rebac.check(...)

    # 3. Update Tiger Cache for future checks
    await self._tiger_cache.update_single(...)

    return result
```

**Expected Impact:** 10-100x faster single-file permission checks
**Effort:** Medium (3-5 days)
**Risk:** Low

---

#### 1.4 Goroutine/Task Audit [CRITICAL - SAFETY]

**SeaweedFS Lesson:** Issue #7270 - Goroutine leak (146MB → 5GB → OOM) fixed by `defer cancelFunc()`

**Action Items:**
```python
# Audit all async operations for proper cancellation
# Pattern to find:
#   ctx, cancel = context.WithCancel(...)
#   # Missing: defer cancel()

# Nexus equivalent - ensure all tasks have:
try:
    result = await some_operation()
finally:
    cleanup()  # Always runs
```

**Files to Audit:**
- `src/nexus/core/async_rebac_manager.py`
- `src/nexus/remote/client.py`
- `src/nexus/server/*.py`

**Expected Impact:** Prevents OOM crashes
**Effort:** Low (1-2 days)
**Risk:** None

---

### PHASE 2: CORE OPTIMIZATIONS (Week 3-6)

#### 2.1 Subproblem Caching [HIGH PRIORITY]

**SpiceDB Insight:** Cache intermediate results, not just final permissions

**Problem:** Permission check for `/workspace/project/src/file.txt` computes:
1. Is user member of team?
2. Does team have access to /workspace?
3. Does /workspace permission inherit?
4. ... (repeated for every file)

**Solution:**
```python
# File: src/nexus/core/subproblem_cache.py

class SubproblemCache:
    def __init__(self):
        # Different TTLs for different subproblem types
        self._membership = TTLCache(maxsize=10000, ttl=300)   # 5 min (stable)
        self._hierarchy = TTLCache(maxsize=5000, ttl=600)     # 10 min (very stable)
        self._grants = TTLCache(maxsize=10000, ttl=60)        # 1 min (can change)

    def get_user_groups(self, user_id, tenant_id) -> Optional[frozenset]:
        """Cache: user → all groups (transitively)"""
        return self._membership.get(f"{tenant_id}:{user_id}")

    def get_resource_ancestors(self, path, tenant_id) -> Optional[list]:
        """Cache: path → [parent, grandparent, ...]"""
        return self._hierarchy.get(f"{tenant_id}:{path}")

    def has_grant(self, group, permission, resource, tenant_id) -> Optional[bool]:
        """Cache: (group, permission, resource) → bool"""
        return self._grants.get(f"{tenant_id}:{group}:{permission}:{resource}")
```

**Expected Impact:** 2-3x fewer graph traversals
**Effort:** Medium (1-2 weeks)
**Risk:** Low

---

#### 2.2 Leopard-Style Transitive Closure Index [HIGH PRIORITY]

**Zanzibar Insight:** Pre-compute group memberships for O(1) lookups

**Problem:** Checking "Is Alice in Engineering?" requires traversing:
```
Alice → Team-Frontend → Division-Engineering → Org-Company
```

**Solution:**
```python
# File: src/nexus/core/leopard_index.py

class LeopardIndex:
    """
    Transitive closure index for O(1) membership lookups.

    From Zanzibar paper: Handles 1.56M QPS median, 2.22M QPS at P99
    """

    def __init__(self):
        # subject → frozenset[all_groups_transitively]
        self._closure: dict[str, frozenset[str]] = {}
        # group → set[all_members_transitively] (for invalidation)
        self._reverse: dict[str, set[str]] = {}

    def is_member(self, subject: str, group: str, tenant_id: str) -> bool:
        """O(1) membership check."""
        key = f"{tenant_id}:{subject}"
        return group in self._closure.get(key, frozenset())

    async def rebuild_for_subject(self, subject: str, tenant_id: str):
        """Rebuild closure on membership change."""
        # BFS to find all transitive memberships
        visited = set()
        queue = [subject]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            direct_groups = await self._get_direct_memberships(current, tenant_id)
            queue.extend(direct_groups)

        self._closure[f"{tenant_id}:{subject}"] = frozenset(visited - {subject})
```

**Zanzibar Numbers:**
- Single tuple change can generate 10,000+ Leopard index updates
- But lookup is O(1) vs O(depth) traversal

**Expected Impact:** 50-500x faster group membership checks
**Effort:** High (2-3 weeks)
**Risk:** Medium (invalidation complexity)

---

#### 2.3 BulkCheckPermission API [HIGH PRIORITY]

**SpiceDB Pattern:** Single API call for multiple permission checks

**Problem:** Directory listing with 1000 files = 1000 sequential checks

**Solution:**
```python
# File: src/nexus/core/rebac_manager.py

async def check_permissions_bulk(
    self,
    checks: list[tuple[str, str, str]],  # [(subject, permission, object), ...]
    tenant_id: str
) -> dict[tuple, bool]:
    """
    Check multiple permissions in single optimized operation.

    Optimizations:
    1. Single DB query for all relevant tuples
    2. Shared subproblem computation
    3. Parallel graph traversal
    4. Batch cache population
    """
    # 1. Extract unique subjects/objects
    subjects = {c[0] for c in checks}
    objects = {c[2] for c in checks}

    # 2. Batch fetch all data (3 queries instead of N)
    memberships = await self._get_memberships_bulk(subjects, tenant_id)
    hierarchies = await self._get_hierarchies_bulk(objects, tenant_id)
    tuples = await self._get_tuples_bulk(subjects, objects, tenant_id)

    # 3. Compute using pre-fetched data
    results = {}
    for subject, permission, object in checks:
        results[(subject, permission, object)] = self._compute_with_prefetch(
            subject, permission, object, memberships, hierarchies, tuples
        )

    return results
```

**Expected Impact:** 40x faster directory listings (1s → 25ms)
**Effort:** Medium (1-2 weeks)
**Risk:** Low

---

#### 2.4 Memory-Efficient Metadata (JuiceFS-Inspired) [MEDIUM PRIORITY]

**JuiceFS Achievement:** 100 bytes/file (27% of HDFS, 3.7% of CephFS)

**Techniques:**
1. **Arena memory management** - bypass GC
2. **Path interning** - deduplicate common prefixes
3. **Compact structs** - fixed-size binary format
4. **Directory compression** - 50-67% reduction

**Solution:**
```python
# File: src/nexus/storage/compact_metadata.py

import struct
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)  # slots=True reduces memory by ~40%
class CompactFileMetadata:
    """
    64 bytes fixed (vs ~200+ bytes current Python objects)

    Layout:
    - path_hash: 8 bytes (uint64, for fast lookup)
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

    _struct = struct.Struct('>Q32sQQQ')  # 64 bytes

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
        parent = path.rsplit('/', 1)[0] if '/' in path else ''
        if parent:
            interned_parent = self.intern(parent)
            name = path[len(parent)+1:]
            interned = f"{interned_parent}/{name}"
        else:
            interned = path

        self._interned[path] = interned
        return interned
```

**Expected Impact:** 3-5x memory reduction
**Effort:** High (2-3 weeks)
**Risk:** Medium

---

#### 2.5 Watch API for Real-Time Invalidation [MEDIUM PRIORITY]

**SpiceDB Pattern:** Stream permission changes for cache invalidation

**Problem:** TTL-based invalidation = up to 60s stale permissions

**Solution:**
```python
# File: src/nexus/core/permission_watcher.py

class PermissionWatcher:
    """Stream permission changes for real-time cache invalidation."""

    async def watch(
        self,
        tenant_id: str,
        start_position: int = 0
    ) -> AsyncIterator[PermissionChange]:
        """
        Stream permission changes since position.

        Uses rebac_changelog table for change tracking.
        """
        position = start_position

        while True:
            changes = await self._poll_changelog(tenant_id, position)

            for change in changes:
                yield PermissionChange(
                    subject=change.subject,
                    relation=change.relation,
                    object=change.object,
                    operation=change.operation,
                    position=change.id
                )
                position = change.id

            if not changes:
                await asyncio.sleep(0.1)  # Poll interval


class WatchBasedInvalidator:
    """Invalidate caches based on Watch events."""

    async def run(self, tenant_id: str):
        async for change in self._watcher.watch(tenant_id):
            # Precise invalidation instead of TTL
            await self._cache.invalidate_for_change(change)
            await self._tiger_cache.queue_rebuild(change.object)
            await self._leopard.update_if_membership(change)
```

**Expected Impact:** Real-time consistency (60s → <100ms)
**Effort:** Medium (1-2 weeks)
**Risk:** Low

---

### PHASE 3: ADVANCED OPTIMIZATIONS (Week 7-12)

#### 3.1 Volume-Based Storage (SeaweedFS-Inspired)

**SeaweedFS Achievement:** O(1) disk reads, 16 bytes/file in memory

**Concept:** Store multiple files in large volume files (32GB each)

```python
# File: src/nexus/backends/volume_backend.py

class VolumeBackend(Backend):
    """
    Store files as needles in volume files.

    Benefits:
    - Reduces filesystem inode pressure
    - Enables O(1) reads via in-memory index
    - Better sequential write performance
    """

    VOLUME_SIZE = 32 * 1024 * 1024 * 1024  # 32GB

    def __init__(self, base_path: str):
        self._volumes: dict[int, VolumeFile] = {}
        self._index: dict[str, NeedleLocation] = {}  # hash → (vol_id, offset, size)

    async def write_content(self, content: bytes) -> str:
        content_hash = self._hash(content)

        if content_hash in self._index:
            return content_hash  # Deduplication

        volume = await self._get_writable_volume()
        offset = await volume.append(content_hash, content)

        self._index[content_hash] = NeedleLocation(volume.id, offset, len(content))
        return content_hash

    async def read_content(self, content_hash: str) -> bytes:
        location = self._index.get(content_hash)
        if not location:
            raise FileNotFoundError(content_hash)

        # O(1) disk read - single seek
        volume = self._volumes[location.volume_id]
        return await volume.read_at(location.offset, location.size)
```

**Expected Impact:**
- 13x metadata efficiency (40 bytes vs 536 bytes/file)
- O(1) disk reads
- Better for billions of files

**Effort:** Very High (4-6 weeks)
**Risk:** High (major architecture change)

---

#### 3.2 Tiered Storage with Erasure Coding

**Facebook f4 Achievement:** 42% storage reduction for warm data

**Concept:** Hot (replicated) → Warm (erasure coded) → Cold (cloud)

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

    async def read(self, content_hash: str) -> bytes:
        tier = await self._get_tier(content_hash)
        self._access_tracker.record(content_hash)

        if tier == Tier.HOT:
            return await self._hot.read(content_hash)
        elif tier == Tier.WARM:
            content = await self._warm.read(content_hash)
            # Promote if frequently accessed
            if self._access_tracker.is_hot(content_hash):
                await self._promote(content_hash, content, Tier.HOT)
            return content
        else:
            content = await self._cold.read(content_hash)
            await self._cache_locally(content_hash, content)
            return content

    async def background_tiering(self):
        """Periodic job to demote cold content."""
        for content_hash in self._access_tracker.get_cold_content():
            await self._demote(content_hash)
```

**Expected Impact:** 42% storage cost reduction for warm data
**Effort:** High (3-4 weeks)
**Risk:** Medium

---

#### 3.3 Cross-Tenant Optimization

**Problem:** Cross-tenant checks are 2x slower (queries both tenants)

**Solution:** Dedicated cross-tenant grant index

```python
# File: src/nexus/core/cross_tenant_index.py

class CrossTenantGrantIndex:
    """
    Specialized index for cross-tenant shares.
    Optimized for: "Can user from tenant A access resource in tenant B?"
    """

    def __init__(self):
        # (object_tenant, object) → [(subject_tenant, subject, relation)]
        self._by_object: dict[tuple, list[tuple]] = {}
        # (subject_tenant, subject) → [(object_tenant, object, relation)]
        self._by_subject: dict[tuple, list[tuple]] = {}

    def has_cross_tenant_access(
        self,
        subject: str,
        subject_tenant: str,
        permission: str,
        object: str,
        object_tenant: str
    ) -> Optional[bool]:
        """O(1) cross-tenant permission check."""
        grants = self._by_object.get((object_tenant, object), [])

        for grant_subj_tenant, grant_subj, relation in grants:
            if grant_subj_tenant == subject_tenant and grant_subj == subject:
                if self._relation_implies(relation, permission):
                    return True

        return None  # Not in index, fall back to full check
```

**Expected Impact:** 5-10x faster cross-tenant checks
**Effort:** Medium (1-2 weeks)
**Risk:** Low

---

## SAME-TENANT VS CROSS-TENANT OPTIMIZATION

### Traffic Distribution
- **Same-tenant:** 95% of operations
- **Cross-tenant:** 5% of operations (sharing, delegation)

### Optimization Strategy

```
┌─────────────────────────────────────────────────────────────┐
│                    Permission Check                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  SAME-TENANT (95%) ─────────────────────► HOT PATH          │
│  │                                                           │
│  ├─ L1: Tiger Cache bitmap (O(1))          Target: <1ms     │
│  ├─ L2: Subproblem cache                                    │
│  ├─ L3: Leopard transitive closure                          │
│  └─ L4: Full ReBAC (rare)                                   │
│                                                              │
│  CROSS-TENANT (5%) ─────────────────────► DEDICATED PATH    │
│  │                                                           │
│  ├─ Cross-tenant grant index (O(1))        Target: <5ms     │
│  ├─ Separate cache partition                                │
│  ├─ Longer TTL (600s - grants stable)                       │
│  └─ Full check with both tenants                            │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Cache Partitioning

```python
class TenantAwareCache:
    def __init__(self):
        # Separate partitions prevent cross-contamination
        self._same_tenant = TTLCache(maxsize=50000, ttl=60)
        self._cross_tenant = TTLCache(maxsize=5000, ttl=600)

    def get(self, subject, permission, object, subject_tenant, object_tenant):
        if subject_tenant == object_tenant:
            # Same-tenant: high-performance path
            key = f"{subject_tenant}:{subject}:{permission}:{object}"
            return self._same_tenant.get(key)
        else:
            # Cross-tenant: dedicated partition with longer TTL
            key = f"{subject_tenant}:{object_tenant}:{subject}:{permission}:{object}"
            return self._cross_tenant.get(key)
```

---

## CRITICAL LESSONS FROM GITHUB ISSUES

### SeaweedFS Issues (Avoid These Mistakes)

| Issue | Problem | Root Cause | Prevention |
|-------|---------|------------|------------|
| #2325 | 45x perf regression | Over-tuning LevelDB | Use defaults first |
| #7270 | OOM from goroutine leak | Missing `defer cancel()` | Audit all async code |
| #7102 | Data resurrection | Restore without metadata check | Validate before restore |
| #5794 | Data purge on disconnect | No health check for destructive ops | Circuit breakers |
| #211 | 10x memory duplication | No streaming for concurrent reads | Stream large files |

### JuiceFS Issues (Apply These Fixes)

| Issue | Problem | Root Cause | Prevention |
|-------|---------|------------|------------|
| #145 | 5-10 ops/sec instead of 10000 | 22ms metadata latency | Keep metadata <2ms |
| #496 | 22GB memory usage | Native SDK memory leak | Use S3-compatible API |
| #182 | Write failures under load | 3s timeout too aggressive | Use 30s+ timeout |
| #5018 | Quota double-counting | Open files during delete | Handle sustained inodes |
| #132 | Wasted bandwidth | Prefetch not cancelled | Cancel on file close |

### Nexus Issues (Already Fixed)

| Issue | Status | Solution |
|-------|--------|----------|
| #380 | ✅ Fixed | Bulk permission checking |
| #682 | ✅ Fixed | Tiger Cache implementation |
| #687 | ✅ Fixed | Partial indexes for active tuples |
| #819 | ✅ Fixed | Tenant boundary security |
| #847 | ✅ Fixed | Cache trust optimization |
| #858 | ✅ Fixed | Negative caching |
| #865 | ✅ Fixed | Incremental embedding updates |

---

## DATABASE OPTIMIZATIONS

### Connection Pooling (Issue #860)

```python
# File: src/nexus/storage/connection_pool.py

from sqlalchemy.pool import QueuePool

def create_optimized_engine(database_url: str):
    return create_async_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,  # Verify connections
        pool_recycle=3600,   # Recycle hourly
        connect_args={
            "server_settings": {
                "statement_timeout": "30000",
                "idle_in_transaction_session_timeout": "60000"
            }
        }
    )
```

### Partitioning Strategy (Issue #870)

```sql
-- Partition by tenant for 100M+ files
CREATE TABLE files (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    created_at TIMESTAMP
) PARTITION BY HASH (tenant_id);

-- Create 64 partitions
CREATE TABLE files_p0 PARTITION OF files FOR VALUES WITH (modulus 64, remainder 0);
-- ... repeat for 1-63

-- Shared content index (NOT partitioned, for cross-tenant queries)
CREATE TABLE shared_content_index (
    content_hash TEXT PRIMARY KEY,
    owner_tenant_id UUID NOT NULL,
    shared_with_tenants UUID[] DEFAULT '{}',
    created_at TIMESTAMP
);

CREATE INDEX idx_shared_tenants ON shared_content_index
    USING GIN (shared_with_tenants);
```

### Missing Indexes

```sql
-- For userset lookups (partial index)
CREATE INDEX idx_rebac_userset_lookup ON rebac_tuples
    (tenant_id, relation, object_type, object_id)
    WHERE subject_relation IS NOT NULL;

-- For reverse expansion
CREATE INDEX idx_rebac_reverse ON rebac_tuples
    (tenant_id, object_type, object_id, relation);

-- For path prefix queries
CREATE INDEX idx_paths_prefix ON file_paths
    USING btree (virtual_path text_pattern_ops);
```

---

## MONITORING & METRICS

### Required Metrics

```python
# File: src/nexus/core/metrics.py

CACHE_METRICS = {
    # Permission cache
    "rebac_cache_hit_rate": Gauge,
    "rebac_cache_latency_ms": Histogram,
    "rebac_cache_size": Gauge,

    # Tiger cache
    "tiger_cache_hit_rate": Gauge,
    "tiger_cache_bitmap_size_bytes": Gauge,

    # Subproblem cache
    "subproblem_cache_hit_rate": Gauge,
    "subproblem_cache_reuse_rate": Gauge,

    # Leopard index
    "leopard_index_size": Gauge,
    "leopard_lookup_latency_ms": Histogram,
}

OPERATION_METRICS = {
    "permission_check_latency_ms": Histogram,
    "directory_list_latency_ms": Histogram,
    "file_read_latency_ms": Histogram,
    "graph_traversal_depth": Histogram,
    "graph_traversal_nodes": Histogram,
}

HEALTH_METRICS = {
    "metadata_latency_ms": Gauge,  # Alert if >10ms
    "database_connection_pool_used": Gauge,
    "memory_usage_bytes": Gauge,
    "goroutine_count": Gauge,  # Alert on unexpected growth
}
```

### Alert Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| Permission cache hit rate | <70% | <50% |
| Metadata latency | >5ms | >10ms |
| Permission check P99 | >50ms | >100ms |
| Memory per file | >150 bytes | >200 bytes |
| Goroutine count growth | >10%/hour | >50%/hour |

---

## IMPLEMENTATION PRIORITY MATRIX

### Phase 1: Quick Wins (Week 1-2)

| # | Task | Impact | Effort | Risk |
|---|------|--------|--------|------|
| 1.1 | Timestamp quantization | 10-100x cache | 1-2 days | Low |
| 1.2 | Request deduplication | 10-50x concurrent | 2-3 days | Low |
| 1.3 | Tiger Cache for single ops | 10-100x reads | 3-5 days | Low |
| 1.4 | Goroutine audit | OOM prevention | 1-2 days | None |

### Phase 2: Core (Week 3-6)

| # | Task | Impact | Effort | Risk |
|---|------|--------|--------|------|
| 2.1 | Subproblem caching | 2-3x checks | 1-2 weeks | Low |
| 2.2 | Leopard index | 50-500x groups | 2-3 weeks | Medium |
| 2.3 | BulkCheckPermission | 40x listings | 1-2 weeks | Low |
| 2.4 | Memory-efficient metadata | 3-5x memory | 2-3 weeks | Medium |
| 2.5 | Watch API | Real-time invalidation | 1-2 weeks | Low |

### Phase 3: Advanced (Week 7-12)

| # | Task | Impact | Effort | Risk |
|---|------|--------|--------|------|
| 3.1 | Volume-based storage | 13x metadata | 4-6 weeks | High |
| 3.2 | Tiered storage + EC | 42% storage | 3-4 weeks | Medium |
| 3.3 | Cross-tenant optimization | 5-10x cross | 1-2 weeks | Low |

---

## SUCCESS CRITERIA

### Phase 1 Complete When:
- [ ] Cache hit rate >85%
- [ ] Permission check (cached) <1ms
- [ ] No goroutine leaks under 24h load test
- [ ] Concurrent access deduplication working

### Phase 2 Complete When:
- [ ] Directory listing (1000 files) <100ms
- [ ] Memory per file <100 bytes
- [ ] Group membership check <1ms (Leopard)
- [ ] Real-time permission invalidation <100ms

### Phase 3 Complete When:
- [ ] Support for 100M+ files per tenant
- [ ] Storage cost reduced 40%+ for warm data
- [ ] Cross-tenant check <5ms (cached)

---

## REFERENCES

### Research Documents Created
1. `docs/research/seaweedfs-deep-technical-dive.md` - 15,000+ words
2. `docs/research/seaweedfs-github-issues-analysis.md` - 50+ issues analyzed
3. `docs/research/juicefs-issues-deep-dive.md` - Critical bugs and fixes
4. `docs/research/spicedb-deep-dive.md` - Authorization patterns
5. `docs/research/zanzibar-permission-optimizations.md` - Google's approach
6. `docs/research/distributed-filesystem-best-practices.md` - Production patterns

### External Sources
- [SeaweedFS GitHub](https://github.com/seaweedfs/seaweedfs)
- [JuiceFS GitHub](https://github.com/juicedata/juicefs)
- [SpiceDB GitHub](https://github.com/authzed/spicedb)
- [Google Zanzibar Paper](https://research.google/pubs/pub48190/)
- [Facebook Haystack Paper](https://www.usenix.org/conference/osdi10/finding-needle-haystack-facebooks-photo-storage)
- [Facebook f4 Paper](https://www.usenix.org/conference/osdi14/technical-sessions/presentation/muralidhar)
