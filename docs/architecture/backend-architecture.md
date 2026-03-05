# Backend Storage Architecture (#1323, #1396, #1397)

**Task**: #1323 (CAS x Backend orthogonal composition)
**Depends on**: #1318 (sys_read/sys_write POSIX alignment — merged)
**Blocks**: #1396 (ObjectStoreABC addressing-agnostic refactor), #1397 (Hot/cold WAL write path)
**Status**: CAS local migration complete (PRs #2759, #2772, #2776). Remaining: thin connector rename, passthrough/local_connector migration.

---

## 1. Problem: Legacy Backends Coexisting with New Composition

After PR #2738 merged the CAS x Backend composition (#1323), we have two generations
of backend architecture coexisting. The new architecture serves cloud backends and
local CAS storage; some legacy monoliths still serve path-based local storage.

### 1.1 New Architecture (#1323) — Active

| File | Lines | Class | Reg Name | Role |
|---|---|---|---|---|
| `base/cas_backend.py` | 515 | `CASBackend(Backend)` | — | CAS addressing engine + Feature DI |
| `base/path_backend.py` | 499 | `PathBackend(Backend)` | — | Path addressing engine |
| `base/blob_transport.py` | 140 | `BlobTransport` (Protocol) | — | Transport abstraction (9 methods) |
| `storage/cas_local.py` | 375 | `CASLocalBackend(CASBackend, MultipartUpload)` | `"cas_local"` | CAS + local + CDC + Feature DI |
| `storage/gcs.py` | 153 | `GCSBackend(CASBackend)` | `"gcs"` | Thin: CAS + GCS transport |
| `storage/gcs_connector.py` | 368 | `GCSConnectorBackend(PathBackend)` | `"gcs_connector"` | Thin: Path + GCS transport |
| `storage/s3_connector.py` | 321 | `S3ConnectorBackend(PathBackend)` | `"s3_connector"` | Thin: Path + S3 transport |
| `transports/local_transport.py` | 278 | `LocalBlobTransport` | — | Local filesystem blob I/O |
| `transports/gcs_transport.py` | 326 | `GCSBlobTransport` | — | GCS blob I/O |
| `transports/s3_transport.py` | 413 | `S3BlobTransport` | — | S3 blob I/O |
| `engines/cdc.py` | 373 | `CDCEngine` | — | Content-defined chunking (composition) |
| `engines/multipart.py` | — | `MultipartUpload` (ABC) | — | Resumable upload interface |

**Naming problem:** The cloud thin connector naming is inconsistent. `gcs.py` / `GCSBackend`
doesn't indicate CAS addressing. `gcs_connector.py` / `GCSConnectorBackend` uses "connector"
to mean Path addressing, which is ambiguous. See **Section 5.2** for the rename plan.

### 1.2 Legacy — Remaining

| File | Lines | Class | Status | Replaced By |
|---|---|---|---|---|
| `storage/passthrough.py` | 527 | `PassthroughBackend` | **Pending** | `PathBackend(LocalBlobTransport)` + EventBus |
| `base/cas_blob_store.py` | 567 | `CASBlobStore`, `_StripeLock`, `CASMeta` | **Partial** — `_StripeLock` still imported by `cas_local.py` | Extract `_StripeLock` to shared location, then delete |
| `storage/local_connector.py` | 808 | `LocalConnectorBackend` | **Pending** | `PathBackend(LocalBlobTransport)` + `CacheConnectorMixin` |

**Already deleted:**
- `storage/local.py` (966L `LocalBackend`) — PR #2776
- `storage/async_local.py` (755L `AsyncLocalBackend`) — PR #2776
- `chunked_storage.py` (573L `ChunkedStorageMixin`) — PR #2772, replaced by `engines/cdc.py`

### 1.3 API Connectors — Out of Scope

The API-based connectors (`gdrive_connector.py`, `gmail_connector.py`,
`gcalendar_connector.py`, `slack_connector.py`, `hn_connector.py`, `x_connector.py`)
subclass `Backend` directly but interact with REST APIs, not blob stores. The
`BlobTransport` abstraction does not apply to them. They remain as-is.

---

## 2. WHERE x HOW: Orthogonal Composition Model

PR #1323 established the principle: **transport** (WHERE blobs live) and **addressing**
(HOW blobs are identified) are orthogonal axes.

### 2.1 Composition Matrix (Existing Transports Only)

```
                    Transport (WHERE)
                    Local           GCS             S3
                  +---------------+---------------+---------------+
Addressing  CAS   | CASBackend    | CASBackend    | CASBackend    |
(HOW)             | + LocalBlob   | + GCSBlob     | + S3Blob      |
                  | Transport     | Transport     | Transport     |
                  | (done)        | (done)        | (planned)     |
                  +---------------+---------------+---------------+
            Path  | PathBackend   | PathBackend   | PathBackend   |
                  | + LocalBlob   | + GCSBlob     | + S3Blob      |
                  | Transport     | Transport     | Transport     |
                  | (pending)     | (done)        | (done)        |
                  +---------------+---------------+---------------+
```

**Current state of each cell:**

| Cell | Reg Name | Status |
|---|---|---|
| CAS + Local | `"cas_local"` | **Done** — `CASLocalBackend` in `cas_local.py` (PR #2772, #2776) |
| CAS + GCS | `"gcs"` | **Done** — thin class exists |
| CAS + S3 | — | **Future** — `S3BlobTransport` exists but no CAS wiring yet |
| Path + Local | `"passthrough"`, `"local_connector"` | **Pending** — currently separate monoliths |
| Path + GCS | `"gcs_connector"` | **Done** — thin class exists |
| Path + S3 | `"s3_connector"` | **Done** — thin class exists |

Connector names and file names will be standardized per Section 5.2.

### 2.2 Addressing Semantics

| Axis | CAS Addressing | Path Addressing |
|---|---|---|
| Identity | BLAKE3 hash of content | User-supplied file path |
| Dedup | Automatic — same content = same key | None — each path independent |
| Ref counting | Yes — ref++/ref--, GC at zero | No — content lifecycle = 1:1 with path |
| Use case | Default for all Nexus-owned storage, snapshots, versioning, federation replication | External connectors (user's existing bucket/folder), passthrough/inotify |

**When to use CAS:** All storage that Nexus owns and manages. CAS enables automatic
deduplication, content integrity verification (hash = address), and zero-copy COW
snapshots via ref-count holds. Federation progressive replication requires CAS — blobs
are hash-verified on transfer.

**When to use Path:** External storage where Nexus must not reorganize content layout.
The user's existing GCS bucket, S3 bucket, or local folder stays browseable by external
tools. No CAS hash-named blobs.

### 2.3 Ref Counting Clarification: Two Layers

Ref counting operates at two independent layers:

| Layer | Mechanism | Where | Purpose |
|---|---|---|---|
| **Metastore** | `i_links_count` on `FileMetadata` | redb | Mount references. DT_MOUNT increments, zone removal blocked if > 0. |
| **Backend** | `ref_count` in `.meta` sidecar | ObjectStore | Content references. CAS dedup: multiple paths -> same blob. GC at zero. |

These are orthogonal. Federation DT_MOUNT increments `i_links_count` in the metastore —
it never touches `Backend.get_ref_count()`. Path-addressed backends return ref_count=1
because there is no content dedup (each path owns its blob exclusively).

### 2.4 BlobTransport Protocol (9 methods)

From `backends/base/blob_transport.py` (140 lines):

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

### 2.5 Transport Inventory (Complete)

| Transport | File | Lines | Status |
|---|---|---|---|
| `LocalBlobTransport` | `transports/local_transport.py` | 278 | **Done** — PR #2759 |
| `GCSBlobTransport` | `transports/gcs_transport.py` | 326 | Existing — `google.cloud.storage`, signed URLs, generation tracking |
| `S3BlobTransport` | `transports/s3_transport.py` | 413 | Existing — `boto3`, presigned URLs, multipart, versioning |

Linux analogy: `BlobTransport` is the **block device driver** (ext4 doesn't care if
the disk is SSD or NVMe). `CASBackend`/`PathBackend` are the **filesystem layer**
(ext4 vs FAT32 — different addressing, same block device interface).

---

## 3. CRUD Behavior Matrix

Each WHERE x HOW combination produces different CRUD semantics.

### 3.1 CAS + LocalBlobTransport (`CASLocalBackend`)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> Bloom `add(hash)` -> `put_blob("cas/{h[:2]}/{h[2:4]}/{h}", content)` -> ref++ in `.meta` sidecar (under StripeLock) -> populate ContentCache |
| **Read** | ContentCache check -> `get_blob("cas/{h[:2]}/{h[2:4]}/{h}")` -> return bytes |
| **Delete** | StripeLock -> ref-- in `.meta` -> if zero: `delete_blob()` + cleanup empty dirs |
| **Exists** | Bloom `might_exist(hash)` -> false = definite miss (skip disk) -> true = `blob_exists()` to confirm |
| **Stream** | `stream_blob()` with 64KB chunks (local seek-based) |
| **Large file** | CDC: files >= 16MB -> FastCDC split -> store chunks + manifest -> reassemble on read |

Full local optimization stack: Bloom, StripeLock, CDC, ContentCache.

### 3.2 CAS + GCSBlobTransport (current GCSBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob(key, content)` -> write JSON metadata sidecar |
| **Read** | `get_blob(key)` -> return bytes |
| **Delete** | ref-- in metadata sidecar -> if zero: `delete_blob()` |
| **Exists** | `blob_exists()` — network round-trip |
| **Stream** | `stream_blob()` via GCS streaming download |

No Bloom, no StripeLock (cloud ops are server-side atomic), no CDC, no local cache.

### 3.3 Path + LocalBlobTransport (replaces PassthroughBackend + LocalConnectorBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob(backend_path, content)` -> return hash for metadata |
| **Read** | `get_blob(backend_path)` -> return bytes |
| **Delete** | `delete_blob(backend_path)` — immediate removal |
| **Exists** | `blob_exists(backend_path)` — `os.path.exists()` |
| **Stream** | `stream_blob(backend_path)` with seek-based chunked reads |

No ref counting (content 1:1 with path), no dedup. OS-native paths enable
inotify/fswatch compatibility.

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

## 4. Feature DI: CASBackend Optional Features

`CASBackend` supports four optimization features via dependency injection.
Features only activate when injected — cloud backends pass `None` and get pure
cloud-native behavior. `CASLocalBackend` injects all four for the full local
optimization stack.

| Feature | Source | Purpose | Why CASBackend (not transport) |
|---|---|---|---|
| **BloomFilter** | `nexus_fast` (Rust) | Fast negative lookup on `content_exists()`. Populated from disk scan at startup, updated on every write. | Operates on content hashes — CAS addressing concept |
| **CDC** | `CDCEngine` (`engines/cdc.py`, 373L) | Content-defined chunking for files >= 16MB via FastCDC. Chunk-level dedup. | Decides how to split before writing — addressing decision |
| **StripeLock** | `_StripeLock` (~30L) | 64-stripe lock for local metadata sidecar read-modify-write. | Coordinates CAS ref_count updates — metadata concern |
| **ContentCache** | `ContentCache` (DI) | In-memory LRU keyed by content hash for hot reads. | Keyed by content_hash — CAS addressing concept |

**Multipart upload** is an ABC (`MultipartUpload` in `engines/multipart.py`) that
`CASLocalBackend` implements directly for TUS resumable uploads.

### 4.1 CASBackend Constructor (Actual)

```python
class CASBackend(Backend):
    def __init__(
        self,
        transport: BlobTransport,
        *,
        backend_name: str | None = None,
        # Feature DI — local-only features activate when provided
        bloom_filter: BloomFilter | None = None,       # nexus_fast.BloomFilter
        content_cache: ContentCache | None = None,      # storage.content_cache
        stripe_lock: _StripeLock | None = None,         # 64-stripe metadata lock
        on_write_callback: Any | None = None,           # Zoekt reindex (temporary)
    ):
```

### 4.2 CDCEngine (Composition, Not DI)

`CDCEngine` is used via **composition** in `CASLocalBackend`, not injected into
`CASBackend`. This is because CDC needs to intercept `write_content()` / `read_content()`
at the connector level (routing large files to chunked storage), which requires
overriding CASBackend methods.

```python
# In CASLocalBackend.__init__():
self._cdc = CDCEngine(self)  # takes the backend as dependency

# CDCEngine API (engines/cdc.py, 373L):
class CDCEngine:
    def __init__(self, backend: CASBackend, *, threshold=16MB, ...): ...
    def should_chunk(self, content: bytes) -> bool: ...
    def write_chunked(self, content, context) -> str: ...  # returns manifest hash
    def read_chunked(self, content_hash, context) -> bytes: ...
    def delete_chunked(self, content_hash, context) -> None: ...
    def is_chunked(self, content_hash) -> bool: ...
```

---

## 5. Architecture After Migration

```
ObjectStoreABC (kernel contract)
  |
  Backend (service-level base, 748L)
  |
  |-- CASBackend(transport, bloom?, cache?, stripe_lock?)      <- addressing
  |     |-- CASLocalBackend  -> "cas_local"  (done, 375L)      <- CAS + local + CDC + multipart
  |     |-- GCSBackend       -> "gcs"        (done, thin)       <- to rename "cas_gcs"
  |     |-- (future)         -> "cas_s3"     (planned)
  |
  |-- PathBackend(transport)                                    <- addressing
  |     |-- (pending)        -> "path_local" (pending)          <- replaces passthrough + local_connector
  |     |-- GCSConnectorBackend -> "gcs_connector" (done, thin) <- to rename "path_gcs"
  |     |-- S3ConnectorBackend  -> "s3_connector"  (done, thin) <- to rename "path_s3"
  |
  BlobTransport (Protocol, 9 methods)
  |-- LocalBlobTransport  (done, 278L)
  |-- GCSBlobTransport    (existing, 326L)
  |-- S3BlobTransport     (existing, 413L)
```

### 5.1 LocalBlobTransport (Done)

Created in PR #2759. 278 lines in `transports/local_transport.py`.
Implements all 9 `BlobTransport` protocol methods with atomic temp-write +
`os.replace()` pattern. Supports fsync control via constructor flag.

### 5.2 Thin Connector Naming Convention

The current thin connector names are inconsistent — the naming doesn't encode the
addressing axis, making it unclear which combination a class represents.

**Convention: `{addressing}_{transport}`** — file names, class names, and connector
registration strings all follow this pattern.

#### Rename Table

| Current File | Current Class | Current Reg | New File | New Class | New Reg | Status |
|---|---|---|---|---|---|---|
| `cas_local.py` | `CASLocalBackend` | `"cas_local"` | — | — | — | **Done** |
| `gcs.py` | `GCSBackend` | `"gcs"` | `cas_gcs.py` | `CASGCSBackend` | `"cas_gcs"` | Pending |
| `gcs_connector.py` | `GCSConnectorBackend` | `"gcs_connector"` | `path_gcs.py` | `PathGCSBackend` | `"path_gcs"` | Pending |
| `s3_connector.py` | `S3ConnectorBackend` | `"s3_connector"` | `path_s3.py` | `PathS3Backend` | `"path_s3"` | Pending |
| — | — | — | `cas_s3.py` (future) | `CASS3Backend` | `"cas_s3"` | Future |

#### Why This Convention

1. **Self-documenting.** `cas_gcs.py` immediately tells you: CAS addressing + GCS transport.
2. **Sortable.** `ls cas_*.py` lists all CAS connectors; `ls *_gcs.py` lists all GCS variants.
3. **Extensible.** Adding Azure: `cas_azure.py` / `path_azure.py` — no guesswork.
4. **Matches the matrix.** File names mirror the WHERE x HOW grid in Section 2.1.

---

## 6. WAL Clarification: Two Systems (Event WAL Deleted)

The codebase has two distinct log systems. A third (Event WAL) was deleted because it
was broken in production and never verified to work.

```
+------------------+    +------------------+
| Raft Log         |    | WriteWAL         |
| (openraft)       |    | (proposed)       |
+------------------+    +------------------+
| Purpose:         |    | Purpose:         |
| Metadata         |    | Write buffering  |
| consensus in     |    | Hot/cold delta   |
| federation mode  |    | path separation  |
+------------------+    +------------------+
| Writes:          |    | Writes:          |
| RaftEntry        |    | ContentDelta     |
| (protobuf)       |    | (binary frames)  |
+------------------+    +------------------+
| Engine:          |    | Engine:          |
| openraft (Rust)  |    | New Rust WAL     |
+------------------+    +------------------+
| Status:          |    | Status:          |
| Implemented      |    | Proposed (#1397) |
| (federation)     |    | Post-V0          |
+------------------+    +------------------+
```

### 6.1 Event WAL — DELETED

The `rust/nexus_wal/` Rust engine and `system_services/event_subsystem/log/wal.py`
Python wrapper have been deleted. Rationale:

- The factory had a broken import (`wal_backend` vs `wal`) causing the WAL to be
  silently dead in all production deployments.
- No production user ever verified it worked end-to-end.
- Code with unknown correctness should not remain in the codebase.
- If WriteWAL (#1397) needs a Rust WAL engine, it will be purpose-built.

Files deleted: `rust/nexus_wal/` (2,563L), `log/wal.py` (141L), `log/factory.py` (50L),
`log/protocol.py` (112L), plus all WAL-only tests (~982L). All WAL wiring removed from
factory, lifespan, RedisEventBus, proxy, CI, and config.

### 6.2 Raft Log (federation mode)

The Raft consensus protocol's replicated log. Used by `RaftMetastore` to replicate
metadata operations across zone members. Entries are `RaftEntry` protobuf messages.
Managed by the `openraft` Rust crate.

Only active in SC (Strong Consistency) deployment mode with federation enabled.
Single-node or EC (Eventual Consistency) mode does not use Raft.

### 6.3 WriteWAL (proposed, #1397)

A hot/cold path separator for content writes. Incoming writes land in a fast WAL buffer
(the "hot path"), acknowledged immediately. A background flush worker drains the WAL to
`BlobTransport` (the "cold path").

Decouples write latency from storage latency. Enables batch flush, write coalescing,
and delta compression.

**Status:** Post-V0. The current synchronous write path (direct `BlobTransport.put_blob()`)
is correct and sufficient for V0. The Rust WAL engine will be built from scratch when
needed — purpose-built for ContentDelta frames, not repurposed from the deleted Event WAL.

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

### Phase 1: LocalBlobTransport — DONE (PR #2759)

Created `transports/local_transport.py` (278L):
- All 9 `BlobTransport` protocol methods
- Atomic temp-write + `os.replace()` + fsync
- Tests: protocol conformance, atomic write, fsync behavior

### Phase 2: CASBackend Feature DI + CASLocalBackend — DONE (PR #2759, #2772)

- `CASBackend` enhanced with optional DI params: `bloom_filter`, `content_cache`,
  `stripe_lock`, `on_write_callback`
- `CDCEngine` extracted from `ChunkedStorageMixin` as standalone class (`engines/cdc.py`, 373L)
- `CASLocalBackend` created composing: `CASBackend(LocalBlobTransport)` + `CDCEngine`
  (composition) + `MultipartUpload` (ABC) + Feature DI (Bloom, cache, stripe lock)
- Registered as `"cas_local"` in `ConnectorRegistry`
- Tests: full CRUD, CDC roundtrip, multipart, Bloom fast-miss, contract conformance

### Phase 3: Factory Rewire + Legacy Deletion — DONE (PR #2776)

- Renamed `local_cas.py` → `cas_local.py`, `LocalCASBackend` → `CASLocalBackend`
- Deleted `local.py` (966L), `async_local.py` (755L), `chunked_storage.py` (573L)
- Updated 130+ files: imports, factory calls, `backend_type` references
- No backward-compat aliases — clean break

### Phase 4: PathBackend + LocalBlobTransport — PENDING

Wire `PathBackend(LocalBlobTransport)` to replace:
- `"passthrough"` -> `PathBackend(LocalBlobTransport)` — inotify-compatible local paths
- `"local_connector"` -> `PathBackend(LocalBlobTransport)` + `CacheConnectorMixin`

Test: Feature parity with `PassthroughBackend` and `LocalConnectorBackend`.

### Phase 5: Thin connector rename — PENDING

Rename cloud thin connectors per Section 5.2:
- `gcs.py` → `cas_gcs.py`, `GCSBackend` → `CASGCSBackend`, `"gcs"` → `"cas_gcs"`
- `gcs_connector.py` → `path_gcs.py`, `GCSConnectorBackend` → `PathGCSBackend`, `"gcs_connector"` → `"path_gcs"`
- `s3_connector.py` → `path_s3.py`, `S3ConnectorBackend` → `PathS3Backend`, `"s3_connector"` → `"path_s3"`

### Phase 6: cas_blob_store.py cleanup — PENDING

`cas_blob_store.py` (567L) is mostly dead code. Only `_StripeLock` is still imported
by `cas_local.py`. Extract `_StripeLock` to a shared location, then delete the file.

### Phase 7: WriteWAL hot/cold path (post-V0, #1397)

See Section 7.

### Phase 8: ObjectStoreABC addressing-agnostic refactor (post-V0, #1396)

Refactor `ObjectStoreABC` to remove CAS-specific assumptions (`content_hash` parameters,
`get_ref_count`). CAS vs Path addressing becomes purely a backend concern, not a kernel
contract concern.

---

## 9. Files Changed (Actual)

### New Files (Done)

| File | Lines | Purpose | PR |
|---|---|---|---|
| `transports/local_transport.py` | 278 | `LocalBlobTransport` — local filesystem `BlobTransport` | #2759 |
| `engines/cdc.py` | 373 | `CDCEngine` — CDC via composition | #2772 |
| `engines/multipart.py` | — | `MultipartUpload` ABC | #2772 |
| `storage/cas_local.py` | 375 | `CASLocalBackend` — CAS + local + CDC + multipart | #2772, #2776 |

### Refactored Files (Done)

| File | Change | PR |
|---|---|---|
| `base/cas_backend.py` (515L) | Added Feature DI params: `bloom_filter`, `content_cache`, `stripe_lock`, `on_write_callback` | #2759 |
| `base/factory.py` | Rewired `"cas_local"` connector creation, updated docstrings | #2776 |
| 130+ files | Import/reference updates: `LocalBackend` → `CASLocalBackend`, `backend_type="local"` → `"cas_local"` | #2776 |

### Deleted Files (Done)

| File | Lines | Replaced By | PR |
|---|---|---|---|
| `storage/local.py` | 966 | `CASLocalBackend` | #2776 |
| `storage/async_local.py` | 755 | Deleted (no production use) | #2776 |
| `chunked_storage.py` | 573 | `engines/cdc.py` | #2772 |
| **Total deleted** | **~2,294** | | |

### Pending Deletions

| File | Lines | Blocked By |
|---|---|---|
| `storage/passthrough.py` | 527 | Phase 4 (PathBackend + Local) |
| `base/cas_blob_store.py` | 567 | Phase 6 (extract `_StripeLock`) |
| `storage/local_connector.py` | 808 | Phase 4 (PathBackend + Local) |

---

## 10. What Stays, What Goes

**Stays (no changes):**
- `Backend` base class (`backend.py`, 748L) — service-level contract
- `BlobTransport` Protocol (`blob_transport.py`, 140L) — transport contract
- `CASBackend` core logic (`cas_backend.py`, 515L) — addressing engine + Feature DI
- `PathBackend` core logic (`path_backend.py`, 499L) — addressing engine
- `GCSBlobTransport` (`gcs_transport.py`, 326L) — cloud transport
- `S3BlobTransport` (`s3_transport.py`, 413L) — cloud transport
- All cloud connector thin classes (to be renamed per Section 5.2)
- All API connectors (gdrive, gmail, gcalendar, slack, hn, x)
- All wrappers (`DelegatingBackend`, `CachingBackendWrapper`, `CompressedStorage`, etc.)
- `nexus_fast.BloomFilter` (Rust) — reused via DI injection

**Already gone (deleted):**
- `LocalBackend` (966L) — replaced by `CASLocalBackend`
- `AsyncLocalBackend` (755L) — deleted, no production use
- `ChunkedStorageMixin` (573L) — replaced by `CDCEngine`

**Still to go (pending phases):**
- `PassthroughBackend` (527L) — Phase 4: replace with `PathBackend` + local transport
- `CASBlobStore` (567L) — Phase 6: extract `_StripeLock`, delete rest
- `LocalConnectorBackend` (808L) — Phase 4: replace with `PathBackend` + local transport + cache mixin

---

## 11. Verification

```bash
# Unit tests — CAS + Local
pytest tests/unit/backends/ -v -o 'addopts='

# Core tests — mount/factory integration
pytest tests/unit/core/ -v -o 'addopts='

# Type checking
mypy src/nexus/backends/ --strict

# Lint
ruff check src/nexus/backends/

# Protocol conformance — LocalBlobTransport satisfies BlobTransport
python -c "
from nexus.backends.transports.local_transport import LocalBlobTransport
from nexus.backends.base.blob_transport import BlobTransport
assert isinstance(LocalBlobTransport('/tmp/test'), BlobTransport)
print('OK: LocalBlobTransport conforms to BlobTransport')
"
```

---

## 12. Open Questions

1. **`on_write_callback` migration.** `CASLocalBackend` has `on_write_callback` for Zoekt
   reindex (#1520). This should migrate to EventBus observer pattern (#809, #810) rather
   than being wired into `CASBackend`. (Align with DT_PIPE design.)
