# How Mounting Works

When you call `mount("s3://my-bucket", "local://./data")`, nexus-fs
parses the URIs, creates storage backends, registers them with the
kernel, and returns a facade that routes operations to the right backend.

## URI parsing

Each URI is parsed into a `MountSpec`:

```
s3://my-bucket
^^   ^^^^^^^^^
scheme  authority
```

| Field | Description |
|-------|-------------|
| `scheme` | Backend type: `s3`, `gcs`, `local`, `gdrive`, or a connector scheme |
| `authority` | Bucket name, project/bucket, or path |
| `mount_point` | Auto-derived filesystem path (see below) |
| `uri` | The original URI string |

Supported schemes:

| Scheme | Format | Example |
|--------|--------|---------|
| `s3` | `s3://<bucket>` | `s3://data-lake` |
| `gcs` | `gcs://<project>/<bucket>` | `gcs://myproj/warehouse` |
| `local` | `local://<path>` | `local://./data` |
| `gdrive` | `gdrive://<id>` | `gdrive://shared` |

## Mount point derivation

Each backend is mounted at an auto-derived path in the virtual filesystem:

```
s3://my-bucket        → /s3/my-bucket/
gcs://project/bucket  → /gcs/bucket/
local://./data        → /local/data/
gdrive://shared       → /gdrive/shared/
```

The `at=` parameter overrides the auto-derived path:

```python
# skip-test
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", at="/data")
# /data/ instead of /s3/my-bucket/
```

### Collision detection

nexus-fs checks for mount point collisions. If two URIs would mount at
the same path, `mount()` raises an error. Use `at=` to resolve
collisions.

### Reserved paths

The paths `/__sys__/` and `/__pipes__/` are reserved by the kernel and
cannot be used as mount points.

## What happens during mount

```mermaid
sequenceDiagram
    participant User
    participant mount()
    participant URI Parser
    participant Backend Factory
    participant Kernel

    User->>mount(): mount("s3://bucket", "local://./data")
    mount()->>URI Parser: parse_uri("s3://bucket")
    URI Parser-->>mount(): MountSpec(scheme=s3, authority=bucket, ...)
    mount()->>URI Parser: parse_uri("local://./data")
    URI Parser-->>mount(): MountSpec(scheme=local, authority=./data, ...)
    mount()->>Backend Factory: create_backend(s3_spec)
    Backend Factory-->>mount(): S3 backend instance
    mount()->>Backend Factory: create_backend(local_spec)
    Backend Factory-->>mount(): Local backend instance
    mount()->>Kernel: register mount(/s3/bucket/, s3_backend)
    mount()->>Kernel: register mount(/local/data/, local_backend)
    Kernel-->>User: NexusFS kernel
```

1. **Parse**: Each URI is parsed into a `MountSpec` via `parse_uri()`.
2. **Derive mount point**: `derive_mount_point()` computes the filesystem
   path, checking for collisions and reserved paths.
3. **Create backend**: The backend factory creates a storage backend
   (S3, GCS, local, or connector) based on the scheme. Cloud backends
   trigger credential discovery at this stage.
4. **Register**: Each backend is registered with the kernel at its mount
   point.
5. **Return kernel**: A `NexusFS` kernel is returned. Call its `sys_*`
   methods directly with `LOCAL_CONTEXT` from `nexus.fs._helpers`, or
   wrap it in `SyncNexusFS` for the legacy synchronous facade API.

## Namespace routing

When you call `fs.read("/s3/my-bucket/file.txt")`, the kernel routes
the request:

1. Match the path prefix against registered mount points
2. Strip the mount prefix to get the backend-relative path
3. Delegate the operation to the matching backend
4. Return the result

This is an O(log m) lookup where m is the number of mounts.

## Metadata storage

nexus-fs uses a SQLite database (WAL mode) to persist file metadata
locally. This avoids re-listing remote backends on every operation and
enables features like `stat()` and `exists()` without network calls for
recently accessed files.

The database is stored in the nexus-fs state directory (platform-
specific, see [State Directory](#state-directory) below).

## State directory

nexus-fs writes two files to the state directory:

| File | Purpose | Regenerable? |
|------|---------|-------------|
| `metadata.db` | SQLite database with file metadata | Yes — rebuilt on next `mount()` |
| `mounts.json` | Last-mounted URIs for auto-discovery | Yes — written on every `mount()` |

Default location depends on platform (override with `NEXUS_FS_STATE_DIR`):

- Linux: `~/.local/state/nexus-fs/`
- macOS: `~/Library/Application Support/nexus-fs/`
- Windows: `%LOCALAPPDATA%/nexus-fs/`

!!! tip "Safe to delete"
    The entire state directory can be deleted without data loss.
    nexus-fs recreates it on the next `mount()` call. Your actual
    data remains in the storage backends (S3, GCS, local).

### Cleanup and retention

nexus-fs does **not** automatically delete stale state. If you mount
different backends over time, old metadata entries accumulate in
`metadata.db`. To reset:

```bash
rm -rf ~/.local/state/nexus-fs/   # Linux
rm -rf ~/Library/Application\ Support/nexus-fs/  # macOS
```

On `close()`, nexus-fs checkpoints the WAL and runs `PRAGMA optimize`
to keep the database compact.

### Corruption recovery

If `metadata.db` becomes corrupted (e.g., after a hard crash), delete
it and re-mount. The database is a cache — no user data is stored in it.

If `mounts.json` contains invalid JSON, `nexus-fs playground` will show
an empty mount list and `fsspec` auto-discovery will raise a clear error
asking you to re-run `mount()`.

### Concurrent processes

nexus-fs is designed for **single-process use per state directory**.

- SQLite WAL mode allows concurrent readers, but only one writer.
- `mount()` writes `mounts.json` atomically (temp file + rename), so
  concurrent readers always see valid JSON.
- If two processes call `mount()` against the same state directory
  simultaneously, the second writer may encounter `SQLITE_BUSY` and
  retry (up to 3 times with exponential backoff). If contention persists
  beyond the retry budget, an `OperationalError` is raised.

To run multiple independent nexus-fs instances, set a different
`NEXUS_FS_STATE_DIR` for each process.
