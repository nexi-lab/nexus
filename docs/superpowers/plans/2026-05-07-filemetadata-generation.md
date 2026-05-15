# FileMetadata Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted, monotonic `gen: u64` content-generation counter to Nexus file metadata and expose it through stat, gRPC, and FUSE cache/xattr surfaces.

**Architecture:** Treat `gen` as a content-generation counter distinct from `content_id` and `version`: writes and copy-overwrites bump it, metadata-only operations preserve it, and old records default to zero. Proto metadata is the cross-language source of truth, redb stores a v4 row that can still read v3 rows, and FUSE validates cached file bodies by comparing cached generation to current stat generation.

**Tech Stack:** Rust (`kernel`, `raft`, `transport`, `nexus-fuse`), redb, prost/tonic proto build scripts, Python metadata generator (`scripts/gen_metadata.py`), pytest, cargo test.

---

## File Structure

- Modify `proto/nexus/core/metadata.proto`: add `uint64 gen = 18` to canonical `FileMetadata`.
- Modify `proto/nexus/grpc/vfs/vfs.proto`: add `uint64 gen` to typed read/write responses.
- Generate `src/nexus/contracts/metadata.py`, `src/nexus/core/_compact_generated.py`, `src/nexus/storage/_metadata_mapper_generated.py`, `src/nexus/core/metadata_pb2.py`, and `src/nexus/core/metadata_pb2.pyi` with `python scripts/gen_metadata.py`.
- Modify `scripts/gen_metadata.py`: teach generated Python metadata about proto `uint64`.
- Modify `rust/kernel/src/abc/meta_store.rs`: add `gen: u64` to kernel `FileMetadata`.
- Modify `rust/kernel/src/core/meta_store/mod.rs`: v4 redb serialization/deserialization, tests, and test helpers.
- Modify `rust/kernel/src/core/meta_store/remote.rs`: include `gen` in JSON put/get.
- Modify `rust/kernel/src/core/dispatch/mod.rs`: include `gen` in mutation event payloads.
- Modify `rust/kernel/src/kernel/mod.rs`: add `gen` to `SysReadResult`, `SysWriteResult`, `SysCopyResult`, `StatResult`, and `build_metadata`.
- Modify `rust/kernel/src/kernel/io.rs`: compute generation in write, batch write, read, stat, copy, and zero-gen metadata paths.
- Modify `rust/kernel/src/generated_kernel_abi_pyo3.rs`: expose `gen` in `PySysWriteResult` and `sys_stat` dict.
- Modify `rust/raft/src/zone_meta_store.rs` and `rust/raft/src/zone_manager.rs`: preserve `gen` through proto conversion and mount helper construction.
- Modify `rust/transport/src/grpc.rs`: fill typed gRPC read/write `gen`.
- Modify `rust/kernel/src/rpc_transport.rs`: return typed read/write `gen` to remote callers.
- Modify `src/nexus/core/nexus_fs_content.py`: include `gen` in write/read metadata dictionaries and hook metadata where available.
- Modify `src/nexus/core/nexus_fs_metadata.py`: include `gen` in Python fallback stat dictionaries.
- Modify `src/nexus/fuse/rust_client.py`: include `gen` in Python Rust FUSE client stat dataclass.
- Modify `nexus-fuse/src/client.rs`: parse `gen` in stat/read/write response structs.
- Modify `nexus-fuse/src/cache.rs`: store and validate cached generation.
- Modify `nexus-fuse/src/fs.rs`: pass stat generation into cache reads and implement `user.nexus.gen` xattr.
- Do not edit `nexus-fuse/src/daemon.rs`: it serializes `client.stat` with `serde_json::to_value`, so `gen` is returned once the client metadata struct has the field.
- Test in existing Rust modules plus `tests/unit/storage/test_metadata_generation.py` and `nexus-fuse` tests.

### Task 1: Proto And Python Metadata Generation

**Files:**
- Modify: `proto/nexus/core/metadata.proto`
- Modify: `scripts/gen_metadata.py`
- Generate: `src/nexus/contracts/metadata.py`
- Generate: `src/nexus/core/_compact_generated.py`
- Generate: `src/nexus/storage/_metadata_mapper_generated.py`
- Generate: `src/nexus/core/metadata_pb2.py`
- Generate: `src/nexus/core/metadata_pb2.pyi`
- Create: `tests/unit/storage/test_metadata_generation.py`

- [ ] **Step 1: Write failing Python metadata tests**

Create `tests/unit/storage/test_metadata_generation.py` with:

```python
from __future__ import annotations

from nexus.contracts.metadata import FileMetadata
from nexus.storage._metadata_mapper_generated import MetadataMapper


def test_metadata_json_round_trip_preserves_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, content_id="cid", gen=7)

    encoded = MetadataMapper.to_json(meta)
    restored = MetadataMapper.from_json(encoded)

    assert encoded["gen"] == 7
    assert restored.gen == 7


def test_metadata_json_missing_generation_defaults_to_zero() -> None:
    restored = MetadataMapper.from_json({"path": "/docs/a.txt", "size": 5})

    assert restored.gen == 0


def test_metadata_proto_round_trip_preserves_generation() -> None:
    meta = FileMetadata(path="/docs/a.txt", size=5, content_id="cid", gen=11)

    proto = MetadataMapper.to_proto(meta)
    restored = MetadataMapper.from_proto(proto)

    assert proto.gen == 11
    assert restored.gen == 11
```

- [ ] **Step 2: Run the Python metadata test to verify RED**

Run:

```bash
pytest tests/unit/storage/test_metadata_generation.py -q
```

Expected: FAIL because `FileMetadata.__init__()` does not accept `gen`, or because the generated mapper omits `"gen"`.

- [ ] **Step 3: Add `gen` to the canonical metadata proto**

In `proto/nexus/core/metadata.proto`, add this field after `link_target = 17`:

```proto
  // Monotonic per-file content generation. Starts at 1 on the first
  // successful content write by an upgraded writer. Existing records and
  // non-content metadata entries default to 0.
  uint64 gen = 18;
```

- [ ] **Step 4: Teach the Python generator about `uint64` and the default**

In `scripts/gen_metadata.py`, update `PROTO_TYPE_MAP` and `FIELD_DEFAULTS`:

```python
PROTO_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "int64": "int",
    "uint64": "int",
    "int32": "int",
    "double": "float",
    "bool": "bool",
    "DirEntryType": "int",
}
```

```python
FIELD_DEFAULTS: dict[str, str] = {
    "version": "1",
    "entry_type": "0",
    "ttl_seconds": "0.0",
    "gen": "0",
}
```

Also add `"gen": "int"` to `DIRECT_COMPACT_FIELDS`.

- [ ] **Step 5: Generate Python metadata files**

Run:

```bash
python scripts/gen_metadata.py
```

Expected: generated files are updated and the script prints `Done. SSOT: proto/nexus/core/metadata.proto`.

- [ ] **Step 6: Verify the Python metadata test passes**

Run:

```bash
pytest tests/unit/storage/test_metadata_generation.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit proto and generated Python metadata**

Run:

```bash
git add proto/nexus/core/metadata.proto scripts/gen_metadata.py src/nexus/contracts/metadata.py src/nexus/core/_compact_generated.py src/nexus/storage/_metadata_mapper_generated.py src/nexus/core/metadata_pb2.py src/nexus/core/metadata_pb2.pyi tests/unit/storage/test_metadata_generation.py
git commit -m "feat: add metadata generation to proto"
```

### Task 2: Kernel Metadata Storage And Migration

**Files:**
- Modify: `rust/kernel/src/abc/meta_store.rs`
- Modify: `rust/kernel/src/core/meta_store/mod.rs`

- [ ] **Step 1: Write failing redb serialization tests**

In `rust/kernel/src/core/meta_store/mod.rs`, add these tests inside `#[cfg(test)] mod tests`:

```rust
#[test]
fn serialize_roundtrip_preserves_gen() {
    let meta = FileMetadata {
        path: "/gen.txt".to_string(),
        size: 9,
        content_id: Some("hash".to_string()),
        version: 2,
        entry_type: 0,
        zone_id: Some("root".to_string()),
        mime_type: Some("text/plain".to_string()),
        created_at_ms: Some(10),
        modified_at_ms: Some(20),
        last_writer_address: Some("nexus-1:2028".to_string()),
        target_zone_id: None,
        link_target: None,
        gen: 42,
    };

    let restored = deserialize_metadata(&serialize_metadata(&meta)).unwrap();

    assert_eq!(restored.gen, 42);
    assert_eq!(restored.path, "/gen.txt");
    assert_eq!(restored.content_id.as_deref(), Some("hash"));
}

#[test]
fn deserialize_v3_metadata_defaults_gen_to_zero() {
    let meta = FileMetadata {
        path: "/old.txt".to_string(),
        size: 1,
        content_id: Some("oldhash".to_string()),
        version: 1,
        entry_type: 0,
        zone_id: None,
        mime_type: None,
        created_at_ms: None,
        modified_at_ms: None,
        last_writer_address: None,
        target_zone_id: None,
        link_target: None,
        gen: 99,
    };
    let mut bytes = serialize_metadata(&meta);
    bytes[0] = 3;
    bytes.truncate(bytes.len() - 8);

    let restored = deserialize_metadata(&bytes).unwrap();

    assert_eq!(restored.gen, 0);
}
```

- [ ] **Step 2: Run the redb serialization tests to verify RED**

Run:

```bash
cargo test -p kernel serialize_roundtrip_preserves_gen deserialize_v3_metadata_defaults_gen_to_zero
```

Expected: FAIL because `FileMetadata` has no `gen` field and serialization does not handle v4.

- [ ] **Step 3: Add `gen` to kernel `FileMetadata`**

In `rust/kernel/src/abc/meta_store.rs`, add this field after `content_id`:

```rust
    /// Monotonic per-file content generation. Existing migrated records and
    /// non-content metadata entries use 0.
    pub gen: u64,
```

- [ ] **Step 4: Update redb serializer to write v4 records**

In `rust/kernel/src/core/meta_store/mod.rs`, change the serializer tag and append `gen`:

```rust
    buf.push(4); // version tag — v4 appends gen:u64 after the v3 fields.
```

After `write_opt_str(&mut buf, &meta.link_target);`, add:

```rust
    buf.extend_from_slice(&meta.gen.to_le_bytes());
```

- [ ] **Step 5: Update redb deserializer to read v3 and v4**

Replace the current tag guard with:

```rust
    let tag = data[0];
    if tag != 3 && tag != 4 {
        return Err(MetaStoreError::IOError(format!(
            "unsupported FileMetadata serialization tag {tag}; expected 3 or 4"
        )));
    }
```

After `let link_target = read_opt_str(data, &mut pos).ok().flatten();`, add:

```rust
    let gen = if tag >= 4 && pos + 8 <= data.len() {
        let n = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
        pos += 8;
        n
    } else {
        0
    };
```

Add `gen,` to the returned `FileMetadata`.

- [ ] **Step 6: Update all kernel `FileMetadata` literals and helpers**

For every `FileMetadata` struct literal in `rust/kernel/src`, `rust/raft/src`, and Rust tests, add `gen: 0` unless the test is specifically asserting another value. Update `mk_meta` in `rust/kernel/src/core/meta_store/mod.rs` to return:

```rust
        FileMetadata {
            path: path.to_string(),
            size: 0,
            content_id: None,
            gen: 0,
            version,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
        }
```

- [ ] **Step 7: Verify redb serialization is GREEN**

Run:

```bash
cargo test -p kernel serialize_roundtrip_preserves_gen deserialize_v3_metadata_defaults_gen_to_zero
```

Expected: PASS.

- [ ] **Step 8: Commit kernel metadata storage**

Run:

```bash
git add rust/kernel/src/abc/meta_store.rs rust/kernel/src/core/meta_store/mod.rs rust/kernel/src rust/raft/src
git commit -m "feat: persist file metadata generation"
```

### Task 3: Kernel Generation Semantics

**Files:**
- Modify: `rust/kernel/src/kernel/mod.rs`
- Modify: `rust/kernel/src/kernel/io.rs`
- Modify: `rust/kernel/src/core/dispatch/mod.rs`
- Modify: `rust/kernel/src/generated_kernel_abi_pyo3.rs`
- Modify: `src/nexus/core/nexus_fs_content.py`
- Modify: `src/nexus/core/nexus_fs_metadata.py`

- [x] **Step 1: Write failing kernel generation behavior tests**

In `rust/kernel/src/kernel/mod.rs`, add these tests inside the main `#[cfg(test)] mod tests`:

```rust
#[test]
fn sys_write_increments_content_generation() {
    let k = Kernel::new();
    let ctx = OperationContext::new("test", "root", true, None, true);

    let first = k.sys_write("/gen.txt", &ctx, b"one", 0).unwrap();
    let second = k.sys_write("/gen.txt", &ctx, b"two", 0).unwrap();
    let stat = k.sys_stat("/gen.txt", "root").unwrap();

    assert_eq!(first.gen, 1);
    assert_eq!(second.gen, 2);
    assert_eq!(stat.gen, 2);
}

#[test]
fn sys_setattr_metadata_update_preserves_generation() {
    let k = Kernel::new();
    let ctx = OperationContext::new("test", "root", true, None, true);
    k.sys_write("/mime.txt", &ctx, b"body", 0).unwrap();

    k.sys_setattr(
        "/mime.txt",
        0,
        "",
        None,
        None,
        None,
        "memory",
        "root",
        false,
        0,
        None,
        None,
        Some("text/plain"),
        Some(1234),
        None,
        None,
        None,
    )
    .unwrap();

    let stat = k.sys_stat("/mime.txt", "root").unwrap();
    assert_eq!(stat.gen, 1);
}

#[test]
fn copy_uses_destination_generation() {
    let k = Kernel::new();
    let ctx = OperationContext::new("test", "root", true, None, true);
    k.sys_write("/src.txt", &ctx, b"body", 0).unwrap();

    let copied = k.sys_copy("/src.txt", "/dst.txt", &ctx).unwrap();
    let dst = k.sys_stat("/dst.txt", "root").unwrap();

    assert_eq!(copied.gen, 1);
    assert_eq!(dst.gen, 1);

    let copied_again = k.sys_copy("/src.txt", "/dst.txt", &ctx).unwrap();
    let dst_again = k.sys_stat("/dst.txt", "root").unwrap();

    assert_eq!(copied_again.gen, 2);
    assert_eq!(dst_again.gen, 2);
}

#[test]
fn batch_write_increments_each_path_generation() {
    let k = Kernel::new();
    let ctx = OperationContext::new("test", "root", true, None, true);

    let first = k
        ._write_batch(
            &[
                ("/a.txt".to_string(), b"a1".to_vec()),
                ("/b.txt".to_string(), b"b1".to_vec()),
            ],
            &ctx,
        )
        .unwrap();
    let second = k
        ._write_batch(&[("/a.txt".to_string(), b"a2".to_vec())], &ctx)
        .unwrap();

    assert_eq!(first[0].gen, 1);
    assert_eq!(first[1].gen, 1);
    assert_eq!(second[0].gen, 2);
    assert_eq!(k.sys_stat("/a.txt", "root").unwrap().gen, 2);
    assert_eq!(k.sys_stat("/b.txt", "root").unwrap().gen, 1);
}
```

- [x] **Step 2: Run the kernel behavior tests to verify RED**

Run:

```bash
cargo test -p kernel sys_write_increments_content_generation sys_setattr_metadata_update_preserves_generation copy_uses_destination_generation batch_write_increments_each_path_generation
```

Expected: FAIL because `SysWriteResult`, `SysCopyResult`, and `StatResult` do not expose `gen`, and write paths do not compute it.

- [x] **Step 3: Add generation fields to kernel result structs**

In `rust/kernel/src/kernel/mod.rs`, add:

```rust
    /// Content generation after this read.
    pub gen: u64,
```

to `SysReadResult`, add:

```rust
    /// Content generation after write.
    pub gen: u64,
```

to `SysWriteResult`, add:

```rust
    /// Destination content generation.
    pub gen: u64,
```

to `SysCopyResult`, and add:

```rust
    pub gen: u64,
```

to `StatResult`.

- [x] **Step 4: Change `build_metadata` to accept and store generation**

In `rust/kernel/src/kernel/mod.rs`, add `gen: u64` after `content_id: Option<String>` in `fn build_metadata`, and include:

```rust
            gen,
```

in the returned `FileMetadata`.

Update all `build_metadata` calls: use computed `new_gen` for content writes/copies and `0` for mkdir, parent directory creation, pipe, stream, mount, and link creation.

- [x] **Step 5: Compute generation in `sys_write`**

In `rust/kernel/src/kernel/io.rs`, after `old_version`, add:

```rust
                let old_gen = old_entry.as_ref().map(|e| e.gen).unwrap_or(0);
                let new_gen = old_gen.saturating_add(1);
```

In `rust/kernel/src/core/dispatch/mod.rs`, add `pub(crate) gen: Option<u64>` to `FileEvent`, initialize it to `None` in `FileEvent::new`, and add this to `to_json_map` after the existing `version` insertion:

```rust
        if let Some(v) = self.gen {
            map.insert("gen".to_string(), serde_json::Value::from(v));
        }
```

Pass `new_gen` into `build_metadata`, set `ev.gen = Some(new_gen)` inside the `FileEventType::FileWrite` mutation-dispatch closure in `sys_write`, and return:

```rust
                    gen: new_gen,
```

in every successful `SysWriteResult`. For miss and IPC short-circuit `SysWriteResult` values, set `gen: 0`.

- [x] **Step 6: Compute generation in batch write**

In `_write_batch`, after `old_version`, add:

```rust
                    let old_gen = batch_old_entry.as_ref().map(|e| e.gen).unwrap_or(0);
                    let new_gen = old_gen.saturating_add(1);
```

Pass `new_gen` to `build_metadata` and set `gen: new_gen` in the per-item `SysWriteResult`. Set `gen: 0` on all miss results.

- [x] **Step 7: Expose generation in read and stat**

In successful `SysReadResult` construction, set:

```rust
            gen: entry.gen,
```

In synthetic `StatResult` values, set `gen: 0`. In normal `sys_stat`, set:

```rust
            gen: entry.gen,
```

- [x] **Step 8: Compute destination generation in copy**

In `sys_copy`, load destination metadata before building the destination record:

```rust
        let old_dst_meta: Option<FileMetadata> = self
            .with_metastore_route(&dst_route, |ms| ms.get(dst_path).ok().flatten())
            .flatten();
        let new_gen = old_dst_meta
            .as_ref()
            .map(|m| m.gen)
            .unwrap_or(0)
            .saturating_add(1);
```

Use `new_version = old_dst_meta.as_ref().map(|m| m.version).unwrap_or(0) + 1`, pass `new_gen` into `build_metadata`, and return `gen: new_gen` in `SysCopyResult`.

- [x] **Step 9: Expose `gen` through PyO3 and Python wrappers**

In `rust/kernel/src/generated_kernel_abi_pyo3.rs`, add `pub gen: u64` to `PySysWriteResult`, set `gen: result.gen` in `sys_write` and `_write_batch`, and set:

```rust
                dict.set_item("gen", s.gen)?;
```

in `sys_stat`.

In `src/nexus/core/nexus_fs_content.py`, include `"gen": result.gen` in the write return dict and in read metadata results from `sys_stat`.

In `src/nexus/core/nexus_fs_metadata.py`, include `"gen": meta.gen` in Python fallback stat dictionaries built from `FileMetadata`.

- [x] **Step 10: Verify kernel generation behavior is GREEN**

Run:

```bash
cargo test -p kernel sys_write_increments_content_generation sys_setattr_metadata_update_preserves_generation copy_uses_destination_generation batch_write_increments_each_path_generation
```

Expected: PASS.

- [x] **Step 11: Commit kernel generation semantics**

Run:

```bash
git add rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs rust/kernel/src/core/dispatch/mod.rs rust/kernel/src/generated_kernel_abi_pyo3.rs src/nexus/core/nexus_fs_content.py src/nexus/core/nexus_fs_metadata.py
git commit -m "feat: bump file generation on content writes"
```

Implementation note: the committed tests use a small in-test root ObjectStore so the kernel syscalls exercise real write and copy paths instead of bare `Kernel::new()` route misses.

Verification evidence:

- `cargo test -p kernel --no-default-features generation` passed: 5 tests, 0 failures.
- `cargo check -p kernel` passed.
- `cargo fmt -p kernel --check` passed.
- `PATH=/Users/tafeng/.cache/pre-commit/repou6sx7beq/py_env-python3.14/bin:$PATH .venv/bin/python scripts/codegen_kernel_abi.py --check` passed.
- `.venv/bin/python -m py_compile src/nexus/core/nexus_fs_content.py src/nexus/core/nexus_fs_metadata.py` passed.
- `git diff --check` passed.

### Task 4: Raft, Remote Metastore, And Typed gRPC Exposure

**Files:**
- Modify: `proto/nexus/grpc/vfs/vfs.proto`
- Modify: `rust/kernel/build.rs`
- Modify: `rust/raft/src/zone_meta_store.rs`
- Modify: `rust/raft/src/zone_manager.rs`
- Modify: `rust/kernel/src/core/meta_store/remote.rs`
- Modify: `rust/transport/src/grpc.rs`
- Modify: `rust/kernel/src/rpc_transport.rs`

- [x] **Step 1: Write failing raft proto round-trip test**

In `rust/raft/src/zone_meta_store.rs`, extend `proto_roundtrip_preserves_kernel_fields`:

```rust
            gen: 17,
```

in the test `KernelFileMetadata`, and add:

```rust
        assert_eq!(restored.gen, meta.gen);
```

- [x] **Step 2: Run raft round-trip test to verify RED**

Run:

```bash
cargo test -p raft --features grpc proto_roundtrip_preserves_kernel_fields
```

Expected: FAIL because `gen` is not copied through `kernel_to_proto`/`proto_to_kernel`.

- [x] **Step 3: Add `gen` to typed VFS proto**

In `proto/nexus/grpc/vfs/vfs.proto`, add:

```proto
  uint64 gen = 6;
```

to `ReadResponse`, and add:

```proto
  uint64 gen = 5;
```

to `WriteResponse`.

- [x] **Step 4: Preserve `gen` through raft metadata conversion**

In `rust/raft/src/zone_meta_store.rs`, add to `proto_to_kernel`:

```rust
        gen: proto.gen,
```

and to `kernel_to_proto`:

```rust
        gen: meta.gen,
```

In `rust/raft/src/zone_manager.rs`, set `gen: 0` in federation mount helper proto literals.

- [x] **Step 5: Preserve `gen` through remote metastore JSON**

In `rust/kernel/src/core/meta_store/remote.rs`, add `"gen": metadata.gen` to the `put` JSON payload and add this to `parse_metadata_from_json`:

```rust
        gen: obj.get("gen").and_then(|v| v.as_u64()).unwrap_or(0),
```

- [x] **Step 6: Fill typed gRPC generation responses**

In `rust/transport/src/grpc.rs`, set `gen: result.gen` in successful `ReadResponse` and `WriteResponse`. Set `gen: 0` in `error_read`, `error_write`, and explicit error response literals.

In `rust/kernel/src/rpc_transport.rs`, add `pub gen: u64` to `ReadResult` and `WriteRpcResult`, and fill it from `inner.gen`.

- [x] **Step 7: Verify raft and transport compile**

Run:

```bash
cargo test -p raft@0.1.0 --features grpc proto_roundtrip_preserves_kernel_fields
cargo check -p transport
cargo check -p kernel --features python
```

Expected: all commands PASS.

- [x] **Step 8: Commit transport exposure**

Run:

```bash
git add proto/nexus/grpc/vfs/vfs.proto rust/kernel/build.rs rust/raft/src/zone_meta_store.rs rust/raft/src/zone_manager.rs rust/kernel/src/core/meta_store/remote.rs rust/transport/src/grpc.rs rust/kernel/src/rpc_transport.rs
git commit -m "feat: expose file generation over metadata transports"
```

Implementation note: raft metadata conversion and remote-metastore JSON preservation landed with Task 2. The Task 4 commit adds the typed VFS proto fields, fills transport/RPC response structs, and adds a kernel build-script `rerun-if-changed` for the external VFS proto.

Verification evidence:

- `cargo test -p raft@0.1.0 --features grpc proto_roundtrip_preserves_kernel_fields` passed.
- `cargo check -p transport` passed with existing dead-code warnings in `rust/transport/src/federation.rs`.
- `cargo check -p kernel --features python` passed.
- `.venv/bin/python -m pytest tests/unit/remote/test_rpc_transport.py -q` passed.
- `.venv/bin/python` descriptor check confirmed `ReadResponse.gen = 6` and `WriteResponse.gen = 5`.
- `.venv/bin/python -m py_compile src/nexus/remote/rpc_transport.py src/nexus/grpc/vfs/vfs_pb2.py src/nexus/grpc/vfs/vfs_pb2_grpc.py` passed.
- `git diff --check` passed.

### Task 5: FUSE Generation Cache And Xattr

**Files:**
- Modify: `nexus-fuse/src/client.rs`
- Modify: `nexus-fuse/src/cache.rs`
- Modify: `nexus-fuse/src/fs.rs`
- Modify: `nexus-fuse/tests/error_handling_test.rs`
- Modify: `src/nexus/fuse/rust_client.py`

- [x] **Step 1: Write failing FUSE client stat test**

In `nexus-fuse/tests/error_handling_test.rs`, update `test_successful_stat` response body to include `"gen":7`, and add:

```rust
    assert_eq!(meta.gen, 7);
```

- [x] **Step 2: Write failing cache generation tests**

In `nexus-fuse/src/cache.rs`, add:

```rust
#[test]
fn test_generation_mismatch_invalidates_cache() {
    let cache = test_cache("generation-mismatch");
    cache.put("/gen.txt", b"v1", Some("e1"), 1);

    assert!(matches!(cache.get("/gen.txt", 1), CacheLookup::Hit(_)));
    assert!(matches!(cache.get("/gen.txt", 2), CacheLookup::Miss));
    assert!(matches!(cache.get("/gen.txt", 2), CacheLookup::Miss));
}

#[test]
fn test_generation_stored_with_entry() {
    let cache = test_cache("generation-stored");
    cache.put("/gen.txt", b"v1", Some("e1"), 9);

    match cache.get("/gen.txt", 9) {
        CacheLookup::Hit(entry) => assert_eq!(entry.gen, 9),
        other => panic!("expected hit, got {other:?}"),
    }
}
```

- [x] **Step 3: Run FUSE tests to verify RED**

Run:

```bash
cd nexus-fuse && cargo test test_successful_stat
cd nexus-fuse && cargo test generation
```

Expected: FAIL because `FileMetadata`, `CacheEntry`, `get`, and `put` do not carry `gen`.

- [x] **Step 4: Add generation to FUSE client metadata**

In `nexus-fuse/src/client.rs`, add to `FileMetadata`:

```rust
    #[serde(default)]
    pub gen: u64,
```

In `src/nexus/fuse/rust_client.py`, add `gen: int = 0` to `FileMetadata` and set `gen=result.get("gen", 0)` in `RustFUSEClient.stat`.

- [x] **Step 5: Add generation column to FUSE cache**

In `nexus-fuse/src/cache.rs`, change schema creation for `file_cache` to include:

```sql
                gen INTEGER NOT NULL DEFAULT 0,
```

After table creation, run this migration:

```rust
        let _ = conn.execute("ALTER TABLE file_cache ADD COLUMN gen INTEGER NOT NULL DEFAULT 0", []);
```

Change `CacheEntry` to:

```rust
pub struct CacheEntry {
    pub content: Vec<u8>,
    pub etag: Option<String>,
    pub gen: u64,
}
```

Change `pub fn get(&self, path: &str) -> CacheLookup` to `pub fn get(&self, path: &str, gen: u64) -> CacheLookup`, select `etag, cached_at, gen`, and if `cached_gen != gen`, delete the row and return `CacheLookup::Miss`.

Change `pub fn put(&self, path: &str, content: &[u8], etag: Option<&str>)` to `pub fn put(&self, path: &str, content: &[u8], etag: Option<&str>, gen: u64)`, and insert the `gen` column.

- [x] **Step 6: Thread generation through FUSE reads**

In `nexus-fuse/src/fs.rs`, change `read_cached(&self, path: &str)` to:

```rust
    fn read_cached(&self, path: &str, gen: u64) -> anyhow::Result<(Vec<u8>, Option<String>)>
```

Use `cache.get(path, gen)` and `cache.put(path, &content, etag.as_deref(), gen)`.

Before calling `read_cached` in `read` and partial-write read-modify-write paths, call:

```rust
        let gen = self.client.stat(&path).map(|m| m.gen).unwrap_or(0);
```

and pass `gen` into `read_cached`.

- [x] **Step 7: Add read-only `user.nexus.gen` xattr**

In `nexus-fuse/src/fs.rs`, update imports:

```rust
use fuser::{
    FileAttr, FileType, Filesystem, ReplyAttr, ReplyData, ReplyDirectory, ReplyEntry, ReplyWrite,
    ReplyXattr, Request, FUSE_ROOT_ID,
};
use libc::{EIO, EISDIR, ENOENT, ENOTDIR, ENOTEMPTY, ENODATA, ERANGE, EROFS};
use std::ffi::{OsStr, OsString};
```

Add:

```rust
const XATTR_GEN: &str = "user.nexus.gen";
```

Implement in `impl Filesystem for NexusFs`:

```rust
    fn getxattr(
        &mut self,
        _req: &Request,
        ino: u64,
        name: &OsStr,
        size: u32,
        reply: ReplyXattr,
    ) {
        let path = resolve_path!(self, ino, reply);
        if name != OsStr::new(XATTR_GEN) {
            reply.error(ENODATA);
            return;
        }
        match self.client.stat(&path) {
            Ok(meta) => {
                let value = meta.gen.to_string();
                let bytes = value.as_bytes();
                if size == 0 {
                    reply.size(bytes.len() as u32);
                } else if (size as usize) < bytes.len() {
                    reply.error(ERANGE);
                } else {
                    reply.data(bytes);
                }
            }
            Err(e) => {
                error!("getxattr stat error for {}: {}", path, e);
                reply.error(EIO);
            }
        }
    }

    fn listxattr(&mut self, _req: &Request, ino: u64, size: u32, reply: ReplyXattr) {
        let _path = resolve_path!(self, ino, reply);
        let mut names = OsString::from(XATTR_GEN);
        names.push("\0");
        let bytes = names.as_encoded_bytes();
        if size == 0 {
            reply.size(bytes.len() as u32);
        } else if (size as usize) < bytes.len() {
            reply.error(ERANGE);
        } else {
            reply.data(bytes);
        }
    }

    fn setxattr(
        &mut self,
        _req: &Request,
        ino: u64,
        name: &OsStr,
        _value: &[u8],
        _flags: i32,
        _position: u32,
        reply: fuser::ReplyEmpty,
    ) {
        let _path = resolve_path!(self, ino, reply);
        if name == OsStr::new(XATTR_GEN) {
            reply.error(EROFS);
        } else {
            reply.error(ENODATA);
        }
    }
```

- [x] **Step 8: Verify daemon stat returns generation through serde**

Read `nexus-fuse/src/daemon.rs` and confirm `handle_stat` uses `serde_json::to_value(metadata)`. No code change is needed in that file because `FileMetadata` derives `Serialize`.

- [x] **Step 9: Verify FUSE generation tests are GREEN**

Run:

```bash
cd nexus-fuse && cargo test test_successful_stat
cd nexus-fuse && cargo test --lib
```

Expected: PASS.

- [ ] **Step 10: Commit FUSE generation support**

Run:

```bash
git add nexus-fuse/src/client.rs nexus-fuse/src/cache.rs nexus-fuse/src/fs.rs nexus-fuse/tests/error_handling_test.rs src/nexus/fuse/rust_client.py
git commit -m "feat: use file generation in fuse cache"
```

Implementation note: `nexus-fuse/src/daemon.rs::handle_stat` already serializes the full `FileMetadata` with `serde_json::to_value(metadata)`, so adding `gen` to the serializable struct is enough for daemon IPC stat responses.

Verification evidence:

- RED: `cd nexus-fuse && cargo test test_successful_stat` failed because `FileMetadata` had no `gen` field.
- RED: `cd nexus-fuse && cargo test generation` failed because `FileCache::get`/`put` had no generation argument and `CacheEntry` had no `gen` field.
- `cd nexus-fuse && cargo test test_successful_stat` passed.
- `cd nexus-fuse && cargo test --lib` passed: 31 tests.
- `cd nexus-fuse && cargo check` passed with existing warnings in `daemon.rs` and `fs.rs`.
- `.venv/bin/python -m py_compile src/nexus/fuse/rust_client.py` passed.
- `git diff --check` passed.

### Task 6: Full Verification And Cleanup

**Files:**
- Review all files changed by Tasks 1-5.

- [x] **Step 1: Run focused Rust kernel tests**

Run:

```bash
cargo test -p kernel --no-default-features gen
cargo test -p kernel --no-default-features copy_
```

Expected: PASS.

- [x] **Step 2: Run kernel Python-feature compile check**

Run:

```bash
cargo check -p kernel --features python
```

Expected: PASS.

- [x] **Step 3: Run raft metadata tests**

Run:

```bash
cargo test -p raft@0.1.0 --features grpc proto_roundtrip_preserves_kernel_fields
```

Expected: PASS.

- [x] **Step 4: Run transport compile check**

Run:

```bash
cargo check -p transport
```

Expected: PASS.

- [x] **Step 5: Run Python metadata tests**

Run:

```bash
pytest tests/unit/storage/test_metadata_generation.py tests/unit/core/test_file_events.py tests/unit/remote/test_rpc_transport.py -q
```

Expected: PASS.

- [x] **Step 6: Run FUSE focused tests**

Run:

```bash
cd nexus-fuse && cargo test test_successful_stat
cd nexus-fuse && cargo test --lib
cd nexus-fuse && cargo check
```

Expected: PASS.

- [x] **Step 7: Run formatting checks**

Run:

```bash
cargo fmt --check
cd nexus-fuse && cargo fmt --check
ruff format --check scripts/gen_metadata.py src/nexus/core/nexus_fs_content.py src/nexus/core/nexus_fs_metadata.py src/nexus/fuse/rust_client.py tests/unit/storage/test_metadata_generation.py
```

Expected: PASS.

- [x] **Step 8: Inspect the final diff**

Run:

```bash
git diff --stat HEAD
git diff --check
```

Expected: changed files match the file structure above, and `git diff --check` prints no whitespace errors.

- [ ] **Step 9: Commit final verification fixes when the working tree changed**

If Step 7 or Step 8 changed files, run:

```bash
git add .
git commit -m "chore: finish file generation verification"
```

Expected: a small cleanup commit is created. If `git status --short` is empty, skip this step.

Verification evidence:

- `cargo test -p kernel --no-default-features gen` passed: 34 tests.
- `cargo test -p kernel --no-default-features copy_` passed: 4 tests.
- `cargo check -p kernel --features python` passed.
- `cargo test -p raft@0.1.0 --features grpc proto_roundtrip_preserves_kernel_fields` passed after clearing generated incremental build artifacts; initial run failed with `No space left on device`.
- `cargo check -p transport` passed with existing dead-code warnings in `rust/transport/src/federation.rs`.
- `.venv/bin/python -m pytest tests/unit/storage/test_metadata_generation.py tests/unit/core/test_file_events.py tests/unit/remote/test_rpc_transport.py -q` passed: 29 tests.
- `cd nexus-fuse && cargo test test_successful_stat && cargo test --lib && cargo check` passed with existing warnings in `nexus-fuse/src/daemon.rs`, `nexus-fuse/src/fs.rs`, and `nexus-fuse/tests/integration_test.rs`.
- `cargo fmt -p kernel --check`, `cargo fmt -p transport --check`, and `cargo fmt --manifest-path rust/raft/Cargo.toml --check` passed after formatting `rust/transport/src/grpc.rs`.
- `/Users/tafeng/.cache/pre-commit/repou6sx7beq/py_env-python3.14/bin/ruff format --check scripts/gen_metadata.py src/nexus/core/nexus_fs_content.py src/nexus/core/nexus_fs_metadata.py src/nexus/fuse/rust_client.py tests/unit/storage/test_metadata_generation.py` passed.
- Repo-wide `cargo fmt --check` and `cd nexus-fuse && cargo fmt --check` still fail on pre-existing unrelated formatting drift outside this feature scope; not swept into this branch.
- `git diff --stat develop..HEAD`, `git diff --check develop..HEAD`, and `git diff --check` passed for the branch/current worktree.
