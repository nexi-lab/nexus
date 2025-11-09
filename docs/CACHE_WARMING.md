# Cache Warming for Cold Start Performance

## Problem: Cold Start Latency

When users first mount Nexus or connect to a server, the initial operations are slow due to **cold caches**:

| Operation | Cold Start | After Warming |
|-----------|------------|---------------|
| First `ls /workspace` | 100-500ms | 10-50ms |
| First file read | 50-100ms | <10ms |
| First permission check | 50-500ms | <1ms |

**Root Cause**: All caches are empty at startup:
- L1 permission cache (in-memory)
- L2 permission cache (database)
- Metadata cache (file/directory info)
- Directory listing cache

## Solution: Automatic Cache Warming

### For `nexus serve` ✅ **IMPLEMENTED**

Cache warming now happens automatically during server startup:

```bash
nexus serve --auth-type database
```

**Output:**
```
[green]Starting Nexus RPC server...[/green]
  Host: 0.0.0.0
  Port: 8080
  Backend: local
  Data Dir: /Users/you/.nexus/data
  Authentication: database
  Permissions: Enabled

[yellow]Warming caches...[/yellow] [green]✓[/green] (4 paths, 0.23s)
  L2 permission cache: +12 entries

[green]Press Ctrl+C to stop server[/green]
```

**What Gets Warmed:**

1. **Metadata cache**: Common directories (`/`, `/workspace`, `/tmp`, `/data`)
2. **Directory listing cache**: Pre-lists common directories
3. **Permission cache (L1 + L2)**: Populates permission checks for accessed paths

**Implementation**: `/Users/tafeng/nexus/src/nexus/cli/commands/server.py:811-869`

### For `nexus mount` (Future)

Cache warming for mount is **NOT YET IMPLEMENTED** but would be beneficial:

```bash
# Future API (not yet implemented)
nexus mount /workspace --warm-cache --warm-depth 2
```

**Why mount warming is harder:**
- FUSE mount blocks until successful
- Can't preload before mount completes
- Would need background thread after mount
- Less universal (only benefits single mount point)

**Recommendation**: Use `nexus serve` with automatic warming instead of direct FUSE mount for better performance.

## Performance Impact

### Server Startup (with warming)

| Phase | Time | What Happens |
|-------|------|--------------|
| NexusFS init | 40-250ms | Database + ReBAC manager setup |
| **Cache warming** | **100-300ms** | **Preload common paths** |
| Ready for requests | - | All caches warm |

**Total delay: 140-550ms** (acceptable for server startup)

### First Client Request (after warming)

| Operation | Without Warming | With Warming | Improvement |
|-----------|-----------------|--------------|-------------|
| `ls /workspace` | 100-500ms | 10-50ms | **2-10x faster** |
| Read file | 50-100ms | 5-20ms | **2.5-5x faster** |
| Permission check | 50-500ms | <1ms | **50-500x faster** |

### User Experience

**Before warming:**
```
$ nexus serve
Starting server... (250ms)
# Server ready

$ curl http://localhost:8080/api/nfs/list -d '{"path": "/workspace"}'
# First request: 300ms (cold cache)

$ curl http://localhost:8080/api/nfs/list -d '{"path": "/workspace"}'
# Second request: 10ms (warm cache)
```

**After warming:**
```
$ nexus serve
Starting server... (250ms)
Warming caches... ✓ (200ms)
# Server ready (total: 450ms)

$ curl http://localhost:8080/api/nfs/list -d '{"path": "/workspace"}'
# First request: 15ms (warm cache!)

$ curl http://localhost:8080/api/nfs/list -d '{"path": "/workspace"}'
# Second request: 10ms (warm cache)
```

## Implementation Details

### What Gets Cached

1. **Metadata Cache** (`SQLAlchemyMetadataStore`)
   - File/directory metadata
   - Existence checks
   - Path information
   - **Size**: 512-1024 entries
   - **TTL**: 300 seconds (5 minutes)

2. **Permission Cache - L1** (`ReBACPermissionCache`)
   - In-memory permission check results
   - **Size**: 10,000 entries (~1-2MB)
   - **TTL**: 60 seconds
   - **Hit time**: <1ms

3. **Permission Cache - L2** (Database table: `rebac_check_cache`)
   - Persistent permission check results
   - **TTL**: 300 seconds (5 minutes)
   - **Hit time**: 5-10ms

4. **Directory Listing Cache** (`MetadataCache`)
   - Cached directory contents
   - **Size**: 128 entries
   - **TTL**: 300 seconds

### Warming Strategy

**Paths warmed** (in order):
1. `/` - Root directory
2. `/workspace` - Common workspace
3. `/tmp` - Temporary files
4. `/data` - Data directory

**For each path:**
```python
if nx.exists(path):
    # 1. Warm metadata cache
    nx.get_metadata(path)  # Populates metadata cache

    # 2. Warm listing cache
    nx.list(path, recursive=False)  # Populates listing cache

    # 3. Permission checks happen automatically
    # (triggered by exists/get_metadata/list operations)
```

**Non-blocking & Best-effort:**
- Continues even if some paths don't exist
- Doesn't fail server startup if warming fails
- Takes 100-300ms total (acceptable overhead)

## Configuration

### Default Behavior

Cache warming is **enabled by default** with no configuration needed:

```python
nexus serve  # Automatically warms caches
```

### Disabling Cache Warming

Currently, cache warming is **always enabled** for optimal performance. To disable it (not recommended):

1. Comment out lines 811-869 in `src/nexus/cli/commands/server.py`
2. Or set a feature flag (future enhancement)

### Adding Custom Paths

To warm additional paths, modify `common_paths` in `server.py:831`:

```python
# Before (default)
common_paths = ["/", "/workspace", "/tmp", "/data"]

# After (custom paths)
common_paths = ["/", "/workspace", "/tmp", "/data", "/projects", "/shared"]
```

## Monitoring

### Cache Stats API

Check cache performance after warming:

```python
from nexus import get_filesystem

nx = get_filesystem()
stats = nx._rebac_manager.get_cache_stats()

print(f"L1 Cache Size: {stats['l1_stats']['current_size']}")
print(f"L2 Cache Size: {stats['l2_size']}")
print(f"L1 Hit Rate: {stats['l1_stats']['hit_rate_percent']}%")
```

### Server Output

The server shows warming progress:

```
Warming caches... ✓ (4 paths, 0.23s)
  L2 permission cache: +12 entries
```

This tells you:
- **4 paths warmed**: Successfully preloaded 4 directories
- **0.23s**: Time spent warming caches
- **+12 entries**: Number of permission checks cached

## Comparison: Mount vs Serve

| Aspect | `nexus mount` | `nexus serve` |
|--------|---------------|---------------|
| **Warming implemented** | ❌ No | ✅ Yes |
| **Who benefits** | Single mount point | All clients |
| **When to warm** | After mount (tricky) | Before accepting requests |
| **Overhead** | N/A | 100-300ms at startup |
| **Recommendation** | Use serve instead | ✅ Preferred |

### Recommendation

**Use `nexus serve` for better performance:**

```bash
# Server with cache warming (recommended)
nexus serve --auth-type database

# Then mount from clients
nexus mount http://localhost:8080 /mnt/nexus --api-key <key>
```

This ensures:
- ✅ Caches warmed once at server startup
- ✅ All clients benefit from warm caches
- ✅ No per-mount warming overhead
- ✅ Better multi-user performance

## Future Enhancements

Potential improvements:

1. **Configurable warming depth**
   ```bash
   nexus serve --warm-depth 2  # Recurse 2 levels deep
   ```

2. **Custom warming paths**
   ```bash
   nexus serve --warm-paths /workspace,/projects,/data
   ```

3. **Smart warming based on usage history**
   - Track most-accessed paths
   - Automatically warm hot paths
   - Adaptive warming based on workload

4. **Mount-time warming** (future)
   ```bash
   nexus mount /workspace --warm-cache --warm-depth 1
   ```

5. **Background re-warming**
   - Periodically refresh caches before TTL expires
   - Prevent cache staleness

## Troubleshooting

### Warming Takes Too Long

**Problem**: Cache warming adds >500ms to startup

**Solutions:**
1. Reduce number of paths to warm
2. Remove non-existent paths from `common_paths`
3. Check backend latency (GCS might be slow)

### First Request Still Slow

**Problem**: First request is still slow after warming

**Possible causes:**
1. **Different path accessed**: Warming only covers common paths
   - Solution: Add your paths to `common_paths`

2. **Large files**: Parsing large documents takes time
   - Solution: This is expected, only metadata is warmed

3. **Permission graph traversal**: Complex permissions take time
   - Solution: L1 cache helps on subsequent checks

### No Cache Stats Shown

**Problem**: "L2 permission cache: +X entries" not displayed

**Cause**: Server started without authentication (no ReBAC manager)

**Solution**: Use `--auth-type database` to enable permissions:
```bash
nexus serve --auth-type database --init
```

## Related Documentation

- `REBAC_CACHE.md` - Two-level permission cache architecture
- `PERMISSION_ARCHITECTURE_ANALYSIS.md` - Detailed permission system analysis
- `src/nexus/core/rebac_cache.py` - L1 cache implementation
- `src/nexus/cli/commands/server.py:811-869` - Cache warming implementation

## Support

For issues or questions:
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Documentation: https://docs.nexus.ai

---

**Version**: 1.0.0
**Last Updated**: 2025-11-08
**Status**: ✅ Implemented for `nexus serve`, pending for `nexus mount`
