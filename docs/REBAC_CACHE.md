# ReBAC Two-Level Cache Architecture

## Overview

Nexus now implements a **two-level caching system** for permission checks to dramatically improve performance:

- **L1 Cache (In-Memory)**: Ultra-fast LRU cache with <1ms lookup time
- **L2 Cache (Database)**: Persistent cache with 5-10ms lookup time
- **Compute (Graph Traversal)**: Falls back to full permission graph traversal when both caches miss

## Performance Comparison

| Operation | L1 Cache Hit | L2 Cache Hit | Cache Miss |
|-----------|--------------|--------------|------------|
| Single permission check | <1ms | 5-10ms | 50-500ms |
| Directory listing (100 files) | 10-20ms | 50ms | 1-2s |
| Expected hit rate | 85-95% | 70-90% | N/A |

**Expected speedup**: **5-10x faster** for hot paths (file reads, directory listings)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              Permission Check Request                   │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ▼
         ┌─────────────────┐
         │  L1 Cache Check │  <1ms
         │  (In-Memory)    │
         └────────┬────────┘
                  │
        ┌─────────┴─────────┐
        │ Hit?              │
        └─┬─────────────────┘
     Yes  │          No
          │           │
          │           ▼
          │    ┌─────────────────┐
          │    │  L2 Cache Check │  5-10ms
          │    │  (Database)     │
          │    └────────┬────────┘
          │             │
          │   ┌─────────┴─────────┐
          │   │ Hit?              │
          │   └─┬─────────────────┘
          │Yes  │          No
          │     │           │
          │     │           ▼
          │     │    ┌──────────────────┐
          │     │    │ Compute          │  50-500ms
          │     │    │ (Graph Traversal)│
          │     │    └────────┬─────────┘
          │     │             │
          │     │      ┌──────┴──────┐
          │     └──────┤ Populate L1 │
          │            │ & L2 Caches │
          │            └──────┬──────┘
          │                   │
          ▼                   ▼
┌────────────────────────────────┐
│    Return Permission Result    │
└────────────────────────────────┘
```

## Configuration

The L1 cache is **enabled by default** with sensible defaults:

```python
from nexus.core.rebac_manager import ReBACManager
from sqlalchemy import create_engine

engine = create_engine("sqlite:///nexus.db")

# Default configuration (recommended)
manager = ReBACManager(
    engine=engine,
    enable_l1_cache=True,       # L1 cache enabled
    l1_cache_size=10000,        # 10k entries (~1-2MB memory)
    l1_cache_ttl=60,            # 60 second TTL
    cache_ttl_seconds=300,      # L2 cache: 5 minutes
    enable_metrics=True,        # Track hit rates
    enable_adaptive_ttl=False,  # Optional: adjust TTL by write frequency
)
```

### Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enable_l1_cache` | `True` | Enable in-memory L1 cache |
| `l1_cache_size` | `10000` | Maximum L1 cache entries |
| `l1_cache_ttl` | `60` | L1 cache TTL (seconds) |
| `cache_ttl_seconds` | `300` | L2 cache TTL (seconds) |
| `enable_metrics` | `True` | Track cache metrics |
| `enable_adaptive_ttl` | `False` | Adjust TTL based on write frequency |

## Monitoring Cache Performance

Get real-time cache statistics:

```python
# Get cache stats
stats = manager.get_cache_stats()

print(f"L1 Cache Hit Rate: {stats['l1_stats']['hit_rate_percent']}%")
print(f"L1 Avg Lookup Time: {stats['l1_stats']['avg_lookup_time_ms']}ms")
print(f"L1 Current Size: {stats['l1_stats']['current_size']}/{stats['l1_stats']['max_size']}")
print(f"L2 Cache Size: {stats['l2_size']} entries")

# Example output:
# {
#   "l1_enabled": True,
#   "l1_stats": {
#     "max_size": 10000,
#     "current_size": 2847,
#     "ttl_seconds": 60,
#     "hits": 8532,
#     "misses": 1241,
#     "sets": 1241,
#     "invalidations": 53,
#     "hit_rate_percent": 87.31,
#     "total_requests": 9773,
#     "avg_lookup_time_ms": 0.023
#   },
#   "l2_enabled": True,
#   "l2_size": 4521,
#   "l2_ttl_seconds": 300
# }

# Reset metrics for benchmarking
manager.reset_cache_stats()
```

## Cache Invalidation Strategy

The cache uses **precise invalidation** to minimize cache churn. When permissions change, only affected entries are invalidated:

### Invalidation Scenarios

| Change Type | L1 Invalidation | L2 Invalidation |
|-------------|-----------------|-----------------|
| Direct permission grant | Subject-object pair | Subject-object pair |
| Group membership change | All subject permissions | All subject permissions |
| Parent directory permission | All child paths | All child paths |
| Namespace config update | Clear all (conservative) | Clear object type |

### Example: Permission Grant

```python
# Grant alice read permission on /workspace/doc.txt
manager.rebac_write(
    subject=("agent", "alice"),
    relation="viewer-of",
    object=("file", "/workspace/doc.txt")
)

# Cache invalidation happens automatically:
# 1. L1: Invalidates (alice, *, /workspace/doc.txt)
# 2. L2: Invalidates (alice, *, /workspace/doc.txt)
# 3. Tracks write for adaptive TTL
```

## Adaptive TTL (Optional)

When enabled, the cache automatically adjusts TTL based on write frequency:

| Write Frequency | TTL Adjustment |
|-----------------|----------------|
| >10 writes/min | 10s (aggressive refresh) |
| 5-10 writes/min | 30s |
| 1-5 writes/min | 60s |
| <1 write/min | 300s (5 minutes) |

```python
# Enable adaptive TTL
manager = ReBACManager(
    engine=engine,
    enable_adaptive_ttl=True  # Adjust TTL dynamically
)
```

## Implementation Details

### L1 Cache (In-Memory)

- **Technology**: `cachetools.TTLCache` with LRU eviction
- **Thread-safety**: `threading.RLock` for concurrent access
- **Memory**: ~100 bytes per entry → 10k entries ≈ 1MB
- **Eviction**: TTL expiration + LRU when size limit reached

### L2 Cache (Database)

- **Table**: `rebac_check_cache`
- **Schema**:
  ```sql
  CREATE TABLE rebac_check_cache (
      cache_id TEXT PRIMARY KEY,
      tenant_id TEXT,
      subject_type TEXT,
      subject_id TEXT,
      permission TEXT,
      object_type TEXT,
      object_id TEXT,
      result INTEGER,  -- 0 or 1
      computed_at TIMESTAMP,
      expires_at TIMESTAMP
  );
  ```
- **Indexes**: Composite index on `(tenant_id, subject_type, subject_id, permission, object_type, object_id)`

### Cache Key Format

```
L1 Key: "{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}:{tenant_id}"

Example:
  "agent:alice:read:file:/workspace/doc.txt:default"
  "user:bob:write:memory:conv_123:tenant_acme"
```

## Best Practices

### When to Enable L1 Cache

✅ **Enable** (default):
- Read-heavy workloads
- Frequent permission checks on same files
- Directory listings
- Multi-tenant deployments

❌ **Disable** (set `enable_l1_cache=False`):
- Memory-constrained environments (<100MB RAM available)
- Write-heavy workloads (>1000 permission changes/min)
- Testing/debugging scenarios

### Tuning TTL

**Shorter L1 TTL (30-60s)** for:
- High write frequency environments
- Strict consistency requirements
- Smaller cache sizes

**Longer L1 TTL (120-300s)** for:
- Read-heavy workloads
- Stable permission hierarchies
- Larger cache sizes

### Monitoring Guidelines

Monitor these metrics in production:

1. **Hit Rate**: Target >80% for L1, >70% for L2
2. **Avg Lookup Time**: Should be <1ms for L1
3. **Cache Size**: Monitor to avoid eviction thrashing
4. **Invalidation Rate**: High rate may indicate cache churn

If hit rate < 70%:
- Increase cache size
- Increase TTL
- Review permission change patterns

## Migration Guide

### Before (Single L2 Cache)

```python
from nexus.core.rebac_manager import ReBACManager

manager = ReBACManager(
    engine=engine,
    cache_ttl_seconds=300  # L2 cache only
)
```

### After (Two-Level Cache)

```python
from nexus.core.rebac_manager import ReBACManager

# L1 cache is enabled by default!
manager = ReBACManager(
    engine=engine,
    enable_l1_cache=True,       # NEW: L1 cache
    l1_cache_size=10000,        # NEW: 10k entries
    l1_cache_ttl=60,            # NEW: 60s TTL
    cache_ttl_seconds=300,      # Existing L2 cache
    enable_metrics=True,        # NEW: Track stats
)

# Monitor performance
stats = manager.get_cache_stats()
print(f"L1 hit rate: {stats['l1_stats']['hit_rate_percent']}%")
```

### Breaking Changes

**None!** The changes are **fully backward compatible**:
- Default behavior enables L1 cache automatically
- Existing code works without modifications
- Old `cache_ttl_seconds` parameter still controls L2 cache

## Performance Benchmarks

Based on our analysis (see `PERMISSION_ARCHITECTURE_ANALYSIS.md`):

### Single File Read
- **Before**: 50-100ms (L2 cache miss)
- **After**: <1ms (L1 cache hit)
- **Improvement**: **50-100x faster**

### Directory Listing (100 files)
- **Before**: 1-2s (L2 cache miss)
- **After**: 10-20ms (L1 cache hit)
- **Improvement**: **50-100x faster**

### Permission Grant
- **Before**: 10-20ms
- **After**: 10-20ms (no change - writes go to both caches)

## Troubleshooting

### High Memory Usage

**Problem**: L1 cache consuming too much memory

**Solutions**:
1. Reduce `l1_cache_size` (e.g., 5000 instead of 10000)
2. Disable L1 cache: `enable_l1_cache=False`
3. Use shorter TTL to allow more aggressive eviction

### Low Hit Rate

**Problem**: L1 hit rate < 70%

**Solutions**:
1. Increase `l1_cache_size`
2. Increase `l1_cache_ttl`
3. Enable `enable_adaptive_ttl=True`
4. Check if write frequency is too high

### Stale Cache Results

**Problem**: Permission changes not reflected immediately

**Expected Behavior**:
- Cache invalidation is **automatic** and **precise**
- Invalidation happens **immediately** on permission changes
- Staleness only occurs within TTL window (60s for L1, 300s for L2)

**If seeing stale results**:
1. Check cache logs for invalidation events
2. Verify tenant isolation is working
3. Use shorter TTL if consistency is critical
4. Clear cache manually: `manager._l1_cache.clear()` (debug only)

## Future Enhancements

Potential improvements for future versions:

1. **Distributed L1 Cache**: Redis/Memcached for multi-instance deployments
2. **Smart Prefetching**: Preload related permissions during directory listings
3. **Cache Warming**: Pre-populate cache on server startup
4. **Advanced Metrics**: P50/P95/P99 latency percentiles
5. **Per-Tenant TTL**: Different TTL per tenant based on write patterns

## Related Documentation

- `PERMISSION_ARCHITECTURE_ANALYSIS.md`: Detailed architecture analysis
- `src/nexus/core/rebac_cache.py`: L1 cache implementation
- `src/nexus/core/rebac_manager.py`: ReBAC manager with two-level cache
- `tests/unit/core/test_rebac_cache.py`: Cache unit tests

## Support

For issues or questions:
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Documentation: https://docs.nexus.ai

---

**Version**: 1.0.0
**Last Updated**: 2025-11-08
**Author**: Nexus Team
