# Backend Storage Architecture (#1323, #1396, #1397)

**Task**: #1323 (CAS x Backend orthogonal composition)
**Depends on**: #1318 (sys_read/sys_write POSIX alignment — merged)
**Blocks**: #1396 (ObjectStoreABC addressing-agnostic refactor), #1397 (Hot/cold WAL write path)
**Status**: V0 design complete. #1323 merged. Phases 1–6 done. passthrough.py deleted (#1447 — kernel OBSERVE replaces pointer/inotify layer). local_connector.py kept.

---

## 1. Problem: Legacy Backends Coexisting with New Composition

After PR #2738 merged the CAS x Backend composition (#1323), we have two generations
of backend architecture coexisting. The new architecture serves cloud backends; the old
monoliths still serve local storage. Goal: migrate everything to the new model, then
delete all legacy code.

### 1.1 New Architecture (#1323) — Active

| File | Class | Reg Name | Role |
|---|---|---|---|
| `cas_addressing_engine.py` | `CASAddressingEngine(Backend)` | — | CAS addressing engine |
| `path_addressing_engine.py` | `PathAddressingEngine(Backend)` | — | Path addressing engine |
| `transport.py` | `Transport` (Protocol) | — | Transport abstraction (10 methods) |
| `cas_gcs.py` | `CASGCSBackend(CASAddressingEngine)` | `"cas_gcs"` | Thin: CAS + GCS transport |
| `path_gcs.py` | `PathGCSBackend(PathAddressingEngine)` | `"path_gcs"` | Thin: Path + GCS transport |
| `path_s3.py` | `PathS3Backend(PathAddressingEngine)` | `"path_s3"` | Thin: Path + S3 transport |
| `transports/gcs_transport.py` | `GCSTransport` | — | GCS blob I/O |
| `transports/s3_transport.py` | `S3Transport` | — | S3 blob I/O |

**API Connector Transports** (all compose with `PathAddressingEngine`):

| Package | Transport | Backend | Auth |
|---|---|---|---|
| `connectors/gmail/` | `GmailTransport` | `PathGmailBackend` | OAuth (Google) |
| `connectors/calendar/` | `CalendarTransport` | `PathCalendarBackend` | OAuth (Google) |
| `connectors/gdrive/` | `DriveTransport` | `PathGDriveBackend` | OAuth (Google) |
| `connectors/slack/` | `SlackTransport` | `PathSlackBackend` | OAuth (Slack) |
| `connectors/x/` | `XTransport` | `PathXBackend` | OAuth (X) |
| `connectors/hn/` | `HNTransport` | `PathHNBackend` | None (public) |
| `connectors/cli/` | `CLITransport` | `PathCLIBackend` | Per-connector env vars |

### 1.2 Surviving Legacy

| File | Class | Status |
|---|---|---|
| `local_connector.py` | `LocalConnectorBackend` | **Kept** — unique path-based features (symlink safety, inode versioning, L1 cache) |

All other legacy backends have been deleted and replaced by the composition model.

---

## 2. WHERE x HOW: Orthogonal Composition Model

PR #1323 established the principle: **transport** (WHERE blobs live) and **addressing**
(HOW blobs are identified) are orthogonal axes.

### 2.1 Composition Matrix

```
              Transport (WHERE)
              Local   GCS    S3    Gmail  GDrive  Slack  X    HN   CLI   Calendar
Addressing   +------+------+-----+------+-------+------+----+----+-----+---------+
(HOW)  CAS   | ✓    | ✓    | ✓   |      |       |      |    |    |     |         |
       Path  | ✓    | ✓    | ✓   | ✓    | ✓     | ✓    | ✓  | ✓  | ✓   | ✓       |
             +------+------+-----+------+-------+------+----+----+-----+---------+
```

**Blob storage cells:**

| Cell | Reg Name | Status |
|---|---|---|
| CAS + Local | `"cas_local"` | **Done** — `CASLocalBackend` |
| CAS + GCS | `"cas_gcs"` | **Done** — `CASGCSBackend` |
| CAS + S3 | — | **Future** — `S3Transport` exists but no CAS wiring yet |
| Path + Local | `"local_connector"` | **Keep** — unique architecture (symlink safety, inode versioning) |
| Path + GCS | `"path_gcs"` | **Done** — `PathGCSBackend` |
| Path + S3 | `"path_s3"` | **Done** — `PathS3Backend` |

**API connector cells** (all Path addressing, `DT_EXTERNAL_STORAGE`):

| Cell | Reg Name | Status |
|---|---|---|
| Path + Gmail | `"gmail_connector"` | **Done** — `PathGmailBackend` + `GmailTransport` |
| Path + GDrive | `"gdrive_connector"` | **Done** — `PathGDriveBackend` + `DriveTransport` |
| Path + Calendar | `"gcalendar_connector"` | **Done** — `PathCalendarBackend` + `CalendarTransport` |
| Path + Slack | `"slack_connector"` | **Done** — `PathSlackBackend` + `SlackTransport` |
| Path + X | `"x_connector"` | **Done** — `PathXBackend` + `XTransport` |
| Path + HN | `"hn_connector"` | **Done** — `PathHNBackend` + `HNTransport` |
| Path + CLI | (dynamic) | **Done** — `PathCLIBackend` + `CLITransport` (7 subclasses) |

See `connector-transport-matrix.md` for per-connector method coverage and auth details.

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
| **Metastore** | Zone-level reserved key (`__i_links_count__`) | redb | Mount references. DT_MOUNT increments via Raft-side atomic op, zone removal blocked if > 0. |
| **Backend** | `ref_count` in `.meta` sidecar | ObjectStore | Content references. CAS dedup: multiple paths -> same blob. GC at zero. |

These are orthogonal. Federation DT_MOUNT increments the zone-level link count in the
metastore — it never touches `Backend.get_ref_count()`. Path-addressed backends return ref_count=1
because there is no content dedup (each path owns its blob exclusively).

### 2.4 Transport Protocol (10 methods)

From `backends/base/transport.py`:

```python
@runtime_checkable
class Transport(Protocol):
    transport_name: str
    def store(self, key: str, data: bytes, content_type: str = "") -> str | None: ...
    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]: ...
    def remove(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def get_size(self, key: str) -> int: ...
    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]: ...
    def copy_key(self, src_key: str, dst_key: str) -> None: ...
    def create_dir(self, key: str) -> None: ...
    def stream(self, key, chunk_size=8192, version_id=None) -> Iterator[bytes]: ...
    def store_chunked(self, key, chunks, content_type="") -> str | None: ...
```

Method names map to REST verbs: `store`=PUT, `fetch`=GET, `remove`=DELETE,
`exists`=HEAD, `list_keys`=GET collection. This makes API connectors natural —
REST APIs are filesystems (HATEOAS).

### 2.5 Transport Inventory

**Blob storage transports:**

| Transport | File | Description |
|---|---|---|
| `LocalTransport` | `transports/local_transport.py` | Local filesystem I/O |
| `GCSTransport` | `transports/gcs_transport.py` | Google Cloud Storage, signed URLs |
| `S3Transport` | `transports/s3_transport.py` | AWS S3, presigned URLs, multipart |

**API connector transports:**

| Transport | File | Description |
|---|---|---|
| `GmailTransport` | `connectors/gmail/transport.py` | Gmail API, label-based folders |
| `CalendarTransport` | `connectors/calendar/transport.py` | Google Calendar API, full CRUD |
| `DriveTransport` | `connectors/gdrive/transport.py` | Google Drive API, folder ID caching |
| `SlackTransport` | `connectors/slack/transport.py` | Slack API, channel-based |
| `XTransport` | `connectors/x/transport.py` | X/Twitter API v2 |
| `HNTransport` | `connectors/hn/transport.py` | HN Firebase API, read-only |
| `CLITransport` | `connectors/cli/transport.py` | Subprocess execution |

Linux analogy: `Transport` is the **block device driver** (ext4 doesn't care if
the disk is SSD, NVMe, or a network API). `CASAddressingEngine`/`PathAddressingEngine`
are the **filesystem layer** (ext4 vs FAT32 — different addressing, same block device interface).

---

## 3. CRUD Behavior Matrix

Each WHERE x HOW combination produces different CRUD semantics.

### 3.1 CAS + LocalBlobTransport (replaces LocalBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> Bloom `add(hash)` -> `put_blob("cas/{h[:2]}/{h[2:4]}/{h}", content)` -> ref++ in `.meta` sidecar (under StripeLock) -> populate ContentCache |
| **Read** | ContentCache check -> `get_blob("cas/{h[:2]}/{h[2:4]}/{h}")` -> return bytes |
| **Delete** | StripeLock -> ref-- in `.meta` -> if zero: `delete_blob()` + cleanup empty dirs |
| **Exists** | Bloom `might_exist(hash)` -> false = definite miss (skip disk) -> true = `blob_exists()` to confirm |
| **Stream** | `stream_blob()` with 64KB chunks (local seek-based) |
| **Large file** | CDC: files >= 16MB -> FastCDC split -> store chunks + manifest -> reassemble on read |

Full local optimization stack: Bloom, StripeLock, CDC, ContentCache.

### 3.2 CAS + GCSBlobTransport (current CASGCSBackend)

| Op | Behavior |
|---|---|
| **Write** | `hash = blake3(content)` -> `put_blob(key, content)` -> write JSON metadata sidecar |
| **Read** | `get_blob(key)` -> return bytes |
| **Delete** | ref-- in metadata sidecar -> if zero: `delete_blob()` |
| **Exists** | `blob_exists()` — network round-trip |
| **Stream** | `stream_blob()` via GCS streaming download |

No Bloom, no StripeLock (cloud ops are server-side atomic), no CDC, no local cache.

### 3.3 Path + LocalBlobTransport (replaces LocalConnectorBackend)

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

## 4. Feature Migration: LocalBackend -> CASBackend

`LocalBackend` (966L) bundles four optimization features that must migrate to
`CASBackend` as optional DI parameters. These are addressing-level concerns, not
transport-level — they operate on content hashes and CAS metadata, not raw blob I/O.

| Feature | Source | Purpose | Why CASBackend (not transport) |
|---|---|---|---|
| **BloomFilter** | `nexus_fast` (Rust) | Fast negative lookup on `content_exists()`. Populated from disk scan at startup, updated on every write. | Operates on content hashes — CAS addressing concept |
| **CDC** | `ChunkedStorageMixin` (573L) | Content-defined chunking for files >= 16MB via FastCDC. Chunk-level dedup. | Decides how to split before writing — addressing decision |
| **StripeLock** | `backends/base/stripe_lock.py` (47L) | 64-stripe lock for local metadata sidecar read-modify-write. | Coordinates CAS ref_count updates — metadata concern |
| **ContentCache** | `ContentCache` (DI) | In-memory LRU keyed by content hash for hot reads. | Keyed by content_hash — CAS addressing concept |

**Multipart upload** stays at the connector level. Currently used in production via
TUS resumable upload endpoints (`/api/v2/uploads`) -> `ChunkedUploadService` ->
`LocalBackend.init_multipart()`. For the new architecture, `PathS3Backend` already
delegates multipart to `S3BlobTransport`. `CASBackend` with `LocalBlobTransport` should
also support multipart via the transport layer (add `init_multipart` / `upload_part` /
`complete_multipart` / `abort_multipart` to `LocalBlobTransport`).

### 4.1 CASBackend Constructor

```python
class CASBackend(Backend):
    def __init__(
        self,
        transport: BlobTransport,
        *,
        backend_name: str | None = None,
        # Feature DI — local-only optimizations, all None-safe
        bloom_filter: Any | None = None,       # nexus_fast.BloomFilter
        content_cache: Any | None = None,      # storage.content_cache.ContentCache
        stripe_lock: Any | None = None,        # _StripeLock instance for local metadata coordination
        on_write_callback: Any | None = None,  # e.g. Zoekt reindex hook
    ):
```

**Note:** `CDCEngine` is composed at the subclass level (`CASLocalBackend`), not injected
into base `CASBackend`. This keeps the base class clean — CDC is a local-only concern
that operates on the backend's internal methods.

**Principle:** Features only activate when injected. Cloud backends pass `None` and get
pure cloud-native behavior. Local backend passes all four and gets the full optimization
stack. No conditional logic in the transport layer.

### 4.2 CDCEngine Extraction

`ChunkedStorageMixin` (573L) was a mixin on `LocalBackend`. Extracted as standalone
`CDCEngine` class at `backends/engines/cdc.py` (373L):

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
  |-- CASBackend(transport, bloom?, cdc?, cache?, stripe_lock?)     <- addressing
  |     |-- LocalBlobTransport    -> "cas_local"  (factory-built)    <- transport
  |     |-- GCSBlobTransport      -> "cas_gcs"    (thin class)
  |     |-- S3BlobTransport       -> "cas_s3"     (future thin class)
  |
  |-- PathBackend(transport)                                         <- addressing
  |     |-- LocalBlobTransport    -> "path_local" (factory-built)    <- transport
  |     |-- GCSBlobTransport      -> "path_gcs"   (thin class)
  |     |-- S3BlobTransport       -> "path_s3"    (thin class)
  |
  BlobTransport (Protocol, 9 methods)
  |-- LocalBlobTransport  (317L)
  |-- GCSBlobTransport    (325L)
  |-- S3BlobTransport     (412L)
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

    # ... remaining 5 methods (get_blob_size, list_blobs, copy_blob,
    #     create_directory_marker, stream_blob)
```

317 lines. Reuses the atomic temp-write + `os.replace()` pattern from `CASBlobStore`.
Also supports `MultipartUploadMixin` methods for TUS resumable uploads.

### 5.2 Thin Connector Naming Convention

Thin connectors now follow a consistent naming convention that encodes the addressing
axis. Rename completed in Phase 5.

**Convention: `{addressing}_{transport}`** — file names, class names, and connector
registration strings all follow this pattern.

#### Rename Table

| Old File | Old Class | Old Reg | New File | New Class | New Reg | Status |
|---|---|---|---|---|---|---|
| `gcs.py` | `GCSBackend` | `"gcs"` | `cas_gcs.py` | `CASGCSBackend` | `"cas_gcs"` | **DONE** |
| `gcs_connector.py` | `GCSConnectorBackend` | `"gcs_connector"` | `path_gcs.py` | `PathGCSBackend` | `"path_gcs"` | **DONE** |
| `s3_connector.py` | `S3ConnectorBackend` | `"s3_connector"` | `path_s3.py` | `PathS3Backend` | `"path_s3"` | **DONE** |
| — | — | — | `cas_s3.py` (future) | `CASS3Backend` | `"cas_s3"` | Future |

**Local transport:** `CASLocalBackend` lives in `cas_local.py` (registered as
`"cas_local"`). It composes `CASBackend(LocalBlobTransport)` + `CDCEngine` +
`MultipartUpload` with Feature DI (Bloom, cache, stripe lock). A future
`PathLocalBackend` would register as `"path_local"`.

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

### Phase 1: LocalBlobTransport — **DONE**

`backends/transports/local_transport.py` (317L):
- Extracted blob I/O from `CASBlobStore.write_blob/read_blob/blob_exists`
- Extracted directory ops from `LocalBackend.mkdir/rmdir/is_directory/list_dir`
- All 9 `BlobTransport` protocol methods + multipart support

### Phase 2: Feature migration into CASBackend — **DONE**

`CASBackend.__init__()` accepts optional DI params:
- `bloom_filter`: Wired into `content_exists()` as fast-miss pre-check
- `content_cache`: Wired into `read_content()` (check cache first) and `write_content()` (populate on write)
- `stripe_lock`: `_StripeLock` instance for local metadata coordination
- `on_write_callback`: Hook for external observers (e.g. Zoekt reindex)
- `CDCEngine` composed at `CASLocalBackend` subclass level (`engines/cdc.py`, 373L)

### Phase 3: PathBackend + LocalBlobTransport verification — **DONE**

`passthrough.py` deleted in #1447 (kernel OBSERVE replaces pointer/inotify layer).
`local_connector.py` kept — unique path-based features (symlink safety, inode versioning, L1 cache).
`PathBackend(LocalBlobTransport)` remains available for future use cases.

### Phase 4: AsyncLocalBackend migration — **DONE**

`AsyncLocalBackend` (755L) deleted. Replaced by async wrapper around
`CASBackend(LocalBlobTransport)`.

### Phase 5: Factory rewire + naming cleanup + legacy deletion — **DONE**

- ~~Rename thin connector files/classes/reg names per Section 5.2~~ **DONE**
- Wire factory: `"cas_local"` -> `CASBackend(LocalBlobTransport, ...)`, `"path_local"` -> `PathBackend(LocalBlobTransport)`
- Add backward-compat aliases for old connector names (Section 5.2)
- Delete: `local.py`, `chunked_storage.py`, `async_local.py`
- `cas_blob_store.py` already deleted
- **Keep**: `local_connector.py` (unique path-based features)
- ~~`passthrough.py`~~ — deleted in #1447

### Phase 6: StripeLock extraction — **DONE**

`_StripeLock` extracted to standalone module at
`backends/base/stripe_lock.py`. Now importable independently for CAS metadata
coordination.

### Phase 7: WriteWAL hot/cold path (post-V0, #1397)

See Section 7.

### Phase 8: ObjectStoreABC addressing-agnostic refactor (post-V0, #1396)

Refactor `ObjectStoreABC` to remove CAS-specific assumptions (`content_hash` parameters,
`get_ref_count`). CAS vs Path addressing becomes purely a backend concern, not a kernel
contract concern.

---

## 9. Verification

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

## 10. Open Questions

1. **`on_write_callback` sunset.** `CASBackend` currently accepts `on_write_callback`
   as DI param (used by Zoekt reindex). This should migrate to EventBus observer pattern
   (#809, #810) or DT_PIPE, then remove the DI param.
