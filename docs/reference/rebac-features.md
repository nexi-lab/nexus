# ReBAC Feature Matrix - What's Real vs Aspirational

## Purpose
This document clearly distinguishes between:
- ✅ **Implemented & Enforced** - Schema enforces these patterns
- 🎭 **Demonstrated (Illustrative)** - Demo shows pattern but no schema enforcement
- ❌ **Not Implemented** - Mentioned but not available

---

## Core Features

| Feature | Status | Schema Support | Notes |
|---------|--------|----------------|-------|
| **Direct Relationships** | ✅ ENFORCED | Yes | direct_owner, direct_editor, direct_viewer |
| **Permission Hierarchy** | ✅ ENFORCED | Yes | owner ⊃ editor ⊃ viewer |
| **Group Inheritance** | ✅ ENFORCED | Yes | Via tupleToUserset |
| **Deny Semantics** | ✅ ENFORCED | Yes | Via exclusion operator |
| **Exception Override** | ✅ ENFORCED | Yes | exception_viewer overrides deny |
| **ABAC Conditions** | ✅ ENFORCED | Yes | Time, IP, device evaluated |
| **Multi-Tenant Isolation** | ✅ ENFORCED | Data scoping | Via tenant_id filtering |
| **Batch Operations** | ✅ ENFORCED | Yes | rebac_check_batch |
| **Explainability** | ✅ ENFORCED | Yes | rebac_explain with proof paths |

---

## Workflow Patterns

### 1. Delegation & Approval (Demo 6)

**Status:** 🎭 **ILLUSTRATIVE ONLY**

**What Demo Shows:**
```python
# Create delegation relationship
nx.rebac_create(("agent", "alice"), "delegates-to", ("agent", "bob"))

# Create approval requirement
nx.rebac_create(("agent", "bob"), "requires-approval-from", ("agent", "carol"))
```

**Reality:**
- ❌ No schema enforcement
- ❌ `delegates-to` relation not connected to permissions
- ❌ `requires-approval-from` is just a tuple, not checked

**To Actually Enforce:**
```python
# Would need in schema:
"permissions": {
    "approve_purchase": {
        "intersection": [
            "is_approver",
            "has_delegation_or_direct"
        ]
    },
    "has_delegation_or_direct": {
        "union": [
            "direct_approver",
            "delegated_approver"
        ]
    },
    "delegated_approver": {
        "tupleToUserset": {
            "tupleset": "delegates-to",
            "computedUserset": "direct_approver"
        }
    }
}
```

**Current Impact:** Demo educates about delegation patterns but doesn't enforce them.

---

### 2. Separation of Duties (Demo 7)

**Status:** 🎭 **ILLUSTRATIVE ONLY**

**What Demo Shows:**
```python
# Create requester relationship
nx.rebac_create(("agent", "emma"), "requester-of", ("purchase-order", "PO-001"))

# Check if can approve (should fail due to SoD)
can_approve = nx.rebac_check(("agent", "emma"), "approver-of", ("purchase-order", "PO-001"))
```

**Reality:**
- ❌ No schema enforcement
- ❌ `requester-of` and `approver-of` not connected
- ❌ SoD rule not checked

**To Actually Enforce:**
```python
# Would need in schema:
"permissions": {
    "approve": {
        "intersection": [
            "is_approver",
            "not_requester"  # SoD check
        ]
    },
    "not_requester": {
        "exclusion": "requester-of"
    }
}
```

**Current Impact:** Demo shows SoD concept but any approver can approve their own requests.

---

### 3. Break-Glass Emergency Access (Demo 8)

**Status:** 🎭 **ILLUSTRATIVE ONLY**

**What Demo Shows:**
```python
# Grant emergency access
nx.rebac_create(
    ("agent", "admin"),
    "emergency-access",
    ("file", "/critical-system"),
    expires_at=datetime.now(UTC) + timedelta(hours=1)
)
```

**Reality:**
- ✅ TTL works (expires_at honored)
- ❌ No schema connection to permissions
- ❌ No audit trail enforcement
- ❌ No approval requirement

**To Actually Enforce:**
```python
# Would need in schema:
"permissions": {
    "admin": {
        "union": [
            "normal_admin",
            "emergency_admin"  # Break-glass path
        ]
    },
    "emergency_admin": {
        "intersection": [
            "emergency-access",
            "not_expired"  # Checked via expires_at
        ]
    }
}

# Plus: Audit log trigger on emergency-access creation
```

**Current Impact:** TTL works, but emergency-access is just a label.

---

### 4. External Sharing (Demo 9)

**Status:** 🎭 **ILLUSTRATIVE ONLY**

**What Demo Shows:**
```python
# Create external share link
nx.rebac_create(
    ("public", "share-link-abc123"),
    "external-viewer",
    ("file", "/project/report.pdf"),
    expires_at=datetime.now(UTC) + timedelta(days=7)
)
```

**Reality:**
- ✅ TTL works
- ❌ `external-viewer` not connected to `view` permission
- ❌ No token validation
- ❌ No rate limiting

**To Actually Enforce:**
```python
# Would need in schema:
"permissions": {
    "view": {
        "union": [
            "exception_viewer",
            "standard_view",
            "external_view"  # Share link path
        ]
    },
    "external_view": ["external-viewer"]
}

# Plus: Token validation in application layer
```

**Current Impact:** Creates tuple but doesn't grant view permission.

---

## Advanced Features Status

### 5. Consent & Privacy Controls

**Status:** ❌ **NOT IMPLEMENTED**

**Mentioned In:** Demo header, REBAC_GAPS document

**What's Missing:**
- No `consent-granted` relation
- No `self-discoverable` pattern
- No expand redaction
- No privacy-aware queries

**Would Need:**
```python
"permissions": {
    "discover_contact": {
        "intersection": [
            "has_contact_info",
            "consent_given"
        ]
    },
    "consent_given": {
        "union": [
            "public-profile",
            "explicit-consent"
        ]
    }
}

# Plus: rebac_expand with redaction
viewers = nx.rebac_expand("view", obj, respect_consent=True)
```

---

### 6. Policy Versioning

**Status:** ❌ **NOT IMPLEMENTED**

**What's Missing:**
- No version tracking for namespace changes
- No migration tooling
- No rollback mechanism
- No audit of schema changes

**Would Need:**
```python
# Versioned namespace API
nx.register_namespace(config, version="2.0")
nx.get_namespace_version("file")  # Returns: "2.0"

# Migration API
nx.migrate_namespace("file", from_version="1.0", to_version="2.0")

# Audit log
changes = nx.get_namespace_history("file")
```

---

### 7. As-of-Time Queries

**Status:** ❌ **NOT IMPLEMENTED**

**What Works:**
- ✅ TTL (expires_at) - Forward-looking expiration
- ✅ Tuple deletion

**What Doesn't Work:**
- ❌ Point-in-time reconstruction
- ❌ "Who had access on 2025-01-01?"
- ❌ Changelog queries
- ❌ Time-travel reads

**Would Need:**
```python
# Changelog table
CREATE TABLE rebac_changelog (
    id UUID PRIMARY KEY,
    tuple_id UUID,
    operation VARCHAR(10),  -- 'CREATE', 'DELETE'
    timestamp TIMESTAMP,
    tuple_snapshot JSONB
);

# API
had_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    as_of=datetime(2025, 1, 1)  # ❌ NOT SUPPORTED
)

# Who had access on Jan 1?
viewers = nx.rebac_expand(
    permission="view",
    object=("file", "/doc.txt"),
    as_of=datetime(2025, 1, 1)  # ❌ NOT SUPPORTED
)
```

**Estimation:** ~8 hours to implement (changelog tracking + query logic)

---

## Summary Table

| Feature | Implemented | Schema Enforced | Estimation to Enforce |
|---------|-------------|-----------------|----------------------|
| Direct permissions | ✅ Yes | ✅ Yes | N/A |
| Group inheritance | ✅ Yes | ✅ Yes | N/A |
| Deny semantics | ✅ Yes | ✅ Yes | N/A |
| ABAC conditions | ✅ Yes | ✅ Yes | N/A |
| Tenant isolation | ✅ Yes | ✅ Data scoping | N/A |
| **Delegation** | 🎭 Demo only | ❌ No | ~2 hours |
| **Approval workflow** | 🎭 Demo only | ❌ No | ~2 hours |
| **SoD (Separation of Duties)** | 🎭 Demo only | ❌ No | ~1 hour |
| **Break-glass** | 🎭 TTL only | ❌ No schema | ~2 hours |
| **External sharing** | 🎭 Demo only | ❌ No schema | ~1 hour |
| **Consent/privacy** | ❌ Not implemented | ❌ No | ~6 hours |
| **Policy versioning** | ❌ Not implemented | ❌ No | ~8 hours |
| **As-of-time queries** | ❌ Not implemented | ❌ No | ~8 hours |

---

## What This Means

### ✅ **Production-Ready Features:**

These work end-to-end with schema enforcement:

1. **Basic Permissions**
   - owner/edit/view hierarchy
   - Direct and group-based grants
   - Deny with exception override

2. **ABAC**
   - Time-based (ISO8601)
   - IP-based
   - Device-based
   - Custom attributes

3. **Multi-Tenant**
   - Implicit data scoping
   - Cross-tenant blocking

4. **Performance**
   - Batch operations
   - Caching with TTL
   - Graph traversal limits

5. **Observability**
   - Explainability (proof paths)
   - Audit trails (via tuple changelog)

### 🎭 **Educational Patterns:**

These demos show workflows but don't enforce them:

1. **Delegation** - Shows pattern, but delegate can't actually act
2. **Approval** - Shows workflow, but no requirement enforcement
3. **SoD** - Shows concept, but no conflict detection
4. **Break-glass** - TTL works, but no audit/approval enforced
5. **External sharing** - Creates tuple, but doesn't grant access

**To make these work:** Add schema enforcement (1-2 hours each)

### ❌ **Not Available:**

1. **Consent/privacy** - Not implemented (~6 hours)
2. **Policy versioning** - Not implemented (~8 hours)
3. **As-of-time** - Not implemented (~8 hours)

---

## Recommendations

### For Production Use:

**DO USE:**
- ✅ Direct permissions (owner/edit/view)
- ✅ Group inheritance
- ✅ Deny with exceptions
- ✅ ABAC (time/IP/device)
- ✅ Tenant isolation
- ✅ Batch operations

**DON'T RELY ON (without schema changes):**
- 🎭 Delegation (need schema)
- 🎭 Approval workflows (need schema)
- 🎭 SoD enforcement (need schema)
- 🎭 Break-glass audit (need schema)
- 🎭 External sharing (need schema)

### To Add Enforcement:

**Quick Wins (1-2 hours each):**
1. Connect `external-viewer` to `view` permission
2. Add SoD check to approval permission
3. Connect `emergency-access` to admin permission

**Medium Effort (2-4 hours each):**
4. Delegation with tupleToUserset
5. Multi-step approval workflow

**Large Effort (6-8 hours each):**
6. Consent/privacy system
7. Policy versioning
8. As-of-time queries

---

## Demo Accuracy

### Accurate Demos (Schema Enforces):
- ✅ Demo 1: Deny rules ← **WORKS**
- ✅ Demo 2: Proof paths ← **WORKS**
- ✅ Demo 3: Permission lattice ← **WORKS**
- ✅ Demo 4: Deduplication ← **WORKS**
- ✅ Demo 5: Graph limits ← **WORKS**
- ✅ Demo 11: Batch API ← **WORKS**
- ✅ Demo 13: ABAC ← **WORKS**
- ✅ Demo 14: Tenant isolation ← **WORKS**

### Illustrative Demos (No Schema Enforcement):
- 🎭 Demo 6: Delegation ← **Pattern only**
- 🎭 Demo 7: SoD ← **Pattern only**
- 🎭 Demo 8: Break-glass ← **TTL works, audit doesn't**
- 🎭 Demo 9: External sharing ← **Creates tuple, no access**
- 🎭 Demo 10: Ownership transfer ← **Pattern only**

### Missing Features:
- ❌ Consent/privacy ← **Not implemented**
- ❌ Policy versioning ← **Not implemented**
- ❌ As-of-time ← **Not implemented**

---

## How to Use This Document

### For Developers:

**When deciding what to use:**
1. Check this matrix first
2. ✅ = Use in production
3. 🎭 = Educational only (add schema to enforce)
4. ❌ = Not available (estimate time to build)

### For Demos:

**Add disclaimers:**
```python
# Demo 6: Delegation
print_warning("NOTE: Illustrative only - not enforced by schema")
print_info("To enforce: Add tupleToUserset for delegates-to relation")
```

### For Documentation:

**Be explicit:**
- "This demo shows the **pattern** for delegation..."
- "To actually enforce this, you would need to..."
- "Currently, this is **illustrative only**..."

---

## Conclusion

**Strong Foundation:**
- ✅ Core ReBAC features are production-ready
- ✅ ABAC fully implemented
- ✅ Multi-tenant working
- ✅ Performance optimized

**Educational Value:**
- 🎭 Workflow demos teach patterns
- 🎭 Easy to convert to enforced (1-2 hours each)
- 🎭 Show best practices

**Clear Gaps:**
- ❌ 3 features not implemented (documented)
- ❌ Honest about limitations
- ❌ Estimation for each

**Recommendation:** Use core features in production, treat workflow demos as educational templates for building enforcement.
