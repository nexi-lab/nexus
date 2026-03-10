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
