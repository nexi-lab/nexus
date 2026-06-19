# #4343 — Durable metastore for nexusd-cluster: design

**Issue**: [nexi-lab/nexus#4343](https://github.com/nexi-lab/nexus/issues/4343)
**Date**: 2026-06-10
**Status**: approved (sections reviewed interactively)

## Problem

`nexusd-cluster` boots its `Kernel` with a tempfile-backed `LocalMetaStore`
(`rust/kernel/src/kernel/mod.rs` boot path, nexus-vfs) and never calls
`Kernel::set_metastore_path`, the only durable wiring method. The Python
`KernelClient` spawn path passes only `NEXUS_DATA_DIR` (payload dir) and
`NEXUS_BOOTSTRAP_MODE=dynamic` (rootless boot — no raft-backed zone
metastore either), so the global tempdir metastore serves the entire VFS
namespace. Every kernel restart drops every file registration: payload
bytes survive under `<data_dir>/root/**`, but `exists` → `false` and reads
→ 404 for everything written before the boot. Confirmed twice in
production (issue + DeepBuildAI/koodle#1302).

## Scope (user-decided)

- Issue asks 1 (durable wiring) + 2 (regression test), across both repos.
- Ask 3 (`walk-import` recovery command) **deferred** to a separate issue;
  recovery is already proven downstream via `batch/write`.
- Regression test is **Python-only** (nexus repo) — keeps the nexus-vfs
  diff minimal for kernel-team review.
- Config knob: flag + env + default (`--metastore-path` /
  `NEXUS_METASTORE_PATH` / `<data_dir>/metastore.redb`).
- Approach A chosen: profile-level wiring only. Rejected: kernel reads env
  in `Kernel::new()` (env magic in a library crate, kernel-core
  governance); new `Kernel::with_metastore_path` constructor (duplicate
  API surface, no gain over the existing setter).

## PR 1 — nexus-vfs (`rust/profiles/cluster/src/main.rs` only)

1. New `CommonArgs` field after `data_dir`:

   ```rust
   /// Durable global metastore (redb). The kernel's VFS namespace —
   /// file registrations survive restarts only if this lives on
   /// persistent storage. Defaults to `<data_dir>/metastore.redb`.
   #[arg(long, env = "NEXUS_METASTORE_PATH", global = true)]
   metastore_path: Option<PathBuf>,
   ```

   Helper `fn metastore_path(&self) -> PathBuf` defaulting to
   `self.data_dir.join("metastore.redb")`, mirroring the existing
   `root_fs_path()` pattern.

2. Wiring immediately after `Arc::new(Kernel::new())` (~line 516), before
   the `/` mount and before VFS gRPC routes are built:

   ```rust
   let metastore_path = common.metastore_path();
   if let Some(parent) = metastore_path.parent() {
       std::fs::create_dir_all(parent)
           .with_context(|| format!("create metastore dir {}", parent.display()))?;
   }
   kernel
       .set_metastore_path(metastore_path.to_str().context("metastore path must be UTF-8")?)
       .map_err(|e| anyhow::anyhow!(
           "open durable metastore {}: {:?}", metastore_path.display(), e))?;
   tracing::info!(path = %metastore_path.display(), "durable metastore wired");
   ```

**Error handling**: boot fails hard if redb cannot open. A silent tempdir
fallback is exactly the data-loss bug; the operator must see a crash, not
an amnesiac kernel. Side effect: a second daemon on the same data_dir now
fails loudly at boot (redb exclusive lock) — consistent with the
documented `share`/`join` "daemon must be stopped" rule.

**Ordering rationale**: `set_metastore_path` swaps the global metastore
slot and drops the boot tempdir; any registration made before the swap
would be lost, so the swap happens before any mount or traffic.

No kernel-crate, raft, or transport changes.

**Coverage**: `nexus-full`'s `[[bin]]` target reuses `../cluster/src/main.rs`
verbatim (`rust/profiles/full/Cargo.toml`), so this one fix site covers both
`nexusd-cluster` and `nexusd-full` — the binary the nexus Docker image
actually ships (installed via `cargo install --git`, symlinked to
`nexus-cluster` for the Python runtime).

**Why nexus-vfs must change (no nexus-only fix exists)**: the nexus repo
builds no kernel host binary (workspace = services + two plugin cdylibs);
the transport layer exposes no RPC for metastore wiring (the Python
`set_metastore_path` stub is a no-op for this reason); and the plugin ABI
neither exposes the setter nor loads early enough to swap safely.

## PR 2 — nexus (this repo, after PR 1 merges)

1. `Cargo.toml` + `Cargo.lock`: bump all seven nexus-vfs git dep pins
   (root `Cargo.toml`) from rev
   `32c47310e48d449bb5c3f02e48ef12455bf0e8fa` to the PR-1 merge commit.
2. `Dockerfile` `ARG NEXUS_VFS_REV`: bump from `f4227a2…` (stale pre-#27-
   merge integration pin) to the PR-1 merge commit. **This is the bump
   that ships the fix** — the image's kernel binary (`nexusd-full`) is
   `cargo install`ed at this rev; the Cargo.toml pins only sync the
   plugin/services crates.
3. `src/nexus/remote/kernel_client.py` `set_metastore_path` stub (~line
   880): replace the false "already passed via env" comment — the binary
   now derives `<NEXUS_DATA_DIR>/metastore.redb` itself; `NEXUS_DATA_DIR`
   set at spawn is the carrier. Comment-only, no behavior change.
4. Regression test `tests/integration/test_kernel_restart_survival.py`:
   - Spawn real `nexus-cluster` via `KernelClient(metadata_path=tmp_path)`
     + explicit `.open()` (#4133 fixture pattern); skip if the binary is
     not on PATH (s3_parity conftest pattern).
   - Write a file; assert `exists` → `True`.
   - `close()` (SIGTERM + wait), new `KernelClient` on the same
     `metadata_path`, `.open()`.
   - Assert `exists` still `True`; content reads back byte-identical;
     a never-written path stays `False` (guards walk-import-style false
     positives).
   - Fails on rev `32c47310`, passes after the bump.

## Validation & sequencing

1. Implement both changes locally; build `nexusd-cluster` from the patched
   local nexus-vfs (`PROTOC=protobuf@21` gotcha applies), symlink as
   `nexus-cluster` on PATH, run the Python test — proves the pair
   end-to-end before either PR goes up.
2. vfs PR → kernel-team (@elfenlieds7) review → merge.
3. nexus PR with real rev bump + test + comment fix; linear history
   (rebase, not merge).

## Governance

All Rust paths in both repos are CODEOWNERS-gated on @elfenlieds7. Profile
wiring is not kernel-core internals; precedent: nexus-vfs PRs #19, #27
(profile/backends changes by non-kernel authors accepted).

## Out of scope

- `walk-import` admin recovery command (separate issue).
- Migration for already-bitten deployments — the old namespace lived in a
  tempdir and is unrecoverable by this fix; downstream recovery uses
  `batch/write` re-registration.
- Rust-side restart test in nexus-vfs (user opted Python-only).
