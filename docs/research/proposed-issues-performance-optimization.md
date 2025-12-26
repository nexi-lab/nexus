# Proposed Issues: File Operations & Permission Check Optimization

**Research Sources:** JuiceFS, SpiceDB, Google Zanzibar, Nexus Codebase Analysis
**Date:** December 26, 2025
**Focus:** Same-tenant hot path optimization with cross-tenant sharing support

---

## Executive Summary

### Key Insights

| Source | Key Learning | Nexus Application |
|--------|--------------|-------------------|
| **JuiceFS** | 100 bytes/file metadata, arena memory, 45μs write latency | Memory-efficient indexing, write buffering |
| **SpiceDB** | Subproblem caching, 5s quantization, 1M QPS | Permission check decomposition, cache sharing |
| **Zanzibar** | Leopard index O(1) lookups, request hedging | Transitive closure precomputation |
| **Nexus Analysis** | Permission checks = 60% of read latency | Tiger Cache expansion, batch APIs |

### Performance Targets

| Operation | Current | Target | Improvement |
|-----------|---------|--------|-------------|
| Permission check (cached) | 10-100ms | <1ms | 10-100x |
| Permission check (cold) | 50-500ms | <10ms | 5-50x |
| Directory list (1000 files) | 500-1000ms | <50ms | 10-20x |
| Same-tenant file read | 15-20ms | <5ms | 3-4x |
| Cross-tenant file read | 40-100ms | <15ms | 3-7x |

---

## Issue 1: Timestamp Quantization for Permission Cache (P0)

**Title:** `perf: Implement timestamp quantization for 10-100x permission cache improvement`

**Labels:** `performance`, `rebac`, `caching`

**Body:**
```markdown
## Summary
Implement SpiceDB-style timestamp quantization to dramatically improve permission cache hit rates.

## Problem
Current cache keys include exact timestamps, causing cache misses even for identical permission checks made milliseconds apart.

From SpiceDB research:
- **5-second quantum** = all requests in window share cache
- **60%+ cache hit rates** in production
- **10-100x improvement** vs exact timestamps

## Current Code
```python
# rebac_cache.py - Cache key includes precise time
cache_key = f"{subject}:{permission}:{object}:{timestamp}"
```

## Proposed Solution
```python
QUANTIZATION_INTERVAL = 5  # seconds

def _quantize_timestamp(ts: float) -> int:
    """Round timestamp to quantum boundary for cache sharing."""
    return int(ts // QUANTIZATION_INTERVAL) * QUANTIZATION_INTERVAL

def _get_cache_key(subject, permission, object, timestamp=None):
    quantized = _quantize_timestamp(timestamp or time.time())
    return f"{subject}:{permission}:{object}:{quantized}"
```

## Same-Tenant Optimization
For same-tenant checks (95%+ of traffic):
- Use **tenant-specific cache partition** for isolation
- Share quantized cache across all users in tenant
- Result: Single permission computation serves all concurrent requests

```python
def _get_cache_key_tenant_aware(subject, permission, object, tenant_id):
    quantized = _quantize_timestamp(time.time())
    # Tenant-specific partition enables sharing within tenant
    return f"{tenant_id}:{subject}:{permission}:{object}:{quantized}"
```

## Cross-Tenant Handling
For cross-tenant checks:
- Include **both tenant IDs** in cache key
- Separate cache partition prevents leakage
- Slightly lower hit rate acceptable (rare path)

```python
def _get_cache_key_cross_tenant(subject, subject_tenant, permission, object, object_tenant):
    quantized = _quantize_timestamp(time.time())
    return f"cross:{subject_tenant}:{object_tenant}:{subject}:{permission}:{object}:{quantized}"
```

## Expected Impact
- Same-tenant: 10-100x cache hit improvement
- Cross-tenant: 5-10x improvement
- P95 permission latency: 10-100ms → <1ms

## References
- SpiceDB quantization: https://authzed.com/blog/how-caching-works-in-spicedb
- Related: #877 (split grant/denial caches), #878 (stampede prevention)
```

---

## Issue 2: Request Deduplication for Concurrent Permission Checks (P0)

**Title:** `perf: Implement request deduplication to prevent thundering herd`

**Labels:** `performance`, `rebac`, `concurrency`

**Body:**
```markdown
## Summary
Coalesce concurrent identical permission checks into single computation.

## Problem
When 100 users access same file simultaneously:
- Current: 100 separate permission checks (100 × 50ms = 5 seconds total compute)
- Optimal: 1 permission check, 99 waiters (50ms total)

From SpiceDB/Zanzibar:
- **Lock table** tracks in-flight requests
- **Waiters** subscribe to result instead of recomputing
- **40% reduction** in total permission computations

## Proposed Solution

```python
import asyncio
from collections import defaultdict

class PermissionDeduplicator:
    """Coalesce concurrent identical permission checks."""

    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def check_deduplicated(
        self,
        subject, permission, object, tenant_id,
        compute_fn
    ):
        cache_key = f"{tenant_id}:{subject}:{permission}:{object}"

        async with self._lock:
            if cache_key in self._in_flight:
                # Another request is computing - wait for it
                return await self._in_flight[cache_key]

            # We're the first - create future for waiters
            future = asyncio.get_event_loop().create_future()
            self._in_flight[cache_key] = future

        try:
            # Compute permission (only once)
            result = await compute_fn(subject, permission, object, tenant_id)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            async with self._lock:
                del self._in_flight[cache_key]
```

## Same-Tenant Optimization
Same-tenant requests are most likely to be concurrent (team accessing shared folder):
- **Hot path**: Deduplication key = `{tenant}:{object}`
- Team of 50 opening same folder = 1 computation instead of 50

## Integration Point
```python
# In rebac_manager.py
async def rebac_check(self, subject, permission, object, tenant_id):
    # Check L1 cache first
    if cached := self._cache.get(subject, permission, object, tenant_id):
        return cached

    # Deduplicate concurrent cache misses
    return await self._deduplicator.check_deduplicated(
        subject, permission, object, tenant_id,
        compute_fn=self._compute_permission_async
    )
```

## Expected Impact
- Concurrent access to same resource: 10-50x fewer computations
- Directory listing with N files: N parallel checks → ~sqrt(N) actual checks
- Reduced database load during traffic spikes

## References
- SpiceDB lock table: https://authzed.com/blog/hotspot-caching-in-google-zanzibar-and-spicedb
```

---

## Issue 3: Subproblem Caching for Permission Checks (P0)

**Title:** `perf: Implement subproblem caching for 2-3x permission check speedup`

**Labels:** `performance`, `rebac`, `caching`

**Body:**
```markdown
## Summary
Cache intermediate permission check results (subproblems) instead of only final results.

## Problem
Current caching only stores final result: "Can Alice read /files/report.pdf?"

But permission checks involve multiple subproblems:
1. Is Alice member of Engineering team?
2. Does Engineering team have access to /files/?
3. Is report.pdf in /files/?
4. Does /files/ permission inherit to children?

**Current**: Cache miss on any file = recompute ALL subproblems
**Optimal**: Reuse cached subproblems across different checks

## SpiceDB Insight
> "Instead of caching entire permission checks, SpiceDB breaks them into
> sub-problems. Each sub-problem cached independently enables 60%+ reuse
> across different queries."

## Proposed Solution

```python
class SubproblemCache:
    """Cache intermediate permission check results."""

    def __init__(self, ttl: int = 60):
        # Subproblem types with different TTLs
        self._membership_cache = LRUCache(maxsize=10000)  # user→group: 5min
        self._hierarchy_cache = LRUCache(maxsize=5000)    # resource→parent: 10min
        self._permission_cache = LRUCache(maxsize=10000)  # final results: 1min

    def get_membership(self, subject, group, tenant_id) -> Optional[bool]:
        """Is subject a member of group? (Frequently reused)"""
        key = f"{tenant_id}:{subject}:member:{group}"
        return self._membership_cache.get(key)

    def set_membership(self, subject, group, tenant_id, is_member: bool):
        key = f"{tenant_id}:{subject}:member:{group}"
        self._membership_cache.set(key, is_member, ttl=300)  # 5 min

    def get_hierarchy(self, resource, tenant_id) -> Optional[str]:
        """Get parent of resource (very stable, long TTL)"""
        key = f"{tenant_id}:{resource}:parent"
        return self._hierarchy_cache.get(key)
```

## Integration with Permission Check

```python
async def _compute_permission(self, subject, permission, object, tenant_id):
    # 1. Check membership subproblem cache
    groups = await self._get_subject_groups_cached(subject, tenant_id)

    # 2. Check hierarchy subproblem cache
    ancestors = await self._get_resource_ancestors_cached(object, tenant_id)

    # 3. Check permission grants on ancestors
    for ancestor in ancestors:
        for group in groups:
            # This check can also be cached!
            if await self._has_grant_cached(group, permission, ancestor, tenant_id):
                return True

    return False
```

## Same-Tenant Optimization
Within a tenant, subproblems are highly reusable:
- **Membership**: Same user's groups reused across all file checks
- **Hierarchy**: Same folder structure reused for all files in folder
- **Grants**: Team→folder permissions reused for all team members

## Cache Hit Rate Analysis

| Subproblem | Reuse Pattern | Expected Hit Rate |
|------------|---------------|-------------------|
| User memberships | Same user, any file | 90%+ |
| Resource hierarchy | Any user, same folder | 95%+ |
| Group→resource grants | Any user in group, same resource | 80%+ |
| Final permission | Same user, same file | 60%+ |

## Expected Impact
- Cold permission check: 50-500ms → 10-50ms (subproblems cached)
- Warm permission check: <1ms (final result cached)
- Overall: 2-3x fewer database queries per check

## References
- SpiceDB subproblem caching: https://authzed.com/blog/how-caching-works-in-spicedb
- Related: Tiger Cache (#682), Leopard Index (#691)
```

---

## Issue 4: BulkCheckPermission API (P0)

**Title:** `feat: Add BulkCheckPermission API for 10-100x faster file listings`

**Labels:** `performance`, `api`, `rebac`

**Body:**
```markdown
## Summary
Add batch permission check API that shares computation across multiple checks.

## Problem
Directory listing with 1000 files:
- Current: 1000 sequential permission checks = 1000 × 2ms = 2 seconds
- With batch: 1 bulk query + shared subproblems = 50ms

## SpiceDB BulkCheckPermission
```protobuf
rpc BulkCheckPermission(BulkCheckPermissionRequest)
    returns (BulkCheckPermissionResponse);

message BulkCheckPermissionRequest {
    repeated CheckPermissionRequestItem items = 1;
}
```

## Proposed API

```python
# New method in ReBACManager
async def check_permissions_bulk(
    self,
    checks: list[tuple[str, str, str]],  # [(subject, permission, object), ...]
    tenant_id: str,
    consistency: str = "eventual"  # eventual | bounded | strong
) -> dict[tuple, bool]:
    """
    Check multiple permissions in single optimized operation.

    Optimizations:
    1. Single database query for all relevant tuples
    2. Shared subproblem computation (memberships, hierarchy)
    3. Parallel graph traversal where possible
    4. Batch cache population
    """
```

## Implementation Strategy

```python
async def check_permissions_bulk(self, checks, tenant_id):
    results = {}

    # 1. Extract unique subjects and objects
    subjects = {c[0] for c in checks}
    objects = {c[2] for c in checks}

    # 2. Batch fetch all memberships (single query)
    memberships = await self._get_memberships_bulk(subjects, tenant_id)

    # 3. Batch fetch all hierarchies (single query)
    hierarchies = await self._get_hierarchies_bulk(objects, tenant_id)

    # 4. Batch fetch all relevant tuples (single query)
    tuples = await self._get_tuples_for_bulk_check(
        subjects, objects, tenant_id
    )

    # 5. Compute permissions using pre-fetched data
    for subject, permission, object in checks:
        results[(subject, permission, object)] = self._compute_with_prefetch(
            subject, permission, object,
            memberships, hierarchies, tuples
        )

    return results
```

## Same-Tenant Hot Path

For same-tenant (most common):
```python
async def list_with_permissions(self, path, user, tenant_id):
    # 1. Get all files in directory
    files = await self.metadata.list(path, tenant_id)

    # 2. Bulk permission check (FAST)
    checks = [(user, "read", f.path) for f in files]
    permissions = await self.rebac.check_permissions_bulk(checks, tenant_id)

    # 3. Filter to accessible files
    return [f for f in files if permissions.get((user, "read", f.path))]
```

## Cross-Tenant Handling

```python
async def check_permissions_bulk_cross_tenant(
    self,
    checks: list[tuple[str, str, str, str, str]],  # (subj, subj_tenant, perm, obj, obj_tenant)
):
    # Group by tenant pair for efficient querying
    by_tenant_pair = defaultdict(list)
    for check in checks:
        pair = (check[1], check[4])  # (subject_tenant, object_tenant)
        by_tenant_pair[pair].append(check)

    # Process each tenant pair
    results = {}
    for (subj_tenant, obj_tenant), group_checks in by_tenant_pair.items():
        if subj_tenant == obj_tenant:
            # Same-tenant: use optimized path
            group_results = await self.check_permissions_bulk(...)
        else:
            # Cross-tenant: check shared-* relations
            group_results = await self._check_cross_tenant_bulk(...)
        results.update(group_results)

    return results
```

## Expected Impact
- Directory listing (1000 files): 2s → 50ms (40x faster)
- Bulk file access check: Linear speedup with batch size
- Database queries: N → 3-5 (membership, hierarchy, tuples)

## References
- SpiceDB BulkCheckPermission: https://authzed.com/docs/reference/api#bulkcheckpermission
- Related: Tiger Cache (#682), filter_list optimization
```

---

## Issue 5: Leopard-Style Transitive Closure Index (P1)

**Title:** `perf: Implement Leopard-style index for O(1) group membership lookups`

**Labels:** `performance`, `rebac`, `indexing`

**Body:**
```markdown
## Summary
Implement Google Zanzibar's Leopard indexing for O(1) transitive group membership lookups.

## Problem
Checking if user is member of deeply nested group requires graph traversal:
```
User → Team → Department → Division → Company
```
Current: O(depth) database queries per check
Leopard: O(1) in-memory lookup

## Google Zanzibar Leopard System
From the paper:
> "Leopard maintains the transitive closure of all group memberships.
> A single Zanzibar tuple change can generate 10,000+ Leopard index updates."

Performance:
- **1.56M QPS median**, 2.22M QPS at P99
- Handles nested groups with 100k+ members

## Proposed Implementation

```python
class LeopardIndex:
    """
    Transitive closure index for O(1) membership lookups.

    Maintains: subject → set[all_groups_transitively_member_of]
    """

    def __init__(self):
        # In-memory index: subject → frozenset[groups]
        self._membership_closure: dict[str, frozenset[str]] = {}
        # Reverse index for invalidation: group → set[members]
        self._group_members: dict[str, set[str]] = {}
        # Last update timestamp per entry
        self._timestamps: dict[str, float] = {}

    def is_member(self, subject: str, group: str, tenant_id: str) -> bool:
        """O(1) membership check."""
        key = f"{tenant_id}:{subject}"
        if key not in self._membership_closure:
            return False  # Unknown subject, fall back to DB
        return group in self._membership_closure[key]

    def get_all_groups(self, subject: str, tenant_id: str) -> frozenset[str]:
        """Get all groups subject belongs to (transitively)."""
        key = f"{tenant_id}:{subject}"
        return self._membership_closure.get(key, frozenset())

    async def rebuild_for_subject(self, subject: str, tenant_id: str):
        """Rebuild transitive closure for a subject."""
        # BFS/DFS to find all transitive memberships
        visited = set()
        queue = [subject]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            # Get direct memberships
            direct_groups = await self._get_direct_memberships(current, tenant_id)
            queue.extend(direct_groups)

        # Store closure
        key = f"{tenant_id}:{subject}"
        self._membership_closure[key] = frozenset(visited - {subject})
```

## Incremental Updates

```python
async def on_tuple_change(self, event: TupleChangeEvent):
    """Handle tuple changes incrementally."""
    if event.relation != "member":
        return

    if event.type == "write":
        # New membership: update affected subjects
        await self._propagate_membership_add(
            event.subject, event.object, event.tenant_id
        )
    elif event.type == "delete":
        # Removed membership: rebuild affected subjects
        await self._propagate_membership_remove(
            event.subject, event.object, event.tenant_id
        )
```

## Same-Tenant Optimization

```python
class TenantAwareLeopardIndex(LeopardIndex):
    """Partition index by tenant for isolation and efficiency."""

    def __init__(self):
        # Per-tenant indexes
        self._tenant_indexes: dict[str, LeopardIndex] = {}

    def get_tenant_index(self, tenant_id: str) -> LeopardIndex:
        if tenant_id not in self._tenant_indexes:
            self._tenant_indexes[tenant_id] = LeopardIndex()
        return self._tenant_indexes[tenant_id]

    def is_member(self, subject, group, tenant_id) -> bool:
        # O(1) lookup in tenant-specific index
        return self.get_tenant_index(tenant_id).is_member(subject, group, tenant_id)
```

## Memory Estimation

| Tenant Size | Users | Groups | Index Size |
|-------------|-------|--------|------------|
| Small | 100 | 20 | ~50 KB |
| Medium | 1,000 | 100 | ~500 KB |
| Large | 10,000 | 500 | ~10 MB |
| Enterprise | 100,000 | 2,000 | ~200 MB |

## Expected Impact
- Group membership check: 5-50ms → <0.1ms (50-500x faster)
- Deeply nested groups (5+ levels): 100-500ms → <0.1ms
- Permission check with group expansion: 50-500ms → 5-10ms

## References
- Zanzibar Leopard: Section 4.3 of original paper
- Current Nexus Leopard: `rebac_manager_enhanced.py:220` (verify implementation)
```

---

## Issue 6: Watch API for Real-Time Cache Invalidation (P1)

**Title:** `feat: Implement Watch API for event-based permission cache invalidation`

**Labels:** `feature`, `rebac`, `caching`

**Body:**
```markdown
## Summary
Implement SpiceDB-style Watch API for real-time permission change notifications.

## Problem
Current cache invalidation is TTL-based:
- **Too aggressive**: 60s TTL causes unnecessary cache misses
- **Too permissive**: Stale permissions for up to 60s after change
- **No downstream notification**: Clients can't react to permission changes

## SpiceDB Watch API
```protobuf
rpc Watch(WatchRequest) returns (stream WatchResponse);

message WatchResponse {
    repeated RelationshipUpdate updates = 1;
    ZedToken changes_through = 2;  // Consistency token
}
```

## Proposed Implementation

```python
class PermissionWatcher:
    """Stream permission changes for cache invalidation."""

    def __init__(self, rebac_manager):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._changelog_position: int = 0

    async def watch(
        self,
        tenant_id: str,
        resource_types: list[str] = None,
        start_token: str = None
    ) -> AsyncIterator[PermissionChange]:
        """
        Stream permission changes for a tenant.

        Yields:
            PermissionChange events with (subject, relation, object, operation)
        """
        position = self._parse_token(start_token) if start_token else 0

        while True:
            # Poll changelog for new entries
            changes = await self._get_changes_since(tenant_id, position)

            for change in changes:
                if resource_types and change.object_type not in resource_types:
                    continue

                yield PermissionChange(
                    subject=change.subject,
                    relation=change.relation,
                    object=change.object,
                    operation=change.operation,  # "write" | "delete"
                    token=self._make_token(change.id)
                )
                position = change.id

            if not changes:
                await asyncio.sleep(0.1)  # Poll interval
```

## Cache Invalidation Integration

```python
class WatchBasedCacheInvalidator:
    """Invalidate caches based on Watch events."""

    def __init__(self, cache, watcher):
        self._cache = cache
        self._watcher = watcher

    async def run(self, tenant_id: str):
        async for change in self._watcher.watch(tenant_id):
            # Invalidate affected cache entries
            await self._invalidate_for_change(change)

    async def _invalidate_for_change(self, change: PermissionChange):
        # 1. Invalidate direct permission cache
        self._cache.invalidate(change.subject, "*", change.object)

        # 2. Invalidate subproblem caches
        if change.relation == "member":
            # Membership changed - invalidate Leopard index
            await self._leopard.rebuild_for_subject(change.subject)

        # 3. Invalidate Tiger Cache bitmaps
        await self._tiger.queue_rebuild(change.object)

        # 4. Notify downstream subscribers
        await self._notify_subscribers(change)
```

## Same-Tenant Optimization

```python
# Per-tenant Watch streams for isolation
class TenantWatchManager:
    async def get_watch_stream(self, tenant_id: str):
        # Each tenant has dedicated watch stream
        # Changes in tenant A don't wake up tenant B's stream
        return self._watcher.watch(tenant_id)
```

## Cross-Tenant Considerations

```python
# Cross-tenant changes need dual notification
async def on_cross_tenant_share(self, change):
    # Notify object owner's tenant
    await self._notify_tenant(change.object_tenant_id, change)
    # Also notify subject's tenant
    await self._notify_tenant(change.subject_tenant_id, change)
```

## Expected Impact
- Cache invalidation latency: 60s (TTL) → <100ms (event-based)
- False cache invalidations: Eliminated (precise invalidation)
- Enables: Real-time permission UIs, audit logging, compliance

## References
- SpiceDB Watch: https://authzed.com/docs/spicedb/concepts/watch
- Related: rebac_changelog table, cache invalidation
```

---

## Issue 7: Tiger Cache Expansion for Single-File Operations (P0)

**Title:** `perf: Extend Tiger Cache to single-file permission checks`

**Labels:** `performance`, `rebac`, `caching`

**Body:**
```markdown
## Summary
Expand Tiger Cache (Roaring Bitmaps) usage from list operations to all permission checks.

## Current State
From Nexus analysis:
- Tiger Cache provides **10-100x speedup** for directory listings
- But single-file reads still use slow graph traversal (10-100ms)
- **Permission checks = 60% of read latency**

## Problem
```python
# Current: Tiger Cache only used in filter_list()
def list(path, user):
    files = metadata.list(path)
    return tiger_cache.filter(files, user, "read")  # Fast!

# But read() still does slow check
def read(path, user):
    if not rebac_check(user, "read", path):  # Slow: 10-100ms
        raise PermissionError()
    return content
```

## Proposed Solution

```python
class UnifiedPermissionChecker:
    """Use Tiger Cache for ALL permission checks."""

    def __init__(self, tiger_cache, rebac_manager):
        self._tiger = tiger_cache
        self._rebac = rebac_manager

    async def check(self, subject, permission, object, tenant_id) -> bool:
        # 1. Try Tiger Cache first (O(1))
        tiger_result = self._tiger.check_cached(
            subject, permission, object, tenant_id
        )
        if tiger_result is not None:
            return tiger_result  # Cache hit: <1ms

        # 2. Fall back to ReBAC computation
        result = await self._rebac.check(subject, permission, object, tenant_id)

        # 3. Update Tiger Cache for future checks
        await self._tiger.update_single(subject, permission, object, tenant_id, result)

        return result
```

## Tiger Cache Enhancement

```python
class EnhancedTigerCache:
    """Extended Tiger Cache with single-resource support."""

    def check_cached(self, subject, permission, object, tenant_id) -> Optional[bool]:
        """Check if permission is in bitmap cache."""
        # Get subject's permission bitmap
        bitmap_key = f"{tenant_id}:{subject}:{permission}"
        bitmap = self._cache.get(bitmap_key)

        if bitmap is None:
            return None  # Cache miss

        # Get object's integer ID
        object_id = self._resource_map.get_id(object, tenant_id)
        if object_id is None:
            return None  # Object not in map

        # O(1) bitmap membership check
        return object_id in bitmap

    async def update_single(self, subject, permission, object, tenant_id, has_permission: bool):
        """Update bitmap for single permission result."""
        bitmap_key = f"{tenant_id}:{subject}:{permission}"

        # Get or create bitmap
        bitmap = self._cache.get(bitmap_key) or RoaringBitmap()

        # Get or assign object ID
        object_id = await self._resource_map.get_or_create_id(object, tenant_id)

        # Update bitmap
        if has_permission:
            bitmap.add(object_id)
        else:
            bitmap.discard(object_id)

        # Save back
        self._cache.set(bitmap_key, bitmap, ttl=300)
```

## Same-Tenant Hot Path

```python
# Most checks are same-tenant - optimize this path
async def check_same_tenant(self, subject, permission, object, tenant_id):
    # 1. Tiger Cache check (in-memory, O(1))
    if result := self._tiger.check_cached(subject, permission, object, tenant_id):
        return result

    # 2. Subproblem cache check
    if result := self._subproblem.check_cached(subject, permission, object, tenant_id):
        return result

    # 3. Fall back to full computation (rare for warm cache)
    return await self._compute_and_cache(...)
```

## Expected Impact
- Single-file read permission: 10-100ms → <1ms (after warm-up)
- Permission check proportion of read latency: 60% → 10%
- Overall read performance: 15-20ms → 5-8ms

## References
- Current Tiger Cache: `tiger_cache.py:407-963`
- Related: #682 (Tiger Cache implementation)
```

---

## Issue 8: JuiceFS-Inspired Memory-Efficient Metadata (P1)

**Title:** `perf: Implement memory-efficient metadata indexing (JuiceFS-inspired)`

**Labels:** `performance`, `metadata`, `memory`

**Body:**
```markdown
## Summary
Implement JuiceFS-style memory optimizations for metadata handling at scale.

## JuiceFS Achievements
- **100 bytes per file** (vs HDFS 370 bytes, CephFS 2,700 bytes)
- **300M files in 30 GiB memory** (single instance)
- **100μs average metadata latency**

Key techniques:
1. Arena memory management (bypass GC)
2. Directory compression (50-67% reduction)
3. Lock-free single-threaded metadata

## Current Nexus State
From analysis:
- Path metadata: ~200-500 bytes per file (Python objects)
- 1M files = 200-500 MB metadata memory
- GC pressure increases with file count

## Proposed Optimizations

### 1. Compact Metadata Representation

```python
import struct
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class CompactFileMetadata:
    """
    Minimal metadata representation: 64 bytes fixed.

    Layout:
    - path_hash: 8 bytes (uint64)
    - content_hash: 32 bytes (SHA-256)
    - size: 8 bytes (uint64)
    - mtime: 8 bytes (uint64, unix timestamp)
    - flags: 8 bytes (permissions, type, etc.)
    """
    path_hash: int
    content_hash: bytes
    size: int
    mtime: int
    flags: int

    def to_bytes(self) -> bytes:
        return struct.pack(
            '>Q32sQQQ',
            self.path_hash,
            self.content_hash,
            self.size,
            self.mtime,
            self.flags
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> 'CompactFileMetadata':
        path_hash, content_hash, size, mtime, flags = struct.unpack(
            '>Q32sQQQ', data
        )
        return cls(path_hash, content_hash, size, mtime, flags)
```

### 2. Path Interning

```python
class PathInterner:
    """
    Intern path strings to reduce memory duplication.

    Many paths share prefixes: /workspace/project/src/...
    Intern common prefixes to save memory.
    """

    def __init__(self):
        self._interned: dict[str, str] = {}
        self._prefix_cache: dict[str, list[str]] = {}

    def intern(self, path: str) -> str:
        """Return interned version of path."""
        if path in self._interned:
            return self._interned[path]

        # Try to reuse prefix
        parts = path.rsplit('/', 1)
        if len(parts) == 2:
            prefix, name = parts
            interned_prefix = self.intern(prefix)
            interned_path = f"{interned_prefix}/{name}"
        else:
            interned_path = path

        self._interned[path] = interned_path
        return interned_path
```

### 3. Directory Compression

```python
class CompressedDirectoryListing:
    """
    Compress directory listings for memory efficiency.

    JuiceFS achieves 50-67% compression via:
    1. Common prefix elimination
    2. Serialization to bytes
    3. Optional zstd compression
    """

    def __init__(self, entries: list[str]):
        # Find common prefix
        self._prefix = os.path.commonpath(entries) if entries else ""

        # Store only suffixes
        self._suffixes = [e[len(self._prefix):].lstrip('/') for e in entries]

        # Compress if beneficial
        if len(self._suffixes) > 100:
            self._compressed = zstd.compress(
                '\n'.join(self._suffixes).encode()
            )
            self._suffixes = None

    def __iter__(self):
        if self._compressed:
            suffixes = zstd.decompress(self._compressed).decode().split('\n')
        else:
            suffixes = self._suffixes

        for suffix in suffixes:
            yield f"{self._prefix}/{suffix}" if suffix else self._prefix
```

## Same-Tenant Memory Isolation

```python
class TenantMetadataPartition:
    """
    Per-tenant metadata partition for isolation and efficiency.

    Benefits:
    - Tenant deletion = drop partition (instant)
    - No cross-tenant interference
    - Separate LRU eviction per tenant
    """

    def __init__(self, tenant_id: str, max_memory: int):
        self.tenant_id = tenant_id
        self.max_memory = max_memory
        self._metadata = CompactMetadataStore()
        self._path_interner = PathInterner()
```

## Expected Impact
- Memory per file: 200-500 bytes → 64-100 bytes (3-5x reduction)
- 10M files: 2-5 GB → 640 MB - 1 GB
- GC pressure: Significantly reduced with slots/frozen dataclasses

## References
- JuiceFS memory optimization: https://juicefs.com/docs/community/performance_evaluation
- Related: #870 (database partitioning)
```

---

## Issue 9: Write Buffering for Batch Performance (P2)

**Title:** `perf: Implement write buffering for batch write optimization (JuiceFS-inspired)`

**Labels:** `performance`, `storage`, `writes`

**Body:**
```markdown
## Summary
Implement JuiceFS-style write buffering for improved batch write performance.

## JuiceFS Write Performance
- **45μs buffer write latency**
- Writeback mode: commit first, upload async
- Automatic compaction for fragmented writes

## Current Nexus Write Path
```
write() → permission_check (10ms) → CAS_check (5ms) →
         backend_write (10-20ms) → metadata_update (10-20ms)
Total: 30-50ms per write
```

## Proposed Write Buffer

```python
class WriteBuffer:
    """
    Buffer writes for batch commit.

    Benefits:
    1. Single permission check for batch
    2. Batch metadata updates
    3. Reduced transaction overhead
    """

    def __init__(self, flush_interval: float = 0.1, max_size: int = 100):
        self._buffer: list[PendingWrite] = []
        self._flush_interval = flush_interval
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def write(self, path: str, content: bytes, context: OperationContext) -> Future:
        """Buffer a write, return future for completion."""
        future = asyncio.get_event_loop().create_future()

        async with self._lock:
            self._buffer.append(PendingWrite(path, content, context, future))

            if len(self._buffer) >= self._max_size:
                await self._flush()

        return future

    async def _flush(self):
        """Flush buffered writes as batch."""
        if not self._buffer:
            return

        writes = self._buffer
        self._buffer = []

        try:
            # 1. Batch permission check
            paths = [w.path for w in writes]
            permissions = await self._rebac.check_permissions_bulk(
                [(w.context.user, "write", w.path) for w in writes],
                writes[0].context.tenant_id
            )

            # 2. Filter permitted writes
            permitted = [w for w in writes if permissions.get((..., w.path))]

            # 3. Batch backend writes
            results = await self._backend.write_batch([
                (w.path, w.content) for w in permitted
            ])

            # 4. Batch metadata updates
            await self._metadata.put_batch([...])

            # 5. Resolve futures
            for w in permitted:
                w.future.set_result(results[w.path])

        except Exception as e:
            for w in writes:
                w.future.set_exception(e)
```

## Same-Tenant Batch Optimization

```python
# Group writes by tenant for efficient batching
class TenantAwareWriteBuffer:
    def __init__(self):
        self._buffers: dict[str, WriteBuffer] = {}

    async def write(self, path, content, context):
        tenant_id = context.tenant_id
        if tenant_id not in self._buffers:
            self._buffers[tenant_id] = WriteBuffer()
        return await self._buffers[tenant_id].write(path, content, context)
```

## Expected Impact
- Batch write (100 files): 3-5s → 200-500ms (6-10x faster)
- Write latency (buffered): 30-50ms → <1ms (return from buffer)
- Actual commit: Async, batched

## References
- JuiceFS writeback: https://juicefs.com/docs/community/cache
- Current write_batch: `metadata_store.py:1251-1420`
```

---

## Issue 10: Cross-Tenant Permission Optimization (P1)

**Title:** `perf: Optimize cross-tenant permission checks with dedicated cache`

**Labels:** `performance`, `rebac`, `multi-tenancy`

**Body:**
```markdown
## Summary
Implement optimized cross-tenant permission checking for shared resources.

## Current Cross-Tenant Flow
```python
# Cross-tenant check requires:
# 1. Verify subject exists in subject_tenant
# 2. Verify object exists in object_tenant
# 3. Check for shared-* relation in object_tenant
# 4. Validate cross-tenant grant is active

# Result: 2x queries vs same-tenant
```

## Proposed Optimization

### 1. Cross-Tenant Grant Index

```python
class CrossTenantGrantIndex:
    """
    Specialized index for cross-tenant shares.

    Optimized for the cross-tenant check pattern:
    "Can user from tenant A access resource in tenant B?"
    """

    def __init__(self):
        # Index: (object_tenant, object) → list[(subject_tenant, subject, relation)]
        self._grants_by_object: dict[tuple, list[tuple]] = {}
        # Reverse: (subject_tenant, subject) → list[(object_tenant, object, relation)]
        self._grants_by_subject: dict[tuple, list[tuple]] = {}

    def has_cross_tenant_access(
        self,
        subject: str,
        subject_tenant: str,
        permission: str,
        object: str,
        object_tenant: str
    ) -> Optional[bool]:
        """O(1) cross-tenant permission check."""
        key = (object_tenant, object)
        grants = self._grants_by_object.get(key, [])

        for grant_subj_tenant, grant_subj, relation in grants:
            if grant_subj_tenant == subject_tenant and grant_subj == subject:
                if self._relation_implies_permission(relation, permission):
                    return True

        return None  # Not found in index, fall back to DB
```

### 2. Optimized Cross-Tenant Check

```python
async def check_cross_tenant(
    self,
    subject: str,
    subject_tenant: str,
    permission: str,
    object: str,
    object_tenant: str
) -> bool:
    # 1. Check dedicated cross-tenant index (O(1))
    if result := self._cross_tenant_index.has_cross_tenant_access(
        subject, subject_tenant, permission, object, object_tenant
    ):
        return result

    # 2. Check if subject has transitive access via group in object_tenant
    # (e.g., user added to external team that has access)
    if result := await self._check_transitive_cross_tenant(
        subject, subject_tenant, permission, object, object_tenant
    ):
        return result

    # 3. Fall back to full ReBAC check
    return await self._rebac.check_with_cross_tenant(...)
```

### 3. Cross-Tenant Cache Partitioning

```python
class CrossTenantCache:
    """
    Separate cache partition for cross-tenant permissions.

    Benefits:
    - Doesn't pollute same-tenant cache
    - Different TTL (longer, cross-tenant grants change rarely)
    - Separate metrics for monitoring
    """

    def __init__(self):
        self._cache = LRUCache(maxsize=10000)
        self._ttl = 600  # 10 minutes (grants change rarely)

    def get(self, subject_tenant, subject, permission, object_tenant, object):
        key = f"{subject_tenant}:{subject}:{permission}:{object_tenant}:{object}"
        return self._cache.get(key)

    def set(self, subject_tenant, subject, permission, object_tenant, object, result):
        key = f"{subject_tenant}:{subject}:{permission}:{object_tenant}:{object}"
        self._cache.set(key, result, ttl=self._ttl)
```

## Same-Tenant Hot Path Preserved

```python
async def check(self, subject, permission, object, subject_tenant, object_tenant=None):
    # Default: same-tenant (95%+ of checks)
    if object_tenant is None or object_tenant == subject_tenant:
        return await self._check_same_tenant(subject, permission, object, subject_tenant)

    # Cross-tenant: use specialized path
    return await self._check_cross_tenant(
        subject, subject_tenant, permission, object, object_tenant
    )
```

## Expected Impact
- Cross-tenant check (cached): 40-100ms → <5ms
- Cross-tenant check (cold): 100-200ms → 20-40ms
- Same-tenant path: Unchanged (already optimized)

## References
- SpiceDB multi-tenancy: https://authzed.com/blog/multi-tenancy-patterns
- Current cross-tenant: `rebac_manager_tenant_aware.py`
```

---

## Summary: Implementation Priority

### Phase 1: Quick Wins (1-2 weeks) - P0

| Issue | Title | Impact | Effort |
|-------|-------|--------|--------|
| #1 | Timestamp Quantization | 10-100x cache | Low |
| #2 | Request Deduplication | 10-50x concurrent | Medium |
| #4 | BulkCheckPermission API | 40x list speed | Medium |
| #7 | Tiger Cache for Single Ops | 10-100x reads | Low |

### Phase 2: Core Optimizations (2-4 weeks) - P1

| Issue | Title | Impact | Effort |
|-------|-------|--------|--------|
| #3 | Subproblem Caching | 2-3x checks | Medium |
| #5 | Leopard Index | 50-500x groups | High |
| #6 | Watch API | Real-time invalidation | High |
| #10 | Cross-Tenant Optimization | 5-10x cross-tenant | Medium |

### Phase 3: Advanced (4-8 weeks) - P2

| Issue | Title | Impact | Effort |
|-------|-------|--------|--------|
| #8 | Memory-Efficient Metadata | 3-5x memory | High |
| #9 | Write Buffering | 6-10x batch writes | Medium |

---

## Cross-Tenant vs Same-Tenant Summary

| Aspect | Same-Tenant (95%) | Cross-Tenant (5%) |
|--------|-------------------|-------------------|
| **Cache partition** | Per-tenant | Separate cross-tenant |
| **TTL** | 60s (changes often) | 600s (grants stable) |
| **Index** | Tiger Cache bitmaps | Cross-tenant grant index |
| **Optimization** | Hot path, all optimizations | Dedicated checks |
| **Target latency** | <1ms cached, <10ms cold | <5ms cached, <40ms cold |
