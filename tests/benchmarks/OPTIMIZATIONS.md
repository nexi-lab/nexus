# Performance Optimization Opportunities

Based on benchmark results and code analysis, here are concrete optimization opportunities ranked by impact.

## 🔴 High Impact Optimizations

### 1. **Batch Metadata Operations** (Addresses: 28x overhead for small files)

**Problem**: Writing 100 small files is 28x slower in Nexus due to per-file metadata overhead.

**Current bottleneck** (`src/nexus/storage/metadata_store.py:330`):
- Each `put()` call creates a new SQLite transaction
- Each write does 2+ database queries:
  - `SELECT` to check if file exists
  - `UPDATE` + `INSERT` for version history
  - Policy lookup and application

**Solution**: Add batch write API
```python
def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """Write multiple files in a single transaction."""
    with self.metadata.SessionLocal() as session:
        results = []
        for path, content in files:
            # All metadata operations in ONE transaction
            ...
        session.commit()
        return results
```

**Expected improvement**: 10-15x faster for small file batches

**Location to implement**:
- `src/nexus/core/nexus_fs_core.py` - add `write_batch()` method
- `src/nexus/storage/metadata_store.py` - add `put_batch()` method

---

### 2. **SQLite WAL Mode + Pragma Optimization** (Addresses: concurrent write failures)

**Problem**:
- Concurrent writes to same file cause `UNIQUE constraint` failures
- SQLite default mode has poor concurrency

**Current config** (`src/nexus/storage/metadata_store.py`):
```python
# No SQLite optimization pragmas set
```

**Solution**: Enable WAL mode and optimize pragmas
```python
def _configure_sqlite_for_performance(self):
    """Apply SQLite performance optimizations."""
    with self.engine.connect() as conn:
        # WAL mode: allows concurrent reads during writes
        conn.execute(text("PRAGMA journal_mode=WAL"))

        # Performance optimizations
        conn.execute(text("PRAGMA synchronous=NORMAL"))  # Faster commits
        conn.execute(text("PRAGMA cache_size=-64000"))   # 64MB cache
        conn.execute(text("PRAGMA temp_store=MEMORY"))   # In-memory temp tables
        conn.execute(text("PRAGMA mmap_size=268435456")) # 256MB memory-mapped I/O
        conn.commit()
```

**Expected improvement**:
- 2-3x faster writes
- Better concurrent write handling
- Reduced lock contention

**Location**: `src/nexus/storage/metadata_store.py:__init__`

---

### 3. **Content Cache for Read Operations** (Addresses: 4.5x read overhead for 10MB)

**Problem**: Every read does CAS lookup even for recently read files
- Read path: `metadata.get(path)` → `backend.read_content(hash)` → disk I/O

**Current state**: Only metadata is cached, not content!

**Solution**: Add LRU content cache
```python
class ContentCache:
    def __init__(self, max_size_mb: int = 256):
        self._cache = LRUCache(maxsize=max_size_mb * 1024 * 1024)  # Size-based LRU
        self._lock = threading.Lock()

    def get(self, content_hash: str) -> bytes | None:
        with self._lock:
            return self._cache.get(content_hash)

    def put(self, content_hash: str, content: bytes) -> None:
        with self._lock:
            # Only cache if content fits
            if len(content) <= self._cache.maxsize:
                self._cache[content_hash] = content
```

Then in `LocalBackend.read_content()`:
```python
def read_content(self, content_hash: str) -> bytes:
    # Check cache first
    cached = self.content_cache.get(content_hash)
    if cached is not None:
        return cached

    # Cache miss - read from disk
    content = content_path.read_bytes()
    self.content_cache.put(content_hash, content)
    return content
```

**Expected improvement**:
- **10-50x faster** for cached reads
- Especially impactful for AI agents re-reading same files

**Location**:
- Create `src/nexus/storage/content_cache.py`
- Integrate in `src/nexus/backends/local.py:read_content()`
- Add to `NexusFS.__init__()` with `enable_content_cache` flag

---

### 4. **Lazy Version History Creation** (Addresses: write overhead)

**Problem**: Every file write creates a version history entry
- This adds overhead even when versioning isn't needed

**Current**: `metadata_store.py:383-407` - always creates version entry

**Solution**: Make versioning opt-in per file or namespace
```python
def write(
    self,
    path: str,
    content: bytes,
    enable_versioning: bool = True,  # New parameter
) -> dict[str, Any]:
    ...
    if enable_versioning:
        # Create version history
    else:
        # Skip version history for performance
```

Or add namespace-level configuration:
```python
NamespaceConfig(
    prefix="/temp/",
    ...
    enable_versioning=False,  # No versioning for temp files
)
```

**Expected improvement**: 20-30% faster writes when versioning disabled

**Location**: `src/nexus/core/nexus_fs_core.py:write()`

---

## 🟡 Medium Impact Optimizations

### 5. **Reduce Permission Check Overhead**

**Problem**: Every write/read checks permissions (lines 213-231 in nexus_fs_core.py)

**Solution**: Cache permission check results
```python
# In MetadataCache
def get_permission_check(self, path: str, permission: Permission) -> bool | None:
    return self._permission_cache.get((path, permission))

def set_permission_check(self, path: str, permission: Permission, allowed: bool):
    self._permission_cache[(path, permission)] = allowed
```

**Expected improvement**: 10-15% faster for repeated access to same files

---

### 6. **Optimize Hash Computation for Large Files**

**Problem**: Computing SHA-256 for 10MB files adds overhead

**Current**: `backends/local.py` - always computes full hash

**Solution**: Incremental hashing with early abort for duplicates
```python
def write_content(self, content: bytes) -> str:
    # For large files, check first 4KB fingerprint
    if len(content) > 1_000_000:  # 1MB threshold
        fingerprint = hashlib.sha256(content[:4096]).hexdigest()[:16]
        if fingerprint in self._fingerprint_cache:
            # Likely duplicate - compute full hash
            full_hash = self._compute_hash(content)
            if self._hash_to_path(full_hash).exists():
                return full_hash  # Deduplicated!

    # Normal path for small files or non-duplicates
    return self._compute_hash(content)
```

**Expected improvement**: 20-40% faster for large duplicate files

---

### 7. **Connection Pooling for SQLite**

**Problem**: Each operation creates new session

**Solution**: Use connection pool
```python
from sqlalchemy.pool import StaticPool

engine = create_engine(
    db_url,
    poolclass=StaticPool,  # Reuse connections
    pool_size=10,
    max_overflow=20,
)
```

**Expected improvement**: 5-10% faster metadata operations

---

## 🟢 Low Impact (Nice to Have)

### 8. **Prefetch Metadata for Directory Listings**

When listing a directory, prefetch metadata for all files in one query instead of individual queries.

### 9. **Asynchronous Version History Creation**

Write version history in background thread to not block write operations.

### 10. **Compression for Large Text Files**

Compress content before CAS storage if mime_type is text/*

---

## Priority Implementation Roadmap

### Phase 1 (Immediate - Biggest Wins)
1. ✅ SQLite WAL mode + pragmas (30 min)
2. ✅ Content cache for reads (2-3 hours)
3. ✅ Batch write API (4-6 hours)

**Expected combined improvement**:
- Writes: 3-5x faster
- Reads: 10-20x faster (cached)
- Small file batches: 15-20x faster

### Phase 2 (Short Term)
4. Lazy version history (2-3 hours)
5. Permission check caching (2 hours)
6. Connection pooling (1 hour)

**Expected improvement**: Additional 30-40% speedup

### Phase 3 (Long Term)
7. Hash optimization for large files
8. Async version history
9. Compression

---

## Testing the Optimizations

After each optimization, run:
```bash
# Save baseline BEFORE optimization
bash scripts/run_benchmarks.sh save before-opt

# Implement optimization

# Compare AFTER optimization
bash scripts/run_benchmarks.sh save after-opt
pytest-benchmark compare before-opt after-opt
```

---

## Specific Code Locations for Implementation

| Optimization | File | Line(s) | Function |
|--------------|------|---------|----------|
| WAL mode | `storage/metadata_store.py` | ~100 | `__init__` |
| Content cache | `backends/local.py` | 194-204 | `read_content()` |
| Batch writes | `core/nexus_fs_core.py` | New | `write_batch()` |
| Permission cache | `storage/cache.py` | ~230 | Add new cache type |
| Lazy versioning | `core/nexus_fs_core.py` | 257 | `write()` parameter |

---

## Performance Targets After Optimization

| Operation | Current | Target | Optimization |
|-----------|---------|--------|--------------|
| 1KB write | 6.6 ms | **1.5 ms** | Batch + WAL |
| 100 small writes | 551 ms | **40 ms** | Batch API |
| 1MB read (cached) | 445 µs | **50 µs** | Content cache |
| 10MB read (cached) | 5.0 ms | **0.5 ms** | Content cache |
| Concurrent writes | Fails | Succeeds | WAL mode |

These targets would make Nexus **competitive with local FS** while retaining all metadata/versioning benefits!
