# CasTransport Trait Extraction (cas-1, #4264) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **⚠️ Deferred-work gate (read first).** Issue #4264 is **Approach 2** of epic #4259 and was explicitly deferred by the assignee on 2026-06-01. Deferral had two gates: (1) Approach 1 (bridge-1..3, #4261/#4262/#4263) lands — **now true, all closed**; (2) dedup-in-Rust becomes a *concrete requirement* — **not yet confirmed**. This refactor is the prerequisite for cas-2 (#4265, `S3CasTransport`). Do **not** start execution until gate 2 is confirmed. This plan exists so the work is ready the moment it is.

**Goal:** Extract a `CasTransport` trait from `LocalCASTransport`'s blob surface and make `CASEngine` drive any `CasTransport` implementation — a pure, behavior-preserving refactor whose regression guard is the existing CAS test suite.

**Architecture:** Trait-object, **not** generics. `CASEngine` holds `Arc<dyn CasTransport>`; the engine stays a single non-generic type. This is forced by two existing seams: (a) `ObjectStore::as_cas(&self) -> Option<&CASEngine>` is one shared signature across all backends — a generic `CASEngine<T>` would pin it to a single `T`, blocking cas-2's S3-backed engine from being returned through it; (b) the `ChunkingStrategy` / `ChunkAssembler` DI traits are object-safe (`Arc<dyn ...>`), so their methods must accept `&dyn CasTransport`, never a generic. `LocalCASTransport` becomes the first impl; `S3CasTransport` (cas-2) is the second.

**Tech Stack:** Rust (crates `kernel`, `backends`), `std::io`, `Arc<dyn _>` trait objects, BLAKE3 via `lib::hash`, `cargo test` + `cargo clippy -- -D warnings` (CI lint gate is per-crate).

---

## Scope note

Single subsystem (kernel CAS + its two consumer crates). One plan is correct — do not split.

## Discrepancy with the issue text

The issue lists the trait surface as "read_blob, write_blob[_tracked], exists, remove_blob/remove_meta, **get_mtime, list**". `get_mtime` and `list` **do not exist** on `LocalCASTransport` and are not called by `CASEngine` or the chunking layer (verified by grep). Per YAGNI they are **not** added — inventing unused trait methods would break "implements it unchanged" and add dead surface. The trait covers exactly the 11 methods the engine + chunking actually invoke.

## File Structure

All production changes live in the `kernel` crate's CAS module; the `backends` crate needs only test-import touch-ups.

- **Modify** `rust/kernel/src/core/cas/transport.rs` — add `pub trait CasTransport`; move the 11 blob-surface methods from `impl LocalCASTransport` into `impl CasTransport for LocalCASTransport`. Keep `new`, `resolve`, `ensure_parent`, `blob_path`, `root` inherent (local-only / private helpers, never reached through `&dyn`).
- **Modify** `rust/kernel/src/core/cas/chunking.rs` — change all 5 `transport: &LocalCASTransport` parameters to `&dyn CasTransport`; swap the import.
- **Modify** `rust/kernel/src/core/cas/engine.rs` — field `transport: LocalCASTransport` → `Arc<dyn CasTransport>`; constructors accept `impl CasTransport + 'static`; `transport()` returns `&dyn CasTransport`; pass `self.transport.as_ref()` to chunking helpers.
- **Modify** `rust/backends/src/transports/api/ai/openai/mod.rs` and `.../anthropic/mod.rs` — add `use kernel::cas_transport::CasTransport;` to the `#[cfg(test)] mod tests` block so `engine.transport().read_blob(...)` (now a trait method on `&dyn CasTransport`) resolves.
- **Untouched** (verified): `remote.rs` (no transport coupling), `cas_local.rs` (`CASEngine::new(transport)` still type-checks — `LocalCASTransport: CasTransport + 'static`), `abc/object_store.rs` (`as_cas` signature unchanged), `vfs_router.rs`, `lib.rs` re-exports (`kernel::cas_transport::CasTransport` is exposed automatically once the trait is `pub`).

## The 11 trait methods (union of what engine.rs + chunking.rs invoke on the transport)

`read_blob`, `write_blob`, `write_blob_tracked`, `write_blob_with_hash`, `exists`, `blob_size`, `read_meta`, `write_meta`, `meta_exists`, `remove_blob`, `remove_meta`.

## Compile invariant

Rust compiles the whole crate, so each task must leave the workspace **building and green**. The task order (trait → chunking → engine → backend tests) is chosen so every intermediate state compiles via automatic `&LocalCASTransport → &dyn CasTransport` unsized coercion.

---

### Task 1: Define `CasTransport` and implement it for `LocalCASTransport`

**Files:**
- Modify: `rust/kernel/src/core/cas/transport.rs`
- Modify: `rust/kernel/src/core/cas/engine.rs:22` (import only)
- Modify: `rust/kernel/src/core/cas/chunking.rs:20` (import only)
- Test: `rust/kernel/src/core/cas/transport.rs` (new test in existing `mod tests`)

- [ ] **Step 1: Write the failing test**

Add to `mod tests` in `transport.rs` (the `setup()` helper returns `(TempDir, LocalCASTransport)` and `use super::*` is already present, which will bring the new trait into scope):

```rust
#[test]
fn test_dyn_cas_transport_roundtrip() {
    let (_tmp, transport) = setup();
    let t: &dyn CasTransport = &transport;
    let hash = t.write_blob(b"via trait object").unwrap();
    assert!(t.exists(&hash));
    assert_eq!(t.read_blob(&hash).unwrap(), b"via trait object");
    let (h2, is_new) = t.write_blob_tracked(b"via trait object").unwrap();
    assert_eq!(h2, hash);
    assert!(!is_new); // dedup hit
    assert_eq!(t.blob_size(&hash).unwrap(), 16);
    t.write_meta(&hash, b"{}").unwrap();
    assert!(t.meta_exists(&hash));
    t.remove_meta(&hash).unwrap();
    t.remove_blob(&hash).unwrap();
    assert!(!t.exists(&hash));
}
```

- [ ] **Step 2: Run the test to verify it fails (does not compile)**

Run: `cargo test -p kernel cas::transport::tests::test_dyn_cas_transport_roundtrip`
Expected: FAIL — `error[E0405]: cannot find trait `CasTransport` in this scope`.

- [ ] **Step 3: Define the trait**

In `transport.rs`, immediately after the `blob_key()` function (after line 28, before the `LocalCASTransport` struct), insert:

```rust
/// Blob-storage surface required by `CASEngine` and the CDC chunking layer.
///
/// Extracted from `LocalCASTransport` (#4264) so the engine is storage-agnostic:
/// any backend that can store/fetch/delete content-addressed blobs (plus their
/// `.meta` sidecars) can drive the full CAS engine — hashing, dedup, CDC
/// chunking, scatter-gather — without re-implementing it. `LocalCASTransport`
/// is the first impl; `S3CasTransport` (cas-2, #4265) is next.
///
/// Object-safe: stored behind `Arc<dyn CasTransport>` inside `CASEngine` and
/// passed as `&dyn CasTransport` into the object-safe `ChunkingStrategy` /
/// `ChunkAssembler` DI traits (which cannot take a generic transport).
/// `Send + Sync` because `CASEngine` is shared across threads via `Arc`.
pub trait CasTransport: Send + Sync {
    /// Read a CAS blob by content hash. `io::ErrorKind::NotFound` if absent.
    fn read_blob(&self, content_id: &str) -> io::Result<Vec<u8>>;

    /// Write content, returning its BLAKE3 hash. Dedup: an existing blob is a no-op.
    fn write_blob(&self, content: &[u8]) -> io::Result<String>;

    /// Like `write_blob` but also reports whether storage was touched (`true`)
    /// or the write hit CAS dedup (`false`).
    fn write_blob_tracked(&self, content: &[u8]) -> io::Result<(String, bool)>;

    /// Write a pre-hashed blob. `true` if written, `false` if it already
    /// existed (dedup). Used by scatter-gather chunk write-back.
    fn write_blob_with_hash(&self, content: &[u8], content_id: &str) -> io::Result<bool>;

    /// Does a CAS blob exist for this hash?
    fn exists(&self, content_id: &str) -> bool;

    /// Size in bytes of a CAS blob. `io::ErrorKind::NotFound` if absent.
    fn blob_size(&self, content_id: &str) -> io::Result<u64>;

    /// Read the `.meta` JSON sidecar. `io::ErrorKind::NotFound` if absent.
    fn read_meta(&self, content_id: &str) -> io::Result<Vec<u8>>;

    /// Write the `.meta` JSON sidecar.
    fn write_meta(&self, content_id: &str, meta: &[u8]) -> io::Result<()>;

    /// Cheap existence check for the `.meta` sidecar.
    fn meta_exists(&self, content_id: &str) -> bool;

    /// Remove a CAS blob. `io::ErrorKind::NotFound` if absent.
    fn remove_blob(&self, content_id: &str) -> io::Result<()>;

    /// Remove the `.meta` sidecar. Absorbs `NotFound` (best-effort).
    fn remove_meta(&self, content_id: &str) -> io::Result<()>;
}
```

- [ ] **Step 4: Move the 11 methods into a `CasTransport` impl (bodies unchanged)**

This is a **cut-and-paste move, changing no method body** — that is what guarantees "implements it unchanged in behavior."

1. From `impl LocalCASTransport` (currently lines 44–261), **cut** these 11 methods *verbatim*: `read_blob` (95–99), `write_blob` (107–109), `write_blob_tracked` (115–143), `write_blob_with_hash` (150–174), `exists` (179–183), `blob_size` (193–197), `write_meta` (203–217), `read_meta` (224–228), `meta_exists` (232–236), `remove_blob` (250–254), `remove_meta` (240–248).
2. **Leave inherent** in `impl LocalCASTransport`: `new` (49–56), `resolve` (60–62), `ensure_parent` (68–90), `blob_path` (187–190), `root` (258–260). Keep their existing `#[allow(dead_code)]` attributes on `blob_path`/`root`.
3. **Paste** the 11 cut methods into a new block placed right after the `impl LocalCASTransport` block closes:

```rust
impl CasTransport for LocalCASTransport {
    // ── the 11 methods, pasted verbatim from the inherent impl ──
    // read_blob, write_blob, write_blob_tracked, write_blob_with_hash,
    // exists, blob_size, write_meta, read_meta, meta_exists,
    // remove_blob, remove_meta
}
```

Notes that keep this correct:
- `write_blob`'s body calls `self.write_blob_tracked(...)` — both are now trait methods on the same impl; resolves fine (trait in scope).
- Trait-impl methods still call the private inherent helpers `self.resolve(...)` / `self.ensure_parent(...)` — legal, same module, same concrete type.
- Drop the `pub` keyword from each moved method signature (trait methods are not `pub`); keep everything else (signatures, doc comments, bodies) identical.

- [ ] **Step 5: Bring the trait into scope for both consumers (imports only — no other change yet)**

The 11 methods are now trait methods, so any call on a concrete `&LocalCASTransport` (which both `engine.rs` and `chunking.rs` still do at this point) requires `CasTransport` in scope. Add it to both, keeping `LocalCASTransport` (still named in both files until Tasks 2/3) so the lib build stays warning-clean:

- `engine.rs:22` — change `use super::transport::LocalCASTransport;` to:
  ```rust
  use super::transport::{CasTransport, LocalCASTransport};
  ```
- `chunking.rs:20` — change `use super::transport::LocalCASTransport;` to:
  ```rust
  use super::transport::{CasTransport, LocalCASTransport};
  ```

Task 2 drops `LocalCASTransport` from `chunking.rs`, and Task 3 gates it behind `#[cfg(test)]` in `engine.rs`, once each file stops naming the concrete type in its lib build.

- [ ] **Step 6: Run the new test + full transport suite + clippy**

Run: `cargo test -p kernel cas::transport`
Expected: PASS — all pre-existing `transport::tests::*` (25 tests) plus `test_dyn_cas_transport_roundtrip`.

Run: `cd rust/kernel && cargo clippy -- -D warnings && cd "$OLDPWD"`
Expected: no warnings (matches CI `lint.yml`).

- [ ] **Step 7: Commit**

```bash
git add rust/kernel/src/core/cas/transport.rs rust/kernel/src/core/cas/engine.rs rust/kernel/src/core/cas/chunking.rs
git commit -m "refactor(cas): extract CasTransport trait; LocalCASTransport impls it (#4264)"
```

---

### Task 2: Switch `chunking.rs` to `&dyn CasTransport`

**Files:**
- Modify: `rust/kernel/src/core/cas/chunking.rs`

- [ ] **Step 1: Widen the 5 transport parameters to trait objects**

In `chunking.rs`, change every `transport: &LocalCASTransport` to `transport: &dyn CasTransport`. The 5 sites:
- `ChunkAssembler::try_reassemble` trait method (line ~54) and its impl on `ChunkedManifestAssembler` (line ~75)
- `read_and_verify_chunk` free fn (line ~126)
- `reassemble_chunks` free fn (line ~181)
- `ChunkingStrategy::write_chunked` trait method (line ~268) and its two impls — `FastCDCStrategy` (line ~361) and `MessageBoundaryStrategy` (line ~455)
- `finalize_manifest` free fn (line ~303)

No method bodies change — every call inside (`transport.read_blob(...)`, `transport.write_blob(...)`, `transport.write_blob_with_hash(...)`, `transport.write_blob_tracked(...)`, `transport.write_meta(...)`) is already a `CasTransport` method and dispatches through the trait object unchanged.

- [ ] **Step 2: Drop the now-unused concrete import**

Change `chunking.rs:20` from `use super::transport::{CasTransport, LocalCASTransport};` to:

```rust
use super::transport::CasTransport;
```

(`LocalCASTransport` is no longer named anywhere in `chunking.rs` after Step 1 — leaving it would fail `-D warnings`.)

- [ ] **Step 3: Verify the crate still compiles (engine coerces automatically)**

`engine.rs` still holds a concrete `LocalCASTransport` field and passes `&self.transport` (type `&LocalCASTransport`) into these helpers; `&LocalCASTransport` unsized-coerces to `&dyn CasTransport` automatically, so no engine edit is needed yet.

Run: `cargo build -p kernel`
Expected: builds clean.

- [ ] **Step 4: Run the CDC/chunking regression tests + clippy**

Run: `cargo test -p kernel cas::engine`
Expected: PASS — the chunked-path guards stay green: `test_is_chunked_*`, `test_get_size_*`, `test_delete_chunked_*`, `test_read_chunked_range_*`, `test_write_chunked_partial_*`, `test_read_content_scatter_gather_*`.

Run: `cd rust/kernel && cargo clippy -- -D warnings && cd "$OLDPWD"`
Expected: no warnings.

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/core/cas/chunking.rs
git commit -m "refactor(cas): chunking layer takes &dyn CasTransport (#4264)"
```

---

### Task 3: Make `CASEngine` hold `Arc<dyn CasTransport>`

**Files:**
- Modify: `rust/kernel/src/core/cas/engine.rs`
- Test: `rust/kernel/src/core/cas/engine.rs` (new `MemoryCasTransport` test double + 2 tests in existing `mod tests`)

- [ ] **Step 1: Write the failing test (the acceptance proof for "transport-generic")**

Add to `mod tests` in `engine.rs`. The test double lives in the test module so it is never linted by the per-crate `cargo clippy` (which checks lib/bins, not `#[cfg(test)]`):

```rust
/// In-memory `CasTransport` double — proves `CASEngine` drives a backend with
/// zero filesystem involvement (the cas-2 / S3 shape, exercised cheaply).
#[derive(Default)]
struct MemoryCasTransport {
    blobs: std::sync::Mutex<std::collections::HashMap<String, Vec<u8>>>,
    metas: std::sync::Mutex<std::collections::HashMap<String, Vec<u8>>>,
}

impl CasTransport for MemoryCasTransport {
    fn read_blob(&self, content_id: &str) -> io::Result<Vec<u8>> {
        self.blobs.lock().unwrap().get(content_id).cloned()
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
    }
    fn write_blob(&self, content: &[u8]) -> io::Result<String> {
        Ok(self.write_blob_tracked(content)?.0)
    }
    fn write_blob_tracked(&self, content: &[u8]) -> io::Result<(String, bool)> {
        let hash = lib::hash::hash_content(content);
        let mut b = self.blobs.lock().unwrap();
        if b.contains_key(&hash) {
            return Ok((hash, false));
        }
        b.insert(hash.clone(), content.to_vec());
        Ok((hash, true))
    }
    fn write_blob_with_hash(&self, content: &[u8], content_id: &str) -> io::Result<bool> {
        let mut b = self.blobs.lock().unwrap();
        if b.contains_key(content_id) {
            return Ok(false);
        }
        b.insert(content_id.to_string(), content.to_vec());
        Ok(true)
    }
    fn exists(&self, content_id: &str) -> bool {
        self.blobs.lock().unwrap().contains_key(content_id)
    }
    fn blob_size(&self, content_id: &str) -> io::Result<u64> {
        self.blobs.lock().unwrap().get(content_id).map(|v| v.len() as u64)
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
    }
    fn read_meta(&self, content_id: &str) -> io::Result<Vec<u8>> {
        self.metas.lock().unwrap().get(content_id).cloned()
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
    }
    fn write_meta(&self, content_id: &str, meta: &[u8]) -> io::Result<()> {
        self.metas.lock().unwrap().insert(content_id.to_string(), meta.to_vec());
        Ok(())
    }
    fn meta_exists(&self, content_id: &str) -> bool {
        self.metas.lock().unwrap().contains_key(content_id)
    }
    fn remove_blob(&self, content_id: &str) -> io::Result<()> {
        self.blobs.lock().unwrap().remove(content_id).map(|_| ())
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
    }
    fn remove_meta(&self, content_id: &str) -> io::Result<()> {
        self.metas.lock().unwrap().remove(content_id);
        Ok(())
    }
}

#[test]
fn test_engine_over_memory_transport_plain_blob() {
    let engine = CASEngine::new(MemoryCasTransport::default());
    let content = b"engine over a non-local transport";
    let hash = engine.write_content(content).unwrap();
    assert_eq!(hash.len(), 64);
    assert!(engine.content_exists(&hash));
    assert_eq!(engine.read_content(&hash).unwrap(), content);
    engine.delete_content(&hash).unwrap();
    assert!(!engine.content_exists(&hash));
}

#[test]
fn test_engine_over_memory_transport_chunked() {
    // Drives the full CDC chunked write+read path through &dyn CasTransport
    // against a non-local backend.
    let engine = CASEngine::with_strategy(
        MemoryCasTransport::default(),
        Arc::new(super::super::chunking::MessageBoundaryStrategy),
    );
    let content = sample_conversation();
    let manifest_hash = engine.write_content(&content).unwrap();
    assert!(engine.is_chunked(&manifest_hash));

    let parsed: Value = serde_json::from_slice(&content).unwrap();
    let mut concat: Vec<u8> = Vec::new();
    for msg in parsed.as_array().unwrap() {
        concat.extend_from_slice(&serde_json::to_vec(msg).unwrap());
    }
    assert_eq!(engine.read_content(&manifest_hash).unwrap(), concat);
}
```

- [ ] **Step 2: Run the test to verify it fails (does not compile)**

Run: `cargo test -p kernel cas::engine::tests::test_engine_over_memory_transport_plain_blob`
Expected: FAIL — `CASEngine::new` currently takes `LocalCASTransport`, so passing `MemoryCasTransport` is `error[E0308]: mismatched types`.

- [ ] **Step 3: Change the field type**

In `engine.rs`, change the struct field (line 75) from:

```rust
    transport: LocalCASTransport,
```
to:
```rust
    transport: Arc<dyn CasTransport>,
```

(`Arc` is already imported at line 15: `use std::sync::{Arc, RwLock};`.)

- [ ] **Step 4: Make the constructors accept any `CasTransport`**

Change `new` (line 93) and `with_strategy` (line 111) signatures + the field initializer:

```rust
pub fn new(transport: impl CasTransport + 'static) -> Self {
    Self {
        transport: Arc::new(transport),
        chunk_assembler: Some(super::chunking::default_chunk_assembler()),
        chunking_strategy: Some(super::chunking::default_chunking_strategy()),
        fetcher: None,
        bloom: RwLock::new(Self::new_bloom()),
    }
}
```

```rust
pub fn with_strategy(
    transport: impl CasTransport + 'static,
    strategy: Arc<dyn ChunkingStrategy>,
) -> Self {
    Self {
        transport: Arc::new(transport),
        chunk_assembler: Some(super::chunking::default_chunk_assembler()),
        chunking_strategy: Some(strategy),
        fetcher: None,
        bloom: RwLock::new(Self::new_bloom()),
    }
}
```

Existing callers — `CASEngine::new(transport)` in `cas_local.rs` and `CASEngine::with_strategy(transport, ...)` in the openai/anthropic backends — keep compiling unchanged because `LocalCASTransport: CasTransport + 'static`.

- [ ] **Step 5: Update the `transport()` accessor return type**

Change (lines 229–233):

```rust
    /// Expose the transport for direct blob access (read-back in tests,
    /// pre-hashed writes). Returns a trait object — concrete-only methods
    /// (`blob_path`, `root`) are intentionally not reachable here.
    pub fn transport(&self) -> &dyn CasTransport {
        self.transport.as_ref()
    }
```

- [ ] **Step 6: Pass `&dyn` (not `&Arc<dyn>`) into the chunking helpers**

`self.transport` is now `Arc<dyn CasTransport>`. Direct method calls (`self.transport.read_blob(...)`, `.write_blob(...)`, `.exists(...)`, `.blob_size(...)`, `.meta_exists(...)`, `.read_meta(...)`, `.remove_blob(...)`, `.remove_meta(...)`) are unchanged — `Arc<dyn _>` auto-derefs. Only the **5 call sites that pass the transport as an argument** need `&self.transport` → `self.transport.as_ref()`:
- line 159: `assembler.try_reassemble(&data, self.transport.as_ref(), fetcher, origins)`
- line 181: `strategy.write_chunked(content, self.transport.as_ref())`
- line 382: `read_and_verify_chunk(self.transport.as_ref(), hash, fetcher, origins)`
- line 563: `read_and_verify_chunk(self.transport.as_ref(), hash, fetcher, origins)`
- line 615: `finalize_manifest(all_chunks, chunk_count, total_size as usize, String::new(), self.transport.as_ref())`

- [ ] **Step 7: Refine the imports for a warning-clean lib build**

In `engine.rs:22` set the imports to (the field/sigs now name only `CasTransport` in the lib build; `LocalCASTransport` survives only in tests):

```rust
use super::transport::CasTransport;
#[cfg(test)]
use super::transport::LocalCASTransport;
```

- [ ] **Step 8: Run the new tests, the full kernel CAS suite, and clippy**

Run: `cargo test -p kernel cas`
Expected: PASS — all `transport::tests::*`, all `engine::tests::*` (including the 2 new memory-transport tests), unchanged.

Run: `cd rust/kernel && cargo clippy -- -D warnings && cd "$OLDPWD"`
Expected: no warnings (watch specifically for unused-import on `LocalCASTransport`).

- [ ] **Step 9: Commit**

```bash
git add rust/kernel/src/core/cas/engine.rs
git commit -m "refactor(cas): CASEngine drives Arc<dyn CasTransport> (#4264)"
```

---

### Task 4: Fix `backends` test imports and run the full downstream suite

**Files:**
- Modify: `rust/backends/src/transports/api/ai/openai/mod.rs` (`mod tests`)
- Modify: `rust/backends/src/transports/api/ai/anthropic/mod.rs` (`mod tests`)

- [ ] **Step 1: Confirm the breakage**

Run: `cargo test -p backends`
Expected: FAIL to compile — `b.engine.transport().read_blob(...)` (openai/mod.rs:216,218 and anthropic/mod.rs:216,218) now calls a `CasTransport` trait method on `&dyn CasTransport`; without the trait in scope: `error[E0599]: no method named `read_blob``.

- [ ] **Step 2: Bring the trait into scope in both test modules**

In the `#[cfg(test)] mod tests { ... }` block of **each** file, add as the first `use`:

```rust
    use kernel::cas_transport::CasTransport;
```

(`kernel::cas_transport::CasTransport` is exported automatically — `lib.rs:73` re-exports the module and the trait is `pub`. No `lib.rs` change.)

- [ ] **Step 3: Run the backends suite + clippy**

Run: `cargo test -p backends`
Expected: PASS — `storage::cas_local::tests::*` (CAS + path + connector + partial-write), `transports::api::ai::openai::*`, `transports::api::ai::anthropic::*` all green.

Run: `cd rust/backends && cargo clippy -- -D warnings && cd "$OLDPWD"`
Expected: no warnings.

- [ ] **Step 4: Commit**

```bash
git add rust/backends/src/transports/api/ai/openai/mod.rs rust/backends/src/transports/api/ai/anthropic/mod.rs
git commit -m "test(backends): import CasTransport for engine.transport() trait calls (#4264)"
```

---

### Task 5: Whole-workspace verification + perf spot-check (acceptance gate)

**Files:** none (verification only).

- [ ] **Step 1: Full kernel + backends test run**

Run: `cargo test -p kernel -p backends`
Expected: PASS, zero failures, zero ignored CAS tests.

- [ ] **Step 2: Lint gate exactly as CI runs it**

Run: `cd rust/kernel && cargo clippy -- -D warnings && cd "$OLDPWD" && cd rust/backends && cargo clippy -- -D warnings && cd "$OLDPWD"`
Expected: no warnings in either crate.

- [ ] **Step 3: Perf spot-check (hot path)**

There is **no CAS-specific bench** (`rust/kernel/benches/` holds only `read_batch`, `syscall_bench`, `readdir_bench`). The local read/write hot path gains exactly one vtable indirection per transport op (`Arc<dyn>` dispatch), which is dominated by the underlying `std::fs` syscall by orders of magnitude — no measurable regression is expected. Confirm the existing benches still build and run:

Run: `cargo bench -p kernel --no-run`
Expected: benches compile. (Full `cargo bench` run is optional; record numbers only if gate 2 reviewers ask for them.)

- [ ] **Step 4: Acceptance-criteria checklist (from #4264)**

- [ ] `CasTransport` trait defined; `LocalCASTransport` implements it, bodies moved verbatim → behavior unchanged.
- [ ] `CASEngine` parameterized over `CasTransport` (via `Arc<dyn CasTransport>` + `impl CasTransport` constructors); `as_cas()` returns `Option<&CASEngine>` unchanged and still works for `CasLocalBackend`, `OpenAIBackend`, `AnthropicBackend`.
- [ ] All existing Rust CAS + cas_local tests pass unchanged (regression guard green).
- [ ] No measurable perf regression on the local read/write hot path (reasoned + benches build).

- [ ] **Step 5: Finishing the branch**

Use superpowers:finishing-a-development-branch to open the PR. PR body must:
- Reference #4264 and note it unblocks cas-2 (#4265).
- State it is a pure refactor (no behavior change); regression guard = existing CAS suite + 3 new trait-seam tests.
- Flag the deferral gate: confirm with the assignee that "dedup-in-Rust is now a concrete requirement" before merge (per the 2026-06-01 deferral note on the epic).
- Respect repo PR norms (linear history — rebase, not merge; cluster-binary size gate unaffected as this is kernel/backends only).

---

## Self-Review

**Spec coverage** — every #4264 task/acceptance item maps to a task: trait extraction → Task 1; `CASEngine` parameterized + `as_cas()` intact → Task 3 + Task 5/Step 4; tests pass unchanged → Tasks 1–4 run the suite, Task 5 the full gate; perf → Task 5/Step 3. The `get_mtime`/`list` items are explicitly scoped out with rationale.

**Placeholder scan** — no TBD/"handle errors"/"similar to". The one verbatim move (Task 1/Step 4) is specified by exact source line ranges + destination structure rather than re-transcribing 150 identical lines, because reproducing a pure move invites transcription drift; this is the precise, lower-risk instruction.

**Type consistency** — `CasTransport` (11 methods, exact signatures) is used identically in Task 1 (definition), Task 2 (`&dyn CasTransport` params), Task 3 (`Arc<dyn CasTransport>` field, `impl CasTransport + 'static` constructors, `&dyn CasTransport` accessor, `self.transport.as_ref()` coercions), and Task 4 (`kernel::cas_transport::CasTransport` import). `MemoryCasTransport` implements the same 11 signatures. `as_cas() -> Option<&CASEngine>` is asserted unchanged throughout.
