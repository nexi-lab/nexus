//! Nexus Witness Node
//!
//! A lightweight Raft witness that participates in leader election
//! but doesn't apply state machine. This enables cost-effective high availability
//! with only 2 full nodes + 1 witness.
//!
//! # What is a Witness?
//!
//! - Votes in leader elections
//! - Stores Raft log (for vote validation)
//! - Does NOT apply state machine
//! - Does NOT serve reads
//! - Cannot become leader
//!
//! # Usage
//!
//! ```bash
//! NEXUS_NODE_ID=3 NEXUS_BIND_ADDR=0.0.0.0:2028 \
//!   NEXUS_PEERS=1@http://10.0.0.1:2026,2@http://10.0.0.2:2026 \
//!   nexus-witness
//! ```
//!
//! # Resource Requirements
//!
//! - Memory: ~64MB (just Raft log, no data)
//! - CPU: <0.1 core (only processes votes/heartbeats)
//! - Disk: ~1GB (Raft log only, auto-compacted)

use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;

#[tokio::main]
#[allow(unreachable_code)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("_nexus_raft=debug".parse()?)
                .add_directive("tonic=info".parse()?),
        )
        .init();

    // Parse configuration from environment
    let node_id: u64 = env::var("NEXUS_NODE_ID")
        .unwrap_or_else(|_| "1".to_string())
        .parse()
        .expect("NEXUS_NODE_ID must be a valid u64");

    let bind_addr: SocketAddr = env::var("NEXUS_BIND_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:2028".to_string())
        .parse()
        .expect("NEXUS_BIND_ADDR must be a valid socket address");

    let data_dir =
        env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| "./nexus_witness_data".to_string());

    let data_path = PathBuf::from(&data_dir);

    // Ensure data directory exists
    std::fs::create_dir_all(&data_path)?;

    tracing::info!(
        "Starting Nexus Witness Node\n  Node ID: {}\n  Bind: {}\n  Data: {}",
        node_id,
        bind_addr,
        data_path.display()
    );

    // Import and start the witness server (requires grpc feature AND proto files)
    #[cfg(all(feature = "grpc", has_protos))]
    {
        use _nexus_raft::transport::{
            NodeAddress, RaftClientPool, RaftWitnessServer, ServerConfig, TlsConfig, TransportLoop,
        };

        // Parse TLS configuration from environment
        let tls_config = match (
            env::var("NEXUS_TLS_CERT").ok(),
            env::var("NEXUS_TLS_KEY").ok(),
            env::var("NEXUS_TLS_CA").ok(),
        ) {
            (Some(cert_path), Some(key_path), Some(ca_path)) => {
                let cert_pem = std::fs::read(&cert_path)
                    .unwrap_or_else(|e| panic!("Failed to read TLS cert '{}': {}", cert_path, e));
                let key_pem = std::fs::read(&key_path)
                    .unwrap_or_else(|e| panic!("Failed to read TLS key '{}': {}", key_path, e));
                let ca_pem = std::fs::read(&ca_path)
                    .unwrap_or_else(|e| panic!("Failed to read TLS CA '{}': {}", ca_path, e));
                tracing::info!(
                    "TLS enabled (cert={}, key={}, ca={})",
                    cert_path,
                    key_path,
                    ca_path
                );
                Some(TlsConfig {
                    cert_pem,
                    key_pem,
                    ca_pem,
                })
            }
            (None, None, None) => {
                tracing::info!("TLS disabled (no NEXUS_TLS_CERT/KEY/CA set)");
                None
            }
            _ => {
                panic!(
                    "TLS requires all three env vars: NEXUS_TLS_CERT, NEXUS_TLS_KEY, NEXUS_TLS_CA"
                );
            }
        };

        let use_tls = tls_config.is_some();

        // Parse peers from NEXUS_PEERS env var
        let peers: Vec<NodeAddress> = env::var("NEXUS_PEERS")
            .unwrap_or_default()
            .split(',')
            .filter(|s| !s.is_empty())
            .map(|s| {
                NodeAddress::parse_with_tls(s.trim(), use_tls)
                    .unwrap_or_else(|e| panic!("Invalid peer address '{}': {}", s, e))
            })
            .collect();

        if peers.is_empty() {
            tracing::warn!("No peers configured (NEXUS_PEERS is empty).");
        } else {
            tracing::info!(
                "Peers: {}",
                peers
                    .iter()
                    .map(|p| p.to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }

        let config = ServerConfig {
            bind_address: bind_addr,
            tls: tls_config,
            ..Default::default()
        };

        let mut server = RaftWitnessServer::with_config(
            node_id,
            data_path.to_str().unwrap(),
            config,
            peers.clone(),
        )
        .map_err(|e| format!("Failed to create witness server: {}", e))?;

        // Set up shutdown signal
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start transport loop in background — owns the driver exclusively
        let driver = server.take_driver();
        let peer_map: std::collections::HashMap<u64, NodeAddress> =
            peers.into_iter().map(|p| (p.id, p)).collect();
        let shared_peers = std::sync::Arc::new(std::sync::RwLock::new(peer_map));
        let transport_loop = TransportLoop::new(driver, shared_peers, RaftClientPool::new());
        tokio::spawn(transport_loop.run(shutdown_rx));

        tracing::info!("Witness server starting on {}", bind_addr);

        // Handle shutdown signal
        let shutdown = async move {
            tokio::signal::ctrl_c()
                .await
                .expect("Failed to install Ctrl+C handler");
            tracing::info!("Shutdown signal received");
            let _ = shutdown_tx.send(true);
        };

        server
            .serve_with_shutdown(shutdown)
            .await
            .map_err(|e| format!("Witness server error: {}", e))?;

        tracing::info!("Witness server stopped");
    }

    #[cfg(not(all(feature = "grpc", has_protos)))]
    {
        eprintln!("Error: This binary requires the 'grpc' feature and proto files.");
        eprintln!("Build with: cargo build --features grpc --bin nexus-witness");
        return Err("grpc feature or proto files not available".into());
    }

    #[cfg(all(feature = "grpc", has_protos))]
    Ok(())
}
