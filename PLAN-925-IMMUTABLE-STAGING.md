# Plan: #925 Immutable Staging Writes — Eliminate Lock Contention

## Summary

Issue #925 proposed ClickHouse-style staging tables for PostgreSQL metadata, but the
architecture has evolved since filing (redb replaced SQLAlchemy, CASBlobStore already
implements lock-free blob writes). We scope to **CAS blob store improvements** + code
quality fixes + test hardening. The PG staging table is no longer needed.

---

## Decisions (from interactive review)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Scope | CAS blob store only (redb solved PG contention) |
| 2 | Encapsulation | Add `meta_lock()` context manager to CASBlobStore |
| 3 | Read DRY | Delegate async_local reads to CASBlobStore.read_blob() |
| 4 | Immutability | Make ChunkInfo/ChunkedReference frozen + tuple |
| 5 | Dead code | Remove old non-CAS code paths in chunked_storage.py |
| 6 | Race fix | Use release() atomically in _delete_chunked |
| 7 | Read verify | Add optional `verify` param to CASBlobStore.read_blob() |
| 8 | Typing | Add TypeVar to cas_retry |
| 9 | Edge tests | Add release() edge case tests |
| 10 | Assertion | Fix concurrent test to assert exact ref_count |
| 11 | Integration | Add chunked + CAS integration test |
| 12 | E2E | Verify fixture + run with permissions enabled |
| 13 | Bloom | Keep as-is (1s startup acceptable) |
| 15 | Parallel delete | Parallelize chunk releases in _delete_chunked |
| 16 | fsync | Make fsync configurable (default: True) |

---

## Implementation Phases

### Phase 1: CASBlobStore Improvements (cas_blob_store.py)

1. **Add TypeVar to cas_retry** (#8)
   - `T = TypeVar("T")` + `Callable[[], T] -> T`

2. **Add `meta_lock()` context manager** (#2)
   ```python
   @contextmanager
   def meta_lock(self, content_hash: str) -> Iterator[None]:
       lock = self._meta_locks.acquire_for(content_hash)
       with lock:
           yield
   ```

3. **Add `verify` param to read_blob()** (#7)
   ```python
   def read_blob(self, content_hash: str, *, verify: bool = False) -> bytes:
       ...
       if verify:
           actual = hash_content(content)
           if actual != content_hash:
               raise BackendError(...)
   ```

4. **Make fsync configurable** (#16)
   - Add `fsync_blobs: bool = True` to `__init__`
   - Use in `write_blob()`: `if self._fsync_blobs: os.fsync(tmp.fileno())`

### Phase 2: Immutability (chunked_storage.py)

5. **Make ChunkInfo frozen** (#4)
   - `@dataclass(frozen=True, slots=True)`

6. **Make ChunkedReference frozen** (#4)
   - `@dataclass(frozen=True, slots=True)`
   - `chunks: tuple[ChunkInfo, ...]` instead of `list[ChunkInfo]`
   - Update `from_dict` / `from_json` to produce tuples
   - Update `_write_chunked` to build tuple

### Phase 3: Dead Code Removal (chunked_storage.py)

7. **Remove old non-CAS code paths** (#5)
   - Remove all `if hasattr(self, "_cas"):` branches
   - Keep only the CAS path in: `_write_single_chunk`, `_write_chunked_manifest`,
     `_delete_chunk_ref`, `_delete_chunked`, `_is_chunked_content`, `_get_content_size_chunked`
   - Remove fallback `_read_metadata` / `_write_metadata` / `_get_meta_path` abstract stubs
     if no longer needed

8. **Fix _delete_chunked race** (#6)
   - Read manifest BEFORE releasing
   - Call `self._cas.release()` for manifest
   - If returns True (deleted), parallelize chunk releases using ThreadPoolExecutor

9. **Parallelize chunk releases** (#15)
   - In `_delete_chunked`, use ThreadPoolExecutor for chunk unreferences

10. **Use `meta_lock()` context manager** (#2)
    - Replace `self._cas._meta_locks.acquire_for()` with `self._cas.meta_lock()`

### Phase 4: Async Local Backend (async_local.py)

11. **Delegate reads to CASBlobStore** (#3, #7)
    - Replace manual retry loop in `read_content()` with `self._cas.read_blob(verify=True)`
    - Remove duplicate hash verification code
    - Keep cache population logic

### Phase 5: Tests

12. **Add release() edge case tests** (#9)
    - `test_release_nonexistent_hash` — release on never-stored hash
    - `test_double_release` — release twice on ref_count=1
    - `test_release_after_full_release` — release after blob already deleted

13. **Fix concurrent assertion** (#10)
    - `test_concurrent_store_and_release`: assert `meta.ref_count == NUM_THREADS`
      (50 initial + 25 stores - 25 releases = 50)

14. **Add chunked + CAS integration test** (#11)
    - Test >16MB file through LocalBackend
    - Verify manifest stored in CAS with correct chunk metadata
    - Verify read-back reassembles correctly
    - Test concurrent chunked writes

15. **Verify E2E fixture + run with permissions** (#12)
    - Check conftest for test_app fixture
    - Ensure permissions are enabled
    - Run E2E test suite
    - Validate logs for lock contention issues

### Phase 6: Lint + Format + Final Validation

16. Run `ruff check . && ruff format --check .`
17. Run full unit test suite for modified files
18. Run E2E test with FastAPI + permissions enabled
19. Check for performance regression (timing assertions in tests)

---

## Files Modified

| File | Changes |
|------|---------|
| `src/nexus/backends/cas_blob_store.py` | TypeVar, meta_lock(), verify, fsync config |
| `src/nexus/backends/chunked_storage.py` | Frozen dataclasses, remove old paths, fix race, parallel delete |
| `src/nexus/backends/async_local.py` | Delegate reads to CASBlobStore |
| `src/nexus/backends/local.py` | Pass fsync config to CASBlobStore |
| `tests/unit/backends/test_cas_blob_store.py` | Edge case tests |
| `tests/unit/backends/test_cas_concurrent.py` | Fix assertion |
| `tests/unit/backends/test_local_backend.py` | Chunked + CAS integration test |
| `tests/e2e/test_cas_lockfree_e2e.py` | Permission-enabled E2E |

## Risks

- **Low**: Removing old chunked paths could break hypothetical external subclasses.
  Mitigation: No external consumers of ChunkedStorageMixin exist.
- **Low**: Changing ChunkedReference.chunks from list to tuple could break
  serialization. Mitigation: `to_dict()` works identically on tuples.
- **Low**: fsync=False could cause data loss on crash. Mitigation: Default is True,
  opt-in only.
