# Issue #4059 - Kernel Write Coalescing Buffer Design

**Date**: 2026-05-11
**Issue**: [#4059](https://github.com/nexi-lab/nexus/issues/4059) - Write coalescing buffer with configurable flush window

## Context

The Rust kernel owns the hot file I/O path through `rust/kernel/src/kernel/io.rs`. `Kernel::sys_write` validates the path, checks permissions, runs native pre-hooks, routes through `VFSRouter`, handles DT_PIPE and DT_STREAM directly, writes DT_REG content to the routed `ObjectStore`, then commits metadata through the resolved `MetaStore`.

Today each DT_REG `sys_write` reaches the backend immediately. Bursty agent workloads that write small updates to the same file therefore pay one backend write per logical update. This burns backend RPC budget and tail latency, especially for object-store-style backends where a write is a full object PUT.

Issue #4059 asks for a write-back buffer that coalesces those bursts while preserving kernel semantics:

- per-file dirty state
- configurable flush policy per workspace
- read-your-own-writes
- forced flush on close, sync/fsync, and snapshot commit
- explicit durability documentation
- benchmark evidence that 100 burst writes produce at least 10x fewer backend writes

## Decision

Implement write coalescing as a kernel-owned Rust facility, not as a Python `NexusFS` shim or a backend-specific optimization.

The buffer will be owned by `Kernel`, live under the Rust kernel tree, and sit between the DT_REG portion of `sys_write` and the existing backend write plus metastore commit logic. This keeps correctness at the syscall boundary:

- Rust services and Python callers share the same behavior.
- Reads can consult the dirty buffer before backend I/O.
- Snapshot and close paths can force a single authoritative flush API.
- Backends remain simple `ObjectStore` implementations.

## Non-Goals

1. Buffering DT_PIPE or DT_STREAM writes.
2. Adding journaling or crash recovery for dirty bytes inside the flush window.
3. Changing backend `ObjectStore` trait semantics for normal writes.
4. Rewriting the Python workspace snapshot system.
5. Making every connector support efficient partial object updates.
6. Persisting dirty buffers across process restart.

## Policy Model

Add the shared Rust policy type in `rust/contracts` so services, kernel callers, and Python bindings use one vocabulary:

```text
WriteCoalescingMode:
  strict
  latency
  batch

WriteCoalescingPolicy:
  mode
  flush_window_ms
  byte_budget
  flush_on_close
```

Initial defaults:

- `latency`: enabled, 1 second flush window, 4 MiB byte budget, flush on close
- `batch`: enabled, 60 second flush window, 4 MiB byte budget, flush on close
- `strict`: disabled, current write-through behavior

The implementation exposes kernel-level setters so `NexusFS` and future workspace registry code can apply a per-workspace policy. If no explicit workspace policy exists, the kernel uses `strict` write-through behavior to preserve existing metadata visibility semantics.

Path-to-policy lookup will be prefix based. The workspace registry already treats workspace roots as paths, and `WorkspaceManager.create_snapshot` accepts `workspace_path`. The kernel policy store will use canonical VFS path prefixes and choose the longest matching prefix for a write path. A root policy applies as the fallback.

## Dirty Entry Model

The write buffer tracks dirty state by the routed logical file identity:

```text
DirtyWrite:
  path
  zone_id
  backend_path
  mount_point
  content_bytes
  old_content_id
  old_size
  old_version
  old_modified_at_ms
  is_new
  first_dirty_at
  last_dirty_at
  dirty_bytes
```

For a full-file write at offset 0, `content_bytes` becomes the new full file image. For partial writes, the buffer composes bytes using POSIX pwrite semantics:

- if the file is already dirty, splice into the buffered bytes
- if the file is clean and offset is greater than 0, read the current file content once, splice into it, and keep the result dirty
- if offset extends past current size, zero-fill the gap

This choice deliberately flushes full file images. It gives all backends one consolidated write using their existing `write_content(..., offset=0)` support. It also avoids silently performing repeated read-modify-write cycles against object stores that do not support efficient partial writes.

## Write Data Flow

`Kernel::sys_write` will keep the existing front half unchanged:

1. validate path
2. reject or miss trie-resolved virtual paths
3. check write permission
4. run native pre-hooks and apply replacement bytes
5. route through `VFSRouter`
6. load metadata for DT_LINK, DT_PIPE, and DT_STREAM handling

DT_PIPE and DT_STREAM continue using their current immediate paths.

For DT_REG:

1. Resolve the write coalescing policy for the path.
2. If policy mode is `strict`, call the current backend write plus metastore commit implementation.
3. Otherwise acquire the VFS write lock.
4. Merge the write into the dirty entry.
5. Return `SysWriteResult` from the dirty entry's projected metadata.
6. If dirty bytes exceed the byte budget, flush that entry synchronously before returning.
7. Dispatch write observer and post-hook behavior only when the dirty entry is flushed to the backend and metastore.

Deferring observer and post-hook dispatch until flush makes those events describe committed backend state. The API still returns bytes-written immediately, but downstream systems that consume metadata changes see the consolidated committed write.

## Read Data Flow

`Kernel::sys_read` will check the write buffer after validation, permission, hooks, route, metadata lookup, DT_LINK resolution, and IPC handling, but before backend DT_REG read.

If a dirty entry exists for the routed path, `sys_read` returns bytes from the dirty entry. Offset and count slicing remain the Python wrapper's responsibility as today. `content_id` in `SysReadResult` is the projected dirty content id when available, or the last committed content id before flush if the dirty entry has not been hashed yet.

If no dirty entry exists, `sys_read` follows the current backend path unchanged.

## Flush Semantics

Add a kernel API for explicit flush:

```text
flush_write_buffer(path: Option<&str>, zone_id: Option<&str>) -> FlushWriteBufferResult
```

Flush behavior:

1. Select matching dirty entries by exact path, prefix path, zone, or all entries.
2. For each entry, acquire the VFS write lock in stable path order.
3. Write the consolidated full bytes to the routed backend with offset 0.
4. Build metadata using the old metadata snapshot and new backend write result.
5. Commit metadata through the same per-route metastore path as current `sys_write`.
6. Dispatch mutation observers and native post-hooks once per flushed file.
7. Remove the dirty entry only after backend write and metastore commit both succeed.

Failures leave the dirty entry in memory and return per-path error details. A later flush can retry.

Flush triggers:

- time window: background worker wakes and flushes entries whose `last_dirty_at` exceeds the policy window
- byte budget: synchronous flush of the affected entry
- close: `NexusFS.close` calls `flush_write_buffer(all)` before closing IPC primitives, services, and metastores
- sync/fsync: new Python-facing method calls `flush_write_buffer(path)` or `flush_write_buffer(all)`
- snapshot: `WorkspaceManager.create_snapshot` calls `flush_write_buffer(workspace_path prefix)` before reading the metastore for manifest creation

## Background Worker

`Kernel` already owns a Tokio runtime. The write buffer will use that runtime to run a lightweight periodic task or a blocking thread plus condition variable. The worker only decides which entries are due. Actual flush code will share the same synchronous helper used by explicit flush paths.

The worker must stop during kernel teardown after flushing pending entries best-effort. Explicit close remains the primary correctness path; the background worker is for latency-window behavior, not final durability.

## Snapshot Semantics

Workspace snapshots must never point at stale backend content. Before `WorkspaceManager.create_snapshot` calls `metastore_list_iter`, it will call the kernel flush API for the workspace prefix:

```text
flush_write_buffer(prefix=workspace_path)
```

If the flush fails, snapshot creation fails. This is intentionally stricter than best-effort close because a snapshot manifest is a durable pointer to content. A snapshot with unflushed dirty bytes would be incorrect.

Transactional snapshot hooks continue to track committed writes. If a caller uses transactional snapshot APIs around buffered writes, only flushed writes enter the committed metadata timeline. API documentation will state that transaction commit/snapshot boundaries force a flush.

## Sync And Close Semantics

Expose these synchronous Python methods on `NexusFS` and wire them through generated PyO3 bindings plus RPC aliasing:

- `flush_write_buffer(path: str | None = None, zone_id: str | None = None)`
- `fsync(path: str)`
- `sync(zone_id: str | None = None)`

`NexusFS.close` will call the method before:

- close callbacks
- pipe and stream close
- transport pool close
- service close
- `release_metastores`

This ordering preserves pending file bytes before services and metastores disappear.

The POSIX-style `sync` and `fsync` surfaces map to the same kernel call:

- `fsync(path)`: flush one dirty file
- `sync()`: flush all dirty files for the current zone or all zones, depending on the caller context

## Durability Contract

Write coalescing is a write-back cache. Dirty bytes are at risk until flushed.

Documented guarantee by mode:

- `strict`: no coalescing; write-through behavior matches today's durability.
- `latency`: successful write returns after memory buffering; up to 1 second or 4 MiB per dirty file can be lost on process crash or power loss.
- `batch`: successful write returns after memory buffering; up to 60 seconds or 4 MiB per dirty file can be lost on process crash or power loss.

Explicit `flush_write_buffer`, `sync`, `fsync`, snapshot commit, and close reduce the at-risk window by forcing backend and metastore commit.

## Error Handling

Buffered writes can fail at merge time only for validation, permission, lock timeout, route errors, metadata read errors for partial writes, or memory allocation failures. Backend failures are deferred to flush.

Flush errors return structured per-path results. For explicit flush calls, any failed path makes the call fail. For background flush, errors are logged and dirty state remains queued for retry.

If a path is deleted or renamed while dirty, the implementation must flush or discard in a defined order:

- `sys_unlink(path)`: flush the dirty file first, then delete using existing logic. If flush fails, unlink fails.
- `sys_rename(old, new)`: flush the dirty source first, then rename using existing logic. If flush fails, rename fails.
- `sys_copy(src, dst)`: read source through dirty-buffer-aware `sys_read`, then write destination through normal policy.

These rules keep metadata and backend content from diverging.

## Tests

Rust unit tests will cover the write buffer directly:

- full-write overwrite coalesces multiple writes to one dirty entry
- partial write splices into clean current content
- partial write splices into already dirty content
- sparse partial write zero-fills gaps
- read-your-own-writes returns dirty bytes before flush
- byte-budget trigger flushes synchronously
- strict policy bypasses the buffer
- flush success removes dirty entry
- flush failure keeps dirty entry for retry
- close/sync-style flush drains all entries

Kernel-level tests will use a counting `ObjectStore` test double:

- 100 writes to one file under buffered policy produce no more than 10 backend writes
- same workload under strict policy produces 100 backend writes
- metadata version advances once per flushed coalesced file
- post-hooks and observers fire once per flushed file

Python/service tests will cover integration points:

- `NexusFS.flush_write_buffer(path)` exposes force-flush
- `NexusFS.close` flushes pending dirty bytes before metastore release
- `WorkspaceManager.create_snapshot` flushes workspace prefix before manifest build
- snapshot creation fails if the prefix flush fails

## Benchmark

Add a Rust benchmark or targeted test benchmark for a bursty workload:

```text
write /workspace/burst.txt 100 times with small payloads
flush
assert backend write count <= 10
compare strict mode backend write count == 100
```

The benchmark should report:

- policy mode and flush window
- payload size
- write count
- backend write count
- elapsed time
- backend write reduction ratio

The acceptance result is at least 10x fewer backend writes for the 100-write burst. The result and command will be documented in an appropriate performance file or issue-linked benchmark note.

## Documentation

Update architecture or API documentation to describe:

- write coalescing modes
- strict default policy with opt-in latency/batch modes
- flush window and byte-budget triggers
- read-your-own-writes behavior
- `flush_write_buffer` or sync/fsync API
- snapshot flush requirement
- power-loss durability tradeoff

The docs must explicitly state that non-strict modes acknowledge writes before backend durability.

## Acceptance Mapping

- Per-workspace flush policy: prefix-based policy store with longest-prefix lookup.
- 1s default flush, configurable: latency default plus setter/getter APIs.
- Read-your-own-writes preserved: `sys_read` consults dirty entries before backend read.
- `sync` / `fsync` / `close` force flush: kernel flush API exposed through `NexusFS`; close calls it before teardown.
- Snapshot commit forces flush before manifest write: `WorkspaceManager.create_snapshot` flushes the workspace prefix before listing metadata.
- Power-loss durability documented: docs describe strict, latency, and batch at-risk windows.
- Benchmark 100-write workload reduces backend RPC count by at least 10x: counting backend benchmark/test records the reduction.

## Open Implementation Notes

1. The implementation should extract the existing backend-write-plus-metastore-commit block from `Kernel::sys_write_with_link_depth` into a private helper before adding buffering. This reduces duplication and lets direct writes and flushes share one commit path.
2. The buffer should avoid holding the global dirty map lock while doing backend I/O. Select entries, mark them flushing, release the map lock, then perform writes.
3. The flush helper must re-route at flush time or validate that the stored route is still current. Re-routing is safer when mounts can change.
4. If adding new generated PyO3 bindings requires running `scripts/codegen_kernel_abi.py`, include generated files in the implementation plan rather than hand-editing generated ABI code.
