# bridge-3: nexusd-cluster startup/config mount for S3-compatible backends

**Issue:** [#4263](https://github.com/nexi-lab/nexus/issues/4263) — part of epic [#4259](https://github.com/nexi-lab/nexus/issues/4259) (Approach 1, connector-first).
**Depends on:** #4261 (bridge-1: `DefaultObjectStoreProvider` + driver gate) and #4262 (bridge-2: backend params over gRPC + Rust-side mount construction), both merged into `develop`.
**Date:** 2026-05-31

## Problem

`nexusd-cluster` (`rust/profiles/cluster/src/main.rs`) mounts `PathLocalBackend` at `/` at boot and exposes no config/env path to declare a cloud mount. After bridge-1/bridge-2, the daemon registers `DefaultObjectStoreProvider` and the gRPC `setattr_mount` path can build S3 backends from wire params — but there is no *startup-time* surface for an operator to declare an S3 mount. bridge-3 closes that gap, completing Approach 1 (connector) of the epic.

## Goal

Let an operator declare a single S3-compatible mount (AWS S3 / Cloudflare R2 / MinIO) for `nexusd-cluster` via config/env. At startup, after the provider is registered and the root `/` mount is in place, construct the declared backend through the registered provider and mount it at an operator-chosen sub-path. Misconfiguration fails fast with a clear, env-var-named error.

## Scope

### In scope
- A clap-arg-with-env-fallback config surface for **one** S3 mount (mirrors every existing cluster knob).
- Boot-time construction of the S3 backend **through the registered `ObjectStoreProvider`** (the same SSOT path the gRPC bridge uses) and mount via `Kernel::mount`.
- Fail-fast validation: missing required fields, driver not compiled in, illegal mount point.
- Operator docs in `docs/operations/nexusd-cluster-config.md`.
- Rust unit tests for validation + an env-gated integration test booting against Cloudflare R2.

### Out of scope
- Multiple S3 mounts in one daemon (use the future JSON-config escape hatch).
- Replacing the `/` root with S3 (operator chose: keep local root, mount S3 at a sub-path).
- GCS startup mounts (identical pattern, separate `driver-gcs` feature — not requested here).
- Federation/replication of the S3 mount across nodes (each node mounts independently from its own env).
- Dedup-in-Rust / CAS-over-S3 (epic Approach 2, deferred).

## Configuration surface

New global args on `CommonArgs` in `rust/profiles/cluster/src/main.rs`, each a clap `#[arg(long, env = "…", global = true)]`, following the established `NEXUS_*` pattern (`NEXUS_ROOT_FS`, `NEXUS_PEERS`, etc.). `NEXUS_S3_BUCKET` is the **trigger**: when unset, the daemon behaves exactly as today (no S3 mount, byte-identical boot). When set, the remaining required fields are validated.

| Env | Flag | Required | Default | Notes |
|-----|------|----------|---------|-------|
| `NEXUS_S3_BUCKET` | `--s3-bucket` | trigger | — | Presence declares an S3 mount. |
| `NEXUS_S3_REGION` | `--s3-region` | yes (if bucket) | — | AWS region; Cloudflare R2 uses `auto`. |
| `NEXUS_S3_ACCESS_KEY_ID` | `--s3-access-key-id` | yes (if bucket) | — | **Prefer env over flag** (argv is world-readable via `ps`). |
| `NEXUS_S3_SECRET_ACCESS_KEY` | `--s3-secret-access-key` | yes (if bucket) | — | **Prefer env over flag.** |
| `NEXUS_S3_ENDPOINT` | `--s3-endpoint` | no | — | Custom S3-compatible endpoint (R2/MinIO). Omit → AWS addressing. |
| `NEXUS_S3_PREFIX` | `--s3-prefix` | no | `` (empty) | Key prefix within the bucket. |
| `NEXUS_S3_MOUNT` | `--s3-mount` | no | `/s3` | VFS mount point. Must be a non-root absolute path. |

**Credential naming decision:** namespaced `NEXUS_S3_ACCESS_KEY_ID` / `NEXUS_S3_SECRET_ACCESS_KEY` rather than the conventional `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Rationale: collision-free and consistent with the rest of the `NEXUS_*` surface; the daemon may run in environments where ambient `AWS_*` vars mean something else. (A future enhancement could fall back to `AWS_*` when the namespaced vars are unset; not in scope.)

## Architecture & boot wiring

The new logic lives in one place: a helper `mount_declared_s3(&kernel, &common) -> Result<()>` called from `run_daemon`, immediately **after** the root `/` mount and **before** `build_vfs_routes` / `open_zone_manager`, so the S3 mount is live before the VFS gRPC server begins serving.

Ordering within `run_daemon` (current → new):
1. provider registration: `set_provider(DefaultObjectStoreProvider)` + `set_enabled_drivers([...])`. *(unchanged — already present)*
2. root `/` mount via `PathLocalBackend`. *(unchanged)*
3. **NEW:** `mount_declared_s3(&kernel, &common)?` — no-op when `NEXUS_S3_BUCKET` unset.
4. `build_vfs_routes(...)` → `open_zone_manager(...)` → bootstrap → serve. *(unchanged)*

### Construction path — through the provider (SSOT)

`mount_declared_s3` does **not** call `S3Backend::new` directly. It builds an `ObjectStoreProviderArgs` with `backend_type: "s3"` and the S3 fields, calls `get_provider().build(&args)`, then mounts the result:

```rust
fn mount_declared_s3(kernel: &Arc<Kernel>, common: &CommonArgs) -> Result<()> {
    let Some(bucket) = common.s3_bucket.as_deref() else { return Ok(()); }; // trigger

    // fail-fast required-field checks with env-var-named errors (see below)
    let region = common.s3_region.as_deref()
        .ok_or_else(|| anyhow!("NEXUS_S3_REGION is required when NEXUS_S3_BUCKET is set"))?;
    let access_key = common.s3_access_key_id.as_deref()
        .ok_or_else(|| anyhow!("NEXUS_S3_ACCESS_KEY_ID is required when NEXUS_S3_BUCKET is set"))?;
    let secret_key = common.s3_secret_access_key.as_deref()
        .ok_or_else(|| anyhow!("NEXUS_S3_SECRET_ACCESS_KEY is required when NEXUS_S3_BUCKET is set"))?;

    let mount_point = common.s3_mount.as_deref().unwrap_or("/s3");
    if mount_point.trim_end_matches('/').is_empty() {
        bail!("NEXUS_S3_MOUNT must be a sub-path; \"/\" is reserved for the local host-fs root");
    }

    let provider = get_provider()
        .ok_or_else(|| anyhow!("no ObjectStoreProvider registered"))?;
    let peer_client = kernel.peer_client_arc();
    let self_address = kernel.self_address_string();
    let args = ObjectStoreProviderArgs {
        backend_type: "s3",
        backend_name: "s3",
        mount_path: Some(mount_point),
        s3_bucket: Some(bucket),
        s3_prefix: common.s3_prefix.as_deref(),       // None → provider defaults to ""
        aws_region: Some(region),
        aws_access_key: Some(access_key),
        aws_secret_key: Some(secret_key),
        s3_endpoint: common.s3_endpoint.as_deref(),
        // all other fields: None / defaults, mirroring grpc.rs::setattr_mount
        peer_client: &peer_client,
        self_address: self_address.as_deref(),
        runtime: kernel.runtime(),
        ..
    };
    let built = provider.build(&args)
        .map_err(|e| anyhow!("failed to build S3 backend for {mount_point}: {e}"))?;
    let backend = built.backend
        .ok_or_else(|| anyhow!("provider returned no backend for S3 mount"))?;

    kernel.mount(mount_point, MountOptions::new("s3").with_backend(backend))
        .map_err(|e| anyhow!("mount S3 at {mount_point}: {e:?}"))?;
    tracing::info!(bucket, mount_point, "mounted S3-compatible backend");
    Ok(())
}
```

Reusing the provider means a single S3 construction site shared with the gRPC bridge, and the driver gate (`is_driver_enabled("s3")`) is honored automatically.

The exact `ObjectStoreProviderArgs` field set mirrors `VfsServiceImpl::setattr_mount` in `rust/transport/src/grpc.rs` (the `..` above is shorthand for the ~25 `None`/default fields; the struct has no `Default`, so the implementation lists them explicitly, exactly as `setattr_mount` does).

## Error handling — fail fast (AC #4)

| Misconfiguration | Detection | Result |
|------------------|-----------|--------|
| `driver-s3` not compiled into the binary | gate excludes `"s3"`; `provider.build` returns `driver 's3' not enabled in current deployment profile` | `mount_declared_s3` returns `Err` → `run_daemon` aborts before serving |
| `NEXUS_S3_BUCKET` set, region/creds missing | explicit `ok_or_else` checks in `mount_declared_s3` | `Err` with the exact missing env-var name → abort |
| `NEXUS_S3_MOUNT` = `/` (or `///`) | `trim_end_matches('/').is_empty()` check | `Err` "must be a sub-path; / is reserved" → abort |
| Bad endpoint / unreachable bucket | surfaces lazily on first I/O (S3Backend is constructed eagerly but does no network I/O at build) | first read/write returns a backend error; not a boot failure (matches S3Backend semantics) |

All boot-path errors propagate as `anyhow::Error` out of `run_daemon`, so the process exits non-zero with the message on stderr — the established fail-fast pattern for `bootstrap_mode`, TLS, and root-mount failures.

The default slim binary (no `driver-s3`) with `NEXUS_S3_BUCKET` set fails fast at startup with the gate error rather than silently ignoring the config — an operator who declares an S3 mount on a binary that can't serve it learns immediately.

## Mount semantics

Node-local mount, exactly like the boot `/` mount: the S3 backend is mounted at `NEXUS_S3_MOUNT` (default `/s3`) on this node's kernel; the `/` `PathLocalBackend` host-fs mount stays intact and unmodified. Both coexist. Every node that sets the env vars mounts the same bucket independently — there is no raft replication of this mount (consistent with the per-node `/` root mount and out of scope per the epic).

## Testing

### Rust unit tests (`rust/profiles/cluster/src/main.rs`, `#[cfg(test)]`)
A small pure validation helper is extracted so the field/mount-point checks are testable without a live kernel:
- `NEXUS_S3_BUCKET` set + region missing → `Err` naming `NEXUS_S3_REGION`.
- bucket + creds missing → `Err` naming the missing credential var.
- mount point defaults to `/s3` when unset.
- mount point `/` → `Err`.
- bucket unset → `Ok(None)` / no-op (daemon behaves as today).

### Integration test (AC #3) — `tests/integration/`
Mirrors the bridge-2 E2E (`test_typed_grpc_cluster.py::test_s3_r2_dt_mount_builds_backend_and_round_trips`):
- Build `nexusd-cluster` with `--features driver-s3`.
- Boot the daemon with `NEXUS_S3_*` env pointing at **Cloudflare R2** (team precedent; `region=auto`, custom endpoint). The bridge-2 fixture's `--hostname 127.0.0.1` fix is required so the single-voter zone becomes healthy.
- Over the VFS gRPC service, write a file under `<NEXUS_S3_MOUNT>/…` and read it back; assert the round-trip.
- Env-gated (`NEXUS_E2E=1` + `NEXUS_R2_*`); skips cleanly when the binary lacks `driver-s3` ("not enabled") or creds are absent.

R2 over MinIO confirmed by the operator: the team already uses Cloudflare R2 for bridge-2 E2E, the Rust `S3Backend` is proven against it live (`s3.rs::tests::live_r2_round_trip`), and it needs no local container.

## Documentation

The `nexusd-cluster` env surface (`NEXUS_ROOT_FS`, `NEXUS_BOOTSTRAP_MODE`, `NEXUS_FEDERATION_*`, …) is not currently documented anywhere under `docs/`. Rather than retrofit a full daemon-config reference (out of scope), create a new focused operator doc **`docs/operations/nexusd-cluster-s3-mounts.md`** covering just this feature:
1. A quick-reference table of the seven `NEXUS_S3_*` vars.
2. The `driver-s3` build requirement (`cargo build -p nexus-cluster --features driver-s3`) — the default slim binary does not ship the S3 arm and fails fast if `NEXUS_S3_BUCKET` is set.
3. A worked AWS example and a worked Cloudflare R2 example (custom endpoint + `region=auto`).
4. The credentials-in-env security note (prefer env over `--flag` to keep secrets out of `argv`).
5. The fail-fast behaviors (missing required field, illegal mount point, driver not compiled).

## Acceptance criteria mapping

- **Operator can declare an S3 mount via config/env** → the `NEXUS_S3_*` clap/env surface.
- **Startup mounts the declared backend (in addition to the local root)** → `mount_declared_s3` after the `/` mount; `/` stays intact, S3 at `/s3`.
- **Integration: boots with an S3 mount against object storage and serves read/write through Rust E2E** → the R2 integration test.
- **Misconfiguration fails fast with a clear error** → the error-handling table; env-var-named messages; driver-not-compiled gate error.
