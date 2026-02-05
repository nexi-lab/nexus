# Vector Search Tuning Guide

This guide covers pgvector HNSW index tuning for optimal performance at different scales.

## Quick Reference

| Scale | Vectors | m | ef_construction | ef_search | maintenance_work_mem |
|-------|---------|---|-----------------|-----------|----------------------|
| Small | <100K | 16 | 64 | 40 | 512MB |
| Medium | 100K-1M | 24 | 128 | 100 | 2GB |
| Large | >1M | 32 | 200 | 200 | 4-8GB |

## HNSW Parameters Explained

### Index Build Parameters

#### `m` (Max Connections)
- **Default:** 16
- **Range:** 8-64
- **Effect:** Controls the maximum number of connections per node in the HNSW graph
- **Trade-off:** Higher values improve recall but increase index size and build time
- **Rule of thumb:** Use 24-32 for high-dimensional embeddings (1536+ dims)

#### `ef_construction`
- **Default:** 64
- **Range:** 64-512
- **Effect:** Size of the dynamic candidate list during index construction
- **Trade-off:** Higher values improve index quality but slow down builds
- **Rule of thumb:** Should be at least 2x the value of `m`

### Search Parameter

#### `ef_search` (hnsw.ef_search)
- **Default:** 40
- **Range:** 40-500
- **Effect:** Size of the dynamic candidate list during search
- **Trade-off:** Higher values improve recall but slow queries
- **Rule of thumb:** Must be >= LIMIT (k) in your queries; use 100 for 0.99+ recall

```sql
-- Set per-session or per-query
SET hnsw.ef_search = 100;
-- Or for a single query
SET LOCAL hnsw.ef_search = 100;
```

## Auto-Configuration

Nexus automatically configures HNSW parameters based on your dataset size:

```python
from nexus.search.hnsw_config import HNSWConfig

# Get optimal config for your vector count
config = HNSWConfig.for_dataset_size(vector_count=500_000)
# Returns: HNSWConfig(m=24, ef_construction=128, ef_search=100)
```

## Index Creation

### Small Scale (<100K vectors)

```sql
CREATE INDEX idx_chunks_embedding_hnsw
ON document_chunks
USING hnsw (embedding halfvec_cosine_ops)
WITH (m = 16, ef_construction = 64);

SET hnsw.ef_search = 40;
```

### Medium Scale (100K - 1M vectors)

```sql
-- Speed up index build
SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 7;

CREATE INDEX idx_chunks_embedding_hnsw
ON document_chunks
USING hnsw (embedding halfvec_cosine_ops)
WITH (m = 24, ef_construction = 128);

SET hnsw.ef_search = 100;
```

### Large Scale (>1M vectors)

```sql
-- Allocate more memory for large indexes
SET maintenance_work_mem = '4GB';
SET max_parallel_maintenance_workers = 7;

CREATE INDEX idx_chunks_embedding_hnsw
ON document_chunks
USING hnsw (embedding halfvec_cosine_ops)
WITH (m = 32, ef_construction = 200);

SET hnsw.ef_search = 200;
```

**Considerations for >1M vectors:**
- Consider IVFFlat for faster builds (12-42x faster than HNSW)
- Use partitioning by `zone_id` for multi-zone deployments
- Consider `halfvec` for 50% memory reduction (see below)

## Half-Precision Vectors (halfvec)

pgvector 0.7.0+ supports half-precision (float16) vectors via `halfvec`:

### Benefits

| Metric | vector (float32) | halfvec (float16) | Reduction |
|--------|------------------|-------------------|-----------|
| Storage per vector | 6KB (1536 dims) | 3KB | 50% |
| Index size (1M vectors) | ~9GB | ~3GB | 66% |
| Recall accuracy | Baseline | <1% loss | Negligible |
| Query latency | Baseline | Slightly faster | ~5-10% |

### Migration

```sql
-- 1. Drop existing index
DROP INDEX IF EXISTS idx_chunks_embedding_hnsw;

-- 2. Add new halfvec column
ALTER TABLE document_chunks ADD COLUMN embedding_new halfvec(1536);

-- 3. Convert existing embeddings
UPDATE document_chunks
SET embedding_new = embedding::halfvec
WHERE embedding IS NOT NULL;

-- 4. Swap columns
ALTER TABLE document_chunks DROP COLUMN embedding;
ALTER TABLE document_chunks RENAME COLUMN embedding_new TO embedding;

-- 5. Recreate index with halfvec operator
CREATE INDEX idx_chunks_embedding_hnsw
ON document_chunks
USING hnsw (embedding halfvec_cosine_ops)
WITH (m = 24, ef_construction = 128);
```

Nexus uses `halfvec` by default for new installations.

## PostgreSQL Configuration

### Docker Compose Settings

Add these to your PostgreSQL service for optimal HNSW performance:

```yaml
services:
  postgres:
    command: >
      postgres
      -c maintenance_work_mem=2GB
      -c max_parallel_maintenance_workers=7
      -c shared_buffers=1GB
      -c effective_cache_size=3GB
```

### maintenance_work_mem Sizing

| Vector Count | Dimensions | Recommended |
|--------------|------------|-------------|
| <100K | 1536 | 512MB |
| 100K-500K | 1536 | 1GB |
| 500K-1M | 1536 | 2GB |
| >1M | 1536 | 4GB+ |

**Warning:** If you see this message during index build:
```
NOTICE: hnsw graph no longer fits into maintenance_work_mem after 100000 tuples
HINT: Increase maintenance_work_mem to speed up builds.
```
Increase `maintenance_work_mem` for faster builds.

### Parallel Index Build

pgvector 0.6.0+ supports parallel index building:

```sql
-- Use up to 7 parallel workers (default: 2)
SET max_parallel_maintenance_workers = 7;
```

This can provide up to **30x faster** index builds.

## HNSW vs IVFFlat

| Aspect | HNSW | IVFFlat |
|--------|------|---------|
| Build time | Slower (32x) | Faster |
| Index size | Larger (1.3-4.5x) | Smaller |
| Query speed | Faster (15x) | Slower |
| Recall | Better | Good |
| Incremental updates | Yes | Requires rebuild |
| Best for | Production queries | Batch updates, large scale |

**Recommendation:** Use HNSW for most use cases. Consider IVFFlat only if:
- Build time is critical (batch processing)
- Dataset exceeds 10M vectors
- Memory is severely constrained

## Filtered Search (pgvector 0.8.0+)

For tenant-scoped searches, enable iterative scanning:

```sql
-- Prevent "overfiltering" - ensures you get K results even with filters
SET hnsw.iterative_scan = relaxed_order;

SELECT chunk_id, chunk_text, embedding <=> :query AS distance
FROM document_chunks dc
JOIN file_paths fp ON dc.path_id = fp.path_id
WHERE fp.zone_id = :zone_id
ORDER BY embedding <=> :query
LIMIT 10;
```

## Benchmarking

Use the provided benchmark script to test configurations:

```bash
# Run HNSW parameter benchmark
python scripts/benchmark_hnsw.py \
  --database-url postgresql://user:pass@localhost/nexus \
  --test-queries 100 \
  --output results.json
```

The script tests:
- Build time for different m/ef_construction values
- Query latency (P50, P95, P99)
- Recall@10 against exact search
- QPS (queries per second)

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| Recall@10 | >0.95 | Use ef_search >= 100 |
| P99 latency | <50ms | With warm index |
| QPS | >40 | Per connection |
| Index build (1M) | <30min | With parallel workers |

## Troubleshooting

### Slow Index Builds
1. Increase `maintenance_work_mem`
2. Increase `max_parallel_maintenance_workers`
3. Consider IVFFlat for very large datasets

### Low Recall
1. Increase `ef_search` (runtime tunable)
2. Rebuild index with higher `m` and `ef_construction`

### High Memory Usage
1. Use `halfvec` instead of `vector`
2. Reduce `m` parameter
3. Consider partitioning large tables

### Filtered Queries Return Few Results
1. Enable `hnsw.iterative_scan = relaxed_order` (pgvector 0.8.0+)
2. Increase `ef_search` to compensate

## References

- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [AWS pgvector Optimization Guide](https://aws.amazon.com/blogs/database/optimize-generative-ai-applications-with-pgvector-indexing/)
- [Neon pgvector Optimization](https://neon.com/docs/ai/ai-vector-search-optimization)
- [HNSW vs IVFFlat Comparison](https://medium.com/@bavalpreetsinghh/pgvector-hnsw-vs-ivfflat-a-comprehensive-study-21ce0aaab931)
- [halfvec Storage Optimization](https://neon.com/blog/dont-use-vector-use-halvec-instead-and-save-50-of-your-storage-cost)
