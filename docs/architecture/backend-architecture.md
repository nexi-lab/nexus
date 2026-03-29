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

## 3. Verification

```bash
# Unit tests — CAS + Local with feature parity
pytest tests/unit/backends/test_cas_backend.py -v
pytest tests/unit/backends/test_path_backend.py -v

# Integration — full connector stack
pytest tests/integration/backends/ -v

# Type checking
mypy src/nexus/backends/ --strict

# Lint
ruff check src/nexus/backends/

# Protocol conformance — LocalTransport satisfies Transport
python -c "
from nexus.backends.base.transport import Transport
from nexus.backends.transports.local_transport import LocalTransport
assert isinstance(LocalTransport('/tmp/test'), Transport)
print('OK: LocalTransport conforms to Transport')
"
```
