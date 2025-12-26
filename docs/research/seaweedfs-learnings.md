# SeaweedFS Research: Lessons for Nexus

**Research Date:** December 26, 2025
**Branch:** `claude/research-seaweedfs-0rFsy`

---

## Executive Summary

This document synthesizes deep research on SeaweedFS architecture and maps applicable innovations to Nexus. SeaweedFS is a distributed object storage system inspired by Facebook's Haystack paper, optimized for billions of small files with O(1) disk operations. Several architectural patterns and performance optimizations from SeaweedFS can significantly enhance Nexus's scalability, particularly as we approach the 100M+ file scale identified in GitHub issue #870.

### Key Findings

| SeaweedFS Innovation | Nexus Applicability | Priority | Issue Reference |
|---------------------|---------------------|----------|-----------------|
| Needle/Haystack storage model | High - 100M+ file scale | P0 | #870 |
| Pluggable metadata backends | Already implemented ✓ | - | - |
| Tiered storage architecture | High - cloud integration | P1 | #853, #788 |
| O(1) disk operations | Medium - backend optimization | P1 | Performance |
| Erasure coding | Low - replication needs | P3 | Future |
| Active-active replication | Medium - multi-region | P2 | Future |

---

## 1. Architectural Comparison

### 1.1 Current Nexus Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      NexusFS (Core)                         │
├─────────────────────────────────────────────────────────────┤
│  Mixins: Core | Search | ReBAC | Versions | Mounts | ...   │
├─────────────────────────────────────────────────────────────┤
│                   Storage Layer                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ LocalBackend│  │ GCSBackend  │  │ S3Connector │  ...    │
│  │   (CAS)     │  │   (CAS)     │  │  (Direct)   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
├─────────────────────────────────────────────────────────────┤
│              Metadata Store (SQLAlchemy)                    │
│        SQLite (embedded) | PostgreSQL (production)         │
└─────────────────────────────────────────────────────────────┘
```

**Strengths:**
- Content-addressable storage (CAS) with SHA-256 deduplication
- Multi-level caching (content, metadata, permissions, Tiger Cache)
- Flexible backend mounting with namespace routing
- Rich permission system (ReBAC/Zanzibar-inspired)

**Current Challenges (from GitHub issues):**
- Database partitioning needed for 100M+ files (#870)
- Connection pooling at scale (#860)
- Large file streaming (#853, #788)
- Media handling optimization (#789-794)

### 1.2 SeaweedFS Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Master Server(s)                         │
│         (Volume metadata, File ID allocation)               │
│              ~512 bytes per volume (not per file)           │
├─────────────────────────────────────────────────────────────┤
│    ┌─────────────────┐    ┌─────────────────┐              │
│    │ Volume Server 1 │    │ Volume Server N │              │
│    │  ┌───────────┐  │    │  ┌───────────┐  │              │
│    │  │ Vol 1(32GB)│ │    │  │ Vol M(32GB)│ │              │
│    │  │ Needles...│  │    │  │ Needles...│  │              │
│    │  └───────────┘  │    │  └───────────┘  │              │
│    │  16B/file mem   │    │  16B/file mem   │              │
│    └─────────────────┘    └─────────────────┘              │
├─────────────────────────────────────────────────────────────┤
│                   Filer Server(s)                           │
│    (Stateless, POSIX-like interface, pluggable metadata)   │
│      MySQL | PostgreSQL | Redis | Cassandra | etc.         │
└─────────────────────────────────────────────────────────────┘
```

**Key Innovations:**
- **Needle Storage**: Multiple files in single 32GB volume files
- **Minimal Metadata**: 40 bytes/file on disk, 16 bytes in memory
- **O(1) Disk Reads**: All metadata in memory, single disk operation per read
- **Decentralized Metadata**: Each volume server manages its own file index

---

## 2. Applicable Learnings

### 2.1 [P0] Database Partitioning Strategy (Issue #870)

**SeaweedFS Approach:**
- Master only tracks ~10,000 volumes (not billions of files)
- Each volume server independently manages its file metadata
- Total system-wide metadata overhead: ~512 bytes/volume

**Recommendation for Nexus:**
```sql
-- PostgreSQL table partitioning by tenant (from issue #870)
CREATE TABLE files (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    created_at TIMESTAMP
) PARTITION BY HASH (tenant_id);

-- Create partitions (e.g., 64 partitions)
CREATE TABLE files_p0 PARTITION OF files FOR VALUES WITH (modulus 64, remainder 0);
-- ... repeat for 1-63
```

**Additional SeaweedFS-inspired improvement:**
```python
# Hybrid metadata strategy
class HybridMetadataStore:
    """
    Inspired by SeaweedFS filer architecture:
    - Hot metadata: In-memory LRU cache
    - Warm metadata: Redis/Memcached
    - Cold metadata: PostgreSQL with partitioning
    """

    def __init__(self):
        self.l1_cache = LRUCache(maxsize=100_000)  # ~2.4MB
        self.l2_cache = RedisClient()  # Shared across instances
        self.l3_store = PostgresPartitioned()

    async def get_metadata(self, path: str) -> FileMetadata:
        # L1: In-memory (~100μs)
        if result := self.l1_cache.get(path):
            return result

        # L2: Redis (~1ms)
        if result := await self.l2_cache.get(path):
            self.l1_cache.set(path, result)
            return result

        # L3: PostgreSQL (~5-10ms)
        result = await self.l3_store.query(path)
        await self.l2_cache.set(path, result, ttl=300)
        self.l1_cache.set(path, result)
        return result
```

### 2.2 [P0] Volume-Based Storage for Large Scale

**SeaweedFS Innovation:**
- Files stored as "needles" in 32GB "volumes"
- Only 40 bytes disk overhead per file (vs 536 bytes for XFS inodes)
- 13x more space efficient for metadata

**Potential Nexus Enhancement:**
```python
# Volume-based content storage for LocalBackend
class VolumeBasedBackend(Backend):
    """
    Store multiple files in volume files to reduce filesystem overhead.
    Inspired by SeaweedFS needle-in-haystack design.
    """

    VOLUME_SIZE = 32 * 1024 * 1024 * 1024  # 32GB

    def __init__(self, base_path: str):
        self.base_path = base_path
        self.current_volume: VolumeFile = None
        self.index: Dict[str, NeedleLocation] = {}  # hash -> (volume_id, offset, size)

    async def write_content(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()

        if content_hash in self.index:
            return content_hash  # Deduplication

        # Append to current volume
        volume = await self._get_writable_volume()
        offset = await volume.append(content_hash, content)

        self.index[content_hash] = NeedleLocation(
            volume_id=volume.id,
            offset=offset,
            size=len(content)
        )

        return content_hash

    async def read_content(self, content_hash: str) -> bytes:
        location = self.index.get(content_hash)
        if not location:
            raise FileNotFoundError(content_hash)

        # O(1) disk read - single seek
        volume = self._get_volume(location.volume_id)
        return await volume.read_at(location.offset, location.size)
```

**Benefits:**
- Reduces inode pressure on filesystem
- Better sequential write performance (append-only)
- Easier backup (copy volume files vs millions of small files)
- Compatible with existing CAS deduplication

### 2.3 [P1] Tiered Storage Architecture (Issues #853, #788)

**SeaweedFS Cloud Tiering:**
```
NVME (Hot) → SSD (Warm) → HDD (Cool) → Cloud (Cold)
         ↘     ↓      ↙           ↙
           Transparent O(1) access
```

**Recommendation for Nexus:**
```python
class TieredStorageBackend(Backend):
    """
    Inspired by SeaweedFS cloud tiering with O(1) access.

    - Hot tier: Local SSD for recent/frequently accessed
    - Warm tier: Local HDD for older content
    - Cold tier: Cloud (S3/GCS) for archival
    """

    def __init__(self):
        self.hot = LocalSSDBackend(max_size="100GB")
        self.warm = LocalHDDBackend(max_size="1TB")
        self.cold = S3Backend(bucket="nexus-cold-storage")

        # Metadata tracks content location
        self.tier_index: Dict[str, StorageTier] = {}
        self.access_tracker = AccessTracker()

    async def read_content(self, content_hash: str) -> bytes:
        tier = self.tier_index.get(content_hash, StorageTier.COLD)

        self.access_tracker.record(content_hash)

        if tier == StorageTier.HOT:
            return await self.hot.read(content_hash)
        elif tier == StorageTier.WARM:
            content = await self.warm.read(content_hash)
            # Promote to hot if frequently accessed
            if self.access_tracker.is_hot(content_hash):
                await self._promote(content_hash, content, StorageTier.HOT)
            return content
        else:
            # Fetch from cloud, cache locally
            content = await self.cold.read(content_hash)
            await self._cache_locally(content_hash, content)
            return content

    async def background_tiering(self):
        """Periodic job to demote cold content"""
        for content_hash, last_access in self.access_tracker.get_cold_content():
            await self._demote(content_hash)
```

### 2.4 [P1] Memory-Efficient Indexing

**SeaweedFS Memory Model:**
- 16 bytes per file in memory (volume server)
- LevelDB option: Only 4MB footprint for millions of files
- Fast startup with LevelDB vs full memory load

**Current Nexus Caching (from code analysis):**
```python
# Existing caches
- ContentCache: LRU, 256MB
- MetadataCache: 512 entries, 300s TTL
- DirectoryCache: 1024 entries
- ReBACCache: 50,000 entries, L1/L2
- TigerCache: Roaring Bitmaps
```

**Enhancement Recommendation:**
```python
class MemoryEfficientIndex:
    """
    Inspired by SeaweedFS memory-mapped index with LevelDB fallback.

    Current Nexus: Full metadata in SQLAlchemy
    Proposed: Hybrid memory/disk index for path→hash lookups
    """

    def __init__(self, index_path: str, mode: str = "leveldb"):
        if mode == "memory":
            # Fast access, slower startup, higher memory
            self.index = {}  # Full in-memory
        elif mode == "leveldb":
            # Fast startup, 4MB footprint
            self.index = plyvel.DB(index_path)
        elif mode == "rocksdb":
            # Better for high write volumes
            self.index = rocksdb.DB(index_path)

    def get(self, path: str) -> Optional[str]:
        """O(1) path to content_hash lookup"""
        if isinstance(self.index, dict):
            return self.index.get(path)
        return self.index.get(path.encode())

    def set(self, path: str, content_hash: str):
        """Append-only write"""
        if isinstance(self.index, dict):
            self.index[path] = content_hash
        else:
            self.index.put(path.encode(), content_hash.encode())
```

### 2.5 [P1] Streaming and Chunking (Issues #853, #788)

**SeaweedFS Large File Handling:**
```
Small files (<chunk_size): Single needle
Medium files (8-80GB): Chunked with manifest
Super large files (up to 8TB): Hierarchical manifests
  - 1000 chunks per manifest
  - 1000 manifests = 8TB max
  - Non-recursive for predictable access time
```

**Recommendation for Nexus:**
```python
class ChunkedFileManager:
    """
    Inspired by SeaweedFS chunking with manifest storage.

    Addresses issues #853 (streaming) and #788 (chunked uploads).
    """

    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB (SeaweedFS default)
    MAX_MANIFEST_CHUNKS = 1000

    async def write_large_file(
        self,
        path: str,
        stream: AsyncIterator[bytes]
    ) -> FileManifest:
        """Stream-write large files with resumable chunks"""

        manifest = FileManifest(path=path, chunks=[])

        async for chunk in self._chunk_stream(stream, self.CHUNK_SIZE):
            chunk_hash = await self.backend.write_content(chunk)
            manifest.chunks.append(ChunkInfo(
                hash=chunk_hash,
                size=len(chunk),
                offset=manifest.total_size
            ))
            manifest.total_size += len(chunk)

        # Store manifest (not in volume, in metadata store)
        await self.metadata_store.save_manifest(path, manifest)
        return manifest

    async def read_large_file(
        self,
        path: str,
        range_start: int = None,
        range_end: int = None
    ) -> AsyncIterator[bytes]:
        """Stream-read with HTTP Range support (issue #790)"""

        manifest = await self.metadata_store.get_manifest(path)

        for chunk_info in manifest.get_chunks_in_range(range_start, range_end):
            content = await self.backend.read_content(chunk_info.hash)

            # Handle partial chunk reads for range requests
            if range_start or range_end:
                content = self._slice_chunk(content, chunk_info, range_start, range_end)

            yield content
```

### 2.6 [P2] Connection Pooling (Issue #860)

**SeaweedFS Approach:**
- Stateless filer servers with external metadata stores
- Connection pooling at application level
- PgBouncer/HAProxy for database connections

**Recommendation:**
```python
# Enhanced connection pooling for Nexus
from sqlalchemy.pool import QueuePool

class OptimizedConnectionPool:
    """
    Inspired by SeaweedFS production setup with PgBouncer.

    Addresses issue #860 for centralized connection pooling.
    """

    @staticmethod
    def create_engine(database_url: str, pool_size: int = 20):
        return create_async_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=10,
            pool_pre_ping=True,  # Verify connections before use
            pool_recycle=3600,   # Recycle connections hourly
            connect_args={
                "server_settings": {
                    "statement_timeout": "30000",  # 30s query timeout
                    "idle_in_transaction_session_timeout": "60000"
                }
            }
        )
```

### 2.7 [P2] Erasure Coding for Cost Efficiency

**SeaweedFS EC:**
- RS(10,4): 10 data shards + 4 parity shards
- Survives 4 shard loss with only 1.4x storage overhead
- vs 5x overhead for full replication (3.6x savings)

**Future Consideration for Nexus:**
```python
# Erasure coding for warm/cold storage tiers
class ErasureCodingBackend(Backend):
    """
    Future enhancement for cost-efficient redundancy.

    For warm/cold data where latency is less critical
    but durability and cost are important.
    """

    def __init__(self, data_shards: int = 10, parity_shards: int = 4):
        self.ec = pyeclib.ECDriver(
            k=data_shards,
            m=parity_shards,
            ec_type="liberasurecode_rs_vand"
        )
        self.shard_backends: List[Backend] = []  # Distributed across nodes

    async def write_content(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        shards = self.ec.encode(content)

        # Distribute shards across different backends/racks
        for i, shard in enumerate(shards):
            backend = self.shard_backends[i % len(self.shard_backends)]
            await backend.write_content(shard)

        return content_hash
```

---

## 3. Performance Benchmarks Reference

### SeaweedFS Performance (for comparison)

| Operation | Performance | Notes |
|-----------|------------|-------|
| Write (1KB files) | 5,747 req/s | 64 concurrent |
| Read (1KB files) | 12,988 req/s | Single replica |
| Read (with replication) | 19,423 req/s | Reads from replicas |
| Small object latency | 2.1ms | Average |
| Download throughput | 269 MB/s | Parallel, 100MB files |
| List (1000 results) | 44.43ms | - |

### Current Nexus Performance (from codebase)

| Feature | Performance | Source |
|---------|-------------|--------|
| Dynamic tool discovery | 78% token reduction | benchmarks/ |
| Directory listings (Tiger Cache) | 10-100x speedup | core/tiger_cache.py |
| CAS deduplication | 30-50% storage savings | backends/ |

### Target Improvements

| Metric | Current | Target | Approach |
|--------|---------|--------|----------|
| Files supported | ~10M | 100M+ | Partitioning (#870) |
| Read latency (cold) | ~50ms | <10ms | Tiered caching |
| Write throughput | ~1K/s | 5K+/s | Volume batching |
| Memory per file | ~100B | <50B | Efficient indexing |

---

## 4. Implementation Roadmap

### Phase 1: Database Scale (Q1 2026)
**Addresses:** Issue #870, #860

1. **Implement PostgreSQL partitioning** (2-3 weeks)
   - Hash partitioning by tenant_id
   - 64 partitions initial deployment
   - Migration script for existing data

2. **Add connection pooling layer** (1 week)
   - PgBouncer deployment
   - SQLAlchemy pool optimization

3. **Implement hybrid metadata index** (2 weeks)
   - LevelDB for path→hash lookups
   - Memory-mapped for hot paths

### Phase 2: Storage Optimization (Q2 2026)
**Addresses:** Issues #853, #788, #789-794

1. **Chunked upload/download** (3 weeks)
   - 8MB chunk size
   - Resumable uploads
   - HTTP Range support

2. **Tiered storage backend** (2 weeks)
   - Hot/warm/cold tiers
   - Automatic promotion/demotion
   - Cloud integration

3. **Volume-based storage** (4 weeks)
   - Needle storage format
   - Volume compaction
   - Background GC

### Phase 3: Advanced Features (Q3 2026)
**Future enhancements**

1. **Erasure coding** for cold storage
2. **Active-active replication** for multi-region
3. **FUSE mount** with local caching

---

## 5. Architectural Recommendations Summary

### Immediate Actions (P0)

1. **Database Partitioning**
   - Implement hash partitioning by tenant_id
   - Enables 100M+ file scale
   - Reference: SeaweedFS filer metadata distribution

2. **Memory-Efficient Indexing**
   - Add LevelDB/RocksDB index option
   - Reduce memory footprint from ~100B to ~24B per file
   - Reference: SeaweedFS volume server index

### Near-Term Actions (P1)

3. **Streaming Infrastructure**
   - Implement chunked uploads/downloads
   - Add HTTP Range request support
   - Reference: SeaweedFS large file handling

4. **Tiered Storage**
   - Implement hot/warm/cold tiers
   - Transparent access across tiers
   - Reference: SeaweedFS cloud tiering

### Long-Term Considerations (P2-P3)

5. **Volume-Based Storage**
   - Consider needle-in-haystack model for extreme scale
   - Reduces filesystem inode pressure
   - Major architectural change, evaluate ROI

6. **Erasure Coding**
   - For cost-efficient redundancy on cold data
   - 3.6x storage savings vs replication
   - Lower priority, evaluate when >1PB scale

---

## 6. Key Takeaways

### What SeaweedFS Does Exceptionally Well

1. **Minimal Metadata Overhead**: 40 bytes/file vs 536 bytes (XFS)
2. **O(1) Disk Operations**: All metadata in memory
3. **Separation of Concerns**: Master, Volume, Filer clearly separated
4. **Pluggable Everything**: Metadata stores, replication, cloud backends
5. **Operational Simplicity**: Simple to deploy and scale

### What Nexus Already Does Well

1. **Rich Permission Model**: ReBAC with Tiger Cache optimization
2. **Semantic Search**: Hybrid vector + keyword
3. **Multi-Tenancy**: Built-in tenant isolation
4. **Content Deduplication**: SHA-256 CAS across all backends
5. **Flexible Backends**: Local, GCS, S3, OAuth connectors

### Synergy Opportunities

| SeaweedFS Strength | Nexus Enhancement |
|-------------------|-------------------|
| Volume storage | Replace LocalBackend for scale |
| Tiered storage | Add cloud tiering to existing backends |
| Memory indexing | Add LevelDB option to metadata store |
| Chunked files | Streaming for large file support |
| Erasure coding | Cost optimization for cold storage |

---

## References

### Primary Sources
- [SeaweedFS GitHub Repository](https://github.com/seaweedfs/seaweedfs)
- [SeaweedFS Wiki](https://github.com/seaweedfs/seaweedfs/wiki)
- [Facebook Haystack Paper](https://www.usenix.org/legacy/event/osdi10/tech/full_papers/Beaver.pdf)
- [Facebook F4 Paper](https://www.usenix.org/system/files/conference/osdi14/osdi14-paper-muralidhar.pdf)

### Nexus References
- Issue #870: Database partitioning for 100M+ files
- Issue #860: PgBouncer connection pooling
- Issue #853: HTTP streaming endpoint
- Issue #788: Chunked/resumable uploads
- Issue #789-794: Media handling features

### Benchmarks
- [SeaweedFS Benchmarks Wiki](https://github.com/seaweedfs/seaweedfs/wiki/Benchmarks)
- [RepoFlow S3 Benchmark 2025](https://www.repoflow.io/blog/benchmarking-self-hosted-s3-compatible-storage)
