# Backend Storage Architecture (#1323, #1396, #1397)

**Task**: #1323 (CAS x Backend orthogonal composition)
**Depends on**: #1318 (sys_read/sys_write POSIX alignment — merged)
**Blocks**: #1396 (ObjectStoreABC addressing-agnostic refactor), #1397 (Hot/cold WAL write path)
**Status**: V0 design complete. #1323 merged. Migration pending.

---

## 1. Problem: Three Coexisting Backend Architectures

After PR #2738 merged the CAS x Backend composition (#1323), we have three overlapping
backend architectures coexisting in the codebase:

| Architecture | Files | Lines | Status |
|---|---|---|---|
| **New (#1323)**: `CASBackend` + `PathBackend` + `BlobTransport` | `cas_backend.py`, `path_backend.py`, `blob_transport.py` | 1,066 | Active — cloud backends migrated |
| **Old monolith**: `LocalBackend` + `CASBlobStore` + `ChunkedStorageMixin` | `local.py`, `cas_blob_store.py`, `chunked_storage.py` | 2,106 | Legacy — local storage only |
| **Old passthrough**: `PassthroughBackend` | `passthrough.py` | 527 | Legacy — inotify pointer files |

**The problem:** `LocalBackend` (966L) is a monolith that bundles CAS addressing, CDC
chunking, Bloom filter, StripeLock, and ContentCache into one class. It cannot compose
with the new `BlobTransport` abstraction. Meanwhile, `PassthroughBackend` (527L)
reimplements path-based storage from scratch instead of using `PathBackend` + transport.

**Target state:** Delete all three legacy files (~2,060 lines), replace with
`CASBackend(LocalBlobTransport)` and `PathBackend(LocalBlobTransport)`.

---

## 2. WHERE x HOW: Orthogonal Composition Model

PR #1323 established the principle: **transport** (WHERE blobs live) and **addressing**
(HOW blobs are identified) are orthogonal axes.

```
                     Transport (WHERE)
                     Local       GCS         S3         Azure(future)
                   +-----------+-----------+-----------+--------------+
Addressing   CAS   | Local+CAS | GCS+CAS   | S3+CAS   | Azure+CAS    |
(HOW)              |           |           |           |              |
             Path  | Local+Path| GCS+Path  | S3+Path   | Azure+Path   |
                   +-----------+-----------+-----------+--------------+
```

**Addressing** (CASBackend vs PathBackend) decides how content is identified:

| Axis | CAS Addressing | Path Addressing |
|---|---|---|
| Identity | BLAKE3 hash of content | User-supplied file path |
| Dedup | Automatic — same content = same key | None — each path is independent |
| Ref counting | Yes — delete decrements, GC at zero | No — always 1 |
| Use case | Local storage, content-addressed archives | Cloud connectors, external sync |

**Transport** (BlobTransport) decides where bytes physically live:

| Transport | Medium | Atomicity | Latency |
|---|---|---|---|
| `LocalBlobTransport` (new) | Local filesystem | `os.replace()` | ~50us |
| `GCSBlobTransport` | Google Cloud Storage | Server-side | ~50ms |
| `S3BlobTransport` | Amazon S3 | Server-side | ~80ms |

### 2.1 BlobTransport Protocol (9 methods)

From `backends/blob_transport.py` (140 lines):

```python
@runtime_checkable
class BlobTransport(Protocol):
    transport_name: str
    def put_blob(self, key: str, data: bytes, content_type: str = "") -> str | None: ...
    def get_blob(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]: ...
    def delete_blob(self, key: str) -> None: ...
    def blob_exists(self, key: str) -> bool: ...
    def get_blob_size(self, key: str) -> int: ...
    def list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]: ...
    def copy_blob(self, src_key: str, dst_key: str) -> None: ...
    def create_directory_marker(self, key: str) -> None: ...
    def stream_blob(self, key, chunk_size=8192, version_id=None) -> Iterator[bytes]: ...
```

Linux analogy: `BlobTransport` is the **block device driver** (ext4 doesn't care if
the disk is SSD or NVMe). `CASBackend`/`PathBackend` are the **filesystem layer**
(ext4 vs FAT32 — different addressing, same block device interface).

---

## 3. CRUD Behavior Matrix

Each WHERE x HOW combination produces different CRUD semantics:

### 3.1 CAS + LocalBlobTransport (replaces LocalBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob("cas/{h[:2]}/{h[2:4]}/{h}", content)` -> increment ref_count in `.meta` sidecar |
| **Read** | `get_blob("cas/{h[:2]}/{h[2:4]}/{h}")` -> verify hash (optional) -> return bytes |
| **Delete** | Decrement ref_count in `.meta` -> if zero, `delete_blob()` + cleanup empty dirs |
| **Exists** | Bloom filter fast-miss check -> `blob_exists()` on miss |
| **Stream** | `stream_blob()` with 64KB chunks (local seek-based) |

Enhancements over cloud CAS: Bloom filter, StripeLock for metadata coordination,
CDC chunking for large files, ContentCache for hot reads.

### 3.2 CAS + GCSBlobTransport (current GCSBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob(key, content)` -> write JSON metadata sidecar |
| **Read** | `get_blob(key)` -> return bytes |
| **Delete** | Decrement ref_count in metadata -> if zero, `delete_blob()` |
| **Exists** | `blob_exists()` — network round-trip |
| **Stream** | `stream_blob()` via GCS streaming download |

No Bloom, no StripeLock (cloud operations are server-side atomic), no CDC.

### 3.3 Path + LocalBlobTransport (replaces PassthroughBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob(backend_path, content)` -> return hash for metadata |
| **Read** | `get_blob(backend_path)` -> return bytes |
| **Delete** | `delete_blob(backend_path)` — immediate removal |
| **Exists** | `blob_exists(backend_path)` — `os.path.exists()` |
| **Stream** | `stream_blob(backend_path)` with seek-based chunked reads |

No ref counting (always 1), no dedup. OS-native paths for inotify/fswatch compatibility.

### 3.4 Path + GCSBlobTransport / S3BlobTransport (current connectors)

| Op | Behavior |
|---|---|
| **Write** | `put_blob(backend_path, content)` -> return version_id or hash |
| **Read** | `get_blob(backend_path, version_id=...)` -> return bytes |
| **Delete** | `delete_blob(backend_path)` |
| **Exists** | `blob_exists(backend_path)` — network round-trip |
| **Stream** | `stream_blob()` via cloud streaming download |

Supports versioning (`version_id`), bulk download, signed URLs. No CAS semantics.

---

## 4. Feature Migration: LocalBackend -> CASBackend

`LocalBackend` (966L) bundles five optimization features that must migrate to
`CASBackend` as optional DI parameters:

| Feature | Source | Lines | Purpose | Cloud-applicable? |
|---|---|---|---|---|
| **BloomFilter** | `nexus_fast` (Rust) | ~30L init | Fast negative lookup. Skip disk I/O for non-existent hashes. | No — network latency dominates |
| **CDC** | `ChunkedStorageMixin` | 573L | Content-defined chunking for files >= 16MB. Dedup at chunk level. | Future — GCS/S3 multipart |
| **StripeLock** | `CASBlobStore._StripeLock` | ~30L class | 64-stripe lock for metadata read-modify-write on local fs. | No — cloud ops are atomic |
| **ContentCache** | `ContentCache` (DI) | ~10L wiring | In-memory LRU for hot reads. | Yes — but latency profile differs |
| **Multipart** | `MultipartUploadMixin` | via mixin | Chunked upload for large files. | Yes — cloud-native multipart |

### 4.1 CASBackend Constructor (After Migration)

```python
class CASBackend(Backend):
    def __init__(
        self,
        transport: BlobTransport,
        *,
        backend_name: str | None = None,
        # Feature injection — local-only features activate when provided
        bloom_filter: BloomFilter | None = None,       # nexus_fast.BloomFilter
        cdc_engine: CDCEngine | None = None,            # extracted from ChunkedStorageMixin
        content_cache: ContentCache | None = None,      # storage.content_cache
        use_stripe_lock: bool = False,                  # enables 64-stripe metadata lock
    ):
```

**Principle:** Features only activate when injected. Cloud backends pass `None` and get
pure cloud-native behavior. Local backend passes all four and gets the full optimization
stack. No conditional logic in the transport layer.

### 4.2 CDCEngine Extraction

`ChunkedStorageMixin` (573L) is currently a mixin on `LocalBackend`. Extract as
standalone `CDCEngine` class (~300L):

```python
class CDCEngine:
    """Content-Defined Chunking engine. Stateless — depends only on BlobTransport."""

    def __init__(self, transport: BlobTransport, *, min_chunk=8*1024, avg_chunk=16*1024, max_chunk=64*1024):
        self._transport = transport

    def should_chunk(self, size: int) -> bool:
        return size >= 16 * 1024 * 1024  # 16MB threshold

    def write_chunked(self, content: bytes, content_hash: str) -> ChunkedReference:
        """FastCDC split -> store chunks -> store manifest."""
        ...

    def read_chunked(self, manifest_key: str) -> bytes:
        """Read manifest -> parallel fetch chunks -> reassemble."""
        ...

    def delete_chunked(self, manifest_key: str) -> None:
        """Read manifest -> delete chunks -> delete manifest."""
        ...
```

The key data structures (`ChunkInfo`, `ChunkedReference`) remain unchanged.

---

## 5. Architecture After Migration

```
ObjectStoreABC (kernel contract)
  |
  Backend (service-level base, 748L)
  |
  |-- CASBackend(transport, bloom?, cdc?, cache?, stripe_lock?)     ← addressing
  |     |-- LocalBlobTransport    → "local" connector               ← transport
  |     |-- GCSBlobTransport      → "gcs" connector
  |     |-- S3BlobTransport       → "s3_cas" connector (future)
  |
  |-- PathBackend(transport)                                         ← addressing
  |     |-- LocalBlobTransport    → "passthrough" connector          ← transport
  |     |-- GCSBlobTransport      → "gcs_connector"
  |     |-- S3BlobTransport       → "s3_connector"
  |
  BlobTransport (Protocol, 9 methods)
  |-- LocalBlobTransport  (new, ~150L) ← extracts I/O from CASBlobStore + LocalBackend
  |-- GCSBlobTransport    (existing, 326L)
  |-- S3BlobTransport     (existing, 413L)
```

### 5.1 LocalBlobTransport (New)

Extracted from `CASBlobStore` (567L) blob I/O methods + `LocalBackend` directory ops:

```python
class LocalBlobTransport:
    """Local filesystem BlobTransport. Key = relative path under root."""

    transport_name = "local"

    def __init__(self, root_path: Path, *, fsync: bool = True):
        self._root = root_path
        self._fsync = fsync

    def put_blob(self, key, data, content_type=""):
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        if self._fsync:
            fd = os.open(str(tmp), os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        os.replace(tmp, path)
        return None

    def get_blob(self, key, version_id=None):
        return (self._root / key).read_bytes(), None

    def delete_blob(self, key):
        (self._root / key).unlink(missing_ok=True)

    def blob_exists(self, key):
        return (self._root / key).exists()

    # ... remaining 5 methods
```

~150 lines. Reuses the atomic temp-write + `os.replace()` pattern from `CASBlobStore`.

---

## 6. WAL Clarification: Three Separate Systems

The codebase has (or will have) three distinct WAL/log systems. They are separate and
serve different purposes:

```
+------------------+    +------------------+    +------------------+
| Event WAL        |    | Raft Log         |    | WriteWAL         |
| (nexus_wal/)     |    | (openraft)       |    | (proposed)       |
+------------------+    +------------------+    +------------------+
| Purpose:         |    | Purpose:         |    | Purpose:         |
| FileEvent        |    | Metadata         |    | Write buffering  |
| durability for   |    | consensus in     |    | Hot/cold delta   |
| EventBus         |    | federation mode  |    | path separation  |
+------------------+    +------------------+    +------------------+
| Writes:          |    | Writes:          |    | Writes:          |
| FileEvent        |    | RaftEntry        |    | ContentDelta     |
| (JSON via orjson)|    | (protobuf)       |    | (binary frames)  |
+------------------+    +------------------+    +------------------+
| Consumers:       |    | Consumers:       |    | Consumers:       |
| EventBus ->      |    | RaftMetastore -> |    | Flush worker ->  |
| CDC, Search,     |    | redb metastore   |    | BlobTransport    |
| Watch, Audit     |    | apply            |    | (eventual write) |
+------------------+    +------------------+    +------------------+
| Engine:          |    | Engine:          |    | Engine:          |
| nexus_wal (Rust) |    | openraft (Rust)  |    | nexus_wal (Rust) |
| CRC32 + segments |    | Raft protocol    |    | reuse WAL engine |
+------------------+    +------------------+    +------------------+
| Status:          |    | Status:          |    | Status:          |
| Implemented      |    | Implemented      |    | Proposed (#1397) |
| (#1397)          |    | (federation)     |    | Post-V0          |
+------------------+    +------------------+    +------------------+
```

### 6.1 Event WAL (`nexus_wal/`)

**What it is:** Durable event log for the EventBus subsystem. Sub-5us writes via Rust
PyO3 extension. Segment-based (configurable rotation size), CRC32 integrity, crash
recovery. Located at `rust/nexus_wal/` and `system_services/event_subsystem/log/wal.py`.

**What it is NOT:** Not a write-ahead log for content writes. Does not buffer blob data.
Does not participate in the write path. The name "WAL" is used in the database sense
(durable sequential log) but serves event durability, not write buffering.

### 6.2 Raft Log (federation mode)

**What it is:** The Raft consensus protocol's replicated log. Used by `RaftMetastore`
to replicate metadata operations across zone members. Entries are `RaftEntry` protobuf
messages. Managed by the `openraft` Rust crate.

**When active:** Only in SC (Strong Consistency) deployment mode with federation enabled.
Single-node or EC (Eventual Consistency) mode does not use Raft.

### 6.3 WriteWAL (proposed, #1397)

**What it is:** A hot/cold path separator for content writes. Incoming writes land in a
fast WAL buffer (the "hot path"), acknowledged immediately. A background flush worker
drains the WAL to `BlobTransport` (the "cold path").

**Why:** Decouples write latency from storage latency. Enables batch flush, write
coalescing, and delta compression. Particularly valuable for local storage where
fsync dominates write time.

**Engine reuse:** Will reuse the `nexus_wal` Rust engine (same CRC32 + segment rotation),
but with `ContentDelta` binary frames instead of JSON `FileEvent` payloads.

**Status:** Post-V0. The current write path (direct `BlobTransport.put_blob()`) is
correct and sufficient for V0.

---

## 7. Hot/Cold Path Design (Post-V0, #1397)

Standard WAL-buffered delta write pattern, inspired by LSM-tree write path:

```
Client write
     |
     v
+----+-----+
| WriteWAL  |  <-- Hot path: append-only, sequential I/O
| (Rust)    |      ~1us append + optional fsync
+----+-----+
     |  ACK to client (write is "durable" once in WAL)
     |
     v  (background flush worker)
+----+------+
| Flush     |  <-- Cold path: random I/O, checksums, metadata
| Worker    |      Batch multiple deltas -> single put_blob()
+----+------+
     |
     v
+----+-----------+
| BlobTransport  |  <-- Final resting place
| (Local/GCS/S3) |
+----------------+
```

### 7.1 Delta Types

| Delta | Payload | Flush Action |
|---|---|---|
| `PUT` | `content_hash + blob_bytes` | `transport.put_blob()` + write metadata sidecar |
| `DELETE` | `content_hash` | Decrement ref_count, `transport.delete_blob()` if zero |
| `META` | `content_hash + meta_dict` | Update metadata sidecar only |

### 7.2 Recovery

On crash, the flush worker replays un-flushed WAL segments on startup. Each delta is
idempotent (CAS puts are content-addressed, so replaying a PUT is a no-op if blob exists).

### 7.3 Why Post-V0

The current synchronous write path works correctly. WriteWAL adds complexity
(crash recovery, flush ordering, read-your-writes consistency) that is not justified
until benchmarks show write latency is a bottleneck. The architecture is designed to
slot in without changing the `CASBackend`/`PathBackend` interface — only the internal
`transport.put_blob()` call gets wrapped.

---

## 8. V0 Migration Plan

### Phase 1: LocalBlobTransport (~150L new)

Create `backends/transports/local_transport.py`:
- Extract blob I/O from `CASBlobStore.write_blob/read_blob/blob_exists`
- Extract directory ops from `LocalBackend.mkdir/rmdir/is_directory/list_dir`
- Implement all 9 `BlobTransport` protocol methods
- Test: `BlobTransport` protocol conformance, atomic write, fsync behavior

### Phase 2: Feature migration into CASBackend

Enhance `CASBackend.__init__()` with optional DI params:
- `bloom_filter`: Wire into `content_exists()` as fast-miss pre-check
- `cdc_engine`: Extract `CDCEngine` from `ChunkedStorageMixin`, wire into `write_content()` / `read_content()`
- `content_cache`: Wire into `read_content()` (check cache first) and `write_content()` (populate on write)
- `use_stripe_lock`: Create `_StripeLock` for local metadata coordination

Test: CAS + Local with all features enabled matches `LocalBackend` behavior.

### Phase 3: PathBackend + LocalBlobTransport verification

Wire `PathBackend(LocalBlobTransport)` as `"passthrough"` connector:
- Verify directory listing, path-based read/write, delete
- Verify no ref counting (always returns 1)
- No pointer files needed — `PathBackend` stores at actual paths

Test: `PassthroughBackend` feature parity.

### Phase 4: Factory rewire + deletion

Update `ConnectorRegistry` and `BackendFactory`:
- `"local"` -> `CASBackend(LocalBlobTransport, bloom=..., cdc=..., cache=..., stripe_lock=True)`
- `"passthrough"` -> `PathBackend(LocalBlobTransport)`
- Delete `LocalBackend`, `PassthroughBackend`, `CASBlobStore`

### Phase 5: WriteWAL hot/cold path (post-V0, #1397)

See Section 7.

### Phase 6: ObjectStoreABC addressing-agnostic refactor (post-V0, #1396)

Refactor `ObjectStoreABC` to remove CAS-specific assumptions (`content_hash` parameters,
`get_ref_count`). CAS vs Path addressing becomes purely a backend concern, not a kernel
contract concern.

---

## 9. Files Changed

### New Files

| File | Lines | Purpose |
|---|---|---|
| `backends/transports/local_transport.py` | ~150 | `LocalBlobTransport` — local filesystem `BlobTransport` |
| `backends/cdc_engine.py` | ~300 | `CDCEngine` — extracted from `ChunkedStorageMixin` |

### Refactored Files

| File | Change |
|---|---|
| `backends/cas_backend.py` (427L) | Add optional `bloom_filter`, `cdc_engine`, `content_cache`, `use_stripe_lock` DI params |
| `backends/factory.py` (177L) | Rewire `"local"` and `"passthrough"` connector creation |
| `backends/registry.py` (650L) | Update connector registration for new wiring |
| `backends/__init__.py` (130L) | Update exports: remove old, add `LocalBlobTransport`, `CDCEngine` |

### Deleted Files

| File | Lines | Replaced By |
|---|---|---|
| `backends/local.py` | 966 | `CASBackend(LocalBlobTransport)` + feature DI |
| `backends/passthrough.py` | 527 | `PathBackend(LocalBlobTransport)` |
| `backends/cas_blob_store.py` | 567 | `LocalBlobTransport` (I/O) + `CASBackend` (addressing) + `_StripeLock` (inline) |
| `backends/chunked_storage.py` | 573 | `CDCEngine` (standalone class) |
| **Total deleted** | **~2,633** | |

### Net Change

| Metric | Count |
|---|---|
| New code | ~450L (`LocalBlobTransport` + `CDCEngine`) |
| Deleted code | ~2,633L (4 legacy files) |
| Modified code | ~100L (DI params + factory rewire) |
| **Net** | **~-2,083L** |

---

## 10. What Stays, What Goes

**Stays (no changes):**
- `Backend` base class (`backend.py`, 748L) — service-level contract
- `BlobTransport` Protocol (`blob_transport.py`, 140L) — transport contract
- `CASBackend` core logic (`cas_backend.py`, 427L) — addressing engine
- `PathBackend` core logic (`path_backend.py`, 499L) — addressing engine
- `GCSBlobTransport` (`gcs_transport.py`, 326L) — cloud transport
- `S3BlobTransport` (`s3_transport.py`, 413L) — cloud transport
- All cloud connector thin classes (GCSBackend, GCSConnectorBackend, S3ConnectorBackend)
- Event WAL (`nexus_wal/`) — unrelated system, stays as-is
- `nexus_fast.BloomFilter` (Rust) — reused via DI injection

**Goes (deleted):**
- `LocalBackend` (966L) — monolith split into composition
- `PassthroughBackend` (527L) — replaced by `PathBackend` + transport
- `CASBlobStore` (567L) — split into `LocalBlobTransport` + `CASBackend` metadata
- `ChunkedStorageMixin` (573L) — replaced by `CDCEngine`
- `MultipartUploadMixin` usage in local — multipart not needed for local (direct write)

---

## 11. Verification

```bash
# Unit tests — CAS + Local with feature parity
pytest tests/unit/backends/test_cas_backend.py -v
pytest tests/unit/backends/test_path_backend.py -v
pytest tests/unit/backends/test_local_transport.py -v
pytest tests/unit/backends/test_cdc_engine.py -v

# Integration — full connector stack
pytest tests/integration/backends/ -v

# Existing cloud backend tests (must not regress)
pytest tests/unit/backends/test_batch_operations.py -v
pytest tests/unit/backends/test_streaming.py -v

# Type checking
mypy src/nexus/backends/ --strict

# Lint
ruff check src/nexus/backends/
lint-imports

# Protocol conformance — LocalBlobTransport satisfies BlobTransport
python -c "
from nexus.backends.transports.local_transport import LocalBlobTransport
from nexus.backends.blob_transport import BlobTransport
assert isinstance(LocalBlobTransport('/tmp/test'), BlobTransport)
print('OK: LocalBlobTransport conforms to BlobTransport')
"
```

---

## 12. Open Questions

1. **Multipart upload for local?** `LocalBackend` supports multipart via `MultipartUploadMixin`.
   Do we need this for `LocalBlobTransport`, or is direct `put_blob()` sufficient? (Likely
   sufficient — multipart is a cloud concern for upload resumability.)

2. **Bloom filter persistence.** Currently `LocalBackend` rebuilds Bloom on startup by
   scanning CAS directory. Should `CASBackend` persist Bloom state to disk for faster
   cold start? (Deferred — startup scan is fast enough for V0.)

3. **`on_write_callback` migration.** `LocalBackend` has `on_write_callback` for Zoekt
   reindex (#1520). This should migrate to EventBus observer pattern (#809, #810) rather
   than being wired into `CASBackend`. (Align with DT_PIPE design.)

4. **StripeLock scope.** Currently 64 stripes in `CASBlobStore`. Is this optimal? Should
   `CASBackend` expose stripe count as a DI param? (Defer — 64 is battle-tested.)
