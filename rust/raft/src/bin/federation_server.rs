//! Nexus Federation Server — single-node bootstrap binary.
//!
//! Starts a ``ZoneRaftRegistry`` on this node, creates the root zone,
//! and serves the raft gRPC transport until SIGINT. Intended for
//! operators who run the first node of a federation cluster over a
//! WireGuard link — TLS is off by default (the tunnel provides the
//! encryption + auth boundary). Peer nodes join later via
//! ``ZoneApiService.JoinZone``.
//!
//! Replaces ``scripts/federation_server.py`` — same env-var surface,
//! same defaults, but runs as a native Rust binary with no Python /
//! maturin dependency at runtime.
//!
//! # Env vars
//!
//! | Variable | Default | Meaning |
//! |----------|---------|---------|
//! | ``NEXUS_HOSTNAME`` | ``gethostname()`` | Advertised hostname. |
//! | ``NEXUS_BIND_ADDR`` | ``10.99.0.1:2126`` | gRPC bind address (WireGuard IP). |
//! | ``NEXUS_DATA_DIR`` | ``~/.nexus/federation/data/zones`` | Zone storage directory. |
//!
//! # Usage
//!
//! ```bash
//! NEXUS_BIND_ADDR=10.99.0.1:2126 nexus-federation-server
//! ```

use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("nexus_raft=info".parse()?)
                .add_directive("tonic=info".parse()?),
        )
        .init();

    let hostname = env::var("NEXUS_HOSTNAME")
        .unwrap_or_else(|_| gethostname::gethostname().to_string_lossy().into_owned());
    let node_id = nexus_raft::transport::hostname_to_node_id(&hostname);

    let bind_addr: SocketAddr = env::var("NEXUS_BIND_ADDR")
        .unwrap_or_else(|_| "10.99.0.1:2126".to_string())
        .parse()
        .expect("NEXUS_BIND_ADDR must be a valid socket address");

    // Default data dir: ~/.nexus/federation/data/zones, matching the
    // prior Python script so existing on-disk state is picked up.
    let data_dir = env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| {
        let home = env::var("HOME")
            .or_else(|_| env::var("USERPROFILE"))
            .unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home)
            .join(".nexus")
            .join("federation")
            .join("data")
            .join("zones")
            .to_string_lossy()
            .into_owned()
    });
    let data_path = PathBuf::from(&data_dir);
    std::fs::create_dir_all(&data_path)?;

    tracing::info!(
        "Nexus Federation Server\n  Hostname: {}\n  Node ID:  {}\n  Bind:     {}\n  Data:     {}",
        hostname,
        node_id,
        bind_addr,
        data_path.display(),
    );

    #[cfg(all(feature = "grpc", has_protos))]
    {
        use nexus_raft::raft::ZoneRaftRegistry;
        use nexus_raft::transport::{RaftGrpcServer, ServerConfig};

        let registry = Arc::new(ZoneRaftRegistry::new(data_path, node_id));
        let runtime_handle = tokio::runtime::Handle::current();

        // Root zone — no peers, so raft-rs bootstraps ConfState as a
        // single-node group and self-elects. Peer nodes join later via
        // the JoinZone RPC once they come online.
        registry
            .create_zone(contracts::ROOT_ZONE_ID, vec![], &runtime_handle)
            .await
            .map_err(|e| format!("create root zone: {}", e))?;

        let server = RaftGrpcServer::new(
            registry.clone(),
            ServerConfig {
                bind_address: bind_addr,
                tls: None, // WireGuard provides the link-layer security.
                ..Default::default()
            },
        );

        let shutdown_registry = registry.clone();
        let shutdown = async move {
            tokio::signal::ctrl_c()
                .await
                .expect("install Ctrl+C handler");
            tracing::info!("Shutdown signal received");
            shutdown_registry.shutdown_all();
        };

        tracing::info!("Waiting for peer nodes to join...  (Ctrl+C to stop)");
        server
            .serve_with_shutdown(shutdown)
            .await
            .map_err(|e| format!("server error: {}", e))?;
        tracing::info!("Server stopped");
    }

    #[cfg(not(all(feature = "grpc", has_protos)))]
    {
        eprintln!("Error: nexus-federation-server requires the 'grpc' feature + proto files.");
        eprintln!("Build with: cargo build --features grpc --bin nexus-federation-server");
        return Err("grpc feature or proto files not available".into());
    }

    Ok(())
}
