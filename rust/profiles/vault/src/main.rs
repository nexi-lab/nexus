//! `nexusd-vault` — password-vault profile runtime.
//!
//! Single-purpose binary that hosts `PasswordVaultService` over gRPC
//! on a loopback port. Runs **alongside** (and independent of)
//! `nexusd-cluster` — no federation, no raft, no VFS. Just the
//! encrypted vault for password-agent + sudowork-2 clients to call.
//!
//! Defaults are loopback-only by design: the service currently has no
//! auth layer (the assumption is "loopback = trusted"), so binding to
//! a non-loopback address is refused at startup. Future hardening:
//! add mTLS + an auth provider hook (mirroring nexusd-cluster's
//! `services::auth::NoAuth` → `mTLS as boundary` pattern), then drop
//! the loopback restriction.
//!
//! Data layout (defaults under `--data-dir`):
//!   data_dir/vault.redb       — encrypted entry storage (redb)
//!   data_dir/master.key       — 32-byte AES-256 master key
//!                                (auto-generated on first start)
//!
//! Override `--master-key-path` to put the key elsewhere — e.g. on
//! a Dropbox-synced path so a laptop can read the same vault. See
//! password-agent's README §Cross-box for that workflow.

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use tonic::transport::Server;
use tracing::info;

use services::password_vault::proto::password_vault_service_server::PasswordVaultServiceServer;
use services::password_vault::PasswordVaultServiceImpl;

#[derive(Debug, Parser)]
#[command(
    name = "nexusd-vault",
    version,
    about = "Password-vault profile runtime (gRPC PasswordVaultService on loopback)"
)]
struct Args {
    /// Bind address for the gRPC server. Loopback only (no auth layer
    /// yet — refused at startup if non-loopback).
    #[arg(
        long,
        env = "NEXUS_VAULT_BIND_ADDR",
        default_value = "127.0.0.1:12013"
    )]
    bind_addr: String,

    /// Directory holding `vault.redb`. Created on first start.
    #[arg(
        long,
        env = "NEXUS_VAULT_DATA_DIR",
        default_value = "./nexus-vault-data"
    )]
    data_dir: PathBuf,

    /// Path to the 32-byte AES-256 master key. Auto-generated on first
    /// run if absent. Defaults to `<data_dir>/master.key` if unset.
    #[arg(long, env = "NEXUS_VAULT_MASTER_KEY")]
    master_key_path: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let args = Args::parse();
    let bind: std::net::SocketAddr = args
        .bind_addr
        .parse()
        .with_context(|| format!("parse bind_addr {:?}", args.bind_addr))?;

    if !bind.ip().is_loopback() {
        anyhow::bail!(
            "bind_addr must be loopback (127.0.0.0/8 or ::1) — \
             vault has no auth layer yet; refusing non-loopback bind"
        );
    }

    let master_key_path = args
        .master_key_path
        .unwrap_or_else(|| args.data_dir.join("master.key"));

    info!(
        bind = %bind,
        data_dir = %args.data_dir.display(),
        master_key = %master_key_path.display(),
        "starting nexusd-vault"
    );

    let svc = PasswordVaultServiceImpl::new(&args.data_dir, &master_key_path)
        .context("open vault")?;

    Server::builder()
        .add_service(PasswordVaultServiceServer::new(svc))
        .serve_with_shutdown(bind, async {
            tokio::signal::ctrl_c().await.ok();
            info!("shutdown signal received");
        })
        .await
        .context("gRPC server error")?;

    info!("nexusd-vault exited cleanly");
    Ok(())
}
