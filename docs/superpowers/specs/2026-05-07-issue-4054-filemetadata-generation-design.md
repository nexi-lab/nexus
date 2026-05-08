# Issue #4054 - FileMetadata Generation Design

**Date**: 2026-05-07
**Issue**: [#4054](https://github.com/nexi-lab/nexus/issues/4054) - [P0] Add generation numbers to FileMetadata for optimistic concurrency

## Context

Nexus stores `content_id` in file metadata as an opaque backend content handle. For CAS backends this is a content hash; for PAS and connector backends it can be a path or backend resource id. That value is useful for retrieving content but it is not a monotonic per-file sequence number.

`FileMetadata` also has `version: u32`, and the Rust write path currently increments it on `sys_write`. That field already acts as a broad metadata revision in several call sites, and it is too narrow and overloaded for long-lived content-generation semantics.

Issue #4054 asks for a monotonic `gen: u64` so clients can do cheap write concurrency checks, external mutation detection, HTTP-style validators, and FUSE content-cache invalidation without going through heavier transaction snapshot flows.

## Decision

Add `gen: u64` as the file-content generation counter.

`gen` changes only when file bytes are successfully written. Metadata-only mutations do not bump it. This keeps `gen` separate from the existing `version` field:

- `content_id`: opaque backend content identity used for storage reads.
- `version`: existing metadata revision counter.
- `gen`: monotonic content generation for a file path.

New file content written through `sys_write` starts at `gen = 1`. Existing metadata records that predate the field deserialize as `gen = 0`. Every successful later content write sets `gen = old.gen + 1`.

## Non-goals

1. Replace `content_id` with `gen`.
2. Replace or remove `version`.
3. Implement the full optimistic-write API surface in this change. The field is the foundation that later OCC code can compare.
4. Bump `gen` for directory creation, rename, MIME updates, link creation, mount metadata, or auxiliary metadata.
5. Add database columns for Python SQL metadata stores in this first pass. Python metadata JSON/proto paths gain the field; legacy SQL-backed rows default it to zero until a later SQL migration stores it.

## Metadata Contract

Rust kernel metadata gains:

```rust
pub struct FileMetadata {
    pub gen: u64,
    // existing fields unchanged
}
```

`StatResult` gains `gen: u64`, and the PyO3 `sys_stat` dict includes `"gen": <u64>`.

The generated Python `nexus.contracts.metadata.FileMetadata` dataclass gains `gen: int = 0`. `MetadataMapper` includes `gen` in JSON and proto conversion and strips unknown future keys as it already does.

## Persistence

The local redb metastore currently serializes `FileMetadata` with a version tag byte. Add a v4 encoding that appends `gen` as a trailing `u64`.

Deserializer behavior:

- v4 records read `gen` from the trailing field.
- v3 records remain readable and return `gen = 0`.
- Empty or malformed records remain errors.

The write path always persists v4 records after this change. This is the migration path: old redb files do not require an offline rewrite, and every rewritten record upgrades in place.

Raft metadata uses `proto/nexus/core/metadata.proto`, so adding `uint64 gen = 18` is an additive protobuf change. Older peers that do not know the field ignore it; upgraded peers preserve it through the generated proto structs.

## Write Semantics

`Kernel::sys_write` already loads old metadata before building the replacement record. It should compute:

```rust
let old_gen = old_entry.as_ref().map(|e| e.gen).unwrap_or(0);
let new_gen = old_gen.saturating_add(1);
```

The new metadata record stores `gen = new_gen`. The returned `SysWriteResult` should also carry `gen` so transport and hooks can report it without an extra metadata lookup.

Batch writes use the same rule per path: read the prior metadata for that path, then write `old.gen + 1` in the batch metadata record. Distinct paths have independent counters.

Copy writes destination metadata with a destination generation. If the destination path is new, `gen = 1`. If the copy overwrites an existing destination, `gen = old_destination.gen + 1`. This applies even when a same-CAS copy can reuse the source `content_id` without rewriting bytes, because the destination file's observable content state changed. The destination does not inherit the source file's generation.

Metadata-only paths preserve existing `gen`:

- `setattr_update` clones current metadata and leaves `gen` untouched.
- `rename` moves metadata and leaves `gen` untouched.
- `mkdir`, pipe, stream, mount, and link metadata entries use `gen = 0`.

## Transport And API Exposure

Add `gen` to `proto/nexus/core/metadata.proto` as field 18.

For typed VFS gRPC, expose the resulting generation on write/read responses:

- `ReadResponse.gen = 6`
- `WriteResponse.gen = 5`

`rust/transport/src/grpc.rs` fills those fields from `SysReadResult` and `SysWriteResult`. Error responses set `gen = 0`.

The generic `Call("sys_stat")` path already returns the PyO3 stat dict. Adding `"gen"` to that dict exposes the value to HTTP JSON-RPC clients, the Rust FUSE daemon's `stat`, and Python callers using `NexusFS.sys_stat`.

## FUSE Cache Behavior

The Rust FUSE client metadata structs gain `gen: u64`.

The persistent SQLite content cache stores the generation alongside each cached body:

```sql
ALTER TABLE file_cache ADD COLUMN gen INTEGER NOT NULL DEFAULT 0;
```

Cache lookup compares the current stat generation with the cached generation. A mismatch is a miss and invalidates the row before reading from the server. A matching generation can use the existing freshness and ETag revalidation flow.

This means the cache key is effectively `(path, gen)`, while the schema can remain path-primary by storing `gen` as a validated column. Local FUSE writes already call `invalidate_path`; the generation check covers remote or external writes where the path is unchanged but bytes changed.

Add FUSE xattr support for:

```text
user.nexus.gen
```

`getxattr("user.nexus.gen")` returns the decimal ASCII representation of the current stat generation. `listxattr` includes `user.nexus.gen`. `setxattr` for this name is rejected as read-only.

## Error Handling

- If the metastore cannot read old metadata during a write, preserve existing behavior: the write fails or falls back exactly as it does today. Do not silently reset an existing generation.
- If a migrated record has no `gen`, use zero. The first subsequent content write becomes generation one.
- If `gen` reaches `u64::MAX`, `saturating_add` keeps it at `u64::MAX`. That avoids wraparound and keeps comparison semantics safe.
- FUSE stat failures while checking generation are treated as cache misses only if the content read succeeds. If stat and read both fail, surface the read/stat error as today.

## Testing

Rust kernel and metastore tests:

1. redb serialization round-trip preserves `gen`.
2. v3 redb serialization without `gen` deserializes with `gen = 0`.
3. first `sys_write` on a new file reports and stores `gen = 1`.
4. second write to the same file reports and stores `gen = 2`.
5. concurrent writes to the same path serialize through the existing VFS write lock and produce distinct observed generations.
6. metadata-only `sys_setattr` and rename preserve `gen`.
7. batch writes increment each path independently.

Proto and mapper tests:

1. Rust `kernel_to_proto` and `proto_to_kernel` preserve `gen`.
2. Python `MetadataMapper.to_proto/from_proto` and `to_json/from_json` preserve `gen`.
3. Unknown/missing `gen` in JSON defaults to zero.

FUSE tests:

1. cached content is reused when path and `gen` match.
2. cached content is invalidated when the server stat generation changes.
3. `getxattr("user.nexus.gen")` returns the decimal generation.
4. `listxattr` includes `user.nexus.gen`.
5. `setxattr("user.nexus.gen")` fails read-only.

## Rollout

The change is additive at API boundaries. Existing metadata records default to `gen = 0`, and upgraded writers start producing v4 redb rows and populated proto/stat responses. Old clients that ignore `gen` keep working. New clients can rely on `gen > 0` only after the file has been written by an upgraded writer.
