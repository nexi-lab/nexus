# kernel

Pure Rust VFS kernel crate. Owns all core state: VFSRouter, Trie,
MetaStore, LockManager, PipeManager, StreamManager, FileWatchRegistry,
PermissionLeaseCache.

Zero Python dependency. The kernel compiles to both `nexusd-cluster`
(standalone binary) and `nexus-cdylib` (Python extension via PyO3) from
the same source.

## Syscall surface

14 Tier 1 syscalls defined in `src/abi.rs` (`KernelAbi` trait):

| Syscall | Description |
|---|---|
| `sys_read` | Read content (DT_REG, DT_PIPE blocking, DT_STREAM cursor) |
| `sys_write` | Write content (CAS dedup, three-phase with hooks) |
| `sys_unlink` | Delete entry (recursive dir, DT_MOUNT, IPC cleanup) |
| `sys_setattr` | Create/update metadata, mount backends, register services |
| `sys_stat` | Stat entry (implicit directory detection) |
| `sys_readdir` | Directory listing (metastore + backend merge, procfs intercepts) |
| `sys_rename` | Atomic rename (cross-mount rejected) |
| `sys_copy` | Copy with CAS dedup (same-hash = metadata-only) |
| `sys_mkdir` | Create directory (parents, exist_ok) |
| `sys_lock` | Advisory lock acquire (shared/exclusive, TTL) |
| `sys_unlock` | Advisory lock release |
| `sys_watch` | Block until file event matches pattern (inotify equivalent) |
| `setattr_pipe` | DT_PIPE creation with fd binding |
| `sys_setattr` | 21-param mount/service/metadata upsert |

Tier 2 convenience methods live in `src/kernel/convenience.rs` (composed
from Tier 1 syscalls, no new kernel state).

## Storage pillars

The kernel requires exactly one `MetastoreABC` at init. All other
pillars are mounted dynamically via `sys_setattr`:

- **MetastoreABC** (redb) — ordered KV, inodes, CAS index
- **ObjectStoreABC** (S3/GCS/local) — blob content
- **CacheStoreABC** (Dragonfly) — ephemeral KV, pub/sub, TTL
- **RecordStoreABC** (PostgreSQL) — relational, services only

## Dispatch model

```
PRE-DISPATCH   PathResolver (procfs short-circuit, ~50ns trie lookup)
INTERCEPT PRE  NativeInterceptHook chain (permission, audit, stamping)
EXECUTE        sys_* implementation (metastore + backend I/O)
INTERCEPT POST NativeInterceptHook chain (fire-and-forget)
OBSERVE        MutationObserver (FileEvent → ThreadPool, off hot path)
```

## Crate layout

```
src/
  abi.rs              KernelAbi trait (Tier 1 contracts)
  kernel/
    mod.rs            Kernel struct, KernelError, result types
    io.rs             sys_read, sys_write, sys_stat, sys_unlink, sys_readdir
    ipc.rs            DT_PIPE + DT_STREAM operations
    mount.rs          sys_setattr (DT_MOUNT, DT_EXTERNAL_STORAGE)
    dispatch.rs       Permission gate + hook dispatch
    federation.rs     Cross-zone routing + remote fetch
    locks.rs          sys_lock, sys_unlock
    convenience.rs    Tier 2 composed helpers
    observability.rs  Metrics + diagnostics
  core/
    dispatch/         FileEvent, HookContext, NativeInterceptHook trait
    vfs_router.rs     Longest-prefix-match mount routing
    meta_store/       MetaStore trait + redb/remote impls
    permission_cache.rs  PermissionLeaseCache
  abc/                ObjectStore, MetaStore trait definitions
  pipe_manager.rs     DT_PIPE ring buffers
  stream_manager.rs   DT_STREAM append-only logs
  lock_manager.rs     Advisory lock table
  file_watch.rs       FileWatchRegistry (inotify equivalent)
  cache/              IndexCache, DCache
  hal/                DistributedCoordinator trait (Raft abstraction)
  service_registry.rs Runtime service registration
```

## Building

```bash
# Library check (used by other crates)
cargo check -p kernel

# Run tests
cargo test -p kernel --lib

# Full workspace (kernel + transport + services + cluster binary)
cargo build -p nexus-cluster --release
```

## Architecture docs

- [`docs/architecture/KERNEL-ARCHITECTURE.md`](../../docs/architecture/KERNEL-ARCHITECTURE.md) — full kernel design (1200+ lines)
- [`docs/architecture/syscall-design.md`](../../docs/architecture/syscall-design.md) — syscall contracts and migration history
