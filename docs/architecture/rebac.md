# ReBAC Architecture: Manager Hierarchy

## Overview

Nexus has **three** ReBAC manager classes in an inheritance hierarchy:

```
ReBACManager (base)
    ↓ inherits
TenantAwareReBACManager (adds tenant isolation)
    ↓ inherits
EnhancedReBACManager (adds consistency + graph limits)
```

---

## 1. ReBACManager (Base Class)

**File:** `src/nexus/core/rebac_manager.py`

**Purpose:** Core Zanzibar-style ReBAC implementation

**Features:**
- ✅ Direct tuple checks
- ✅ Graph traversal (union, tupleToUserset)
- ✅ Caching with TTL
- ✅ Cycle detection
- ✅ Max depth limits
- ✅ Expiring tuples
- ✅ Namespace configs
- ✅ Expand API

**Used By:**
- `permissions.py` - Permission enforcer
- `memory_permission_enforcer.py` - Memory permissions
- `rebac_manager_tenant_aware.py` (parent)
- `sdk/__init__.py` - Python SDK

**Limitations:**
- ❌ No tenant isolation enforcement
- ❌ No consistency levels
- ❌ No graph limits/DoS protection
- ❌ No traversal statistics

---

## 2. TenantAwareReBACManager (Tenant Isolation)

**File:** `src/nexus/core/rebac_manager_tenant_aware.py`

**Purpose:** Adds mandatory tenant scoping for multi-zone security

**Additional Features:**
- ✅ **P0-2: Zone ID validation** - All checks require `zone_id`
- ✅ **Tenant-scoped queries** - All tuple queries filtered by `zone_id`
- ✅ **Cross-tenant relationship prevention** - Rejects tuples spanning tenants
- ✅ **Tenant-scoped cache** - Cache keys include `zone_id`

**API Changes:**
```python
# ReBACManager (no tenant required)
rebac_check(subject, permission, object)

# TenantAwareReBACManager (zone_id required)
rebac_check(subject, permission, object, zone_id)  # Raises if zone_id missing
```

**Used By:**
- `rebac_manager_enhanced.py` (parent)

**Key Difference:**
- **ReBACManager**: Optional `zone_id` in tuples, optional in checks
- **TenantAwareReBACManager**: Mandatory `zone_id` for all operations

---

## 3. EnhancedReBACManager (Full Production Features)

**File:** `src/nexus/core/rebac_manager_enhanced.py`

**Purpose:** GA-ready ReBAC with consistency guarantees and DoS protection

**Additional Features:**
- ✅ **P0-1: Consistency levels** - EVENTUAL, BOUNDED, STRONG
- ✅ **Version tokens** - Monotonic consistency tokens for each check
- ✅ **P0-5: Graph limits** - Prevent DoS attacks
  - Max depth (10)
  - Max fan-out (1000 edges per union)
  - Timeout (100ms hard limit)
  - Max visited nodes (10k memory bound)
  - Max DB queries (100 per check)
- ✅ **Traversal statistics** - Query counts, cache hit/miss, timing
- ✅ **Detailed check results** - `CheckResult` with metadata

**API Enhancements:**
```python
# Simple check (returns bool)
allowed = manager.rebac_check(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "doc.txt"),
    zone_id="org_123",
    # Always uses cached consistency (no consistency parameter needed)
)

# Detailed check (returns CheckResult with metadata)
result = manager.rebac_check_detailed(...)
# result.allowed (bool)
# result.consistency_token (str)
# result.decision_time_ms (float)
# result.cached (bool)
# result.cache_age_ms (float | None)
# result.traversal_stats (TraversalStats)
```

**Used By:**
- `nexus_fs.py` - Main NexusFS class (production use)

**Key Difference:**
- **TenantAwareReBACManager**: Tenant isolation only
- **EnhancedReBACManager**: Tenant isolation + consistency + DoS protection

---

## Which Manager Should You Use?

### Use `ReBACManager` if:
- ❌ **DON'T USE IN PRODUCTION** (no tenant isolation)
- ✅ Single-tenant deployments (testing/dev only)
- ✅ You handle tenant isolation at a higher layer

### Use `TenantAwareReBACManager` if:
- ✅ Multi-zone system
- ✅ You need tenant isolation enforcement
- ❌ Don't need consistency levels
- ❌ Don't need DoS protection

### Use `EnhancedReBACManager` if:
- ✅ **PRODUCTION DEPLOYMENTS** (recommended)
- ✅ Multi-zone system
- ✅ Need consistency guarantees
- ✅ Need DoS protection
- ✅ Need observability (traversal stats)

---

## Current Usage in Nexus

```python
# Production (nexus_fs.py)
from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

self.rebac_manager = EnhancedReBACManager(engine)

# SDK (sdk/__init__.py) - SHOULD BE UPGRADED
from nexus.core.rebac_manager import ReBACManager  # ⚠️ No tenant isolation!

self.rebac = ReBACManager(engine)

# Tests (tests/unit/test_rebac.py)
from nexus.core.rebac_manager import ReBACManager  # ✅ OK for unit tests
```

---

## Relationship to Our Changes

### Where We Made Changes:

**✅ `rebac_manager.py` (ReBACManager)**
- Fixed bugs (cache invalidation, expires_at)
- Added intersection/exclusion
- Added userset-as-subject (partial)
- Added batch check (planned)
- Added wildcard support (planned)

### What Needs Propagation:

Since `TenantAwareReBACManager` and `EnhancedReBACManager` **inherit** from `ReBACManager`, they automatically get:
- ✅ Bug fixes (cache invalidation, expires_at)
- ✅ Intersection/exclusion support
- ✅ Userset-as-subject support
- ✅ Batch check (when added)
- ✅ Wildcard support (when added)

**No changes needed** to the child classes! They inherit everything.

---

## Migration Path

### Phase 1: Base Layer (DONE/IN PROGRESS)
- ✅ Fix bugs in `ReBACManager`
- 🚧 Add new features to `ReBACManager`
- ✅ Update tests for `ReBACManager`

### Phase 2: Propagation (AUTOMATIC)
- ✅ Child classes inherit fixes/features automatically
- ⚠️ Need to test `TenantAwareReBACManager` with new features
- ⚠️ Need to test `EnhancedReBACManager` with new features

### Phase 3: SDK/CLI Updates (TODO)
- Update SDK to use `EnhancedReBACManager` (security improvement)
- Update CLI commands to support new features
- Add examples for intersection/exclusion/userset-as-subject

---

## Recommendation: SDK Security Issue

**🔴 CRITICAL:** The SDK currently uses `ReBACManager` without tenant isolation:

```python
# sdk/__init__.py:116
from nexus.core.rebac_manager import ReBACManager  # ⚠️ INSECURE

self.rebac = ReBACManager(engine)
```

**Should be:**
```python
from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

self.rebac = EnhancedReBACManager(engine)
```

**Impact:**
- SDK users can bypass tenant isolation
- No DoS protection on SDK-level rebac operations
- No consistency guarantees

**Fix Priority:** P0 (before GA)

---

## Summary

| Feature | ReBACManager | TenantAwareReBACManager | EnhancedReBACManager |
|---------|--------------|-------------------------|----------------------|
| **Core ReBAC** | ✅ | ✅ (inherited) | ✅ (inherited) |
| **Tenant isolation** | ❌ | ✅ | ✅ (inherited) |
| **Consistency levels** | ❌ | ❌ | ✅ |
| **Graph limits** | ❌ | ❌ | ✅ |
| **Traversal stats** | ❌ | ❌ | ✅ |
| **Production ready** | ❌ | ⚠️ | ✅ |
| **Our changes apply to** | ✅ | ✅ (inherited) | ✅ (inherited) |

**Bottom line:** Our changes to `ReBACManager` automatically improve all three classes! 🎉

---

## Permission caches, TTLs and consistency (Issue #4739)

A permission decision (`check`, and the `filter_list` pass behind `list`,
`glob`, `grep` and search) can be answered by several caches before the tuple
store is consulted. Each cache has its own TTL and its own invalidation
trigger, so the window between a tuple change and every surface reflecting it
is the *longest* of the rows below unless the caller opts into
`consistency=strong`.

### Cache matrix

| Layer | What it caches | Default TTL (env) | Written by | Invalidated by | Bypassed by `consistency=strong` |
|---|---|---|---|---|---|
| **Owner fast-path** (`PermissionEnforcer.check_owner`) | Nothing cached — compares `metadata.owner_id` to the subject on single-path `read`/`write`/`delete` | n/a | Kernel stamps `owner_id` on create | n/a | No (authoritative) |
| **Permission lease** (`PermissionLeaseTable`) | A successful single-path check for `(path, agent_id)`; only agents (`context.agent_id`) get leases | 30 s | `RebacPermissionCheckHook` after a full check | `rebac_delete` → `CacheCoordinator.invalidate_for_write` (path-targeted for direct grants, zone-wide for group/inherited); agent termination | Yes |
| **L1 result cache** (`ReBACPermissionCache`) | `(subject, permission, object, zone) → bool` | grants 300 s `NEXUS_CACHE_PERMISSION_TTL`; denials 60 s `NEXUS_CACHE_DENIAL_TTL`; tiered: owner 3600 s, editor/viewer 600 s, inherited 300 s | `rebac_check` (base path), `rebac_check_bulk`, eager recompute | Every `rebac_write` / `rebac_delete` for the touched subject and object (plus object prefix for file relations) | Yes |
| **Boundary cache** (`PermissionBoundaryCache`) | Nearest ancestor that granted `permission` on a file path | process lifetime, revalidated against a direct-grant lookup on hit | `_check_rebac_*` when a parent granted | `rebac_write` / `rebac_delete` via boundary invalidators | Yes |
| **Tiger bitmap** (`TigerCache`, PostgreSQL only) | Per `(subject, permission, resource_type)` roaring bitmap of accessible resources; L1 in-process, L2 Dragonfly, L3 table | 3600 s `NEXUS_CACHE_TIGER_TTL` (Dragonfly key TTL; L3 rows have none) | Write-through on `rebac_write` (`persist_single_grant`, Leopard-style directory expansion to descendants), background updater | `rebac_delete`: `persist_single_revoke` for the exact object; for a **directory** grant also `remove_directory_grant` + `tiger_invalidate_cache(subject, permission)` (drops L1+L2+L3); `invalidate_for_write` evicts L1/L2 for the subject in every process | Yes (`check`, `filter_list`, search predicate pushdown, directory visibility) |
| **Bitmap completeness / Leopard dir index** (`PermissionCacheCoordinator`) | "This subject's bitmap is complete" and accessible-directory sets used by `filter_list` | 3600 s | `BulkReBACStrategy`, `HierarchyPreFilterStrategy` | `PermissionCacheCoordinator.invalidate` | Yes (strong chain skips both strategies) |
| **Zone graph cache** | Tuple graph snapshot per zone for the Rust evaluator | until next write | graph loader | Every `rebac_write` / `rebac_delete` in the zone | n/a (always refreshed on write) |
| **Deferred permission buffer** (`DeferredPermissionBuffer`) | Not a cache: hierarchy (`parent`) tuples waiting to be written | flushed every `PermissionConfig.deferred_flush_interval` (50 ms) or at 1000 items | `DeferredPermissionHook` on write/mkdir/write_batch | n/a | n/a — see "sync owner grant" |

### Sync owner grant (read-your-writes for the creator)

With `PermissionConfig.enable_deferred=True` (default) the hierarchy tuples for
a new file are still batched, but since #4739 the creating subject's
`direct_owner` tuple is written **synchronously** by `DeferredPermissionHook`
(`rebac_write`, or `rebac_write_batch` for `write_batch`). The writer's own
`check`, `list`, `glob`, `grep` and search therefore see the file as soon as
the write returns; no flush or cache expiry is involved. If the synchronous
write fails, the grant falls back to the deferred queue and the write result
carries a `degraded` warning (`component=deferred_permission`).

`PermissionConfig.sync_owner_grant=False` (`NEXUS_SYNC_OWNER_GRANT=false`)
restores the pre-#4739 behaviour (owner grant deferred with the hierarchy
tuples, up to `deferred_flush_interval` plus a possible 60 s cached denial
before the writer's `list`/`search` see the file).

**Admin-bypass writers get no creator grant.** With
`PermissionConfig.allow_admin_bypass=True` an admin subject bypasses every
check, so its `direct_owner` tuple is never consulted — but writing it costs a
`rebac_write`, three Tiger write-throughs (`read`/`write`/`execute` bitmaps)
and a zone-graph invalidation per new file: measured at 130–190 ms per
`files/write` against a remote Postgres, plus three tuple rows per file for a
tenant that writes everything with an admin key. Before #4739 the
subprocess-kernel deployment never wrote these grants (`is_new` was hard-coded
False), so the orchestrator now constructs `DeferredPermissionHook` with
`skip_admin_owner_grant=True` whenever `allow_admin_bypass` is on. Hierarchy
tuples and non-admin writers are unchanged. Set
`PermissionConfig.owner_grant_admin_bypass=True`
(`NEXUS_OWNER_GRANT_ADMIN_BYPASS=true`) to keep writing the admin tuple under
bypass — for example when bypass may later be switched off and admins must
keep access to what they created. Without `allow_admin_bypass` the flag is
inert: an admin is an ordinary subject and needs the tuple.

The hook keys the grant on `WriteHookContext.is_new_file`. Over the kernel
RPC the `WriteResponse` proto carries no `is_new`, so the Python kernel client
derives it from the kernel's generation counter (`gen == 1` on the first write
to a path, see `rust/kernel/src/kernel/io.rs`). Before #4739 the client
hard-coded `is_new=False`, which silently disabled every creator grant in the
subprocess-kernel deployment. The kernel also does not stamp
`FileMetadata.owner_id` on write, so the O(1) owner fast-path never applies
there; the creator's access comes from this tuple.

### `consistency=strong`

`strong` is the SpiceDB `fully_consistent` analog: for that one call no cached
*allow* or *deny* may answer; the decision is resolved from the tuple store and
the fresh result is written back to the caches (so a strong read also repairs a
stale entry). `eventual` (the default) keeps today's cached behaviour.

| Surface | How to request strong |
|---|---|
| HTTP (every route that builds an `OperationContext`: files, list/glob/grep, search, RPC) | `X-Nexus-Consistency: strong` header. Invalid values → `400`. |
| `rebac_check` RPC / `ReBACService.rebac_check` / `rebac_check_sync` | `consistency="strong"` parameter (defaults to `context.consistency` for an `OperationContext`) |
| `rebac_check_batch` RPC / `ReBACService.rebac_check_batch` / `rebac_check_batch_sync` | `consistency="strong"` parameter (applies to every check in the batch) |
| `ReBACManager.rebac_check` / `rebac_check_bulk` / `rebac_check_batch` / `rebac_check_batch_fast` | `consistency="strong"` keyword |
| `NexusFS.sys_readdir` (behind `/v2/files/list`) | `OperationContext.consistency = "strong"` (non-admin callers; see "Listing surfaces") |
| `PermissionEnforcer.check` / `filter_list` / `has_accessible_descendants*` | `OperationContext.consistency = "strong"` |
| `PermissionEnforcer.filter_search_results` | `consistency="strong"` keyword |
| `SearchService.list` | `OperationContext.consistency = "strong"` (skips Tiger predicate pushdown) |

Cost: a strong `check` is one graph evaluation (Rust) instead of a cache hit;
a strong `list` of N paths is one `rebac_check_bulk` over N paths plus
ancestors instead of a bitmap scan. Use it for read-after-write and
post-revocation verification, not for hot paths.

### Revocation bound

`rebac_delete` invalidates, in this order: zone graph → `CacheCoordinator.invalidate_for_write`
(L1, Tiger L1/L2 eviction, permission leases, boundary and directory-visibility
caches, iterator cache, DT_STREAM / Pub/Sub / durable-stream hints for other
processes and zones) → Tiger write-through revoke (and bitmap drop for
directory grants) → Leopard closure → L1 subject/object. A revoked subject is
therefore denied on the next `check`, and excluded from the next `list` /
`search`, as soon as the delete returns in the deleting process; other
processes converge when they consume the stream/Pub/Sub hint (or, without a
Dragonfly/NATS bus, on the L1/Tiger TTLs above). `rebac_write` does not run the
coordinator path: a new tuple cannot make a cached allow wrong, so grants keep
the cheap write-through path.

The server-side distributed lease invalidator (`DistributedLeaseManager`,
registered in `server/lifespan/permissions.py`) is one of those lease
callbacks: on every revoke it schedules a zone-wide sweep of `lease:<zone>:*`
keys in Dragonfly. Before #4739 its callback took a single argument and raised
on every notification, so it never actually ran.

### Listing surfaces

`sys_readdir` (the kernel syscall behind `/v2/files/list`, the CLI listing and
`NexusFS.sys_readdir`) applied zone filtering only until #4739, so a non-admin
key could enumerate the names of files it cannot read. It now runs the same
ReBAC post-filter as the search service for non-admin, non-system callers:
files through `PermissionEnforcer.filter_list` (which honours
`OperationContext.consistency`), directories through
`has_accessible_descendants_batch` (visible when the caller can reach a
descendant; indeterminate directories stay visible, matching `SearchService`).
Admin and system listings are unchanged here; admin zone scoping is #4740. If
enforcement is on but the permission enforcer is unavailable the listing is
empty (fail-closed), mirroring the read path.

The data-visibility side of read-after-write (the raft `gen` revision token,
`X-Nexus-Min-Revision`) is a separate contract tracked in #4737.
