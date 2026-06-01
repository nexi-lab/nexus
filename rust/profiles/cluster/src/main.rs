//! Nexus cluster-profile runtime — `nexusd-cluster`.
//!
//! A self-contained ~5 MB Rust binary that brings up:
//!   * [`nexus_raft::ZoneManager`] (multi-zone Raft + gRPC server)
//!   * Day-1 TLS bootstrap (CA + node cert + join token) on first start
//!   * Static topology (`NEXUS_FEDERATION_ZONES` + `NEXUS_FEDERATION_MOUNTS`)
//!   * Health-check loop that drives `apply_topology` to convergence
//!
//! Subcommands:
//!   * `nexusd-cluster`             — start the daemon (default)
//!   * `nexusd-cluster share`       — detach a local subtree into a new zone
//!   * `nexusd-cluster join`        — mount a remote zone locally
//!
//! `share` / `join` open the data directory directly — they must run
//! while the daemon is stopped (redb holds an exclusive file lock).
//! Sudowork's primary deployment path is the static topology env vars
//! consumed at daemon startup; share/join are operator escape hatches.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use backends::provider::DefaultObjectStoreProvider;
use backends::storage::path_local::PathLocalBackend;
use clap::{Parser, Subcommand};
use kernel::abc::object_store::ObjectStore;
use kernel::hal::object_store_provider::{
    get_provider, set_enabled_drivers, set_provider, ObjectStoreProviderArgs,
};
use kernel::kernel::convenience::{KernelConvenience, MountOptions};
use kernel::kernel::Kernel;

use nexus_raft::distributed_coordinator::{
    bootstrap_or_join_zone, read_or_mint_node_id, validate_bootstrap_mode,
    validate_peers_excludes_self, BootstrapMode,
};
use nexus_raft::federation::{parse_federation_env, ENV_FEDERATION_MOUNTS, ENV_FEDERATION_ZONES};
use nexus_raft::transport::{bootstrap_tls, NodeAddress};
use nexus_raft::{TlsFiles, ZoneManager};

const DEFAULT_BIND: &str = "0.0.0.0:2126";
const TOPOLOGY_TICK: Duration = Duration::from_secs(10);

#[derive(Debug, Parser)]
#[command(
    name = "nexusd-cluster",
    version,
    about = "Nexus cluster-profile daemon (pure Rust runtime)",
    long_about = None,
)]
struct Args {
    #[command(flatten)]
    common: CommonArgs,

    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Debug, clap::Args)]
struct CommonArgs {
    /// This node's hostname. Falls back to NEXUS_HOSTNAME, then OS hostname.
    #[arg(long, env = "NEXUS_HOSTNAME", global = true)]
    hostname: Option<String>,

    /// Bind address for the federation gRPC server.
    #[arg(long, env = "NEXUS_BIND_ADDR", default_value = DEFAULT_BIND, global = true)]
    bind_addr: String,

    /// Persistent data directory (TLS bundle + per-zone redb files).
    #[arg(
        long,
        env = "NEXUS_DATA_DIR",
        default_value = "./nexus-cluster-data",
        global = true
    )]
    data_dir: PathBuf,

    /// Comma-separated raft peers in `id@host:port` form.
    #[arg(long, env = "NEXUS_PEERS", default_value = "", global = true)]
    peers: String,

    /// Disable TLS — plaintext gRPC for local testing only.
    #[arg(long, env = "NEXUS_NO_TLS", default_value_t = false, global = true)]
    no_tls: bool,

    /// Host filesystem directory exposed as the cluster root mount.
    /// `nexusd-cluster` mounts this path at `/` via `PathLocalBackend`
    /// at boot so gRPC writes through DLC land on the host fs.
    /// Defaults to `<data_dir>/root` for self-contained operation.
    #[arg(long, env = "NEXUS_ROOT_FS", global = true)]
    root_path: Option<PathBuf>,

    /// Bootstrap mode declaration — `static`, `dynamic`, or `restart`.
    ///
    /// Operator must declare intent at startup so the daemon does not
    /// silently mix scenarios.  See `BootstrapMode` in `nexus_raft`
    /// for the full contract.  Required for the daemon mode (no
    /// subcommand) — share/join/mount/unmount subcommands skip the
    /// validator since they always operate on existing state.
    #[arg(long, env = "NEXUS_BOOTSTRAP_MODE", global = true)]
    bootstrap_mode: Option<String>,

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
}

impl CommonArgs {
    fn root_fs_path(&self) -> PathBuf {
        self.root_path
            .clone()
            .unwrap_or_else(|| self.data_dir.join("root"))
    }
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Detach a local subtree into a new federation zone.
    ///
    /// The subtree under `<path>` (in the parent zone) is copied into a
    /// new raft group identified by `--zone-id`, with paths rebased so
    /// that what was at `<parent>/<path>/foo` becomes `/foo` inside the
    /// new zone. After share, peers can join the new zone via
    /// `nexusd-cluster join`.
    ///
    /// Pass `--mount-at <path>` to also write a DT_MOUNT entry in the
    /// parent zone's metastore that routes that path to the new zone.
    /// The mount entry is raft-replicated, so every member of the parent
    /// zone (including future joiners) sees the same mount automatically
    /// — symmetric to what `join` does on the joiner side. Without
    /// `--mount-at` the new zone exists as a raft group but the sharer's
    /// own writes to `<path>` keep routing to the original (local)
    /// mount, which is the historical pitfall.
    Share {
        /// Subtree path in the parent zone (e.g. `/data/shared`).
        path: String,
        /// Zone id for the new federation zone.
        #[arg(long)]
        zone_id: String,
        /// Parent zone id; defaults to root.
        #[arg(long, default_value = "root")]
        parent_zone: String,
        /// Optional VFS path to mount the new zone at on this node (the
        /// sharer). Writes a DT_MOUNT entry via the parent zone's raft
        /// state machine, so the mount is visible on every member of
        /// the parent zone. Idempotent.
        #[arg(long)]
        mount_at: Option<String>,
    },
    /// Mount a remote zone at a local path.
    ///
    /// Joins `<remote_zone_id>` (must already exist on `<peer_addr>`),
    /// then writes a DT_MOUNT entry under `<parent_zone>` so syscalls
    /// at `<local_path>` route into the remote zone.
    Join {
        /// Remote peer in `id@host:port` form (e.g. `2@nexus-2:2126`).
        peer_addr: String,
        /// Zone id to join on the remote side.
        remote_zone_id: String,
        /// Local path to mount the remote zone at.
        local_path: String,
        /// Parent zone for the mount entry; defaults to root.
        #[arg(long, default_value = "root")]
        parent_zone: String,
    },
}

fn main() -> Result<()> {
    // Held until `main` returns so the non-blocking log writer thread stays
    // alive and flushes on shutdown.
    let _tracing_guard = install_tracing();
    let args = Args::parse();
    // Size the multi-thread runtime against the host: federation
    // gRPC + raft IO is IO-bound, so the kernel `available_parallelism`
    // estimate (logical cores under cgroup / affinity constraints) is
    // the right target. Falls back to 2 — the previous hard-coded
    // worker count — when the platform can't report a value (e.g.
    // bare-metal probes that aren't WASI-style sandboxed but lack
    // `_SC_NPROCESSORS_ONLN`).
    let workers = contracts::recommended_worker_threads(2);
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .thread_name("nexusd-cluster")
        .build()
        .context("build tokio runtime")?
        .block_on(async move {
            match args.cmd {
                None => run_daemon(args.common).await,
                Some(Cmd::Share {
                    path,
                    zone_id,
                    parent_zone,
                    mount_at,
                }) => {
                    run_share(
                        args.common,
                        &parent_zone,
                        &path,
                        &zone_id,
                        mount_at.as_deref(),
                    )
                    .await
                }
                Some(Cmd::Join {
                    peer_addr,
                    remote_zone_id,
                    local_path,
                    parent_zone,
                }) => {
                    run_join(
                        args.common,
                        &peer_addr,
                        &remote_zone_id,
                        &local_path,
                        &parent_zone,
                    )
                    .await
                }
            }
        })
}

/// Bundle returned by [`open_zone_manager`].  Carries the opaque
/// `node_id` minted/loaded from `<data_dir>/.node_id` plus the
/// structured peer address book and self-address derived from
/// `--bind-addr`/`--hostname`.  `run_daemon` hands the lot to
/// [`bootstrap_or_join_zone`] which owns the actual root-zone
/// dispatch.
struct ZoneManagerBundle {
    zm: std::sync::Arc<ZoneManager>,
    node_id: u64,
    self_address: String,
    peer_addrs: Vec<NodeAddress>,
}

/// Open a `ZoneManager` against the data dir, sharing the daemon's
/// startup conventions. Used by both `daemon` and the offline
/// `share`/`join` subcommands.
///
/// Node identity is read from (or minted into) `<data_dir>/.node_id`
/// via [`read_or_mint_node_id`] — the same SSOT Python `nexusd` uses.
/// Decoupling node_id from hostname is the PR #3996 contract: a
/// wiped-and-rejoined node's fresh random ID has
/// `Progress[new_id].matched=0` from the moment AddNode commits, so
/// heartbeats with `m.commit=0` cannot trip raft-rs 0.7's
/// `commit_to`'s stale-`Progress` panic.
fn open_zone_manager(
    common: &CommonArgs,
    extra_grpc_services: Option<tonic::service::Routes>,
) -> Result<ZoneManagerBundle> {
    std::fs::create_dir_all(&common.data_dir)
        .with_context(|| format!("create data dir {}", common.data_dir.display()))?;

    let hostname = resolve_hostname(common.hostname.as_deref());
    let zones_dir = common
        .data_dir
        .to_str()
        .context("data_dir must be UTF-8")?
        .to_string();

    // Opaque random `node_id` per first boot, persisted to
    // `<data_dir>/.node_id`.  Restart loads the persisted value;
    // wipe-rejoin mints a fresh ID (see fn doc).
    let node_id = read_or_mint_node_id(&zones_dir)
        .map_err(|e| anyhow::anyhow!("read_or_mint_node_id: {}", e))?;

    let use_tls = !common.no_tls;
    let tls = if !use_tls {
        tracing::warn!("TLS disabled (--no-tls / NEXUS_NO_TLS); plaintext gRPC");
        None
    } else {
        let bundle = bootstrap_tls(
            &common.data_dir,
            contracts::ROOT_ZONE_ID,
            &hostname,
            node_id,
        )
        .map_err(|e| anyhow::anyhow!("TLS bootstrap failed: {}", e))?;
        Some(TlsFiles {
            cert_path: bundle.node_cert_path,
            key_path: bundle.node_key_path,
            ca_path: bundle.ca_path.clone(),
            ca_key_path: Some(bundle.ca_key_path),
            join_token_hash: Some(bundle.join_token_hash),
        })
    };

    // Parse `--peers` into structured `NodeAddress` entries — address
    // book only.  ZoneManager seeds its transport peer map from this;
    // ConfState is independent (mutated only by ConfChange via
    // JoinZone driven by `bootstrap_or_join_zone`).
    let peer_addrs: Vec<NodeAddress> = NodeAddress::parse_peer_list(&common.peers, use_tls)
        .map_err(|e| anyhow::anyhow!("--peers/NEXUS_PEERS parse: {}", e))?;
    let peers_str: Vec<String> = peer_addrs
        .iter()
        .map(NodeAddress::to_raft_peer_str)
        .collect();

    // Advertise address — used as `StepMessage.sender_address` so the
    // peer-map runtime SSOT can learn this node's reachable endpoint.
    // Default: `<hostname>:<bind_port>`.
    let bind_port = common
        .bind_addr
        .rsplit_once(':')
        .and_then(|(_, p)| p.parse::<u16>().ok())
        .unwrap_or(2126);
    let self_address = format!("{hostname}:{bind_port}");

    // Reject "self listed in --peers" early — see
    // `validate_peers_excludes_self` for why this is a hard error
    // under the PR #3996 opaque-ID contract.
    validate_peers_excludes_self(&peer_addrs, &self_address)
        .map_err(|e| anyhow::anyhow!("{}", e))?;

    let zm = ZoneManager::with_node_id(
        &hostname,
        node_id,
        &zones_dir,
        peers_str,
        &common.bind_addr,
        tls,
        Some(self_address.clone()),
        extra_grpc_services,
    )
    .map_err(|e| anyhow::anyhow!("ZoneManager::with_node_id: {}", e))?;

    Ok(ZoneManagerBundle {
        zm,
        node_id,
        self_address,
        peer_addrs,
    })
}

async fn run_daemon(common: CommonArgs) -> Result<()> {
    let hostname = resolve_hostname(common.hostname.as_deref());
    tracing::info!(
        hostname = %hostname,
        bind = %common.bind_addr,
        data_dir = %common.data_dir.display(),
        "nexusd-cluster starting (daemon mode)",
    );

    let bootstrap_new = std::env::var("NEXUS_BOOTSTRAP_NEW")
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true"))
        .unwrap_or(false);

    let peers_non_empty = common.peers.split(',').any(|s| !s.trim().is_empty());

    // `<data_dir>/root/raft/` — caller-side check the validator
    // uses to detect "this is actually a restart, not a fresh
    // bootstrap".  Cheap filesystem stat.
    let data_dir_has_root = common.data_dir.join("root").join("raft").exists();

    // Operator MUST declare bootstrap intent.  No implicit dispatch:
    // explicit mode declaration is the SSOT for what kind of boot
    // this is (static = env-driven cluster formation, dynamic =
    // rootless + runtime API, restart = resume from disk).
    let mode_str = common.bootstrap_mode.as_deref().ok_or_else(|| {
        anyhow::anyhow!(
            "--bootstrap-mode (or NEXUS_BOOTSTRAP_MODE) is required.  Pass one of: \
             static, dynamic, restart.  See BootstrapMode docs in nexus_raft.",
        )
    })?;
    let mode = BootstrapMode::parse(mode_str).map_err(|e| anyhow::anyhow!("{}", e))?;
    validate_bootstrap_mode(mode, data_dir_has_root, bootstrap_new, peers_non_empty)
        .map_err(|e| anyhow::anyhow!("{}", e))?;
    tracing::info!(
        mode = mode.as_str(),
        bootstrap_new,
        peers_non_empty,
        data_dir_has_root,
        "bootstrap mode validated",
    );

    // ── ObjectStoreProvider + driver gate ─────────────────────────
    // Registered before the first DT_MOUNT so that any mount going
    // through the provider (bridge-2, #4262) can call get_provider()
    // and is_driver_enabled() at construction time.
    //
    // The default build compiles only path_local + remote (see
    // Cargo.toml). Building with `--features driver-s3` additionally
    // compiles the provider's S3 arm and enables the "s3" gate, so an
    // S3 / S3-compatible (Cloudflare R2, MinIO) DT_MOUNT arriving over
    // gRPC is constructed instead of rejected by the gate.
    set_provider(Arc::new(DefaultObjectStoreProvider))
        .unwrap_or_else(|_| tracing::warn!("ObjectStoreProvider already registered"));
    #[cfg(feature = "driver-s3")]
    set_enabled_drivers(["path_local", "remote", "s3"]);
    #[cfg(not(feature = "driver-s3"))]
    set_enabled_drivers(["path_local", "remote"]);

    // ── Data plane: mount host-fs at "/" via PathLocalBackend ──
    // Created BEFORE ZoneManager so the VFS gRPC service can be
    // co-hosted on the same port as the raft gRPC server.
    let kernel = Arc::new(Kernel::new());
    let root_fs = common.root_fs_path();
    std::fs::create_dir_all(&root_fs)
        .with_context(|| format!("create cluster root mount dir {}", root_fs.display()))?;
    let backend: Arc<dyn ObjectStore> = Arc::new(
        PathLocalBackend::new(&root_fs, /* fsync */ false)
            .with_context(|| format!("PathLocalBackend init at {}", root_fs.display()))?,
    );
    kernel
        .mount("/", MountOptions::new("local").with_backend(backend))
        .map_err(|e| anyhow::anyhow!("mount / via path_local: {:?}", e))?;
    tracing::info!(
        root_fs = %root_fs.display(),
        "mounted host-fs at \"/\" via PathLocalBackend",
    );

    // ── Optional S3-compatible mount declared via NEXUS_S3_* ──
    // Built through the same provider the gRPC bridge uses, so the
    // driver gate fails fast on a slim binary without `driver-s3`.
    // Runs before the VFS gRPC server serves so the mount is live.
    mount_declared_s3(&kernel, &common)?;

    // Build VFS gRPC service as tonic Routes — co-hosted on the raft
    // port via ZoneManager. Uses NoAuth (mTLS is the boundary).
    let vfs_auth: Arc<dyn transport::auth::AuthProvider> = Arc::new(transport::auth::NoAuth);
    let vfs_routes = transport::grpc::build_vfs_routes(
        Arc::clone(&kernel),
        vfs_auth,
        64 * 1024 * 1024,
        "nexusd-cluster",
    );

    let ZoneManagerBundle {
        zm,
        node_id,
        self_address,
        peer_addrs,
    } = open_zone_manager(&common, Some(vfs_routes))?;

    // Bring root zone online based on declared mode.
    //
    //   * Static: dispatch through `bootstrap_or_join_zone` — empty
    //     peers → 1-voter single-node default; non-empty peers →
    //     joiner retry loop.
    //   * Restart: dispatch through `bootstrap_or_join_zone` —
    //     persisted ConfState resumes (branch 1).
    //   * Dynamic: SKIP root bootstrap entirely; daemon comes up
    //     rootless, operator drives `create_zone` via runtime API.
    //
    // `bootstrap_or_join_zone` is a sync helper that may spin a
    // nested `tokio::runtime` for its JoinZone RPCs (joiner branch),
    // which would panic with "Cannot start a runtime from within a
    // runtime" on a worker thread of the outer `#[tokio::main]`.
    // `spawn_blocking` moves it onto the blocking pool where nested
    // runtime creation is allowed.
    if matches!(mode, BootstrapMode::Static | BootstrapMode::Restart) {
        let zm_for_bootstrap = zm.clone();
        let self_addr_for_bootstrap = self_address.clone();
        let peer_addrs_for_bootstrap = peer_addrs.clone();
        tokio::task::spawn_blocking(move || {
            bootstrap_or_join_zone(
                zm_for_bootstrap.as_ref(),
                "root",
                node_id,
                &self_addr_for_bootstrap,
                &peer_addrs_for_bootstrap,
                bootstrap_new,
                /* max_attempts */ None, // daemon boot — retry forever
                /* as_learner   */
                false, // root cluster votes; learners are for share/join
            )
        })
        .await
        .map_err(|e| anyhow::anyhow!("bootstrap join task panicked: {}", e))?
        .map_err(|e| anyhow::anyhow!("bootstrap_or_join_zone: {}", e))?;
    } else {
        tracing::info!(
            "bootstrap mode = dynamic; daemon up rootless — operator drives \
             create_zone via runtime API",
        );
    }

    // `bootstrap_static` — invoked below when federation env vars are
    // set — is `NEXUS_FEDERATION_ZONES`/`_MOUNTS` driven and only
    // meaningful on the founder (`bootstrap_new` true).
    let peers_str: Vec<String> = peer_addrs
        .iter()
        .map(NodeAddress::to_raft_peer_str)
        .collect();

    let (zones, mounts) = parse_federation_env();
    if !zones.is_empty() || !mounts.is_empty() {
        tracing::info!(
            ?zones,
            mount_count = mounts.len(),
            "Bootstrapping static topology from {} / {}",
            ENV_FEDERATION_ZONES,
            ENV_FEDERATION_MOUNTS,
        );
        zm.bootstrap_static(&zones, peers_str.clone(), &mounts)
            .map_err(|e| anyhow::anyhow!("bootstrap_static: {}", e))?;
    }

    // Canonical coordinator boot wiring: self-address publish, DT_MOUNT
    // apply-cb install on every loaded zone (root + env-listed federation
    // zones + zones restored from disk), DT_MOUNT replay, blob-fetcher
    // slot stash + drain, `bootstrap_done` flip.  Without this, DT_MOUNT
    // entries proposed via `share --mount-at` / `join` / `apply_topology`
    // would write into raft state but never reach `VFSRouter`, writes
    // would carry no `last_writer_address`, and ReadBlob would have
    // nothing to serve.  Held until shutdown so the apply-cb closures +
    // their Arc clones see a stable provider lifetime.
    let _dist_coord = {
        let coord = nexus_raft::distributed_coordinator::RaftDistributedCoordinator::new();
        coord.install_with_kernel(zm.clone(), zm.runtime_handle(), &self_address, &kernel);
        coord
    };

    // Outbound peer-blob client — installs a `PeerBlobClient` over
    // the kernel-shared tokio runtime, replacing the `NoopPeerBlobClient`
    // default so `Kernel::try_remote_fetch` can actually fetch bytes
    // from origin nodes on local-backend misses.  Sits above raft in
    // the dep graph; kept out of `install_with_kernel` for that reason.
    transport::peer_blob::install(kernel.as_ref());

    let zm_for_loop = zm.clone();
    let topology_handle = tokio::spawn(async move {
        loop {
            match zm_for_loop.apply_topology(contracts::ROOT_ZONE_ID) {
                Ok(true) => {
                    if !zm_for_loop.pending_mounts().is_empty() {
                        tokio::time::sleep(TOPOLOGY_TICK).await;
                        continue;
                    }
                    tokio::time::sleep(TOPOLOGY_TICK * 6).await;
                }
                Ok(false) => tokio::time::sleep(TOPOLOGY_TICK).await,
                Err(err) => {
                    tracing::warn!(%err, "apply_topology error; will retry");
                    tokio::time::sleep(TOPOLOGY_TICK).await;
                }
            }
        }
    });

    wait_for_shutdown().await;
    tracing::info!("nexusd-cluster shutting down");

    // Stop the convergence loop first — it's a best-effort reconciler,
    // safe to abort mid-tick.
    topology_handle.abort();

    // Drain ZoneManager: signal gRPC + zone transport loops to exit
    // their serve_with_shutdown paths so in-flight raft messages drain
    // cleanly. ZoneManager::shutdown() is synchronous and uses an
    // internal bridge_block_on; call it from spawn_blocking so we
    // don't trigger "Cannot drop a runtime" / nested-runtime panics.
    //
    // 10s cap matches typical k8s preStop / SIGTERM grace windows —
    // if tonic hasn't finished draining by then, force-drop and exit
    // rather than hang the pod.
    //
    // TODO(leader-transfer): on graceful shutdown of a leader we could
    // proactively transfer leadership before drain, sparing the cluster
    // one election round. raft-rs's `MsgTransferLeader` is not exposed
    // through our wrapper today, and `propose_conf_change(RemoveNode,
    // self_id)` would permanently demote the node — wrong semantics
    // for a restart-and-rejoin cycle. Out of scope for this PR; needs
    // a dedicated commitment-timeline test plan.
    let zm_for_drain = zm.clone();
    let drain = tokio::task::spawn_blocking(move || {
        zm_for_drain.shutdown();
    });
    match tokio::time::timeout(Duration::from_secs(10), drain).await {
        Ok(Ok(())) => tracing::info!("ZoneManager drain complete"),
        Ok(Err(join_err)) => tracing::warn!(?join_err, "ZoneManager drain task panicked"),
        Err(_) => tracing::warn!("ZoneManager drain exceeded 10s — forcing exit"),
    }

    // Drop Kernel (which owns a nested tokio Runtime) on a blocking
    // thread — dropping it inside the current async context panics with
    // "Cannot drop a runtime in a context where blocking is not allowed".
    tokio::task::spawn_blocking(move || {
        drop(kernel);
        drop(zm);
    })
    .await
    .ok();

    Ok(())
}

async fn run_share(
    common: CommonArgs,
    parent_zone: &str,
    path: &str,
    new_zone_id: &str,
    mount_at: Option<&str>,
) -> Result<()> {
    let ZoneManagerBundle { zm, peer_addrs, .. } = open_zone_manager(&common, None)?;
    let peers_str: Vec<String> = peer_addrs
        .iter()
        .map(NodeAddress::to_raft_peer_str)
        .collect();

    if zm.get_zone(new_zone_id).is_none() {
        zm.create_zone(new_zone_id, peers_str)
            .map_err(|e| anyhow::anyhow!("create_zone({}): {}", new_zone_id, e))?;
    }

    // No leader-wait dance here — ``share_subtree_core`` owns its
    // leadership precondition internally (waits on ``new_zone_id``,
    // the actual write target).  Reads on ``parent_zone`` are local
    // sequential-consistency, no leader required.
    let copied = zm
        .share_subtree_core(parent_zone, path, new_zone_id)
        .map_err(|e| anyhow::anyhow!("share_subtree: {}", e))?;

    println!(
        "Shared '{}' from zone '{}' as new zone '{}' ({} entries copied)",
        path, parent_zone, new_zone_id, copied
    );

    // Optional self-mount in the same operation. zm.mount writes a
    // DT_MOUNT entry via the parent zone's raft state machine, so the
    // entry replicates to every member — both the sharer's later writes
    // to `mount_path` and any future joiner see the same mount with no
    // extra coordination. Without this step `share` only creates the
    // raft group; the sharer's own writes keep routing to the original
    // (local) mount until some peer's `join` happens to add the entry.
    // Idempotent re-mount to the same target is a no-op (see
    // `zm.mount`).
    if let Some(mount_path) = mount_at {
        zm.mount(parent_zone, mount_path, new_zone_id, true)
            .map_err(|e| anyhow::anyhow!("mount({mount_path}): {e}"))?;
        println!("Mounted zone '{new_zone_id}' at '{mount_path}' in parent zone '{parent_zone}'");
    }
    Ok(())
}

async fn run_join(
    common: CommonArgs,
    peer_addr: &str,
    remote_zone_id: &str,
    local_path: &str,
    parent_zone: &str,
) -> Result<()> {
    let ZoneManagerBundle {
        zm,
        node_id,
        self_address,
        ..
    } = open_zone_manager(&common, None)?;

    // Pre-#3996 (and pre-this commit) ``run_join`` only invoked
    // ``zm.join_zone(remote_zone_id, peers, false)`` — that registers
    // the zone locally with ``skip_bootstrap=true`` but never tells
    // the leader on ``peer_addr`` "I want in".  No JoinZone RPC fires,
    // no AddNode commits, the joiner waits forever after restart.
    //
    // Drive the same SSOT machinery ``run_daemon`` uses for the root
    // zone: ``bootstrap_or_join_zone`` with ``bootstrap_new=false``.  That
    // (a) registers the zone locally with ``skip_bootstrap=true`` so
    // the local gRPC server can serve append-entries from the leader
    // once the membership change commits, then (b) sends ``JoinZone``
    // RPC to ``peer_addr``, then (c) returns once the leader's response
    // confirms the change + the snapshot has installed authoritative
    // ConfState locally.
    //
    // ``as_learner=true`` — `share` / `join` is the owner-pattern
    // subtree-mount flow.  The creator of the shared zone (`share`)
    // is the authoritative single voter; every joiner enters as a
    // Learner so it receives full replication but never participates
    // in quorum.  This makes wipe-rejoin safe by construction —
    // losing or replacing a learner has zero impact on the owner's
    // ability to commit, so SSD swap / OS reinstall / device
    // migration cannot strand the zone in `not leader` deadlock the
    // way the historical 2-voter pattern could (the failure that
    // motivated this change).
    //
    // ``max_attempts=Some(15)`` × ``JOIN_ZONE_RETRY_INTERVAL`` (2 s)
    // ≈ 30 s upper bound on the operator command — long enough to
    // absorb a leader election round on the remote, short enough that
    // a stuck command terminates with a clear error rather than
    // hanging forever like the daemon-boot path does.
    let use_tls = !common.no_tls;
    let peer = NodeAddress::parse(peer_addr, use_tls)
        .map_err(|e| anyhow::anyhow!("--peer-addr parse '{}': {}", peer_addr, e))?;
    let peer_addrs = vec![peer];

    let zm_for_join = zm.clone();
    let self_addr_for_join = self_address.clone();
    let zone_id_for_join = remote_zone_id.to_string();
    tokio::task::spawn_blocking(move || {
        nexus_raft::distributed_coordinator::bootstrap_or_join_zone(
            zm_for_join.as_ref(),
            &zone_id_for_join,
            node_id,
            &self_addr_for_join,
            &peer_addrs,
            /* bootstrap_new */ false,
            /* max_attempts  */ Some(15),
            /* as_learner    */ true,
        )
    })
    .await
    .map_err(|e| anyhow::anyhow!("join task panicked: {}", e))?
    .map_err(|e| anyhow::anyhow!("bootstrap_or_join_zone({}): {}", remote_zone_id, e))?;

    zm.mount(parent_zone, local_path, remote_zone_id, true)
        .map_err(|e| anyhow::anyhow!("mount: {}", e))?;

    println!(
        "Joined remote zone '{}' (via {}); mounted at '{}' inside zone '{}'",
        remote_zone_id, peer_addr, local_path, parent_zone
    );
    Ok(())
}

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

/// Treat a present-but-blank string (an exported-but-empty or
/// whitespace-only env var, e.g. a poorly-templated Secret) as absent,
/// so a required field triggers the env-var-named error and an optional
/// one (prefix / endpoint) falls back to its default instead of building
/// a degenerate backend (literal whitespace prefix / malformed host).
/// Returns the original (untrimmed) value when non-blank.
fn nonempty_owned(v: &Option<String>) -> Option<&str> {
    v.as_deref().filter(|s| !s.trim().is_empty())
}

/// Return the colliding `NEXUS_FEDERATION_MOUNTS` path if `mount_point`
/// (the declared S3 mount) shares a VFS path with a static federation
/// mount. Both resolve to the same canonical key under the root zone, and
/// `VFSRouter::add` overwrites an occupied key — so a collision would let
/// federation topology silently replace the S3 backend. Comparison is
/// trailing-slash-insensitive to match mount-path normalization.
fn federation_mount_collision<'a>(
    mount_point: &str,
    fed_mounts: &'a BTreeMap<String, String>,
) -> Option<&'a str> {
    let mp = mount_point.trim_end_matches('/');
    for path in fed_mounts.keys() {
        if path.trim_end_matches('/') == mp {
            return Some(path.as_str());
        }
    }
    None
}

/// Parse + validate the `NEXUS_S3_*` declaration.
///
///   * `Ok(None)`        — no `NEXUS_S3_BUCKET`; no S3 mount declared.
///   * `Ok(Some(cfg))`   — a fully-validated mount.
///   * `Err(_)`          — bucket set but a required field is missing /
///     empty, or the mount point is illegal. The
///     message names the offending env var.
fn resolve_s3_mount_config(common: &CommonArgs) -> Result<Option<S3MountConfig>> {
    // `NEXUS_S3_BUCKET` is the declaration trigger. Distinguish "truly
    // absent" (`None` → no S3 mount, boot exactly as before) from "present
    // but blank" (`Some("")` / whitespace → a typo'd or empty-Secret config
    // that must fail fast, not silently disable the mount and let `/s3`
    // writes fall through to the local root). clap surfaces an
    // exported-empty env var as `Some("")`, so this case is reachable.
    let bucket = match common.s3_bucket.as_deref() {
        None => return Ok(None),
        Some(b) if b.trim().is_empty() => anyhow::bail!(
            "NEXUS_S3_BUCKET is set but blank; unset it to disable the S3 \
             mount, or provide a bucket name"
        ),
        Some(b) => b,
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

    // Normalize the mount point to the same canonical shape the kernel
    // router uses for lookups, so a configured value can't silently
    // mis-route. The router strips trailing slashes and walks ancestors on
    // lookup; a stored `/s3/` key would never match `/s3/file`, sending
    // writes to the local root instead. Require an absolute path and reject
    // `..` traversal / NUL, matching the rest of the syscall path's
    // path contract.
    let raw_mount = nonempty_owned(&common.s3_mount).unwrap_or("/s3");
    if !raw_mount.starts_with('/') {
        anyhow::bail!(
            "NEXUS_S3_MOUNT must be an absolute path starting with \"/\" \
             (got {raw_mount:?})"
        );
    }
    if raw_mount.split('/').any(|seg| seg == "..") {
        anyhow::bail!("NEXUS_S3_MOUNT must not contain \"..\" path segments (got {raw_mount:?})");
    }
    if raw_mount.contains('\0') {
        anyhow::bail!("NEXUS_S3_MOUNT must not contain NUL bytes");
    }
    let mount_point = raw_mount.trim_end_matches('/');
    if mount_point.is_empty() {
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

    // Fail fast on a mount-point collision with static federation topology:
    // a `NEXUS_FEDERATION_MOUNTS` entry at the same VFS path would later
    // overwrite this backend (`VFSRouter::add` replaces an occupied canonical
    // key), silently re-routing the mount's traffic. Catch it at boot rather
    // than mis-placing data.
    let (_zones, fed_mounts) = parse_federation_env();
    if let Some(fed_path) = federation_mount_collision(&cfg.mount_point, &fed_mounts) {
        anyhow::bail!(
            "NEXUS_S3_MOUNT {:?} collides with a NEXUS_FEDERATION_MOUNTS entry at \
             {fed_path:?}; choose a different S3 mount point",
            cfg.mount_point,
        );
    }

    let provider =
        get_provider().ok_or_else(|| anyhow::anyhow!("no ObjectStoreProvider registered"))?;

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
    // DefaultObjectStoreProvider always returns Some for s3 on Ok; this
    // guard covers a custom provider that signals "no backend" via None.
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
        prefix = cfg.prefix.as_deref().unwrap_or(""),
        endpoint = cfg.endpoint.as_deref().unwrap_or("default (AWS)"),
        "mounted S3-compatible backend",
    );
    Ok(())
}

/// Install the global tracing subscriber with a non-blocking stdout
/// writer. The returned [`WorkerGuard`] MUST be held for the lifetime of
/// the process — dropping it flushes buffered lines and stops the writer
/// thread, so logs emitted after the drop are lost.
///
/// The non-blocking writer hands every log line to a dedicated thread
/// instead of writing stdout inline. Under a slow or stalled stdout sink
/// the default `fmt()` writer blocks the calling tokio worker in a
/// `write()` syscall; at high log frequency that can stall enough workers
/// to starve the gRPC server's accept/handshake path. Decoupling the I/O
/// keeps the runtime responsive regardless of log volume.
fn install_tracing() -> tracing_appender::non_blocking::WorkerGuard {
    let (non_blocking, guard) = tracing_appender::non_blocking(std::io::stdout());
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                tracing_subscriber::EnvFilter::new("nexusd_cluster=info,nexus_raft=info")
            }),
        )
        .with_writer(non_blocking)
        .init();
    guard
}

fn resolve_hostname(cli: Option<&str>) -> String {
    if let Some(h) = cli {
        return h.to_string();
    }
    gethostname::gethostname().to_string_lossy().into_owned()
}

#[cfg(unix)]
async fn wait_for_shutdown() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut sigterm = signal(SignalKind::terminate()).expect("install SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => tracing::info!("Received Ctrl+C"),
        _ = sigterm.recv() => tracing::info!("Received SIGTERM"),
    }
}

#[cfg(not(unix))]
async fn wait_for_shutdown() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("Received Ctrl+C");
}

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
        let cfg = resolve_s3_mount_config(&s3_args())
            .expect("ok")
            .expect("some");
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
        let args = CommonArgs {
            s3_region: None,
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_REGION"), "err was: {err}");
    }

    #[test]
    fn missing_access_key_errors_naming_the_env_var() {
        let args = CommonArgs {
            s3_access_key_id: None,
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_ACCESS_KEY_ID"), "err was: {err}");
    }

    #[test]
    fn missing_secret_key_errors_naming_the_env_var() {
        let args = CommonArgs {
            s3_secret_access_key: None,
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_SECRET_ACCESS_KEY"), "err was: {err}");
    }

    #[test]
    fn root_mount_point_is_rejected() {
        let args = CommonArgs {
            s3_mount: Some("/".into()),
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("sub-path"), "err was: {err}");
    }

    #[test]
    fn trailing_slash_only_mount_point_is_rejected() {
        let args = CommonArgs {
            s3_mount: Some("///".into()),
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("sub-path"), "err was: {err}");
    }

    #[test]
    fn trailing_slash_is_trimmed_to_canonical_mount_point() {
        // `/s3/` must canonicalize to `/s3` — the kernel router strips
        // trailing slashes on lookup, so a stored `/s3/` key would never
        // match `/s3/file` and writes would silently fall through to the
        // local root mount.
        for raw in ["/s3/", "/s3///", "/cloud/data/"] {
            let args = CommonArgs {
                s3_mount: Some(raw.into()),
                ..s3_args()
            };
            let cfg = resolve_s3_mount_config(&args).expect("ok").expect("some");
            assert_eq!(
                cfg.mount_point,
                raw.trim_end_matches('/'),
                "trailing slash not trimmed for {raw:?}"
            );
        }
    }

    #[test]
    fn relative_mount_point_is_rejected() {
        // A non-absolute mount point can't be routed; reject it rather than
        // register a key the syscall path treats as invalid.
        let args = CommonArgs {
            s3_mount: Some("s3".into()),
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("absolute"), "err was: {err}");
    }

    #[test]
    fn parent_dir_traversal_mount_point_is_rejected() {
        let args = CommonArgs {
            s3_mount: Some("/s3/../etc".into()),
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains(".."), "err was: {err}");
    }

    #[test]
    fn truly_absent_bucket_is_no_mount() {
        // Unset NEXUS_S3_BUCKET (None) → no S3 mount declared; boot as before.
        let cfg = resolve_s3_mount_config(&base_args()).expect("ok");
        assert!(cfg.is_none());
    }

    #[test]
    fn present_but_blank_bucket_fails_fast() {
        // An exported-but-empty / whitespace `NEXUS_S3_BUCKET` (e.g. a k8s
        // Secret typo) must fail fast naming the var, NOT silently disable the
        // mount and let `/s3` writes fall through to the local root.
        for blank in ["", "   "] {
            let args = CommonArgs {
                s3_bucket: Some(blank.into()),
                ..s3_args()
            };
            let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
            assert!(err.contains("NEXUS_S3_BUCKET"), "err was: {err}");
        }
    }

    #[test]
    fn whitespace_only_required_fields_fail_fast() {
        // Whitespace-only required fields (e.g. a poorly-templated Secret)
        // must fail fast naming the var, not build a degenerate backend that
        // only errors on first I/O.
        let cases: [(fn(&mut CommonArgs), &str); 3] = [
            (|a| a.s3_region = Some("   ".into()), "NEXUS_S3_REGION"),
            (
                |a| a.s3_access_key_id = Some("  ".into()),
                "NEXUS_S3_ACCESS_KEY_ID",
            ),
            (
                |a| a.s3_secret_access_key = Some(" ".into()),
                "NEXUS_S3_SECRET_ACCESS_KEY",
            ),
        ];
        for (mutate, var) in cases {
            let mut args = s3_args();
            mutate(&mut args);
            let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
            assert!(err.contains(var), "expected {var} in err, got: {err}");
        }
    }

    #[test]
    fn federation_mount_collision_detection() {
        let mut m = BTreeMap::new();
        m.insert("/corp".to_string(), "corp".to_string());
        m.insert("/family".to_string(), "family".to_string());
        // Exact collision.
        assert_eq!(federation_mount_collision("/corp", &m), Some("/corp"));
        // Trailing-slash-insensitive (both sides normalized).
        assert_eq!(federation_mount_collision("/corp/", &m), Some("/corp"));
        // No collision for a distinct path.
        assert_eq!(federation_mount_collision("/s3", &m), None);
        // Empty federation map never collides.
        assert_eq!(federation_mount_collision("/s3", &BTreeMap::new()), None);
    }

    #[test]
    fn empty_string_required_field_is_treated_as_missing() {
        // An exported-but-empty env var arrives as Some(""); it must not
        // build a degenerate backend.
        let args = CommonArgs {
            s3_region: Some(String::new()),
            ..s3_args()
        };
        let err = resolve_s3_mount_config(&args).unwrap_err().to_string();
        assert!(err.contains("NEXUS_S3_REGION"), "err was: {err}");
    }
}
