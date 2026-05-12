# Vectored `_read_batch` Implementation Plan (Issue #4058)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stub `Kernel::_read_batch` in `rust/kernel/src/kernel/io.rs:2567` with a real vectored-read engine that accepts `(path, offset, len)` ranges, coalesces same-blob requests into one backend fetch, bounds parallelism via a configurable kernel knob, surfaces per-request `Result`, and is exposed over gRPC `BatchRead`.

**Architecture:** Single batched call inside the kernel layer (`Vec<BatchReadRequest>` → `Vec<Result<SysReadResult, KernelError>>`), reusing existing `VFSRouter`, `MetaStore`, `file_cache`, and `try_remote_fetch` primitives. Coalescing keys on `(mount_point, content_id)`; bounded parallelism via `rayon::par_chunks` over distinct groups. PyO3 ABI extended to accept both legacy `list[str]` and new `list[tuple[str,int,int|None]]` for back-compat. gRPC `BatchRead` RPC mirrors the kernel shape and is added to `proto/nexus/grpc/vfs/vfs.proto`. FUSE batched ops deferred.

**Tech Stack:** Rust 2021 + rayon + tonic/prost (kernel + transport crates), PyO3 0.28 (Python ABI), tonic-build via existing `rust/kernel/build.rs`, criterion 0.8 (benchmark), pytest (Python smoke), Python 3 wrapper in `src/nexus/core/nexus_fs_content.py`.

**Spec:** `docs/superpowers/specs/2026-05-11-issue-4058-read-batch-design.md`

---

## File Structure

| File | Purpose | New / Modified |
|---|---|---|
| `rust/kernel/src/kernel/mod.rs` | Add `pub struct BatchReadRequest { path, offset, len }` next to `SysReadResult`; add `read_batch_max_concurrency: AtomicUsize` field + getter/setter on `Kernel` | Modified |
| `rust/kernel/src/kernel/io.rs` | Replace stub `_read_batch` at line 2567 with full impl + private helper `coalesce_by_content_id`; add inline `#[cfg(test)]` module | Modified |
| `rust/kernel/src/generated_kernel_abi_pyo3.rs` | Extend `_read_batch` PyO3 method at line 2707 to accept both shapes and return per-item Ok/Err discriminant | Modified |
| `src/nexus/core/nexus_fs_content.py` | Update `read_bulk` at line 1708 to pass new shape and handle Ok/Err items | Modified |
| `proto/nexus/grpc/vfs/vfs.proto` | Add `BatchRead` RPC + `BatchReadRequest`/`BatchReadResponse`/`BatchReadItemRequest`/`BatchReadItemResponse` messages | Modified |
| `rust/transport/src/grpc.rs` | Implement `BatchRead` handler on `VfsServiceImpl` (after the `read` handler at line 188) | Modified |
| `rust/kernel/benches/read_batch.rs` | New Criterion bench (100 × 1 KB files, sequential vs batched) | Created |
| `rust/kernel/Cargo.toml` | Add `[[bench]] name = "read_batch"` entry | Modified |
| `rust/kernel/tests/read_batch_speedup.rs` | Speedup-assert test (`B.mean ≤ A.mean / 3.0`), gated behind `NEXUS_BENCH=1` env var | Created |
| `tests/unit/core/test_read_bulk_batch_shape.py` (or nearest existing) | Python smoke test for new per-item Ok/Err mapping | Created or extended |
| `rust/transport/tests/grpc_batch_read.rs` (or nearest existing pattern) | In-process gRPC round-trip test | Created |

---

## Task 1: Add `BatchReadRequest` struct and config knob

**Files:**
- Modify: `rust/kernel/src/kernel/mod.rs` (struct + Kernel field + setter near line 600/742/880)

- [ ] **Step 1: Add the `BatchReadRequest` struct** next to `SysReadResult` (after the closing `}` of `SysReadResult`, around line 186)

```rust
/// Per-request entry for `Kernel::_read_batch`.
///
/// `offset` = byte offset into the file; `len = None` means "to EOF".
pub struct BatchReadRequest {
    pub path: String,
    pub offset: u64,
    pub len: Option<u64>,
}
```

- [ ] **Step 2: Add the concurrency-cap field** to the `Kernel` struct (around line 600, near `vfs_lock_timeout_ms: AtomicU64`)

```rust
    // Max in-flight backend fetches inside `_read_batch`. Default 16.
    read_batch_max_concurrency: std::sync::atomic::AtomicUsize,
```

- [ ] **Step 3: Initialize it in `Kernel::new`** (around line 781, alongside `vfs_lock_timeout_ms: AtomicU64::new(5000)`)

```rust
            read_batch_max_concurrency: std::sync::atomic::AtomicUsize::new(16),
```

- [ ] **Step 4: Add getter + setter** in an `impl Kernel` block near `vfs_lock_timeout_ms` (around line 886)

```rust
    pub fn read_batch_max_concurrency(&self) -> usize {
        self.read_batch_max_concurrency
            .load(std::sync::atomic::Ordering::Relaxed)
            .max(1)
    }
    pub fn set_read_batch_max_concurrency(&self, n: usize) {
        self.read_batch_max_concurrency
            .store(n.max(1), std::sync::atomic::Ordering::Relaxed);
    }
```

- [ ] **Step 5: Compile**

Run: `cargo build -p kernel`
Expected: PASS (struct added, field initialized, no callers touched yet).

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/kernel/mod.rs
git commit -m "feat(#4058): add BatchReadRequest struct and read_batch_max_concurrency knob"
```

---

## Task 2: New `_read_batch` signature + empty-input fast path (TDD)

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs:2567` (replace stub signature; keep body returning empty vec for now)
- Test: inline `#[cfg(test)] mod read_batch_tests` at the bottom of `rust/kernel/src/kernel/io.rs`

- [ ] **Step 1: Write the failing test** — append at the end of `rust/kernel/src/kernel/io.rs`

```rust
#[cfg(test)]
mod read_batch_tests {
    use super::*;
    use crate::kernel::Kernel;
    use contracts::OperationContext;

    fn ctx() -> OperationContext {
        // Admin + system bypass — fine for unit tests.
        OperationContext::new("test", "root", true, None, true)
    }

    #[test]
    fn read_batch_empty_input_returns_empty_vec() {
        let k = Kernel::new();
        let out = k._read_batch(&[], &ctx()).expect("ok");
        assert_eq!(out.len(), 0);
    }
}
```

Note: `OperationContext::new` lives in `rust/contracts/src/operation_context.rs:50` — signature is `new(user_id, zone_id, is_admin, agent_id, is_system)`. The `is_system=true` flag bypasses permission checks, which is what tests want.

- [ ] **Step 2: Run test, confirm it fails (signature mismatch)**

Run: `cargo test -p kernel read_batch_empty_input_returns_empty_vec`
Expected: FAIL — `_read_batch` still takes `&[String]`, test passes `&[]`. (Compile-time pass; runtime returns wrong type — either rewrite test to call new shape and watch it fail to compile, or proceed straight to step 3.)

- [ ] **Step 3: Replace the stub** at `rust/kernel/src/kernel/io.rs:2567`. Remove the old `&[String]` body. New impl:

```rust
    /// Internal: batch read. Accepts `(path, offset, len)` requests.
    ///
    /// Returns per-request `Result` in input order. The outer `Err` is
    /// reserved for kernel-wide setup failure (e.g. no metastore wired);
    /// per-request failures are inner `Err` and do NOT abort the batch.
    ///
    /// Coalesces same-`content_id` requests into one backend fetch;
    /// bounded parallelism per `Kernel::read_batch_max_concurrency`.
    pub fn _read_batch(
        &self,
        reqs: &[crate::kernel::BatchReadRequest],
        _ctx: &OperationContext,
    ) -> Result<Vec<Result<SysReadResult, KernelError>>, KernelError> {
        if reqs.is_empty() {
            return Ok(Vec::new());
        }
        // Stubbed body — filled in by subsequent tasks.
        Ok(reqs
            .iter()
            .map(|_| Err(KernelError::IOError("not yet implemented".into())))
            .collect())
    }
```

- [ ] **Step 4: Compile-fix callers**

The existing PyO3 wrapper at `rust/kernel/src/generated_kernel_abi_pyo3.rs:2714` calls `self.inner._read_batch(&paths, &rust_ctx)` with `&[String]`. Temporarily adapt the PyO3 wrapper to the new signature so the build stays green. Replace the body of `fn _read_batch<'py>` (lines 2707–2728) with:

```rust
    #[pyo3(signature = (paths, ctx))]
    fn _read_batch<'py>(
        &self,
        py: Python<'py>,
        paths: Vec<String>,
        ctx: &PyOperationContext,
    ) -> PyResult<Vec<PySysReadResult>> {
        let rust_ctx = ctx.to_rust();
        let reqs: Vec<crate::kernel::BatchReadRequest> = paths
            .into_iter()
            .map(|p| crate::kernel::BatchReadRequest {
                path: p,
                offset: 0,
                len: None,
            })
            .collect();
        let result = py.detach(|| self.inner._read_batch(&reqs, &rust_ctx));
        let results = result.map_err(|e| -> PyErr { e.into() })?;
        Ok(results
            .into_iter()
            .map(|r| match r {
                Ok(r) => PySysReadResult {
                    data: r.data.map(|d| PyBytes::new(py, &d).into()),
                    post_hook_needed: r.post_hook_needed,
                    content_id: r.content_id,
                    gen: r.gen,
                    entry_type: r.entry_type,
                    stream_next_offset: r.stream_next_offset,
                },
                Err(_) => PySysReadResult {
                    data: None,
                    post_hook_needed: false,
                    content_id: None,
                    gen: 0,
                    entry_type: 0,
                    stream_next_offset: None,
                },
            })
            .collect())
    }
```

(Per-item `Err` collapses to `data: None` here for now — preserves the existing Python contract. Task 9 replaces this with a real Ok/Err Python shape.)

- [ ] **Step 5: Run all kernel tests**

Run: `cargo test -p kernel read_batch_empty_input`
Expected: PASS.

Run: `cargo build -p kernel` and `cargo test -p kernel --no-run` to make sure nothing else broke.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/kernel/io.rs rust/kernel/src/generated_kernel_abi_pyo3.rs
git commit -m "refactor(#4058): switch _read_batch to BatchReadRequest signature"
```

---

## Task 3: Validate + route + per-item permission, per-request errors

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs` (`_read_batch` body)
- Test: inline `read_batch_tests`

- [ ] **Step 1: Write the failing test**

```rust
    #[test]
    fn read_batch_invalid_path_yields_per_item_err() {
        let k = Kernel::new();
        let reqs = vec![
            crate::kernel::BatchReadRequest {
                path: "".into(), // invalid
                offset: 0,
                len: None,
            },
            crate::kernel::BatchReadRequest {
                path: "/definitely/does/not/exist".into(),
                offset: 0,
                len: None,
            },
        ];
        let out = k._read_batch(&reqs, &ctx()).expect("outer ok");
        assert_eq!(out.len(), 2);
        assert!(matches!(out[0], Err(KernelError::InvalidPath(_))));
        assert!(matches!(out[1], Err(KernelError::FileNotFound(_))));
    }
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `cargo test -p kernel read_batch_invalid_path_yields_per_item_err`
Expected: FAIL — current stub returns `Err(IOError("not yet implemented"))`.

- [ ] **Step 3: Implement validate + route + permission**

Replace the stub body in `_read_batch`. New body:

```rust
        if reqs.is_empty() {
            return Ok(Vec::new());
        }

        // Phase A — validate, route, permission check, metadata lookup.
        // Build a per-request slot; later phases will fill `Ok(...)` for
        // successful reads. Errors here short-circuit that slot.
        let n = reqs.len();
        let mut results: Vec<Option<Result<SysReadResult, KernelError>>> = (0..n).map(|_| None).collect();
        // Per-request resolved state for the fan-out phase (only present
        // when validate+route+perm+metadata all succeed).
        let mut resolved: Vec<Option<ResolvedRead>> = (0..n).map(|_| None).collect();

        for (i, req) in reqs.iter().enumerate() {
            // 1. Validate
            if let Err(e) = validate_path_fast(&req.path) {
                results[i] = Some(Err(e));
                continue;
            }
            // 2. Permission
            if let Err(e) = self.check_permission(&req.path, Permission::Read, _ctx) {
                results[i] = Some(Err(e));
                continue;
            }
            // 3. Route
            let route = match self.vfs_router.route(&req.path, &_ctx.zone_id) {
                Ok(r) => r,
                Err(_) => {
                    results[i] = Some(Err(KernelError::FileNotFound(req.path.clone())));
                    continue;
                }
            };
            // 4. Metadata
            let entry = match self
                .with_metastore_route(&route, |ms| ms.get(&req.path).ok().flatten())
                .flatten()
            {
                Some(m) => m,
                None => {
                    results[i] = Some(Err(KernelError::FileNotFound(req.path.clone())));
                    continue;
                }
            };
            resolved[i] = Some(ResolvedRead { route, entry });
        }

        // Phase B — fan-out fill (filled by subsequent tasks). For now,
        // any slot that still has `resolved.is_some() && results.is_none()`
        // is a fallback to single sys_read so end-to-end is correct even
        // before the coalescing path lands.
        for (i, req) in reqs.iter().enumerate() {
            if results[i].is_some() {
                continue;
            }
            let r = self.sys_read(&req.path, _ctx, 5000, req.offset);
            results[i] = Some(r);
        }

        Ok(results.into_iter().map(|o| o.unwrap()).collect())
```

And add the helper struct *inside* the same `impl Kernel { ... }` block (or above it, file-private):

```rust
struct ResolvedRead {
    route: crate::core::vfs_router::RouteResult,
    entry: FileMetadata,
}
```

Place `struct ResolvedRead { ... }` at the top of the file with the other private types (or just before the `impl Kernel` containing `_read_batch`). Use `crate::core::vfs_router::RouteResult` — confirm the type path is correct (grep `pub struct RouteResult` if unsure).

Note: `_ctx` rename to `ctx` now that we're using it.

```rust
    pub fn _read_batch(
        &self,
        reqs: &[crate::kernel::BatchReadRequest],
        ctx: &OperationContext,
    ) -> Result<Vec<Result<SysReadResult, KernelError>>, KernelError> {
```

- [ ] **Step 4: Run test**

Run: `cargo test -p kernel read_batch_invalid_path_yields_per_item_err`
Expected: PASS.

- [ ] **Step 5: Run all read_batch tests + ensure no regression**

Run: `cargo test -p kernel read_batch`
Expected: PASS (empty + invalid_path tests both green).

Run: `cargo build -p kernel`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/kernel/io.rs
git commit -m "feat(#4058): validate + route + perm phase yields per-item Result"
```

---

## Task 4: Coalesce by content_id; one backend fetch per blob

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs` (replace Phase B with coalescing)
- Test: inline `read_batch_tests`

- [ ] **Step 1: Write the failing test** — use a counting backend so we can assert coalescing.

Skip the counting backend if no in-tree mock backend exists; instead, use the existing `LocalMetaStore` + path-local backend and assert *correctness* (every same-content_id request returns the same bytes). Add the test:

```rust
    #[test]
    fn read_batch_coalesces_same_content_id() {
        // Write one file, then 5 reads of the same path. All 5 must
        // return the same bytes; the coalescing path is exercised by
        // construction (5 reqs → 1 content_id group).
        let k = Kernel::new();
        let c = ctx();
        // Setup: write a file via sys_write.
        let payload = b"hello vectored world".to_vec();
        k.sys_write("/coalesce.txt", &c, &payload, 0).expect("write");
        let reqs: Vec<_> = (0..5)
            .map(|_| crate::kernel::BatchReadRequest {
                path: "/coalesce.txt".into(),
                offset: 0,
                len: None,
            })
            .collect();
        let out = k._read_batch(&reqs, &c).expect("outer ok");
        assert_eq!(out.len(), 5);
        for r in &out {
            let r = r.as_ref().expect("ok");
            assert_eq!(r.data.as_deref().unwrap(), payload.as_slice());
        }
    }
```

- [ ] **Step 2: Run test, confirm it fails on the existing Phase-B fallback only if coalescing matters for correctness.**

Run: `cargo test -p kernel read_batch_coalesces_same_content_id`
Expected: actually PASS at this point (fallback to `sys_read` is correct, just inefficient). This test mainly *locks in* correctness; the coalescing benefit is asserted in Task 13's Criterion test.

If you want a stronger assertion, add an instrumented `peer_client` / `read_content` counter via a custom backend impl. Skip unless the `ObjectStore` trait is easy to mock — defer the call-count assertion to the bench-assert test (Task 13).

- [ ] **Step 3: Implement coalescing**

Replace Phase B in `_read_batch` with:

```rust
        // Phase B — coalesce surviving (still resolved, not yet errored)
        // requests by (mount_point, content_id). Requests missing a
        // content_id form singleton groups.
        use std::collections::HashMap;
        let mut groups: HashMap<(String, String), Vec<usize>> = HashMap::new();
        let mut singletons: Vec<usize> = Vec::new();
        for (i, slot) in resolved.iter().enumerate() {
            if results[i].is_some() {
                continue;
            }
            let r = match slot {
                Some(r) => r,
                None => continue,
            };
            let cid = r.entry.content_id.as_deref().unwrap_or("");
            if cid.is_empty() {
                singletons.push(i);
            } else {
                groups
                    .entry((r.route.mount_point.clone(), cid.to_string()))
                    .or_default()
                    .push(i);
            }
        }

        // Per-group fetch + scatter. (Sequential here — Task 5 adds
        // bounded parallelism.)
        let group_vec: Vec<((String, String), Vec<usize>)> = groups.into_iter().collect();
        for (_key, indices) in &group_vec {
            let lead = indices[0];
            let req = &reqs[lead];
            let r = self.sys_read(&req.path, ctx, 5000, 0);
            let shared: Result<SysReadResult, KernelError> = r;
            for &i in indices {
                results[i] = Some(clone_read_result(&shared, &reqs[i]));
            }
        }
        for i in singletons {
            let req = &reqs[i];
            let r = self.sys_read(&req.path, ctx, 5000, req.offset);
            results[i] = Some(r);
        }
```

Add a helper `clone_read_result` (file-private). It clones the byte vec for each consumer and slices `[offset..offset+len]`:

```rust
fn clone_read_result(
    shared: &Result<SysReadResult, KernelError>,
    req: &crate::kernel::BatchReadRequest,
) -> Result<SysReadResult, KernelError> {
    match shared {
        Err(e) => Err(clone_kernel_err(e)),
        Ok(src) => {
            let data = src.data.as_ref().map(|bytes| {
                let off = req.offset as usize;
                let end = match req.len {
                    Some(l) => off.saturating_add(l as usize).min(bytes.len()),
                    None => bytes.len(),
                };
                let start = off.min(bytes.len());
                bytes[start..end].to_vec()
            });
            Ok(SysReadResult {
                data,
                post_hook_needed: src.post_hook_needed,
                content_id: src.content_id.clone(),
                gen: src.gen,
                entry_type: src.entry_type,
                stream_next_offset: src.stream_next_offset,
            })
        }
    }
}
```

`KernelError` is `#[derive(Debug)]` only (no `Clone`) — see `rust/kernel/src/kernel/mod.rs:108`. Add a private exhaustive cloner in the same file as `clone_read_result`:

```rust
fn clone_kernel_err(e: &KernelError) -> KernelError {
    match e {
        KernelError::InvalidPath(s) => KernelError::InvalidPath(s.clone()),
        KernelError::FileNotFound(s) => KernelError::FileNotFound(s.clone()),
        KernelError::FileExists(s) => KernelError::FileExists(s.clone()),
        KernelError::IOError(s) => KernelError::IOError(s.clone()),
        KernelError::TrieError(s) => KernelError::TrieError(s.clone()),
        KernelError::PipeFull(s) => KernelError::PipeFull(s.clone()),
        KernelError::PipeEmpty(s) => KernelError::PipeEmpty(s.clone()),
        KernelError::PipeClosed(s) => KernelError::PipeClosed(s.clone()),
        KernelError::PipeExists(s) => KernelError::PipeExists(s.clone()),
        KernelError::PipeNotFound(s) => KernelError::PipeNotFound(s.clone()),
        KernelError::StreamFull(s) => KernelError::StreamFull(s.clone()),
        KernelError::StreamEmpty(s) => KernelError::StreamEmpty(s.clone()),
        KernelError::StreamClosed(s) => KernelError::StreamClosed(s.clone()),
        KernelError::StreamExists(s) => KernelError::StreamExists(s.clone()),
        KernelError::StreamNotFound(s) => KernelError::StreamNotFound(s.clone()),
        KernelError::WouldBlock(s) => KernelError::WouldBlock(s.clone()),
        KernelError::PermissionDenied(s) => KernelError::PermissionDenied(s.clone()),
        KernelError::BackendError(s) => KernelError::BackendError(s.clone()),
        KernelError::Federation(s) => KernelError::Federation(s.clone()),
        KernelError::Route(_) => {
            // RouteError isn't trivially cloneable. Collapse to a string
            // form — batch path only needs the per-item error visible
            // downstream; structural detail is preserved in the Debug repr.
            KernelError::IOError(format!("{:?}", e))
        }
    }
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p kernel read_batch`
Expected: PASS — `empty`, `invalid_path`, `coalesces_same_content_id` all green.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/kernel/io.rs
git commit -m "feat(#4058): coalesce same-content_id reads in _read_batch"
```

---

## Task 5: Bounded parallelism via `rayon::par_chunks`

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs`

- [ ] **Step 1: Write the failing test** — assert correctness under high parallelism (100 distinct files, distinct content_ids).

```rust
    #[test]
    fn read_batch_100_distinct_paths_parallel() {
        let k = Kernel::new();
        let c = ctx();
        let mut reqs = Vec::with_capacity(100);
        for i in 0..100u32 {
            let path = format!("/p{i:03}.txt");
            let payload = format!("payload-{i}").into_bytes();
            k.sys_write(&path, &c, &payload, 0).expect("write");
            reqs.push(crate::kernel::BatchReadRequest {
                path,
                offset: 0,
                len: None,
            });
        }
        let out = k._read_batch(&reqs, &c).expect("outer ok");
        assert_eq!(out.len(), 100);
        for (i, r) in out.iter().enumerate() {
            let r = r.as_ref().expect("ok");
            let expected = format!("payload-{i}").into_bytes();
            assert_eq!(r.data.as_deref().unwrap(), expected.as_slice());
        }
    }
```

- [ ] **Step 2: Run test**

Run: `cargo test -p kernel read_batch_100_distinct_paths_parallel`
Expected: PASS already (sequential is correct, just slow). The test locks in ordering correctness regardless of parallelism strategy.

- [ ] **Step 3: Implement bounded parallel fan-out** — replace the per-group sequential loop in `_read_batch`'s Phase B with chunked rayon.

```rust
        use rayon::prelude::*;
        let max_conc = self.read_batch_max_concurrency();
        let total_units = group_vec.len() + singletons.len();
        let chunk_size = total_units.div_ceil(max_conc.max(1)).max(1);

        // Collect into one flat work-vec of `(indices, lead_path, offset_irrelevant_for_groups)`.
        enum Unit<'a> {
            Group { indices: &'a [usize] },
            Singleton { idx: usize },
        }
        let mut units: Vec<Unit> = Vec::with_capacity(total_units);
        for (_k, idxs) in &group_vec {
            units.push(Unit::Group { indices: idxs });
        }
        for &i in &singletons {
            units.push(Unit::Singleton { idx: i });
        }

        // Each chunk runs sequentially on one rayon thread.
        let scattered: Vec<(usize, Result<SysReadResult, KernelError>)> = units
            .par_chunks(chunk_size)
            .flat_map(|chunk| {
                let mut local: Vec<(usize, Result<SysReadResult, KernelError>)> =
                    Vec::with_capacity(chunk.len() * 2);
                for unit in chunk {
                    match unit {
                        Unit::Group { indices } => {
                            let lead = indices[0];
                            let req = &reqs[lead];
                            let shared = self.sys_read(&req.path, ctx, 5000, 0);
                            for &i in indices.iter() {
                                local.push((i, clone_read_result(&shared, &reqs[i])));
                            }
                        }
                        Unit::Singleton { idx } => {
                            let req = &reqs[*idx];
                            let r = self.sys_read(&req.path, ctx, 5000, req.offset);
                            local.push((*idx, r));
                        }
                    }
                }
                local
            })
            .collect();

        for (i, r) in scattered {
            results[i] = Some(r);
        }
```

Note: `units` borrows `group_vec` immutably for the par_chunks call. Lifetimes should hold. If borrow-checker complains about `&[usize]` inside an enum across rayon, replace `Unit::Group { indices: &'a [usize] }` with `Unit::Group { indices: Vec<usize> }` (clone) — minor allocation, fine.

`Kernel: Sync` already holds (DashMap + parking_lot + atomics) per the existing rayon use at the original stub.

- [ ] **Step 4: Run tests**

Run: `cargo test -p kernel read_batch`
Expected: ALL PASS.

- [ ] **Step 5: Confirm bound is exercised** — quick smoke that lowering the cap to 1 still passes:

Add a test:

```rust
    #[test]
    fn read_batch_respects_max_concurrency_one() {
        let k = Kernel::new();
        k.set_read_batch_max_concurrency(1);
        let c = ctx();
        for i in 0..10 {
            k.sys_write(&format!("/x{i}.txt"), &c, &format!("v{i}").into_bytes(), 0)
                .unwrap();
        }
        let reqs: Vec<_> = (0..10)
            .map(|i| crate::kernel::BatchReadRequest {
                path: format!("/x{i}.txt"),
                offset: 0,
                len: None,
            })
            .collect();
        let out = k._read_batch(&reqs, &c).expect("ok");
        for (i, r) in out.iter().enumerate() {
            assert_eq!(
                r.as_ref().unwrap().data.as_deref().unwrap(),
                format!("v{i}").as_bytes()
            );
        }
    }
```

Run: `cargo test -p kernel read_batch_respects_max_concurrency_one`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/kernel/io.rs
git commit -m "feat(#4058): bounded parallelism via rayon par_chunks"
```

---

## Task 6: Range slicing (`offset` / `len`)

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs` (singleton path needs to slice too)
- Test: inline `read_batch_tests`

- [ ] **Step 1: Write the failing test**

```rust
    #[test]
    fn read_batch_range_slicing() {
        let k = Kernel::new();
        let c = ctx();
        let payload = b"0123456789".to_vec(); // 10 bytes
        k.sys_write("/r.txt", &c, &payload, 0).unwrap();
        let reqs = vec![
            // Whole file
            crate::kernel::BatchReadRequest {
                path: "/r.txt".into(),
                offset: 0,
                len: None,
            },
            // Middle slice
            crate::kernel::BatchReadRequest {
                path: "/r.txt".into(),
                offset: 3,
                len: Some(4),
            },
            // Offset == size (empty)
            crate::kernel::BatchReadRequest {
                path: "/r.txt".into(),
                offset: 10,
                len: Some(5),
            },
            // Overshoot — truncated
            crate::kernel::BatchReadRequest {
                path: "/r.txt".into(),
                offset: 8,
                len: Some(50),
            },
        ];
        let out = k._read_batch(&reqs, &c).expect("ok");
        assert_eq!(out[0].as_ref().unwrap().data.as_deref().unwrap(), b"0123456789");
        assert_eq!(out[1].as_ref().unwrap().data.as_deref().unwrap(), b"3456");
        assert_eq!(out[2].as_ref().unwrap().data.as_deref().unwrap(), b"");
        assert_eq!(out[3].as_ref().unwrap().data.as_deref().unwrap(), b"89");
    }
```

- [ ] **Step 2: Run test**

Run: `cargo test -p kernel read_batch_range_slicing`
Expected: FAIL on the first three assertions because:
- The group path goes through `clone_read_result` (which already slices) — case 0 may pass.
- The singleton path calls `sys_read(path, ctx, 5000, req.offset)` and `sys_read` does NOT slice for DT_REG (offset is only used for DT_STREAM/DT_PIPE). So slicing is bypassed.

Note all 4 cases use `/r.txt` with the same content_id → all coalesce into one group. So they all go through `clone_read_result`, which already slices. The test should actually PASS at this point.

If it does pass: keep the test as a regression guard and skip to Step 5.

If it fails (e.g. on `out[2]` returning whole bytes): the slicing logic in `clone_read_result` has a bug. Fix the boundary math.

- [ ] **Step 3: Add slicing to the singleton path too** — singleton requests do not go through `clone_read_result`. Wrap their result:

In the `Unit::Singleton` arm in `_read_batch` (Task 5), replace:

```rust
                        Unit::Singleton { idx } => {
                            let req = &reqs[*idx];
                            let r = self.sys_read(&req.path, ctx, 5000, req.offset);
                            local.push((*idx, r));
                        }
```

with:

```rust
                        Unit::Singleton { idx } => {
                            let req = &reqs[*idx];
                            let r = self.sys_read(&req.path, ctx, 5000, 0);
                            local.push((*idx, slice_read_result(r, req)));
                        }
```

Add a helper:

```rust
fn slice_read_result(
    r: Result<SysReadResult, KernelError>,
    req: &crate::kernel::BatchReadRequest,
) -> Result<SysReadResult, KernelError> {
    let mut r = r?;
    if let Some(bytes) = r.data.as_ref() {
        let off = (req.offset as usize).min(bytes.len());
        let end = match req.len {
            Some(l) => off.saturating_add(l as usize).min(bytes.len()),
            None => bytes.len(),
        };
        r.data = Some(bytes[off..end].to_vec());
    }
    Ok(r)
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p kernel read_batch_range_slicing`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/kernel/io.rs
git commit -m "feat(#4058): per-request offset/len slicing in _read_batch"
```

---

## Task 7: Reject DT_PIPE / DT_STREAM entries with per-item error

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs` (phase A: check `entry.entry_type` before resolving)
- Test: inline

- [ ] **Step 1: Write the failing test** — inject a `DT_PIPE`-tagged metadata entry directly via the metastore, then attempt batch-read.

```rust
    #[test]
    fn read_batch_rejects_pipe_entry() {
        use crate::meta_store::{DT_PIPE, FileMetadata};
        let k = Kernel::new();
        let c = ctx();
        // Inject a DT_PIPE metadata row directly into the boot metastore.
        // This bypasses the full pipe-creation path (which involves the
        // pipe_manager + native syscall) and exercises only the guard we
        // are testing: entry_type-based rejection inside _read_batch.
        let mut meta = FileMetadata::default();
        meta.entry_type = DT_PIPE;
        {
            let ms = k.metastore.read();
            ms.as_ref()
                .expect("boot metastore")
                .put("/fake_pipe", meta)
                .expect("put");
        }
        let reqs = vec![crate::kernel::BatchReadRequest {
            path: "/fake_pipe".into(),
            offset: 0,
            len: None,
        }];
        let out = k._read_batch(&reqs, &c).expect("outer ok");
        match &out[0] {
            Err(KernelError::IOError(m)) => {
                assert!(m.contains("pipe") || m.contains("stream"), "got: {m}");
            }
            other => panic!("expected IOError, got {other:?}"),
        }
    }
```

Note: `FileMetadata::default()` may not exist — grep `pub struct FileMetadata` in `rust/kernel/src/meta_store/mod.rs` to see the actual constructor (likely `FileMetadata::new(...)` or a struct-literal). Substitute accordingly. The key is `entry_type = DT_PIPE`.

- [ ] **Step 2: Run test**

Run: `cargo test -p kernel read_batch_rejects_pipe_entry`
Expected: FAIL — current code falls through to `sys_read` which would do pipe semantics (return None data or block).

- [ ] **Step 3: Implement guard** — in `_read_batch` Phase A, after successful metadata fetch:

```rust
            // Reject pipe/stream — they have blocking semantics that don't
            // belong in batch reads.
            if entry.entry_type == crate::meta_store::DT_PIPE
                || entry.entry_type == crate::meta_store::DT_STREAM
            {
                results[i] = Some(Err(KernelError::IOError(format!(
                    "batch read does not support pipes/streams: {}",
                    req.path
                ))));
                continue;
            }
```

(Insert immediately after the `entry` is bound, before `resolved[i] = Some(ResolvedRead { ... })`.)

- [ ] **Step 4: Run tests**

Run: `cargo test -p kernel read_batch`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/kernel/io.rs
git commit -m "feat(#4058): reject DT_PIPE/DT_STREAM in batch read with per-item error"
```

---

## Task 8: PRE-read hooks fire per request (verification only — no code change)

**Files:** none (verification step)

The batch path delegates to `sys_read` inside the per-request fan-out. `sys_read` already calls `dispatch_native_pre(&HookContext::Read(ReadHookCtx { ... }))` at `rust/kernel/src/kernel/io.rs:177` for every invocation. Therefore PRE-read hooks naturally fire once per request without any extra code in `_read_batch`.

- [ ] **Step 1: Confirm in-code** — open `rust/kernel/src/kernel/io.rs:177` and verify `dispatch_native_pre` runs inside `sys_read_with_link_depth`.

- [ ] **Step 2: Verify no double-dispatch** — `_read_batch` (from Task 5) calls `self.sys_read(...)` inside each `Unit::Group` / `Unit::Singleton` arm. No explicit `dispatch_native_pre` call lives in `_read_batch`. Audit the Task-5 fan-out body — make sure no hook code was accidentally added.

- [ ] **Step 3: No commit needed** — this task is purely a verification gate. Move on to Task 9.

If a follow-up wants instrumented hook-count testing, file a separate issue: the registration API for native hooks (`Kernel::native_hooks.write().push(...)`) needs a small helper that exposes a closure-based registration. Out of scope for this PR.

---

## Task 9: PyO3 ABI accepts both shapes; returns per-item Ok/Err

**Files:**
- Modify: `rust/kernel/src/generated_kernel_abi_pyo3.rs:2707`

- [ ] **Step 1: Read the existing PyO3 method body** at line 2707 — already touched in Task 2.

- [ ] **Step 2: Add a Python error class for batch errors** — extend the `PySysReadResult` shape or add a sibling `PyBatchReadItem` class.

Grep `pub struct PySysReadResult` in `rust/kernel/src/generated_kernel_abi_pyo3.rs` to find existing fields. Add to that same file:

```rust
#[pyclass]
#[derive(Clone)]
pub struct PyBatchReadItem {
    #[pyo3(get)]
    pub data: Option<Py<PyBytes>>,
    #[pyo3(get)]
    pub content_id: Option<String>,
    #[pyo3(get)]
    pub gen: u64,
    #[pyo3(get)]
    pub entry_type: u8,
    #[pyo3(get)]
    pub post_hook_needed: bool,
    /// "" on success; one of "not_found" / "permission_denied" /
    /// "invalid_path" / "io_error" on failure.
    #[pyo3(get)]
    pub error_kind: String,
    /// Empty string on success.
    #[pyo3(get)]
    pub error_message: String,
}
```

Add a constant `pub const PY_BATCH_READ_OK: &str = "";` for clarity, or skip and use `is_error_kind_empty` helpers in Python.

- [ ] **Step 3: Replace the PyO3 `_read_batch` method**

```rust
    /// Batch read. Accepts either `list[str]` (legacy) or
    /// `list[tuple[str, int, int | None]]`. Returns `list[PyBatchReadItem]`.
    #[pyo3(signature = (reqs, ctx))]
    fn _read_batch<'py>(
        &self,
        py: Python<'py>,
        reqs: Bound<'py, PyAny>,
        ctx: &PyOperationContext,
    ) -> PyResult<Vec<PyBatchReadItem>> {
        // Parse either shape.
        let rust_reqs: Vec<crate::kernel::BatchReadRequest> = if let Ok(paths) =
            reqs.extract::<Vec<String>>()
        {
            paths
                .into_iter()
                .map(|p| crate::kernel::BatchReadRequest {
                    path: p,
                    offset: 0,
                    len: None,
                })
                .collect()
        } else {
            let tuples: Vec<(String, u64, Option<u64>)> = reqs.extract()?;
            tuples
                .into_iter()
                .map(|(p, off, len)| crate::kernel::BatchReadRequest {
                    path: p,
                    offset: off,
                    len,
                })
                .collect()
        };

        let rust_ctx = ctx.to_rust();
        let result = py.detach(|| self.inner._read_batch(&rust_reqs, &rust_ctx));
        let results = result.map_err(|e| -> PyErr { e.into() })?;
        Ok(results
            .into_iter()
            .map(|r| match r {
                Ok(r) => PyBatchReadItem {
                    data: r.data.map(|d| PyBytes::new(py, &d).into()),
                    content_id: r.content_id,
                    gen: r.gen,
                    entry_type: r.entry_type,
                    post_hook_needed: r.post_hook_needed,
                    error_kind: String::new(),
                    error_message: String::new(),
                },
                Err(e) => {
                    let (kind, msg) = batch_err_kind_msg(&e);
                    PyBatchReadItem {
                        data: None,
                        content_id: None,
                        gen: 0,
                        entry_type: 0,
                        post_hook_needed: false,
                        error_kind: kind,
                        error_message: msg,
                    }
                }
            })
            .collect())
    }
```

And add a helper at file scope in the same file:

```rust
fn batch_err_kind_msg(e: &KernelError) -> (String, String) {
    match e {
        KernelError::FileNotFound(p) => ("not_found".into(), p.clone()),
        KernelError::PermissionDenied(m) => ("permission_denied".into(), m.clone()),
        KernelError::InvalidPath(m) => ("invalid_path".into(), m.clone()),
        other => ("io_error".into(), format!("{:?}", other)),
    }
}
```

Register `PyBatchReadItem` in the PyO3 module init. Grep `add_class::<PySysReadResult>` in the same file to find the existing add-class spot; add `m.add_class::<PyBatchReadItem>()?;` next to it.

- [ ] **Step 4: Build the cdylib**

Run: `cargo build -p nexus-cdylib`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/generated_kernel_abi_pyo3.rs
git commit -m "feat(#4058): PyO3 _read_batch accepts (path,offset,len); per-item Ok/Err"
```

---

## Task 10: Update Python `read_bulk` wrapper

**Files:**
- Modify: `src/nexus/core/nexus_fs_content.py:1708`
- Test: `tests/unit/core/test_read_bulk_batch_shape.py` (create or extend nearest existing pytest)

- [ ] **Step 1: Find an existing pytest module** for `nexus_fs_content`.

Run: `find tests -name "*read_bulk*" -o -name "*nexus_fs_content*" | head -5`

If one exists, add a new test function inside it. If not, create `tests/unit/core/test_read_bulk_batch_shape.py`.

- [ ] **Step 2: Write the failing test**

```python
import pytest
from nexus.core.nexus_fs_content import NexusFSContent  # adjust to actual import

def test_read_bulk_back_compat_returns_existing_shape(tmp_nexus_fs):
    fs = tmp_nexus_fs
    fs.write("/a.txt", b"alpha")
    fs.write("/b.txt", b"beta")
    out = fs.read_bulk(["/a.txt", "/b.txt"])
    assert len(out) == 2
    assert out[0]["content"] == b"alpha"
    assert out[1]["content"] == b"beta"
    assert "content_id" in out[0]

def test_read_bulk_partial_mode_reports_per_item_errors(tmp_nexus_fs):
    fs = tmp_nexus_fs
    fs.write("/exists.txt", b"hi")
    out = fs.read_bulk(["/exists.txt", "/missing.txt"], partial=True)
    assert out[0]["content"] == b"hi"
    assert out[1]["error"] == "not_found"
```

`tmp_nexus_fs` fixture: grep existing fixtures (`grep -rn "def tmp_nexus_fs\|@pytest.fixture" tests/conftest.py tests/unit/conftest.py 2>/dev/null`). Use whatever fixture already wires up an in-memory or tempdir NexusFS for tests.

- [ ] **Step 3: Run test, confirm it fails or skips**

Run: `pytest tests/unit/core/test_read_bulk_batch_shape.py -v`
Expected: FAIL — the Python wrapper still calls `_read_batch(paths, ctx)` (paths only) and treats results as `SysReadResult`.

- [ ] **Step 4: Update Python wrapper at `src/nexus/core/nexus_fs_content.py:1708`**

Replace:
```python
rust_results = self._kernel._read_batch(allowed_paths, _rust_ctx) if allowed_paths else []
```

with:
```python
rust_results = (
    self._kernel._read_batch(
        [(p, 0, None) for p in allowed_paths], _rust_ctx
    )
    if allowed_paths
    else []
)
```

Then update the per-item handling loop (around line 1727+). Replace the `if r.data is None:` block with explicit Ok/Err discrimination:

```python
for path in validated_paths:
    if path in denied_paths:
        results.append({"path": path, "error": "permission_denied"})
        continue

    r = next(allowed_iter)
    meta = batch_meta.get(path)

    if r.error_kind:
        # Per-item failure from kernel. Translate kinds → existing wrapper
        # error vocabulary. "not_found" passes through; others map to
        # "read_error" plus message in partial mode, or raise in strict.
        if not partial:
            from nexus.contracts.exceptions import (
                NexusFileNotFoundError,
                NexusPermissionError,
            )
            if r.error_kind == "not_found":
                raise NexusFileNotFoundError(path)
            if r.error_kind == "permission_denied":
                raise NexusPermissionError(r.error_message or path)
            raise IOError(f"read_bulk({path}): {r.error_message}")
        results.append({"path": path, "error": r.error_kind})
        continue

    # Success path — fall through to the existing post-hook + dict-build
    # logic, but now using r.data (bytes) directly.
    # … (keep the rest of the existing success-path code) …
```

Carefully preserve the post-hook logic (around lines 1755+) and the `_loaded_bytes` accounting — they were added in Issue #4080 review rounds.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/core/test_read_bulk_batch_shape.py -v`
Expected: PASS.

Run: `pytest tests/ -k "read_bulk" -x`
Expected: PASS — full read_bulk test suite stays green.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/core/nexus_fs_content.py tests/unit/core/test_read_bulk_batch_shape.py
git commit -m "feat(#4058): switch read_bulk to new _read_batch shape"
```

---

## Task 11: Add `BatchRead` proto RPC

**Files:**
- Modify: `proto/nexus/grpc/vfs/vfs.proto`

- [ ] **Step 1: Add the RPC and messages**

Append to the `service NexusVFSService { ... }` block (after `rpc Ping`):

```proto
  // Vectored batch read (Issue #4058). Per-request errors are reported
  // per-item; outer Status is reserved for transport-level failures.
  rpc BatchRead(BatchReadRequest) returns (BatchReadResponse);
```

After the existing `PingResponse` message:

```proto
// --- Batch read (Issue #4058) ---

message BatchReadRequest {
  string auth_token = 1;
  repeated BatchReadItemRequest items = 2;
}

message BatchReadItemRequest {
  string path = 1;
  uint64 offset = 2;
  uint64 len = 3;       // 0 = entire file from offset (sentinel)
}

message BatchReadResponse {
  repeated BatchReadItemResponse results = 1;  // input order
}

message BatchReadItemResponse {
  bytes content = 1;        // empty when is_error
  bool is_error = 2;
  bytes error_payload = 3;  // JSON RPC error dict (same as CallResponse)
  string content_id = 4;
  uint64 gen = 5;
}
```

- [ ] **Step 2: Confirm proto compiles**

Run: `cargo build -p kernel`
Expected: PASS — `rust/kernel/build.rs` compiles `vfs.proto` and exposes `kernel::kernel::vfs_proto::{BatchReadRequest, BatchReadResponse, ...}` types via tonic-build.

- [ ] **Step 3: Stub server handler so build stays green**

`tonic-build` generates a trait method `async fn batch_read(...)` on `NexusVfsService`. The existing `impl NexusVfsService for VfsServiceImpl` in `rust/transport/src/grpc.rs:187` is now missing this method — add a stub:

```rust
    async fn batch_read(
        &self,
        _req: Request<kernel::kernel::vfs_proto::BatchReadRequest>,
    ) -> Result<Response<kernel::kernel::vfs_proto::BatchReadResponse>, Status> {
        Err(Status::unimplemented("batch_read not yet wired"))
    }
```

(Task 12 replaces this stub with the real handler.)

- [ ] **Step 4: Build**

Run: `cargo build -p kernel -p transport`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add proto/nexus/grpc/vfs/vfs.proto rust/transport/src/grpc.rs
git commit -m "feat(#4058): add BatchRead RPC to vfs.proto (stub handler)"
```

---

## Task 12: gRPC `BatchRead` server handler + integration test

**Files:**
- Modify: `rust/transport/src/grpc.rs` (replace the stub from Task 11)
- Test: `rust/transport/tests/grpc_batch_read.rs` (create — or extend nearest existing test if one matches)

- [ ] **Step 1: Find an existing in-process gRPC test pattern**

Run: `find rust/transport -name "*.rs" -path "*test*"` and `grep -rn "VfsServiceImpl\|NexusVfsServiceServer\|tonic::transport::Server\|tokio::test" rust/transport/tests rust/transport/src/ | head -20`

Two cases:
- **Case A — an existing test file already sets up an in-process server.** Add the new test as another `#[tokio::test]` in that file, reusing whatever fixture function it uses.
- **Case B — no in-process test exists.** Drive the kernel directly without the network round-trip by calling `VfsServiceImpl`'s methods through the `NexusVfsService` trait. This still exercises the BatchRead handler end-to-end without needing a real socket.

- [ ] **Step 2: Write the test (Case B — trait-direct, no socket)**

`rust/transport/tests/grpc_batch_read.rs`:

```rust
// Trait-direct exercise of the BatchRead handler (no socket).

use std::sync::Arc;
use kernel::kernel::{Kernel};
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::NexusVfsService,
    BatchReadItemRequest, BatchReadRequest,
};
use tonic::Request;
use transport::grpc::VfsServiceImpl; // adjust path: grep `pub struct VfsServiceImpl` in transport/src/grpc.rs

#[tokio::test]
async fn batch_read_returns_per_item_results_in_order() {
    let kernel = Arc::new(Kernel::new());
    // Seed two files via the kernel directly.
    let ctx = contracts::OperationContext::new("test", "root", true, None, true);
    kernel.sys_write("/x.txt", &ctx, b"hello", 0).expect("write");

    // Build the gRPC service handle. Look at how `VfsServiceImpl` is
    // constructed elsewhere in the crate (grep `VfsServiceImpl::new` or
    // `VfsServiceImpl {`) to mirror its dependencies — likely
    // (kernel: Arc<Kernel>, auth: Arc<dyn AuthResolver>, …). For test
    // purposes use whatever no-auth/test-auth helper the crate already
    // exposes; if none, pass a hand-rolled one that returns `ctx`.
    let svc = VfsServiceImpl::for_test(kernel.clone());

    let req = Request::new(BatchReadRequest {
        auth_token: "test-token".into(),
        items: vec![
            BatchReadItemRequest { path: "/x.txt".into(), offset: 0, len: 0 },
            BatchReadItemRequest { path: "/missing.txt".into(), offset: 0, len: 0 },
            BatchReadItemRequest { path: "/x.txt".into(), offset: 1, len: 3 },
        ],
    });

    let resp = svc.batch_read(req).await.expect("rpc ok").into_inner();
    assert_eq!(resp.results.len(), 3);
    assert!(!resp.results[0].is_error);
    assert_eq!(resp.results[0].content, b"hello");
    assert!(resp.results[1].is_error);
    assert!(!resp.results[2].is_error);
    assert_eq!(resp.results[2].content, b"ell");
}
```

If `VfsServiceImpl::for_test` doesn't exist, grep `VfsServiceImpl::new` and call it with the real constructor arguments — for tests you can pass a no-op auth resolver. As a last resort, factor a small `for_test` constructor while adding this test; gate it behind `#[cfg(test)]` so production code paths aren't affected.

- [ ] **Step 3: Run test, confirm FAIL** (the handler still returns `Status::unimplemented`).

Run: `cargo test -p transport batch_read_round_trip_ordering_and_errors`
Expected: FAIL with `unimplemented` status.

- [ ] **Step 4: Implement the real handler** in `rust/transport/src/grpc.rs` — replace the stub from Task 11:

```rust
    async fn batch_read(
        &self,
        req: Request<kernel::kernel::vfs_proto::BatchReadRequest>,
    ) -> Result<
        Response<kernel::kernel::vfs_proto::BatchReadResponse>,
        Status,
    > {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Err(s),
        };
        // Federation tokens use Call dispatch (same rule as typed Read).
        if !ctx.zone_perms.is_empty() {
            return Err(Status::permission_denied(
                "federation token: use Call dispatch (read_bulk RPC) — typed BatchRead bypasses zone authorization",
            ));
        }

        let rust_reqs: Vec<kernel::kernel::BatchReadRequest> = req
            .items
            .into_iter()
            .map(|it| kernel::kernel::BatchReadRequest {
                path: it.path,
                offset: it.offset,
                len: if it.len == 0 { None } else { Some(it.len) },
            })
            .collect();

        let outcome = self.kernel._read_batch(&rust_reqs, &ctx);
        let results = match outcome {
            Ok(v) => v,
            Err(e) => return Err(Status::internal(format!("{:?}", e))),
        };

        // Aggregate-size cap — same 100 MB as the Python wrapper enforces.
        const MAX_AGG: usize = 100 * 1024 * 1024;
        let mut total = 0usize;
        for r in results.iter().filter_map(|r| r.as_ref().ok()) {
            total = total.saturating_add(r.data.as_ref().map(|b| b.len()).unwrap_or(0));
            if total > MAX_AGG {
                return Err(Status::resource_exhausted(format!(
                    "batch read aggregate {} bytes exceeds {} MB",
                    total,
                    MAX_AGG / (1024 * 1024)
                )));
            }
        }

        let mapped: Vec<_> = results
            .into_iter()
            .map(|r| match r {
                Ok(r) => kernel::kernel::vfs_proto::BatchReadItemResponse {
                    content: r.data.unwrap_or_default(),
                    is_error: false,
                    error_payload: Vec::new(),
                    content_id: r.content_id.unwrap_or_default(),
                    gen: r.gen,
                },
                Err(e) => {
                    let (code, msg) = self.map_kernel_err(e);
                    kernel::kernel::vfs_proto::BatchReadItemResponse {
                        content: Vec::new(),
                        is_error: true,
                        error_payload: encode_rpc_error(code, &msg),
                        content_id: String::new(),
                        gen: 0,
                    }
                }
            })
            .collect();

        Ok(Response::new(
            kernel::kernel::vfs_proto::BatchReadResponse { results: mapped },
        ))
    }
```

- [ ] **Step 5: Run tests**

Run: `cargo test -p transport batch_read_round_trip_ordering_and_errors`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rust/transport/src/grpc.rs rust/transport/tests/grpc_batch_read.rs
git commit -m "feat(#4058): wire BatchRead gRPC handler + integration test"
```

---

## Task 13: Criterion benchmark — 100-file batch vs sequential

**Files:**
- Create: `rust/kernel/benches/read_batch.rs`
- Modify: `rust/kernel/Cargo.toml` (add `[[bench]]` entry)

- [ ] **Step 1: Add Cargo entry**

Append to `rust/kernel/Cargo.toml`:

```toml
[[bench]]
name = "read_batch"
harness = false
```

- [ ] **Step 2: Write the bench file**

Create `rust/kernel/benches/read_batch.rs`:

```rust
//! Criterion bench for vectored _read_batch (Issue #4058).
//!
//! Run: cd rust/kernel && cargo bench read_batch

use criterion::{criterion_group, criterion_main, Criterion};
use kernel::kernel::{BatchReadRequest, Kernel};
use std::hint::black_box;

fn setup() -> Kernel {
    let k = Kernel::new();
    let ctx = contracts::OperationContext::new("bench", "root", true, None, true);
    for i in 0..100u32 {
        let path = format!("/bench/f{i:03}.txt");
        let payload = vec![b'x'; 1024]; // 1 KB each
        // sys_mkdir for /bench first.
        let _ = k.sys_mkdir("/bench", &ctx);
        k.sys_write(&path, &ctx, &payload, 0).expect("write");
    }
    k
}

fn bench_sequential(c: &mut Criterion) {
    let k = setup();
    let ctx = contracts::OperationContext::new("bench", "root", true, None, true);
    c.bench_function("read_batch/sequential_100", |b| {
        b.iter(|| {
            for i in 0..100u32 {
                let path = format!("/bench/f{i:03}.txt");
                let r = k.sys_read(&path, &ctx, 5000, 0).expect("read");
                black_box(r);
            }
        });
    });
}

fn bench_batched(c: &mut Criterion) {
    let k = setup();
    let ctx = contracts::OperationContext::new("bench", "root", true, None, true);
    let reqs: Vec<BatchReadRequest> = (0..100u32)
        .map(|i| BatchReadRequest {
            path: format!("/bench/f{i:03}.txt"),
            offset: 0,
            len: None,
        })
        .collect();
    c.bench_function("read_batch/batched_100", |b| {
        b.iter(|| {
            let out = k._read_batch(&reqs, &ctx).expect("batch");
            black_box(out);
        });
    });
}

criterion_group!(benches, bench_sequential, bench_batched);
criterion_main!(benches);
```

- [ ] **Step 3: Run the bench**

Run: `cd rust/kernel && cargo bench read_batch`
Expected: Two benchmarks reported. Capture the mean times — `batched_100` should be at least 3× faster than `sequential_100`.

- [ ] **Step 4: If speedup is < 3×, profile and tune**

Common culprits:
- `clone_read_result` allocates a new `Vec<u8>` per consumer — for whole-file reads, point at the same Arc<Bytes> if `SysReadResult::data` is `Option<Vec<u8>>`. Switching to `Bytes` is out of scope; if the issue is real, file a follow-up.
- `read_batch_max_concurrency` too low or too high. Try 8, 16, 32.
- Per-request `validate_path_fast` allocating regex state. Should be cheap — verify.

Iterate until ≥ 3×.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/Cargo.toml rust/kernel/benches/read_batch.rs
git commit -m "feat(#4058): Criterion bench — _read_batch 100-file speedup"
```

---

## Task 14: Speedup-assert test (gated `NEXUS_BENCH=1`)

**Files:**
- Create: `rust/kernel/tests/read_batch_speedup.rs`

- [ ] **Step 1: Write the test**

```rust
//! Acceptance criterion (Issue #4058): batched read ≥ 3× sequential.
//!
//! Skipped unless NEXUS_BENCH=1 is set (timing flaky on shared CI).

use kernel::kernel::{BatchReadRequest, Kernel};
use std::time::Instant;

#[test]
fn read_batch_meets_3x_speedup_target() {
    if std::env::var("NEXUS_BENCH").ok().as_deref() != Some("1") {
        eprintln!("skipping: set NEXUS_BENCH=1 to run");
        return;
    }
    let k = Kernel::new();
    let ctx = contracts::OperationContext::new("bench", "root", true, None, true);
    let _ = k.sys_mkdir("/bench", &ctx);
    for i in 0..100u32 {
        let path = format!("/bench/f{i:03}.txt");
        let payload = vec![b'x'; 1024];
        k.sys_write(&path, &ctx, &payload, 0).expect("write");
    }

    // Warmup
    for i in 0..100u32 {
        let _ = k.sys_read(&format!("/bench/f{i:03}.txt"), &ctx, 5000, 0);
    }

    // Sequential
    let seq_iters = 20;
    let t0 = Instant::now();
    for _ in 0..seq_iters {
        for i in 0..100u32 {
            let _ = k
                .sys_read(&format!("/bench/f{i:03}.txt"), &ctx, 5000, 0)
                .expect("read");
        }
    }
    let seq_mean = t0.elapsed().as_secs_f64() / seq_iters as f64;

    // Batched
    let reqs: Vec<BatchReadRequest> = (0..100u32)
        .map(|i| BatchReadRequest {
            path: format!("/bench/f{i:03}.txt"),
            offset: 0,
            len: None,
        })
        .collect();
    let batch_iters = 20;
    let t1 = Instant::now();
    for _ in 0..batch_iters {
        let _ = k._read_batch(&reqs, &ctx).expect("batch");
    }
    let batch_mean = t1.elapsed().as_secs_f64() / batch_iters as f64;

    let ratio = seq_mean / batch_mean;
    eprintln!("seq_mean={seq_mean:.6}s batch_mean={batch_mean:.6}s ratio={ratio:.2}x");
    assert!(
        ratio >= 3.0,
        "expected batched read >= 3x faster, got {ratio:.2}x"
    );
}
```

- [ ] **Step 2: Run it without the env var**

Run: `cargo test -p kernel read_batch_meets_3x_speedup_target`
Expected: PASS (test no-ops without `NEXUS_BENCH=1`).

- [ ] **Step 3: Run it with the env var**

Run: `NEXUS_BENCH=1 cargo test -p kernel --release read_batch_meets_3x_speedup_target -- --nocapture`
Expected: PASS with printed ratio ≥ 3.0×.

- [ ] **Step 4: Commit**

```bash
git add rust/kernel/tests/read_batch_speedup.rs
git commit -m "test(#4058): 3x speedup assertion gated by NEXUS_BENCH=1"
```

---

## Task 15: Federation fallback (`try_remote_fetch`) per-group

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs`

- [ ] **Step 1: Verify current behavior**

The fan-out code delegates to `sys_read`, which already calls `try_remote_fetch` on local backend miss (see `rust/kernel/src/kernel/io.rs:438`). The batch path inherits this for free.

Confirm by reading the call chain: `_read_batch` → fan-out → `sys_read` → backend miss → `try_remote_fetch`. No code change needed.

- [ ] **Step 2: Add a regression test** that exercises a federation miss

This requires a multi-node test fixture. If one exists (grep `federation` in `rust/kernel/tests/`), add a test there. If the fixture is non-trivial, file a follow-up issue and add a TODO comment in `_read_batch` referencing it. Do NOT add a half-finished test.

- [ ] **Step 3: Commit (if no test was added, skip this task)**

```bash
git commit --allow-empty -m "chore(#4058): note _read_batch inherits federation fallback via sys_read"
```

(Or skip the empty commit and just include this note in the final PR body.)

---

## Task 16: Final sweep — clippy, fmt, full test suite

**Files:** all

- [ ] **Step 1: Format**

Run: `cargo fmt --all`
Expected: no diff.

- [ ] **Step 2: Clippy**

Run: `cargo clippy -p kernel -p transport --all-targets -- -D warnings`
Expected: PASS.

- [ ] **Step 3: Full Rust tests**

Run: `cargo test -p kernel -p transport`
Expected: PASS.

- [ ] **Step 4: Full Python tests (touching files only)**

Run: `pytest tests/unit/core/test_read_bulk_batch_shape.py -v` (and any other read_bulk-related test files)
Expected: PASS.

- [ ] **Step 5: Commit any fmt/clippy fixes**

```bash
git add -p
git commit -m "chore(#4058): fmt + clippy cleanup"
```

- [ ] **Step 6: Final review checklist**

- [ ] All acceptance criteria from `docs/superpowers/specs/2026-05-11-issue-4058-read-batch-design.md` §10 mapped to code/tests.
- [ ] No `todo!()` / `unimplemented!()` / `dbg!()` left in the diff.
- [ ] No new `unwrap()` calls on user input paths.
- [ ] Python `read_bulk` preserves its existing dict shape on success.
- [ ] gRPC `BatchRead` server enforces 100 MB aggregate cap.
- [ ] Speedup assertion passes locally with `NEXUS_BENCH=1`.

---

## Acceptance-criteria checklist (from Issue #4058)

- [ ] `_read_batch()` implemented end-to-end — Tasks 2–8
- [ ] Adjacent range coalescing — Task 4
- [ ] Bounded parallelism (configurable) — Tasks 1, 5
- [ ] gRPC `BatchRead` exposed — Tasks 11, 12
- [ ] Benchmark: 100-file batch ≥ 3× sequential — Tasks 13, 14
- [ ] Error semantics: per-request Result — Tasks 3, 9
