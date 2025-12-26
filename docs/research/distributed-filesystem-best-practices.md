# Distributed Filesystem Best Practices: Production Systems Research

**Date**: 2025-12-26
**Purpose**: Critical research on distributed filesystem patterns from production systems applicable to Nexus

---

## Table of Contents

1. [Facebook/Meta Systems](#facebookmeta-systems)
2. [Google Systems](#google-systems)
3. [Netflix/Dropbox/Uber Systems](#netflixdropboxuber-systems)
4. [Open Source Production Systems](#open-source-production-systems)
5. [Academic Research Findings](#academic-research-findings)
6. [Key Patterns & Optimizations](#key-patterns--optimizations)
7. [Performance Benchmarks & Numbers](#performance-benchmarks--numbers)
8. [Recommendations for Nexus](#recommendations-for-nexus)

---

## Facebook/Meta Systems

### 1. Haystack: Photo Storage System (OSDI 2010)

**Scale**: 260 billion images, 20+ petabytes, 1 billion new photos/week (60TB), 1M+ images/second at peak

**Key Problem Solved**: Traditional NAS/NFS systems had excessive disk operations for metadata lookups. Disk IOs for metadata was the limiting factor for read throughput.

**Architecture**:
- **Haystack Store**: Manages filesystem-level metadata for photos
- **Haystack Directory**: Maintains logical-to-physical volume mapping
- **Haystack Cache**: Internal CDN to avoid unnecessary store calls (80% hit rate for recent uploads)

**Critical Innovation - Metadata Optimization**:
- Aggregates hundreds of thousands of images in a single haystack store file
- Eliminates per-file metadata overhead
- Stores each needle's location in an **in-memory index**
- Physical volumes are ~100GB each (like segments)
- Each photo referenced as **offset + length** within a file
- **Single random seek** per photo read when metadata is in memory

**Performance Results**:
- **4x more reads per second** vs previous solution
- **28% cheaper** per terabyte
- Metadata kept in memory allows minimal I/O operations

**Nexus Applicability**:
- Store file content as (offset, length) in large segment files
- Keep all metadata index in memory for fast lookups
- Segment files should be 100GB+ for efficiency
- Aggregate many small files into larger storage units

### 2. f4: Warm BLOB Storage (OSDI 2014)

**Scale**: 65+ petabytes of logical BLOBs

**Key Observation**: BLOBs with low request rates don't need triple replication - it's overkill.

**Storage Efficiency Strategy**:
- **Hot storage (Haystack)**: Triple replication = 3.6x storage overhead
- **Warm storage (f4)**: Erasure coding = 2.8x or 2.1x effective replication factor
- **Storage reduction**: 42% less storage for warm data

**Erasure Coding Implementation**:
- Within datacenter: **Reed-Solomon (10,4)** - 10 data blocks + 4 parity blocks
- Across datacenters: **XOR coding** - blocks paired across DCs
- Careful block layout ensures resilience to disk, host, rack failures

**Performance Tradeoffs**:
- Latency increase: 14ms → 17ms (acceptable for warm data)
- Recovery time increased but storage cost significantly reduced
- Migration: Photos move to f4 after 3 months, other content after 1 month

**Migration Pattern**:
- Week-old BLOBs have **10x lower** request rate than day-old content
- Request rate determines hot vs warm classification

**Nexus Applicability**:
- Implement tiered storage based on access patterns
- Use erasure coding for infrequently accessed data
- Monitor access patterns to trigger migration
- Accept small latency increase for significant storage savings

### 3. Tectonic: Exabyte-Scale Filesystem (FAST 2021)

**Scale**: Multiple exabyte-scale clusters, serving entire datacenters

**Key Achievement**: Unified 10x specialized storage systems into single general-purpose system

**Architecture**:
- **Chunk Store**: Flat, distributed object store for chunks (unit of data storage)
- **Metadata Store**: Filesystem hierarchy stored in ZippyDB (key-value store)
- **Metadata Layers**: Name, file, and block layers (each hash-partitioned)
- **Stateless metadata services**: Reconstruct from ZippyDB on demand

**Resource Management**:
- **Non-ephemeral resources**: Storage capacity (provisioned and fixed)
- **Ephemeral resources**: IOPS (can move between tenants dynamically)
- **Traffic Classes**: Gold (latency-sensitive), Silver (normal), Bronze (background)

**Multitenancy Benefits**:
- More efficient resource usage through consolidation
- Simplified operations: 10x fewer clusters
- "Unstranded" resources through sharing

**Metadata Design**:
- Three-layer architecture: Names → Files → Blocks → Chunks
- Hash partitioning for horizontal scalability
- Stateless services enable fast recovery

**Nexus Applicability**:
- Separate metadata into logical layers
- Use hash partitioning for metadata scalability
- Implement traffic classes for QoS
- Design stateless services where possible for easier scaling

---

## Google Systems

### 1. Colossus: GFS Successor (2010+)

**Scale**:
- Multiple exabytes per filesystem (two filesystems with 10+ exabytes each)
- **50+ TB/s read throughput** in largest filesystems
- **25+ TB/s write throughput**
- **600M+ IOPS** in busiest single cluster
- "Enough to send 100+ full-length 8K movies every second"

**Why Built**: GFS single-master architecture hit scaling limits:
- Metadata size exceeded available RAM
- CPU capacity insufficient for concurrent client operations
- Long recovery times from master failures
- **100x scalability improvement** over largest GFS clusters

**Architecture**:
- **Distributed metadata** (vs GFS single master - removed single point of failure)
- **Curators**: Control plane, replaces master functionality
- **Custodians**: Background storage managers for durability, availability, efficiency
- **D File Servers**: Network-attached disks, direct client connections
- **Metadata Database**: BigTable stores all metadata

**Key Innovation**: Direct data flow between clients and D file servers - minimizes network hops

**Nexus Applicability**:
- Never use single-master architecture
- Distribute metadata across multiple servers
- Store metadata in distributed database (not in-memory only)
- Minimize network hops in data path
- Background processes for maintenance tasks

### 2. BigTable: Distributed Storage for Structured Data (OSDI 2006)

**Scale**: Petabytes across thousands of commodity servers

**Metadata Architecture - Three-Level Hierarchy**:

```
Level 1: Chubby file → Root tablet location
Level 2: Root tablet → METADATA table locations
Level 3: METADATA tablets → User tablet locations
```

**Critical Design**: Root tablet never splits (ensures max 3 levels)

**Scalability Math**:
- METADATA record: ~1KB
- METADATA tablet limit: 128MB
- Capacity: **~16 billion tablet locations**

**Client Optimization**:
- Caches tablet locations
- On stale cache: Move up only as many levels as needed (not always all 3)

**Dependencies**:
- **Chubby**: Lock service for distributed coordination
- **GFS/Colossus**: Stores data and logs (SSTables format)
- **Fault tolerance**: Fast recovery - only metadata migrates to replacement node

**Automatic Management**:
- Splits busy/large tablets automatically
- Merges less-accessed/smaller tablets
- Redistributes between nodes for load balancing

**Nexus Applicability**:
- Use hierarchical metadata lookup (2-3 levels max)
- Cache metadata aggressively on clients
- Implement auto-splitting/merging for load balancing
- Design for metadata-only migration on failures
- Root-level metadata must never be split

### 3. Spanner: Globally-Distributed Database

**Key Features**: First system with global-scale data + externally-consistent distributed transactions

**Storage Architecture**:
- **Colossus as backing store**: All data stored in distributed filesystem
- **Decoupled compute/storage**: Scale independently
- **Tablets**: Sharded tables stored on Colossus
- **Dynamic sharding**: Splits auto-adjust based on workload/size

**"Shared Nothing" Architecture**:
- High scalability through partitioning
- But any server can read distributed filesystem for fast recovery

**Replication & Consensus**:
- **Paxos algorithm** for sharding across hundreds of servers
- **TrueTime**: GPS + atomic clocks for global consistency
- Each split has Paxos group spanning multiple zones
- Leader handles all writes for its split

**Transaction Durability**: Writes commit to majority of replicas before acknowledging

**Storage Options**:
- **SSD**: Low latency + high throughput for operational data
- **HDD**: Less frequent access, higher latency acceptable
- **Tiering policies**: Auto-move SSD → HDD after time window

**Nexus Applicability**:
- Separate storage from compute for independent scaling
- Use consensus (Raft/Paxos) for critical metadata
- Implement automatic sharding based on load
- Consider storage tiering for cost optimization

---

## Netflix/Dropbox/Uber Systems

### 1. Dropbox Magic Pocket

**Scale**: Multi-exabyte storage, millions of queries/second, handles hundreds of hardware failures/day

**Durability & Availability**:
- **Annual durability**: 99.9999999999% (12 nines)
- **Availability**: 99.99%+
- Encrypted at rest

**Growth Achievement**: Scaled from "double-digit petabytes" → "multi-exabyte" in **~6 months**

**Architecture**:
- **Multi-zone**: Western, central, eastern US
- **Blocks**: Up to 4MB each, compressed and encrypted
- **Replication**: Each block in at least 2 separate zones, then replicated within zones
- **Eventual erasure coding**: Recent blocks replicated, older blocks erasure coded

**Hardware Scale**:
- **100+ drives** per storage device
- **1.5-2 PB raw data** per single storage host
- Multi-petabyte capacity per device

**Design Philosophy**:
- **Eschews quorum-style consensus** when possible
- **Centralized coordination** when fault-tolerant and scalable
- **Giant sharded MySQL cluster** for Block Index (instead of distributed hash table)
- Simplified development, minimized unknowns

**Cost Savings**: **$75M** reduction in operating costs (per SEC filing)

**SMR Technology**: First major tech company to adopt SMR drives at scale (hundreds of petabytes)

**Nexus Applicability**:
- Don't over-engineer with complex distributed algorithms
- Sharded MySQL can work at massive scale
- Consider centralized coordination with fault tolerance
- Hardware investment in high-density storage
- Start with replication, move to erasure coding over time

### 2. Netflix Open Connect CDN

**Architecture**: Control plane (AWS) + Data plane (Open Connect CDN)

**Two-Tier Storage**:
- **Storage Appliances**: At internet exchange points, nearly full Netflix catalog
- **Edge Appliances**: Within ISP networks, cache regionally popular content

**Performance**:
- **98% cache hit rate** at edge
- Origin egress only 2% of edge traffic
- **73 Tbps edge** → **~1.46 Tbps origin**

**Open Connect Appliances (OCAs)**:
- Heavy RAM and storage
- Advanced caching algorithms predict and retain popular titles
- Hyper-optimized for long-term caching efficiency
- Direct placement inside/near ISPs

**Backend Storage**: Amazon S3 for media storage

**Viewing History Optimization**:
- **Live Viewing History (LiveVH)**: Recent records, uncompressed, frequent updates
- **Compressed Viewing History (CompressedVH)**: Old records, compressed, rare updates
- Goal: Smaller storage footprint + consistent read/write performance

**Content Delivery**:
- Control plane steers clients to optimal OCAs based on file availability, health, network proximity
- Nightly fill operations add new files to OCAs

**Nexus Applicability**:
- Tiered caching: hot data in fast tier, warm data in slower tier
- Separate recent (mutable) from historical (immutable) data
- Compress old/cold data aggressively
- Locality-aware routing for optimal performance

### 3. Uber Storage Systems

**Scale**: Tens of petabytes, tens of millions of requests/second

**Docstore**: Uber's distributed database on MySQL
- **Three layers**: Stateless query engine, stateful storage engine, control plane
- **Query engine**: Planning, routing, sharding, schema, health monitoring, AuthN/AuthZ
- **Storage engine**: Raft consensus, replication, transactions, concurrency control

**Schemaless**: Key-value store
- Append-only sharded MySQL
- Buffered writes for failing masters
- Publish-subscribe for data change notifications
- Global indexes over data
- Saves any JSON without strict schema validation

**CacheFront + Redis**:
- **99.9% cache hit rate**
- **40M+ requests/second**
- Adaptive timeouts, negative caching, cache warming
- Eases load on storage engine

**Big Data**: HDFS foundation
- Stores Kafka streams
- Converts to Parquet for long-term storage
- Trillions of messages, petabytes daily

**RingPop**: Distributed coordination
- Consistent hash ring on membership protocol
- Request forwarding/routing
- Auto-distributes load when servers added/removed

**Marketplace Storage Gateway (MSG)**:
- Abstracts underlying storage (Cassandra)
- Redundant clusters: 1 write → 2 Cassandra cluster writes
- 3 replicas per cluster
- Cross-region asynchronous replication

**Location Data**: Google S2 library
- Divides map into cells with unique IDs
- Easy distribution in distributed systems

**Nexus Applicability**:
- Extremely high cache hit rates (99%+) are achievable
- Stateless services simplify scaling
- Append-only designs handle failures gracefully
- Consistent hashing for load distribution
- Redundant storage clusters for high availability

---

## Open Source Production Systems

### 1. MinIO Performance Tuning

**Minimum Production Setup**: 4+ servers for erasure coding and fault tolerance

**Kernel Tuning**:
- `transparent_hugepage=madvise` (not `always`)
- CPU governor: `performance`
- `vm.swappiness=0` (no swap usage)
- `vm.dirty_background_ratio=3` (start writeback at 3% memory)
- `vm.dirty_ratio=10` (force writeback at 10% memory)

**Hardware Requirements**:
- **Minimum 8 CPU cores** (diagnostics warn if less)
- CPU make/model/count must match across nodes (avoid bottlenecks)
- Drives must be consistent (no mixing SSD + HDD - causes random bottlenecks)

**Filesystem**: **XFS required**, not EXT4

**What to Avoid**:
- RAID (introduces extra durability overhead)
- LVM
- ZFS
- NFS
- GlusterFS
- Older filesystems (ext4)

**Performance Test**: Built-in distributed performance assessment
- Tests PUTs, then GETs
- Aggregated throughput results
- Track performance over time
- Proactive problem identification

**Nexus Applicability**:
- Require minimum node count for meaningful erasure coding
- Hardware homogeneity critical for consistent performance
- Kernel tuning for dirty page writeback
- Simple filesystem stack (no layering)
- Built-in performance monitoring

### 2. Ceph Performance Optimization

**Hardware Selection**:
- **Enterprise SSDs or NVMe** for storage
- Sufficient RAM for BlueStore operations
- **10 Gbps minimum networking** (40/50/100 Gbps common in 2022)
- **40 Gb/s or 25/50/100 Gb/s** networking for production clusters

**Network Optimization**:
- Separate public and cluster traffic
- Enable jumbo frames
- Link aggregation for high throughput

**Placement Groups (PGs)**:
- Formula: `PGs = round2((Total_OSD * 100) / max_replication_count)`
- Enable **PG Autoscaler** to auto-adjust as data grows

**Configuration Optimization**:
- Fine-tune RBD cache policies
- Adjust BlueStore memory allocation
- Thread management for workload
- CGroup pin each OSD to CPU core/socket (avoid NUMA issues)
- Scrubbing severely impacts performance - configure carefully

**HDD OSD Optimization**: Offload WAL+DB to SSD for significant write latency improvement

**MDS Bottlenecks**: Deploy multiple MDSs per host
- One case needed 20 total MDSs (4 per host, 16 active)

**Real-World Results**:
- Relocating RGW daemons to OSD hosts (collocation): **7x PUT throughput improvement**

**Nexus Applicability**:
- SSD/NVMe for metadata and write-ahead logs
- Network isolation for data traffic
- Auto-scaling algorithms for partition management
- NUMA awareness in process pinning
- Background operations (scrubbing) must be rate-limited

### 3. GlusterFS Scalability

**Client-to-Server Ratio**:
- Optimal: **12:1 to 48:1** for most workloads
- Example: 8 servers → 32 clients
- Horizontal scale-out doesn't help single/few clients

**Network Performance**:
- **10GbE or faster** for data traffic as nodes increase
- **Jumbo frames** at all levels (client, nodes, switches)
- **Bonding mode 6 (balance-alb)** for clients: Parallel writes on separate NICs

**Storage Configuration**:
- **JBOD** for highly multi-threaded sequential reads
- **RAID 6 or RAID 10** for other workloads
- 2-way replication shows **half network performance** for writes (synchronous writes)

**Client Access Methods**:
- **Native FUSE client**: Aware of volume geometry, all servers, direct connections, even distribution
- **NFS/SMB**: Client connects to single server, which then makes secondary calls (less efficient)

**Database Workloads**:
- PostgreSQL: GlusterFS ≈ gluster-block performance
- **MongoDB: gluster-block significantly better**

**Production Use Cases**:
- 10GB/day Lucene indexes stored on GlusterFS (200GB/month)
- Indexes opened directly via POSIX
- Version 3.0.x on 8 servers, distributed replicated, ~4TB available

**Nexus Applicability**:
- Design for many clients, not single-client optimization
- Fast networks are essential (10GbE+)
- Jumbo frames everywhere
- Native clients perform better than NFS gateways
- Match storage backend to workload type

---

## Academic Research Findings

### Metadata Scalability at Billions of Files

**Key Challenge**:
- Modern storage expected to exceed billions of files
- **Most files are small**
- **Over 50% of accesses are metadata operations**
- Metadata operations can be **up to 80% of total filesystem operations**

**Scale Requirements**:
- Hundreds of billions of files
- Hundreds of thousands of concurrent tenants
- Massive concurrent metadata access

**Notable Research Systems**:

#### GIGA+
- Scalable directory design for millions to billions of small files
- POSIX-compliant
- **Asynchrony and eventual consistency** to partition index without synchronization
- Distributes directory entries over cluster of servers

#### InfiniFS
- Addresses: load balancing with locality, long path resolution, near-root hotspots
- **Decouples access and content metadata** of directories
- **Speculative path resolution**: Traverse possible paths in parallel (substantially reduces latency)

#### SingularFS
- "Billion-scale distributed file system using single metadata server" (USENIX ATC 2023)
- Demonstrates that careful design can push single-server limits much further

#### AsyncFS
- **Asynchronous metadata updates**: Operations return early
- Defer directory updates for latency hiding and conflict resolution
- Contrast to typical synchronous updates

#### FileScale
- Uses shared-nothing distributed database systems (DDBMS) for metadata
- Alleviates scalability challenges without compromising high availability

**Modern Architectural Approaches**:
- Eliminate single-leader model
- Multiple concurrent, multi-threaded metadata servers
- Metadata in sharded, ACID-compliant transactional databases (e.g., FoundationDB)
- Benefits: horizontal scalability, fault tolerance, reduced memory, consistent performance
- Enables **exabyte-scale operation with billions of small files**

**Industry Examples**:
- **Taobao FileSystem (TFS)**: 28.6+ billion small photos
- **Facebook Tectonic**: Exabyte-scale (FAST 2021)

**Nexus Applicability**:
- Never assume metadata is small enough for single server
- Distribute metadata across multiple servers from the start
- Consider eventual consistency for non-critical metadata
- Speculative/parallel operations to hide latency
- Sharded transactional database for metadata (FoundationDB, etc.)

### Permission Check Optimization

**Key Problem**: Permission checks that query on every request will break at scale

**Pre-computed Permissions (Recommended)**:
- Pre-compute permissions at **write-time** for fast reads
- Recursive queries with nested folders become bottleneck
- Since systems read more than write, **optimize for reads**

**Core Trade-off**: Pay cost at read-time (recursive queries) OR write-time (maintain permissions index)

**ABAC (Attribute-Based Access Control)**:
- Great for complex decisions
- Example: Figma's permission system
- Use when need dynamic, context-aware decisions

**Main Risk**: Pre-computed permissions can get out of sync
- **Build rebuild script from day one**
- Can recompute all permissions from source of truth
- Plan for eventual inconsistencies

**Distributed Systems Considerations**:
- Strong authentication: MFA, OAuth, JWT
- Centralized identity providers or federated identity management
- Fine-grained authorization based on roles and permissions

**Ceph Example**:
- Metanode service retains file ownership and permissions for each file
- Token manager server synchronizes concurrent access
- Supports **250,000+ metadata operations/second**

**Nexus Applicability**:
- Pre-compute all permissions at write time
- Store in denormalized, indexed form for fast reads
- Build permission rebuild mechanism from day one
- Avoid recursive permission queries in read path
- Cache permission check results aggressively

### Caching Strategies

**Metadata Performance Problems**:
- Billions of small files
- More metadata operations than data operations
- **Metadata lookup operations dominate** workload
- Poor performance without caching

**Aggressive Metadata Caching Results**:
- **50% improvement** in creation rates
- **40x improvement** in stat rates
- Must guarantee consistency while caching

**Client-Side Caching**:
- Client stores local copy of frequently accessed files
- Checks if local copy is up-to-date before using
- Reduces server load and network traffic

**Server-Side Caching**:
- Server stores frequently accessed files in memory or local disks
- Avoids disk access for cached items
- Returns from cache without disk I/O
- Reduces network traffic

**Distributed Caching (JuiceFS example)**:
- Clients form "distributed cache group" (consistent hashing ring)
- Each cached block's location calculated via consistent hashing
- Shared within group
- Virtual nodes for load balance (prevent hot spots)

**Cache Validation**:
- **Client-initiated**: Contact server before accessing cache
- **Server-initiated**: Server notifies clients when data stale

**Architecture Considerations**:
- Caching contributes most to distributed filesystem performance
- Exploits temporal locality of reference
- Directories, protection, file status, location info all exhibit locality
- Store frequently accessed metadata in memory

**Partitioning & Sharding**:
- Large shared caches: partition data across nodes
- Reduces contention, improves scalability
- Dynamic add/remove nodes and rebalance

**Hot Spot Mitigation (IndexFS example)**:
- Evenly distribute directory tree into metadata servers
- Path traversal makes some directories (e.g., root) more accessed
- **Stateless directory caching** mitigates hot spots

**Nexus Applicability**:
- Implement aggressive metadata caching (50%+ improvement possible)
- Three-tier caching: client-side, server-side, distributed
- Stateless caching for hot spots (especially root/top-level)
- Cache validation strategy (push vs pull)
- Partition large caches to avoid contention

---

## Key Patterns & Optimizations

### 1. Write Path Optimization

**Ceph BlueStore Findings (MSST 2024)**:
- Abstraction and redundancy cause **significant write amplification**
- Object creation throughput: **80% higher on raw HDD**, **70% higher on raw NVMe** vs BlueStore
- BlueStore saves metadata in RocksDB, stores data on raw disks

**Access Pattern Reshaping**:
- Reshape random writes → sequential writes for SSDs
- Performance improvement: **up to 46.26%** vs existing schemes

**LSM-Tree (RocksDB/LevelDB) Compaction**:
- Minimize write amplification through tuning
- Delete operations trigger SSD internal GC and increase I/O latency
- **Rate-limit file deletions** during compaction

**Data Locality for Large Files**:
- Local-write protocol for distributed filesystems
- Performance improvement: **~13%** across various update patterns

**Load Balancing**:
- Distribute workload equally among servers/storage nodes
- Dynamically route based on current load, capacity, proximity
- Eliminates hot spots

**Nexus Applicability**:
- Minimize abstraction layers in write path
- Convert random writes to sequential where possible
- Rate-limit background operations (compaction, GC)
- Implement load-aware routing
- Measure and minimize write amplification

### 2. Read Path Optimization

**In-Memory Filesystem Caching (Alluxio)**:
- Two-layer user-space cache:
  - **Packet-level cache**: Reduce page fault interruptions (**2x performance**)
  - **Object-level cache**: Avoid redundant IPCs (**4x faster** than native client)

**High-Performance Read-Intensive Filesystems (AWS)**:
- Achieved **10,257,000 IOPS** (8K) across 13 nodes
- Average latency: **171.34 µsec**
- Flash-based scalable shared filesystems for massive IOPS + bandwidth

**File Caching Benefits**:
- Reduces network traffic
- Minimizes disk access
- Reduces latency by avoiding network/disk access

**ClickHouse Distributed Cache**:
- Object storage has high access latency (performance bottleneck)
- Local SSDs as filesystem cache (middle ground)
- Fast-but-volatile memory ↔ slow-but-durable object storage

**Prefetching**:
- Hide latency of wide-area file transfers
- Fetch data asynchronously while application busy
- Provides performance benefits for WAN filesystems

**CubeFS Local Disk Caching**:
- Local disk of compute node as data block cache
- Read requests access data cache first
- If cache hit: Get from local disk
- If cache miss: Read from backend (replication/erasure coding subsystem)

**Nexus Applicability**:
- Multi-layer caching (L1: memory, L2: local SSD, L3: remote)
- Prefetch based on access patterns
- Cache at multiple levels simultaneously
- Optimize for sub-millisecond latency on cache hits
- Local disk cache for compute nodes

### 3. Garbage Collection & Compaction

**Object Storage GC at Scale (WarpStream)**:
- **Mark and sweep**: Files not tracked in metadata = orphaned → safe to delete
- **Problem**: Listing files in commodity object stores is slow, expensive, rate-limited
- HEAD requests for file age cost money
- **Solution**: Delayed queue approach
  - Files deleted from metadata are first enqueued
  - Deleted later after delay (avoid disrupting live queries)

**Distributed GC Algorithms**:
- Global garbage collection for loosely-coupled multiprocessor systems
- System-wide marking phase for accessible objects
- Parallel breadth-first/depth-first strategies
- Decentralized credit mechanism
- Scales to **1024 node MPPs**

**RocksDB/LSM-Tree Compaction**:
- File deletion triggers SSD internal GC and increases I/O latency
- **Rate-limit file deletions** to prevent simultaneous deletions during compaction
- LSM structure: MemTable → WAL → L0 → L1... compaction

**BigTable Compaction**:
- **Merging compaction**: Memtable + several recent SSTables → single SSTable
  - Preserves tombstones to hide keys in lower, uncompacted SSTables
- **Major compaction**: All SSTables → single SSTable
  - Eliminates all deleted data
- Many compaction operations in **background** while updates/lookups proceed in parallel

**Key Challenges**:
- Must scale with number of nodes and objects
- Handle failures gracefully
- Memory fragmentation causes costly compaction operations
- Introduces latency spikes (disruptive for real-time/latency-sensitive apps)

**Nexus Applicability**:
- Implement delayed deletion queue (don't delete immediately)
- Run compaction in background, parallel to read/write operations
- Rate-limit deletions to avoid SSD GC storms
- Distinguish merging compaction (preserves tombstones) from major compaction (full cleanup)
- Handle GC failures gracefully
- Monitor for latency spikes during compaction

### 4. Memory Efficiency

**JuiceFS Memory Optimization**:
- All-in-memory metadata engine approach
- **27% of HDFS NameNode memory** for same number of files
- **3.7% of CephFS MDS memory** for same number of files
- **Average: 100 bytes per file** for metadata

**Four Optimization Techniques**:
1. Memory pools
2. Manual management of small memory blocks
3. Compression of idle directories
4. Optimization of small file formats

**Two Main Approaches**:
1. **All metadata in memory** (HDFS NameNode): Excellent performance, large memory requirements
2. **Partial metadata in memory** (CephFS MDS): Retrieves from disk when not cached

**Performance Optimizations**:
- Lookup dentry cache hash table in **backward manner**
- Compact metadata into dentry structures for in-memory space efficiency
- Reduces dcache lookup latency: **up to 40%**
- Improves overall throughput: **up to 72%**

**Small Files Problem**:
- Massive small files generate lots of metadata in HDFS
- NameNode holds all metadata in memory → can run out of memory and hurt performance
- No effective DFS works well for massive small files

**IndexFS Solution**:
- Table-based architecture
- Incrementally partitions namespace per-directory
- Optimized log-structured layout for metadata and small files
- Adds support to PVFS, Lustre, HDFS

**Nexus Applicability**:
- Target 100 bytes per file for in-memory metadata
- Use memory pools for metadata allocation
- Compress idle/cold metadata
- Optimize data structures for small memory footprint
- Consider hybrid approach: hot metadata in memory, cold on disk
- Special handling for small files

### 5. Erasure Coding vs Replication

**Storage Efficiency**:
- **Replication**: 3x capacity minimum (3 copies)
- **Erasure Coding**: 1.5x to 1.75x capacity
- Example: 4,2 erasure code = 1.5x overhead vs 3.0x for replication

**Performance Costs**:

*CPU Overhead*:
- Parity calculation is CPU-intensive
- Increased latency can slow production writes and rebuilds
- Replication only copies data (minimal CPU)

*Read/Write Performance*:
- EC spreads data across nodes/racks → higher network cost
- Parity block generated on write → impacts write speed
- Every read I/O comes from multiple nodes

*Recovery Performance*:
- Media/node failure: EC recalculates parity on the fly (performance problem)
- Background rebuild required
- Decoding performance varies with recovered data chunks
- Gaussian elimination for decoding from data + parity chunks

**Best Use Cases**:

*Erasure Coding*:
- **Cold data** (accessed/modified less frequently)
- **Larger files**
- Large quantities of data requiring failure tolerance
- Archival storage

*Replication*:
- **Hot/highly valuable data** (accessed/modified regularly)
- **Data locality** (all reads are remote with EC)
- Better for hot data

**Small File Considerations**:
- EC produces more blocks for small files (data + parity blocks)
- Heightened memory consumption
- Worse bytes/blocks ratio
- Increases NameNode memory usage

**Summary Trade-offs**:
- EC: Storage efficient but high performance overhead (repair, updates)
- Replication: More storage but better performance
- Major tension: storage efficiency vs performance vs reliability

**Nexus Applicability**:
- Use replication for hot data, EC for cold data
- Transition: replication → EC as data ages
- Small files: favor replication
- Large files: favor erasure coding
- Accept higher CPU/network cost for storage savings
- Factor in recovery performance requirements

### 6. LSM-Tree Compaction Strategies

**Leveled Compaction**:
- Some data from L(n-1) merged with overlapping data in L(n)
- When level-L exceeds size target: Select SSTables, merge with level-(L+1)
- Removes deleted and overwritten data
- Optimizes for read performance and space efficiency

**Dynamic Leveled Compaction (RocksDB)**:
- Level sizes automatically adjusted based on oldest (last) level size
- Better overall and more stable space efficiency than static sizing

**Universal (Tiered) Compaction**:
- Triggered by: number of sorted runs OR estimated space amplification
- Thresholds determine when to compact

**Default Hybrid Strategy (RocksDB "1-Lvl")**:
- First disk level (L0): Tiered
- Other levels: Leveled

**Performance Trade-offs**:

*Write Amplification*:
- Size ratio T: Leveling has **T× higher WA** than tiering
- But offers **T× lower read amplification**
- Leveled compaction: WA often **larger than 10**

*L0 Optimization*:
- Too many L0 files hurt read performance
- Intra-L0 compaction: Compact some L0 files together
- Sacrifices write amplification by 1x
- Significantly improves read amplification

**Advanced Optimizations**:

*Partial Compaction Policies*:
- RoundRobin (from LevelDB):
  - Classic: Pick files using key cursor
  - Alternative: Pick files using file rank

*Delete-Driven Compaction*:
- TSD (Tombstone Density): Based on tombstone density
- TSA (Tombstone Age): Based on oldest tombstone age
- Select files by deletion distribution
- Eliminate deletion-related records
- Avoid redundant data merging

*Compaction Offloading*:
- Performance-critical L0-L1 compaction on fast host cores
- Dynamically offload L2-Ln compaction to slower DPU cores
- Based on computation headroom

**Practical Considerations**:
- Pick compaction method to reduce WA when write rate high
- Compact more aggressively when write rate low (space efficiency + better reads)
- Multiple policies and parameters for different scenarios
- Cost of compaction inevitable, but policies affect read-write and space amplification

**Nexus Applicability**:
- Implement dynamic level sizing
- L0 optimization critical for read performance
- Use delete-driven compaction when tombstones accumulate
- Consider offloading heavy compactions to background workers
- Tune WA vs RA based on workload (read-heavy vs write-heavy)
- Monitor and adjust compaction triggers dynamically

### 7. Consistent Hashing

**Overview**:
- Enables horizontal scaling while maintaining performance and reliability
- Distributes data evenly while allowing dynamic server changes
- Minimizes cost of transitions

**How It Works**:
- Assigns data objects and nodes to positions on virtual ring (hash ring)
- Same hash function for both node identifiers and data keys
- Maps keys and nodes to same namespace around a ring

**Key Benefits**:
- Minimizes keys remapped when total nodes change
- With K keys and n slots: Only **K/n keys remapped** (vs nearly all keys in traditional hashing)
- Gradual scaling: Adding/removing server doesn't overhaul entire data distribution

**Virtual Nodes (Vnodes)**:
- Each physical node assigned set of Vnodes
- Each Vnode handles smaller hash ranges
- Prevents uneven distribution with few nodes
- Creates even distribution across ring
- Example: 3 physical nodes → 9 virtual nodes

**Real-World Applications**:
- Amazon Dynamo
- Apache Cassandra
- Vimeo (load balancing for video streaming)
- Content Delivery Networks (CDNs)
- Distributed Hash Tables (DHTs)

**Historical Context**: Introduced by David Karger et al. at MIT for distributed caching (especially web)

**Nexus Applicability**:
- Use for data partitioning across storage nodes
- Implement virtual nodes (10-256 per physical node)
- Minimal data movement when scaling
- Enables incremental scaling
- Standard approach for distributed systems

### 8. Bloom Filters for Metadata Lookup

**Hierarchical Bloom Filter Arrays (HBA)**:
- Efficient distributed file mapping/lookup scheme
- Critical for decentralizing metadata management
- Maps file names to servers holding their metadata

**Two-Level Design**:
1. **Lower accuracy array**: Represents entire metadata distribution
   - Trades accuracy for significantly reduced memory overhead
2. **Higher accuracy array**: Caches partial distribution
   - Exploits temporal locality of file access patterns

**Scale**: Highly effective for clusters with **1,000 to 10,000 nodes** (or superclusters)

**Lookup Optimization**:
- Reduces disk/network accesses required
- Store dataset metadata in Bloom filter
- Quickly determine if data likely in dataset
- Avoid unnecessary disk/network accesses

**Production Examples**:
- **Google BigTable**: Reduces disk accesses for lookups
- **Apache Cassandra**: Optimizes lookups, reduces disk accesses

**Distributed Storage Systems**:
- Each node selects items, inserts into probabilistic data structure
- Index node gathers all data structures
- Capable of locating items in system

**Performance Benefits**:
- Decrease false positives by **up to 79.83%**
- Optimize lookups on UUIDs, enums, text fields by **up to 100x**
- Storage overhead: Few hundred bytes per batch (max 1KB)
- 1M batches: ~100MB to 1GB bloom filter metadata
- **0.01% storage overhead** for massive query speedups

**Distributed Bloom Filter Variants**:
- Distribute global Bloom filter over all nodes (not separate on each)
- Far larger filters possible
- Larger capacity, lower false positive rate
- Improve duplicate detection by filtering "unique" elements
- Communicate only hashes (not full elements - far smaller)

**Nexus Applicability**:
- Implement Bloom filters for metadata existence checks
- Two-tier approach: coarse + fine-grained filters
- Use for "does file exist in this subtree?" queries
- Store in metadata servers
- Exploit temporal locality
- Minimal memory overhead for huge speedups

---

## Performance Benchmarks & Numbers

### IOPS (Input/Output Operations Per Second)

**Storage Media**:
- **SSDs**: 10,000 to 100,000+ IOPS
- **HDDs**: 100 to 1,000 IOPS

**Production Systems**:
- **Google Colossus**: 600M+ IOPS (single cluster, combined reads + writes)
- **AWS High-Performance FS**: 10,257,000 IOPS (8K, across 13 nodes)
- **StorPool Distributed Storage**: 6,800,000 IOPS peak, 13.8M IOPS on 12-node cluster
- **Uber CacheFront**: 40M+ requests/second

**Latency**:
- **HDDs**: 10-20ms acceptable (20ms upper limit)
- **SSDs**: 1-3ms depending on workload (<1ms typical)
- **StorPool**: <0.15ms under typical load
- **AWS FS**: 171.34 µsec average
- **3FS**: ~5µs at ~1 GB/s (4K messages)

### Throughput

**Production Systems**:
- **Google Colossus**: 50+ TB/s read, 25+ TB/s write (largest filesystems)
- **Netflix Edge**: 73 Tbps edge traffic, ~1.46 Tbps origin (98% cache hit rate)
- **3FS**: Peaks at ~11.5 GB/s (92% of theoretical, 4K-8K messages)
- **Facebook Haystack**: 1M+ images/second at peak

**Access Pattern Impact**:
- Same disk: 29,000 IOPS (sequential) → 245 IOPS (random)
- Throughput: 121 MB/s (sequential) → <1 MB/s (random)
- **Most production systems have random access = optimize for IOPS + low latency**

### Cache Hit Rates

- **Haystack Cache**: 80% for recently uploaded photos
- **Netflix OCAs**: 98% at edge
- **Uber CacheFront**: 99.9%

### Metadata Performance

- **Ceph**: 250,000+ metadata operations/second
- **JuiceFS caching**: 50% improvement in creation rates, 40x improvement in stat rates
- **Metadata lookup optimization**: Up to 40% reduction in dcache lookup latency, up to 72% throughput improvement

### Memory Efficiency

- **JuiceFS**: 100 bytes per file average
- **JuiceFS vs HDFS NameNode**: 27% of memory for same files
- **JuiceFS vs CephFS MDS**: 3.7% of memory for same files

### Storage Efficiency

- **Replication (3x)**: 3.0x capacity overhead
- **Erasure Coding (10,4)**: 1.4x capacity overhead
- **Erasure Coding (4,2)**: 1.5x capacity overhead
- **Facebook f4**: Reduced from 3.6x to 2.1x (42% storage savings)

### Scalability Numbers

- **BigTable**: Support for 16 billion tablet locations (theoretical)
- **Facebook Haystack**: 260 billion images, 20+ petabytes
- **Dropbox Magic Pocket**: Multi-exabyte scale
- **Taobao FileSystem**: 28.6+ billion small photos
- **Google Colossus**: 100x improvement over GFS, multiple exabyte filesystems

### Write Amplification

- **RocksDB Leveled Compaction**: Often >10x write amplification
- **Access Pattern Reshaping**: 46.26% performance improvement (random → sequential for SSDs)
- **Ceph BlueStore**: 80% higher throughput on raw HDD vs abstracted storage

### Network

- **Minimum for production**: 10 GbE
- **Modern production clusters**: 25/40/50/100 GbE
- **GlusterFS client:server ratio**: 12:1 to 48:1 optimal

### Recovery & Durability

- **Dropbox Magic Pocket**: 99.9999999999% annual durability (12 nines), 99.99% availability
- **Facebook Haystack**: 4x more reads/second, 28% cheaper per TB than previous solution
- **Uber CacheFront**: 99.9% cache hit rate eases load on storage engine

---

## Recommendations for Nexus

Based on this comprehensive research, here are prioritized recommendations for the Nexus distributed filesystem:

### 1. Metadata Architecture (CRITICAL)

**Implement Three-Layer Metadata Hierarchy**:
```
Level 1: Root metadata (never split) → Namespace partition locations
Level 2: Namespace partitions → File metadata locations
Level 3: File metadata → Block/chunk locations
```

**Storage Strategy**:
- Use sharded, ACID-compliant transactional database (FoundationDB, TiKV, or sharded MySQL)
- Hash-partition metadata for horizontal scalability
- Keep hot metadata in memory (target: 100 bytes per file)
- Stateless metadata services that reconstruct from database

**Key Metrics to Target**:
- 250,000+ metadata operations/second
- Sub-millisecond latency for cached metadata
- Support for billions of files

### 2. Caching Strategy (CRITICAL)

**Three-Tier Caching**:
1. **L1 (Client-side)**: In-memory metadata cache
2. **L2 (Server-side)**: Metadata server memory cache
3. **L3 (Distributed)**: Consistent hash ring for shared cache

**Specific Implementations**:
- Aggressive metadata caching (target: 50%+ improvement in creation rates, 40x in stat rates)
- Stateless directory caching for hot spots (root, top-level directories)
- Bloom filters for existence checks (100x speedup possible, 0.01% overhead)
- Cache validation: Server-push for invalidations

**Expected Results**:
- 80-99% cache hit rates
- Sub-millisecond cache hits

### 3. Storage Architecture

**Hybrid Approach**:
- **Recent/hot data**: 3x replication for low latency
- **Warm data (>1 month old)**: Transition to erasure coding (1.5x overhead)
- **Segment-based storage**: Like Haystack, aggregate many files into large segments (100GB+)

**Block/Chunk Design**:
- 4MB chunks (Dropbox Magic Pocket pattern)
- Store as (offset, length) in segment files
- Keep index in memory for single-seek retrieval

**Expected Results**:
- 40%+ storage savings for warm data
- 4x read performance improvement
- Minimal I/O operations per read

### 4. Permission System (HIGH PRIORITY)

**Pre-computed Permissions**:
- Calculate at write-time, not read-time
- Store in denormalized, indexed form
- Avoid recursive queries in read path
- Build permission rebuild mechanism from day one

**Implementation**:
- Use RBAC for simple cases
- ABAC for complex, dynamic decisions
- Cache permission check results aggressively
- Accept write-time cost for read-time speed

### 5. Write Path Optimization

**Key Strategies**:
- Minimize abstraction layers (avoid write amplification)
- Append-only where possible (like Schemaless)
- Buffer writes to handle transient failures
- Implement load-aware routing
- Rate-limit background operations

**LSM-Tree Compaction** (if using embedded key-value store):
- Dynamic level sizing
- L0 optimization for read performance
- Delete-driven compaction when tombstones accumulate
- Background compaction parallel to reads/writes

### 6. Read Path Optimization

**Multi-Layer Optimization**:
- L1: In-memory cache (target: <1ms latency)
- L2: Local SSD cache (target: <5ms latency)
- L3: Remote storage (target: <50ms latency)

**Techniques**:
- Prefetch based on access patterns
- Parallel speculative path resolution
- Direct data flow (minimize network hops)
- Bloom filters to avoid negative lookups

### 7. Scalability Patterns

**Consistent Hashing**:
- Use for data partitioning across storage nodes
- 128-256 virtual nodes per physical node
- Minimal data movement when scaling

**Load Balancing**:
- Client:server ratio of 12:1 to 48:1
- Dynamic routing based on load, capacity, proximity
- Traffic classes: Gold (latency-sensitive), Silver (normal), Bronze (background)

**Network Requirements**:
- Minimum 10 GbE (prefer 25/40/100 GbE)
- Jumbo frames everywhere
- Separate public and cluster traffic

### 8. Operational Excellence

**Monitoring & Tuning**:
- Built-in performance testing (like MinIO)
- Track IOPS, throughput, latency over time
- Auto-scaling for partition management
- Proactive problem identification

**Kernel Tuning**:
- `transparent_hugepage=madvise`
- CPU governor: `performance`
- `vm.swappiness=0`
- `vm.dirty_background_ratio=3`, `vm.dirty_ratio=10`

**Hardware Homogeneity**:
- Consistent CPU across nodes
- Consistent storage media (don't mix SSD + HDD)
- XFS filesystem (not ext4)
- Avoid layering (RAID, LVM, ZFS on top)

### 9. Garbage Collection & Compaction

**Delayed Deletion Queue**:
- Don't delete immediately from storage
- Enqueue for delayed deletion
- Avoid disrupting live queries

**Background Operations**:
- Run parallel to reads/writes
- Rate-limit to avoid latency spikes
- Distinguish merging (preserve tombstones) vs major (full cleanup) compaction

### 10. Memory Efficiency

**Target**: 100 bytes per file for in-memory metadata

**Techniques**:
- Memory pools for metadata allocation
- Manual management of small memory blocks
- Compress idle/cold metadata
- Optimize data structures for small footprint
- Hybrid: hot in memory, cold on disk

### Priority Implementation Order

1. **Phase 1 (Foundation)**:
   - Three-layer metadata hierarchy
   - Sharded metadata database
   - Basic caching (client + server)
   - Pre-computed permissions

2. **Phase 2 (Performance)**:
   - Segment-based storage (Haystack pattern)
   - Aggressive metadata caching
   - Bloom filters for lookups
   - Write path optimization

3. **Phase 3 (Scale)**:
   - Consistent hashing
   - Distributed caching
   - Erasure coding for warm data
   - LSM-tree compaction tuning

4. **Phase 4 (Production Readiness)**:
   - Monitoring & performance testing
   - Garbage collection & compaction
   - Kernel & network tuning
   - Operational tooling

### Key Performance Targets

Based on production systems, target these metrics:

- **Metadata operations**: 100,000+ ops/second (start), 250,000+ ops/second (scaled)
- **Latency**: <1ms (cache hit), <10ms (cache miss), <50ms (p99)
- **Cache hit rate**: 80%+ (day 1), 99%+ (optimized)
- **Memory per file**: <200 bytes (start), ~100 bytes (optimized)
- **Storage overhead**: 3x (hot), 1.5x (warm/cold after migration)
- **Throughput**: 1+ GB/s per node (start), scale linearly

---

## Sources

### Facebook/Meta Systems
- [Needle in a haystack: efficient storage of billions of photos - Engineering at Meta](https://engineering.fb.com/2009/04/30/core-infra/needle-in-a-haystack-efficient-storage-of-billions-of-photos/)
- [Finding a needle in Haystack: Facebook's photo storage - USENIX](https://www.usenix.org/legacy/event/osdi10/tech/full_papers/Beaver.pdf)
- [f4: Facebook's Warm BLOB Storage System - USENIX](https://www.usenix.org/conference/osdi14/technical-sessions/presentation/muralidhar)
- [f4. Facebook's Warm BLOB Storage System - Medium](https://medium.com/@shagun/f4-cba2f141cb0c)
- [Consolidating Facebook storage infrastructure with Tectonic file system](https://engineering.fb.com/2021/06/21/data-infrastructure/tectonic-file-system/)
- [Facebook's Tectonic Filesystem: Efficiency from Exascale - USENIX](https://www.usenix.org/conference/fast21/presentation/pan)

### Google Systems
- [A peek behind Colossus, Google's file system - Google Cloud Blog](https://cloud.google.com/blog/products/storage-data-transfer/a-peek-behind-colossus-googles-file-system)
- [Colossus: Successor to the Google File System (GFS) - SysTutorials](https://www.systutorials.com/colossus-successor-to-google-file-system-gfs/)
- [How Google stores Exabytes of Data](https://blog.quastor.org/p/google-stores-exabytes-data)
- [Bigtable: A Distributed Storage System for Structured Data](https://research.google.com/archive/bigtable-osdi06.pdf)
- [Paper Notes: Bigtable - Distributed Computing Musings](https://distributed-computing-musings.com/2022/09/paper-notes-bigtable-a-distributed-storage-system-for-structured-data/)
- [Spanner: Google's Globally-Distributed Database](https://research.google/pubs/spanner-googles-globally-distributed-database-2/)
- [How Google Spanner Powers Trillions of Rows with 5 Nines Availability](https://blog.bytebytego.com/p/how-google-spanner-powers-trillions)

### Netflix/Dropbox/Uber
- [Inside the Magic Pocket - Dropbox](https://dropbox.tech/infrastructure/inside-the-magic-pocket)
- [Scaling to exabytes and beyond - Dropbox](https://dropbox.tech/infrastructure/magic-pocket-infrastructure)
- [Magic Pocket: Dropbox's Exabyte-Scale Blob Storage System - InfoQ](https://www.infoq.com/articles/dropbox-magic-pocket-exabyte-storage/)
- [Inside Netflix's Video Streaming Delivery Architecture - Medium](https://medium.com/@hjain5164/inside-netflixs-video-streaming-delivery-architecture-e2c848e98a85)
- [Open Connect Overview](https://openconnect.netflix.com/Open-Connect-Overview.pdf)
- [How Uber Serves Over 40 Million Reads Per Second from Online Storage Using an Integrated Cache](https://www.uber.com/blog/how-uber-serves-over-40-million-reads-per-second-using-an-integrated-cache/)
- [Distributed Systems Design - Uber](https://elatov.github.io/2021/04/distributed-systems-design-uber/)

### Open Source Production Systems
- [GitHub - minio/minio - Kernel Tuning](https://github.com/minio/minio/blob/master/docs/tuning/README.md)
- [Selecting the Best Hardware for Your MinIO Deployment](https://blog.min.io/selecting-hardware-for-minio-deployment/)
- [How to Tune Ceph for Block Storage Performance](https://openmetal.io/resources/blog/how-to-tune-ceph-for-block-storage-performance/)
- [Ceph all-flash/NVMe performance: benchmark and optimization](https://croit.io/blog/ceph-performance-test-and-optimization)
- [7 Best Practices to Maximize Your Ceph Cluster's Performance](https://tracker.ceph.com/projects/ceph/wiki/7_Best_Practices_to_Maximize_Your_Ceph_Cluster's_Performance)
- [Best Practices of GlusterFS Performance Tuning - Medium](https://medium.com/@eren.c.uysal/best-practices-of-glusterfs-performance-tuning-a7474f00730e)
- [Gluster linear scaling: How to choose wisely](https://www.redhat.com/en/blog/gluster-linear-scaling-how-choose-wisely)

### Academic Research
- [Metadata Performance Optimization in Distributed File System](https://www.researchgate.net/publication/254037230_Metadata_Performance_Optimization_in_Distributed_File_System)
- [The State of the Art of Metadata Managements in Large-Scale Distributed File Systems](https://ieeexplore.ieee.org/document/9768784/)
- [InfiniFS: Scientists claim to have solved the 100-billion-file problem](https://blocksandfiles.com/2022/03/10/infinifs-solves-the-100-billion-file-metadata-problem/)
- [AsyncFS: Metadata Updates Made Asynchronous for Distributed Filesystems](https://arxiv.org/html/2410.08618v1)
- [Permission Systems for Enterprise that Scale](https://eliocapella.com/blog/permission-systems-for-enterprise/)

### Caching & Performance
- [Metadata Management in Distributed File Systems - GeeksforGeeks](https://www.geeksforgeeks.org/system-design/metadata-management-in-distributed-file-systems/)
- [File Caching in Distributed File Systems - GeeksforGeeks](https://www.geeksforgeeks.org/file-caching-in-distrubuted-file-systems/)
- [Building a Distributed Cache for S3 - ClickHouse](https://clickhouse.com/blog/building-a-distributed-cache-for-s3)
- [Improving in-memory file system reading performance by fine-grained user-space cache](https://www.sciencedirect.com/science/article/abs/pii/S1383762121000151)
- [Scaling a read-intensive, low-latency file system to 10M+ IOPs - AWS](https://aws.amazon.com/blogs/hpc/scaling-a-read-intensive-low-latency-file-system-to-10m-iops/)

### Compaction & GC
- [Taking out the Trash: Garbage Collection of Object Storage at Massive Scale - WarpStream](https://www.warpstream.com/blog/taking-out-the-trash-garbage-collection-of-object-storage-at-massive-scale)
- [Compaction - facebook/rocksdb Wiki](https://github.com/facebook/rocksdb/wiki/Compaction)
- [Leveled Compaction - facebook/rocksdb Wiki](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction)
- [RocksDB: Evolution of Development Priorities in a Key-value Store](https://dl.acm.org/doi/fullHtml/10.1145/3483840)
- [Constructing and Analyzing the LSM Compaction Design Space](https://vldb.org/pvldb/vol14/p2216-sarkar.pdf)

### Memory & Storage Efficiency
- [How a Distributed File System in Go Reduced Memory Usage by 90% - JuiceFS](https://juicefs.com/en/blog/engineering/reduce-metadata-memory-usage)
- [Erasure Coding vs Replication - SNIA](https://www.snia.org/sites/default/files/SDC15_presentations/datacenter_infra/Shenoy_The_Pros_and_Cons_of_Erasure_v3-rev.pdf)
- [Comparing replication and erasure coding - Cloudera](https://docs-archive.cloudera.com/runtime/7.2.10/scaling-namespaces/topics/hdfs-ec-comparing-replication-and-erasure-coding.html)
- [Erasure Coding for Distributed Systems](https://transactional.blog/blog/2024-erasure-coding)

### Distributed Systems Patterns
- [Consistent hashing algorithm - High Scalability](https://highscalability.com/consistent-hashing-algorithm/)
- [Partitioning and Consistent Hashing - Medium](https://medium.com/the-bytedoodle-blog/partitioning-and-consistent-hashing-1c52245fe706)
- [Hierarchical Bloom Filter Arrays (HBA) - IEEE](https://ieeexplore.ieee.org/document/1392614/)
- [Implement fast, space-efficient lookups using Bloom filters - AWS](https://aws.amazon.com/blogs/database/implement-fast-space-efficient-lookups-using-bloom-filters-in-amazon-elasticache/)
- [Bloom Filters: The Unsung Heroes of Computer Science](https://www.bytedrum.com/posts/bloom-filters/)

### Benchmarks & Performance
- [IOPS vs Throughput vs Latency - Storage Performance Metrics](https://www.simplyblock.io/blog/iops-throughput-latency-explained/)
- [Understanding Storage Performance - IOPS and Latency](https://louwrentius.com/understanding-storage-performance-iops-and-latency.html)
- [Record-Breaking IOPS Storage - StorPool](https://storpool.com/performance-results-and-iops)
- [etcd Benchmark IOPS Throughput](https://kubedo.com/etcd-benchmark-iops-throughput-comparison-of-6-storage-backends/)

### Write/Read Path Optimization
- [Optimization of Distributed Block Storage Services for Cloud - MSST 2024](https://www.msstconference.org/MSST-history/2024/Papers/msst24-5.1.pdf)
- [Optimization and performance improvement of distributed data storage - WJAETS 2024](https://wjaets.com/sites/default/files/WJAETS-2024-0443.pdf)
- [Improving I/O performance in distributed file systems for flash-based SSDs](https://www.sciencedirect.com/science/article/abs/pii/S0167739X19331000)

---

**Document prepared**: 2025-12-26
**Research scope**: Production distributed filesystems at billions of files scale
**Application**: Nexus AI Filesystem optimization and architecture decisions
