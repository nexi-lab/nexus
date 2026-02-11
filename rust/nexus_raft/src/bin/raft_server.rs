//! Nexus Raft Server
//!
//! A full Raft member that participates in consensus and stores data.
//!
//! # Usage
//!
//! ```bash
//! # Start a single-node Raft server (for development)
//! nexus-raft-server --id 1 --bind 0.0.0.0:2026 --data ./nexus_data
//!
//! # Start a 3-node cluster
//! NEXUS_NODE_ID=1 NEXUS_BIND_ADDR=0.0.0.0:2026 \
//!   NEXUS_PEERS=2@http://10.0.0.2:2026,3@http://10.0.0.3:2026 \
//!   nexus-raft-server
//! ```
//!
//! # Environment Variables
//!
//! - `NEXUS_NODE_ID`: Node ID (defaults to 1)
//! - `NEXUS_BIND_ADDR`: Bind address (defaults to 0.0.0.0:2026)
//! - `NEXUS_DATA_DIR`: Data directory (defaults to ./nexus_data)
//! - `NEXUS_PEERS`: Comma-separated peer list in `id@host:port` format

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
        .unwrap_or_else(|_| "0.0.0.0:2026".to_string())
        .parse()
        .expect("NEXUS_BIND_ADDR must be a valid socket address");

    let data_dir = env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| "./nexus_data".to_string());

    let data_path = PathBuf::from(&data_dir);

    // Ensure data directory exists
    std::fs::create_dir_all(&data_path)?;

    tracing::info!(
        "Starting Nexus Raft Server\n  Node ID: {}\n  Bind: {}\n  Data: {}",
        node_id,
        bind_addr,
        data_path.display()
    );

    // Import and start the server (requires grpc feature AND proto files)
    #[cfg(all(feature = "grpc", has_protos))]
    {
        use _nexus_raft::transport::{
            NodeAddress, RaftClientPool, RaftServer, ServerConfig, TransportLoop,
        };

        // Parse peers from NEXUS_PEERS env var (format: "2@host:port,3@host:port")
        let peers: Vec<NodeAddress> = env::var("NEXUS_PEERS")
            .unwrap_or_default()
            .split(',')
            .filter(|s| !s.is_empty())
            .map(|s| {
                NodeAddress::parse(s.trim())
                    .unwrap_or_else(|e| panic!("Invalid peer address '{}': {}", s, e))
            })
            .collect();

        if peers.is_empty() {
            tracing::warn!("No peers configured (NEXUS_PEERS is empty). Running as single node.");
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
            ..Default::default()
        };

        let mut server =
            RaftServer::with_config(node_id, data_path.to_str().unwrap(), config, peers.clone())
                .map_err(|e| format!("Failed to create server: {}", e))?;

        // Set up shutdown signal
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start transport loop in background — owns the driver exclusively
        let driver = server.take_driver();
        let peer_map = peers.into_iter().map(|p| (p.id, p)).collect();
        let transport_loop = TransportLoop::new(driver, peer_map, RaftClientPool::new());
        tokio::spawn(transport_loop.run(shutdown_rx));

        tracing::info!("Raft server starting on {}", bind_addr);

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
            .map_err(|e| format!("Server error: {}", e))?;

        tracing::info!("Raft server stopped");
    }

    #[cfg(not(all(feature = "grpc", has_protos)))]
    {
        eprintln!("Error: This binary requires the 'grpc' feature and proto files.");
        eprintln!("Build with: cargo build --features grpc --bin nexus-raft-server");
        return Err("grpc feature or proto files not available".into());
    }

    #[cfg(all(feature = "grpc", has_protos))]
    Ok(())
}
