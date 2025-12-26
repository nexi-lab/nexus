# SpiceDB Key Learnings: High-Impact Patterns for Permission Systems

**Date:** 2025-12-26
**Summary:** Critical architectural patterns from SpiceDB for building efficient permission systems

---

## 🚀 Performance Optimization Techniques

### 1. Subproblem Caching (60%+ Cache Hit Rate)
**Problem:** Caching entire permission checks has low reuse.

**SpiceDB Solution:**
- Break permission checks into independent sub-problems
- Cache each sub-problem separately
- Different top-level requests reuse cached sub-problems
- **Result:** 60% cache hit rate, up to 40% request reduction

**Application to Any System:**
```
Instead of: cache("can_user_123_edit_doc_456")
Do: cache("user_123_in_editors_of_doc_456")
    cache("user_123_is_owner_of_doc_456")
    cache("user_123_org_can_admin_doc_456")
```

### 2. Quantization Windows (Simple Cache Invalidation)
**Problem:** Complex cache invalidation logic is error-prone.

**SpiceDB Solution:**
- Define time window (default 5 seconds)
- All cache entries in window expire together
- No per-entry invalidation logic needed
- Trade staleness tolerance for simplicity

**Tunable Parameters:**
- Window size: 5s default, configurable
- Max staleness: 10% of window by default
- Prevents thundering herd on rollover

### 3. Request Deduplication (40% Reduction)
**Problem:** Concurrent identical requests waste computation.

**SpiceDB Solution:**
- Track in-flight requests
- Subsequent identical requests wait on first
- Single computation serves all waiters
- **Result:** 40% fewer actual computations

### 4. Consistent Hash Load Balancing
**Problem:** Random request distribution dilutes cache effectiveness.

**SpiceDB Solution:**
- Hash request parameters
- Always route to same backend node
- Cache locality maximized
- Works across cache tiers

### 5. Per-Request Consistency Levels
**Problem:** All operations don't need same consistency.

**SpiceDB's 4 Levels:**
1. **minimize_latency**: Fastest, uses cache aggressively (default for reads)
2. **at_least_as_fresh**: Ensure at least as fresh as token (balanced)
3. **at_exact_snapshot**: Exact point-in-time (pagination)
4. **fully_consistent**: Bypass cache entirely (revocations)

**Pattern:**
```
// Public content view - can be slightly stale
check(resource, permission, user, consistency=minimize_latency)

// Admin removal - must be immediate
check(resource, permission, user, consistency=fully_consistent)
```

---

## 🏗️ Architectural Patterns

### 1. Dispatch Model (Parallel Graph Traversal)
**Architecture:**
- Break complex operation into sub-operations
- Dispatch to cluster nodes via internal gRPC
- Execute in parallel with goroutines
- Aggregate results

**Configuration:**
- Max concurrency per request: 50 (default)
- Max recursion depth: 50 (default)
- Prevents runaway resource consumption

### 2. Multi-Version Concurrency Control (MVCC)
**Problem:** Distributed systems have replication lag causing "new enemy problem".

**SpiceDB Solution (ZedTokens):**
1. Write returns point-in-time token
2. Subsequent reads include token
3. System ensures read sees at least that state
4. Prevents seeing permission removal before resource update

**Pattern:**
```
token = write_relationship(user, role, admin)
// Later...
check_permission(user, action, resource, at_least_as_fresh=token)
```

### 3. Dual-Layer Caching
**Layers:**
- **L1 (Server-side)**: Between API handlers and graph engine
- **L2 (Client-side)**: Between graph engine and remote dispatch

**Composable Dispatcher Pattern:**
- Caching logic wraps other dispatchers
- Same code serves both layers
- Easy to add additional cache tiers

---

## 🔐 Multi-Tenancy Patterns

### 1. Schema-Level Tenant Isolation
**Model tenancy as first-class entity:**

```
definition tenant {
    relation member: user
}

definition document {
    relation tenant: tenant
    relation reader: user

    // Permission only if reader AND tenant member
    permission view = reader & tenant->member
}
```

**Benefits:**
- Enforced at permission layer
- Can't accidentally leak cross-tenant
- Cross-tenant grants explicit

### 2. Datastore-Level Isolation (Mapping Proxies)
**For compliance or performance:**
- Different tenants → different databases/schemas
- Reduces table contention
- Better single-tenant performance
- Easier compliance (data residency)

### 3. Session Context Pattern
```
session = {user_id: "123", tenant_id: "acme"}
subject = f"user:{tenant_id}|{user_id}"
check_permission(resource, permission, subject)
```

---

## 🎯 API Design Learnings

### 1. Streaming APIs for Unbounded Results
**Problem:** LookupResources might return millions of results.

**SpiceDB Solution:**
- gRPC streaming responses
- Client processes incrementally
- Avoids massive memory allocation
- Note: Duplicates possible (client deduplicates)

### 2. Bulk Operations for Batch Efficiency
**BulkCheckPermission:**
- Single API call for multiple checks
- Batches database queries
- Shares subproblem computation
- Example: Check view permission on 1000 documents

**Use Case:** UI hydration on page load

### 3. Watch API for Real-Time Invalidation
**Pattern:**
- Stream all relationship changes
- Each change includes point-in-time token
- Downstream caches invalidate on relevant changes
- Store latest token to resume after restart

---

## 🛠️ Developer Experience

### 1. Schema as Code
- Declarative schema language
- Version controlled
- CI/CD validation (GitHub Actions)
- Test-driven development with assertions

### 2. Interactive Playground (WebAssembly)
- Browser-based development
- Real-time validation
- Assertions for testing
- Export to CI/CD

### 3. Language Server Protocol
- IDE integration (VS Code)
- Syntax highlighting
- Real-time validation
- Autocomplete

---

## 📊 Benchmarking Insights

### Production Performance (AuthZed)
- **1M QPS** with 100B relationships
- **P95 Latency:** 5.76ms (CheckPermission)
- **Cache Hit Rate:** 95.9% at peak
- **Infrastructure:** 56 nodes (21x 32vCPU, 35x 16vCPU)

### Scaling Characteristics
- Linear resource growth with traffic and data
- Similar latency across 10M to 100B relationships
- 20% better throughput on ARM (Graviton)

### Data Import
- 100M relationships: 6m 20s
- 1B relationships: 2h 40m
- 100B relationships: 121h (5+ days)

---

## 🎨 Schema Design Best Practices

### 1. Additive Design
**Principle:** Default to no access if relationship missing.

**Good:**
```
permission edit = writer + owner
```

**Bad:**
```
permission edit = writer + owner - blocked
// If write fails, blocked user might have access
```

### 2. Relations Over Caveats
**Use caveats only for:**
- Runtime context (IP address, time)
- ABAC logic that can't be relationships

**Why:** Caveats harder to cache, slower to evaluate

### 3. Expiring Relationships (Not Caveats) for TTL
**Before (v1.40):**
```
caveat not_expired(expiry timestamp) {
    now() < expiry  // Evaluated every check
}
```

**After:**
```
use expiration
relation contractor: user with expiration
// Automatic GC, more cacheable
```

### 4. Documentation & Naming
- Document all definitions, relations, permissions
- Relations: nouns (reader, owner, parent)
- Permissions: verbs (view, edit, delete)
- Underscore prefix for private/internal

---

## ⚡ Quick Wins for Any Permission System

### Immediate (Low Effort, High Impact)
1. **Quantization windows** instead of complex cache invalidation
2. **Request deduplication** for concurrent identical requests
3. **Subproblem caching** instead of result caching
4. **Consistent hashing** for cache locality
5. **Bulk APIs** for batch operations

### Medium Term
1. **Per-request consistency levels** for performance/correctness trade-offs
2. **Watch API** for real-time downstream invalidation
3. **Dispatch model** for parallel computation
4. **MVCC/token-based causality** to prevent consistency issues
5. **Schema as code** with CI/CD validation

### Long Term
1. **Multi-datastore support** for operational flexibility
2. **Distributed tracing** (OpenTelemetry) for debugging
3. **Advanced caching** (L1/L2 hierarchies)
4. **Materialized views** for extreme scale (narrow use cases)
5. **Interactive playground** for developer experience

---

## 🔍 Critical Configuration Tuning

### Cache Tuning
```bash
--datastore-revision-quantization-interval=5s  # Cache window
--datastore-revision-quantization-max-staleness-percent=0.1  # 10%
```

### Concurrency Tuning
```bash
--dispatch-concurrency-limit=50  # Parallel goroutines per request
--dispatch-max-depth=50  # Max recursion depth
```

### Garbage Collection
```bash
--datastore-gc-window=24h  # How long to keep old revisions
--datastore-gc-interval=3m  # GC frequency (PostgreSQL)
```

### Connection Pooling
```bash
--datastore-conn-pool-read-max-open=20
--datastore-conn-pool-write-max-open=10
```

---

## 🎓 Lessons for Nexus

### 1. Metadata Operations as Graph Traversal
- Directory listings → graph reachability
- Permission checks → subgraph existence
- Search → filtered graph traversal

### 2. Tenant Isolation
- Model workspace as first-class entity
- All operations filtered by workspace context
- Cross-workspace grants explicit in schema

### 3. Consistency Levels
- Metadata list: minimize_latency acceptable
- Permission revocation: fully_consistent required
- File access: at_least_as_fresh balanced

### 4. Caching Strategy
- Don't cache entire directory listings
- Cache individual entry permissions
- Cache directory membership
- Quantization window for invalidation

### 5. Batching
- Batch permission checks for UI
- Batch metadata lookups
- Share computation across requests

---

## 📚 Further Reading

**Essential:**
- [SpiceDB Architecture](https://authzed.com/blog/spicedb-architecture)
- [How Caching Works in SpiceDB](https://authzed.com/blog/how-caching-works-in-spicedb)
- [1 Million QPS Benchmark](https://authzed.com/blog/google-scale-authorization)

**Advanced:**
- [Preventing New Enemy Problem with CockroachDB](https://authzed.com/blog/prevent-newenemy-cockroachdb)
- [Hotspot Caching](https://authzed.com/blog/hotspot-caching-in-google-zanzibar-and-spicedb)
- [Schema Design Patterns](https://authzed.com/blog/schema-language-patterns)

**Production:**
- [Load Testing Guide](https://authzed.com/blog/spicedb-load-testing-guide)
- [Deploying SpiceDB Operator](https://authzed.com/docs/spicedb/ops/deploying-spicedb-operator)
- [Best Practices](https://authzed.com/docs/best-practices)
