//! `nexusd-vault` — standalone PasswordVaultService gRPC server.
//!
//! Hosts both `PasswordVaultService` and `GenericSecretsService` on a
//! loopback gRPC endpoint (no auth layer — refuses non-loopback bind).
//!
//! Data layout (under `<data_dir>/vault/`):
//!   vault-meta.redb  — private kernel metastore
//!   content/         — PathLocalBackend (encrypted entry blobs)
//!   master.key       — 32-byte AES-256 master key (auto-generated)
//!
//! Designed to be spawned on-demand by `password-agent` for ~1s RPCs,
//! so Dropbox sees the redb file closed >99% of the time.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use clap::Parser;
use tonic::service::interceptor::InterceptedService;
use tonic::transport::Server;

use nexus_vault::idle::{wait_for_shutdown, IdleTracker, ShutdownReason};
use services::generic_secrets::proto::generic_secrets_service_server::GenericSecretsServiceServer;
use services::generic_secrets::GenericSecretsServiceImpl;
use services::password_vault::proto::password_vault_service_server::PasswordVaultServiceServer;
use services::password_vault::PasswordVaultServiceImpl;

#[derive(Parser)]
#[command(
    name = "nexusd-vault",
    about = "Password-vault gRPC server (PasswordVaultService + GenericSecretsService on loopback)"
)]
struct Args {
    /// Bind address for the gRPC server.
    /// Loopback only (no auth layer yet — refused at startup if non-loopback).
    #[arg(long, env = "NEXUS_VAULT_BIND_ADDR", default_value = "127.0.0.1:12013")]
    bind_addr: SocketAddr,

    /// Data directory. Vault data lives under `<data_dir>/vault/`.
    /// Created on first start.
    #[arg(
        long,
        env = "NEXUS_VAULT_DATA_DIR",
        default_value = "./nexus-vault-data"
    )]
    data_dir: PathBuf,

    /// Path to the 32-byte AES-256 master key.
    /// Auto-generated on first run if absent.
    /// Defaults to `<data_dir>/vault/master.key` if unset.
    #[arg(long, env = "NEXUS_VAULT_MASTER_KEY")]
    master_key_path: Option<PathBuf>,

    /// Shut down when idle for this many seconds (0 = disabled).
    ///
    /// Use this on hosts whose `--data-dir` lives on file-level cloud-synced
    /// storage (Dropbox / OneDrive / iCloud / Syncthing / NFS). The OS holds
    /// exclusive locks on `vault-meta.redb` while the daemon runs; a
    /// long-running daemon starves the sync client. With this set, the
    /// daemon exits when traffic has been quiet long enough that the sync
    /// client can publish a clean state. Forward note: this is an interim
    /// story until nexus-vfs federation supplants file-level cloud sync.
    ///
    /// Should be set higher than the longest expected RPC duration. Typical
    /// values: 30 (interactive on-demand), 300 (back-to-back batch).
    #[arg(long, env = "NEXUS_VAULT_IDLE_SHUTDOWN_SECONDS", default_value_t = 0)]
    idle_shutdown_seconds: u64,
}

/// gRPC interceptor that marks the shared `IdleTracker` active on every
/// inbound request. Cheap (a single `Relaxed` atomic store) and stateless.
#[derive(Clone)]
struct BumpInterceptor(IdleTracker);

impl tonic::service::Interceptor for BumpInterceptor {
    fn call(
        &mut self,
        req: tonic::Request<()>,
    ) -> Result<tonic::Request<()>, tonic::Status> {
        self.0.bump();
        Ok(req)
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();

    let args = Args::parse();

    // Refuse non-loopback bind — vault has no auth layer.
    if !args.bind_addr.ip().is_loopback() {
        eprintln!(
            "bind_addr must be loopback (127.0.0.0/8 or ::1) — \
             vault has no auth layer yet; refusing non-loopback bind"
        );
        std::process::exit(1);
    }

    // Set up vault storage.
    let vault_dir = args.data_dir.join("vault");
    std::fs::create_dir_all(&vault_dir)?;

    let kernel = Arc::new(kernel::kernel::Kernel::new());
    let meta_path = vault_dir.join("vault-meta.redb");
    kernel
        .set_metastore_path(
            meta_path
                .to_str()
                .ok_or("vault-meta.redb path not valid UTF-8")?,
        )
        .map_err(|e| format!("set_metastore_path: {e:?}"))?;

    let content_dir = vault_dir.join("content");
    let backend: Arc<dyn kernel::abc::object_store::ObjectStore> = Arc::new(
        backends::storage::path_local::PathLocalBackend::new(&content_dir, true)
            .map_err(|e| format!("PathLocalBackend: {e:?}"))?,
    );
    let backend_name = backend.name().to_string();

    kernel
        .sys_setattr(
            "/vault",
            /* DT_MOUNT */ 2,
            &backend_name,
            Some(backend),
            None,
            None,
            "memory",
            "root",
            false,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .map_err(|e| format!("mount /vault: {e:?}"))?;

    let master_key_path = args
        .master_key_path
        .unwrap_or_else(|| vault_dir.join("master.key"));
    let master_key = services::password_vault::crypto::load_or_create_master_key(&master_key_path)?;

    let secrets_svc =
        GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key)?;
    let svc = PasswordVaultServiceImpl::new_with_secrets(secrets_svc.clone());

    tracing::info!(
        vault_dir = %vault_dir.display(),
        bind_addr = %args.bind_addr,
        idle_shutdown_seconds = args.idle_shutdown_seconds,
        "starting nexusd-vault"
    );

    let tracker = IdleTracker::new();
    let pwd_intercepted = InterceptedService::new(
        PasswordVaultServiceServer::new(svc),
        BumpInterceptor(tracker.clone()),
    );
    let sec_intercepted = InterceptedService::new(
        GenericSecretsServiceServer::new(secrets_svc),
        BumpInterceptor(tracker.clone()),
    );

    let signal = async {
        tokio::signal::ctrl_c()
            .await
            .expect("install SIGINT handler");
    };
    let idle_shutdown_seconds = args.idle_shutdown_seconds;
    let shutdown_fut = async move {
        match wait_for_shutdown(tracker, signal, idle_shutdown_seconds).await {
            ShutdownReason::Signal => tracing::info!("shutdown signal received"),
            ShutdownReason::Idle { idle_for_secs } => {
                tracing::info!(idle_for_secs, "idle threshold reached — initiating shutdown");
            }
        }
    };

    Server::builder()
        .add_service(pwd_intercepted)
        .add_service(sec_intercepted)
        .serve_with_shutdown(args.bind_addr, shutdown_fut)
        .await?;

    tracing::info!("nexusd-vault exited cleanly");
    Ok(())
}
