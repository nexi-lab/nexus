# Proposed Issues: SeaweedFS-Inspired Improvements

Based on deep research of SeaweedFS architecture. Create these issues in GitHub.

---

## Issue 1: Volume-Based Storage Backend (P1)

**Title:** `feat: Implement volume-based storage backend inspired by SeaweedFS needle storage`

**Labels:** `enhancement`, `performance`, `storage`

**Body:**
```markdown
## Summary
Implement a volume-based storage backend that stores multiple files in large volume files (32GB each), inspired by SeaweedFS's needle-in-haystack design.

## Motivation
From SeaweedFS research:
- Current approach: Each file = 1 filesystem inode (536 bytes overhead on XFS)
- SeaweedFS: 40 bytes per file, 16 bytes in memory
- **13x more efficient metadata storage**
- O(1) disk reads (all metadata in memory)

## Proposed Design

```python
class VolumeBasedBackend(Backend):
    VOLUME_SIZE = 32 * 1024 * 1024 * 1024  # 32GB

    # Needle format: [cookie|key|size|data|checksum]
    # Index: hash -> (volume_id, offset, size) - 24 bytes in memory
```

### Key Features
- [ ] Volume file management (create, compact, seal)
- [ ] In-memory index with LevelDB persistence option
- [ ] Background compaction for deleted files
- [ ] Compatible with existing CAS deduplication

## Cross-Tenant Considerations
**Important:** For cross-tenant content sharing:
- Content stored by hash (CAS) - same content = same hash regardless of tenant
- Volume index is content-hash based, not tenant-scoped
- Tenant isolation enforced at metadata layer, not storage layer
- Shared content (deduplication) works across tenants automatically

## References
- SeaweedFS research: docs/research/seaweedfs-learnings.md
- Facebook Haystack paper
- Related: #870 (database partitioning)
```

---

## Issue 2: Memory-Efficient Path Index with LevelDB (P0)

**Title:** `perf: Add LevelDB-based path index for memory-efficient file lookups`

**Labels:** `enhancement`, `performance`, `database`

**Body:**
```markdown
## Summary
Add optional LevelDB/RocksDB-based index for path→content_hash lookups to reduce memory footprint at scale.

## Motivation
From SeaweedFS research:
- SeaweedFS LevelDB mode: 4MB total footprint for millions of files
- Current Nexus: Full SQLAlchemy queries for every path lookup
- At 100M files, in-memory metadata becomes prohibitive

## Proposed Design

```python
class HybridPathIndex:
    def __init__(self, mode: str = "leveldb"):
        if mode == "memory":
            self.index = {}  # Fast, high memory
        elif mode == "leveldb":
            self.index = plyvel.DB(path)  # 4MB footprint
        elif mode == "rocksdb":
            self.index = rocksdb.DB(path)  # Better for writes
```

### Configuration
```yaml
storage:
  path_index:
    mode: leveldb  # memory | leveldb | rocksdb
    cache_size_mb: 64
```

## Cross-Tenant Considerations
Index can be:
1. **Shared index** (default): All tenants in one index, prefix paths with tenant_id
   - Pro: Simpler, better for cross-tenant file sharing
   - Con: No isolation at storage level

2. **Per-tenant index**: Separate LevelDB per tenant
   - Pro: Full isolation, easier tenant deletion
   - Con: More file handles, no cross-tenant dedup benefits

Recommend: **Shared index** with tenant-prefixed keys for cross-tenant sharing support.

## References
- SeaweedFS research: docs/research/seaweedfs-learnings.md
- Related: #870 (database partitioning)
```

---

## Issue 3: Tiered Storage Architecture (P1)

**Title:** `feat: Implement tiered storage with hot/warm/cold data movement`

**Labels:** `enhancement`, `performance`, `storage`

**Body:**
```markdown
## Summary
Implement automatic tiered storage that moves data between hot (SSD), warm (HDD), and cold (cloud) tiers based on access patterns.

## Motivation
From SeaweedFS research:
- Transparent O(1) access across all tiers
- 80% cost savings with 20/80 hot/warm split
- Automatic promotion/demotion based on access frequency

## Proposed Design

```python
class TieredStorageBackend(Backend):
    tiers = {
        "hot": LocalSSDBackend(max_size="100GB"),
        "warm": LocalHDDBackend(max_size="1TB"),
        "cold": S3Backend(bucket="nexus-archive")
    }

    async def read_content(self, hash: str) -> bytes:
        tier = self.get_tier(hash)
        content = await self.tiers[tier].read(hash)

        # Promote if frequently accessed
        if self.should_promote(hash):
            await self.promote(hash, content)

        return content
```

### Features
- [ ] Access frequency tracking
- [ ] Background tier migration job
- [ ] Configurable promotion/demotion thresholds
- [ ] Cloud backend integration (S3, GCS)

## Cross-Tenant Considerations
Tiering should be **content-based, not tenant-based**:
- Same content hash = same tier (supports cross-tenant sharing)
- Access frequency aggregated across all tenants accessing the content
- Hot content stays hot regardless of which tenant accesses it

Alternative: Per-tenant tiering policies (premium tenants get more hot storage)

## References
- SeaweedFS research: docs/research/seaweedfs-learnings.md
- Related: #853 (streaming), #788 (chunked uploads)
```

---

## Issue 4: Cross-Tenant Content Sharing Architecture (P0)

**Title:** `feat: Design cross-tenant content sharing with CAS deduplication`

**Labels:** `enhancement`, `architecture`, `multi-tenancy`

**Body:**
```markdown
## Summary
Design and document the architecture for cross-tenant content sharing while maintaining security isolation.

## Current State
- CAS (Content-Addressable Storage) already deduplicates by SHA-256 hash
- Same file uploaded by different tenants = stored once
- **Problem:** No explicit sharing mechanism between tenants

## Proposed Architecture

### Layer 1: Storage Layer (Content-Based)
```
┌─────────────────────────────────────────┐
│           Content Store (CAS)           │
│  hash → content (tenant-agnostic)       │
│  Deduplication automatic across tenants │
└─────────────────────────────────────────┘
```

### Layer 2: Metadata Layer (Tenant-Scoped)
```
┌─────────────────────────────────────────┐
│         Metadata Store (Partitioned)    │
│  (tenant_id, path) → content_hash       │
│  Partitioned by tenant_id               │
└─────────────────────────────────────────┘
```

### Layer 3: Permission Layer (ReBAC)
```
┌─────────────────────────────────────────┐
│              ReBAC Tuples               │
│  Cross-tenant sharing via explicit      │
│  permission grants                      │
└─────────────────────────────────────────┘
```

## Cross-Tenant Sharing Mechanisms

### Option A: Symbolic Links (Recommended)
```python
# Tenant B shares file with Tenant A
nexus.create_symlink(
    source="/tenant:B/shared/report.pdf",
    target="/tenant:A/external/partner-report.pdf",
    permissions=["viewer"]
)
```
- Creates path entry in Tenant A pointing to Tenant B's content
- ReBAC tuple grants access
- No content duplication

### Option B: Content Sharing via Hash
```python
# Get shareable content reference
share_ref = nexus.create_share(
    path="/tenant:B/files/data.csv",
    expires_at="2025-12-31",
    allowed_tenants=["tenant:A", "tenant:C"]
)

# Other tenant imports by reference
nexus.import_shared(
    share_ref=share_ref,
    target_path="/tenant:A/imports/data.csv"
)
```
- Creates new metadata entry pointing to same content hash
- Original tenant retains ownership
- Reference counting for garbage collection

### Option C: Shared Namespace
```python
# Create shared namespace accessible by multiple tenants
nexus.create_namespace(
    path="/shared/consortium",
    members=["tenant:A", "tenant:B", "tenant:C"],
    default_permission="viewer"
)
```
- Special namespace type with multi-tenant access
- Single metadata partition
- Collaborative access model

## Implementation Considerations

### Reference Counting
```sql
-- Track content references across tenants
CREATE TABLE content_refs (
    content_hash TEXT PRIMARY KEY,
    ref_count INTEGER DEFAULT 1,
    tenant_refs JSONB  -- {"tenant:A": 2, "tenant:B": 1}
);
```
- Content deleted only when ref_count = 0
- Per-tenant ref tracking for billing/quotas

### Quota & Billing
- Shared content: Split quota across referencing tenants?
- Or: First uploader owns quota, others get free reference?
- Configurable per deployment

### Database Partitioning Impact
With hash partitioning by tenant_id:
- Cross-tenant queries need to hit multiple partitions
- Solution: Shared content index table (not partitioned)
```sql
CREATE TABLE shared_content (
    content_hash TEXT,
    owner_tenant_id UUID,
    shared_with UUID[],
    created_at TIMESTAMP
);
-- Not partitioned, relatively small
```

## Security Considerations
- [ ] Explicit opt-in for sharing (no implicit cross-tenant access)
- [ ] Audit logging for cross-tenant access
- [ ] Revocation propagates immediately
- [ ] Content encryption keys: per-tenant or per-content?

## References
- SeaweedFS research: docs/research/seaweedfs-learnings.md
- Related: #870 (partitioning), #820 (provisioning), #823 (permissions)
```

---

## Issue 5: Chunked Upload/Download with Streaming (P1)

**Title:** `feat: Implement chunked file uploads with resumable transfers`

**Labels:** `enhancement`, `api`, `storage`

**Body:**
```markdown
## Summary
Implement chunked upload/download with resumable transfers and HTTP Range support.

## Motivation
From SeaweedFS research:
- 8MB default chunk size
- Manifest-based large file tracking
- Up to 8TB files with hierarchical manifests

## Proposed Design

### Upload API
```python
# Initiate chunked upload
POST /api/nfs/upload/init
{
    "path": "/files/large-video.mp4",
    "total_size": 5368709120,  # 5GB
    "chunk_size": 8388608       # 8MB
}
Response: {"upload_id": "abc123", "chunk_count": 640}

# Upload chunk
PUT /api/nfs/upload/{upload_id}/chunk/{chunk_number}
Content-Type: application/octet-stream
<binary data>

# Complete upload
POST /api/nfs/upload/{upload_id}/complete
```

### Download API (Range Support)
```python
GET /api/nfs/stream/{path}
Range: bytes=0-8388607

Response:
Content-Range: bytes 0-8388607/5368709120
<chunk data>
```

### Manifest Storage
```python
class FileManifest:
    path: str
    total_size: int
    chunks: List[ChunkInfo]  # [{hash, offset, size}, ...]
```

## Cross-Tenant Considerations
- Manifests are tenant-scoped (metadata layer)
- Chunks stored by hash (content layer) - automatic cross-tenant dedup
- If two tenants upload same large file:
  - Different manifests (different paths)
  - Same chunks (deduplication at storage layer)
  - 50% storage savings for duplicated large files

## References
- Related: #853 (streaming endpoint), #788 (chunked uploads), #790 (range requests)
```

---

## Issue 6: Update Database Partitioning for Cross-Tenant Sharing

**Title:** `Update #870: Add cross-tenant sharing considerations to partitioning design`

**Labels:** `enhancement`, `database`, `architecture`

**Body:**
```markdown
## Summary
Update the database partitioning strategy (#870) to support efficient cross-tenant content sharing.

## Problem
With pure hash partitioning by tenant_id:
- Cross-tenant queries require scanning all partitions
- Shared content lookups are O(N) where N = partition count

## Proposed Solution

### Hybrid Partitioning Strategy

```sql
-- Main files table: Partitioned by tenant
CREATE TABLE files (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    ...
) PARTITION BY HASH (tenant_id);

-- Shared content index: NOT partitioned
CREATE TABLE shared_content_index (
    content_hash TEXT PRIMARY KEY,
    owner_tenant_id UUID NOT NULL,
    shared_with_tenants UUID[] DEFAULT '{}',
    share_type TEXT,  -- 'public' | 'explicit' | 'consortium'
    created_at TIMESTAMP
);

-- Fast lookup for cross-tenant access
CREATE INDEX idx_shared_tenants ON shared_content_index
USING GIN (shared_with_tenants);
```

### Query Patterns

```sql
-- Check if content is shared with requesting tenant
SELECT EXISTS (
    SELECT 1 FROM shared_content_index
    WHERE content_hash = $1
    AND (
        owner_tenant_id = $2
        OR $2 = ANY(shared_with_tenants)
        OR share_type = 'public'
    )
);
```

### Migration Path
1. Deploy shared_content_index table
2. Backfill for any existing shared content
3. Update write path to populate index on share operations
4. Update read path to check index for cross-tenant access

## References
- Original: #870 (database partitioning)
- SeaweedFS research: docs/research/seaweedfs-learnings.md
```

---

## Summary Table

| Issue | Title | Priority | Cross-Tenant Impact |
|-------|-------|----------|---------------------|
| 1 | Volume-Based Storage | P1 | Content-based, supports sharing |
| 2 | LevelDB Path Index | P0 | Shared index with tenant prefix |
| 3 | Tiered Storage | P1 | Content-based tiering |
| **4** | **Cross-Tenant Sharing** | **P0** | **Core architecture** |
| 5 | Chunked Uploads | P1 | Chunk dedup across tenants |
| 6 | Partitioning Update | P0 | Hybrid strategy for sharing |

---

## Cross-Tenant Sharing: Recommended Approach

Based on Nexus's CAS architecture and SeaweedFS patterns:

```
┌────────────────────────────────────────────────────────────┐
│                    Cross-Tenant Sharing                    │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Content Layer (CAS)         │  Already shared by hash    │
│  ─────────────────────────   │  No changes needed         │
│                              │                            │
│  Metadata Layer              │  Add shared_content_index  │
│  ─────────────────────────   │  Hybrid partitioning       │
│                              │                            │
│  Permission Layer (ReBAC)    │  Cross-tenant tuples       │
│  ─────────────────────────   │  (tenant:A, viewer,        │
│                              │   file:/tenant:B/doc.pdf)  │
│                              │                            │
│  API Layer                   │  Share/import endpoints    │
│  ─────────────────────────   │  Symlink support           │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

**Key Insight:** Nexus's CAS already enables storage-level sharing. The gaps are:
1. Explicit sharing API (create_share, import_shared)
2. Cross-tenant permission grants in ReBAC
3. Metadata index for efficient cross-tenant lookups
4. Reference counting for garbage collection
