# Plan: #1392 — CachingBackendWrapper for Transparent Storage Caching

## Approved Decisions

| # | Section | Decision | Choice |
|---|---------|----------|--------|
| 1 | Architecture | File location | **1A**: `src/nexus/cache/backend_wrapper.py` |
| 2 | Architecture | Composition pattern | **2A**: Explicit delegation (subclass Backend, delegate to self._inner) |
| 3 | Architecture | Cache layers | **3A**: Reuse ContentCache (L1) + CacheStoreABC (L2) |
| 4 | Architecture | Invalidation strategy | **4C**: Configurable enum (write-through / write-around), default write-around |
| 5 | Code Quality | Sync/Async bridge | **5C**: Sync wrapper, async L2 population via background task |
| 6 | Code Quality | Cache key scope | **6A**: CAS operations only (content_hash-based) |
| 7 | Code Quality | Delegation boilerplate | **7B**: Hybrid — explicit for cached methods, `__getattr__` for pass-through |
| 8 | Code Quality | Error handling | **8C**: try/except + OTel error counter (cache never breaks hot path) |
| 9 | Tests | Test location | **9A**: `tests/unit/cache/test_backend_wrapper.py` |
| 10 | Tests | Test strategy | **10C**: Conformance tests (parametrized raw vs wrapped) + cache behavior tests |
| 11 | Tests | Mock backend | **11A**: Build minimal in-memory MockBackend (~100 lines) |
| 12 | Tests | E2E scope | **12C**: Correctness + performance + permissions (FastAPI + ReBAC) |
| 13 | Performance | Lock strategy | **13A**: Accept current ContentCache locking (microsecond hold times) |
| 14 | Performance | Thundering herd | **14A**: Accept it (CAS is idempotent), TODO for singleflight upgrade |
| 15 | Performance | Memory budget | **15B**: Wrapper owns its own ContentCache (128MB default, configurable) |
| 16 | Performance | L2 scope | **16C**: Content only in L2 (read_content results), skip metadata |

## Design Principles (from LEGO Architecture)

1. **Brick independence** — Zero imports from other bricks. Only import Protocol interfaces and kernel primitives.
2. **Pattern E: Decorator/Wrapper** — Wraps any `Backend` to add caching. Same interface, transparent to consumers.
3. **Composition over inheritance** — Delegation, not mixins.
4. **Immutability** — All config is frozen dataclass. Cache entries are not mutated after creation.
5. **Graceful degradation** — Cache failures fall through to inner backend silently.

## File Structure

### New Files

```
src/nexus/cache/
    backend_wrapper.py          # CachingBackendWrapper — main implementation (~250 lines)

tests/unit/cache/
    __init__.py
    test_backend_wrapper.py     # Conformance + cache behavior tests (~400 lines)
    mock_backend.py             # In-memory MockBackend for testing (~100 lines)

tests/e2e/
    test_caching_wrapper_e2e.py # E2E: FastAPI + permissions + performance (~150 lines)
```

### Modified Files

```
src/nexus/cache/__init__.py     # Export CachingBackendWrapper, CacheStrategy
src/nexus/cache/factory.py      # Add create_caching_wrapper() method to CacheFactory
```

## Core Data Types

```python
from enum import Enum
from dataclasses import dataclass

class CacheStrategy(Enum):
    """Cache write strategy."""
    WRITE_AROUND = "write_around"    # Default: invalidate on write, populate on read miss
    WRITE_THROUGH = "write_through"  # Write to cache on every write

@dataclass(frozen=True)
class CacheWrapperConfig:
    """Immutable configuration for CachingBackendWrapper."""
    strategy: CacheStrategy = CacheStrategy.WRITE_AROUND
    l1_max_size_mb: int = 128          # L1 ContentCache memory budget
    l1_compression_threshold: int = 1024  # Bytes; smaller content not compressed
    l2_enabled: bool = True            # Enable L2 (CacheStoreABC) background population
    l2_ttl_seconds: int = 3600         # L2 TTL (1 hour default)
    l2_key_prefix: str = "cbw"         # L2 key prefix for namespace isolation
    metrics_enabled: bool = True       # Enable OTel cache hit/miss/error counters
```

## CachingBackendWrapper API

```python
class CachingBackendWrapper(Backend):
    """Transparent caching decorator for any Backend implementation.

    Wraps an inner Backend and adds two-layer caching:
    - L1: In-memory ContentCache (sync, fast, process-local)
    - L2: CacheStoreABC/Dragonfly (async background population, distributed)

    Follows LEGO Architecture Pattern E (Decorator/Wrapper Composition).
    All Backend operations pass through transparently. Only CAS read operations
    are cached (read_content, batch_read_content). Writes invalidate or populate
    cache based on the configured CacheStrategy.

    Cache failures are silently swallowed — inner backend is always the fallback.
    """

    def __init__(
        self,
        inner: Backend,
        config: CacheWrapperConfig | None = None,
        cache_store: CacheStoreABC | None = None,  # L2 (optional, for distributed caching)
    ) -> None: ...

    # === Explicitly delegated + cached methods ===

    @property
    def name(self) -> str:
        return f"cached({self._inner.name})"

    def read_content(self, content_hash, context=None) -> HandlerResponse[bytes]:
        # L1 check → L2 check → inner.read_content() → populate L1 → schedule L2 population
        ...

    def write_content(self, content, context=None) -> HandlerResponse[str]:
        # inner.write_content() → write-around: invalidate L1/L2
        #                       → write-through: populate L1, schedule L2
        ...

    def delete_content(self, content_hash, context=None) -> HandlerResponse[None]:
        # inner.delete_content() → invalidate L1 + L2
        ...

    def content_exists(self, content_hash, context=None) -> HandlerResponse[bool]:
        # L1 has it? → True. Otherwise → inner.content_exists()
        ...

    def batch_read_content(self, content_hashes, context=None) -> dict[str, bytes | None]:
        # Check L1 for all → read uncached from inner → populate L1
        ...

    def get_content_size(self, content_hash, context=None) -> HandlerResponse[int]:
        # Delegate to inner (not cached — metadata is cheap)
        ...

    def get_ref_count(self, content_hash, context=None) -> HandlerResponse[int]:
        # Delegate to inner (not cached — ref count changes)
        ...

    # === Pure delegation via __getattr__ ===
    # mkdir, rmdir, is_directory, list_dir, connect, disconnect, check_connection,
    # stream_content, write_stream, get_file_info, get_object_type, get_object_id
    def __getattr__(self, name):
        return getattr(self._inner, name)

    # === Explicit property delegation ===
    @property
    def user_scoped(self) -> bool: return self._inner.user_scoped
    @property
    def is_connected(self) -> bool: return self._inner.is_connected
    @property
    def thread_safe(self) -> bool: return self._inner.thread_safe
    @property
    def supports_rename(self) -> bool: return self._inner.supports_rename
    # ... (all 8 capability properties delegated explicitly)

    # === Cache management ===
    def get_cache_stats(self) -> dict[str, Any]:
        """Return L1 stats + L2 health + hit/miss counters."""
        ...

    def clear_cache(self) -> None:
        """Clear both L1 and L2 caches."""
        ...
```

## L2 Background Population Design

```python
# Inside CachingBackendWrapper

def _schedule_l2_populate(self, content_hash: str, content: bytes) -> None:
    """Fire-and-forget L2 population in the event loop (if running)."""
    if not self._config.l2_enabled or self._cache_store is None:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(self._l2_populate(content_hash, content))
    except RuntimeError:
        # No running event loop — skip L2 (pure sync context)
        pass

async def _l2_populate(self, content_hash: str, content: bytes) -> None:
    """Async L2 cache population. Errors logged, never raised."""
    try:
        key = f"{self._config.l2_key_prefix}:{content_hash}"
        await self._cache_store.set(key, content, ttl=self._config.l2_ttl_seconds)
    except Exception as e:
        self._record_cache_error("l2_populate", e)

def _l2_get_sync(self, content_hash: str) -> bytes | None:
    """Synchronous L2 read — only called on L1 miss."""
    if not self._config.l2_enabled or self._cache_store is None:
        return None
    try:
        loop = asyncio.get_running_loop()
        key = f"{self._config.l2_key_prefix}:{content_hash}"
        # Use a future with timeout to avoid blocking indefinitely
        future = asyncio.run_coroutine_threadsafe(
            self._cache_store.get(key), loop
        )
        return future.result(timeout=0.1)  # 100ms timeout for L2 reads
    except Exception:
        return None  # L2 miss or error — fall through to inner backend
```

## OTel Metrics Integration

```python
# Lazy-loaded OTel counters (follow existing telemetry.py pattern)

def _get_metrics(self):
    """Lazy-init OTel counters. Returns None if OTel disabled."""
    if self._metrics is not None:
        return self._metrics
    try:
        from nexus.server.telemetry import is_telemetry_enabled
        if not is_telemetry_enabled():
            return None
        from opentelemetry import metrics
        meter = metrics.get_meter("nexus.cache.backend_wrapper")
        self._metrics = {
            "l1_hits": meter.create_counter("cache.backend_wrapper.l1.hits"),
            "l1_misses": meter.create_counter("cache.backend_wrapper.l1.misses"),
            "l2_hits": meter.create_counter("cache.backend_wrapper.l2.hits"),
            "l2_misses": meter.create_counter("cache.backend_wrapper.l2.misses"),
            "cache_errors": meter.create_counter("cache.backend_wrapper.errors"),
            "invalidations": meter.create_counter("cache.backend_wrapper.invalidations"),
        }
        return self._metrics
    except Exception:
        return None
```

## Implementation Steps (TDD Order)

### Phase 1: Test Infrastructure

**Step 1**: Create `tests/unit/cache/__init__.py` and `tests/unit/cache/mock_backend.py`
- `MockBackend(Backend)`: in-memory dict-based implementation
- Implements all abstract methods: write_content, read_content, delete_content, content_exists, get_content_size, get_ref_count, mkdir, rmdir, is_directory
- ~100 lines, purpose-built for testing

### Phase 2: Write Tests FIRST (RED)

**Step 2**: `tests/unit/cache/test_backend_wrapper.py` — Conformance tests
- Parametrized fixture: `@pytest.fixture(params=["raw", "wrapped"])`
- ~15 tests proving transparency:
  - write_content returns same hash
  - read_content returns same bytes
  - delete_content succeeds/fails same way
  - content_exists returns same bool
  - batch_read_content returns same dict
  - get_content_size returns same int
  - get_ref_count returns same int
  - mkdir/rmdir/is_directory/list_dir pass through
  - name property returns wrapped name
  - capability properties delegate correctly

**Step 3**: Cache behavior tests
- ~20 tests for caching logic:
  - L1 hit: second read_content is from cache (mock inner to track calls)
  - L1 miss + populate: first read populates L1
  - Write-around: write_content invalidates L1, doesn't populate
  - Write-through: write_content populates L1
  - delete_content invalidates L1
  - batch_read_content: mix of L1 hits and inner reads
  - Cache error fallback: L1 corrupted → inner read succeeds
  - get_cache_stats returns hit/miss counts
  - clear_cache empties L1
  - Config: custom l1_max_size_mb respected
  - Config: strategy enum switches behavior
  - Content too large for cache: passes through without caching
  - __getattr__ delegates non-cached methods

**Step 4**: Run tests — they should all FAIL (RED)

### Phase 3: Implementation (GREEN)

**Step 5**: `src/nexus/cache/backend_wrapper.py`
- `CacheStrategy` enum
- `CacheWrapperConfig` frozen dataclass
- `CachingBackendWrapper(Backend)` class
- ~250 lines
- Run Step 2+3 tests: should PASS (GREEN)

**Step 6**: Update `src/nexus/cache/__init__.py`
- Export `CachingBackendWrapper`, `CacheStrategy`, `CacheWrapperConfig`

**Step 7**: Add `create_caching_wrapper()` to `CacheFactory`
- Factory method that wires wrapper with existing CacheStoreABC
- ~20 lines added to factory.py

### Phase 4: L2 Integration

**Step 8**: Add L2 background population tests
- Test L2 population fires on L1 miss (mock CacheStoreABC)
- Test L2 read on L1 miss (InMemoryCacheStore)
- Test L2 error doesn't break read path
- Test L2 disabled config skips L2
- ~10 additional tests

**Step 9**: Implement L2 background population in backend_wrapper.py
- `_schedule_l2_populate()`, `_l2_populate()`, `_l2_get_sync()`
- Run Step 8 tests: GREEN

### Phase 5: OTel Metrics

**Step 10**: Add OTel metrics tests
- Test metrics increment on L1 hit/miss
- Test metrics increment on cache error
- Test metrics disabled when OTel disabled
- ~5 additional tests

**Step 11**: Implement OTel metrics in backend_wrapper.py
- Lazy-loaded counters via existing telemetry module
- ~30 lines
- Run Step 10 tests: GREEN

### Phase 6: E2E Tests

**Step 12**: `tests/e2e/test_caching_wrapper_e2e.py`
- Start FastAPI with permissions enabled
- Create zone, write files, read files
- Assert: second read faster than first (cache hit)
- Assert: cache hit logged in server output
- Assert: permissions still enforced (unauthorized read fails even if cached)
- Assert: cache invalidated on delete
- ~150 lines

**Step 13**: Run full e2e test with `nexus serve`
- Validate logs for cache hit/miss messages
- Validate no performance regression (read latency)
- Validate permissions not bypassed

### Phase 7: Cleanup & Verification

**Step 14**: Run full test suite
- `uv run pytest tests/unit/cache/ -v`
- `uv run pytest tests/e2e/test_caching_wrapper_e2e.py -v`
- Verify no regressions in existing backend tests

**Step 15**: Review code with code-reviewer agent
- Check for DRY violations, error handling completeness, type safety

## Key Edge Cases to Test

1. **Empty content** — write_content(b"") → cache correctly handles zero-length
2. **Large content** — content > l1_max_size_mb → passes through without caching
3. **Concurrent reads** — same hash read from multiple threads → all succeed
4. **Inner backend error** — read_content fails → cache not populated, error propagated
5. **Cache full** — L1 eviction works correctly under wrapper
6. **Strategy switch** — WRITE_THROUGH vs WRITE_AROUND produces different cache state
7. **No event loop** — L2 gracefully skipped in pure sync context
8. **NullCacheStore L2** — L2 operations are no-ops (graceful degradation)

## Deferred to Follow-up Issues

- **Thundering herd protection** — Singleflight/coalescing lock (Phase 2 if profiling shows need)
- **Circuit breaker** — Disable cache after N consecutive failures
- **Directory operation caching** — list_dir, is_directory (complex invalidation)
- **Cache warming** — Pre-populate cache on startup from frequently accessed files
- **Event-based distributed invalidation** — CacheStoreABC publish/subscribe for multi-node

## Estimated Scope

| Component | Lines (approx) | Tests (approx) |
|-----------|----------------|-----------------|
| CachingBackendWrapper | ~250 | — |
| CacheStrategy + CacheWrapperConfig | ~30 | — |
| MockBackend (test infra) | ~100 | — |
| Conformance tests | — | ~15 |
| Cache behavior tests | — | ~20 |
| L2 integration tests | — | ~10 |
| OTel metrics tests | — | ~5 |
| E2E tests | — | ~10 |
| CacheFactory update | ~20 | — |
| **Total** | ~400 new | ~60 tests |
