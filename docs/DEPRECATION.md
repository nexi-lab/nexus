# Deprecated Features & Migration Guide

**Last Updated:** 2026-01-03
**Related:** Phase 1 - Task 1.4 (Issue #987), Phase 2 - Task 2.3 (Issue #988)

---

## Overview

This document catalogs all deprecated features in Nexus, explains why they were deprecated, and provides migration paths for each. Deprecated features are grouped by category and scheduled for removal in specific versions.

**Deprecation Status Legend:**
- 🟡 **Soft Deprecated:** Warnings shown, still functional
- 🟠 **Hard Deprecated:** Raises errors with migration guidance
- 🔴 **Removed:** No longer available (for historical reference)

---

## Table of Contents

1. [Security & Permission System](#1-security--permission-system)
2. [Context & Identity Management](#2-context--identity-management)
3. [API Parameter Changes](#3-api-parameter-changes)
4. [Cache & Performance](#4-cache--performance)
5. [Storage & Database](#5-storage--database)
6. [Authentication](#6-authentication)
7. [Configuration](#7-configuration)
8. [Removal Timeline](#removal-timeline)

---

## 1. Security & Permission System

### 1.1 UNIX-Style Permission Operations (🔴 Removed)

**Status:** Hard deprecated - raises `NotImplementedError`
**Deprecated In:** v0.4.0
**Removed In:** v0.5.0
**Replacement:** ReBAC (Relationship-Based Access Control)

#### Deprecated Methods:

```python
# ❌ DEPRECATED: chmod()
nx.chmod(path="/file.txt", mode=0o644)

# ✅ REPLACEMENT: Use ReBAC
nx.rebac_create(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/file.txt")
)
```

```python
# ❌ DEPRECATED: chown()
nx.chown(path="/file.txt", owner="alice")

# ✅ REPLACEMENT: Use ReBAC
nx.rebac_create(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/file.txt")
)
```

```python
# ❌ DEPRECATED: chgrp()
nx.chgrp(path="/file.txt", group="developers")

# ✅ REPLACEMENT: Use ReBAC
nx.rebac_create(
    subject=("group", "developers"),
    relation="can-write",
    object=("file", "/file.txt")
)
```

**Why Deprecated:**
- UNIX permissions (owner/group/mode) are too simple for modern access control
- Cannot express complex organizational hierarchies
- ReBAC provides fine-grained, relationship-based permissions
- Aligns with Google Zanzibar model (industry standard)

**Migration:**
See [docs/migration/unix-to-rebac.md](docs/migration/unix-to-rebac.md) for detailed migration guide.

---

### 1.2 ACL Operations (🔴 Removed)

**Status:** Hard deprecated - raises `NotImplementedError`
**Deprecated In:** v0.4.0
**Removed In:** v0.5.0
**Replacement:** ReBAC API

#### Deprecated Methods:

```python
# ❌ DEPRECATED: grant_user()
nx.grant_user(path="/file.txt", user="alice", permissions="rwx")

# ✅ REPLACEMENT: Use ReBAC relations
nx.rebac_create(
    subject=("user", "alice"),
    relation="can-read",
    object=("file", "/file.txt")
)
nx.rebac_create(
    subject=("user", "alice"),
    relation="can-write",
    object=("file", "/file.txt")
)
```

```python
# ❌ DEPRECATED: grant_group()
nx.grant_group(path="/file.txt", group="team", permissions="r--")

# ✅ REPLACEMENT: Use ReBAC with groups
nx.rebac_create(
    subject=("group", "team"),
    relation="can-read",
    object=("file", "/file.txt")
)
```

```python
# ❌ DEPRECATED: deny()
nx.deny(path="/file.txt", user="bob")

# ✅ REPLACEMENT: Use ReBAC with negative tuples or simply don't grant
# (ReBAC is deny-by-default, so just don't create the permission)
```

```python
# ❌ DEPRECATED: revoke()
nx.revoke(path="/file.txt", user="alice")

# ✅ REPLACEMENT: Use rebac_delete()
nx.rebac_delete(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/file.txt")
)
```

```python
# ❌ DEPRECATED: get_acl()
acl_entries = nx.get_acl(path="/file.txt")

# ✅ REPLACEMENT: Use rebac_list_tuples()
tuples = nx.rebac_list_tuples(object=("file", "/file.txt"))
```

**Why Deprecated:**
- ACL model is less expressive than ReBAC
- ACL cannot model transitive relationships (e.g., "admins of parent folder")
- ReBAC is more scalable and cacheable
- Industry trend toward relationship-based access control

**Files Affected:**
- [nexus/remote/client.py:2074-2290](nexus/src/nexus/remote/client.py#L2074-L2290)

---

### 1.3 ACL Store Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `rebac_manager` parameter

```python
# ❌ DEPRECATED:
from nexus.core.permissions import PermissionEnforcer

enforcer = PermissionEnforcer(
    metadata_store=metadata,
    acl_store=acl_store  # ⚠️  Deprecated parameter
)

# ✅ REPLACEMENT:
enforcer = PermissionEnforcer(
    metadata_store=metadata,
    rebac_manager=rebac_manager  # Use ReBAC manager instead
)
```

**Warning Message:**
```
DeprecationWarning: acl_store parameter is deprecated and will be removed in v0.7.0.
Use ReBAC for all permissions.
```

**Files Affected:**
- [nexus/core/permissions.py:205](nexus/src/nexus/core/permissions.py#L205)
- [nexus/core/memory_permission_enforcer.py:41](nexus/src/nexus/core/memory_permission_enforcer.py#L41)

---

### 1.4 Direct ReBACManager Instantiation (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning
**Deprecated In:** Phase 2 (v0.6.0)
**Removal Planned:** v0.8.0
**Replacement:** Use `EnhancedReBACManager` for production code

**Related:** Phase 2 Task 2.3, Issue #988, [REBAC_CONSOLIDATION_ANALYSIS.md](REBAC_CONSOLIDATION_ANALYSIS.md)

```python
# ❌ DEPRECATED: Direct ReBACManager instantiation
from nexus.core.rebac_manager import ReBACManager

manager = ReBACManager(engine)  # ⚠️  Missing P0 fixes and optimizations

# ✅ REPLACEMENT: Use EnhancedReBACManager
from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

manager = EnhancedReBACManager(
    engine,
    enable_graph_limits=True,      # P0-5: DoS protection
    enable_leopard=True,            # Leopard: O(1) group lookups
    enable_tiger_cache=True,        # Tiger: Advanced caching
    enforce_tenant_isolation=True  # P0-2: Tenant security
)

# Then use consistency levels for cache control (P0-1)
result = manager.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/doc.txt"),
    zone_id="org_123",
    consistency=ConsistencyLevel.STRONG  # Bypass cache for critical checks
)
```

**Why Deprecated:**
- **Missing P0 GA Fixes:** Base ReBACManager lacks production-ready features
  - P0-1: No consistency levels or version tokens
  - P0-2: No tenant isolation enforcement
  - P0-5: No graph limits or DoS protection
- **Performance:** Missing Leopard (O(1) group lookups) and Tiger cache
- **Security:** No timeout protection or fan-out limits for pathological graphs
- **Observability:** No CheckResult metadata (decision time, cache stats, traversal info)

**What EnhancedReBACManager Adds:**
1. **P0-1 Consistency Levels:** EVENTUAL/BOUNDED/STRONG cache control with version tokens
2. **P0-2 Tenant Isolation:** Enforces same-tenant relationships, prevents cross-tenant traversal
3. **P0-5 Graph Limits:** MAX_DEPTH=50, MAX_FAN_OUT=1000, 1s timeout, memory bounds
4. **Leopard Optimization:** Pre-computed transitive group closure for instant group checks
5. **Tiger Cache:** Advanced caching with iterator-based invalidation
6. **Rich Metadata:** CheckResult with decision time, cache age, traversal stats, indeterminate flags

**When Still Safe to Use:**
- **Testing:** Test fixtures can use ReBACManager for simplicity
- **Base Class:** ReBACManager remains the base for TenantAware and Enhanced
- **Legacy Code:** Existing code continues to work (with deprecation warning)

**Migration Path:**
1. Replace `ReBACManager` imports with `EnhancedReBACManager`
2. Enable P0 fixes in constructor (all default to True for safety)
3. Update permission checks to use `ConsistencyLevel` for cache control
4. Leverage `CheckResult` metadata for debugging and monitoring
5. See [REBAC_CONSOLIDATION_ANALYSIS.md](REBAC_CONSOLIDATION_ANALYSIS.md) for details

**Warning Message:**
```
DeprecationWarning: Direct instantiation of ReBACManager is deprecated.
Use EnhancedReBACManager for production code (includes P0 fixes,
Leopard optimization, Tiger cache, and graph limits).
See REBAC_CONSOLIDATION_ANALYSIS.md for migration guide.
```

**Files Affected:**
- [nexus/core/rebac_manager.py:96-106](nexus/src/nexus/core/rebac_manager.py#L96-L106) (deprecation warning)
- [nexus/core/memory_api.py:67](nexus/src/nexus/core/memory_api.py#L67) (needs migration)
- [nexus/core/memory_router.py:330](nexus/src/nexus/core/memory_router.py#L330) (needs migration)
- [nexus/cli/commands/server.py:1051](nexus/src/nexus/cli/commands/server.py#L1051) (needs migration)

**Production Code Already Using Enhanced:**
- ✅ **NexusFS Core:** Uses EnhancedReBACManager (nexus_fs.py:273)
- ✅ **FastAPI Server:** Uses AsyncReBACManager (needs P0 fixes ported)

---

## 2. Context & Identity Management

### 2.1 Instance-Level zone_id/agent_id (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows security warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Pass context to each method call

```python
# ❌ DEPRECATED (SECURITY RISK in server mode):
nx = NexusFS(
    backend=backend,
    zone_id="tenant-123",  # ⚠️  Instance-level zone_id
    agent_id="agent-456"      # ⚠️  Instance-level agent_id
)

# This is UNSAFE if NexusFS instance is shared across multiple users!

# ✅ REPLACEMENT (safe for server mode):
nx = NexusFS(backend=backend)  # No instance-level identity

# Pass context to each operation:
nx.write(
    path="/file.txt",
    content=b"data",
    context=OperationContext(
        zone_id="tenant-123",
        agent_id="agent-456",
        user_id="user-789"
    )
)
```

**Why Deprecated:**
- **SECURITY RISK:** In server mode, a shared NexusFS instance serves multiple users
- Instance-level zone_id/agent_id causes identity confusion
- Can lead to privilege escalation if not handled carefully
- Server mode MUST use per-request context for security

**When Still Safe to Use:**
- Embedded mode (single-user CLI applications)
- Testing environments
- Development scripts where one NexusFS = one user

**Warning Message:**
```
DeprecationWarning: zone_id and agent_id parameters in NexusFS.__init__() are DEPRECATED.
They should only be used in embedded/CLI mode where a single NexusFS instance serves one user.
For server mode (shared NexusFS instance serving multiple users), these MUST be None and
context must be passed to each method call instead.
Using instance-level zone_id/agent_id in server mode creates SECURITY RISKS!
```

**Files Affected:**
- [nexus/core/nexus_fs.py:113-158](nexus/src/nexus/core/nexus_fs.py#L113-L158)

---

### 2.2 Backward Compatibility Properties (🟡 Soft Deprecated)

**Status:** Soft deprecated - still works but discouraged
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0

```python
# ❌ DEPRECATED:
tenant = nx.zone_id  # Instance property
agent = nx.agent_id    # Instance property
user = nx.user_id      # Instance property

# ✅ REPLACEMENT:
# Access from context parameter in each operation
context = OperationContext(zone_id="...", agent_id="...", user_id="...")
nx.write(path="/file.txt", content=b"data", context=context)
```

**Files Affected:**
- [nexus/core/nexus_fs.py:850-863](nexus/src/nexus/core/nexus_fs.py#L850-L863)

---

### 2.3 Zone ID in Cache Classes (🟡 Soft Deprecated)

**Status:** Soft deprecated - parameter ignored
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0

```python
# ❌ DEPRECATED:
cache = TigerReBACCache(
    db_session=session,
    zone_id="tenant-123"  # ⚠️  Ignored parameter (kept for API compat)
)

# ✅ REPLACEMENT:
cache = TigerReBACCache(db_session=session)
# zone_id is now handled per-operation via context
```

**Why Deprecated:**
- Multi-tenancy should be handled at the operation level, not cache level
- Simplifies cache initialization
- Aligns with context-based architecture

**Files Affected:**
- [nexus/core/tiger_cache.py:93](nexus/src/nexus/core/tiger_cache.py#L93)
- [nexus/core/tiger_cache.py:497](nexus/src/nexus/core/tiger_cache.py#L497)

---

## 3. API Parameter Changes

### 3.1 agent_id Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `workspace_path` parameter

```python
# ❌ DEPRECATED:
nx.list_workspace_files(agent_id="agent-123")
nx.save_workspace_file(agent_id="agent-123", file_path="file.txt", content=b"data")
nx.load_workspace_file(agent_id="agent-123", file_path="file.txt")
nx.delete_workspace_file(agent_id="agent-123", file_path="file.txt")

# ✅ REPLACEMENT:
nx.list_workspace_files(workspace_path="/workspaces/agent-123")
nx.save_workspace_file(workspace_path="/workspaces/agent-123", file_path="file.txt", content=b"data")
nx.load_workspace_file(workspace_path="/workspaces/agent-123", file_path="file.txt")
nx.delete_workspace_file(workspace_path="/workspaces/agent-123", file_path="file.txt")
```

**Why Deprecated:**
- `workspace_path` is more explicit and flexible
- Supports arbitrary workspace organization (not just agent-based)
- Clearer API semantics

**Warning Message:**
```
DeprecationWarning: agent_id parameter is deprecated. Use workspace_path parameter instead.
Conversion: workspace_path = f"/workspaces/{agent_id}"
```

**Files Affected:**
- [nexus/core/nexus_fs.py:2837-3075](nexus/src/nexus/core/nexus_fs.py#L2837-L3075)
- [nexus/core/filesystem.py:539-611](nexus/src/nexus/core/filesystem.py#L539-L611)

---

### 3.2 custom_parsers Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - still works
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `parse_providers` parameter

```python
# ❌ DEPRECATED:
nx = NexusFS(
    backend=backend,
    custom_parsers={
        "pdf": MyPDFParser,
        "docx": MyDocxParser
    }
)

# ✅ REPLACEMENT:
nx = NexusFS(
    backend=backend,
    parse_providers=[
        {"name": "unstructured", "priority": 1, "api_key": "..."},
        {"name": "llamaparse", "priority": 2, "api_key": "..."},
        {"name": "markitdown", "priority": 3}  # Local fallback
    ]
)
```

**Why Deprecated:**
- New provider system is more flexible
- Supports priority-based fallback
- Supports both API-based and local parsers
- Better error handling and retries

**Files Affected:**
- [nexus/core/nexus_fs.py:125](nexus/src/nexus/core/nexus_fs.py#L125)
- [nexus/config.py:153](nexus/src/nexus/config.py#L153)

---

### 3.3 overwrite/skip_existing Parameters (🟡 Soft Deprecated)

**Status:** Soft deprecated - CLI shows warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `--conflict-mode` option

```bash
# ❌ DEPRECATED:
nexus metadata import --overwrite metadata.json
nexus metadata import --skip-existing metadata.json

# ✅ REPLACEMENT:
nexus metadata import --conflict-mode=overwrite metadata.json
nexus metadata import --conflict-mode=skip metadata.json
nexus metadata import --conflict-mode=error metadata.json  # Fail on conflict
```

**Why Deprecated:**
- More explicit conflict resolution strategy
- Supports additional modes (error, merge, etc.)
- Consistent with other CLI tools

**Files Affected:**
- [nexus/cli/commands/metadata.py:206-260](nexus/src/nexus/cli/commands/metadata.py#L206-L260)

---

### 3.4 prefix Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - backward compatibility only
**Deprecated In:** v0.5.0
**Replacement:** Use `path` parameter with glob patterns

```python
# ❌ DEPRECATED:
results = nx.search(query="README", prefix="/docs/")

# ✅ REPLACEMENT:
results = nx.search(query="README", path="/docs/**")
# or
results = nx.search(query="README", filters={"path_prefix": "/docs/"})
```

**Why Deprecated:**
- Less intuitive than glob patterns
- New filter system is more flexible
- Aligns with standard glob syntax

**Files Affected:**
- [nexus/core/nexus_fs.py:2267](nexus/src/nexus/core/nexus_fs.py#L2267)
- [nexus/core/nexus_fs_search.py:229](nexus/src/nexus/core/nexus_fs_search.py#L229)
- [nexus/core/filesystem.py:259](nexus/src/nexus/core/filesystem.py#L259)
- [nexus/skills/protocols.py:227](nexus/src/nexus/skills/protocols.py#L227)

---

### 3.5 keyword_weight/semantic_weight Parameters (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `alpha` parameter

```python
# ❌ DEPRECATED:
results = nx.hybrid_search(
    query="neural networks",
    keyword_weight=0.3,
    semantic_weight=0.7
)

# ✅ REPLACEMENT:
results = nx.hybrid_search(
    query="neural networks",
    alpha=0.7  # 0 = pure keyword, 1 = pure semantic
)
```

**Why Deprecated:**
- Simplified to single parameter (alpha)
- Matches industry standard (Weaviate, etc.)
- Less confusion about weight normalization

**Warning Message:**
```
DeprecationWarning: keyword_weight and semantic_weight are deprecated.
Use alpha instead: alpha=0 (pure keyword) to alpha=1 (pure semantic).
```

**Files Affected:**
- [nexus/search/vector_db.py:992-1015](nexus/src/nexus/search/vector_db.py#L992-L1015)

---

### 3.6 context vs subject Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - context still works
**Deprecated In:** v0.5.0
**Replacement:** Use `subject` parameter in ReBAC operations

```python
# ❌ DEPRECATED:
nx.rebac_check(
    context={"user_id": "alice"},
    relation="owner",
    object=("file", "/file.txt")
)

# ✅ REPLACEMENT:
nx.rebac_check(
    subject=("user", "alice"),
    relation="owner",
    object=("file", "/file.txt")
)
```

**Why Deprecated:**
- More explicit about entity type
- Aligns with Zanzibar tuple format
- Better type safety

**Files Affected:**
- [nexus/core/nexus_fs.py:1384](nexus/src/nexus/core/nexus_fs.py#L1384)

---

## 4. Cache & Performance

### 4.1 L1 Cache Quantization Interval (🟡 Soft Deprecated)

**Status:** Soft deprecated - parameter ignored
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Reason:** Broken implementation (Issue #909)
**Replacement:** Use `l1_cache_revision_window`

```python
# ❌ DEPRECATED (BROKEN):
rebac_manager = EnhancedReBACManager(
    db_session=session,
    l1_cache_quantization_interval=100  # ⚠️  Was broken, now ignored
)

# ✅ REPLACEMENT:
rebac_manager = EnhancedReBACManager(
    db_session=session,
    l1_cache_revision_window=100  # Correctly implemented
)
```

**Why Deprecated:**
- Original implementation was broken (Issue #909)
- Revision window approach is more reliable
- Better cache invalidation semantics

**Warning Message:**
```
DeprecationWarning: l1_cache_quantization_interval is deprecated and was broken (Issue #909).
Use l1_cache_revision_window instead. This parameter is ignored.
```

**Files Affected:**
- [nexus/core/rebac_manager.py:69-101](nexus/src/nexus/core/rebac_manager.py#L69-L101)
- [nexus/core/rebac_cache.py:60-98](nexus/src/nexus/core/rebac_cache.py#L60-L98)

---

### 4.2 Cache Getter Methods (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows message
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use new method names

```python
# ❌ DEPRECATED:
cache_obj = backend.get_cache()
stats = backend.get_cache_stats()
backend.clear_cache()

# ✅ REPLACEMENT:
cache_obj = backend._get_l1_cache()
stats = backend.get_l1_cache_stats()
backend.clear_l1_cache()
```

**Why Deprecated:**
- More explicit naming (L1 vs L2 cache)
- Avoids confusion with content cache

**Files Affected:**
- [nexus/backends/cache_mixin.py:210-220](nexus/src/nexus/backends/cache_mixin.py#L210-L220)

---

### 4.3 sync() Method (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `sync_content_to_cache()`

```python
# ❌ DEPRECATED:
backend.sync(path="/file.txt")

# ✅ REPLACEMENT:
backend.sync_content_to_cache(path="/file.txt")
```

**Why Deprecated:**
- More explicit method name
- Clarifies what is being synced (content to cache)

**Warning Message:**
```
DeprecationWarning: CacheConnectorMixin.sync() is deprecated.
Use sync_content_to_cache() instead.
```

**Files Affected:**
- [nexus/backends/cache_mixin.py:1527-1535](nexus/src/nexus/backends/cache_mixin.py#L1527-L1535)

---

## 5. Storage & Database

### 5.1 content_binary Database Column (🟡 Soft Deprecated)

**Status:** Soft deprecated - column kept for migration
**Deprecated In:** v0.5.0
**Removal Planned:** v0.8.0 (after migration period)
**Replacement:** Disk-based content storage via `FileContentCache`

```python
# ❌ OLD: Binary content in PostgreSQL
ContentCache(
    path_id="...",
    content_binary=b"large binary data..."  # ⚠️  Deprecated, bloats DB
)

# ✅ NEW: Binary content on disk
# Content automatically stored on disk via FileContentCache
# Database stores only metadata (hash, size, synced_at)
```

**Why Deprecated:**
- Storing large binary content in PostgreSQL is slow
- Disk storage enables mmap for fast reads
- Enables Zoekt trigram indexing for sub-50ms code search
- Better scalability

**Migration Notes:**
- Column still exists for backward compatibility
- New writes go to disk only
- Old data will be migrated in background job
- Column will be dropped in v0.8.0

**Files Affected:**
- [nexus/storage/models.py:2980-3014](nexus/src/nexus/storage/models.py#L2980-L3014)

**Related Docs:**
- See `docs/design/cache-layer.md` for architecture

---

### 5.2 db_session Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - comment indicates deprecation
**Deprecated In:** v0.5.0
**Removal Planned:** v0.7.0
**Replacement:** Use `session_factory` parameter

```python
# ❌ DEPRECATED:
s3_backend = S3Backend(
    bucket="my-bucket",
    db_session=session  # ⚠️  Single session, not thread-safe
)

# ✅ REPLACEMENT:
s3_backend = S3Backend(
    bucket="my-bucket",
    session_factory=lambda: Session(bind=engine)  # Factory pattern
)
```

**Why Deprecated:**
- Factory pattern is more flexible
- Supports connection pooling
- Thread-safe session management
- Better for async/concurrent operations

**Files Affected:**
- [nexus/backends/s3_connector.py:139](nexus/src/nexus/backends/s3_connector.py#L139)
- [nexus/backends/gcs_connector.py:137](nexus/src/nexus/backends/gcs_connector.py#L137)

---

### 5.3 db_path Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - comment indicates deprecation
**Deprecated In:** v0.5.0
**Replacement:** Use `db_url` parameter

```python
# ❌ DEPRECATED:
metadata_store = MetadataStore(db_path="/path/to/db.sqlite")

# ✅ REPLACEMENT:
metadata_store = MetadataStore(db_url="sqlite:////path/to/db.sqlite")
# or
metadata_store = MetadataStore(db_url="postgresql://user:pass@host/db")
```

**Why Deprecated:**
- `db_url` supports multiple database backends (SQLite, PostgreSQL, etc.)
- More flexible and standard (SQLAlchemy format)

**Files Affected:**
- [nexus/storage/metadata_store.py:80](nexus/src/nexus/storage/metadata_store.py#L80)

---

## 6. Authentication

### 6.1 Static API Key Authentication (🟡 Soft Deprecated)

**Status:** Soft deprecated - shows warning in CLI
**Deprecated In:** v0.5.0
**Removal Planned:** v0.8.0
**Replacement:** Database-based authentication

```bash
# ❌ DEPRECATED:
nexus server start --api-key "static-key-12345"

# ✅ REPLACEMENT:
# Use database authentication with API key management
nexus server start  # Reads from database
nexus api-key create --name "My App" --expires "2024-12-31"
```

**Why Deprecated:**
- Static keys cannot be rotated without restart
- No per-key permissions or audit trail
- Database auth supports key rotation, expiry, and scopes
- Better security posture

**Warning Message:**
```
⚠️  Static API key authentication (deprecated)
Use database authentication for production deployments.
```

**Files Affected:**
- [nexus/cli/commands/server.py:840-859](nexus/src/nexus/cli/commands/server.py#L840-L859)

---

## 7. Configuration

### 7.1 BatchHttpRequest() Constructor (🟡 Soft Deprecated)

**Status:** Soft deprecated - upstream deprecation
**Deprecated In:** Google API Client Library
**Replacement:** Use `service.new_batch_http_request()`

```python
# ❌ DEPRECATED:
from googleapiclient.http import BatchHttpRequest
batch = BatchHttpRequest()

# ✅ REPLACEMENT:
batch = service.new_batch_http_request()
```

**Why Deprecated:**
- Google deprecated the direct constructor
- New method is preferred by upstream library

**Files Affected:**
- [nexus/backends/gmail_connector_utils.py:417](nexus/src/nexus/backends/gmail_connector_utils.py#L417)

---

### 7.2 _metadata Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated
**Deprecated In:** v0.5.0
**Replacement:** Use structured content dict

```python
# ❌ DEPRECATED:
nx.memory_create(
    path="/note.txt",
    content="My note",
    _metadata={"tags": ["important"]}  # Unstructured metadata
)

# ✅ REPLACEMENT:
nx.memory_create(
    path="/note.txt",
    content={
        "text": "My note",
        "tags": ["important"],
        "priority": "high"
    }
)
```

**Why Deprecated:**
- Structured content dict is more flexible
- Better type safety
- Aligns with JSON-based storage

**Files Affected:**
- [nexus/core/memory_api.py:106](nexus/src/nexus/core/memory_api.py#L106)

---

### 7.3 search_mode Parameter (🟡 Soft Deprecated)

**Status:** Soft deprecated - ignored
**Deprecated In:** v0.5.0
**Replacement:** Automatically determined from query

```python
# ❌ DEPRECATED:
results = nx.search(query="test", search_mode="hybrid")

# ✅ REPLACEMENT:
results = nx.search(query="test")  # Mode auto-detected
```

**Why Deprecated:**
- Search mode is now automatically determined
- Simplifies API

**Files Affected:**
- [nexus/core/nexus_fs_search.py:1114](nexus/src/nexus/core/nexus_fs_search.py#L1114)

---

## Removal Timeline

### v0.6.0 (Current Development)
- None (all deprecations active)

### v0.7.0 (Planned: Q2 2026)
**Breaking Changes:**
- 🔴 Remove `acl_store` parameter
- 🔴 Remove `zone_id`/`agent_id` in NexusFS.__init__()
- 🔴 Remove `agent_id` parameter in workspace methods
- 🔴 Remove `custom_parsers` parameter
- 🔴 Remove `overwrite`/`skip_existing` CLI flags
- 🔴 Remove `keyword_weight`/`semantic_weight` parameters
- 🔴 Remove `l1_cache_quantization_interval`
- 🔴 Remove old cache getter methods
- 🔴 Remove `sync()` method
- 🔴 Remove `db_session` parameter

### v0.8.0 (Planned: Q4 2026)
**Breaking Changes:**
- 🔴 Remove `content_binary` column from database
- 🔴 Remove static API key authentication
- 🔴 Remove `db_path` parameter

---

## Migration Strategies

### Strategy 1: Gradual Migration (Recommended)

For production codebases, migrate gradually:

1. **Phase 1:** Update code to use new APIs while old APIs still work
2. **Phase 2:** Test thoroughly in staging environment
3. **Phase 3:** Deploy to production
4. **Phase 4:** Remove old API usage after monitoring

### Strategy 2: Automated Migration

Use provided migration scripts:

```bash
# Migrate UNIX permissions to ReBAC
python scripts/migrate/unix_to_rebac.py --dry-run
python scripts/migrate/unix_to_rebac.py --apply

# Migrate ACL to ReBAC
python scripts/migrate/acl_to_rebac.py --dry-run
python scripts/migrate/acl_to_rebac.py --apply

# Update deprecated parameters
python scripts/migrate/update_parameters.py --check
python scripts/migrate/update_parameters.py --fix
```

### Strategy 3: IDE Search & Replace

Use these regex patterns for automated refactoring:

```regex
# zone_id/agent_id in __init__
NexusFS\((.*?)zone_id=(.*?)(,|\))
→ NexusFS($1)  # Remove zone_id, pass via context

# agent_id → workspace_path
agent_id=["']([^"']+)["']
→ workspace_path=f"/workspaces/$1"

# keyword_weight/semantic_weight → alpha
keyword_weight=[\d.]+,\s*semantic_weight=([\d.]+)
→ alpha=$1
```

---

## Deprecation Warning Suppression

### Temporary Suppression (Not Recommended)

If you need to suppress deprecation warnings temporarily:

```python
import warnings

# Suppress all deprecation warnings (NOT RECOMMENDED)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Suppress specific warning (better)
warnings.filterwarnings(
    "ignore",
    message="zone_id and agent_id parameters.*are DEPRECATED",
    category=DeprecationWarning
)
```

**Warning:** Suppressing deprecation warnings hides technical debt and may cause breakage in future versions.

---

## Getting Help

If you encounter issues during migration:

1. **Documentation:** See [docs/migration/](docs/migration/)
2. **Examples:** Check [examples/migration/](examples/migration/)
3. **Issues:** Report at https://github.com/nexi-intra/nexus-system/issues
4. **Discord:** Ask in #nexus-help channel

---

## Version Support Matrix

| Feature | v0.5.x | v0.6.x | v0.7.x | v0.8.x+ |
|---------|--------|--------|--------|---------|
| UNIX permissions (chmod/chown/chgrp) | ❌ | ❌ | ❌ | ❌ |
| ACL operations (grant/deny/revoke) | ❌ | ❌ | ❌ | ❌ |
| acl_store parameter | ⚠️ | ⚠️ | ❌ | ❌ |
| zone_id/agent_id in __init__ | ⚠️ | ⚠️ | ❌ | ❌ |
| agent_id parameter | ⚠️ | ⚠️ | ❌ | ❌ |
| custom_parsers | ⚠️ | ⚠️ | ❌ | ❌ |
| keyword_weight/semantic_weight | ⚠️ | ⚠️ | ❌ | ❌ |
| content_binary column | ⚠️ | ⚠️ | ⚠️ | ❌ |
| Static API keys | ⚠️ | ⚠️ | ⚠️ | ❌ |

Legend: ✅ Supported | ⚠️  Deprecated | ❌ Removed

---

**Document Maintained By:** Nexus Core Team
**Last Audit:** 2026-01-02 (Phase 1, Task 1.4)
**Next Review:** Before v0.7.0 release
