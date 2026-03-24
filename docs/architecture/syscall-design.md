# Syscall Primitive Separation (#1316)

**Tasks**: #834 (sys_ prefix rename), #1316 (primitive separation)
**Prerequisite for**: #1202 (gRPC transport), #1271 (FastAPI sunset)
**Status**: Design complete. Implementation pending.

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

## 2. Kernel Syscall Table (11 syscalls)

All path-addressed. No hash-addressing (CAS is driver detail, not kernel concern).

### Metadata Plane (8)

| # | Syscall | Signature | POSIX Ref |
|---|---------|-----------|-----------|
| 1 | `sys_stat` | `(path) → FileMetadata \| None` | `stat(2)` |
| 2 | `sys_setattr` | `(path, **attrs) → FileMetadata` | `chmod/chown/utimes` + `mknod` (DT_DIR, DT_PIPE, DT_STREAM) |
| 3 | `sys_rmdir` | `(path, recursive=False) → None` | `rmdir(2)` |
| 4 | `sys_readdir` | `(path, recursive=True) → list` | `readdir(3)` |
| 5 | `sys_access` | `(path, mode=F_OK) → bool` | `access(2)` |
| 6 | `sys_rename` | `(old, new) → None` | `rename(2)` |
| 7 | `sys_unlink` | `(path) → None` | `unlink(2)` |
| 8 | `sys_is_directory` | `(path) → bool` | `S_ISDIR` macro |

`mkdir(path, parents, exist_ok)` is Tier 2 convenience over `sys_setattr(path, entry_type=DT_DIR)`.

### Content Plane (2)

| # | Syscall | Signature | POSIX Ref |
|---|---------|-----------|-----------|
| 10 | `sys_read` | `(path, count=None, offset=0) → bytes` | `pread(2)` |
| 11 | `sys_write` | `(path, buf, count=None, offset=0) → int` | `pwrite(2)` |

### What's NOT a kernel syscall

Hash-addressed content operations (`read_content`, `write_content`, `delete_content`,
`get_content_size`) stay on **ObjectStoreABC** (driver level):

- Hash-addressing implies CAS, but not all backends use CAS (S3Connector, PassthroughBackend
  use path-addressing). Kernel must be backend-agnostic.
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
| `write(path, buf, count, offset)` | `sys_write` + `sys_setattr` | POSIX pwrite + metadata update |
| `stat(path)` | `sys_stat` | Thin wrapper |
| `mkdir(path, parents, exist_ok)` | `sys_setattr(entry_type=DT_DIR)` | Directory creation with hooks + events |
| `unlink(path)` | `sys_unlink` | Thin wrapper |
| `append(path, content)` | `read` + `write` | Shell `>>` semantics |
| `edit(path, edits)` | `read` + transform + `write` | Apply diffs |

### HDFS Half — Driver-level content access

| Method | Delegates to | Purpose |
|--------|-------------|---------|
| `read_content(hash)` | `ObjectStoreABC.read_content(hash)` | Direct blob access by hash |
| `write_content(content)` | `ObjectStoreABC.write_content(content)` | Direct blob store, return hash |

### Higher-level

| Method | Composes |
|--------|----------|
| `glob(pattern)` | `sys_readdir` + filter |
| `grep(pattern, path)` | `sys_readdir` + `sys_read` + regex |
| `write_batch(files)` | N × `write()` |

---

## 4. Key Design Decisions

### 4.1 sys_read / sys_write: Content-only (POSIX pread/pwrite)

| Aspect | Current | New (POSIX-aligned) |
|--------|---------|---------------------|
| Signature | `sys_write(path, content, if_match, force, lock) → dict` | `sys_write(path, buf, count, offset) → int` |
| Content | Whole-file replacement | Partial write at offset |
| Metadata | Updates etag/version/mtime | Does NOT update metadata |
| Return | dict with etag/version/size | int (bytes written) |
| CAS params | In signature (`if_match`, `force`) | In `OperationContext` or removed |

CAS read-modify-write for offset writes is handled internally by the driver.
Kernel does not know whether backend is CAS or path-addressed.

### 4.2 sys_unlink: Metadata-only (HDFS/GFS GC pattern)

Current: `sys_unlink` deletes metadata AND calls `backend.delete_content(hash)`.
New: `sys_unlink` only deletes the directory entry. Content orphans cleaned by
async `ContentGarbageCollector` (like HDFS BlockManager).

Rationale: HDFS/GFS standard pattern. See `federation-memo.md` §7f Caveat 4.

### 4.3 sys_access: POSIX mode flags + DI PermissionChecker

```python
F_OK = 0  # existence
R_OK = 4  # read
W_OK = 2  # write
X_OK = 1  # execute

def sys_access(self, path, mode=F_OK, context=None) -> bool:
    if mode == F_OK:
        return self._metadata.exists(path)
    return self._permission_checker.check(path, mode, context)
```

`PermissionChecker` is DI'd. Default: `NoOpPermissionChecker` (allow all).
Works with simple rwx, RBAC, ReBAC — all reduce to "can user do r/w/x on path?"

**Future (privacy computing)**: rwx may be insufficient for fine-grained data access
(e.g., "compute aggregate without reading raw data"). Extension path: capability-based
model (like Linux `CAP_*` extending rwx to 40+ capabilities).

### 4.4 Hash-addressed ops: Driver level, not kernel

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
| `sys_setattr` | ✅ | Bundles chmod/chown/utimes + mknod (DT_DIR, DT_PIPE, DT_STREAM) |
| `sys_rmdir` | ✅ | `recursive` is extension |
| `sys_readdir` | ✅ | No opendir/closedir (acceptable simplification) |
| `sys_access` | ⚠️→✅ | Adding mode flags (F_OK/R_OK/W_OK/X_OK) |
| `sys_rename` | ✅ | — |
| `sys_unlink` | ⚠️→✅ | Changing to metadata-only |
| `sys_is_directory` | ✅ | Our extension (S_ISDIR macro equivalent) |
| `sys_read` | ⚠️→✅ | Adding count/offset, content-only |
| `sys_write` | ⚠️→✅ | Adding count/offset, content-only, return int |

---

## 6. Verification

```bash
uv run pytest tests/ -x --timeout=60
uv run mypy src/nexus/contracts/filesystem/ src/nexus/core/nexus_fs.py
uv run ruff check src/
PYTHONPATH=src uv run lint-imports
```
