//! Nexus cluster-profile runtime — `nexusd-cluster`.
//!
//! A self-contained ~5 MB Rust binary that brings up:
//!   * [`nexus_raft::ZoneManager`] (multi-zone Raft + gRPC server)
//!   * Day-1 TLS bootstrap (CA + node cert + join token) on first start
//!   * Static topology (`NEXUS_FEDERATION_ZONES` + `NEXUS_FEDERATION_MOUNTS`)
//!   * Health-check loop that drives `apply_topology` to convergence
//!
//! Designed for sudowork-style integration: drop the binary in
//! `$PATH`, point `NEXUS_DATA_DIR` at a writable directory, set
//! `NEXUS_HOSTNAME` + `NEXUS_PEERS`, and the federation forms itself.
//!
//! Subcommands beyond the daemon (`share`, `join`) land in C2.

use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::Parser;

use nexus_raft::federation::{parse_federation_env, ENV_FEDERATION_MOUNTS, ENV_FEDERATION_ZONES};
use nexus_raft::transport::{bootstrap_tls, hostname_to_node_id};
use nexus_raft::{TlsFiles, ZoneManager};

/// Default Raft / federation gRPC port. Matches witness binary +
/// PYthon NEXUS_BIND_ADDR convention (`:2126`).
const DEFAULT_BIND: &str = "0.0.0.0:2126";

/// Apply-topology retry interval. Each tick, the cluster binary tries
/// to land any pending DT_MOUNT writes. Aligned with the Python
/// health-check cadence (~10 s) so the human-visible convergence
/// window matches what operators are used to.
const TOPOLOGY_TICK: Duration = Duration::from_secs(10);

#[derive(Debug, Parser)]
#[command(
    name = "nexusd-cluster",
    version,
    about = "Nexus cluster-profile daemon (pure Rust runtime)",
    long_about = None,
)]
struct Args {
    /// This node's hostname. Falls back to `NEXUS_HOSTNAME`, then to
    /// the OS hostname. Must be unique across the federation and
    /// match the host's entry in `--peers` for raft membership.
    #[arg(long, env = "NEXUS_HOSTNAME")]
    hostname: Option<String>,

    /// Bind address for the federation gRPC server.
    #[arg(long, env = "NEXUS_BIND_ADDR", default_value = DEFAULT_BIND)]
    bind_addr: String,

    /// Persistent data directory. Holds `tls/` (CA + node certs +
    /// join token) and per-zone redb files.
    #[arg(long, env = "NEXUS_DATA_DIR", default_value = "./nexus-cluster-data")]
    data_dir: PathBuf,

    /// Comma-separated raft peers in `id@host:port` form. All cluster
    /// nodes must use identical lists.
    #[arg(long, env = "NEXUS_PEERS", default_value = "")]
    peers: String,

    /// Disable TLS — plaintext gRPC for local testing only. Production
    /// deployments must leave TLS on (the default).
    #[arg(long, env = "NEXUS_NO_TLS", default_value_t = false)]
    no_tls: bool,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    install_tracing();

    let args = Args::parse();
    let hostname = resolve_hostname(args.hostname.as_deref());
    let node_id = hostname_to_node_id(&hostname);

    std::fs::create_dir_all(&args.data_dir)
        .with_context(|| format!("create data dir {}", args.data_dir.display()))?;

    tracing::info!(
        hostname = %hostname,
        node_id,
        bind = %args.bind_addr,
        data_dir = %args.data_dir.display(),
        "nexusd-cluster starting",
    );

    let tls = if args.no_tls {
        tracing::warn!("TLS disabled (--no-tls / NEXUS_NO_TLS); plaintext gRPC");
        None
    } else {
        let bundle = bootstrap_tls(&args.data_dir, contracts::ROOT_ZONE_ID, &hostname, node_id)
            .map_err(|e| anyhow::anyhow!("TLS bootstrap failed: {}", e))?;
        Some(TlsFiles {
            cert_path: bundle.node_cert_path,
            key_path: bundle.node_key_path,
            ca_path: bundle.ca_path.clone(),
            ca_key_path: Some(bundle.ca_key_path),
            join_token_hash: Some(bundle.join_token_hash),
        })
    };

    let peers: Vec<String> = args
        .peers
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();

    let zm = ZoneManager::new(
        &hostname,
        args.data_dir
            .to_str()
            .context("data_dir must be UTF-8")?,
        peers.clone(),
        &args.bind_addr,
        tls,
    )
    .map_err(|e| anyhow::anyhow!("ZoneManager init failed: {}", e))?;

    // Root zone always — every federation pivots on it.
    if zm.get_zone(contracts::ROOT_ZONE_ID).is_none() {
        zm.create_zone(contracts::ROOT_ZONE_ID, peers.clone())
            .map_err(|e| anyhow::anyhow!("create root zone: {}", e))?;
        tracing::info!("Created root zone");
    }

    // Static topology from env (sudowork supplies these per node).
    let (zones, mounts) = parse_federation_env();
    if !zones.is_empty() || !mounts.is_empty() {
        tracing::info!(
            ?zones,
            mount_count = mounts.len(),
            "Bootstrapping static topology from {} / {}",
            ENV_FEDERATION_ZONES,
            ENV_FEDERATION_MOUNTS,
        );
        zm.bootstrap_static(&zones, peers.clone(), &mounts)
            .map_err(|e| anyhow::anyhow!("bootstrap_static: {}", e))?;
    }

    // Convergence loop: drive apply_topology until it returns true,
    // then keep checking on a slow tick (mounts can be added at runtime
    // via gRPC — that path proposes through raft and lands on every
    // node's apply callback, but a fresh `bootstrap_static` call on a
    // restart still needs a few ticks to fully converge).
    let zm_for_loop = zm.clone();
    let topology_handle = tokio::spawn(async move {
        loop {
            match zm_for_loop.apply_topology(contracts::ROOT_ZONE_ID) {
                Ok(true) => {
                    if !zm_for_loop.pending_mounts().is_empty() {
                        // Someone called bootstrap_static again — keep ticking.
                        tokio::time::sleep(TOPOLOGY_TICK).await;
                        continue;
                    }
                    // Nothing pending; sleep a longer interval before next health probe.
                    tokio::time::sleep(TOPOLOGY_TICK * 6).await;
                }
                Ok(false) => {
                    tokio::time::sleep(TOPOLOGY_TICK).await;
                }
                Err(err) => {
                    tracing::warn!(%err, "apply_topology error; will retry");
                    tokio::time::sleep(TOPOLOGY_TICK).await;
                }
            }
        }
    });

    // Wait for shutdown (Ctrl+C or SIGTERM).
    wait_for_shutdown().await;
    topology_handle.abort();
    tracing::info!("nexusd-cluster shutting down");
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
