# Google Zanzibar & Modern Permission System Optimizations

**Research Date:** December 26, 2025
**Purpose:** Identify high-performance optimization patterns from Google Zanzibar and modern ReBAC implementations to improve Nexus permission system

---

## Executive Summary

Google Zanzibar processes **10M+ authorization requests per second** with **<10ms P95 latency** and **99.999% availability**. This research examines the key optimization techniques from Zanzibar and modern implementations (SpiceDB, OpenFGA, Ory Keto) that enable this performance level.

**Key Findings:**
- Leopard indexing system reduces deeply nested group resolution from O(depth) to O(1)
- Distributed caching with consistent hashing achieves 60%+ cache hit rates
- Request hedging eliminates tail latency from slow operations
- Timestamp quantization enables massive cache reuse
- Subproblem decomposition enables parallelization and deduplication

---

## 1. Google Zanzibar Paper Insights

### 1.1 Core Architecture

**Data Model:**
- Simple relation tuples: `<object>#<relation>@<user>`
- Namespace-based organization (one database table per namespace)
- Multi-version storage with commit timestamps
- Primary key: `(shard_id, object_id, relation, user, commit_timestamp)`

**Performance Characteristics:**
- Trillions of ACLs managed
- 10M+ QPS sustained
- 95% of requests < 10ms
- 99.999% availability over 3+ years
- Only 20M read RPCs/sec to Spanner (despite 10M+ QPS to Zanzibar)

### 1.2 Leopard Indexing System

**Problem:** Deeply nested groups require serial database queries - O(depth) latency.

**Example:**
```
user:alice -> group:eng -> group:employees -> group:all_users
```
Checking if `alice` is in `all_users` requires 4 serial Spanner reads.

**Solution:** Leopard maintains in-memory transitive closure of all group memberships.

**Architecture:**
1. **Base Layer:** Periodic snapshots of ACL data
2. **Incremental Layer:** Watches Zanzibar changes via Watch API
3. **Denormalization:** Flattens all nested group-to-group paths
4. **Single-Call Resolution:** All nested lookups become single Leopard queries

**Performance:**
- 1.56M QPS median, 2.22M QPS at P99
- Converts O(depth) serial queries to O(1) lookups
- One Zanzibar tuple change can generate 10,000+ Leopard index updates

**Implementation Details:**
- Maintains group-to-group membership graph in memory
- Incrementally updates via temporally-ordered tuple stream
- Selective opt-in: Only namespaces with deep nesting use Leopard
- Trade-off: Higher memory usage for lower latency

**Key Insight for Nexus:** For deeply nested organization hierarchies or group memberships, consider maintaining a denormalized index of transitive relationships. This could be especially valuable for common patterns like:
- User in folder → folder in workspace → workspace in organization
- File inherits from parent → parent inherits from grandparent

### 1.3 Request Hedging

**Problem:** Occasional slow operations create tail latency spikes.

**Solution:** Send duplicate requests to multiple servers, use first response, cancel others.

**Implementation:**
- First request sent immediately
- Hedged requests delayed by dynamic threshold (Nth percentile latency)
- Limits additional traffic to small fraction of total
- ~1% of Spanner reads (200K/sec) benefit from hedging

**Mechanism:**
```python
# Pseudo-code
latency_estimator = dynamic_percentile_tracker(percentile=95)
threshold = latency_estimator.get_threshold()

result = send_request(primary_server)
if not result.received_within(threshold):
    hedged_result = send_request(secondary_server)
    return first_to_complete([result, hedged_result])
```

**Benefits:**
- Prevents single slow operation from blocking user interactions
- Critical for clients issuing 10-100s of checks (e.g., Drive search results)
- Minimal traffic overhead due to delayed hedging

**Key Insight for Nexus:** Implement request hedging for permission checks in critical paths (file listing, search results). Start with higher percentile (P95) to minimize traffic impact.

### 1.4 Distributed Caching Architecture

**Multi-Layer Caching:**
1. **Service-level cache:** Complete check results
2. **Intermediate results cache:** Subproblem solutions
3. **Read cache:** Raw relation tuples

**Consistent Hashing:**
- Distributes cache entries across servers
- Same subproblem always routes to same server
- Enables request deduplication (multiple requests for same subproblem wait for single computation)
- Cache hit rates up to 60%+ in production

**Lock Table (Cache Stampede Prevention):**
- Tracks outstanding reads/checks per cache key
- Only first request computes result
- Subsequent requests block waiting for cache population
- Eliminates "thundering herd" on cache misses

**Key Insight for Nexus:** Current Nexus caching is node-local. Implementing distributed cache with consistent hashing would:
- Enable horizontal scaling of cache capacity
- Deduplicate concurrent identical requests across nodes
- Improve cache hit rates through centralization

### 1.5 Timestamp Quantization

**Problem:** Microsecond-resolution timestamps in cache keys prevent cache reuse.

**Solution:** Round evaluation timestamps up to coarse granularity (1-10 seconds).

**Mechanism:**
```python
def quantize_timestamp(timestamp, quantum=10_000_000):  # 10 seconds
    """Round up to next quantum boundary"""
    return ((timestamp // quantum) + 1) * quantum

# Cache key includes quantized timestamp
cache_key = (resource, permission, user, quantize_timestamp(request_time))
```

**Benefits:**
- All requests within quantum window use same timestamp
- Massive cache hit rate improvement
- Controlled staleness (maximum 1-10 seconds)
- Different servers independently choose same timestamp

**Trade-off:** Intentional staleness for performance. Mitigated by:
- Zookie protocol ensures minimum freshness requirements
- Critical operations can bypass quantization
- Configurable quantum based on use case

**Key Insight for Nexus:** Implement timestamp quantization with configurable quantum:
- Aggressive (10s) for read-heavy workloads
- Conservative (1s) for write-heavy workloads
- Bypass for operations requiring immediate consistency

### 1.6 Zookie Protocol (Consistency Tokens)

**Problem:** Balance consistency with performance and cache reuse.

**Solution:** Client-provided tokens that encode minimum required freshness.

**Protocol:**
1. Client writes content, gets current timestamp
2. Client performs ACL check with timestamp → gets Zookie
3. Future operations include Zookie to ensure "at-least-as-fresh" semantics
4. Server can use any timestamp >= Zookie timestamp

**Benefits:**
- Solves "New Enemy Problem" (user loses access but cached result grants it)
- Enables controlled staleness for performance
- Gives servers freedom to optimize timestamp selection
- Compatible with quantization

**SpiceDB Implementation (ZedTokens):**
- Encoded as base64 token
- Represents point in time in permission graph
- Per-request consistency control:
  - `minimize_latency`: Use stale cache for speed
  - `at_least_as_fresh`: Respect token timestamp
  - `fully_consistent`: Latest snapshot only

**Key Insight for Nexus:** Implement consistency tokens to:
- Enable per-operation consistency control
- Support "fast but possibly stale" reads for UI
- Guarantee "fresh" reads for critical operations (delete, revoke access)

---

## 2. Modern Zanzibar Implementations

### 2.1 SpiceDB (AuthZed)

**Performance Achievements:**
- 1M QPS with 100B relationships on CockroachDB
- 60%+ cache hit rates in production
- Consistent sub-10ms P95 latency

**Key Innovations:**

#### 2.1.1 Distributed Dispatch System

**Subproblem Decomposition:**
- Every permission check broken into graph traversal subproblems
- Each subproblem dispatched to appropriate node via consistent hashing
- Parallel execution of independent subproblems
- Caching at every layer (caller and callee sides)

**Example:**
```
Check: user:alice has permission:view on doc:123

Decomposed into:
1. Direct check: doc:123#view@user:alice
2. Inherited: doc:123#parent@folder:xyz
3. Group: folder:xyz#view@group:editors
4. Membership: group:editors#member@user:alice

Subproblems 1-4 execute in parallel
Each result cached independently
```

**Consistent Hash Ring:**
- Each node builds own hash ring (no coordination required)
- Token-based distribution (default 100 tokens per node)
- Requests for same subproblem always route to same node
- Automatic rebalancing on node add/remove

**Configuration:**
- `--dispatch-hashring-replication-factor=100`
- `--dispatch-concurrency-limit=50` (parallel goroutines per request)

**Key Insight for Nexus:** Implement subproblem-based architecture:
1. Decompose permission checks into minimal units
2. Hash subproblems to specific workers/nodes
3. Cache each subproblem result independently
4. Execute independent subproblems in parallel

#### 2.1.2 Advanced Caching with Ristretto

**Ristretto Cache:**
- High-performance Go cache library
- Platform-specific optimizations
- Admission policy prevents cache pollution
- Cost-based eviction

**Cache Layers:**
- Same caching implementation for client-side and server-side
- Shared cache interface: `DispatchInterface`
- Automatic cache invalidation via quantization interval

**Quantization Interval:**
- Default: 5 seconds
- Configurable: `--datastore-revision-quantization-interval`
- All requests in window use same revision
- Cached results valid for remainder of window

**Key Insight for Nexus:** Adopt high-performance cache library with:
- Admission policy to prevent cache pollution from one-off queries
- Cost-based eviction (larger computation results kept longer)
- Platform-specific optimizations

#### 2.1.3 Watch API for Cache Invalidation

**Real-Time Change Stream:**
- Streaming API for all relationship changes
- Historical replay via ZedToken
- Checkpoint support for long-running watches
- Transaction metadata for audit trail

**Use Cases:**
1. **Invalidation:** Detect changes affecting cached results
2. **Audit:** Track who changed what and when
3. **Materialized Views:** Build denormalized permission data
4. **Event-Driven:** Trigger downstream actions on permission changes

**Data Retention:**
- Default: 24 hours of change history
- Application responsibility to persist for longer retention
- Non-durable stream (consumers must handle)

**Integration Example:**
```go
watch, err := client.Watch(ctx, &v1.WatchRequest{
    OptionalStartCursor: lastToken,
})

for {
    resp, err := watch.Recv()
    // resp.Updates contains relationship changes
    // resp.ChangesThrough contains new token to resume from
}
```

**Key Insight for Nexus:** Implement Watch API to enable:
- Event-driven cache invalidation
- Audit logging for compliance
- Building denormalized views (like Leopard)
- Real-time notifications (user added to folder)

#### 2.1.4 Lookup APIs for Reverse Queries

**LookupResources:**
- Find all resources user can access
- Critical for UI filtering (show only accessible files)
- Returns resource IDs matching permission

**LookupSubjects:**
- Find all users with permission on resource
- Critical for sharing UI (who has access?)
- Returns subject IDs (users, groups) with permission

**Performance Characteristics:**
- Computationally expensive (full graph traversal)
- Intersections/exclusions require checking each candidate
- Cursor support for pagination (limit memory usage)
- Can overwhelm database without proper tuning

**Optimization Patterns:**
1. **Cursoring:** Limit results per request, paginate
2. **Denormalization:** Replicate results to app database/search index
3. **BulkCheck Alternative:** Sometimes faster to enumerate candidates and bulk check

**Example - Denormalization Strategy:**
```python
# Watch API detects permission change
watch_event = receive_watch_event()

# Update materialized view in PostgreSQL
if watch_event.type == "permission_granted":
    db.execute("""
        INSERT INTO user_accessible_files (user_id, file_id)
        VALUES (%s, %s)
    """, user_id, file_id)

# Application queries PostgreSQL directly for filtering
accessible_files = db.execute("""
    SELECT file_id FROM user_accessible_files
    WHERE user_id = %s
""", current_user)
```

**Key Insight for Nexus:** For list operations (files in folder):
1. Use LookupResources with cursoring for small result sets
2. Build materialized view in PostgreSQL for large result sets
3. Keep materialized view updated via Watch API
4. Consider BulkCheck when result set size is predictable

### 2.2 OpenFGA (Auth0/CNCF)

**Key Differentiators:**

#### 2.2.1 Multi-Store Architecture

**Store Concept:**
- Isolated authorization model environment
- Each store has own schema, tuples, permissions
- Enables multi-tenancy at platform level

**Benefits:**
- Teams can experiment with different models independently
- Development/staging/production isolation
- True multi-tenant SaaS capability

**vs. Ory Keto:**
- Keto: Single model loaded at startup
- OpenFGA: Dynamic model management, multiple concurrent models

**Key Insight for Nexus:** Current Nexus has single global schema. Consider:
- Per-workspace authorization models (custom permission types)
- Isolated environments for testing schema changes
- Multi-tenant hosting with schema isolation

#### 2.2.2 Contextual & Attribute-Based Access

**Extensions Beyond Pure ReBAC:**
- Conditional tuples: `user:alice#viewer@doc:123 if current_time < 2025-01-01`
- Attribute checks: `user:alice#viewer@doc:123 if user.department == doc.department`
- Context evaluation: Pass context variables at check time

**Example:**
```json
{
  "tuple": {
    "user": "user:alice",
    "relation": "viewer",
    "object": "doc:123",
    "condition": {
      "name": "time_limited",
      "context": {
        "expiration": "2025-01-01T00:00:00Z"
      }
    }
  }
}
```

**Trade-offs:**
- More expressive than pure ReBAC
- Potentially lower cache hit rates (context-dependent)
- Increased complexity

**Key Insight for Nexus:** Evaluate need for conditional permissions:
- Time-limited shares (expire after 7 days)
- IP-restricted access (only from corporate network)
- Attribute-based (same department only)

Balance expressiveness vs. caching efficiency.

#### 2.2.3 Built-in Audit Logging

**Automatic Relationship Tracking:**
- All tuple additions/deletions logged
- Who made change, when, what changed
- Built into core system (not optional add-on)

**Governance Benefits:**
- Compliance requirements (SOC2, GDPR)
- Security incident investigation
- Permission history/rollback

**Key Insight for Nexus:** Implement comprehensive audit logging:
- Log all permission changes with actor, timestamp, reason
- Enable permission history queries (who had access when?)
- Support compliance reporting

### 2.3 Ory Keto

**Key Characteristics:**
- Microservice-oriented architecture
- Cloud-native design (Kubernetes-first)
- Configuration-based model (not dynamic like OpenFGA)

**Development Status:**
- Less active development vs. OpenFGA/SpiceDB
- Smaller community, fewer recent updates

**Key Insight for Nexus:** OpenFGA and SpiceDB are more actively maintained and feature-rich. Recommend focusing on patterns from these implementations.

### 2.4 Comparison Summary

| Feature | SpiceDB | OpenFGA | Ory Keto |
|---------|---------|---------|----------|
| **Organization** | AuthZed (YC) | CNCF | Ory |
| **Performance** | 1M+ QPS proven | Good | Good |
| **Multi-Store** | ❌ | ✅ | ❌ |
| **Conditional Perms** | ❌ | ✅ | ❌ |
| **Watch API** | ✅ | ✅ | Limited |
| **Lookup APIs** | ✅ Both | ✅ Both | ✅ Limited |
| **BulkCheck** | ✅ | ✅ | ❌ |
| **Audit Logging** | Via Watch | ✅ Built-in | ❌ |
| **Consistency Control** | ✅ ZedTokens | ✅ | Limited |
| **Active Development** | ✅✅✅ | ✅✅✅ | ✅ |
| **Documentation** | Excellent | Excellent | Good |

---

## 3. Permission Check Optimization Patterns

### 3.1 Request Deduplication

**Problem:** Multiple concurrent identical requests waste compute.

**Solution:** Coalesce concurrent requests for same subproblem.

**Implementation Pattern:**
```python
class RequestDeduplicator:
    def __init__(self):
        self.pending = {}  # key -> Future
        self.lock = threading.Lock()

    async def check_permission(self, key, compute_fn):
        with self.lock:
            if key in self.pending:
                # Another request already computing this
                return await self.pending[key]

            # Create future for this computation
            future = asyncio.create_task(compute_fn())
            self.pending[key] = future

        try:
            result = await future
            return result
        finally:
            with self.lock:
                del self.pending[key]
```

**Zanzibar Lock Table:**
- Per-server lock table tracking outstanding operations
- Concurrent requests with same cache key share result
- First request computes, others wait
- Prevents cache stampede on hot items

**Metrics:**
- 10-100x reduction in duplicate work for hot paths
- Critical during cache warm-up or invalidation

**Key Insight for Nexus:** Implement request coalescing for:
- Concurrent checks of same permission (file listing)
- Cache miss scenarios (first request after invalidation)
- Hot path operations (public file accessed by many users)

### 3.2 Subproblem Caching

**Granular Cache Keys:**
Instead of caching only final results, cache every intermediate step.

**Example Check:** `user:alice can view doc:123`

**Subproblems to Cache:**
1. `doc:123#view@user:alice` (direct relationship)
2. `doc:123#parent@folder:xyz` (parent folder)
3. `folder:xyz#view@user:alice` (inherited permission)
4. `folder:xyz#view@group:editors` (group permission)
5. `group:editors#member@user:alice` (group membership)

**Benefits:**
- Subproblem 5 reused across all files in folder
- Subproblem 4 reused across all users in group
- Higher cache hit rate than caching only final result

**Cache Key Design:**
```python
def subproblem_cache_key(
    resource: str,
    relation: str,
    subject: str,
    timestamp: int
) -> str:
    quantized_ts = quantize_timestamp(timestamp)
    return f"{resource}#{relation}@{subject}:{quantized_ts}"
```

**Key Insight for Nexus:** Current Nexus may cache full permission checks only. Implement subproblem-level caching:
- Cache each step of relationship traversal
- Reuse common subproblems (user in group, file in folder)
- Dramatically increase cache hit rates

### 3.3 Parallel Graph Traversal

**Serial vs Parallel:**

**Serial (Naive):**
```python
async def check_permission_serial(user, permission, resource):
    # Check direct permission
    if await has_direct_permission(user, permission, resource):
        return True

    # Check parent permissions (serial)
    parent = await get_parent(resource)
    if parent:
        return await check_permission_serial(user, permission, parent)

    # Check group permissions (serial)
    groups = await get_user_groups(user)
    for group in groups:
        if await check_permission_serial(group, permission, resource):
            return True

    return False
```

**Parallel:**
```python
async def check_permission_parallel(user, permission, resource):
    # Start all checks concurrently
    tasks = [
        check_direct_permission(user, permission, resource),
        check_parent_permission(user, permission, resource),
        check_group_permissions(user, permission, resource)
    ]

    # Use asyncio.gather or wait for first True
    results = await asyncio.gather(*tasks)
    return any(results)
```

**Short-Circuit Optimization:**
- Union (OR): Return immediately on first True
- Intersection (AND): Return immediately on first False
- Exclusion (EXCEPT): Return immediately on conflicting evidence

**SpiceDB Approach:**
- Decompose into independent subproblems
- Dispatch subproblems to different nodes (distributed parallelism)
- Local parallelism within node (goroutines)
- Configurable concurrency limit to prevent resource exhaustion

**Limitations:**
- Intersections less parallelizable (need both sides)
- Exclusions less parallelizable (need both sides)
- Use unions where possible for maximum parallelism

**Key Insight for Nexus:** Parallelize permission checks:
1. Identify independent subproblems in check logic
2. Execute independent checks concurrently
3. Short-circuit on first definitive answer
4. Limit concurrency to prevent resource exhaustion

### 3.4 Pre-computed Permission Sets

**Pattern:** Maintain materialized views of permission derivations.

**Use Case:** When read:write ratio is very high.

**Example:**
```sql
-- Materialized view: user_file_permissions
CREATE MATERIALIZED VIEW user_file_permissions AS
SELECT
    u.user_id,
    f.file_id,
    ARRAY_AGG(DISTINCT p.permission) as permissions
FROM users u
CROSS JOIN files f
CROSS JOIN permissions p
WHERE check_permission(u.user_id, p.permission, f.file_id)
GROUP BY u.user_id, f.file_id;

-- Query becomes simple lookup
SELECT permissions
FROM user_file_permissions
WHERE user_id = ? AND file_id = ?;
```

**Update Strategies:**
1. **Full Refresh:** Periodic complete recompute (slow but simple)
2. **Incremental:** Update only affected rows on permission changes
3. **Watch-Based:** Listen to Watch API, update affected entries

**Trade-offs:**
- Extremely fast reads (direct lookup)
- Slower writes (must update materialized view)
- Storage overhead (potentially large)
- Staleness (refresh lag)

**Best For:**
- High read:write ratio (>100:1)
- Bounded result sets (not all users × all files)
- Acceptable staleness (seconds to minutes)

**OpenFGA/SpiceDB Pattern:**
Use Watch API to maintain denormalized views in application database:

```python
# Background worker watching permission changes
async def maintain_materialized_view():
    watch = spicedb.watch(start_token=last_token)

    async for event in watch:
        if event.relationship_type == "folder#member":
            # User added to folder - recompute their file access
            affected_files = get_files_in_folder(event.folder_id)
            for file_id in affected_files:
                recompute_user_file_access(event.user_id, file_id)

        last_token = event.token
```

**Key Insight for Nexus:** For common queries (files user can access):
1. Maintain materialized view in PostgreSQL
2. Update incrementally via permission change events
3. Fall back to real-time check if view stale
4. Dramatically faster than computing permissions on every request

### 3.5 Negative Caching

**Problem:** Failed permission checks (DENIED) are not cached in many systems.

**Issue:** Repeated unauthorized access attempts cause same expensive computation.

**Solution:** Cache negative results (DENIED) with appropriate TTL.

**Implementation:**
```python
class PermissionCache:
    def check(self, user, permission, resource):
        key = f"{user}:{permission}:{resource}"

        # Check cache for both positive and negative results
        cached = self.cache.get(key)
        if cached is not None:
            return cached  # True (GRANTED) or False (DENIED)

        # Compute and cache result (whether True or False)
        result = self.compute_permission(user, permission, resource)
        self.cache.set(key, result, ttl=self.ttl)
        return result
```

**Considerations:**
- **Shorter TTL for DENIED:** More sensitive to staleness
- **Security Concern:** Cached DENIED must invalidate when permission granted
- **Attack Vector:** Brute-force attacks benefit from negative caching

**Android Runtime Permissions Issue:**
- Cached DENIED can cause confusion when permission later granted
- Trade-off between performance and UX
- Recommendation: Don't cache negative results for runtime-grantable permissions

**Key Insight for Nexus:** Implement negative caching with caution:
- Cache DENIED results for performance
- Use shorter TTL than GRANTED (e.g., 5s vs 60s)
- Invalidate immediately when permission granted
- Monitor for security implications

---

## 4. Multi-tenancy in Permission Systems

### 4.1 Tenant Isolation Patterns

**Isolation Spectrum:**

1. **Fully Isolated (Separate Databases):**
   - Highest security
   - Highest cost
   - Operational complexity
   - Best for enterprise customers

2. **Shared Database, Separate Schemas:**
   - Good security
   - Moderate cost
   - Easier operations
   - Good for mid-market

3. **Shared Schema with Tenant ID:**
   - Lowest cost
   - Highest efficiency
   - Requires careful query filtering
   - Best for SMB/consumer

**Nexus Context:** Currently shared schema with workspace_id scoping.

### 4.2 Cross-Tenant Permission Grants

**Use Case:** User from Org A granted access to resource in Org B.

**Challenge:** Permission check must traverse across tenant boundaries.

**Zanzibar Approach:**
- No built-in tenant isolation (Google-internal use)
- All namespaces globally accessible
- Tenant isolation enforced at application layer

**SpiceDB/OpenFGA Approach:**
- Namespaces are global (no tenant scoping)
- Application encodes tenant in object IDs: `tenant1:file:123`
- Cross-tenant grants possible: `tenant1:file:123#viewer@tenant2:user:alice`

**Implementation Pattern:**
```python
# Tenant-scoped object IDs
def make_object_id(tenant_id: str, object_type: str, object_id: str) -> str:
    return f"{tenant_id}:{object_type}:{object_id}"

# Example: User from tenant B accesses file in tenant A
file_id = make_object_id("tenant_a", "file", "123")
user_id = make_object_id("tenant_b", "user", "alice")

# Permission check works across tenants
can_access = check_permission(user_id, "view", file_id)
```

**Security Considerations:**
- Application must validate cross-tenant grants are intentional
- Audit logging critical for compliance
- Consider requiring explicit approval for cross-tenant shares

**Key Insight for Nexus:** Current workspace isolation may be too strict. Consider:
- Support cross-workspace file sharing
- Workspace ID in all object IDs
- Application-layer validation of cross-workspace grants
- Audit trail for all cross-workspace access

### 4.3 Optimizing "Same Tenant" Hot Path

**Observation:** 95%+ of permission checks are same-tenant.

**Optimization:** Special-case same-tenant checks for performance.

**Pattern:**
```python
async def check_permission_optimized(user_id, permission, resource_id):
    user_tenant = extract_tenant(user_id)
    resource_tenant = extract_tenant(resource_id)

    if user_tenant == resource_tenant:
        # Fast path: Same tenant
        # Use tenant-local cache, skip tenant boundary checks
        return await check_permission_same_tenant(
            user_id, permission, resource_id, user_tenant
        )
    else:
        # Slow path: Cross-tenant
        # Additional validation, audit logging
        await log_cross_tenant_access(user_id, resource_id)
        return await check_permission_cross_tenant(
            user_id, permission, resource_id
        )
```

**Same-Tenant Optimizations:**
- Dedicated cache partition (no tenant ID in cache key)
- Skip cross-tenant validation logic
- Higher cache hit rate (more focused data set)
- Potentially different consistency guarantees

**Key Insight for Nexus:** Optimize for workspace-local access:
- Fast path for same-workspace checks
- Separate cache partition per workspace
- Cross-workspace as explicit, slower path
- Monitor ratio of same-workspace vs cross-workspace

### 4.4 Tenant-Aware Caching

**Challenge:** Global cache shared across tenants has issues:
- Noisy neighbor (one tenant evicts another's cache)
- Security concern (cache key collision)
- Inefficient (low hit rate per tenant)

**Solution:** Partition cache by tenant.

**Pattern 1: Separate Cache Instances**
```python
class TenantCacheManager:
    def __init__(self):
        self.caches = {}  # tenant_id -> Cache
        self.max_size_per_tenant = 1000

    def get_cache(self, tenant_id):
        if tenant_id not in self.caches:
            self.caches[tenant_id] = LRUCache(
                max_size=self.max_size_per_tenant
            )
        return self.caches[tenant_id]

    def check_permission(self, user_id, permission, resource_id):
        tenant_id = extract_tenant(resource_id)
        cache = self.get_cache(tenant_id)
        # Use tenant-specific cache...
```

**Pattern 2: Tenant-Prefixed Keys**
```python
def cache_key(tenant_id, user_id, permission, resource_id):
    # Tenant ID in cache key prevents collisions
    return f"{tenant_id}:{user_id}:{permission}:{resource_id}"
```

**Pattern 3: Cache Quota Per Tenant**
```python
class QuotaCache:
    def __init__(self, total_size, num_tenants):
        self.quota_per_tenant = total_size // num_tenants
        self.tenant_usage = {}

    def set(self, tenant_id, key, value):
        if self.tenant_usage[tenant_id] >= self.quota_per_tenant:
            # Evict LRU from this tenant
            self.evict_lru(tenant_id)

        self.cache[key] = value
        self.tenant_usage[tenant_id] += 1
```

**Key Insight for Nexus:** Implement tenant-aware caching:
- Partition cache by workspace_id
- Prevent one workspace from evicting another's cache
- Monitor cache hit rate per workspace
- Consider quota limits for fairness

---

## 5. Caching Strategies

### 5.1 TTL vs Event-Based Invalidation

**TTL (Time-To-Live):**

**Pros:**
- Simple to implement
- No coordination required
- Bounded staleness (max = TTL)
- Works with any data source

**Cons:**
- Wasteful (invalidates even if data unchanged)
- Potentially stale (up to TTL)
- Difficult to tune (too short = poor hit rate, too long = stale data)

**Implementation:**
```python
cache.set(key, value, ttl=60)  # Expires after 60 seconds
```

**Event-Based Invalidation:**

**Pros:**
- Invalidate only when data actually changes
- Lower staleness (near real-time)
- Better cache hit rates (entries don't expire unnecessarily)

**Cons:**
- Requires event infrastructure (Watch API, message bus)
- Coordination complexity
- Potential consistency issues (missed events)
- More complex to implement

**Implementation:**
```python
# Watch for permission changes
async for event in permission_watch_stream():
    if event.type == "relationship_deleted":
        # Invalidate affected cache entries
        cache.delete(f"{event.user}:*:{event.resource}")
```

**Hybrid Approach (Recommended):**
- **Event-based invalidation** for known changes
- **TTL as safety net** for missed events or bugs
- **Long TTL** (e.g., 5-10 minutes) since events handle most invalidation

```python
# Set with long TTL
cache.set(key, value, ttl=600)

# Also invalidate on events
async for event in watch_stream():
    affected_keys = compute_affected_keys(event)
    for key in affected_keys:
        cache.delete(key)
```

**Key Insight for Nexus:** Implement hybrid approach:
1. Watch permission changes in database
2. Invalidate affected cache entries immediately
3. Set TTL (e.g., 5 minutes) as safety net
4. Monitor invalidation rate vs TTL expiration rate

### 5.2 Negative Caching (Permission Denied)

*(Covered in Section 3.5 - see above)*

**Summary:**
- Cache DENIED results to prevent repeated expensive checks
- Use shorter TTL than GRANTED results
- Invalidate immediately when permission granted
- Monitor for security implications

### 5.3 Cache Warming Strategies

**Problem:** Cold cache after deployment/restart has 0% hit rate.

**Strategy 1: Predictive Pre-Warming**

Populate cache with likely-to-be-requested items based on patterns.

```python
async def warm_cache_on_startup():
    # Most active users
    top_users = await db.get_top_users(limit=1000)

    # Most accessed files
    top_files = await db.get_top_files(limit=10000)

    # Pre-compute common checks
    for user in top_users:
        for file in user.get_recent_files(limit=100):
            # Compute and cache permission
            await check_permission(user.id, "view", file.id)
```

**Strategy 2: Request Pattern Replay**

Record production request patterns, replay them to warm cache.

```python
# Record production requests
async def record_request(user_id, permission, resource_id):
    await redis.zadd(
        "cache_warmup_requests",
        {f"{user_id}:{permission}:{resource_id}": time.time()}
    )

# Replay on new instance
async def warm_cache_from_patterns():
    # Get top N most recent/frequent requests
    patterns = await redis.zrevrange("cache_warmup_requests", 0, 10000)

    for pattern in patterns:
        user_id, permission, resource_id = pattern.split(":")
        await check_permission(user_id, permission, resource_id)
```

**Strategy 3: Scheduled Warming**

Periodically refresh cache for high-value items (e.g., public files).

```python
@scheduled(every="10 minutes")
async def refresh_public_file_cache():
    public_files = await db.get_public_files()
    for file in public_files:
        # Refresh cache for anonymous user access
        await check_permission("anonymous", "view", file.id)
```

**Strategy 4: Event-Driven Warming**

Warm cache in response to events (e.g., file uploaded → warm owner's cache).

```python
@event_handler("file_uploaded")
async def warm_cache_for_new_file(file_id, owner_id):
    # Pre-compute owner's permissions
    await check_permission(owner_id, "view", file_id)
    await check_permission(owner_id, "edit", file_id)
    await check_permission(owner_id, "delete", file_id)

    # Pre-compute parent folder permissions
    parent = await get_parent_folder(file_id)
    if parent:
        await check_permission(owner_id, "view", parent.id)
```

**Challenges:**
- **Cache Stampede:** All instances warming at once
  - Solution: Use distributed lock (Redlock) to coordinate
- **Wasted Work:** Warming items that won't be accessed
  - Solution: Use analytics to identify high-value items
- **Resource Consumption:** Warming consumes CPU/memory
  - Solution: Rate limit warming, run during off-peak

**Key Insight for Nexus:** Implement multi-strategy warming:
1. **On startup:** Warm cache with most active users/files
2. **Scheduled:** Refresh public/shared file cache every 10 minutes
3. **Event-driven:** Warm cache when file uploaded/shared
4. **Coordinated:** Use distributed lock to prevent stampede

### 5.4 Cache Key Design

**Critical for Hit Rate and Correctness:**

**Components:**
1. **Resource ID:** What is being accessed
2. **Subject ID:** Who is accessing
3. **Permission:** What action
4. **Consistency Token:** When (quantized timestamp or version)

**Example:**
```python
def cache_key(
    resource_id: str,
    permission: str,
    subject_id: str,
    timestamp: int,
    quantum: int = 10_000_000  # 10 seconds
) -> str:
    quantized_ts = (timestamp // quantum) * quantum
    return f"{resource_id}#{permission}@{subject_id}:{quantized_ts}"
```

**Design Principles:**

1. **Include All Relevant Factors:**
   - If context affects result, include in key
   - Example: IP-based access → include IP range in key

2. **Quantize Time:**
   - Use coarse timestamp granularity
   - Enables cache sharing across requests in window

3. **Hierarchical Keys:**
   - Enable wildcard invalidation
   - Example: `workspace:123:file:456:view:user:789`
   - Invalidate all workspace 123: `workspace:123:*`

4. **Compact Representation:**
   - Use IDs not names (shorter, immutable)
   - Hash if key too long (trade-off: harder debugging)

5. **Namespace Prefix:**
   - Prevent key collisions across different subsystems
   - Example: `permission_check:{key}` vs `file_metadata:{key}`

**Key Insight for Nexus:** Review current cache key design:
- Include all relevant factors (user, resource, permission, timestamp)
- Implement timestamp quantization for higher hit rates
- Support hierarchical invalidation (all permissions for user X)
- Use compact representation to reduce memory overhead

---

## 6. Batch/Bulk Operations

### 6.1 BulkCheck API

**Use Case:** Check many permissions in single request.

**SpiceDB BulkCheck:**
```protobuf
message BulkCheckRequest {
  repeated CheckItem items = 1;

  message CheckItem {
    Resource resource = 1;
    string permission = 2;
    Subject subject = 3;
  }
}

message BulkCheckResponse {
  repeated CheckResult results = 1;

  message CheckResult {
    bool is_member = 1;
    CheckItem item = 2;
  }
}
```

**Benefits:**
- Single network round-trip for multiple checks
- Batch database queries (fewer Spanner RPCs)
- Shared subproblem computation (user in group checked once)

**Performance:**
```python
# Inefficient: N network round-trips
results = []
for file in files:
    result = await check_permission(user, "view", file.id)
    results.append(result)

# Efficient: 1 network round-trip
items = [
    CheckItem(resource=file.id, permission="view", subject=user)
    for file in files
]
result = await bulk_check(items)
```

**Query Batching:**
SpiceDB can batch SQL queries for same resource type:

```sql
-- Instead of N queries
SELECT * FROM relationships WHERE resource_id = '123' ...
SELECT * FROM relationships WHERE resource_id = '456' ...
SELECT * FROM relationships WHERE resource_id = '789' ...

-- Single batched query
SELECT * FROM relationships
WHERE resource_id IN ('123', '456', '789') ...
```

**Deduplication:**
If multiple items share subproblems (e.g., same user, same group), compute once:

```python
# Checking user:alice view on 100 files in same folder
# All files inherit from folder:xyz
# folder:xyz#viewer@group:editors computed once
# group:editors#member@user:alice computed once
# Reused across all 100 file checks
```

**Key Insight for Nexus:** Implement BulkCheck API:
1. Accept array of permission checks in single request
2. Batch database queries for same resource types
3. Deduplicate shared subproblems
4. Return results in same order as input
5. Use for file listings, search results, batch operations

### 6.2 LookupResources Optimization

**Challenge:** "List all files user can access" is computationally expensive.

**Naive Approach:**
```python
# Extremely slow: O(N) permission checks
all_files = db.get_all_files()
accessible = []
for file in all_files:
    if check_permission(user, "view", file):
        accessible.append(file)
```

**Optimization 1: Cursor-based Pagination**

```python
def lookup_resources(user, permission, resource_type, cursor=None, limit=1000):
    # Stream results incrementally
    # Don't compute all results upfront
    results, next_cursor = spicedb.lookup_resources(
        subject=user,
        permission=permission,
        resource_type=resource_type,
        cursor=cursor,
        limit=limit
    )
    return results, next_cursor

# Client paginated through results
cursor = None
while True:
    batch, cursor = lookup_resources(user, "view", "file", cursor, 100)
    display_to_user(batch)
    if not cursor:
        break
```

**Benefits:**
- Lower memory usage (process in batches)
- Faster time-to-first-result (don't wait for all results)
- Can cancel early (user navigates away)

**Optimization 2: Materialized View**

*(See Section 3.4 - Pre-computed Permission Sets)*

**Pattern:**
1. Maintain `user_file_access` table in PostgreSQL
2. Update via Watch API when permissions change
3. Query PostgreSQL directly for list operations

```sql
-- Fast lookup (indexed query)
SELECT file_id FROM user_file_access
WHERE user_id = ? AND permission = 'view'
LIMIT 100 OFFSET 0;
```

**Optimization 3: BulkCheck Alternative**

When result set size is bounded and predictable:

```python
# List files in folder (bounded by folder size)
files_in_folder = db.get_files_in_folder(folder_id)  # e.g., 50 files

# BulkCheck faster than LookupResources for small sets
accessible = bulk_check([
    CheckItem(user, "view", file.id)
    for file in files_in_folder
])
```

**Performance Characteristics:**
- **LookupResources:** Slow, traverses full graph
- **LookupResources + Cursor:** Faster time-to-first-result
- **Materialized View:** Fastest, simple query
- **BulkCheck:** Fast for small bounded sets

**Key Insight for Nexus:** For file listings:
1. **Small folders (<100 files):** Use BulkCheck
2. **Large folders:** Use LookupResources with cursor
3. **Frequent access:** Build materialized view
4. **Monitor performance:** Switch strategies based on metrics

### 6.3 LookupSubjects Optimization

**Use Case:** "Who has access to this file?" (sharing UI)

**Challenge:** Potentially unbounded result set (all users in org).

**SpiceDB LookupSubjects:**
```python
subjects = spicedb.lookup_subjects(
    resource="file:123",
    permission="view",
    subject_type="user"
)

# Returns: [user:alice, user:bob, group:editors, ...]
```

**Limitation:** No cursor support (as of current version).

**Workarounds:**

**1. Result Limit:**
```python
# Return only first N subjects
subjects = spicedb.lookup_subjects(
    resource="file:123",
    permission="view",
    subject_type="user"
)[:100]

# UI shows "100+ users have access"
```

**2. Materialized View:**
```sql
-- Maintain file_access_grants table
CREATE TABLE file_access_grants (
    file_id TEXT,
    user_id TEXT,
    permission TEXT,
    granted_via TEXT  -- 'direct', 'group:editors', 'folder:xyz'
);

-- Query for sharing UI
SELECT user_id, granted_via
FROM file_access_grants
WHERE file_id = ? AND permission = 'view';
```

**3. Hierarchical Expansion:**

Show structure instead of flattened list:

```json
{
  "access": {
    "direct": ["user:alice", "user:bob"],
    "via_groups": {
      "group:editors": ["user:charlie", "user:dave"],
      "group:viewers": ["user:eve"]
    },
    "via_folders": {
      "folder:projects": "inherited from parent"
    }
  }
}
```

**Benefits:**
- More understandable than flat list
- Bounded at each level
- Shows permission structure

**Key Insight for Nexus:** For sharing UI:
1. Show hierarchical structure (direct + groups + inheritance)
2. Limit results per category (e.g., 10 direct, 5 groups)
3. Consider materialized view for files with many grants
4. Provide "show more" expansion for large lists

### 6.4 Streaming Large Result Sets

**Problem:** Large result sets consume memory and have high latency.

**Solution:** Stream results as they're computed.

**gRPC Streaming:**
```python
# Server-side streaming
async def lookup_resources_stream(request):
    async for resource in compute_accessible_resources(
        request.user,
        request.permission,
        request.resource_type
    ):
        yield LookupResult(resource_id=resource.id)

# Client consumes stream
async for result in client.lookup_resources_stream(request):
    display_to_user(result.resource_id)
```

**Benefits:**
- Constant memory usage (don't buffer all results)
- Lower latency (first results arrive quickly)
- Backpressure (slow client doesn't overwhelm server)

**Watch API Pattern:**

SpiceDB's Watch API is server-streaming:

```python
watch_stream = spicedb.watch(start_token=last_token)

async for event in watch_stream:
    # Process events as they arrive
    handle_permission_change(event)

    # Update checkpoint periodically
    if event.checkpoint:
        save_checkpoint(event.checkpoint_token)
```

**Key Insight for Nexus:** Implement streaming for:
1. **LookupResources:** Stream file IDs as they're found
2. **Watch API:** Stream permission changes in real-time
3. **Bulk Operations:** Stream batch results incrementally
4. **Audit Logs:** Stream historical permission changes

---

## 7. Recommended Optimizations for Nexus ReBAC

### 7.1 High-Impact Quick Wins

**1. Implement Timestamp Quantization (High Impact, Low Effort)**

**Current:** Every permission check uses unique timestamp.

**Proposed:** Round timestamps to 5-10 second boundaries.

```python
def quantize_timestamp(ts: datetime, quantum_seconds: int = 10) -> datetime:
    epoch = ts.timestamp()
    quantized_epoch = (epoch // quantum_seconds) * quantum_seconds
    return datetime.fromtimestamp(quantized_epoch)
```

**Expected Impact:**
- 10-100x improvement in cache hit rates
- Minimal code changes
- Configurable staleness (5-10 seconds acceptable for most operations)

**Implementation Steps:**
1. Add quantization to cache key generation
2. Add configuration for quantum duration
3. Monitor cache hit rate improvement
4. Adjust quantum based on workload

---

**2. Add Subproblem-Level Caching (High Impact, Medium Effort)**

**Current:** Cache only final permission check results.

**Proposed:** Cache intermediate steps (user in group, file in folder, etc.).

**Benefits:**
- Higher cache hit rate (subproblems shared across checks)
- Lower database load
- Faster permission checks

**Implementation Steps:**
1. Identify common subproblems (group membership, folder hierarchy)
2. Add caching layer for each subproblem type
3. Use same quantization strategy
4. Monitor cache hit rates per subproblem type

---

**3. Implement Request Deduplication (Medium Impact, Low Effort)**

**Current:** Concurrent identical requests all compute result.

**Proposed:** Coalesce concurrent requests for same permission.

```python
class RequestCoalescer:
    def __init__(self):
        self.pending = {}

    async def check_permission(self, user, permission, resource):
        key = f"{user}:{permission}:{resource}"

        if key in self.pending:
            return await self.pending[key]

        future = asyncio.create_task(self._compute(user, permission, resource))
        self.pending[key] = future

        try:
            return await future
        finally:
            del self.pending[key]
```

**Expected Impact:**
- 5-10x reduction in duplicate work for hot paths
- Especially valuable during cache warm-up
- Minimal performance overhead

---

**4. Add BulkCheck API (High Impact, Medium Effort)**

**Current:** File listings make N separate permission checks.

**Proposed:** Single API accepting array of checks.

```python
@app.post("/api/v1/permissions/bulk-check")
async def bulk_check(request: BulkCheckRequest) -> BulkCheckResponse:
    results = await permission_service.bulk_check(request.items)
    return BulkCheckResponse(results=results)
```

**Expected Impact:**
- 10-100x faster file listings
- Reduced network overhead
- Better database query batching

---

### 7.2 Medium-Term Improvements

**5. Implement Watch API for Permission Changes (High Impact, High Effort)**

**Purpose:** Enable event-based cache invalidation and audit logging.

**Design:**
```python
@app.get("/api/v1/permissions/watch")
async def watch_permissions(
    start_token: Optional[str] = None,
    checkpoint_interval: int = 60
):
    # Server-streaming gRPC or WebSocket
    async for event in permission_service.watch_changes(start_token):
        yield PermissionChangeEvent(
            change_type=event.type,  # GRANTED, REVOKED
            user_id=event.user_id,
            resource_id=event.resource_id,
            permission=event.permission,
            token=event.token  # Resume token
        )
```

**Use Cases:**
1. Cache invalidation (invalidate affected entries on permission change)
2. Audit logging (track who granted/revoked permissions when)
3. Real-time notifications (notify user when granted access)
4. Materialized view maintenance (update denormalized tables)

**Implementation Steps:**
1. Add permission change tracking to database (append-only log)
2. Implement streaming API (WebSocket or Server-Sent Events)
3. Add resume token support (clients can reconnect from last position)
4. Build cache invalidation consumer
5. Add audit logging consumer

---

**6. Build Materialized Views for Common Queries (High Impact, High Effort)**

**Target:** File listing operations ("files I can access").

**Approach:**
1. Create `user_file_access` table in PostgreSQL
2. Populate via background job or Watch API
3. Query PostgreSQL for file listings
4. Fall back to real-time check if view stale

```sql
CREATE TABLE user_file_access (
    user_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    granted_via TEXT,  -- 'direct', 'group:X', 'folder:Y'
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, file_id, permission)
);

CREATE INDEX idx_user_file_access_user ON user_file_access(user_id);
```

**Expected Impact:**
- 100-1000x faster file listings
- Reduced permission service load
- Better user experience (instant results)

---

**7. Distributed Cache with Consistent Hashing (Medium Impact, High Effort)**

**Current:** Each node has independent cache.

**Proposed:** Shared cache distributed across nodes.

**Architecture:**
1. Use Redis Cluster or similar for shared cache
2. Implement consistent hashing for key distribution
3. Route permission check subproblems to appropriate node
4. Deduplicate requests across nodes

**Benefits:**
- Higher cache hit rate (shared across all nodes)
- Better resource utilization
- Request deduplication across nodes

**Trade-offs:**
- Network latency to cache (mitigate with local L1 cache)
- Operational complexity (Redis cluster management)
- Potential SPOF (mitigate with replication)

---

### 7.3 Advanced Optimizations

**8. Implement Leopard-Style Index for Nested Groups (Medium Impact, High Effort)**

**Use Case:** If Nexus has deeply nested group/folder hierarchies.

**Approach:**
1. Maintain in-memory transitive closure of group/folder memberships
2. Update incrementally on permission changes
3. Query index for nested membership checks

**Expected Impact:**
- 10-100x faster nested group resolution
- Reduced database load
- Lower latency for permission checks

**When to Implement:**
- Folder hierarchies >5 levels deep are common
- Group nesting >3 levels deep
- Profile shows nested lookups as bottleneck

---

**9. Add Request Hedging for Tail Latency (Low Impact, Medium Effort)**

**Purpose:** Prevent occasional slow operations from impacting P95/P99 latency.

**Implementation:**
```python
async def check_permission_with_hedging(user, permission, resource):
    # Send first request
    primary = asyncio.create_task(
        check_permission_primary(user, permission, resource)
    )

    # Wait for dynamic threshold (e.g., P95 latency)
    try:
        return await asyncio.wait_for(primary, timeout=p95_latency)
    except asyncio.TimeoutError:
        # Send hedged request
        secondary = asyncio.create_task(
            check_permission_secondary(user, permission, resource)
        )

        # Return first to complete
        done, pending = await asyncio.wait(
            [primary, secondary],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel slower request
        for task in pending:
            task.cancel()

        return done.pop().result()
```

**Expected Impact:**
- Improved P95/P99 latency
- ~1% traffic overhead (hedged requests)
- Better UX for operations requiring many checks

---

**10. Implement Negative Caching (Low Impact, Low Effort)**

**Purpose:** Cache DENIED results to prevent repeated unauthorized access attempts.

**Implementation:**
```python
class PermissionCache:
    async def check(self, user, permission, resource):
        key = self._make_key(user, permission, resource)

        # Check cache for both GRANTED and DENIED
        cached = await self.cache.get(key)
        if cached is not None:
            return cached == "GRANTED"

        # Compute and cache result
        granted = await self._compute(user, permission, resource)
        await self.cache.set(
            key,
            "GRANTED" if granted else "DENIED",
            ttl=60 if granted else 10  # Shorter TTL for DENIED
        )
        return granted
```

**Expected Impact:**
- Reduced load from unauthorized access attempts
- Better protection against brute-force attacks
- Minimal code changes

---

## 8. Performance Monitoring & Metrics

### 8.1 Key Metrics to Track

**1. Cache Performance:**
- Cache hit rate (overall and per subproblem type)
- Cache size (memory usage)
- Cache eviction rate
- Average cache key TTL

**2. Permission Check Latency:**
- P50, P95, P99 latency
- Latency breakdown (cache lookup, database query, computation)
- Hedging rate (% of requests hedged)

**3. Database Performance:**
- Query rate (reads/writes per second)
- Query latency (P50, P95, P99)
- Connection pool utilization
- Slow query log

**4. Throughput:**
- Permission checks per second
- Bulk check throughput
- Lookup operations per second

**5. Cache Invalidation:**
- Invalidation rate (events per second)
- Invalidation latency (event to cache clear)
- Watch API lag (if implemented)

**6. Error Rates:**
- Permission check errors
- Cache errors
- Database errors
- Timeout rate

### 8.2 Optimization Opportunities

**Monitor for:**
1. **Low cache hit rate (<40%):** Indicates poor cache key design or short TTLs
2. **High P99 latency (>100ms):** Indicates tail latency issues (consider hedging)
3. **High database query rate:** Indicates insufficient caching or poor query batching
4. **High cache eviction rate:** Indicates cache too small or poor eviction policy
5. **Slow queries (>50ms):** Indicates missing indexes or inefficient queries

---

## 9. Implementation Priority Roadmap

### Phase 1: Quick Wins (1-2 weeks)
1. **Timestamp Quantization:** Immediate cache hit rate improvement
2. **Request Deduplication:** Reduce duplicate work
3. **Negative Caching:** Handle unauthorized access better
4. **Metrics/Monitoring:** Establish baseline for optimization

**Expected Impact:** 2-5x improvement in cache hit rate, 20-50% reduction in database load.

---

### Phase 2: Core Optimizations (1-2 months)
1. **Subproblem-Level Caching:** Higher cache hit rates
2. **BulkCheck API:** Faster file listings
3. **Watch API (Basic):** Enable event-based invalidation
4. **Cache Invalidation:** Reduce staleness

**Expected Impact:** 5-10x improvement in permission check performance, event-based cache invalidation.

---

### Phase 3: Advanced Features (2-4 months)
1. **Materialized Views:** Ultra-fast file listings
2. **Distributed Cache:** Horizontal scaling
3. **LookupResources with Cursor:** Handle large result sets
4. **Audit Logging via Watch:** Compliance and security

**Expected Impact:** 10-100x improvement in list operations, scalable to millions of users.

---

### Phase 4: Scaling & Optimization (Ongoing)
1. **Leopard-Style Index:** Handle deep hierarchies
2. **Request Hedging:** Improve tail latency
3. **Multi-Tenant Optimization:** Per-workspace caching
4. **Performance Tuning:** Continuous optimization based on metrics

**Expected Impact:** 99.9%+ availability, <10ms P95 latency, support for 100K+ QPS.

---

## 10. References & Sources

### Google Zanzibar
- [Zanzibar: Google's Consistent, Global Authorization System (USENIX ATC 2019)](https://www.usenix.org/system/files/atc19-pang.pdf)
- [The Google Zanzibar Paper, annotated by AuthZed](https://authzed.com/zanzibar)
- [Understanding Google Zanzibar: A Comprehensive Overview](https://authzed.com/blog/what-is-google-zanzibar)
- [Insights from Paper (Part II) — Zanzibar](https://hemantkgupta.medium.com/insights-from-paper-part-ii-zanzibar-googles-consistent-global-authorization-system-317309e2f6ae)

### SpiceDB/AuthZed
- [How Caching Works in SpiceDB](https://authzed.com/blog/how-caching-works-in-spicedb)
- [Implementing Google Zanzibar Open Source: The Architecture of SpiceDB](https://authzed.com/blog/spicedb-architecture)
- [Hotspot Caching in Google Zanzibar and SpiceDB](https://authzed.com/blog/hotspot-caching-in-google-zanzibar-and-spicedb)
- [Google-Scale Authorization: Getting to 1 Million QPS on SpiceDB](https://authzed.com/blog/google-scale-authorization)
- [LookupSubjects and SpiceDB v1.12.0](https://authzed.com/blog/lookup-subjects)
- [Optimizing Latencies with SpiceDB's Distributed Authorization Database and Consistent Hashing](https://authzed.com/blog/consistent-hash-load-balancing-grpc)
- [Zed Tokens, Zookies, Consistency for Authorization](https://authzed.com/blog/zedtokens)
- [Watching Relationship Changes - Authzed Docs](https://authzed.com/docs/spicedb/concepts/watch)

### OpenFGA & Ory Keto
- [Top 5 Google Zanzibar open-source implementations in 2024 — WorkOS](https://workos.com/blog/top-5-google-zanzibar-open-source-implementations-in-2024)
- [Future-Proofing Authorization: Leveraging OpenFGA for Enhanced Security and Scalability](https://blog.swcode.io/authz/2024/06/14/authz-openfga-introduction/)
- [Google's Zanzibar and Beyond: A Deep Dive into Relation-based Authorization (Keto)](https://blog.swcode.io/authz/2023/10/13/authz-keto-introduction/)

### Multi-Tenancy
- [How to Choose the Right Authorization Model for Your Multi-Tenant SaaS Application](https://auth0.com/blog/how-to-choose-the-right-authorization-model-for-your-multi-tenant-saas-application/)
- [Best Practices for Multi-Tenant Authorization](https://www.permit.io/blog/best-practices-for-multi-tenant-authorization)
- [Tenant isolation in multi-tenant systems — WorkOS](https://workos.com/blog/tenant-isolation-in-multi-tenant-systems)

### Caching & Performance
- [Cache Invalidation Strategies Time-Based vs Event-Driven](https://leapcell.io/blog/cache-invalidation-strategies-time-based-vs-event-driven)
- [How to Optimize Performance with Cache Warming?](https://newsletter.scalablethread.com/p/how-to-optimize-performance-with)
- [What is Cache Warming? - GeeksforGeeks](https://www.geeksforgeeks.org/system-design/what-is-cache-warming/)

### Graph Traversal & Optimization
- [SpiceDB and Authzed: Resolving Permissions with Intersection and Exclusion](https://authzed.com/blog/check-it-out-2)
- [Graph Traversal Algorithms Explained: DFS, BFS & Applications](https://www.puppygraph.com/blog/graph-traversal)

### Performance Metrics
- [Mastering Latency Metrics: P90, P95, P99](https://medium.com/javarevisited/mastering-latency-metrics-p90-p95-p99-d5427faea879)
- [What Is P99 Latency? Understanding the 99th Percentile of Performance](https://aerospike.com/blog/what-is-p99-latency/)

### Formally Verified Authorization
- [Formally Verified Cloud-Scale Authorization (ICSE 2025)](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/119/Formally-Verified-Cloud-Scale-Authorization) - 1 billion checks/second with formal verification

---

## Appendix A: Glossary

**ACL (Access Control List):** List of permissions attached to an object specifying which users can perform which operations.

**BulkCheck:** API for checking multiple permissions in a single request.

**Cache Stampede:** Situation where many concurrent requests all miss cache and overwhelm backend (thundering herd).

**Consistent Hashing:** Hash function that minimizes remapping when nodes are added/removed.

**Dispatch:** Process of decomposing permission check into subproblems and routing to appropriate nodes.

**Hedging:** Sending duplicate requests to multiple servers to reduce tail latency.

**Leopard:** Google's indexing system for flattening deeply nested group hierarchies.

**Lookup APIs:** LookupResources (find accessible resources) and LookupSubjects (find users with access).

**New Enemy Problem:** User loses access but cached permission grants it (cache staleness issue).

**Quantization:** Rounding timestamps to coarse granularity to enable cache sharing.

**ReBAC (Relationship-Based Access Control):** Authorization model based on relationships between entities.

**Subproblem:** Decomposed unit of permission check that can be cached and executed independently.

**Watch API:** Streaming API for observing permission changes in real-time.

**Zookie/ZedToken:** Consistency token encoding minimum required freshness for permission check.

---

## Appendix B: Code Examples

### Example 1: Timestamp Quantization

```python
from datetime import datetime, timedelta

class QuantizedCache:
    def __init__(self, quantum_seconds: int = 10):
        self.quantum_seconds = quantum_seconds
        self.cache = {}

    def _quantize_timestamp(self, ts: datetime) -> datetime:
        """Round timestamp up to next quantum boundary"""
        epoch = ts.timestamp()
        quantum = self.quantum_seconds
        quantized_epoch = ((int(epoch) // quantum) + 1) * quantum
        return datetime.fromtimestamp(quantized_epoch)

    def _make_key(self, user: str, permission: str, resource: str, ts: datetime) -> str:
        quantized_ts = self._quantize_timestamp(ts)
        return f"{user}:{permission}:{resource}:{quantized_ts.isoformat()}"

    def get(self, user: str, permission: str, resource: str) -> bool | None:
        key = self._make_key(user, permission, resource, datetime.now())
        return self.cache.get(key)

    def set(self, user: str, permission: str, resource: str, granted: bool):
        key = self._make_key(user, permission, resource, datetime.now())
        self.cache[key] = granted
```

### Example 2: Request Deduplication

```python
import asyncio
from typing import Dict, Any

class RequestDeduplicator:
    def __init__(self):
        self.pending: Dict[str, asyncio.Future] = {}
        self.lock = asyncio.Lock()

    async def execute(self, key: str, compute_fn: callable) -> Any:
        """
        Execute compute_fn for the given key, deduplicating concurrent requests.

        If another request is already computing this key, wait for its result.
        Otherwise, compute the result and share it with other concurrent requests.
        """
        async with self.lock:
            if key in self.pending:
                # Another request is already computing this
                future = self.pending[key]
                # Release lock while waiting

        # Wait for result outside lock
        if key in self.pending:
            return await future

        # We're the first request for this key
        async with self.lock:
            # Double-check after acquiring lock
            if key in self.pending:
                return await self.pending[key]

            # Create future for this computation
            future = asyncio.create_task(compute_fn())
            self.pending[key] = future

        try:
            result = await future
            return result
        finally:
            async with self.lock:
                # Remove from pending when done
                if key in self.pending:
                    del self.pending[key]

# Usage
deduplicator = RequestDeduplicator()

async def check_permission(user: str, permission: str, resource: str) -> bool:
    key = f"{user}:{permission}:{resource}"

    async def compute():
        # Expensive permission check
        return await database.check_permission(user, permission, resource)

    return await deduplicator.execute(key, compute)
```

### Example 3: Watch API Consumer for Cache Invalidation

```python
import asyncio
from typing import AsyncIterator

class PermissionWatchConsumer:
    def __init__(self, cache, watch_api):
        self.cache = cache
        self.watch_api = watch_api
        self.last_token = None

    async def start(self):
        """Start consuming watch events and invalidating cache"""
        while True:
            try:
                async for event in self.watch_api.watch(self.last_token):
                    await self._handle_event(event)
                    self.last_token = event.token
            except Exception as e:
                print(f"Watch error: {e}, reconnecting...")
                await asyncio.sleep(5)

    async def _handle_event(self, event):
        """Invalidate affected cache entries on permission change"""
        if event.type == "PERMISSION_GRANTED":
            # Invalidate cached DENIED results for this permission
            await self._invalidate_permission(
                event.user_id,
                event.permission,
                event.resource_id
            )

        elif event.type == "PERMISSION_REVOKED":
            # Invalidate cached GRANTED results
            await self._invalidate_permission(
                event.user_id,
                event.permission,
                event.resource_id
            )

        elif event.type == "GROUP_MEMBERSHIP_ADDED":
            # Invalidate all permissions for users in this group
            await self._invalidate_group_members(event.group_id)

    async def _invalidate_permission(self, user: str, permission: str, resource: str):
        """Invalidate all cached results for this permission"""
        # Pattern matching for cache keys with different timestamps
        pattern = f"{user}:{permission}:{resource}:*"
        await self.cache.delete_pattern(pattern)

    async def _invalidate_group_members(self, group_id: str):
        """Invalidate all cached results for members of this group"""
        # This is more complex - need to find all permissions granted via this group
        # For simplicity, could invalidate all cached results for group members
        members = await self._get_group_members(group_id)
        for member in members:
            pattern = f"{member}:*"
            await self.cache.delete_pattern(pattern)
```

### Example 4: BulkCheck Implementation

```python
from dataclasses import dataclass
from typing import List

@dataclass
class CheckItem:
    user_id: str
    permission: str
    resource_id: str

@dataclass
class CheckResult:
    item: CheckItem
    granted: bool

class PermissionService:
    async def bulk_check(self, items: List[CheckItem]) -> List[CheckResult]:
        """
        Check multiple permissions in a single request.

        Optimizations:
        1. Batch database queries by resource type
        2. Deduplicate subproblems (shared group memberships, etc.)
        3. Parallel execution of independent checks
        """
        results = []

        # Group items by resource type for batching
        by_resource_type = self._group_by_resource_type(items)

        # Process each resource type in parallel
        tasks = []
        for resource_type, type_items in by_resource_type.items():
            tasks.append(self._check_resource_type(resource_type, type_items))

        batch_results = await asyncio.gather(*tasks)

        # Flatten results
        for batch in batch_results:
            results.extend(batch)

        return results

    async def _check_resource_type(
        self,
        resource_type: str,
        items: List[CheckItem]
    ) -> List[CheckResult]:
        """Check all items of same resource type with batched queries"""

        # Extract unique resource IDs
        resource_ids = list({item.resource_id for item in items})

        # Batch fetch all relationships for these resources
        relationships = await self.db.get_relationships_batch(
            resource_type=resource_type,
            resource_ids=resource_ids
        )

        # Check each item using prefetched data
        results = []
        for item in items:
            granted = self._evaluate_permission(
                item.user_id,
                item.permission,
                item.resource_id,
                relationships
            )
            results.append(CheckResult(item=item, granted=granted))

        return results
```

---

*End of Research Document*
