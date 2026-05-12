# Design: Vectored `_read_batch` (Issue #4058)

**Status:** Approved (brainstorming) — pending implementation plan
**Issue:** https://github.com/nexi-lab/nexus/issues/4058
**Date:** 2026-05-11
**Owner:** windoliver

## 1. Problem

`rust/kernel/src/kernel/io.rs:2567` `Kernel::_read_batch` is a thin stub: it
rayon-parallelizes `sys_read` over a `&[String]` of paths and collapses any
per-request failure to `data: None`. It does not:

- Accept `(path, offset, len)` ranges.
- Coalesce same-blob requests into one backend fetch.
- Bound parallelism configurably.
- Surface per-request error detail.
- Expose itself over gRPC.

FUSE syscall overhead dominates small-read workloads (To FUSE or Not to FUSE,
FAST '17). Agents loading many small files (configs, prompts, traces) pay
N round-trips. A real batched read API cuts that to one.

## 2. Goals

| Goal | Acceptance criterion |
|---|---|
| Vectored API | Accept `Vec<(path, offset, len)>` |
| Single-pass routing | One trie/route walk for the whole batch |
| Coalescing | Multiple requests sharing a `content_id` → one backend fetch |
| Bounded parallelism | Configurable max-in-flight |
| Transport | gRPC `BatchRead` RPC |
| Performance | 100-file batch ≥ 3× faster than 100 sequential reads (Criterion-asserted) |
| Error semantics | Per-request `Result`; partial success allowed |

## 3. Non-goals (explicit YAGNI)

- FUSE batched ops — deferred to a follow-up issue.
- `read_range(content_id, offset, len)` on the `ObjectStore` backend trait —
  whole-blob + in-kernel slice is sufficient for the small-files workload
  that motivates this issue. Defer true byte-range to a follow-up.
- gRPC streaming response chunking for very large batches.
- Cross-batch global rate limiting.
- A batched permission-check primitive (existing per-path `check_permission`
  in a loop is fine; revisit only if profiling shows it dominates).

## 4. Architecture

Four layers, each isolated by a typed interface.

```
Python read_bulk (nexus_fs_content.py)
  └── PyO3 _read_batch (generated_kernel_abi_pyo3.rs)
        └── Kernel::_read_batch (kernel/io.rs)   ←── gRPC BatchRead handler (transport/grpc.rs)
              └── coalesce_by_content_id
              └── rayon bounded fan-out
                    └── route.backend.read_content(...)  +  file_cache  +  try_remote_fetch
```

### 4.1 Kernel API

```rust
pub struct BatchReadRequest {
    pub path: String,
    pub offset: u64,
    pub len: Option<u64>,   // None ⇒ entire file from offset
}

pub fn _read_batch(
    &self,
    reqs: &[BatchReadRequest],
    ctx: &OperationContext,
) -> Result<Vec<Result<SysReadResult, KernelError>>, KernelError>;
```

Outer `Err` reserved for kernel-wide setup failure (no metastore wired). Each
inner `Result` carries one request's outcome. Input order preserved.

### 4.2 PyO3 ABI

`_read_batch` accepts either shape (back-compat):

- `list[str]` — legacy callers; interpreted as `(p, 0, None)` for each path.
- `list[tuple[str, int, int | None]]` — new shape.

Returns a Python list whose items are either:
- Success: existing `SysReadResult`-shaped object.
- Error: an object/dict with `path`, `error_kind` (string), `error_message`.

### 4.3 Python wrapper

`src/nexus/core/nexus_fs_content.py:1708` switches its kernel call to:

```python
rust_results = self._kernel._read_batch(
    [(p, 0, None) for p in allowed_paths], _rust_ctx
)
```

Per-item mapping changes from "`data is None` → fallback to single read" to
"check Ok/Err discriminant explicitly." Existing fallback path for
DT_PIPE/DT_STREAM/external-mount stays — those surface as `Err(IOError(...))`
items, which the wrapper routes through the single-file fallback.

### 4.4 gRPC

Add to `proto/nexus/grpc/vfs/vfs.proto`:

```proto
service NexusVFSService {
  rpc BatchRead(BatchReadRequest) returns (BatchReadResponse);
}

message BatchReadRequest {
  string auth_token = 1;
  repeated BatchReadItemRequest items = 2;
}

message BatchReadItemRequest {
  string path = 1;
  uint64 offset = 2;
  uint64 len = 3;          // 0 ⇒ entire file from offset (sentinel)
}

message BatchReadResponse {
  repeated BatchReadItemResponse results = 1;  // input order
}

message BatchReadItemResponse {
  bytes content = 1;
  bool is_error = 2;
  bytes error_payload = 3;  // JSON error dict (same shape as CallResponse.error)
  string content_id = 4;
  uint64 gen = 5;
}
```

Server handler in `rust/transport/src/grpc.rs` decodes items, calls
`Kernel::_read_batch`, maps each per-item `Result` to `BatchReadItemResponse`.
Outer kernel error → `tonic::Status::internal(...)`. Server enforces the
same 100 MB aggregate-response ceiling Python applies upfront.

## 5. Data flow inside `_read_batch`

1. **Validate** every path (`validate_path_fast`); failures become per-item
   `Err(InvalidPath)`.
2. **Route** every valid path via `VFSRouter::route` once; failures →
   per-item `Err(FileNotFound)`.
3. **Permission check** each path via `check_permission`; denials →
   per-item `Err(PermissionDenied)`.
4. **Metastore lookup**, grouped per mount-point so per-mount metastores get
   one batched call where they expose one.
5. **file_cache fast path**, per request: `file_cache.get((zone_id, path,
   "raw"))`. Hits skip coalescing/backend entirely and go straight to step 7.
6. **Coalesce** remaining misses by `(mount_point, content_id)`. Requests
   without a content_id (cold metadata / PAS paths missing it) form singleton
   groups. Each group remembers the input indices it serves.
7. **Fan out** distinct groups across rayon at a bounded width
   (`read_batch_max_concurrency`, default 16). Each group:
   - `route.backend.read_content(content_id_or_path)`.
   - Miss + `last_writer_address` set → `try_remote_fetch`.
   - On success: `file_cache.put((zone_id, path, "raw"), bytes)` for each
     path served by the group (matches `sys_read`'s per-path cache key).
8. **Slice** the resulting bytes per request: `data[offset..offset+len]`.
   Out-of-range offset → empty data, still `Ok`. Overshoot → truncated.
9. **Scatter** results back to their original input positions.
10. **Hooks**: PRE-read hook (`dispatch_native_pre`) fires per request inside
   the per-request work, matching `sys_read` semantics. POST-read remains
   Python-side (`on_post_read_batch`).

## 6. Concurrency / config

New field on `KernelConfig`:

```rust
pub read_batch_max_concurrency: usize,  // default: 16
```

Implementation uses `rayon::par_chunks(chunk_size)` over the distinct
content-id groups, where `chunk_size = groups.len().div_ceil(max_concurrency)`.
This caps in-flight groups at `max_concurrency` without introducing async
primitives or extra deps. Inside each chunk the worker iterates sequentially
over its assigned groups.

The same config drives the gRPC server handler (one Kernel instance, one
knob).

## 7. Error model

Per-item `Result<SysReadResult, KernelError>`:

| Failure | Variant |
|---|---|
| Path validation | `Err(KernelError::InvalidPath)` |
| Route miss / metastore miss / backend miss | `Err(FileNotFound)` |
| Permission denied | `Err(PermissionDenied)` |
| Backend read failure, lock timeout | `Err(IOError)` |
| DT_PIPE / DT_STREAM in batch | `Err(IOError("batch read does not support pipes/streams"))` |
| Range offset ≥ size | `Ok(SysReadResult{ data: Some(empty), ... })` |
| Range offset+len > size | `Ok` with truncated slice |

Outer `Err` only when the kernel cannot service any request (no metastore
wired, etc.).

## 8. Tests

### 8.1 Kernel unit tests (inline `#[cfg(test)]` in `kernel/io.rs`)

- `read_batch_empty_input` → `Ok(vec![])`.
- `read_batch_all_hit_100_paths` → all `Ok`, order preserved.
- `read_batch_mixed_errors` → some `FileNotFound`, some `PermissionDenied`,
  some `Ok`; verify discriminant and positions.
- `read_batch_coalesces_same_content_id` → 50 requests with one content_id;
  mock backend asserts `read_content` called exactly once.
- `read_batch_range_slicing` → boundary cases (0/whole, mid/mid,
  offset≥size→empty, offset+len>size→truncated).
- `read_batch_bounded_concurrency` → instrumented mock backend tracks
  max-in-flight; assert ≤ `max_concurrency`.
- `read_batch_rejects_pipe_stream` → DT_PIPE/DT_STREAM entries → per-item
  `Err(IOError)`.
- `read_batch_pre_hook_fires_per_request` → counting hook sees N calls.

### 8.2 Criterion benchmark (`rust/kernel/benches/read_batch.rs`)

Setup: tempdir + `LocalMetaStore` + path-local backend, 100 × 1 KB files.

- Bench A: 100× sequential `sys_read`.
- Bench B: 1× `_read_batch(100 reqs)`.

Separate `#[test] read_batch_speedup_meets_target` runs both with `Instant`,
asserts `B.mean ≤ A.mean / 3.0`. Gated behind `NEXUS_BENCH=1` env var to
avoid CI timing flakiness while still failing locally when the speedup
regresses.

### 8.3 gRPC integration test

Existing transport test pattern: spin up in-process server, round-trip
`BatchRead` with 3 paths (one OK, one not-found, one permission-denied).
Assert ordering preserved and per-item error mapping correct.

### 8.4 Python smoke test

`tests/.../test_nexus_fs_content.py` (or nearest existing):
- `read_bulk(["a","b","c"])` continues to return existing shape (back-compat).
- `read_bulk(["a","missing"], partial=True)` produces the same per-item dict
  shape as before.

## 9. Touch points

| File | Change |
|---|---|
| `rust/kernel/src/kernel/io.rs:2567` | Replace stub with full impl + `coalesce_by_content_id` helper + inline tests |
| `rust/kernel/src/kernel/mod.rs` | Expose `BatchReadRequest` struct |
| `rust/kernel/src/config.rs` | Add `read_batch_max_concurrency: usize` (default 16) |
| `rust/kernel/src/generated_kernel_abi_pyo3.rs:2707` | Accept both legacy `list[str]` and new `list[(str, int, int\|None)]`; emit per-item Ok/Err |
| `rust/kernel/benches/read_batch.rs` | New Criterion bench + speedup-assert test |
| `src/nexus/core/nexus_fs_content.py:1708` | Switch kernel call to new shape; remap per-item Ok/Err |
| `proto/nexus/grpc/vfs/vfs.proto` | Add `BatchRead` RPC + messages |
| `rust/transport/src/grpc.rs` | Implement `BatchRead` server handler |
| `rust/transport/tests/...` | Add gRPC round-trip test |

## 10. Acceptance-criteria mapping

| Issue criterion | Where met |
|---|---|
| `_read_batch()` implemented end-to-end | §4.1, §5, §9 |
| Adjacent range coalescing | §5 step 6 — coalesce by content_id; "adjacent" reduces to "same blob" for whole-blob model |
| Bounded parallelism (configurable) | §6 |
| gRPC `BatchRead` exposed | §4.4 |
| Benchmark ≥ 3× | §8.2 |
| Per-request Result, partial-success allowed | §7 |

## 11. References

- Issue #4058
- *To FUSE or Not to FUSE*, FAST '17
- *Tectonic*, FAST '21 (batched metadata ops)
- Existing stub: `rust/kernel/src/kernel/io.rs:2567`
- Existing Python caller: `src/nexus/core/nexus_fs_content.py:1599` (`read_bulk`)
