# SeaweedFS Deep Technical Dive - Complete Internals Analysis

**Research Date:** 2025-12-26
**Purpose:** Comprehensive deep-dive into SeaweedFS source code and internals for Nexus optimization insights

---

## Table of Contents

1. [Volume Server Internals](#1-volume-server-internals)
2. [Master Server Internals](#2-master-server-internals)
3. [Filer Deep Dive](#3-filer-deep-dive)
4. [Caching Architecture](#4-caching-architecture)
5. [Critical Performance Code](#5-critical-performance-code)
6. [Replication & Consistency](#6-replication--consistency)
7. [Erasure Coding Implementation](#7-erasure-coding-implementation)
8. [Key Learnings for Nexus](#8-key-learnings-for-nexus)

---

## 1. Volume Server Internals

### 1.1 Needle Storage Format (Exact Byte Layout)

**Core Structure:**
```go
type Needle struct {
    Cookie       uint32    // Random number to mitigate brute force lookups
    Id           NeedleId  // 8 bytes - needle id
    Size         uint32    // Sum of DataSize,Data,NameSize,Name,MimeSize,Mime
    DataSize     uint32    // Actual data size (version2)
    Data         []byte    // The actual file data
    Flags        byte      // Boolean flags (version2)
    NameSize     uint8     // Max 255 characters (version2)
    Name         []byte    // Filename
    MimeSize     uint8     // Max 255 characters (version2)
    Mime         []byte    // MIME type
    PairsSize    uint16    // Additional metadata size
    Pairs        []byte    // JSON format key-value pairs, max 64KB
    LastModified uint64    // Only 5 bytes stored to disk
    Ttl          *TTL      // Time to live
    Checksum     CRC32     // CRC32 for integrity checking
    AppendAtNs   uint64    // Append timestamp in nanoseconds (version3)
    Padding      []byte    // Aligned to 8 bytes
}
```

**Needle Flags:**
- `FlagIsCompressed = 0x01`
- `FlagHasName = 0x02`
- `FlagHasMime = 0x04`
- `FlagHasLastModifiedDate = 0x08`
- `FlagHasTtl = 0x10`
- `FlagHasPairs = 0x20`
- `FlagIsChunkManifest = 0x80`

**Storage Efficiency:**
- **16 bytes** in-memory per file: `<64bit key, 32bit offset, 32bit size>`
- **40 bytes** disk overhead per file metadata
- **Compact format:** 16 bytes when stored in binary (4 + 8 + 4) vs 536 bytes for XFS inode
- **String format:** Max 33 bytes (8+1+16+8 = volume_id + comma + file_key_hex + cookie_hex)

**NeedleId Size:** 8 bytes (`NeedleIdSize = 8`)

### 1.2 Volume File Structure (.dat and .idx files)

**.dat File (Data File):**
```
+-------------+
|SuperBlock   | (Volume header)
+-------------+
|Needle1      | (File + metadata)
+-------------+
|Needle2      |
+-------------+
|Needle...    |
+-------------+
```

**.idx File (Index File):**
- **Normal version (30GB volumes):** 16 bytes per entry
- **Large disk version (8TB volumes):** 20 bytes per entry (5-byte offset instead of 4)
- **Can be regenerated:** .idx files can be reconstructed from .dat files using `weed fix`
- **Format difference:** .dat format is identical across versions, only .idx differs

**.vif File (Volume Info):**
- JSON file containing volume metadata
- Includes `BytesOffset` setting: 4 (normal) or 5 (large_disk)

**Volume Constraints:**
- Default maximum: **30 GiB** (32GB, or 8×2^32 bytes)
- Large disk mode: **~8TB** (with 5-byte offset compilation flag)
- Based on 4-byte offset into 8-byte aligned data
- Available in Docker: `chrislusf/seaweedfs:3.85_large_disk`

### 1.3 Memory-Mapped File Handling

**Memory Map Feature (Windows optimization):**
- Enabled via `memorymapmaxsizemb` parameter
- **Performance gain:** 81-84% faster writes by combining multiple file operations
- **Mechanism:**
  - .idx file behavior unchanged
  - .dat file size reserved upfront (cannot grow after mmap)
  - Reserved space appears on disk but not written until needed
  - Memory only allocated when mapped memory needs paging out

**Collection Behavior:**
- Once collection created with mmap, size is fixed permanently
- Cannot change `memorymapmaxsizemb` for existing collection
- All files in collection go to same storage type (HDD/memory)

### 1.4 Index Structures: Needle Map Implementations

SeaweedFS provides multiple needle map implementations with different memory/performance tradeoffs:

**1. In-Memory Index (Default):**
- **Lookup:** O(1) disk read
- **Memory:** ~20 bytes per file
- **Startup:** Slow (must load all indexes)
- **Access:** Fastest during operation
- **Example:** 30GB volume with 1M files @ 30KB average = 20MB memory

**2. LevelDB Index:**
Three flavors with different memory footprints:
- **leveldb:** 4MB total (1 write buffer, 2 block buffers) - small footprint
- **leveldbMedium:** 8MB total (2 write buffers, 4 block buffers) - medium
- **leveldbLarge:** 12MB total (4 write buffers, 8 block buffers) - large

**Configuration:** `weed volume -index=leveldb` or `-index=leveldbMedium|leveldbLarge`

**LevelDB Performance:**
- Much faster startup vs memory index
- Slightly slower access (but network speed typically bottleneck)
- Index regeneration: 27,188,148 bytes in **8 seconds** (vs 6 minutes for boltdb)
- Optimization: Use `isLevelDbFresh()` to avoid unnecessary regeneration

**3. Sorted Index (.sdx):**
- Volume server can sort and store index as `.sdx` file
- **Lookup:** Binary search (log N) instead of O(1) memory
- Reduces memory pressure for cold/archival data

**4. CompactMap (Recent optimization):**
- Version 3.88+ includes **95% memory reduction** rewrite by @proton-lisandro-pin
- Assumes mostly increasing needle IDs for efficiency
- Methods: `NewCompactMap()`, `AscendingVisit()`, `Delete()`, `Get()`, `Set()`

**Best Practice:** Keep needle IDs in "mostly increasing" order for optimal CompactMap efficiency. If random IDs required, use LevelDB index.

### 1.5 Compaction Algorithm (Vacuum) - Space Reclamation

**How Vacuum Works:**
1. **Trigger:** Empty space > threshold (default 30%, configurable)
2. **Process:**
   - Make volume read-only
   - Create new volume
   - Serially iterate all needles
   - Copy only non-deleted files to new volume
   - Delete old volume
   - Switch to new volume

**Manual Triggering:**
```bash
# In weed shell
volume.vacuum -garbageThreshold=0.0001  # Very aggressive
volume.vacuum -garbageThreshold=0.1     # 10% threshold
```

**Source Code:**
- `weed/storage/store_vacuum.go` - Main vacuum logic
- `weed/storage/volume_vacuum.go` (lines 39-68, 110-124)
- `CheckCompactVolume` - Checks garbage level
- `CompactVolume` - Calculates space, performs compaction

**Offline Compaction:**
- Command: `weed compact`
- Generates `.cpd` and `.cpx` files
- Manual rename required: `.cpd` → `.dat`, `.cpx` → `.idx`

**Key Characteristics:**
- Append-only friendly to SSDs (no fragmentation)
- Background operation, doesn't slow reads
- Volume-level operation (not file-level)
- **Limitation:** EC (erasure coded) shards not supported for compaction

### 1.6 Deletion Implementation (Tombstones, Lazy Deletion)

**Soft Delete Approach:**
- Reference to blob is forgotten (GDPR sufficient)
- Actual data remains until vacuum/compaction
- File appears as "tombstone" in volume data

**Vacuum Process:**
- Copies volume skipping deleted files
- Size increases then decreases during process
- Deleted needles not copied to new volume

**Storage Behavior:**
- Append-only structure (SSD-friendly)
- No immediate space reclamation
- Background deletion/compaction at volume level
- No read slowdown or fragmentation

**Erasure Coded Volumes:**
- Only deletion supported (no updates)
- Compaction requires conversion back to normal volumes first

### 1.7 Write Path: HTTP to Disk Flow

**Client Write Flow:**

1. **Request File ID:**
   - Client sends write request to Master
   - Master returns: `(volume_id, file_key, file_cookie, volume_node_URL)`

2. **Upload Data:**
   - Client POSTs file content to Volume Server HTTP endpoint
   - Default port: 8080
   - Volume Server handles needle creation

3. **Needle Creation:**
   - Data written to `.dat` file (append-only)
   - Index updated in memory or LevelDB
   - Checksum calculated (CRC32)
   - Metadata written with data

4. **Replication (if configured):**
   - Synchronous writes to all replicas
   - Write fails if any replica fails (W=N, strong consistency)

5. **Metadata Registration (if using Filer):**
   - `(path, fileId, fileSize)` registered via gRPC
   - Filer updates metadata store
   - No data passes through Filer (only metadata)

**Fsync Strategy:**
- Volume Server API supports `fsync` URL parameter
- Default: `fsync=false` (performance)
- When `true`: incurs fsync operation for durability
- **Performance consideration:** SATA drives can block; use SSD/NVMe or RAID

**Key Design Choice:** Data flows directly client → volume server. Filer is metadata-only, reducing load.

---

## 2. Master Server Internals

### 2.1 Volume ID Allocation Algorithm

**High Watermark Approach:**
- Master maintains incrementing volume ID counter
- "High watermark of assigned volume id" is soft state
- Risk: Fast master switching can create duplicate volumes (split-brain)

**File ID Generation:**
```
FileID Format: <volume_id>,<file_key>,<file_cookie>
Example: 3,01,637037d6
```

**Components:**
- **Volume ID:** Unsigned 32-bit integer
- **File Key:** Unsigned 64-bit integer (growing, generated per write request)
- **File Cookie:** Unsigned 32-bit integer (prevents URL guessing, customizable)

**Encoding:**
- File key and cookie coded in hex
- Can store as string (33 bytes max) or binary tuple (16 bytes: 4+8+4)

**Multi-Master Considerations:**
- Each master should have unique ID for globally unique volume+file IDs
- Proposals exist to use UUIDs or timestamp-based IDs to avoid collisions

**Volume Assignment:**
- No checking when writing to volumes
- Can write to any volume ID, file key, cookie (if capacity available)
- Master assigns writable volumes, but direct writes possible

### 2.2 Topology Management (DataCenter → Rack → Node)

**Four-Level Hierarchy:**
```
Topology
  └─ DataCenter
      └─ Rack
          └─ DataNode
              └─ Disk
```

**Topology Struct Functions:**
- `GetOrCreateDataCenter(dcName string)` - Manage data centers
- `GetVolumeLayout()` - Manage layouts by collection, replication, TTL

**DataCenter Functions:**
- `GetOrCreateRack(rackName string)` - Create/fetch racks
- Rack can find/create nodes: `GetOrCreateDataNode(ip, port, capacity)`

**Volume Layout Tracking:**
- `vid2location` - Maps volume IDs to VolumeLocationList (replica locations)
- Tracks writable volumes
- Maintains binary states: readonly, oversized
- Organized by: collection, replication placement, TTL, disk type

**Capacity Reservation System:**
- Prevents race conditions during concurrent allocation
- Each node maintains `CapacityReservations`
- `tryReserveAtomic()` provides atomic capacity checks
- Prevents over-provisioning from concurrent requests seeing same capacity

**Configuration:**
- XML topology file defines DC/rack structure with IP mappings
- Example: `<DataCenter name="dc1"><Rack name="rack1"><Ip>192.168.1.1</Ip></Rack></DataCenter>`
- Volume servers configured with: dir, datacenter, rack, ip, port

**Replication Placement Codes:**
- `001` - Same rack
- `010` - Different rack, same DC
- `100` - Different datacenter
- `XYZ` format: (datacenter)(rack)(volume_server)
- Total copies = 1 + sum(digits). Example: `205` = 1+2+0+5 = 8 copies

### 2.3 Raft Consensus Implementation

**Dual Raft Support:**
- **seaweedfs/raft:** Custom lightweight implementation (`weed/server/raft_server.go`)
- **hashicorp/raft:** Battle-tested standard implementation (`weed/server/raft_hashicorp.go`)
- **Selection:** `-raftHashicorp` flag chooses implementation

**State Machine Replication:**
- `StateMachine` struct implements both interfaces:
  - `raft.StateMachine` (seaweedfs)
  - `hashicorpRaft.FSM` (hashicorp)
- Replicates `MaxVolumeIdCommand` across masters
- Located: `weed/topology/cluster_commands.go` (lines 12-45)

**Master Cluster Configuration:**
- 3-5 masters recommended (n/2+1 quorum)
- Raft consensus for leader election
- Leader elected via Raft protocol
- Consistent view of entire cluster

**Leadership & Failover:**
- Leader takes all volume management work
- Leader assigns file IDs
- Non-leader masters forward requests to leader
- **Automatic failover:** ~100ms downtime on failure
- **Election process:**
  - Followers detect missed heartbeats
  - Initiate new election
  - `RaftServer.monitorLeaderLoop()` handles transitions (lines 60-84)

**State Persistence:**
- Masters only track volume metadata (small, stable data)
- "Which volumes exist on which servers"
- Stored in Topology struct
- New leader receives full volume info via heartbeats

**Bootstrap Options:**
- `raftHashicorp` flag for HashiCorp implementation
- `raft Bootstrap` option: force launch without quorum (removes state like max volume ID)
- Use with caution in production

### 2.4 File ID Generation & Free Volume Tracking

**File ID Assignment Process:**

1. **Client Request:**
   - Client sends write request with replication preference to master
   - Master validates replication against topology

2. **Volume Selection:**
   - Master finds writable volume matching criteria:
     - Collection (if specified)
     - Replication level
     - TTL setting
     - Disk type
   - Uses free volume tracking in VolumeLayout

3. **File Key Generation:**
   - Master generates growing 64-bit unsigned integer
   - Unique per volume
   - Usually monotonically increasing

4. **File Cookie Generation:**
   - 32-bit random integer
   - Mitigates brute-force URL guessing attacks
   - Customizable if needed

5. **Response:**
   - Returns: `(volume_id, file_key, file_cookie, volume_node_URL)`
   - Client proceeds with direct upload to volume server

**Free Volume Tracking:**

**VolumeLayout Structure:**
- Tracks volumes by: collection, replication, TTL, disk type
- Maintains list of writable volumes
- Binary states: readonly, oversized
- Capacity reservation system for concurrent allocations

**Volume Growth Strategy:**
- Default: 7 concurrent writable volumes (no-replication)
- Configurable via master.toml: `volume.growth.strategy`
- Can request growth via API: `/vol/grow?count=X`

**Collection Concept:**
- Collection = group of volumes
- Each volume has own TTL and replication
- Initially auto-created if not present
- Default collection starts with 7 volumes

**Volume Capacity Management:**
- Default volume size: 30GB
- Tracks usage per volume
- Marks volumes readonly when full
- Auto-creates new volumes as needed

**Heartbeat Integration:**
- Volume servers send periodic heartbeats to leader
- Include volume status and capacity info
- Master updates free volume tracking
- Stale volumes removed from writable list

---

## 3. Filer Deep Dive

### 3.1 Directory Entry Storage Format

**Metadata Store Options:**
Filer supports 15+ backends: MySQL, PostgreSQL, Sqlite, MongoDB, Redis, Cassandra, HBase, Elasticsearch, LevelDB, RocksDB, MemSQL, TiDB, CockroachDB, Etcd, YDB

**PostgreSQL Schema (Example):**
```sql
CREATE TABLE filemeta (
    dirhash BIGINT,              -- Hash of directory path for fast lookups
    name VARCHAR(65535),          -- Filename (supports up to 65k chars)
    directory VARCHAR(65535),     -- Full path storage
    meta bytea,                   -- Serialized protobuf (timestamps, permissions, chunks)
    PRIMARY KEY (dirhash, name)
);
```

**Key Optimizations:**
- **dirhash:** Optimized directory hash prevents directory-level locks
- **Primary key:** `(dirhash, name)` for fast lookups and uniqueness
- **Protobuf metadata:** Efficient binary serialization

**Storage Overhead:**
- Only 40 bytes disk overhead per file metadata
- Volume level: 512 bytes + 16 bytes per file
- 1 million small files = ~16MB metadata

**LevelDB Variants (for filer metadata store):**
- **leveldb:** Uses full file path as key
- **leveldb2:** Uses 128-bit MD5 hash of partial lookup key (optimization)
- **leveldb3:** Separate instances per bucket

### 3.2 Path Lookup Optimization

**How Path Resolution Works:**

Traditional path `/a/b/c/d` resolution requires multiple lookups. SeaweedFS optimizes this:

**1. Volume ID Caching:**
- Volume IDs statically assigned
- Locating file content = lookup volume ID (easily cached)
- No network round trip for volume location in mounted systems

**2. Filer Store Lookup Performance:**

| Store Type | Lookup Complexity | Scalability |
|------------|------------------|-------------|
| Memory, Redis, Redis2 | O(1) | Limited by RAM |
| LevelDB, RocksDB, SQL | O(log N) | Unlimited entries |
| Cassandra, MongoDB | O(1) distributed | Unlimited entries |

**3. Mount Optimizations (Recent):**
- PR #7818: "Efficient file lookup in large directories, skipping directory caching"
- PR #7697: "Improve EnsureVisited performance with dedup, parallelism, batching"
- Asynchronous metadata replication to local DB
- **No remote metadata reads** after initial sync

**4. PostgreSQL Optimization:**
- `dirhash` (BIGINT) for fast hash-based lookups
- Index on `(dirhash, name)` for O(1) to O(log N) access
- Avoids full directory path comparisons

**5. Metadata Caching:**
- Default cache TTL: 60 seconds (`-cacheMetaTtlSec`)
- FUSE mount maintains local metadata cache
- Directory listings are local operations after sync

### 3.3 Large Directory Handling (Millions of Entries)

**Problem:**
Traditional approaches store all child entries on one node, creating bottlenecks with billions of entries.

**Super Large Directories Feature:**

**Cassandra Implementation:**
- **Normal directories:** Use `<directory_hash, name>` as primary key
  - Directory hash as partitioning key
  - All children co-located on one node

- **Super large directories:** Use `<full_path>` as partitioning key
  - Child entries spread across **all Cassandra nodes**
  - Prevents single-node overload
  - **Trade-off:** Directory listing unsupported (range queries impossible)

**Redis Implementation:**
- **Normal directories:** `<path, sorted_set_of_child_entry_names>`
  - Single key-value pair per directory

- **Super large directories:** Skip sorted set operation entirely
  - Eliminates bottleneck of massive sorted sets
  - No tracking of all child entry names

**Configuration (filer.toml):**
```toml
superLargeDirectories = ["/home/users", "/data/uploads"]
```

**Performance Implications:**

**Advantages:**
- Horizontal scalability across all cluster nodes
- No per-node memory/performance degradation
- Maintains efficient file access at any depth

**Limitations:**
- Directory listing disabled for configured paths
- Example: If `/home/users/` is super large:
  - ❌ `ls /home/users/` doesn't work
  - ✅ `ls /home/users/user1` works
  - ✅ `ls /home/users/user1/books` works
- Metadata import/export unsupported
- Configuration essentially irreversible (requires data iteration to change)

**LevelDB Performance Issue:**
- UUID filenames in millions cause slowdowns at `leveldb/table.(*Reader).find`
- **Solution 1:** Remove `CompactionTableSizeMultiplier: 10`
  - Throughput: 3 Mbps → 60 Mbps
  - CPU usage: significantly reduced
- **Solution 2:** Add bloom filter
  - Throughput: 3 Mbps → 30 Mbps

**Memory Requirements:**
- 2 million files = ~24MB in-memory index (if all in one volume)
- Default in-memory index: ~20 bytes per file
- LevelDB index reduces memory, speeds startup

### 3.4 Chunk Manifest Format for Large Files

**SeaweedFS Large File Strategy:**

**Small Files:** ≤ 8MB (configurable, 1-10MB typical)
- Stored as single needle
- One chunk of data
- O(1) disk read

**Medium Files:** 8MB - 80GB
- Split into chunks (default 8MB each)
- Each chunk = separate SeaweedFS file ID
- Chunk metadata: ~40 bytes uncompressed per chunk
- Key-value store manages 1,000-10,000 chunk references
- File size range: 8GB - 80GB

**Super Large Files:** > 80GB

Uses **manifest chunk system**:

**Data Structures:**
```go
type ChunkInfo struct {
    Fid    string `json:"fid"`      // SeaweedFS file ID
    Offset int64  `json:"offset"`   // Byte offset in original file
    Size   int64  `json:"size"`     // Chunk size in bytes
}

type ChunkManifest struct {
    Name   string      `json:"name,omitempty"`   // Original filename
    Mime   string      `json:"mime,omitempty"`   // MIME type
    Size   int64       `json:"size,omitempty"`   // Total file size
    Chunks []ChunkInfo `json:"chunks,omitempty"` // List of chunks
}
```

**Manifest Chunk Structure:**
- Holds **~1,000 chunk info entries**
- Stored **on volume servers** (not key-value store)
- Greatly reduces metadata store load
- Reduces access time

**Scaling Example:**
```
1 super large file with 1,000 manifest chunks:
- Metadata in key-value store: 400KB
- Addressable file size: 8MB × 1,000 × 1,000 = 8TB
```

**Recursive Manifest Support:**
- Manifests can reference other manifests
- Enables even larger file sizes (theoretical, not common)
- SeaweedFS deliberately avoids deep recursion:
  - Multi-level indirection = unpredictable latency
  - Current limits exceed common use cases
  - Future implementation only when necessary

**Upload Process for Large Files:**

1. **Client-side chunking** (delegated to client):
   - Split file into chunks
   - Upload each chunk as normal file
   - Save metadata into ChunkInfo struct

2. **Chunk distribution:**
   - Each chunk can go to different volumes
   - Enables parallel access
   - No particular chunk size limit
   - Chunks don't need to be same size

3. **Upload manifest:**
   - Create ChunkManifest JSON
   - Upload with `Content-Type: application/json`
   - Add URL parameter: `cm=true`

**Auto-Chunking (Filer & FUSE):**
- Filer server automatically chunks large files
- FUSE mount auto-chunks via:
  - `-cacheDirWrite` - cache directory for writes
  - `-chunkSizeLimitMB` - chunk size (default 2MB)
- Chunk list stored in filer storage
- Managed by filer or weed mount client

**Chunk Size Guidelines:**
- Keep whole chunk in memory
- Avoid too many small chunks
- No strict limit on chunk file size
- Each chunk size can vary within same file

---

## 4. Caching Architecture

### 4.1 Filer Cache Layers

**Filer Metadata Caching:**
- Configuration via filer startup flags
- Default TTL: 60 seconds
- Configurable: `-cacheMetaTtlSec`

**FUSE Mount Caching:**

**Metadata Cache:**
- Asynchronous replication to local database
- **Zero remote metadata reads** after sync
- Persistent client connection to Master for volume location updates
- Continuous metadata synchronization with Filer
- Directory listings are local operations

**Data Cache:**
- `-cacheDir` - Local cache directory for chunks and metadata (default: temp dir)
- `-cacheCapacityMB` - File chunk read cache capacity (default: 128MB)
- `-cacheDirWrite` - Buffer for write operations (mostly large files)

**Example Mount with Cache:**
```bash
weed fuse /mnt/weedcluster \
  -o "filer='10.4.4.66:8888,10.4.4.77:8888',
      cacheCapacityMB=4000,
      cacheDir=/tmp,
      chunkSizeLimitMB=4"
```

**Cache Allocation:**
- Default: 1000MB capacity
- Distributed across different chunk size sections
- Frequently accessed data cached locally

**Known Issues:**
- **Bug:** Metadata cached, but file/chunk content not cached (data pulled from network repeatedly)
- **Bug:** Setting `cacheCapacityMB=0` causes panic on read
- **Bug:** Cache files not cleaned up on normal exit (disk space not released)

### 4.2 Volume Server Read Cache

**In-Memory Index Cache:**
- All file metadata readable from memory
- Each file: 16-byte map entry `<64bit key, 32bit offset, 32bit size>`
- O(1) disk read performance
- Default: ~20 bytes per file in memory

**Index Type Options:**

| Type | Memory | Startup | Access Speed | Use Case |
|------|--------|---------|--------------|----------|
| Memory (default) | ~20 bytes/file | Slow (load all indexes) | Fastest | Hot data, small datasets |
| LevelDB | 4MB | Fast | Slightly slower | General purpose |
| LevelDBMedium | 8MB | Fast | Good | Medium workloads |
| LevelDBLarge | 12MB | Fast | Better | Heavy workloads |

**Metadata in Memory:**
- 30GB volume with 1M files @ 30KB average
- Memory needed: 20MB for index
- **All metadata operations are memory-resident**
- Zero disk seeks for metadata lookups

**Volume Location Caching:**
- weed mount maintains persistent Master connection
- Volume location updates received continuously
- **Zero network round trips** for volume ID location
- Volume IDs statically assigned and easily cached

### 4.3 Client-Side Chunk Cache (FUSE Mount)

**Cache Directory Structure:**
- Metadata directory
- Read cache files
- Write cache files

**Capacity Management:**
- Configurable via `-cacheCapacityMB`
- Default: 128MB
- Distributed across chunk sizes
- LRU eviction implied

**Write Buffering:**
- `-cacheDirWrite` directory for buffering writes
- Particularly for large files
- Reduces network round trips
- Enables batching of small writes

**Chunk Size Configuration:**
- `-chunkSizeLimitMB` - local write buffer size (default 2MB)
- Also controls auto-chunking threshold
- Balances memory vs network efficiency

**Performance Characteristics:**
- **Sysbench (single-threaded):**
  - Reads: ~958 reads/sec
  - Writes: ~639 writes/sec

- **Sysbench (16 threads):**
  - Reads: ~2,153 reads/sec
  - Writes: ~1,436 writes/sec

**Limitations:**
- Performance expected to be less than local disk
- FUSE overhead
- Remote persistence requirements (filer + volume servers)

### 4.4 Cache Coherency & Invalidation

**TTL-Based Invalidation:**
- File metadata automatically expires via TTL
- Volume servers delete expired files
- **Coherency Issue:** Filer metadata may outlive actual data
  - File deleted from volume after TTL
  - Filer database still shows file exists
  - Results in "volume not found" errors
  - Database accumulates garbage

**TTL Calculation:**
- Currently based on file creation time (Crtime)
- Open issue: Requests to use modification time instead
- TTL synchronization between filer and volumes incomplete

**Filer Metadata Coherency Issues:**
- Records after TTL continue in filer database
- File expirations not synchronized between filer and volumes
- Stale metadata persists even after data expires

**Coherency Restoration:**
- No automatic cleanup mechanism
- Manual approach:
  1. Copy all files from each filer
  2. Reinitialize cluster from scratch
  3. Copy data back
- Expensive and disruptive

**Distributed Filer Coherency:**
- Multiple filers can serve same metadata store
- Cache coherency across filers is challenge
- Updates to one filer may not immediately visible to others
- TTL-based eventual consistency

**Volume Server Cache:**
- In-memory index always consistent with disk
- LevelDB index regenerated on startup if needed
- `isLevelDbFresh()` checks if regeneration required

---

## 5. Critical Performance Code

### 5.1 O(1) Disk Read - How Exactly Achieved

**The Fundamental Design:**

SeaweedFS achieves O(1) disk reads through a multi-layered approach inspired by Facebook's Haystack:

**1. Static Volume ID Assignment:**
```
File location = (Volume ID, Offset within volume)
Volume ID → statically assigned, never changes
```

- Volume ID assigned once when file written
- Locating file content = simple volume ID lookup
- **Volume ID easily cached** (doesn't change)

**2. In-Memory Needle Index:**

**Per-file entry (16 bytes):**
```
<64-bit NeedleId, 32-bit Offset, 32-bit Size>
```

- **All file metadata loaded in memory**
- Lookup: Hash table or binary search in memory
- Memory access: nanoseconds
- Disk access: milliseconds
- **Result: O(1) disk seek**

**3. Single Disk Operation Per File:**

**Read flow:**
```
1. Lookup needle in in-memory index: O(1) memory operation
2. Get: (offset, size) from index
3. Seek to offset in .dat file: O(1) disk seek
4. Read size bytes: Sequential I/O
```

**No directory traversal required**
- Traditional filesystems: Multiple seeks for directory tree
- SeaweedFS: Direct offset calculation from memory index

**4. Volume ID to Server Mapping:**

Master maintains lightweight mapping:
```
Volume ID → Volume Server URL
```

- Small amount of data (vs per-file metadata)
- Easily cached at client
- Persistent connection in weed mount eliminates lookups

**Memory Efficiency Math:**

Example: 30GB volume
- 1 million files @ 30KB average
- Memory needed: 1M files × 20 bytes = **20MB**
- Disk overhead: 1M files × 40 bytes = **40MB**

**Comparison to Traditional FS:**
- XFS inode: 536 bytes
- SeaweedFS needle: 40 bytes disk, 16 bytes memory
- **13.4x more efficient** than XFS for storage
- **33.5x more efficient** for in-memory index

**Design Origins:**
- Based on Facebook Haystack paper
- Key insight: **Avoid disk operations for metadata**
- Keep all metadata in memory
- Large files (30GB volumes) reduce metadata overhead
- Per-file expense → per-volume expense

### 5.2 Batch Operations Implementation

**Write Operations:**

**Architecture:**
- HTTP REST operations for read/write/delete
- JSON/JSONP responses
- Direct client → volume server communication

**Concurrent Writes:**
- Default: 7 concurrent writable volumes (no-replication)
- Distributes writes across multiple volumes
- Configurable via master.toml or API: `/vol/grow?count=X`

**Performance Characteristics:**

**Benchmark Results (1 million 1KB files, concurrency 64):**
- **Throughput:** 5,747 - 5,993 requests/sec
- **Completion time:** 182 seconds
- **Average latency:** 10.9ms connection time
- **50th percentile:** 9.7ms

**Optimization Strategies:**

1. **More hard drives = better throughput**
   - Parallel I/O across drives
   - SeaweedFS automatically distributes

2. **Disk preallocation:**
   - On XFS, ext4, Btrfs
   - Flag: `-volumePreallocate`
   - Ensures contiguous blocks
   - Improves large file performance

3. **Volume growth strategy:**
   - More concurrent writable volumes
   - Distributes write load
   - Reduces contention

**Write Flow (with replication):**
1. Client requests FID from master (with replication level)
2. Master returns volume info + URLs for all replicas
3. Client writes to primary volume server
4. Primary **synchronously** replicates to all replicas
5. Write succeeds only if **all N replicas succeed** (W=N)

**Read Operations:**

**Single volume server performance:**
- Essentially tests hard drive random read speed
- O(1) disk seek means minimal software overhead
- Network becomes bottleneck before storage

**Replication benefits:**
- Multiple servers = multiple read sources
- Read from any replica (R=1)
- More volumes = higher read throughput
- Load balancing across replicas

### 5.3 Concurrent Read/Write Handling

**Master Server Design:**

**Concurrency Relief:**
- Master doesn't manage individual file metadata
- Only manages volumes (much less data)
- File metadata spread across volume servers
- **Relieves concurrency pressure** from central master

**Volume-Level Locking:**
- Locking at volume level, not file level
- Multiple files in same volume can have contention
- But: 7+ concurrent writable volumes by default
- Spreads concurrent writes across volumes

**Volume Server Thread Safety:**

**Known Issues (Historical):**
- Version 0.71 beta: "concurrent map read and map write" error
- Go runtime error with unsafe concurrent map access
- Fixed in later versions

**FUSE Mount Concurrency:**
- **Issue:** Write stalls some read operations
- Large file write can block chunk reads
- Reads unstalled after write completes
- **Expected:** Reads should continue during writes

**Connection Pool Thread Safety:**
- Thread-safe borrowing/returning mechanisms
- Reusable network connections to volume servers
- Reduces overhead vs creating new connections

**Concurrent Access Architecture:**

**Volume Server:**
- Manages only local disk files
- 16 bytes per file in memory
- All metadata access from memory = fast concurrent access
- Single disk operation for data read

**File Descriptor Limits:**
- Production requires elevated limits
- `ulimit -n 10240` recommended
- Handles concurrent network requests
- Default 1024 insufficient for high concurrency

**Memory Requirements for Concurrency:**
- Example: 1000 concurrent reads of 100KB files
- Memory needed: 100KB × 1000 = **100MB buffer space**
- Plus index memory: files × 20 bytes

**LOSF (Lots of Small Files) Handling:**
- Specifically designed for high concurrency LOSF workloads
- Append-only structure reduces lock contention
- Memory-resident metadata enables parallel lookups
- Volume-level operations don't block individual file access

### 5.4 Memory Efficiency Tricks

**1. CompactMap Optimization (95% reduction):**

**Version 3.88+ breakthrough:**
- @proton-lisandro-pin rewrote `needle_map.CompactMap()`
- **95% memory reduction**
- Assumes mostly increasing keys
- PR #6813, continued in #6842

**CompactMap Structure:**
```go
type NeedleValue struct {
    Key    NeedleId   // 8 bytes
    Offset uint32     // 4 bytes (volume offset / 8, range: 32GB)
    Size   uint32     // 4 bytes
}
```

**Compact storage:** 16 bytes vs 536 bytes (XFS inode)

**2. Index Type Selection:**

| Index Type | Memory/Volume | Use Case |
|------------|---------------|----------|
| Memory | ~20 bytes × file count | Hot data, fast access needed |
| LevelDB | 4-12 MB fixed | Cold data, memory constrained |
| Sorted (.sdx) | Minimal | Archival, read-mostly |

**3. LevelDB Memory Tiers:**
- **Small (4MB):** 1 write buffer, 2 block buffers
- **Medium (8MB):** 2 write buffers, 4 block buffers
- **Large (12MB):** 4 write buffers, 8 block buffers

**Trade-off:** Startup speed vs memory usage

**4. Increasing Needle ID Order:**

**Best practice:**
```
Keep needle IDs "mostly" increasing:
- CompactMap works best with sequential IDs
- Reduces memory overhead
- Better cache locality
```

**If random IDs required:**
- Use LevelDB index instead
- Pays memory penalty for flexibility

**5. Volume-Level Metadata:**

**Master Server:**
- Stores only volume metadata (not per-file)
- Volume count << file count
- Example: 1M files in 100 volumes
  - Master manages: 100 entries
  - Not: 1M entries

**Volume Server:**
- 16 bytes per file in memory
- 40 bytes per file on disk
- **Both orders of magnitude less than traditional FS**

**6. Metadata Store Selection (Filer):**

**Memory-based:**
- Fastest but RAM-limited
- Good for small datasets or caching

**LSM-based (LevelDB, RocksDB):**
- O(log N) lookups
- Much lower memory footprint
- Unlimited file count

**Distributed (Cassandra, TiDB):**
- Horizontal scaling
- Memory spread across cluster
- Handles billions of entries

**7. Super Large Directory Partitioning:**

**Traditional:** All entries on one node = memory bottleneck

**Super Large (Cassandra):**
- Full path as partition key
- Entries spread across **all nodes**
- No single-node memory pressure

**8. Erasure Coding for Warm Storage:**

**1.4x storage (10+4 RS) vs 5x replication:**
- **Same reliability** (lose 4 of 14 shards)
- **3.6x disk savings**
- Frees memory on replicas no longer needed
- Trade: slightly slower reads (reconstruction)

**9. Lazy Metadata Loading (LevelDB):**
- Doesn't load all metadata at startup
- Loaded on-demand from LevelDB
- Much faster volume server startup
- Lower initial memory footprint

**10. Client-Side Chunking:**
- Delegates large file chunking to clients
- Avoids buffering entire large files in volume server memory
- Chunk manifest stored on volume server (not central metadata)

---

## 6. Replication & Consistency

### 6.1 Synchronous Replication Protocol

**Replication Policy Format: XYZ**

Format: `(DataCenter)(Rack)(VolumeServer)`

Examples:
- `000` - No replication (1 copy only)
- `001` - Replicate once on same rack
- `010` - Replicate once on different rack, same DC
- `100` - Replicate once on different datacenter
- `200` - Replicate twice on two different datacenters
- `205` - 1 + 2 + 0 + 5 = **8 total copies**

**Write Consistency Model: W=N, R=1**

```
All N replicas must succeed for write to succeed.
Only 1 replica needed for read to succeed.
```

**Synchronous Write Flow:**

1. Client requests FID from master with replication level
2. Master returns primary volume server + replica URLs
3. Client sends data to primary volume server
4. Primary volume server:
   - Writes to local disk
   - **Synchronously** replicates to all replicas
   - Waits for **all replicas to confirm**
5. **If any replica fails → entire write fails**
6. Only returns success when all N replicas succeed

**Strong Consistency:**
- All writes are strongly consistent
- No partial writes allowed
- Immediate consistency (no lag)
- Trade-off: Write latency increases with replica count

**Volume Assignment on Write Failure:**
- If write to one volume fails
- Just pick another volume to write
- Adding more volumes is simple
- No complex recovery needed

### 6.2 Failure Handling & Volume States

**Replica Failure Scenarios:**

**During Write:**
- If 1 of N replicas fails during write
- Entire write operation fails
- Client must retry (gets different volume/replica set)
- **No partial writes** persisted

**Missing Replica (Post-Failure):**
- SeaweedFS **does NOT auto-repair**
- Partially unavailable volume becomes **read-only**
- New writes go to different volume
- Prevents over-replication from transient failures

**Manual Repair Process:**

```bash
# In weed shell
volume.fix.replication     # Fix missing replicas
volume.balance -force      # Delete excessive copies, rebalance
```

**Repair should be:**
- Run periodically via scripts (not automatic)
- Complex: must compare timestamps, handle tombstones
- Not just "copy largest volume" - must reconcile deltas

**Volume States:**

| State | Cause | Behavior |
|-------|-------|----------|
| Writable | Normal operation | Accepts new writes |
| Read-only | Full, or missing replicas | Serves reads only |
| Offline | Server down | Temporarily unavailable |
| Missing Replica | Server failure | Need manual repair |

**Leader Failover (Master Server):**
- Leader election via Raft: **~100ms downtime**
- New leader elected automatically
- Volume servers send full heartbeat to new leader
- Temporary state: New leader has partial info
  - Yet-to-heartbeat volumes temporarily not writable
  - Resolves as heartbeats arrive

### 6.3 Volume Fsync Strategies

**Fsync Configuration:**

**Volume Server API Parameter: `fsync`**
- Default: `fsync=false` (performance)
- Set `fsync=true` for durability guarantee
- When enabled: incurs fsync operation on write

**Performance Implications:**

**SATA Drives:**
- Drive can become fully occupied
- Unable to process fsync from kernel
- Can block I/O pipeline

**Recommendation:**
- Use SSD/NVMe drives for fsync workloads
- Or use RAID storage
- HDD performance severely degraded with fsync

**Fsync Strategy by Use Case:**

| Use Case | Fsync Setting | Disk Type |
|----------|---------------|-----------|
| High throughput, some data loss OK | false | SATA HDD OK |
| Critical data, no loss acceptable | true | SSD/NVMe |
| S3 buckets with intensive read/write | true | SSD preferred |
| Warm/cold storage | false | HDD OK |

### 6.4 Data Integrity (Checksums, Verification)

**Checksum Algorithm: CRC32**

**Needle Structure:**
```go
type Needle struct {
    // ... other fields ...
    Checksum CRC32  // CRC32 to check integrity
}
```

**Checksum Verification:**

**On Read:**
- CRC32 verified when reading needle
- Corruption detected before returning data
- Error returned if checksum mismatch

**Test Results:**
- Intentionally corrupted .dat file
- FUSE mount read attempt
- Result: `cp: error reading 'file': Input/output error`
- **Properly detected corruption**

**Important Testing Note:**
- .dat files have fillers between needles
- Editing filler ≠ corrupting data
- Must corrupt actual needle data for valid test

**Volume Integrity Checks:**

**volume.check.disk:**
```bash
# Verifies data consistency across replicas
# Fixes inconsistencies found
```

**volume.fsck:**
```bash
# Identifies orphaned chunks (data not referenced by filer)
# Finds missing chunks referenced by filer
volume.fsck -findMissingChunksInFiler  # Specific check
```

**Startup Integrity:**
- Integrity checks performed on every volume during loading
- Incomplete/corrupted entries identified
- Self-healing removes corrupted entries (Enterprise feature)

**S3 API Checksum Support (Proposed):**

Open issue #6526 for S3-compatible uploads:
- Support trailing checksums
- Verify `x-amz-checksum-crc32` in trailer chunks
- Switch based on `x-amz-content-sha256` header

**Erasure Coding Integrity:**

**Reed-Solomon (10+4) provides:**
- Parity data for error correction
- Can lose up to 4 of 14 shards
- Reconstructs data even with corruption
- Additional layer beyond CRC32

**Data Reconstruction:**
```bash
ec.rebuild -force  # Reconstruct missing/corrupted shards
```

**Self-Healing Storage (Enterprise):**
- Automatic corruption detection
- Removes incomplete entries after crashes
- No manual intervention required
- Maintains consistent state after power loss

**Replication & Checksums:**
- File checksums used in replication
- Detects corruption during replica sync
- Enables resilience against concurrent failures
- Multiple replicas + checksums = robust integrity

---

## 7. Erasure Coding Implementation

### 7.1 Reed-Solomon EC (10+4) Configuration

**Erasure Coding Scheme: RS(10,4)**

- **10 data shards** + **4 parity shards** = **14 total shards**
- Can lose up to **4 of 14 shards** without data loss
- **Storage efficiency:** 1.4x data size (vs 5x for replication)
- **Disk savings:** 3.6x compared to 5-way replication for same reliability

**Shard Structure:**

```
30GB volume → 14 EC shards
Each shard: 3GB (30GB / 10 data shards = 3GB per shard)
Each shard contains 3 EC blocks (1GB each)
```

**Chunk Size:**
- Large volumes: Split into **1GB chunks**
- Small volumes (< 10GB): Split into **1MB chunks**
- Edge case handling for volumes < 10GB

**Customization:**

Source code: `ec_encoder.go`
- `DataShardsCount = 10` (adjustable)
- `ParityShardsCount = 4` (adjustable)

**Build Optimization:**
```bash
# Enable faster AVX2 instructions
GOAMD64=v4 go build
```

### 7.2 Encoding/Decoding Process

**Encoding Process:**

**Command:** `ec.encode`

**Criteria for encoding:**
- Volume ≥ 95% full (configurable: `-fullPercent`)
- Stale for ≥ 1 hour (configurable: `-quietFor`)
- Not already erasure coded

**Process:**
1. Identify eligible volumes
2. Split volume into 1GB chunks (or 1MB for small volumes)
3. Every 10 data chunks → encode into 4 parity chunks
4. Create 14 EC shards total
5. Distribute shards across disks/servers/racks
6. Delete original volume if replicated
7. Automatically trigger shard rebalancing

**Collection-Specific Encoding:**
```bash
ec.encode -collection="collection_name"
```

**Decoding/Reconstruction:**

**Command:** `ec.rebuild -force`

**Process:**
1. Identifies volumes with missing shards
2. Collects available shards (need ≥10 of 14)
3. Reconstructs entire volume (not file-by-file)
4. Much more efficient than granular file recovery
5. Rebuilds missing shards from available data+parity

**Manual Decoding:**
```bash
ec.decode -volumeId XX  # Decode EC volume back to normal
volume.vacuum           # Then compact if needed
```

**Shard Distribution Strategy:**

**Command:** `ec.balance -force`

**Three-step balancing:**
1. **Duplicate elimination:** Remove redundant shard copies
2. **Rack-level balancing:** Distribute across racks
3. **Intra-rack balancing:** Balance within racks
4. Respects replica placement rules

**Fault Tolerance Strategy:**
- Spread shards across physical disks
- Rack-aware placement
- Can handle loss of up to 4/14 pieces
- **Anti-pattern:** Having > 4 pieces on single disk defeats purpose

### 7.3 Read Performance with EC Volumes

**Performance Characteristics:**

**Benchmark Results:**

| Configuration | Requests/Second | Performance |
|--------------|-----------------|-------------|
| Standard volumes | ~31,435 | Baseline (100%) |
| EC volumes (all shards online) | ~13,966 | ~44% (half speed) |
| EC with missing shards | ~9,152 | ~29% |

**Normal EC Reads (All Shards Available):**
- Approximately **half throughput** of standard volumes
- Due to additional network hop
- Most small files in 1 shard (or 2 in edge cases)
- **Still O(1) disk read** for most files

**Degraded EC Reads (Missing Shards):**
- ~9,152 requests/second (~65% of normal EC)
- Reconstruction required
- Clients collect pieces from remaining servers
- Introduces reconstruction latency

**Optimization for Small Files:**
- 1GB chunk size ensures most small files in 1 shard
- Edge case: File might span 2 shards
- **Still better than traditional distributed FS**

**Large File Performance:**
- Files split across multiple 1GB chunks
- Each chunk independently sharded
- Parallel reads from different shards possible
- Reconstruction overhead distributed

**Trade-offs:**

| Aspect | Standard Volumes | EC Volumes |
|--------|-----------------|------------|
| Storage Cost | 5x (for 001 replication) | 1.4x |
| Disk Savings | Baseline | 3.6x |
| Read Speed | Fastest | ~50% |
| Write Speed | Fast | Slower (encoding) |
| Fault Tolerance | Lose 1 replica | Lose 4 of 14 shards |
| Use Case | Hot data | Warm/cold data |

### 7.4 Limitations & Special Considerations

**Compaction Limitations:**

**Volume.vacuum doesn't support EC shards:**
- EC shards simply ignored during vacuum
- Cannot reclaim space from deleted files in EC volumes
- **Workaround:**
  1. `ec.decode -volumeId XX` - convert back to normal
  2. `volume.vacuum` - compact volume
  3. `ec.encode` - re-encode to EC if desired

**Update Limitations:**
- Deletion supported on EC volumes
- **Updates NOT supported**
- Must decode → update → re-encode

**Unrepairable EC Volumes:**
- If too many shards lost (> 4 of 14)
- Volume becomes unrepairable
- **Cleanup:**
  ```bash
  # Manually remove from configuration
  # Delete shard files from disk
  ```

**Disk Space Issues:**
- Encoding doubles space temporarily (original + shards)
- **Issue #6163:** Failed encoding doesn't clean up
- Leaves disk full
- Must manually clean up failed encoding attempts

**Use Case Recommendations:**

**Good for EC:**
- Warm storage (accessed less often)
- Cold storage (archival)
- Large volumes (> 10GB)
- Cost-sensitive deployments
- Read-mostly workloads

**Bad for EC:**
- Hot data (frequently accessed)
- Write-heavy workloads
- Small volumes (< 10GB overhead)
- Latency-sensitive applications
- Frequently updated files

**Disaster Recovery:**
- `ec.rebuild -force` for shard reconstruction
- Requires ≥10 of 14 shards available
- Processes entire volumes (efficient)
- Cannot rebuild with > 4 shards lost

**Enterprise Self-Healing:**
- Automatic corruption detection
- Removes corrupted entries
- Maintains consistency after crashes
- Reduces manual intervention

---

## 8. Key Learnings for Nexus

### 8.1 Architecture Patterns to Consider

**1. Haystack-Inspired Metadata Distribution:**

**Current Nexus:** Centralized metadata in metadata service

**SeaweedFS Approach:**
- Master manages only volume metadata (lightweight)
- Volume servers manage file metadata locally
- **Benefit:** Relieves concurrency pressure on master
- **Benefit:** File metadata spread across storage nodes

**Potential for Nexus:**
- Consider distributing block metadata to storage nodes
- Central coordinator manages only block allocation
- Storage nodes maintain local block index
- Reduces metadata service bottleneck

**2. O(1) Disk Read via In-Memory Index:**

**SeaweedFS:** 16 bytes per file in memory → O(1) lookup

**Nexus Consideration:**
- Current block metadata: likely larger per entry
- Could optimize: `<block_id, offset, size>` minimal index
- Trade memory for lookup speed
- Critical for high-IOPS workloads

**3. Static Assignment Strategy:**

**SeaweedFS:** Volume IDs never change once assigned

**Nexus Parallel:**
- Block IDs could be immutable references
- Enables aggressive caching
- Simplifies cache invalidation
- Reduces coordination overhead

**4. Collection-Based Organization:**

**SeaweedFS Collections:**
- Group of volumes with shared TTL/replication
- Enables policy-based storage tiers
- Auto-creates volumes as needed

**Nexus Application:**
- Collections for different data classes
- Per-collection performance profiles
- Simplifies storage tiering
- Automated capacity management

### 8.2 Performance Optimizations Applicable to Nexus

**1. CompactMap Memory Efficiency (95% reduction):**

**Technique:**
- Assume mostly increasing IDs
- Specialized data structure
- 16 bytes per entry (vs much larger)

**Nexus Application:**
- Block IDs likely monotonic within containers
- Could use similar compact representation
- Massive memory savings for billions of blocks
- Enables larger working sets in memory

**2. LevelDB-Based Index Offloading:**

**SeaweedFS Pattern:**
- Hot data: in-memory index
- Warm/cold data: LevelDB index
- 95%+ memory reduction for warm data

**Nexus Opportunity:**
- Block metadata for inactive containers → LevelDB
- Active container metadata → memory
- Tiered index strategy based on access patterns
- Faster startup (don't load all metadata)

**3. Index Location Optimization:**

**SeaweedFS:** `weed volume -dir.idx=/fast/disk/dir`

**Concept:** Index on fast SSD, data on slow HDD

**Nexus Application:**
- Block index on NVMe
- Block data on cheaper SATA SSDs or HDD
- Metadata operations fast regardless of data tier
- Cost optimization without sacrificing performance

**4. Append-Only Write Pattern:**

**SeaweedFS Benefits:**
- SSD-friendly (no fragmentation)
- Deletion/compaction in background
- Doesn't slow reads
- Sequential writes optimize throughput

**Nexus Consideration:**
- Already uses append-only for many operations
- Could apply more consistently
- Background compaction for deleted blocks
- Optimize write amplification

**5. Concurrent Writable Volumes:**

**SeaweedFS:** 7+ concurrent writable volumes by default

**Why:**
- Distributes write load
- Reduces lock contention
- Parallel I/O across volumes

**Nexus Application:**
- Multiple concurrent writable segments per container
- Parallel writes to different segments
- Reduces hot-spot contention
- Better multi-threaded write performance

**6. Volume Preallocation:**

**SeaweedFS:** `-volumePreallocate` for contiguous blocks

**Nexus Parallel:**
- Preallocate segment files on XFS/ext4
- Ensures contiguous disk layout
- Reduces fragmentation
- Better large block read/write performance

**7. Read Buffer Sizing:**

**SeaweedFS:** `volume.readBufferSizeMB` reduces lock contention

**Nexus:**
- Tune read buffer sizes for workload
- Larger buffers for unstable networks
- Reduces lock contention during slow reads
- Balance memory vs concurrency

### 8.3 Metadata Store Insights

**1. Filer Store Diversity:**

**SeaweedFS supports 15+ backends:**
- SQL: PostgreSQL, MySQL, Sqlite
- NoSQL: MongoDB, Cassandra, Redis
- Distributed: TiDB, CockroachDB, YDB
- LSM: LevelDB, RocksDB
- Search: Elasticsearch
- K/V: Etcd

**Performance profiles vary wildly:**
- O(1): Memory, Redis, Redis3
- O(log N): LevelDB, SQL databases
- Distributed: Unlimited scalability

**Nexus Lesson:**
- Don't lock into single metadata backend
- Different workloads need different stores
- Pluggable metadata store architecture
- Let users choose based on their needs

**2. Super Large Directory Pattern:**

**Challenge:** Billions of entries in single directory

**SeaweedFS Solution:**
- Partition by full path (not directory hash)
- Spread entries across all cluster nodes
- Sacrifice listing for scalability

**Nexus Application:**
- Large container with millions of blocks
- Partition block metadata by block_id prefix
- Spread across metadata service instances
- Sacrifice "list all blocks" for scale

**3. Directory Hash Optimization (PostgreSQL):**

**Pattern:** `dirhash BIGINT` to prevent directory locks

**Nexus Parallel:**
- Hash container IDs to prevent hot partitions
- Distribute metadata shards by hash
- Reduces lock contention
- Enables parallel queries

**4. Metadata Cache TTL:**

**SeaweedFS:** 60-second default TTL

**Nexus Consideration:**
- Tune TTL based on workload
- Longer TTL: better cache hit, stale risk
- Shorter TTL: fresh data, more load
- Per-container TTL policies?

### 8.4 Replication & Consistency Patterns

**1. W=N, R=1 Strong Consistency:**

**SeaweedFS Choice:**
- All replicas must succeed for write
- Any replica can serve read
- Strong consistency at write time
- Higher write latency

**Nexus Consideration:**
- Currently uses quorum writes (W=majority)
- Could offer W=N mode for critical data
- Trade latency for consistency
- Per-container consistency level?

**2. No Auto-Repair Philosophy:**

**SeaweedFS Rationale:**
- Prevents over-replication from transient failures
- Manual repair via periodic scripts
- Operator controls repair timing

**Nexus Lesson:**
- Auto-repair sounds good but has pitfalls
- Transient network issues trigger unnecessary repairs
- Waste bandwidth/storage
- Manual or scheduled repair gives control

**3. Read-Only State for Degraded Volumes:**

**Pattern:**
- Missing replicas → volume becomes read-only
- Prevents write amplification
- New writes go to healthy volumes

**Nexus Application:**
- Degraded containers → read-only mode
- Prevent write amplification during recovery
- Force new writes to healthy containers
- Cleaner failure handling

**4. Synchronous Replication with Timeout:**

**SeaweedFS Approach:**
- Primary waits for all replicas
- But with reasonable timeout
- Fail fast on slow replicas

**Nexus Optimization:**
- Current async replication has lag
- Could offer sync mode for critical writes
- With configurable timeout
- Best of both worlds

### 8.5 Erasure Coding Considerations

**1. When to Use EC:**

**SeaweedFS Pattern:**
- Hot data: Replication (fast reads)
- Warm/cold data: EC (storage savings)
- Seal volumes before encoding

**Nexus Application:**
- Container lifecycle stages:
  - Active: Replication (3x)
  - Warm: EC (1.4x) after 30 days inactive
  - Cold: EC with fewer data shards after 90 days
- Automatic tier transition based on access patterns

**2. Chunk Size for EC:**

**SeaweedFS:** 1GB chunks ensure small files in 1 shard

**Nexus Insight:**
- Block size vs shard size critical
- Chunk size should be >> typical block size
- Minimize multi-shard reads for common operations
- Balance reconstruction parallelism vs overhead

**3. Compaction Before EC:**

**SeaweedFS Lesson:**
- Compact volumes before EC encoding
- EC shards can't be vacuumed
- Wasted space amplified by EC

**Nexus Pattern:**
- Compact container before EC conversion
- Remove deleted blocks first
- Reduces EC storage overhead
- Optimize before committing to EC

**4. Gradual Degradation:**

**SeaweedFS Performance:**
- All shards: ~50% normal speed
- Missing shards: ~29% normal speed

**Nexus Expectation:**
- Plan for degraded performance with EC
- Not suitable for hot data
- Monitor shard health
- Rebuild proactively before too many lost

### 8.6 Caching Strategy Insights

**1. Multi-Layer Cache Hierarchy:**

**SeaweedFS Layers:**
- Volume ID location cache (persistent connection)
- In-memory needle index (file metadata)
- FUSE mount metadata cache (directory entries)
- FUSE mount data cache (file chunks)

**Nexus Parallel:**
- Block location cache (which storage nodes)
- Block metadata cache (size, checksum, etc.)
- Container metadata cache (block lists)
- Data block cache (actual content)

**2. Asynchronous Metadata Replication:**

**SeaweedFS FUSE Mount:**
- Replicate metadata to local DB asynchronously
- Zero remote metadata reads after sync
- Local directory listings

**Nexus Application:**
- Client-side metadata cache with async sync
- Reduce metadata service load
- Faster local operations
- Eventual consistency acceptable for reads

**3. Cache Coherency Challenges:**

**SeaweedFS Issue:**
- TTL-based invalidation incomplete
- Filer metadata outlives volume data
- Stale metadata persists

**Nexus Lesson:**
- TTL alone insufficient for consistency
- Need active invalidation mechanism
- Version numbers or generation counters
- Explicit cache invalidation on updates

**4. Persistent Client Connections:**

**SeaweedFS Pattern:**
- weed mount maintains persistent Master connection
- Receives volume location updates in real-time
- Zero lookup latency for volume locations

**Nexus Opportunity:**
- Persistent connections to metadata service
- Push-based metadata updates
- Eliminates lookup latency
- Reduces metadata service load

### 8.7 Operational Patterns

**1. Topology-Aware Placement:**

**SeaweedFS Hierarchy:**
- DataCenter → Rack → Node → Disk
- Replication respects topology
- Example: `100` = different datacenter

**Nexus Application:**
- Current placement somewhat topology-aware
- Could formalize: Region → AZ → Node → Disk
- Replication policy based on topology
- User-configurable placement rules

**2. Collection-Based Multi-Tenancy:**

**SeaweedFS Collections:**
- Logical grouping with shared policies
- Isolation between collections
- Per-collection TTL, replication, disk type

**Nexus Multi-Tenancy:**
- Collections map to tenants or workload types
- Per-tenant performance profiles
- Resource isolation
- QoS per collection

**3. Volume Balancing:**

**SeaweedFS Pattern:**
- `volume.balance` distributes volumes evenly
- Considers capacity, replica placement
- Can filter by DC, rack, collection

**Nexus Need:**
- Container balancing across storage nodes
- Consider capacity, network, I/O load
- Automated rebalancing on node addition
- Minimize data movement

**4. fsck & Repair Tools:**

**SeaweedFS Tools:**
- `volume.fsck` - find orphaned/missing chunks
- `volume.check.disk` - verify replica consistency
- `volume.fix.replication` - repair missing replicas
- `ec.rebuild` - reconstruct EC shards

**Nexus Requirement:**
- Similar tooling for block consistency
- Orphan block detection
- Replica consistency verification
- Automated or scheduled repair
- Clear operator controls

### 8.8 Specific Implementation Ideas

**1. Hybrid Index Strategy:**

```python
# Pseudo-code for Nexus block metadata
class BlockIndex:
    def __init__(self, container_id):
        self.container_id = container_id
        self.hot_index = {}  # In-memory: recent blocks
        self.warm_index = LevelDB(f"/ssd/index/{container_id}")
        self.access_tracker = LRU(max_size=10000)

    def get_block_metadata(self, block_id):
        # Try hot index first (O(1) memory)
        if block_id in self.hot_index:
            self.access_tracker.touch(block_id)
            return self.hot_index[block_id]

        # Fall back to warm index (O(log N) LevelDB)
        metadata = self.warm_index.get(block_id)

        # Promote to hot if frequently accessed
        if self.access_tracker.should_promote(block_id):
            self.hot_index[block_id] = metadata

        return metadata
```

**2. CompactMap-Inspired Block Index:**

```python
# Minimal memory footprint for block metadata
class CompactBlockEntry:
    __slots__ = ['block_id', 'offset', 'size', 'checksum']

    def __init__(self, block_id: int, offset: int, size: int, checksum: int):
        self.block_id = block_id      # 8 bytes
        self.offset = offset >> 3     # 4 bytes (8-byte aligned, 32GB range)
        self.size = size              # 4 bytes
        self.checksum = checksum      # 4 bytes

    # Total: 20 bytes per block vs current ~100+ bytes
    # 100M blocks: 2GB vs 10GB+ memory savings
```

**3. Topology-Aware Placement:**

```python
class ReplicationPolicy:
    def __init__(self, policy_string: str):
        # Parse "XYZ" format like SeaweedFS
        # X = regions, Y = zones, Z = nodes
        self.regions, self.zones, self.nodes = map(int, policy_string)

    def select_replicas(self, topology, primary_node):
        replicas = [primary_node]

        # Ensure replicas spread across regions/zones/nodes
        for _ in range(self.regions):
            replicas.append(topology.select_node(
                exclude_region=primary_node.region
            ))

        for _ in range(self.zones):
            replicas.append(topology.select_node(
                same_region=primary_node.region,
                exclude_zone=primary_node.zone
            ))

        for _ in range(self.nodes):
            replicas.append(topology.select_node(
                same_region=primary_node.region,
                same_zone=primary_node.zone,
                exclude_node=primary_node
            ))

        return replicas
```

**4. Lazy Vacuum/Compaction:**

```python
class ContainerCompaction:
    def should_compact(self, container):
        # SeaweedFS default: 30% garbage
        deleted_ratio = container.deleted_size / container.total_size
        return deleted_ratio > 0.30

    def compact(self, container):
        # Make read-only
        container.set_read_only()

        # Create new container
        new_container = Container.create()

        # Copy only non-deleted blocks
        for block in container.iterate_blocks():
            if not block.is_deleted():
                new_container.append(block)

        # Atomic switch
        container.replace_with(new_container)

        # Delete old
        container.delete()
```

**5. EC Tiering Strategy:**

```python
class StorageTiering:
    def tier_container(self, container):
        age_days = (now() - container.last_access).days

        if age_days < 7:
            # Hot: 3-way replication
            container.ensure_replication(3)

        elif age_days < 30:
            # Warm: 2-way replication
            container.compact()  # Clean up first
            container.ensure_replication(2)

        elif age_days < 90:
            # Cool: EC (10+4)
            container.compact()
            container.encode_erasure_coding(
                data_shards=10, parity_shards=4
            )

        else:
            # Cold: EC (6+3) - less redundancy, more savings
            container.encode_erasure_coding(
                data_shards=6, parity_shards=3
            )
```

---

## Sources & References

### Primary Sources

- [SeaweedFS GitHub Repository](https://github.com/seaweedfs/seaweedfs)
- [SeaweedFS Wiki - Components](https://github.com/seaweedfs/seaweedfs/wiki/Components)
- [SeaweedFS Wiki - Optimization](https://github.com/seaweedfs/seaweedfs/wiki/Optimization)
- [SeaweedFS Wiki - Replication](https://github.com/seaweedfs/seaweedfs/wiki/Replication)
- [SeaweedFS Wiki - Data Structure for Large Files](https://github.com/seaweedfs/seaweedfs/wiki/Data-Structure-for-Large-Files)
- [SeaweedFS Wiki - Super Large Directories](https://github.com/seaweedfs/seaweedfs/wiki/Super-Large-Directories)
- [SeaweedFS Wiki - Erasure Coding for Warm Storage](https://github.com/seaweedfs/seaweedfs/wiki/Erasure-coding-for-warm-storage)
- [SeaweedFS Wiki - FUSE Mount](https://github.com/seaweedfs/seaweedfs/wiki/FUSE-Mount)
- [SeaweedFS Wiki - Filer Stores](https://github.com/seaweedfs/seaweedfs/wiki/Filer-Stores)
- [SeaweedFS Wiki - Failover Master Server](https://github.com/seaweedfs/seaweedfs/wiki/Failover-Master-Server)

### Technical Deep Dives

- [DeepWiki - SeaweedFS Documentation](https://deepwiki.com/seaweedfs/seaweedfs)
- [JuiceFS vs SeaweedFS Comparison](https://juicefs.com/en/blog/engineering/similarities-and-differences-between-seaweedfs-and-juicefs-structures)
- [Seaweedfs Distributed Storage Part 1: Introduction](https://medium.com/@ahsifer/seaweedfs-25640728775c)
- [Seaweedfs Distributed Storage Part 3: Features](https://medium.com/@ahsifer/seaweedfs-distributed-storage-part-3-features-b720b00479ca)
- [Boost Your File Storage with SeaweedFS & PostgreSQL](https://medium.com/@Monem_Benjeddou/boost-your-file-storage-with-seaweedfs-postgresql-a-step-by-step-setup-23c890a50327)

### Source Code References

- [weed/storage/needle Package](https://pkg.go.dev/github.com/chrislusf/seaweedfs/weed/storage/needle)
- [weed/storage/needle_map Package](https://pkg.go.dev/github.com/chrislusf/seaweedfs/weed/storage/needle_map)
- [weed/topology Package](https://pkg.go.dev/github.com/chrislusf/seaweedfs/weed/topology)
- [weed/storage/store_vacuum.go](https://github.com/seaweedfs/seaweedfs/blob/master/weed/storage/store_vacuum.go)
- [weed/server/raft_server.go](https://github.com/seaweedfs/seaweedfs/blob/master/weed/server/raft_server.go)
- [weed/server/raft_hashicorp.go](https://github.com/seaweedfs/seaweedfs/blob/master/weed/server/raft_hashicorp.go)

### Discussions & Issues

- [GitHub Discussions - Various topics](https://github.com/seaweedfs/seaweedfs/discussions)
- [GitHub Issues - Implementation details](https://github.com/seaweedfs/seaweedfs/issues)
- [Google Groups - SeaweedFS](https://groups.google.com/g/seaweedfs)

### Academic Papers (Referenced)

- Facebook Haystack: Finding a Needle in Haystack (SeaweedFS design inspiration)
- Facebook f4: Warm BLOB Storage System (Erasure coding implementation)
- Facebook Tectonic Filesystem (Additional similarities)

---

## Conclusion

This deep technical dive reveals SeaweedFS as a highly optimized distributed storage system with several innovative patterns:

**Key Technical Achievements:**
1. **O(1) disk reads** through in-memory needle indexing
2. **95% memory reduction** with CompactMap optimization
3. **40 bytes disk overhead** per file vs 536 bytes for XFS
4. **3.6x storage savings** with EC vs replication
5. **Strong consistency** with W=N, R=1 pattern
6. **Topology-aware** replication for fault tolerance

**Most Valuable Patterns for Nexus:**
1. Distributed metadata (volume servers manage own file metadata)
2. Hybrid index strategy (memory for hot, LevelDB for warm)
3. Append-only writes with background compaction
4. Collection-based organization with policy inheritance
5. Super large directory partitioning for scalability
6. Erasure coding for warm/cold data with automatic tiering

**Implementation Priorities:**
1. **Short-term:** CompactMap-inspired block index (95% memory savings)
2. **Medium-term:** Hybrid index with LevelDB offloading
3. **Long-term:** Erasure coding for warm data, topology-aware placement

The research demonstrates that many of SeaweedFS's optimizations are directly applicable to Nexus's architecture, particularly around metadata efficiency, caching strategies, and storage tiering.
