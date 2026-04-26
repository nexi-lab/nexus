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
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use nexus_raft::federation::{parse_federation_env, ENV_FEDERATION_MOUNTS, ENV_FEDERATION_ZONES};
use nexus_raft::transport::{bootstrap_tls, hostname_to_node_id};
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
    }
}

/// Open a `ZoneManager` against the data dir, sharing the daemon's
/// startup conventions. Used by both `daemon` and the offline
/// `share`/`join` subcommands.
fn open_zone_manager(common: &CommonArgs) -> Result<std::sync::Arc<ZoneManager>> {
    std::fs::create_dir_all(&common.data_dir)
        .with_context(|| format!("create data dir {}", common.data_dir.display()))?;

    let hostname = resolve_hostname(common.hostname.as_deref());
    let node_id = hostname_to_node_id(&hostname);

    let tls = if common.no_tls {
        tracing::warn!("TLS disabled (--no-tls / NEXUS_NO_TLS); plaintext gRPC");
        None
    } else {
        let bundle =
            bootstrap_tls(&common.data_dir, contracts::ROOT_ZONE_ID, &hostname, node_id)
                .map_err(|e| anyhow::anyhow!("TLS bootstrap failed: {}", e))?;
        Some(TlsFiles {
            cert_path: bundle.node_cert_path,
            key_path: bundle.node_key_path,
            ca_path: bundle.ca_path.clone(),
            ca_key_path: Some(bundle.ca_key_path),
            join_token_hash: Some(bundle.join_token_hash),
        })
    };

    let peers: Vec<String> = common
        .peers
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();

    ZoneManager::new(
        &hostname,
        common
            .data_dir
            .to_str()
            .context("data_dir must be UTF-8")?,
        peers,
        &common.bind_addr,
        tls,
    )
    .map_err(|e| anyhow::anyhow!("ZoneManager init failed: {}", e))
}

async fn run_daemon(common: CommonArgs) -> Result<()> {
    let hostname = resolve_hostname(common.hostname.as_deref());
    tracing::info!(
        hostname = %hostname,
        bind = %common.bind_addr,
        data_dir = %common.data_dir.display(),
        "nexusd-cluster starting (daemon mode)",
    );

    let peers: Vec<String> = common
        .peers
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();

    let zm = open_zone_manager(&common)?;

    if zm.get_zone(contracts::ROOT_ZONE_ID).is_none() {
        zm.create_zone(contracts::ROOT_ZONE_ID, peers.clone())
            .map_err(|e| anyhow::anyhow!("create root zone: {}", e))?;
        tracing::info!("Created root zone");
    }

    let (zones, mounts) = parse_federation_env();
    if !zones.is_empty() || !mounts.is_empty() {
        tracing::info!(
            ?zones,
            mount_count = mounts.len(),
            "Bootstrapping static topology from {} / {}",
            ENV_FEDERATION_ZONES,
            ENV_FEDERATION_MOUNTS,
        );
        zm.bootstrap_static(&zones, peers, &mounts)
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
    Ok(())
}

async fn run_share(
    common: CommonArgs,
    parent_zone: &str,
    path: &str,
    new_zone_id: &str,
) -> Result<()> {
    let zm = open_zone_manager(&common)?;
    let peers: Vec<String> = common
        .peers
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();

    if zm.get_zone(new_zone_id).is_none() {
        zm.create_zone(new_zone_id, peers)
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

async fn run_join(
    common: CommonArgs,
    peer_addr: &str,
    remote_zone_id: &str,
    local_path: &str,
    parent_zone: &str,
) -> Result<()> {
    let zm = open_zone_manager(&common)?;
    // Treat the supplied peer as the only known voter; raft will
    // discover the rest via the remote's ConfState once we propose.
    let peers = vec![peer_addr.to_string()];

    if zm.get_zone(remote_zone_id).is_none() {
        zm.join_zone(remote_zone_id, peers)
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
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("nexusd_cluster=info,nexus_raft=info")),
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
