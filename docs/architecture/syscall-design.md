# Syscall Design

**Status:** Implemented. Source of truth for syscall inventory and design rationale.
**See also:** `KERNEL-ARCHITECTURE.md` §2 for the kernel-level view.

---

## 1. Architecture: Linux kernel + libc Pattern

Two layers, mirroring Linux kernel + glibc:

```
Linux:   Application → libc read()  → syscall(NR_read) → kernel sys_read()
Nexus:   Client      → nx.read()    →                  → NexusFS.sys_read()
                        ↑ Tier 2 (contracts/)               ↑ Tier 1 (core/)
                        No sys_ prefix                       sys_ prefix
                        Composes primitives                  Atomic primitives
```

- **Tier 1 (kernel)**: Abstract `sys_*` methods on `NexusFilesystemABC`. Implemented by `NexusFS`.
  All POSIX-aligned, path-addressed. No hash-addressing at kernel level.
- **Tier 2 (convenience)**: Concrete methods on `NexusFilesystemABC`. Compose Tier 1 syscalls.
  Half POSIX VFS-aligned, half HDFS/GFS-aligned (content access via driver).

---

## 2. Kernel Syscall Table

All path-addressed. No hash-addressing (CAS is driver detail, not kernel concern).

### Tier 1 — Abstract Syscalls (11)

| # | Plane | Syscall | Signature | POSIX Ref |
|---|-------|---------|-----------|-----------|
| 1 | Content | `sys_read` | `(path, count=None, offset=0) → bytes` | `pread(2)` |
| 2 | Content | `sys_write` | `(path, buf, count=None, offset=0) → dict` | `write(2)` |
| 3 | Metadata | `sys_stat` | `(path) → dict \| None` | `stat(2)` |
| 4 | Metadata | `sys_setattr` | `(path, **attrs) → dict` | `chmod/chown/utimes` + `mknod` (DT_DIR, DT_PIPE, DT_STREAM, DT_MOUNT) |
| 5 | Namespace | `sys_unlink` | `(path, recursive=False) → dict` | `unlink(2)` |
| 6 | Namespace | `sys_rename` | `(old, new) → dict` | `rename(2)` |
| 7 | Namespace | `sys_copy` | `(src, dst) → dict` | — (server-side copy, Issue #3329) |
| 8 | Directory | `sys_readdir` | `(path, recursive=True, limit=None) → list` | `readdir(3)` |
| 9 | Locking | `sys_lock` | `(path, mode, ttl, max_holders) → str \| None` | `flock(2)` |
| 10 | Locking | `sys_unlock` | `(path, lock_id) → bool` | `flock(LOCK_UN)` |
| 11 | Watch | `sys_watch` | `(path, timeout, recursive) → dict \| None` | `inotify(7)` |

### Tier 2 — Concrete Convenience (not abstract, composing Tier 1)

| Method | Tier | Composes | Notes |
|--------|------|----------|-------|
| `sys_rmdir` | 2 | `sys_unlink(recursive=)` | Thin delegation, overridable |
| `access` | 2 | `sys_stat` | Returns `True` if stat succeeds |
| `is_directory` | 2 | `sys_stat` | Checks `is_directory` field |

`sys_setattr` is the universal creation/management syscall:
- `mkdir(path)` = `sys_setattr(path, entry_type=DT_DIR)` (Tier 2)
- `mount` = `sys_setattr(path, entry_type=DT_MOUNT, backend=...)`
- `mkpipe` = `sys_setattr(path, entry_type=DT_PIPE)`
- `mkstream` = `sys_setattr(path, entry_type=DT_STREAM)`
- `/__sys__/` paths = kernel management (service register/unregister)

### What's NOT a kernel syscall

Hash-addressed content operations (`read_content`, `write_content`, `stream`,
`write_stream`) stay on **ObjectStoreABC** (driver level):

- Hash-addressing implies CAS, but not all backends use CAS. Kernel is backend-agnostic.
- Linux doesn't expose `sys_read_block(lba)` — that's the block device driver's concern.
- HDFS separates: ClientProtocol (path-based, NameNode) vs DataTransferProtocol
  (block-based, DataNode). Our ObjectStoreABC = DataNode equivalent.

---

## 3. Convenience Layer (NexusFilesystemABC Tier 2)

Defined in `contracts/filesystem/filesystem_abc.py` as concrete methods.
NexusFS inherits them — callers use `nx.read(path)` directly.

### VFS Half — POSIX-aligned

| Method | Composes | Behavior |
|--------|----------|----------|
| `read(path, count, offset)` | `sys_stat` + `sys_read` | POSIX pread semantics |
| `write(path, buf, consistency=)` | `sys_write` + `sys_setattr` | Write + metadata update, consistency param |
| `mkdir(path, parents, exist_ok)` | `sys_setattr(entry_type=DT_DIR)` | Directory creation with hooks + events |
| `rmdir(path, recursive)` | `sys_rmdir` | Lenient defaults (recursive=True) |
| `append(path, content)` | `read` + `write` | Shell `>>` semantics |
| `edit(path, edits)` | `read` + transform + `write` | Apply diffs |
| `write_batch(files)` | N × `write()` | Batch file writes |
| `access(path)` | `sys_stat` | Existence check |
| `is_directory(path)` | `sys_stat` | Directory check |
| `lock(path, mode, timeout)` | `sys_lock` (retry loop) | Blocking lock (like `fcntl(F_SETLKW)`) |
| `unlock(lock_id, path)` | `sys_unlock` | Release lock |
| `locked(path)` | `lock` + `unlock` | Async context manager |

### HDFS Half — Driver-level content access

| Method | Delegates to | Purpose |
|--------|-------------|---------|
| `read_content(hash)` | `ObjectStoreABC.read_content(hash)` | Direct blob access by hash |
| `write_content(content)` | `ObjectStoreABC.write_content(content)` | Direct blob store, return hash |
| `stream(hash)` | `ObjectStoreABC.stream(hash)` | Streaming blob read |
| `write_stream(path)` | `ObjectStoreABC.write_stream(path)` | Streaming blob write |

### Higher-level

| Method | Composes |
|--------|----------|
| `glob(pattern)` | `sys_readdir` + filter |
| `grep(pattern, path)` | `sys_readdir` + `sys_read` + regex |

---

## 4. Key Design Decisions

### 4.1 sys_read / sys_write: Content-only (POSIX pread/pwrite)

`sys_write` is content-only (SRP). Metadata updates are handled by `sys_setattr`
or Tier 2 `write()`. File must exist — `sys_write` to a non-existent path raises
`NexusFileNotFoundError`. Creation goes through `sys_setattr`.

CAS read-modify-write for offset writes is handled internally by the driver.
Kernel does not know whether backend is CAS or path-addressed.

### 4.2 sys_unlink: Unified delete (files + directories)

`sys_unlink` handles both files and directories (with `recursive=` param).
`sys_rmdir` is Tier 2 convenience that delegates to `sys_unlink(recursive=)`.
CAS content is freed when refcount reaches zero.

### 4.3 sys_setattr: Universal creation/management

`sys_setattr` is the Swiss Army knife — creation, attribute updates, and special
inode types all flow through it:

- **Create**: `entry_type=DT_DIR/DT_PIPE/DT_STREAM/DT_MOUNT` creates the inode
- **Update**: No `entry_type` updates mutable metadata fields
- **Idempotent open**: Same `entry_type` on existing path recovers the buffer (pipes/streams)
- **`/__sys__/`**: Kernel management namespace (service register, config, etc.)

### 4.4 sys_lock / sys_unlock: Advisory locks (POSIX flock)

Exposed as kernel syscalls (not service-layer). `sys_lock` is non-blocking
(`F_SETLK`); Tier 2 `lock()` provides blocking retry (`F_SETLKW`); Tier 2
`locked()` provides async context manager. See `lock-architecture.md` §3.

### 4.5 sys_copy: Server-side copy (Issue #3329)

Uses backend-native server-side copy when available (GCS, S3), streaming for
cross-backend, read+write as fallback. Holds VFS locks internally — callers
must NOT hold locks when calling `sys_copy`.

### 4.6 sys_watch: File change notification (inotify)

Waits for file changes with timeout. Returns `FileEvent` dict or `None` on
timeout. Supports recursive watching. Backed by `FileWatcher` kernel primitive.

### 4.7 Hash-addressed ops: Driver level, not kernel

```
Kernel:  sys_read(path)        → internal: path → metadata → hash → driver.read_content(hash)
                                  kernel knows path, does not know hash
Driver:  object_store.read_content(hash) → bytes
                                  driver knows hash, does not care about path
```

Federation content replication uses ObjectStoreABC directly (like HDFS DataTransferProtocol
between DataNodes — separate from NameNode API).

---

## 5. POSIX Alignment Summary

| Syscall | Aligned? | Notes |
|---------|----------|-------|
| `sys_stat` | ✅ | dict vs struct stat (Pythonic) |
| `sys_setattr` | ✅ | Bundles chmod/chown/utimes + mknod (DT_DIR, DT_PIPE, DT_STREAM, DT_MOUNT) |
| `sys_readdir` | ✅ | No opendir/closedir (acceptable simplification), supports pagination |
| `sys_rename` | ✅ | — |
| `sys_unlink` | ✅ | Unified delete (files + dirs), metadata-only (CAS GC pattern) |
| `sys_copy` | ✅ | No direct POSIX equivalent; server-side optimization |
| `sys_read` | ✅ | count/offset (pread semantics) |
| `sys_write` | ✅ | count/offset, content-only (SRP) |
| `sys_lock` | ✅ | Non-blocking flock(F_SETLK) |
| `sys_unlock` | ✅ | flock(LOCK_UN) |
| `sys_watch` | ✅ | inotify(7) equivalent |

Tier 2 demotions (no longer Tier 1):
- `access` → Tier 2 (derives from `sys_stat`)
- `is_directory` → Tier 2 (derives from `sys_stat`)
- `sys_rmdir` → Tier 2 (delegates to `sys_unlink`)

---

## 6. Verification

```bash
uv run pytest tests/ -x -o "addopts="
uv run mypy src/nexus/contracts/filesystem/ src/nexus/core/nexus_fs.py
uv run ruff check src/
PYTHONPATH=src uv run lint-imports
```
