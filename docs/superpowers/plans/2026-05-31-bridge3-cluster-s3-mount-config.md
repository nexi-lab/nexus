# bridge-3: nexusd-cluster S3 Mount Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator declare a single S3-compatible mount for `nexusd-cluster` via `NEXUS_S3_*` env/flags; at startup the daemon builds the backend through the registered `ObjectStoreProvider` and mounts it at a sub-path, failing fast on misconfiguration.

**Architecture:** Add `NEXUS_S3_*` clap globals to `CommonArgs`. A pure `resolve_s3_mount_config()` validates them into an `S3MountConfig` (unit-testable, no kernel). `mount_declared_s3()` calls that, then builds via `get_provider().build(backend_type="s3", …)` — the same SSOT path the gRPC bridge uses — and mounts via `kernel.mount(mount_point, …)`. Wired into `run_daemon` after the root `/` mount, before the VFS gRPC server serves. The default slim binary lacks the `driver-s3` arm, so declaring S3 on it fails fast via the driver gate.

**Tech Stack:** Rust (clap, anyhow, tokio), `nexus-cluster` profile crate, `DefaultObjectStoreProvider` (backends crate), pytest integration test against Cloudflare R2.

**Spec:** `docs/superpowers/specs/2026-05-31-cluster-s3-mount-config-design.md`

---

## File Structure

- **Modify** `rust/profiles/cluster/src/main.rs` — add `NEXUS_S3_*` fields to `CommonArgs`; add `S3MountConfig` + `resolve_s3_mount_config()` + `mount_declared_s3()`; call from `run_daemon`; add `#[cfg(test)] mod tests`. (This file currently has NO test module — Task 2 creates the first one.)
- **Modify** `tests/integration/test_typed_grpc_cluster.py` — add a `cluster_grpc_s3` fixture (boots with `NEXUS_S3_*` env) and `test_startup_s3_mount_round_trips`.
- **Create** `docs/operations/nexusd-cluster-s3-mounts.md` — operator doc.

No Cargo / driver-gate changes: the `driver-s3` feature and the `set_enabled_drivers([… "s3"])` gate already exist from bridge-2 (#4262).

---

## Background facts the engineer needs (verified against the tree)

- **Provider build path** (mirror exactly): `rust/transport/src/grpc.rs::setattr_mount` (lines ~237–311) constructs `ObjectStoreProviderArgs`, calls `provider.build(&args)`, then mounts. Reuse this shape.
- **Provider import:** `use kernel::hal::object_store_provider::{get_provider, ObjectStoreProviderArgs};`
- **`ObjectStoreProviderArgs` has NO `Default`** — every one of its ~40 fields must be listed in the struct literal (Task 3 shows the complete literal).
- **Kernel accessors:** `kernel.peer_client_arc() -> Arc<dyn PeerBlobClient>`, `kernel.self_address_string() -> Option<String>`, `kernel.runtime() -> &Arc<tokio::runtime::Runtime>`. The `peer_client` / `self_address` locals must outlive the args struct (it borrows them).
- **Mount API:** `kernel.mount(path, MountOptions::new("s3").with_backend(backend))` — `MountOptions::new` defaults zone to `root`, io_profile `"memory"`. (`use kernel::kernel::convenience::{KernelConvenience, MountOptions};` is already imported in main.rs.)
- **`build()` on the slim binary:** `backend_type:"s3"` hits the gate first (`is_driver_enabled("s3")`); slim build's gate set is `["path_local","remote"]` → returns `Err("driver 's3' not enabled in current deployment profile")` before the (absent) match arm. This IS the fail-fast path.
- **Provider `s3` arm required args** (`rust/backends/src/provider.rs:176–193`): `s3_bucket`, `aws_region`, `aws_access_key`, `aws_secret_key` required; `s3_prefix` defaults to `""`; `s3_endpoint` optional.
- **Run all cargo commands from `rust/`** (the workspace root is `rust/Cargo.toml`).

---

### Task 1: Add `NEXUS_S3_*` config fields to `CommonArgs`

**Files:**
- Modify: `rust/profiles/cluster/src/main.rs` (the `CommonArgs` struct, currently ends at line ~101 with `bootstrap_mode`)

- [ ] **Step 1: Add the seven S3 fields to `CommonArgs`**

In `rust/profiles/cluster/src/main.rs`, inside `struct CommonArgs { … }`, immediately after the `bootstrap_mode` field (the last field, ~line 100), add:

```rust
    /// S3-compatible bucket to mount (AWS S3 / Cloudflare R2 / MinIO).
    /// Presence of this var DECLARES an S3 mount; when unset the daemon
    /// boots with only the local `/` root mount, exactly as before.
    #[arg(long, env = "NEXUS_S3_BUCKET", global = true)]
    s3_bucket: Option<String>,

    /// AWS region for the S3 mount. Required when `NEXUS_S3_BUCKET` is
    /// set. Cloudflare R2 uses `auto`.
    #[arg(long, env = "NEXUS_S3_REGION", global = true)]
    s3_region: Option<String>,

    /// Access key id for the S3 mount. Required when `NEXUS_S3_BUCKET`
    /// is set. Prefer the env var over the flag so the secret does not
    /// land in `argv` (visible via `ps`).
    #[arg(long, env = "NEXUS_S3_ACCESS_KEY_ID", global = true)]
    s3_access_key_id: Option<String>,

    /// Secret access key for the S3 mount. Required when
    /// `NEXUS_S3_BUCKET` is set. Prefer the env var over the flag.
    #[arg(long, env = "NEXUS_S3_SECRET_ACCESS_KEY", global = true)]
    s3_secret_access_key: Option<String>,

    /// Custom S3-compatible endpoint (Cloudflare R2 / MinIO). Omit for
    /// AWS S3 (virtual-hosted addressing is derived from bucket+region).
    #[arg(long, env = "NEXUS_S3_ENDPOINT", global = true)]
    s3_endpoint: Option<String>,

    /// Key prefix within the bucket. Optional; defaults to empty (bucket
    /// root).
    #[arg(long, env = "NEXUS_S3_PREFIX", global = true)]
    s3_prefix: Option<String>,

    /// VFS mount point for the S3 backend. Must be a non-root absolute
    /// path; defaults to `/s3`. `/` is reserved for the local host-fs
    /// root mount.
    #[arg(long, env = "NEXUS_S3_MOUNT", global = true)]
    s3_mount: Option<String>,
```

- [ ] **Step 2: Verify it compiles (default + s3 feature)**

Run (from `rust/`):
```bash
cargo build -p nexus-cluster
cargo build -p nexus-cluster --features driver-s3
```
Expected: both succeed. (Fields are unused so far → expect `dead_code` warnings; acceptable, resolved in Task 2/3.)

- [ ] **Step 3: Commit**

```bash
git add rust/profiles/cluster/src/main.rs
git commit -m "feat(cluster): add NEXUS_S3_* config args to CommonArgs (bridge-3, #4263)"
```

---

### Task 2: `S3MountConfig` + `resolve_s3_mount_config` validation helper (TDD)

This is the pure, testable core: validates `CommonArgs` → an `S3MountConfig` or a clear error, with no kernel dependency.

**Files:**
- Modify: `rust/profiles/cluster/src/main.rs` (add struct + fn near the other free functions, e.g. just above `install_tracing`; add `#[cfg(test)] mod tests` at end of file)

- [ ] **Step 1: Write the failing tests**

At the END of `rust/profiles/cluster/src/main.rs`, add a test module. Note `CommonArgs` fields are private but the test module is in the same file, so a struct-literal helper works:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    /// Build a `CommonArgs` with all non-S3 fields at their inert
    /// defaults, so each test sets only the S3 fields it exercises.
    fn base_args() -> CommonArgs {
        CommonArgs {
            hostname: None,
            bind_addr: DEFAULT_BIND.to_string(),
            data_dir: PathBuf::from("./nexus-cluster-data"),
            peers: String::new(),
            no_tls: false,
            root_path: None,
            bootstrap_mode: None,
            s3_bucket: None,
            s3_region: None,
            s3_access_key_id: None,
            s3_secret_access_key: None,
            s3_endpoint: None,
            s3_prefix: None,
            s3_mount: None,
        }
    }

    /// Fully-populated S3 args for the happy-path tests.
    fn s3_args() -> CommonArgs {
        CommonArgs {
            s3_bucket: Some("my-bucket".into()),
            s3_region: Some("auto".into()),
            s3_access_key_id: Some("AKID".into()),
            s3_secret_access_key: Some("SECRET".into()),
            ..base_args()
        }
    }

    #[test]
    fn no_bucket_is_no_mount() {
        let cfg = resolve_s3_mount_config(&base_args()).expect("ok");
        assert!(cfg.is_none(), "no NEXUS_S3_BUCKET → no mount declared");
    }

    #[test]
    fn full_config_resolves_with_default_mount_point() {
        let cfg = resolve_s3_mount_config(&s3_args()).expect("ok").expect("some");
        assert_eq!(cfg.bucket, "my-bucket");
        assert_eq!(cfg.region, "auto");
        assert_eq!(cfg.access_key, "AKID");
        assert_eq!(cfg.secret_key, "SECRET");
        assert_eq!(cfg.mount_point, "/s3", "defaults to /s3");
        assert_eq!(cfg.prefix, None);
        assert_eq!(cfg.endpoint, None);
    }

    #[test]
    fn custom_mount_point_and_optional_fields_pass_through() {
        let args = CommonArgs {
            s3_mount: Some("/cloud".into()),
            s3_prefix: Some("team/data".into()),
            s3_endpoint: Some("https://acct.r2.cloudflarestorage.com".into()),
            ..s3_args()
        };
        let cfg = resolve_s3_mount_config(&args).expect("ok").expect("some");
        assert_eq!(cfg.mount_point, "/cloud");
        assert_eq!(cfg.prefix.as_deref(), Some("team/data"));
        assert_eq!(
            cfg.endpoint.as_deref(),
            Some("https://acct.r2.cloudflarestorage.com")
        );
    }

    #[test]
    fn missing_region_errors_naming_the_env_var() {
        let args = CommonArgs { s3_region: None, ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_REGION"), "err was: {err}");
    }

    #[test]
    fn missing_access_key_errors_naming_the_env_var() {
        let args = CommonArgs { s3_access_key_id: None, ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_ACCESS_KEY_ID"), "err was: {err}");
    }

    #[test]
    fn missing_secret_key_errors_naming_the_env_var() {
        let args = CommonArgs { s3_secret_access_key: None, ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_SECRET_ACCESS_KEY"), "err was: {err}");
    }

    #[test]
    fn root_mount_point_is_rejected() {
        let args = CommonArgs { s3_mount: Some("/".into()), ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("sub-path"), "err was: {err}");
    }

    #[test]
    fn trailing_slash_only_mount_point_is_rejected() {
        let args = CommonArgs { s3_mount: Some("///".into()), ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("sub-path"), "err was: {err}");
    }

    #[test]
    fn empty_string_required_field_is_treated_as_missing() {
        // An exported-but-empty env var arrives as Some(""); it must not
        // build a degenerate backend.
        let args = CommonArgs { s3_region: Some(String::new()), ..s3_args() };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_REGION"), "err was: {err}");
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `rust/`):
```bash
cargo test -p nexus-cluster
```
Expected: FAIL — `cannot find type S3MountConfig` / `cannot find function resolve_s3_mount_config`.

- [ ] **Step 3: Implement `S3MountConfig` + `resolve_s3_mount_config`**

In `rust/profiles/cluster/src/main.rs`, add just above `fn install_tracing`:

```rust
/// Validated S3-compatible mount declaration, resolved from `CommonArgs`.
///
/// Produced by [`resolve_s3_mount_config`]; consumed by
/// [`mount_declared_s3`]. Owns its strings so it outlives the borrowed
/// `ObjectStoreProviderArgs` built from it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct S3MountConfig {
    bucket: String,
    region: String,
    access_key: String,
    secret_key: String,
    endpoint: Option<String>,
    prefix: Option<String>,
    mount_point: String,
}

/// Treat a present-but-empty string (e.g. an exported-but-empty env
/// var) as absent, so it triggers the required-field error instead of
/// building a degenerate backend.
fn nonempty_owned(v: &Option<String>) -> Option<&str> {
    v.as_deref().filter(|s| !s.is_empty())
}

/// Parse + validate the `NEXUS_S3_*` declaration.
///
///   * `Ok(None)`        — no `NEXUS_S3_BUCKET`; no S3 mount declared.
///   * `Ok(Some(cfg))`   — a fully-validated mount.
///   * `Err(_)`          — bucket set but a required field is missing /
///                         empty, or the mount point is illegal. The
///                         message names the offending env var.
fn resolve_s3_mount_config(common: &CommonArgs) -> Result<Option<S3MountConfig>> {
    let Some(bucket) = nonempty_owned(&common.s3_bucket) else {
        return Ok(None); // not declared
    };

    let region = nonempty_owned(&common.s3_region).ok_or_else(|| {
        anyhow::anyhow!("NEXUS_S3_REGION is required when NEXUS_S3_BUCKET is set")
    })?;
    let access_key = nonempty_owned(&common.s3_access_key_id).ok_or_else(|| {
        anyhow::anyhow!("NEXUS_S3_ACCESS_KEY_ID is required when NEXUS_S3_BUCKET is set")
    })?;
    let secret_key = nonempty_owned(&common.s3_secret_access_key).ok_or_else(|| {
        anyhow::anyhow!("NEXUS_S3_SECRET_ACCESS_KEY is required when NEXUS_S3_BUCKET is set")
    })?;

    let mount_point = nonempty_owned(&common.s3_mount).unwrap_or("/s3");
    if mount_point.trim_end_matches('/').is_empty() {
        anyhow::bail!(
            "NEXUS_S3_MOUNT must be a sub-path (e.g. \"/s3\"); \"/\" is \
             reserved for the local host-fs root mount"
        );
    }

    Ok(Some(S3MountConfig {
        bucket: bucket.to_string(),
        region: region.to_string(),
        access_key: access_key.to_string(),
        secret_key: secret_key.to_string(),
        endpoint: nonempty_owned(&common.s3_endpoint).map(str::to_string),
        prefix: nonempty_owned(&common.s3_prefix).map(str::to_string),
        mount_point: mount_point.to_string(),
    }))
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run (from `rust/`):
```bash
cargo test -p nexus-cluster
```
Expected: PASS — all 9 tests in `mod tests` green.

- [ ] **Step 5: Commit**

```bash
git add rust/profiles/cluster/src/main.rs
git commit -m "feat(cluster): validate NEXUS_S3_* into S3MountConfig (bridge-3, #4263)"
```

---

### Task 3: `mount_declared_s3` + wire into `run_daemon`

**Files:**
- Modify: `rust/profiles/cluster/src/main.rs` (add `mount_declared_s3` near `resolve_s3_mount_config`; add imports; add one call in `run_daemon`)

- [ ] **Step 1: Add the provider import**

At the top of `rust/profiles/cluster/src/main.rs`, change the existing line:
```rust
use kernel::hal::object_store_provider::{set_enabled_drivers, set_provider};
```
to:
```rust
use kernel::hal::object_store_provider::{
    get_provider, set_enabled_drivers, set_provider, ObjectStoreProviderArgs,
};
```

- [ ] **Step 2: Implement `mount_declared_s3`**

In `rust/profiles/cluster/src/main.rs`, immediately after `resolve_s3_mount_config`, add. The `ObjectStoreProviderArgs` literal lists every field (no `Default`); it mirrors `grpc.rs::setattr_mount`:

```rust
/// Construct + mount the declared S3 backend at boot, if any.
///
/// No-op when `NEXUS_S3_BUCKET` is unset. Builds through the registered
/// `ObjectStoreProvider` (the same path the gRPC bridge uses), so the
/// driver gate is honored: on a slim binary without `--features
/// driver-s3`, `build` returns the gate error and this fails fast.
fn mount_declared_s3(kernel: &Arc<Kernel>, common: &CommonArgs) -> Result<()> {
    let Some(cfg) = resolve_s3_mount_config(common)? else {
        return Ok(()); // no S3 mount declared
    };

    let provider = get_provider()
        .ok_or_else(|| anyhow::anyhow!("no ObjectStoreProvider registered"))?;

    // Locals the args borrow from must outlive `args`.
    let peer_client = kernel.peer_client_arc();
    let self_address = kernel.self_address_string();

    let args = ObjectStoreProviderArgs {
        backend_type: "s3",
        backend_name: "s3",
        mount_path: Some(cfg.mount_point.as_str()),
        local_root: None,
        fsync: false,
        follow_symlinks: false,
        openai_base_url: None,
        openai_api_key: None,
        openai_model: None,
        openai_blob_root: None,
        anthropic_base_url: None,
        anthropic_api_key: None,
        anthropic_model: None,
        anthropic_blob_root: None,
        s3_bucket: Some(cfg.bucket.as_str()),
        s3_prefix: cfg.prefix.as_deref(),
        aws_region: Some(cfg.region.as_str()),
        aws_access_key: Some(cfg.access_key.as_str()),
        aws_secret_key: Some(cfg.secret_key.as_str()),
        s3_endpoint: cfg.endpoint.as_deref(),
        gcs_bucket: None,
        gcs_prefix: None,
        access_token: None,
        root_folder_id: None,
        bot_token: None,
        default_channel: None,
        hn_stories_per_feed: None,
        hn_include_comments: None,
        cli_command: None,
        cli_service: None,
        cli_auth_env_json: None,
        x_bearer_token: None,
        server_address: None,
        remote_auth_token: None,
        remote_ca_pem: None,
        remote_cert_pem: None,
        remote_key_pem: None,
        remote_timeout: 0.0,
        peer_client: &peer_client,
        self_address: self_address.as_deref(),
        runtime: kernel.runtime(),
    };

    let built = provider.build(&args).map_err(|e| {
        anyhow::anyhow!(
            "failed to build S3 backend for mount '{}' (bucket '{}'): {e}",
            cfg.mount_point,
            cfg.bucket,
        )
    })?;
    let backend = built
        .backend
        .ok_or_else(|| anyhow::anyhow!("ObjectStoreProvider returned no backend for S3 mount"))?;

    kernel
        .mount(
            &cfg.mount_point,
            MountOptions::new("s3").with_backend(backend),
        )
        .map_err(|e| anyhow::anyhow!("mount S3 at '{}': {e:?}", cfg.mount_point))?;

    tracing::info!(
        bucket = %cfg.bucket,
        mount_point = %cfg.mount_point,
        endpoint = ?cfg.endpoint,
        "mounted S3-compatible backend",
    );
    Ok(())
}
```

- [ ] **Step 3: Call it from `run_daemon` after the root mount**

In `run_daemon`, find the root-mount block ending with (around line 403–406):
```rust
    tracing::info!(
        root_fs = %root_fs.display(),
        "mounted host-fs at \"/\" via PathLocalBackend",
    );
```
Immediately AFTER that `tracing::info!(...)` call (and before the `// Build VFS gRPC service …` comment / `let vfs_auth` line), insert:
```rust

    // ── Optional S3-compatible mount declared via NEXUS_S3_* ──
    // Built through the same provider the gRPC bridge uses, so the
    // driver gate fails fast on a slim binary without `driver-s3`.
    // Runs before the VFS gRPC server serves so the mount is live.
    mount_declared_s3(&kernel, &common)?;
```

- [ ] **Step 4: Verify it compiles, both feature sets**

Run (from `rust/`):
```bash
cargo build -p nexus-cluster
cargo build -p nexus-cluster --features driver-s3
```
Expected: both succeed, no `dead_code` warnings for the S3 fields anymore.

- [ ] **Step 5: Verify the slim binary fails fast when S3 is declared without the driver**

Run (from `rust/`):
```bash
cargo build -p nexus-cluster
NEXUS_S3_BUCKET=b NEXUS_S3_REGION=auto \
NEXUS_S3_ACCESS_KEY_ID=k NEXUS_S3_SECRET_ACCESS_KEY=s \
./target/debug/nexusd-cluster --no-tls --hostname 127.0.0.1 \
  --bind-addr 127.0.0.1:0 --data-dir /tmp/nexus-bridge3-failfast \
  --bootstrap-mode static; echo "exit=$?"
```
Expected: process exits non-zero; stderr/log contains `failed to build S3 backend` … `driver 's3' not enabled in current deployment profile`. (Clean up: `rm -rf /tmp/nexus-bridge3-failfast`.)

- [ ] **Step 6: Run the unit tests again (no regressions)**

Run (from `rust/`):
```bash
cargo test -p nexus-cluster
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rust/profiles/cluster/src/main.rs
git commit -m "feat(cluster): mount declared S3 backend at startup via provider (bridge-3, #4263)"
```

---

### Task 4: Integration test — startup S3 mount round-trips against R2

Reuses the bridge-2 harness in `tests/integration/test_typed_grpc_cluster.py` (`_r2_env`, `requires_e2e`, `requires_r2`, and the `_resolve_worktree_cluster_binary` helper). Adds a fixture that boots the daemon with `NEXUS_S3_*` set, then asserts a write/read round-trip through the startup mount (no gRPC `Setattr` — the mount comes from config).

**Files:**
- Modify: `tests/integration/test_typed_grpc_cluster.py`

- [ ] **Step 1: Write the failing test + fixture**

Append to the END of `tests/integration/test_typed_grpc_cluster.py`:

```python
# ── bridge-3 (#4263): S3 mount declared via NEXUS_S3_* at startup ─────────────


@pytest.fixture()
def cluster_grpc_s3(tmp_path: Path) -> Iterator[str]:
    """Boot ``nexus-cluster`` with ``NEXUS_S3_*`` set so the daemon mounts
    an S3-compatible backend at ``/s3`` at startup, and yield ``host:port``.

    Skips cleanly when the binary or R2 creds are absent. Maps the test's
    ``NEXUS_R2_*`` creds onto the daemon's ``NEXUS_S3_*`` surface.
    """
    nexus_cluster = _resolve_worktree_cluster_binary()
    if not nexus_cluster:
        pytest.skip(
            "nexus-cluster binary not in worktree rust/target (build with "
            "`cargo build -p nexus-cluster --features driver-s3`)"
        )
    env_r2 = _r2_env()
    if env_r2 is None:
        pytest.skip("startup S3 mount E2E requires NEXUS_R2_* creds")

    data_dir = tmp_path / "data"
    log_path = tmp_path / "cluster.log"
    log_handle = log_path.open("wb")
    proc: subprocess.Popen | None = None
    addr: str | None = None

    s3_env = {
        "NEXUS_S3_BUCKET": env_r2["NEXUS_R2_BUCKET"],
        "NEXUS_S3_REGION": env_r2["NEXUS_R2_REGION"],
        "NEXUS_S3_ACCESS_KEY_ID": env_r2["NEXUS_R2_ACCESS_KEY_ID"],
        "NEXUS_S3_SECRET_ACCESS_KEY": env_r2["NEXUS_R2_SECRET_ACCESS_KEY"],
        "NEXUS_S3_ENDPOINT": env_r2["NEXUS_R2_ENDPOINT"],
        "NEXUS_S3_MOUNT": "/s3",
    }

    def _cleanup() -> None:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        log_handle.close()

    try:
        last_err = ""
        for _attempt in range(5):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            finally:
                s.close()
            candidate_addr = f"127.0.0.1:{port}"
            proc = subprocess.Popen(
                [
                    nexus_cluster,
                    "--no-tls",
                    "--hostname",
                    "127.0.0.1",
                    "--bind-addr",
                    candidate_addr,
                    "--data-dir",
                    str(data_dir),
                    "--bootstrap-mode",
                    "static",
                ],
                env={
                    **os.environ,
                    **s3_env,
                    "RUST_LOG": os.environ.get("RUST_LOG") or "info,nexus_raft=info",
                },
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 20
            bound = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    last_err = (
                        f"nexus-cluster exited early (rc={proc.returncode}); "
                        f"log: {log_path.read_text()[-600:]}"
                    )
                    # A driver-not-compiled exit is an explicit skip signal.
                    if "not enabled" in log_path.read_text():
                        pytest.skip(
                            "cluster binary lacks the s3 driver — rebuild with "
                            "`cargo build -p nexus-cluster --features driver-s3`"
                        )
                    break
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        bound = True
                        break
                except OSError:
                    time.sleep(0.2)
            if bound:
                addr = candidate_addr
                break
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            proc = None

        if addr is None:
            raise AssertionError(
                f"nexus-cluster (S3 startup) failed to bind; last err: "
                f"{last_err or 'timed out'}"
            )
        yield addr
    except BaseException:
        _cleanup()
        raise
    else:
        _cleanup()


@requires_e2e
@requires_r2
def test_startup_s3_mount_round_trips(cluster_grpc_s3):
    """bridge-3 (#4263) E2E — the daemon mounts S3/R2 at ``/s3`` from
    ``NEXUS_S3_*`` config at startup (no gRPC Setattr), and a write/read
    through that mount round-trips against real R2.
    """
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    ch = grpc.insecure_channel(cluster_grpc_s3)
    stub = vfs_pb2_grpc.NexusVFSServiceStub(ch)

    obj_path = f"/s3/bridge3-startup-{os.getpid()}.txt"
    body = b"startup-s3-mount-through-rust-" + str(os.getpid()).encode()

    try:
        w = stub.Write(vfs_pb2.WriteRequest(path=obj_path, content=body), timeout=30)
        assert not w.is_error, w.error_payload
        assert w.size == len(body)

        r = stub.Read(vfs_pb2.ReadRequest(path=obj_path), timeout=30)
        assert not r.is_error, r.error_payload
        assert r.content == body, "read-back bytes differ — R2 round-trip broken"
    finally:
        with contextlib.suppress(Exception):
            stub.Delete(vfs_pb2.DeleteRequest(path=obj_path), timeout=30)
```

- [ ] **Step 2: Verify it skips cleanly without the gate/creds**

Run (from repo root):
```bash
python -m pytest tests/integration/test_typed_grpc_cluster.py::test_startup_s3_mount_round_trips -v
```
Expected: SKIPPED (`NEXUS_E2E != "1"`), no collection/syntax error.

- [ ] **Step 3: Verify it passes against real R2**

Build the s3 binary and run with creds (creds live in a gitignored file, e.g. `/tmp/nexus-r2.env`, per the bridge-2 precedent):
```bash
( cd rust && cargo build -p nexus-cluster --features driver-s3 )
set -a; . /tmp/nexus-r2.env; set +a
NEXUS_E2E=1 python -m pytest \
  tests/integration/test_typed_grpc_cluster.py::test_startup_s3_mount_round_trips -v
```
Expected: PASS (1 passed). If it SKIPS with "lacks the s3 driver", the build step did not produce the s3 binary in `rust/target/debug` — rebuild.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_typed_grpc_cluster.py
git commit -m "test(cluster): E2E startup S3 mount round-trips via R2 (bridge-3, #4263)"
```

---

### Task 5: Operator documentation

**Files:**
- Create: `docs/operations/nexusd-cluster-s3-mounts.md`

- [ ] **Step 1: Write the doc**

Create `docs/operations/nexusd-cluster-s3-mounts.md` with:

````markdown
# nexusd-cluster: S3-compatible mounts

`nexusd-cluster` can mount one S3-compatible bucket (AWS S3, Cloudflare
R2, MinIO) at startup, alongside the local host-fs root at `/`. The
mount is declared with environment variables (or the equivalent flags)
and is built by the same `ObjectStoreProvider` the gRPC mount path uses.

> **Build requirement.** The default slim binary does **not** include
> the S3 driver. Build with the `driver-s3` feature:
>
> ```bash
> cargo build -p nexus-cluster --features driver-s3
> ```
>
> If `NEXUS_S3_BUCKET` is set on a binary built without `driver-s3`, the
> daemon **fails fast at startup** with
> `driver 's3' not enabled in current deployment profile`.

## Configuration

| Env | Flag | Required | Default | Notes |
|-----|------|----------|---------|-------|
| `NEXUS_S3_BUCKET` | `--s3-bucket` | declares the mount | — | Set this to enable an S3 mount. |
| `NEXUS_S3_REGION` | `--s3-region` | yes (if bucket) | — | AWS region; Cloudflare R2 uses `auto`. |
| `NEXUS_S3_ACCESS_KEY_ID` | `--s3-access-key-id` | yes (if bucket) | — | Prefer env over flag. |
| `NEXUS_S3_SECRET_ACCESS_KEY` | `--s3-secret-access-key` | yes (if bucket) | — | Prefer env over flag. |
| `NEXUS_S3_ENDPOINT` | `--s3-endpoint` | no | — | Custom endpoint (R2/MinIO). Omit for AWS. |
| `NEXUS_S3_PREFIX` | `--s3-prefix` | no | `` (empty) | Key prefix within the bucket. |
| `NEXUS_S3_MOUNT` | `--s3-mount` | no | `/s3` | Mount point. Must be a non-root path. |

**Security:** pass credentials via environment variables, not flags.
Flag values appear in the process's `argv`, which is world-readable via
`ps`. In Kubernetes, source the keys from a `Secret`; under systemd, use
an `EnvironmentFile` with `0600` permissions.

## Example — AWS S3

```bash
export NEXUS_S3_BUCKET=my-prod-bucket
export NEXUS_S3_REGION=us-east-1
export NEXUS_S3_ACCESS_KEY_ID=AKIA...
export NEXUS_S3_SECRET_ACCESS_KEY=...
export NEXUS_S3_PREFIX=nexus/data        # optional
export NEXUS_S3_MOUNT=/s3                 # optional (default)

nexusd-cluster --bootstrap-mode static
```

Files written under `/s3/...` land in `s3://my-prod-bucket/nexus/data/...`.
The local host-fs root at `/` is unaffected.

## Example — Cloudflare R2

R2 is S3-compatible via a custom endpoint and `region=auto`:

```bash
export NEXUS_S3_BUCKET=my-r2-bucket
export NEXUS_S3_REGION=auto
export NEXUS_S3_ACCESS_KEY_ID=...        # from R2 "Manage R2 API Tokens"
export NEXUS_S3_SECRET_ACCESS_KEY=...
export NEXUS_S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
export NEXUS_S3_MOUNT=/r2

nexusd-cluster --bootstrap-mode static --features-built-with driver-s3
```

> R2 API tokens must have **Object Read & Write** permission — a
> read-only token passes a bucket check but fails writes with `403
> AccessDenied`.

## Fail-fast behavior

The daemon refuses to start (non-zero exit, message on stderr) when:

- `NEXUS_S3_BUCKET` is set but `NEXUS_S3_REGION`,
  `NEXUS_S3_ACCESS_KEY_ID`, or `NEXUS_S3_SECRET_ACCESS_KEY` is missing or
  empty — the error names the missing variable.
- `NEXUS_S3_MOUNT` is `/` (or only slashes) — `/` is reserved for the
  local host-fs root.
- The binary was built without `--features driver-s3` — the driver gate
  rejects the `s3` driver.

A bad endpoint or unreachable bucket is **not** a startup failure: the
backend is constructed without network I/O, so the first read/write to
the mount surfaces the error instead.

## Scope

- One S3 mount per daemon. Multiple mounts are not yet supported.
- The mount is node-local; it is not replicated across the federation.
  Each node that needs the bucket sets `NEXUS_S3_*` independently.
- GCS startup mounts are not wired (the mechanism is identical; track
  separately).
````

- [ ] **Step 2: Commit**

```bash
git add docs/operations/nexusd-cluster-s3-mounts.md
git commit -m "docs(ops): document nexusd-cluster NEXUS_S3_* startup mounts (bridge-3, #4263)"
```

---

### Task 6: Final verification — fmt, clippy, full test

**Files:** none (verification only)

- [ ] **Step 1: Format check**

Run (from `rust/`):
```bash
cargo fmt -p nexus-cluster -- --check
```
Expected: clean (no diff). If it reports changes, run `cargo fmt -p nexus-cluster` and re-commit.

- [ ] **Step 2: Clippy, both feature sets, deny warnings**

Run (from `rust/`):
```bash
cargo clippy -p nexus-cluster -- -D warnings
cargo clippy -p nexus-cluster --features driver-s3 -- -D warnings
```
Expected: both clean. (CI's lint gate covers the cluster crate, so this must pass.)

- [ ] **Step 3: Full cluster unit tests**

Run (from `rust/`):
```bash
cargo test -p nexus-cluster
cargo test -p nexus-cluster --features driver-s3
```
Expected: PASS for both. (The `resolve_s3_mount_config` tests are feature-independent and run in both.)

- [ ] **Step 4: Confirm the default slim binary is unchanged in behavior**

Boot the slim binary with NO S3 env and confirm it still comes up (the `mount_declared_s3` no-op path):
```bash
( cd rust && cargo build -p nexus-cluster )
./rust/target/debug/nexusd-cluster --no-tls --hostname 127.0.0.1 \
  --bind-addr 127.0.0.1:2126 --data-dir /tmp/nexus-bridge3-slim \
  --bootstrap-mode static &
sleep 3; curl -sf localhost:2126 >/dev/null 2>&1; echo "booted ok"; kill %1
rm -rf /tmp/nexus-bridge3-slim
```
Expected: daemon boots (no S3 mount log line), no error. (`curl` may not speak gRPC — the point is the process stays up; check the log shows `mounted host-fs at "/"` and no S3 line.)

- [ ] **Step 5: No-op commit guard**

Nothing to commit here unless fmt fixed something. Confirm clean tree:
```bash
git status --short
```
Expected: clean (all prior tasks committed).

---

## Self-Review

**Spec coverage:**
- Config surface (7 `NEXUS_S3_*` clap+env vars, namespaced creds) → Task 1.
- Validation / `S3MountConfig` → Task 2.
- Boot wiring through provider after root mount → Task 3 (steps 2–3).
- Fail-fast: missing field / illegal mount / driver-not-compiled → Task 2 tests + Task 3 step 5.
- Sub-path mount, local `/` intact → default `/s3`, root rejected (Task 2); root mount untouched (Task 3 inserts *after* it).
- Integration test against R2 → Task 4.
- Operator docs → Task 5.
- fmt/clippy/test gates → Task 6.

**Placeholder scan:** none — every code step is complete; the `ObjectStoreProviderArgs` literal is fully spelled out (no `..`).

**Type consistency:** `resolve_s3_mount_config(&CommonArgs) -> Result<Option<S3MountConfig>>` and `mount_declared_s3(&Arc<Kernel>, &CommonArgs) -> Result<()>` used identically across tasks. `S3MountConfig` fields (`bucket/region/access_key/secret_key/endpoint/prefix/mount_point`) match between the struct def (Task 2 step 3), the tests (Task 2 step 1), and the consumer (Task 3 step 2). `nonempty_owned` defined once (Task 2) and reused (Task 3 not needed — config already owns clean strings). Kernel accessors (`peer_client_arc`, `self_address_string`, `runtime`) match verified signatures.
