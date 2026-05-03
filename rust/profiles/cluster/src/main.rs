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

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use backends::storage::path_local::PathLocalBackend;
use clap::{Parser, Subcommand};
use kernel::abc::object_store::ObjectStore;
use kernel::core::dcache::DT_MOUNT;
use kernel::kernel::Kernel;

use nexus_raft::distributed_coordinator::{
    bootstrap_or_join_root, read_or_mint_node_id, validate_peers_excludes_self,
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
    /// The subtree under `<path>` (in the parent zone) is copied into
    /// a new raft group identified by `--zone-id`, with paths rebased
    /// so that what was at `<parent>/<path>/foo` becomes `/foo` inside
    /// the new zone. After share, peers can join the new zone via
    /// `nexusd-cluster join`.
    Share {
        /// Subtree path in the parent zone (e.g. `/data/shared`).
        path: String,
        /// Zone id for the new federation zone.
        #[arg(long)]
        zone_id: String,
        /// Parent zone id; defaults to root.
        #[arg(long, default_value = "root")]
        parent_zone: String,
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

    /// Mount a backend at `<path>` via DLC (offline — daemon must be stopped).
    ///
    /// Only `--type=path_local` works in this binary because that is
    /// the sole driver compiled in.  Other types produce a clean
    /// "driver `X` not compiled into this binary" error from the
    /// factory.
    Mount {
        /// Mount point inside the cluster's VFS (e.g. `/scratch`).
        path: String,
        /// Driver type — currently only `path_local` is compiled in.
        #[arg(long = "type", default_value = "path_local")]
        driver: String,
        /// Host filesystem directory the mount serves (required for
        /// `path_local`).  No default — the operator names the
        /// directory explicitly to avoid shadowing the boot mount.
        #[arg(long)]
        root: Option<PathBuf>,
        /// Backend name label.  Defaults to `local`.
        #[arg(long, default_value = "local")]
        backend_name: String,
        /// Zone id for the new mount; defaults to root.
        #[arg(long, default_value = "root")]
        zone: String,
    },

    /// Unmount a previously-mounted path (offline — daemon must be stopped).
    ///
    /// Drops the DT_MOUNT entry and its routing/dcache state via
    /// `Kernel::sys_unlink`, mirroring the Python `unmount()` shim.
    Unmount {
        /// Mount point to drop (e.g. `/scratch`).
        path: String,
    },
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    install_tracing();
    let args = Args::parse();
    match args.cmd {
        None => run_daemon(args.common).await,
        Some(Cmd::Share {
            path,
            zone_id,
            parent_zone,
        }) => run_share(args.common, &parent_zone, &path, &zone_id).await,
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
        Some(Cmd::Mount {
            path,
            driver,
            root,
            backend_name,
            zone,
        }) => run_mount(args.common, &path, &driver, root.as_deref(), &backend_name, &zone),
        Some(Cmd::Unmount { path }) => run_unmount(args.common, &path),
    }
}

/// Bundle returned by [`open_zone_manager`].  Carries the opaque
/// `node_id` minted/loaded from `<data_dir>/.node_id` plus the
/// structured peer address book and self-address derived from
/// `--bind-addr`/`--hostname`.  `run_daemon` hands the lot to
/// [`bootstrap_or_join_root`] which owns the actual root-zone
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
fn open_zone_manager(common: &CommonArgs) -> Result<ZoneManagerBundle> {
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
    // JoinZone driven by `bootstrap_or_join_root`).
    let peer_addrs: Vec<NodeAddress> = NodeAddress::parse_peer_list(&common.peers, use_tls)
        .map_err(|e| anyhow::anyhow!("--peers/NEXUS_PEERS parse: {}", e))?;
    let peers_str: Vec<String> = peer_addrs.iter().map(NodeAddress::to_raft_peer_str).collect();

    // Advertise address — used as `StepMessage.sender_address` so the
    // peer-map runtime SSOT can learn this node's reachable endpoint.
    // Default mirrors `init_from_env`: `<hostname>:<bind_port>`.
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

    let ZoneManagerBundle {
        zm,
        node_id,
        self_address,
        peer_addrs,
    } = open_zone_manager(&common)?;

    // Bring root zone online via the same dispatcher Python `nexusd`
    // uses (`init_from_env`).  Three branches by storage state:
    //
    //   1. restart — persisted ConfState resumes;
    //   2. fresh create — `NEXUS_BOOTSTRAP_NEW=1` honored, 1-voter
    //      cluster of `node_id`;
    //   3. wait-and-join — empty storage + flag unset + non-empty
    //      peer list = retry JoinZone forever (no deadline).
    //
    // Without this, the previous unconditional `zm.create_zone(...)`
    // produced a fresh 1-voter zone on every joiner, partitioning
    // the federation into disjoint single-node clusters.
    let bootstrap_new = std::env::var("NEXUS_BOOTSTRAP_NEW")
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true"))
        .unwrap_or(false);

    // `bootstrap_or_join_root` is a sync helper that, in branch 3
    // (wait-and-join), spins a nested `tokio::runtime` to drive the
    // JoinZone retry loop's RPCs.  Python `nexusd::init_from_env`
    // calls it from a sync PyO3 entry point — no outer runtime — so
    // the nested-runtime build is allowed.  `nexusd-cluster::main` is
    // `#[tokio::main]`, so calling the sync helper from this `async`
    // function would build the nested runtime *inside* the outer
    // worker pool and trip tokio's "Cannot start a runtime from
    // within a runtime" panic.  Move the call to `spawn_blocking`:
    // the blocking thread is outside the worker pool, so nested
    // runtime creation is allowed (and the worker pool is not held
    // while branch 3 retries indefinitely).
    let zm_for_bootstrap = zm.clone();
    let self_addr_for_bootstrap = self_address.clone();
    let peer_addrs_for_bootstrap = peer_addrs.clone();
    tokio::task::spawn_blocking(move || {
        bootstrap_or_join_root(
            zm_for_bootstrap.as_ref(),
            node_id,
            &self_addr_for_bootstrap,
            &peer_addrs_for_bootstrap,
            bootstrap_new,
        )
    })
    .await
    .map_err(|e| anyhow::anyhow!("bootstrap join task panicked: {}", e))?
    .map_err(|e| anyhow::anyhow!("bootstrap_or_join_root: {}", e))?;

    // `bootstrap_static` — invoked below when federation env vars are
    // set — is `NEXUS_FEDERATION_ZONES`/`_MOUNTS` driven and only
    // meaningful on the founder; mirrors the `init_from_env` guard.
    let peers_str: Vec<String> = peer_addrs.iter().map(NodeAddress::to_raft_peer_str).collect();

    // ── Data plane: mount host-fs at "/" via PathLocalBackend ──
    // Cluster binary's compiled-in feature set is exactly
    // ["driver-path-local"] (see Cargo.toml), so this mount call is the
    // only `ObjectStore` impl available — connectors / S3 / remote are
    // not linked.  No `set_enabled_drivers` needed: the runtime gate is
    // a Python construct (DeploymentProfile-driven), and the cluster
    // binary intentionally bypasses it in favour of the compile-time
    // feature gate.
    let kernel = Arc::new(Kernel::new());
    let root_fs = common.root_fs_path();
    std::fs::create_dir_all(&root_fs).with_context(|| {
        format!("create cluster root mount dir {}", root_fs.display())
    })?;
    let backend: Arc<dyn ObjectStore> = Arc::new(
        PathLocalBackend::new(&root_fs, /* fsync */ false)
            .with_context(|| format!("PathLocalBackend init at {}", root_fs.display()))?,
    );
    kernel
        .sys_setattr(
            "/",
            DT_MOUNT as i32,
            "local",
            Some(backend),
            None, // metastore — kernel falls back to its in-memory MetaStore
            None, // raft_backend — federation wiring is a follow-up
            "memory",
            contracts::ROOT_ZONE_ID,
            false, // is_external
            0,     // capacity
            None,  // read_fd
            None,  // write_fd
            None,  // mime_type
            None,  // modified_at_ms
            None,  // link_target
            None,  // source — same-zone local mount
            None,  // remote_metastore
        )
        .map_err(|e| anyhow::anyhow!("mount / via path_local: {:?}", e))?;
    tracing::info!(
        root_fs = %root_fs.display(),
        "mounted host-fs at \"/\" via PathLocalBackend",
    );

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
    topology_handle.abort();
    tracing::info!("nexusd-cluster shutting down");

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
) -> Result<()> {
    let ZoneManagerBundle { zm, peer_addrs, .. } = open_zone_manager(&common)?;
    let peers_str: Vec<String> = peer_addrs.iter().map(NodeAddress::to_raft_peer_str).collect();

    if zm.get_zone(new_zone_id).is_none() {
        zm.create_zone(new_zone_id, peers_str)
            .map_err(|e| anyhow::anyhow!("create_zone({}): {}", new_zone_id, e))?;
    }
    let copied = zm
        .share_subtree_core(parent_zone, path, new_zone_id)
        .map_err(|e| anyhow::anyhow!("share_subtree: {}", e))?;

    println!(
        "Shared '{}' from zone '{}' as new zone '{}' ({} entries copied)",
        path, parent_zone, new_zone_id, copied
    );
    Ok(())
}

/// Construct an `ObjectStore` for a driver name + local-root the cluster
/// binary's compiled-in feature set actually supports.  The match below
/// will grow as more `driver-*` features land in `Cargo.toml`.
fn build_local_backend(
    driver: &str,
    root: &std::path::Path,
) -> Result<Arc<dyn ObjectStore>> {
    match driver {
        "path_local" => {
            std::fs::create_dir_all(root)
                .with_context(|| format!("create mount root {}", root.display()))?;
            let b = PathLocalBackend::new(root, /* fsync */ false)
                .with_context(|| format!("PathLocalBackend init at {}", root.display()))?;
            Ok(Arc::new(b) as Arc<dyn ObjectStore>)
        }
        other => anyhow::bail!(
            "driver `{}` not compiled into this binary (cluster binary ships only path_local)",
            other
        ),
    }
}

fn run_mount(
    common: CommonArgs,
    mount_point: &str,
    driver: &str,
    local_root: Option<&std::path::Path>,
    backend_name: &str,
    zone: &str,
) -> Result<()> {
    // Open ZoneManager offline so the mount entry is written through
    // the same redb file the daemon will reload on next start.  Same
    // pattern as the `share` / `join` subcommands.
    let _bundle = open_zone_manager(&common)?;
    let kernel = Arc::new(Kernel::new());
    let root = local_root
        .ok_or_else(|| anyhow::anyhow!("--root is required for driver `{driver}`"))?;
    let backend = build_local_backend(driver, root)?;
    kernel
        .sys_setattr(
            mount_point,
            DT_MOUNT as i32,
            backend_name,
            Some(backend),
            None,
            None,
            "memory",
            zone,
            false,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None, // remote_metastore
        )
        .map_err(|e| anyhow::anyhow!("mount {mount_point}: {:?}", e))?;
    println!(
        "Mounted '{}' (zone='{}', driver='{}', root='{}')",
        mount_point,
        zone,
        driver,
        root.display()
    );
    Ok(())
}

fn run_unmount(common: CommonArgs, mount_point: &str) -> Result<()> {
    let _bundle = open_zone_manager(&common)?;
    let kernel = Arc::new(Kernel::new());
    let ctx = contracts::OperationContext::new(
        /* user_id */ "operator",
        /* zone_id */ "root",
        /* is_admin */ true,
        /* agent_id */ None,
        /* is_system */ true,
    );
    let res = kernel
        .sys_unlink(mount_point, &ctx, /* recursive */ false)
        .map_err(|e| anyhow::anyhow!("unmount {mount_point}: {:?}", e))?;
    if res.hit {
        println!("Unmounted '{}' (entry_type={})", mount_point, res.entry_type);
    } else {
        anyhow::bail!("'{}' is not a mount point on this node", mount_point);
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
    let ZoneManagerBundle { zm, .. } = open_zone_manager(&common)?;
    // Treat the supplied peer as the only known voter; raft will
    // discover the rest via the remote's ConfState once we propose.
    let peers = vec![peer_addr.to_string()];

    if zm.get_zone(remote_zone_id).is_none() {
        // CLI `join` defaults to full voter; learner promotion is reserved
        // for the gRPC path (PyZoneManager.join_zone exposes the flag).
        zm.join_zone(remote_zone_id, peers, false)
            .map_err(|e| anyhow::anyhow!("join_zone({}): {}", remote_zone_id, e))?;
    }
    zm.mount(parent_zone, local_path, remote_zone_id, true)
        .map_err(|e| anyhow::anyhow!("mount: {}", e))?;

    println!(
        "Joined remote zone '{}' (via {}); mounted at '{}' inside zone '{}'",
        remote_zone_id, peer_addr, local_path, parent_zone
    );
    Ok(())
}

fn install_tracing() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                tracing_subscriber::EnvFilter::new("nexusd_cluster=info,nexus_raft=info")
            }),
        )
        .init();
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
