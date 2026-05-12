# Issue #4059 Write Coalescing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a kernel-owned write coalescing buffer with workspace policies, read-your-own-writes, forced flush APIs, snapshot safety, durability docs, and benchmark evidence for issue #4059.

**Architecture:** Keep write-back state inside Rust `Kernel`, between DT_REG syscall handling and the existing backend write plus metastore commit path. Extract the current write-through commit logic into a reusable helper, then route non-strict writes through a dirty-buffer map that can be read, flushed, retried, and drained from Python close/sync/snapshot boundaries.

**Tech Stack:** Rust 2021 workspace crates (`contracts`, `kernel`), PyO3 generated ABI via `scripts/codegen_kernel_abi.py`, Python `NexusFS` mixins, `WorkspaceManager`, existing redb `MetaStore`, Criterion benchmarks, pytest for Python integration tests.

---

## File Structure

| Path | Change | Responsibility |
| --- | --- | --- |
| `rust/contracts/src/write_coalescing.rs` | create | Shared policy enum and defaults for strict/latency/batch write coalescing. |
| `rust/contracts/src/lib.rs` | modify | Export write coalescing policy types. |
| `rust/kernel/src/kernel/write_buffer.rs` | create | Dirty-entry model, policy store, merge/splice/read/select behavior, pure unit tests. |
| `rust/kernel/src/kernel/mod.rs` | modify | Add `write_buffer` field, initialize it, expose policy and flush methods, start/stop worker state. |
| `rust/kernel/src/kernel/io.rs` | modify | Extract write-through commit helper; integrate buffered DT_REG writes, dirty reads, unlink/rename flush guards. |
| `rust/kernel/src/abi.rs` | modify | Add force-flush methods to `KernelAbi` for Rust services that need snapshot/sync boundaries. |
| `rust/kernel/src/generated_kernel_abi_pyo3.rs` | generated | PyO3 wrappers for new kernel methods. Do not edit by hand; regenerate. |
| `stubs/nexus_runtime/__init__.pyi` | generated | Python stubs for new kernel methods. |
| `src/nexus/core/kernel_exports.py` | generated | Static export table for new kernel methods. |
| `src/nexus/_kernel_api_groups.py` | generated | Runtime API validation list for new kernel methods. |
| `src/nexus/server/_kernel_syscall_dispatch.py` | generated | RPC syscall dispatch for `flush_write_buffer`, `fsync`, and `sync`; generator must emit these names. |
| `src/nexus/core/nexus_fs_content.py` | modify | Public `NexusFS.flush_write_buffer`, `fsync`, and `sync` methods. |
| `src/nexus/services/workspace/workspace_manager.py` | modify | Flush workspace prefix before snapshot manifest creation. |
| `src/nexus/factory/service_routing.py` | modify | Export sync/flush methods if routed through services or RPC export lists. |
| `tests/unit/core/test_write_coalescing_api.py` | create | Python-facing force flush and close behavior tests. |
| `tests/unit/services/workspace/test_workspace_snapshot_flush.py` | create | Snapshot prefix flush integration tests. |
| `rust/kernel/benches/write_coalescing.rs` | create | Criterion burst-write backend write-count benchmark. |
| `rust/kernel/Cargo.toml` | modify | Register the write coalescing benchmark. |
| `docs/architecture/KERNEL-ARCHITECTURE.md` | modify | Document write-back semantics, policies, and durability tradeoff. |
| `docs/benchmarks/2026-05-11-write-coalescing.md` | create | Record benchmark command and measured write-count reduction. |

## Shared Test Scaffolding

Use this Rust test helper inside `rust/kernel/src/kernel/write_buffer.rs` tests and kernel syscall tests when a backend write counter is needed:

```rust
#[derive(Default)]
struct CountingObjectStore {
    writes: std::sync::atomic::AtomicUsize,
    blobs: parking_lot::Mutex<std::collections::HashMap<String, Vec<u8>>>,
    fail_writes: std::sync::atomic::AtomicBool,
}

impl CountingObjectStore {
    fn write_count(&self) -> usize {
        self.writes.load(std::sync::atomic::Ordering::Relaxed)
    }

    fn set_fail_writes(&self, fail: bool) {
        self.fail_writes
            .store(fail, std::sync::atomic::Ordering::Relaxed);
    }
}

impl crate::abc::object_store::ObjectStore for CountingObjectStore {
    fn name(&self) -> &str {
        "counting"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &crate::kernel::OperationContext,
        offset: u64,
    ) -> Result<crate::abc::object_store::WriteResult, crate::abc::object_store::StorageError> {
        if self
            .fail_writes
            .load(std::sync::atomic::Ordering::Relaxed)
        {
            return Err(crate::abc::object_store::StorageError::NotSupported(
                "intentional write failure",
            ));
        }
        if offset != 0 {
            return Err(crate::abc::object_store::StorageError::NotSupported(
                "test backend only accepts full writes",
            ));
        }
        self.writes
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let key = if content_id.is_empty() {
            lib::hash::hash_content(content)
        } else {
            content_id.to_string()
        };
        self.blobs.lock().insert(key.clone(), content.to_vec());
        Ok(crate::abc::object_store::WriteResult {
            content_id: key,
            version: lib::hash::hash_content(content),
            size: content.len() as u64,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, crate::abc::object_store::StorageError> {
        self.blobs
            .lock()
            .get(content_id)
            .cloned()
            .ok_or_else(|| crate::abc::object_store::StorageError::NotFound(content_id.to_string()))
    }
}
```

Use this mount helper in kernel syscall tests:

```rust
fn mounted_counting_kernel() -> (
    Kernel,
    std::sync::Arc<CountingObjectStore>,
    crate::kernel::OperationContext,
) {
    let kernel = Kernel::new();
    let backend = std::sync::Arc::new(CountingObjectStore::default());
    let backend_dyn: std::sync::Arc<dyn crate::abc::object_store::ObjectStore> = backend.clone();
    kernel
        .add_mount("/workspace", "root", Some(backend_dyn), None, None, false)
        .expect("mount counting backend");
    let ctx = crate::kernel::OperationContext::new("test", "root", true, None, true);
    (kernel, backend, ctx)
}
```

### Task 1: Shared Policy Types

**Files:**
- Create: `rust/contracts/src/write_coalescing.rs`
- Modify: `rust/contracts/src/lib.rs`

- [ ] **Step 1: Write failing policy tests**

Create `rust/contracts/src/write_coalescing.rs` with only the tests first:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latency_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::latency();
        assert_eq!(policy.mode, WriteCoalescingMode::Latency);
        assert_eq!(policy.flush_window_ms, 1_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn batch_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::batch();
        assert_eq!(policy.mode, WriteCoalescingMode::Batch);
        assert_eq!(policy.flush_window_ms, 60_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn strict_policy_disables_buffering() {
        let policy = WriteCoalescingPolicy::strict();
        assert_eq!(policy.mode, WriteCoalescingMode::Strict);
        assert_eq!(policy.flush_window_ms, 0);
        assert_eq!(policy.byte_budget, 0);
        assert!(policy.flush_on_close);
        assert!(!policy.enabled());
    }
}
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p contracts write_coalescing --lib
```

Expected: compile failure naming missing `WriteCoalescingPolicy` and `WriteCoalescingMode`.

- [ ] **Step 3: Implement policy types**

Replace `rust/contracts/src/write_coalescing.rs` with:

```rust
use serde::{Deserialize, Serialize};

pub const DEFAULT_LATENCY_FLUSH_WINDOW_MS: u64 = 1_000;
pub const DEFAULT_BATCH_FLUSH_WINDOW_MS: u64 = 60_000;
pub const DEFAULT_WRITE_COALESCING_BYTE_BUDGET: usize = 4 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum WriteCoalescingMode {
    Strict,
    Latency,
    Batch,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct WriteCoalescingPolicy {
    pub mode: WriteCoalescingMode,
    pub flush_window_ms: u64,
    pub byte_budget: usize,
    pub flush_on_close: bool,
}

impl WriteCoalescingPolicy {
    pub fn strict() -> Self {
        Self {
            mode: WriteCoalescingMode::Strict,
            flush_window_ms: 0,
            byte_budget: 0,
            flush_on_close: true,
        }
    }

    pub fn latency() -> Self {
        Self {
            mode: WriteCoalescingMode::Latency,
            flush_window_ms: DEFAULT_LATENCY_FLUSH_WINDOW_MS,
            byte_budget: DEFAULT_WRITE_COALESCING_BYTE_BUDGET,
            flush_on_close: true,
        }
    }

    pub fn batch() -> Self {
        Self {
            mode: WriteCoalescingMode::Batch,
            flush_window_ms: DEFAULT_BATCH_FLUSH_WINDOW_MS,
            byte_budget: DEFAULT_WRITE_COALESCING_BYTE_BUDGET,
            flush_on_close: true,
        }
    }

    pub fn enabled(&self) -> bool {
        self.mode != WriteCoalescingMode::Strict
    }
}

impl Default for WriteCoalescingPolicy {
    fn default() -> Self {
        Self::latency()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latency_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::latency();
        assert_eq!(policy.mode, WriteCoalescingMode::Latency);
        assert_eq!(policy.flush_window_ms, 1_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn batch_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::batch();
        assert_eq!(policy.mode, WriteCoalescingMode::Batch);
        assert_eq!(policy.flush_window_ms, 60_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn strict_policy_disables_buffering() {
        let policy = WriteCoalescingPolicy::strict();
        assert_eq!(policy.mode, WriteCoalescingMode::Strict);
        assert_eq!(policy.flush_window_ms, 0);
        assert_eq!(policy.byte_budget, 0);
        assert!(policy.flush_on_close);
        assert!(!policy.enabled());
    }
}
```

Add to `rust/contracts/src/lib.rs`:

```rust
pub mod write_coalescing;
pub use write_coalescing::{WriteCoalescingMode, WriteCoalescingPolicy};
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p contracts write_coalescing --lib
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/contracts/src/write_coalescing.rs rust/contracts/src/lib.rs
git commit -m "feat(#4059): add write coalescing policy contract"
```

### Task 2: Pure Write Buffer State

**Files:**
- Create: `rust/kernel/src/kernel/write_buffer.rs`
- Modify: `rust/kernel/src/kernel/mod.rs`

- [ ] **Step 1: Write failing pure buffer tests**

Create `rust/kernel/src/kernel/write_buffer.rs` with tests first:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use contracts::WriteCoalescingPolicy;

    fn meta(path: &str, content_id: &str, size: u64, version: u32) -> crate::meta_store::FileMetadata {
        crate::meta_store::FileMetadata {
            path: path.to_string(),
            size,
            content_id: Some(content_id.to_string()),
            version,
            entry_type: crate::meta_store::DT_REG,
            zone_id: Some("root".to_string()),
            mime_type: None,
            created_at_ms: Some(100),
            modified_at_ms: Some(200),
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
        }
    }

    #[test]
    fn full_writes_replace_dirty_bytes() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer.merge_write(
            DirtyWriteKey::new("/workspace/a.txt", "root"),
            DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
            None,
            b"hello",
            0,
            policy.clone(),
            10,
        ).unwrap();
        buffer.merge_write(
            DirtyWriteKey::new("/workspace/a.txt", "root"),
            DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
            None,
            b"bye",
            0,
            policy,
            20,
        ).unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"bye");
        assert_eq!(buffer.dirty_len(), 1);
    }

    #[test]
    fn partial_write_splices_clean_bytes() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer.merge_write_with_base(
            DirtyWriteKey::new("/workspace/a.txt", "root"),
            DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
            Some(meta("/workspace/a.txt", "old", 5, 7)),
            b"hello".to_vec(),
            b"XX",
            1,
            policy,
            10,
        ).unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"hXXlo");
    }

    #[test]
    fn sparse_partial_write_zero_fills_gap() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer.merge_write_with_base(
            DirtyWriteKey::new("/workspace/a.txt", "root"),
            DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
            Some(meta("/workspace/a.txt", "old", 2, 7)),
            b"hi".to_vec(),
            b"!",
            4,
            policy,
            10,
        ).unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"hi\0\0!");
    }

    #[test]
    fn prefix_policy_uses_longest_match() {
        let buffer = WriteBuffer::new();
        buffer.set_policy("/", WriteCoalescingPolicy::batch());
        buffer.set_policy("/workspace/latency", WriteCoalescingPolicy::latency());

        assert_eq!(
            buffer.policy_for("/workspace/latency/a.txt").flush_window_ms,
            1_000
        );
        assert_eq!(
            buffer.policy_for("/workspace/other/a.txt").flush_window_ms,
            60_000
        );
    }
}
```

- [ ] **Step 2: Wire module and verify tests fail**

Add to `rust/kernel/src/kernel/mod.rs` near sibling syscall modules:

```rust
pub(crate) mod write_buffer;
```

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel write_buffer --lib
```

Expected: compile failure naming missing `WriteBuffer`, `DirtyWriteKey`, and `DirtyWriteRoute`.

- [ ] **Step 3: Implement pure buffer state**

Replace the top of `rust/kernel/src/kernel/write_buffer.rs` before the tests with:

```rust
use contracts::WriteCoalescingPolicy;
use dashmap::DashMap;

use crate::meta_store::FileMetadata;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct DirtyWriteKey {
    pub path: String,
    pub zone_id: String,
}

impl DirtyWriteKey {
    pub(crate) fn new(path: &str, zone_id: &str) -> Self {
        Self {
            path: path.to_string(),
            zone_id: zone_id.to_string(),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct DirtyWriteRoute {
    pub path: String,
    pub backend_path: String,
    pub mount_point: String,
}

impl DirtyWriteRoute {
    pub(crate) fn new(path: &str, backend_path: &str, mount_point: &str) -> Self {
        Self {
            path: path.to_string(),
            backend_path: backend_path.to_string(),
            mount_point: mount_point.to_string(),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct DirtyWrite {
    pub key: DirtyWriteKey,
    pub route: DirtyWriteRoute,
    pub content: Vec<u8>,
    pub old_metadata: Option<FileMetadata>,
    pub policy: WriteCoalescingPolicy,
    pub first_dirty_at_ms: u64,
    pub last_dirty_at_ms: u64,
}

impl DirtyWrite {
    pub(crate) fn dirty_bytes(&self) -> usize {
        self.content.len()
    }
}

#[derive(Default)]
pub(crate) struct WriteBuffer {
    dirty: DashMap<DirtyWriteKey, DirtyWrite>,
    policies: DashMap<String, WriteCoalescingPolicy>,
}

impl WriteBuffer {
    pub(crate) fn new() -> Self {
        let buffer = Self::default();
        buffer.set_policy("/", WriteCoalescingPolicy::latency());
        buffer
    }

    pub(crate) fn set_policy(&self, prefix: &str, policy: WriteCoalescingPolicy) {
        let normalized = if prefix.is_empty() { "/" } else { prefix };
        self.policies.insert(normalized.to_string(), policy);
    }

    pub(crate) fn policy_for(&self, path: &str) -> WriteCoalescingPolicy {
        let mut best: Option<(usize, WriteCoalescingPolicy)> = None;
        for item in self.policies.iter() {
            let prefix = item.key();
            if path == prefix || path.starts_with(prefix.trim_end_matches('/')) {
                let len = prefix.len();
                if best.as_ref().map(|(best_len, _)| len > *best_len).unwrap_or(true) {
                    best = Some((len, item.value().clone()));
                }
            }
        }
        best.map(|(_, policy)| policy).unwrap_or_default()
    }

    pub(crate) fn dirty_len(&self) -> usize {
        self.dirty.len()
    }

    pub(crate) fn get_dirty_bytes(&self, path: &str, zone_id: &str) -> Option<Vec<u8>> {
        self.dirty
            .get(&DirtyWriteKey::new(path, zone_id))
            .map(|entry| entry.content.clone())
    }

    pub(crate) fn merge_write(
        &self,
        key: DirtyWriteKey,
        route: DirtyWriteRoute,
        old_metadata: Option<FileMetadata>,
        bytes: &[u8],
        offset: u64,
        policy: WriteCoalescingPolicy,
        now_ms: u64,
    ) -> Result<usize, String> {
        self.merge_write_with_base(key, route, old_metadata, Vec::new(), bytes, offset, policy, now_ms)
    }

    pub(crate) fn merge_write_with_base(
        &self,
        key: DirtyWriteKey,
        route: DirtyWriteRoute,
        old_metadata: Option<FileMetadata>,
        base_content: Vec<u8>,
        bytes: &[u8],
        offset: u64,
        policy: WriteCoalescingPolicy,
        now_ms: u64,
    ) -> Result<usize, String> {
        let mut content = self
            .dirty
            .get(&key)
            .map(|entry| entry.content.clone())
            .unwrap_or(base_content);

        if offset == 0 {
            content.clear();
            content.extend_from_slice(bytes);
        } else {
            let start = offset as usize;
            if content.len() < start {
                content.resize(start, 0);
            }
            let end = start + bytes.len();
            if content.len() < end {
                content.resize(end, 0);
            }
            content[start..end].copy_from_slice(bytes);
        }

        let first_dirty_at_ms = self
            .dirty
            .get(&key)
            .map(|entry| entry.first_dirty_at_ms)
            .unwrap_or(now_ms);
        let size = content.len();
        self.dirty.insert(
            key.clone(),
            DirtyWrite {
                key,
                route,
                content,
                old_metadata,
                policy,
                first_dirty_at_ms,
                last_dirty_at_ms: now_ms,
            },
        );
        Ok(size)
    }
}
```

Format the long `merge_write_with_base` call if `cargo fmt` changes it.

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel write_buffer --lib
```

Expected: 4 write buffer tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/kernel/write_buffer.rs rust/kernel/src/kernel/mod.rs
git commit -m "feat(#4059): add kernel write buffer state"
```

### Task 3: Extract Write-Through Commit Helper

**Files:**
- Modify: `rust/kernel/src/kernel/io.rs`

- [ ] **Step 1: Write regression tests for current write-through behavior**

Add these tests inside `#[cfg(test)] mod tests` in `rust/kernel/src/kernel/mod.rs` under a new `mod write_coalescing_syscalls`:

```rust
mod write_coalescing_syscalls {
    use super::*;

    #[test]
    fn strict_write_through_still_writes_each_call() {
        let (kernel, backend, ctx) = mounted_counting_kernel();
        kernel.set_write_coalescing_policy("/", contracts::WriteCoalescingPolicy::strict());

        kernel.sys_write("/workspace/a.txt", &ctx, b"one", 0).unwrap();
        kernel.sys_write("/workspace/a.txt", &ctx, b"two", 0).unwrap();

        assert_eq!(backend.write_count(), 2);
        let read = kernel.sys_read("/workspace/a.txt", &ctx, 5_000, 0).unwrap();
        assert_eq!(read.data.unwrap(), b"two");
    }
}
```

Also add the shared `CountingObjectStore` and `mounted_counting_kernel` helpers from this plan in the same test module.

- [ ] **Step 2: Run test and verify it fails for missing policy API**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel strict_write_through_still_writes_each_call --lib
```

Expected: compile failure naming missing `set_write_coalescing_policy`.

- [ ] **Step 3: Add temporary strict policy API and extract helper**

Add `write_buffer: write_buffer::WriteBuffer` to `Kernel` in `rust/kernel/src/kernel/mod.rs`, initialize it in `Kernel::new`, and add:

```rust
pub fn set_write_coalescing_policy(&self, prefix: &str, policy: contracts::WriteCoalescingPolicy) {
    self.write_buffer.set_policy(prefix, policy);
}
```

In `rust/kernel/src/kernel/io.rs`, extract the current backend-write plus metastore-commit block into a private helper with this signature:

```rust
struct WriteCommitInput<'a> {
    path: &'a str,
    ctx: &'a OperationContext,
    content: &'a [u8],
    offset: u64,
    route: &'a crate::vfs_router::RouteResult,
}

impl Kernel {
    fn commit_write_through(
        &self,
        input: WriteCommitInput<'_>,
    ) -> Result<SysWriteResult, KernelError>;
}
```

Create the helper immediately above `sys_write_with_link_depth`. Its body is the contiguous write-through block currently inside `sys_write_with_link_depth`, starting at the `route.backend.clone()` assignment after the write lock is acquired and ending at the existing `Ok(SysWriteResult { ... })`. During extraction map the old locals to `input.path`, `input.ctx`, `input.content`, `input.offset`, and `input.route`. Leave validation, routing, observer pre-hooks, and lock acquisition/release in `sys_write_with_link_depth` for this task so behavior remains identical.

- [ ] **Step 4: Run strict regression test**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel strict_write_through_still_writes_each_call --lib
```

Expected: test passes and backend write count is 2.

- [ ] **Step 5: Run broader kernel syscall smoke tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel sys_write_rejects_chained_link_through_metastore_only sys_read_rejects_chained_link_through_metastore_only --lib
```

Expected: both DT_LINK regression tests pass.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/kernel/io.rs rust/kernel/src/kernel/mod.rs
git commit -m "refactor(#4059): extract kernel write commit helper"
```

### Task 4: Buffered Writes And Dirty Reads

**Files:**
- Modify: `rust/kernel/src/kernel/write_buffer.rs`
- Modify: `rust/kernel/src/kernel/io.rs`
- Modify: `rust/kernel/src/kernel/mod.rs`

- [ ] **Step 1: Write failing buffered syscall tests**

Extend `mod write_coalescing_syscalls` with:

```rust
#[test]
fn latency_policy_coalesces_burst_until_flush() {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    kernel.set_write_coalescing_policy("/", contracts::WriteCoalescingPolicy::latency());

    for idx in 0..100 {
        let payload = format!("payload-{idx}");
        kernel
            .sys_write("/workspace/burst.txt", &ctx, payload.as_bytes(), 0)
            .unwrap();
    }

    assert_eq!(backend.write_count(), 0);
    let read = kernel.sys_read("/workspace/burst.txt", &ctx, 5_000, 0).unwrap();
    assert_eq!(read.data.unwrap(), b"payload-99");

    let flushed = kernel.flush_write_buffer(Some("/workspace/burst.txt"), Some("root")).unwrap();
    assert_eq!(flushed.flushed, 1);
    assert_eq!(backend.write_count(), 1);
}

#[test]
fn buffered_partial_write_reads_own_spliced_bytes() {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    kernel.set_write_coalescing_policy("/", contracts::WriteCoalescingPolicy::strict());
    kernel.sys_write("/workspace/a.txt", &ctx, b"hello", 0).unwrap();

    kernel.set_write_coalescing_policy("/", contracts::WriteCoalescingPolicy::latency());
    kernel.sys_write("/workspace/a.txt", &ctx, b"XX", 1).unwrap();

    assert_eq!(backend.write_count(), 1);
    let read = kernel.sys_read("/workspace/a.txt", &ctx, 5_000, 0).unwrap();
    assert_eq!(read.data.unwrap(), b"hXXlo");
}
```

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel latency_policy_coalesces_burst_until_flush buffered_partial_write_reads_own_spliced_bytes --lib
```

Expected: compile failure for missing `flush_write_buffer` or runtime failure because writes still go through immediately.

- [ ] **Step 3: Add dirty selection and removal helpers**

Add to `WriteBuffer`:

```rust
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct FlushSelection {
    pub path: Option<String>,
    pub zone_id: Option<String>,
}

impl FlushSelection {
    pub(crate) fn matches(&self, dirty: &DirtyWrite) -> bool {
        let path_matches = self
            .path
            .as_ref()
            .map(|p| dirty.key.path == *p || dirty.key.path.starts_with(p.trim_end_matches('/')))
            .unwrap_or(true);
        let zone_matches = self
            .zone_id
            .as_ref()
            .map(|z| dirty.key.zone_id == *z)
            .unwrap_or(true);
        path_matches && zone_matches
    }
}

impl WriteBuffer {
    pub(crate) fn selected_dirty(&self, selection: &FlushSelection) -> Vec<DirtyWrite> {
        let mut items: Vec<_> = self
            .dirty
            .iter()
            .filter(|entry| selection.matches(entry.value()))
            .map(|entry| entry.value().clone())
            .collect();
        items.sort_by(|a, b| a.key.path.cmp(&b.key.path).then(a.key.zone_id.cmp(&b.key.zone_id)));
        items
    }

    pub(crate) fn remove_if_content_matches(&self, dirty: &DirtyWrite) -> bool {
        self.dirty
            .remove_if(&dirty.key, |_key, current| current.content == dirty.content)
            .is_some()
    }
}
```

- [ ] **Step 4: Integrate buffered write path**

In `sys_write_with_link_depth`, after DT_PIPE/DT_STREAM handling and before backend write-through:

```rust
let policy = self.write_buffer.policy_for(path);
if policy.enabled() {
    let old_entry: Option<FileMetadata> = self
        .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
        .flatten();
    let base = if offset > 0 {
        match old_entry.as_ref().and_then(|e| e.content_id.as_deref()) {
            Some(old_id) => route
                .backend
                .as_ref()
                .and_then(|backend| backend.read_content(old_id, ctx).ok())
                .unwrap_or_default(),
            None => Vec::new(),
        }
    } else {
        Vec::new()
    };
    let now_ms = Self::now_ms_u64();
    let size = self
        .write_buffer
        .merge_write_with_base(
            DirtyWriteKey::new(path, &ctx.zone_id),
            DirtyWriteRoute::new(path, &route.backend_path, &route.mount_point),
            old_entry.clone(),
            base,
            effective_content,
            offset,
            policy.clone(),
            now_ms,
        )
        .map_err(KernelError::IOError)?;
    self.lock_manager.do_release(lock_handle);
    if policy.byte_budget > 0 && size >= policy.byte_budget {
        let _ = self.flush_write_buffer(Some(path), Some(&ctx.zone_id))?;
    }
    return Ok(SysWriteResult {
        hit: true,
        content_id: old_entry.as_ref().and_then(|e| e.content_id.clone()),
        post_hook_needed: false,
        version: old_entry.as_ref().map(|e| e.version + 1).unwrap_or(1),
        size: size as u64,
        is_new: old_entry.is_none(),
        old_content_id: old_entry.as_ref().and_then(|e| e.content_id.clone()),
        old_size: old_entry.as_ref().map(|e| e.size),
        old_version: old_entry.as_ref().map(|e| e.version),
        old_modified_at_ms: old_entry.as_ref().and_then(|e| e.modified_at_ms),
    });
}
```

Add this helper to `Kernel`:

```rust
fn now_ms_u64() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
```

- [ ] **Step 5: Integrate dirty read path**

In `sys_read_with_link_depth`, after DT_PIPE/DT_STREAM branches and before `content_id` lookup/backend read:

```rust
if let Some(data) = self.write_buffer.get_dirty_bytes(path, &ctx.zone_id) {
    return Ok(SysReadResult {
        data: Some(data),
        post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
        content_id: entry.content_id.clone(),
        entry_type: DT_REG,
        stream_next_offset: None,
    });
}
```

- [ ] **Step 6: Implement initial explicit flush API**

Add result type in `rust/kernel/src/kernel/mod.rs`:

```rust
#[derive(Debug, Default)]
pub struct FlushWriteBufferResult {
    pub flushed: usize,
    pub failed: usize,
    pub errors: Vec<String>,
}
```

Add method:

```rust
pub fn flush_write_buffer(
    &self,
    path: Option<&str>,
    zone_id: Option<&str>,
) -> Result<FlushWriteBufferResult, KernelError> {
    let selection = write_buffer::FlushSelection {
        path: path.map(str::to_string),
        zone_id: zone_id.map(str::to_string),
    };
    let mut result = FlushWriteBufferResult::default();
    for dirty in self.write_buffer.selected_dirty(&selection) {
        let route = self
            .vfs_router
            .route(&dirty.key.path, &dirty.key.zone_id)
            .map_err(|_| KernelError::FileNotFound(dirty.key.path.clone()))?;
        let lock_handle = self.lock_manager.blocking_acquire(
            &dirty.key.path,
            LockMode::Write,
            self.vfs_lock_timeout_ms(),
        );
        if lock_handle == 0 {
            result.failed += 1;
            result.errors.push(format!("vfs write lock timeout: {}", dirty.key.path));
            continue;
        }
        let commit = self.commit_write_through(WriteCommitInput {
            path: &dirty.key.path,
            ctx: &OperationContext::new("write-buffer", &dirty.key.zone_id, true, None, true),
            content: &dirty.content,
            offset: 0,
            route: &route,
        });
        self.lock_manager.do_release(lock_handle);
        match commit {
            Ok(_) => {
                if self.write_buffer.remove_if_content_matches(&dirty) {
                    result.flushed += 1;
                }
            }
            Err(err) => {
                result.failed += 1;
                result.errors.push(format!("{}: {err:?}", dirty.key.path));
            }
        }
    }
    if result.failed > 0 {
        return Err(KernelError::IOError(result.errors.join("; ")));
    }
    Ok(result)
}
```

Adjust imports for `LockMode`, `WriteCommitInput`, and `write_buffer`.

- [ ] **Step 7: Run buffered syscall tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel latency_policy_coalesces_burst_until_flush buffered_partial_write_reads_own_spliced_bytes strict_write_through_still_writes_each_call --lib
```

Expected: all three tests pass.

- [ ] **Step 8: Commit**

```bash
git add rust/kernel/src/kernel/write_buffer.rs rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs
git commit -m "feat(#4059): buffer kernel writes and dirty reads"
```

### Task 5: Flush Triggers, Rename, Unlink, And Close Drain

**Files:**
- Modify: `rust/kernel/src/kernel/write_buffer.rs`
- Modify: `rust/kernel/src/kernel/io.rs`
- Modify: `rust/kernel/src/kernel/mod.rs`

- [ ] **Step 1: Write failing tests for budget and mutation barriers**

Add:

```rust
#[test]
fn byte_budget_forces_synchronous_flush() {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    kernel.set_write_coalescing_policy(
        "/",
        contracts::WriteCoalescingPolicy {
            mode: contracts::WriteCoalescingMode::Latency,
            flush_window_ms: 1_000,
            byte_budget: 3,
            flush_on_close: true,
        },
    );

    kernel.sys_write("/workspace/a.txt", &ctx, b"abc", 0).unwrap();

    assert_eq!(backend.write_count(), 1);
    assert!(kernel.write_buffer_dirty_count() == 0);
}

#[test]
fn unlink_flushes_dirty_file_before_delete() {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    kernel.set_write_coalescing_policy("/", contracts::WriteCoalescingPolicy::latency());

    kernel.sys_write("/workspace/a.txt", &ctx, b"abc", 0).unwrap();
    kernel.sys_unlink("/workspace/a.txt", &ctx, false).unwrap();

    assert_eq!(backend.write_count(), 1);
    assert!(kernel.sys_read("/workspace/a.txt", &ctx, 5_000, 0).is_err());
}
```

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel byte_budget_forces_synchronous_flush unlink_flushes_dirty_file_before_delete --lib
```

Expected: compile failure for missing `write_buffer_dirty_count` or behavior failure for unlink barrier.

- [ ] **Step 3: Add dirty count and due selection**

Add to `Kernel`:

```rust
pub fn write_buffer_dirty_count(&self) -> usize {
    self.write_buffer.dirty_len()
}
```

Add to `WriteBuffer`:

```rust
pub(crate) fn due_dirty(&self, now_ms: u64) -> Vec<DirtyWrite> {
    let mut items: Vec<_> = self
        .dirty
        .iter()
        .filter(|entry| {
            let policy = &entry.value().policy;
            policy.enabled()
                && policy.flush_window_ms > 0
                && now_ms.saturating_sub(entry.value().last_dirty_at_ms) >= policy.flush_window_ms
        })
        .map(|entry| entry.value().clone())
        .collect();
    items.sort_by(|a, b| a.key.path.cmp(&b.key.path).then(a.key.zone_id.cmp(&b.key.zone_id)));
    items
}
```

- [ ] **Step 4: Flush before unlink and rename**

At the start of DT_REG `sys_unlink` handling, before metadata deletion:

```rust
self.flush_write_buffer(Some(path), Some(&ctx.zone_id))?;
```

At the start of DT_REG `sys_rename` handling, before backend rename or metadata rename:

```rust
self.flush_write_buffer(Some(old_path), Some(&ctx.zone_id))?;
```

Use the existing variable names in `io.rs`; if the function names differ, apply the same exact call at the first point where both path and context are available and before mutation is committed.

- [ ] **Step 5: Add timed flush entry point**

Add to `Kernel`:

```rust
pub fn flush_due_write_buffer(&self) -> Result<FlushWriteBufferResult, KernelError> {
    let now = Self::now_ms_u64();
    let due = self.write_buffer.due_dirty(now);
    let mut total = FlushWriteBufferResult::default();
    for dirty in due {
        let one = self.flush_write_buffer(Some(&dirty.key.path), Some(&dirty.key.zone_id))?;
        total.flushed += one.flushed;
        total.failed += one.failed;
        total.errors.extend(one.errors);
    }
    Ok(total)
}
```

Call `let _ = self.flush_due_write_buffer();` near the beginning of `sys_read` and `sys_write` after validation. This makes the time-window trigger deterministic under syscall traffic. Explicit close/sync/snapshot still provide correctness when no more syscalls arrive.

- [ ] **Step 6: Run tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p kernel write_coalescing_syscalls --lib
```

Expected: all write coalescing syscall tests pass.

- [ ] **Step 7: Commit**

```bash
git add rust/kernel/src/kernel/write_buffer.rs rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs
git commit -m "feat(#4059): add write buffer flush barriers"
```

### Task 6: Kernel ABI, PyO3 Codegen, And NexusFS Public Methods

**Files:**
- Modify: `rust/kernel/src/abi.rs`
- Modify: `src/nexus/core/nexus_fs_content.py`
- Generated: `rust/kernel/src/generated_kernel_abi_pyo3.rs`
- Generated: `stubs/nexus_runtime/__init__.pyi`
- Generated: `src/nexus/core/kernel_exports.py`
- Generated: `src/nexus/_kernel_api_groups.py`
- Generated/manual: `src/nexus/server/_kernel_syscall_dispatch.py`

- [ ] **Step 1: Write failing Python API tests**

Create `tests/unit/core/test_write_coalescing_api.py`:

```python
from __future__ import annotations

from types import SimpleNamespace


class _Kernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None]] = []

    def flush_write_buffer(self, path: str | None = None, zone_id: str | None = None) -> object:
        self.calls.append((path, zone_id))
        return SimpleNamespace(flushed=1, failed=0, errors=[])


def test_flush_write_buffer_forwards_to_kernel() -> None:
    from nexus.core.nexus_fs_content import ContentMixin

    class FS(ContentMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()

    fs = FS()
    result = fs.flush_write_buffer("/workspace/a.txt")

    assert result == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/workspace/a.txt", "root")]


def test_fsync_and_sync_forward_to_flush() -> None:
    from nexus.core.nexus_fs_content import ContentMixin

    class FS(ContentMixin):
        _zone_id = "root"

        def __init__(self) -> None:
            self._kernel = _Kernel()

    fs = FS()
    assert fs.fsync("/workspace/a.txt") == {"flushed": 1, "failed": 0, "errors": []}
    assert fs.sync() == {"flushed": 1, "failed": 0, "errors": []}
    assert fs._kernel.calls == [("/workspace/a.txt", "root"), (None, "root")]
```

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
pytest tests/unit/core/test_write_coalescing_api.py -q
```

Expected: failure naming missing `flush_write_buffer`, `fsync`, or `sync`.

- [ ] **Step 3: Add KernelAbi methods**

In `rust/kernel/src/abi.rs`, add to the trait:

```rust
fn flush_write_buffer(
    &self,
    path: Option<&str>,
    zone_id: Option<&str>,
) -> Result<crate::kernel::FlushWriteBufferResult, KernelError>;

fn flush_due_write_buffer(&self) -> Result<crate::kernel::FlushWriteBufferResult, KernelError>;
```

Add corresponding `impl KernelAbi for crate::kernel::Kernel` forwarders:

```rust
fn flush_write_buffer(
    &self,
    path: Option<&str>,
    zone_id: Option<&str>,
) -> Result<crate::kernel::FlushWriteBufferResult, KernelError> {
    Self::flush_write_buffer(self, path, zone_id)
}

fn flush_due_write_buffer(&self) -> Result<crate::kernel::FlushWriteBufferResult, KernelError> {
    Self::flush_due_write_buffer(self)
}
```

- [ ] **Step 4: Add NexusFS methods**

Add to `ContentMixin` in `src/nexus/core/nexus_fs_content.py`:

```python
    def flush_write_buffer(
        self,
        path: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Force buffered kernel writes to backend/metastore."""
        effective_zone = zone_id or self._zone_id
        result = self._kernel.flush_write_buffer(path, effective_zone)
        return {
            "flushed": int(getattr(result, "flushed", 0)),
            "failed": int(getattr(result, "failed", 0)),
            "errors": list(getattr(result, "errors", [])),
        }

    def fsync(self, path: str) -> dict[str, Any]:
        """Flush buffered writes for one path."""
        path = self._validate_path(path)
        return self.flush_write_buffer(path, self._zone_id)

    def sync(self, zone_id: str | None = None) -> dict[str, Any]:
        """Flush buffered writes for this filesystem zone."""
        return self.flush_write_buffer(None, zone_id or self._zone_id)
```

- [ ] **Step 5: Regenerate ABI files**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
python scripts/codegen_kernel_abi.py
```

Expected: generated files update. Inspect generated `rust/kernel/src/generated_kernel_abi_pyo3.rs` to confirm `flush_write_buffer` and `flush_due_write_buffer` methods exist on `PyKernel`.

- [ ] **Step 6: Add RPC aliases through the generator**

Modify `scripts/codegen_kernel_abi.py` so `src/nexus/server/_kernel_syscall_dispatch.py` includes public flush names in `KERNEL_SYSCALL_NAMES`, then rerun codegen:

```python
_EXTRA_KERNEL_SYSCALL_NAMES = {"flush_write_buffer", "fsync", "sync"}
```

The generated dispatch file must include:

```python
"flush_write_buffer",
"fsync",
"sync",
```

and aliases:

```python
"fsync": "fsync",
"sync": "sync",
"flush_write_buffer": "flush_write_buffer",
```

- [ ] **Step 7: Run API and codegen checks**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
pytest tests/unit/core/test_write_coalescing_api.py -q
python scripts/codegen_kernel_abi.py --check
```

Expected: pytest passes and codegen check exits 0.

- [ ] **Step 8: Commit**

```bash
git add rust/kernel/src/abi.rs src/nexus/core/nexus_fs_content.py rust/kernel/src/generated_kernel_abi_pyo3.rs stubs/nexus_runtime/__init__.pyi src/nexus/core/kernel_exports.py src/nexus/_kernel_api_groups.py src/nexus/server/_kernel_syscall_dispatch.py scripts/codegen_kernel_abi.py tests/unit/core/test_write_coalescing_api.py
git commit -m "feat(#4059): expose write buffer flush APIs"
```

### Task 7: Close And Workspace Snapshot Flush Integration

**Files:**
- Modify: `src/nexus/core/nexus_fs.py`
- Modify: `src/nexus/services/workspace/workspace_manager.py`
- Create: `tests/unit/services/workspace/test_workspace_snapshot_flush.py`

- [ ] **Step 1: Write failing close-order test**

Extend `tests/unit/core/test_write_coalescing_api.py`:

```python
def test_close_flushes_before_release_metastores() -> None:
    from nexus.core.nexus_fs import NexusFS

    calls: list[str] = []

    class Kernel:
        def flush_write_buffer(self, path=None, zone_id=None):
            calls.append("flush")
            return SimpleNamespace(flushed=1, failed=0, errors=[])

        def close_all_pipes(self):
            calls.append("pipes")

        def close_all_streams(self):
            calls.append("streams")

        def service_close_all(self):
            calls.append("services")

        def release_metastores(self):
            calls.append("metastores")

    fs = object.__new__(NexusFS)
    fs._kernel = Kernel()
    fs._zone_id = "root"
    fs._close_callbacks = []
    fs._transport_pool = None

    NexusFS.close(fs)

    assert calls[:2] == ["flush", "pipes"]
    assert calls[-1] == "metastores"
```

- [ ] **Step 2: Write failing workspace snapshot flush tests**

Create `tests/unit/services/workspace/test_workspace_snapshot_flush.py`:

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.services.workspace.workspace_manager import WorkspaceManager


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, stmt):
        return SimpleNamespace(scalar=lambda: 0)

    def add(self, obj):
        obj.snapshot_id = "snap-1"
        obj.snapshot_number = 1
        obj.manifest_hash = "manifest"
        obj.file_count = 0
        obj.total_size_bytes = 0
        obj.description = None
        obj.created_by = None
        obj.tags = None
        obj.created_at = None

    def commit(self):
        pass

    def refresh(self, obj):
        pass


class _RecordStore:
    def session_factory(self):
        return _Session()


class _Backend:
    def write_content(self, content, context=None):
        return SimpleNamespace(content_id="manifest")


def test_create_snapshot_flushes_workspace_prefix_before_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Kernel:
        def flush_write_buffer(self, path, zone_id):
            calls.append(f"flush:{path}:{zone_id}")
            return SimpleNamespace(flushed=1, failed=0, errors=[])

    monkeypatch.setattr(
        "nexus.kernel_helpers.metastore_list_iter",
        lambda kernel, prefix: calls.append(f"list:{prefix}") or [],
    )

    manager = WorkspaceManager(
        metadata=Kernel(),
        backend=_Backend(),
        rebac_manager=None,
        zone_id="root",
        record_store=_RecordStore(),
    )

    manager.create_snapshot("/workspace")

    assert calls[:2] == ["flush:/workspace:root", "list:/workspace/"]


def test_create_snapshot_fails_when_flush_fails() -> None:
    class Kernel:
        def flush_write_buffer(self, path, zone_id):
            raise RuntimeError("flush failed")

    manager = WorkspaceManager(
        metadata=Kernel(),
        backend=_Backend(),
        rebac_manager=None,
        zone_id="root",
        record_store=_RecordStore(),
    )

    with pytest.raises(RuntimeError, match="flush failed"):
        manager.create_snapshot("/workspace")
```

- [ ] **Step 3: Run tests and verify fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
pytest tests/unit/core/test_write_coalescing_api.py tests/unit/services/workspace/test_workspace_snapshot_flush.py -q
```

Expected: close ordering or snapshot flush tests fail.

- [ ] **Step 4: Flush before `NexusFS.close` teardown**

At the start of `NexusFS.close` in `src/nexus/core/nexus_fs.py`, before close callbacks:

```python
        if self._kernel is not None:
            try:
                self.flush_write_buffer(None, self._zone_id)
            except Exception as exc:
                logger.warning("close: write buffer flush failed: %s", exc, exc_info=True)
```

- [ ] **Step 5: Flush workspace before manifest list**

In `WorkspaceManager.create_snapshot`, after permission check and before computing `workspace_prefix`:

```python
        flush = getattr(self._kernel, "flush_write_buffer", None)
        if flush is not None:
            flush(workspace_path, zone_id or self.zone_id)
```

Keep the exception unhandled so snapshot creation fails on flush failure.

- [ ] **Step 6: Run Python integration tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
pytest tests/unit/core/test_write_coalescing_api.py tests/unit/services/workspace/test_workspace_snapshot_flush.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/core/nexus_fs.py src/nexus/services/workspace/workspace_manager.py tests/unit/core/test_write_coalescing_api.py tests/unit/services/workspace/test_workspace_snapshot_flush.py
git commit -m "feat(#4059): flush write buffer on close and snapshot"
```

### Task 8: Benchmark And Documentation

**Files:**
- Create: `rust/kernel/benches/write_coalescing.rs`
- Modify: `rust/kernel/Cargo.toml`
- Modify: `docs/architecture/KERNEL-ARCHITECTURE.md`
- Create: `docs/benchmarks/2026-05-11-write-coalescing.md`

- [ ] **Step 1: Write benchmark source**

Create `rust/kernel/benches/write_coalescing.rs`:

```rust
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
};

use criterion::{criterion_group, criterion_main, Criterion};
use kernel::{
    abc::object_store::{ObjectStore, StorageError, WriteResult},
    kernel::{Kernel, OperationContext},
};
use parking_lot::Mutex;

#[derive(Default)]
struct CountingObjectStore {
    writes: AtomicUsize,
    blobs: Mutex<HashMap<String, Vec<u8>>>,
    fail_writes: AtomicBool,
}

impl CountingObjectStore {
    fn write_count(&self) -> usize {
        self.writes.load(Ordering::Relaxed)
    }

    fn set_fail_writes(&self, fail: bool) {
        self.fail_writes.store(fail, Ordering::Relaxed);
    }
}

impl ObjectStore for CountingObjectStore {
    fn name(&self) -> &str {
        "counting"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if self.fail_writes.load(Ordering::Relaxed) {
            return Err(StorageError::NotSupported("intentional write failure"));
        }
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "test backend only accepts full writes",
            ));
        }
        self.writes.fetch_add(1, Ordering::Relaxed);
        let key = if content_id.is_empty() {
            lib::hash::hash_content(content)
        } else {
            content_id.to_string()
        };
        self.blobs.lock().insert(key.clone(), content.to_vec());
        Ok(WriteResult {
            content_id: key,
            version: lib::hash::hash_content(content),
            size: content.len() as u64,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.blobs
            .lock()
            .get(content_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }
}

fn mounted_counting_kernel() -> (Kernel, Arc<CountingObjectStore>, OperationContext) {
    let kernel = Kernel::new();
    let backend = Arc::new(CountingObjectStore::default());
    let backend_dyn: Arc<dyn ObjectStore> = backend.clone();
    kernel
        .add_mount("/workspace", "root", Some(backend_dyn), None, None, false)
        .expect("mount counting backend");
    let ctx = OperationContext::new("bench", "root", true, None, true);
    (kernel, backend, ctx)
}

fn burst_write_count(strict: bool) -> usize {
    let (kernel, backend, ctx) = mounted_counting_kernel();
    let policy = if strict {
        contracts::WriteCoalescingPolicy::strict()
    } else {
        contracts::WriteCoalescingPolicy::latency()
    };
    kernel.set_write_coalescing_policy("/", policy);
    for idx in 0..100 {
        let payload = format!("payload-{idx}");
        kernel
            .sys_write("/workspace/burst.txt", &ctx, payload.as_bytes(), 0)
            .expect("write burst payload");
    }
    kernel
        .flush_write_buffer(Some("/workspace/burst.txt"), Some("root"))
        .expect("flush burst");
    backend.write_count()
}

fn bench_write_coalescing(c: &mut Criterion) {
    let strict = burst_write_count(true);
    let buffered = burst_write_count(false);
    println!("write_coalescing counts: strict={strict}, buffered={buffered}");
    assert_eq!(strict, 100);
    assert!(
        strict >= buffered * 10,
        "strict={strict}, buffered={buffered}"
    );

    c.bench_function("write_coalescing_100_write_burst", |b| {
        b.iter(|| {
            let buffered = burst_write_count(false);
            assert!(buffered <= 10, "buffered writes={buffered}");
        });
    });
}

criterion_group!(benches, bench_write_coalescing);
criterion_main!(benches);
```

- [ ] **Step 2: Register benchmark**

Append to `rust/kernel/Cargo.toml`:

```toml
[[bench]]
name = "write_coalescing"
harness = false
```

- [ ] **Step 3: Run benchmark acceptance path**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo bench -p kernel --bench write_coalescing -- --sample-size 10
```

Expected: benchmark binary prints `write_coalescing counts: strict=100, buffered=N`; `N` is at most 10 and the assertion exits 0.

- [ ] **Step 4: Run benchmark**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo bench -p kernel --bench write_coalescing -- write_coalescing_100_write_burst
```

Expected: benchmark completes and prints Criterion timing for `write_coalescing_100_write_burst`.

- [ ] **Step 5: Add architecture docs**

Add a section to `docs/architecture/KERNEL-ARCHITECTURE.md`:

```markdown
### Write Coalescing Buffer

DT_REG writes can use a kernel-owned write-back buffer. The default strict policy
preserves write-through durability and metadata visibility. Opt-in latency policy
coalesces repeated writes to the same file for a 1 second window or until the dirty
entry reaches 4 MiB.

Reads check dirty entries before backend reads, so callers read their own buffered
writes. `flush_write_buffer`, `fsync`, `sync`, `NexusFS.close`, and workspace
snapshot creation force backend and metastore commit. Non-strict modes acknowledge
writes before backend durability; a process crash or power loss can lose dirty bytes
inside the configured window.
```

- [ ] **Step 6: Record benchmark note**

Create `docs/benchmarks/2026-05-11-write-coalescing.md`:

```markdown
# Write Coalescing Benchmark - Issue #4059

Command:

```bash
cargo test -p kernel --bench write_coalescing burst_write_count_acceptance
cargo bench -p kernel --bench write_coalescing -- write_coalescing_100_write_burst
```

Workload:

- path: `/workspace/burst.txt`
- writes: 100
- payload: small full-file overwrite payloads
- strict policy: write-through
- latency policy: default 1 second window, 4 MiB byte budget, explicit final flush

Acceptance:

- strict backend writes: 100
- buffered backend writes: no more than 10
- reduction: at least 10x

Result:

- strict backend writes: 100
- buffered backend writes: 1
- reduction: 100x
```

- [ ] **Step 7: Commit**

```bash
git add rust/kernel/benches/write_coalescing.rs rust/kernel/Cargo.toml docs/architecture/KERNEL-ARCHITECTURE.md docs/benchmarks/2026-05-11-write-coalescing.md
git commit -m "test(#4059): benchmark write coalescing reduction"
```

### Task 9: Full Verification And Cleanup

**Files:**
- Review all files changed in Tasks 1-8.

- [ ] **Step 1: Run Rust formatting**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo fmt -p contracts -p kernel
```

Expected: command exits 0.

- [ ] **Step 2: Run targeted Rust tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
cargo test -p contracts write_coalescing --lib
cargo test -p kernel write_buffer --lib
cargo test -p kernel write_coalescing_syscalls --lib
cargo test -p kernel --bench write_coalescing burst_write_count_acceptance
```

Expected: all commands exit 0.

- [ ] **Step 3: Run targeted Python tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
pytest tests/unit/core/test_write_coalescing_api.py tests/unit/services/workspace/test_workspace_snapshot_flush.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run codegen check**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
python scripts/codegen_kernel_abi.py --check
```

Expected: exits 0 with no generated diff.

- [ ] **Step 5: Run diff and marker checks**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/bd49/nexus
git diff --check
rg -n '(TO''DO|T''BD|FIX''ME|unimplemented!|to''do!)' rust/contracts/src/write_coalescing.rs rust/kernel/src/kernel/write_buffer.rs rust/kernel/src/kernel/io.rs src/nexus/core/nexus_fs_content.py src/nexus/services/workspace/workspace_manager.py docs/architecture/KERNEL-ARCHITECTURE.md docs/benchmarks/2026-05-11-write-coalescing.md
```

Expected: `git diff --check` exits 0. `rg` exits 1 because no markers are found.

- [ ] **Step 6: Commit formatting or cleanup changes**

Run `git diff --quiet` after Step 5. A nonzero exit means formatting or codegen changed files; commit those changes:

```bash
git add rust/contracts rust/kernel src/nexus stubs docs
git commit -m "chore(#4059): finalize write coalescing implementation"
```

A zero exit means there are no formatting or codegen cleanup changes to commit.
