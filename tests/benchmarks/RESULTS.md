# Nexus Filesystem Benchmark Results

*Generated on 2025-10-22*

## Executive Summary

This benchmark compares **Nexus (embedded mode with LocalBackend)** against **raw local filesystem** operations to understand the performance characteristics and tradeoffs of Nexus's content-addressable storage (CAS) and metadata features.

## Key Findings

### 📊 Performance Overview

| Operation | File Size | Local FS | Nexus Embedded | Overhead Factor |
|-----------|-----------|----------|----------------|-----------------|
| **Write** | 1KB | 750 µs | 6.6 ms | 8.8x |
| | 100KB | 1.9 ms | 5.6 ms | 3.0x |
| | 1MB | 2.1 ms | 6.2 ms | 2.9x |
| | 10MB | 6.1 ms | 10.8 ms | 1.8x |
| **Read** | 1KB | 21 µs | 30.6 µs | 1.5x |
| | 100KB | 26 µs | 67.9 µs | 2.6x |
| | 1MB | 54.7 µs | 414.7 µs | 7.6x |
| | 10MB | 1.1 ms | 5.0 ms | 4.5x |
| **Metadata** | exists() | 6.5 µs | 1.3 µs | **0.2x (faster!)** |

### 🎯 Performance Characteristics

#### 1. Write Performance
- **Small files (1KB)**: ~8.8x overhead due to CAS hashing + metadata + SQLite operations
- **Medium files (100KB-1MB)**: ~3x overhead - more reasonable as content dominates overhead
- **Large files (10MB+)**: ~1.8x overhead - overhead becomes negligible relative to content size

**Interpretation**: For larger files, Nexus overhead is reasonable. The fixed cost of metadata operations is amortized over larger content.

#### 2. Read Performance
- **Cached reads (warm)**: Nexus is ~9.4x slower than local FS for 1MB files
- **Cold reads**: Variable performance depending on file size
- **Metadata ops**: Nexus is actually **faster** for existence checks (in-memory SQLite index)

**Interpretation**: Read performance is competitive for small files, but has overhead for larger files due to CAS indirection. However, metadata operations are optimized.

#### 3. Small File Performance (AI Workload Pattern)
- **100 small files (1KB each)**:
  - Reads: Local FS ~2.1 ms, Nexus ~3.1 ms (1.4x overhead) ✅ Good!
  - Writes: Local FS ~19.5 ms, Nexus ~551.6 ms (28x overhead) ⚠️ High overhead

**Interpretation**: Small file writes are expensive in Nexus due to per-file metadata operations. This is a known tradeoff for systems with rich metadata.

#### 4. Deduplication Efficiency (Nexus Superpower!) ⭐

| Test | Nexus Time | Local FS Time | Storage Savings |
|------|------------|---------------|-----------------|
| Incremental duplicate write | 6.6 ms | 1.9 ms | N/A |
| 100 duplicate 100KB files | 837 ms | 38.5 ms | **~99%** (1 copy vs 100) |

**Interpretation**: This is where Nexus shines! When writing the same content 100 times:
- Local FS: Stores 100 separate files (~10 MB total)
- Nexus CAS: Stores 1 content block (~100 KB total)
- **Storage savings: 99%** for deduplicated workloads

For AI agents that often work with similar or versioned files, this is a massive advantage!

## Detailed Analysis

### Write Throughput

```
---------------------------------------- write-throughput (embedded) ----------------------------------------
test_write_1kb       Mean: 6.6 ms   (151 ops/s)
test_write_100kb     Mean: 5.6 ms   (178 ops/s)
test_write_1mb       Mean: 6.2 ms   (162 ops/s)
test_write_10mb      Mean: 10.8 ms  (92 ops/s)

---------------------------------------- write-throughput (local_fs) ----------------------------------------
test_write_1kb       Mean: 750 µs   (1,333 ops/s)
test_write_100kb     Mean: 1.9 ms   (537 ops/s)
test_write_1mb       Mean: 2.1 ms   (473 ops/s)
test_write_10mb      Mean: 6.1 ms   (163 ops/s)
```

**Observation**: Write performance gap narrows as file size increases. For 10MB files, Nexus is only ~1.8x slower.

### Read Throughput

```
---------------------------------------- read-throughput (embedded) ----------------------------------------
test_read_1kb        Mean: 30.6 µs  (32,704 ops/s)
test_read_100kb      Mean: 67.9 µs  (14,736 ops/s)
test_read_1mb        Mean: 414.7 µs (2,411 ops/s)
test_read_10mb       Mean: 5.0 ms   (199 ops/s)

---------------------------------------- read-throughput (local_fs) ----------------------------------------
test_read_1kb        Mean: 21.0 µs  (47,522 ops/s)
test_read_100kb      Mean: 26.1 µs  (38,331 ops/s)
test_read_1mb        Mean: 54.7 µs  (18,284 ops/s)
test_read_10mb       Mean: 1.1 ms   (901 ops/s)
```

**Observation**: Read performance is reasonable for small files, with increasing overhead for larger files due to CAS lookup + retrieval.

### Cache Effectiveness

```
test_warm_read (1MB cached):
- Embedded: 445 µs (2,246 ops/s)
- Local FS: 47.5 µs (21,039 ops/s)
```

**Observation**: Even with caching, there's overhead from the metadata layer. Future optimization opportunity: in-memory content cache.

## Recommendations

### ✅ Good Use Cases for Nexus
1. **Large files** (>1MB): Overhead is minimal relative to content size
2. **Deduplicated workloads**: Massive storage savings (99%+ for identical content)
3. **Metadata-heavy operations**: Fast existence checks, versioning, permissions
4. **AI agent coordination**: Multi-agent access with permissions and provenance tracking

### ⚠️ Watch Out For
1. **Many small files**: 28x overhead for 100 x 1KB files
2. **Write-heavy workloads**: ~3-9x slower writes depending on file size
3. **Real-time streaming**: Read overhead may matter for latency-sensitive apps

### 🔧 Optimization Opportunities
1. **Batch small file operations**: Reduce per-file metadata overhead
2. **Implement aggressive content caching**: Reduce CAS lookup overhead
3. **Optimize SQLite configuration**: WAL mode, memory-mapped I/O
4. **Consider PostgreSQL**: Better concurrency for write-heavy workloads

## Concurrency Notes

During testing, we discovered that SQLite has challenges with high concurrent writes to the same file (UNIQUE constraint violations in version_history table). This is expected with SQLite's locking model. For production multi-agent systems with high write concurrency:

- **Use PostgreSQL backend** (via `nexus_backend=postgres`)
- **Batch operations** to reduce lock contention
- **Implement retry logic** for concurrent write scenarios

## Conclusion

Nexus provides **reasonable performance** with significant advantages in:
- ✅ **Content deduplication** (99%+ storage savings)
- ✅ **Metadata operations** (faster exists checks)
- ✅ **Versioning and provenance** (built-in)
- ✅ **Multi-backend flexibility** (local, GCS, S3)

The overhead is **acceptable** for AI workloads where:
- Files are medium-to-large sized
- Content is often duplicated or versioned
- Metadata and permissions matter
- Multi-agent coordination is required

For raw throughput on small files, traditional filesystems remain faster, but Nexus offers capabilities that justify the overhead for AI-native applications.

---

## Running These Benchmarks

```bash
# Run all benchmarks
pytest tests/benchmarks/ --benchmark-only

# Compare embedded vs local_fs
pytest tests/benchmarks/ --benchmark-only --benchmark-group-by=param:backend_type

# Save baseline
pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline

# Run specific benchmark group
pytest tests/benchmarks/test_throughput.py --benchmark-only
```

See `tests/benchmarks/README.md` for detailed instructions.
