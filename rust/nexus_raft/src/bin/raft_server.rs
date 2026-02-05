//! Nexus Raft Server
//!
//! A full Raft member that participates in consensus and stores data.
//!
//! # Usage
//!
//! ```bash
//! # Start a Raft server node
//! nexus-raft-server --id 1 --bind 0.0.0.0:2026 --data /var/lib/nexus/data
//!
//! # With specific peers for testing
//! nexus-raft-server --id 1 --bind 0.0.0.0:2026 --data ./data
//! ```
//!
//! # Environment Variables
//!
//! - `NEXUS_NODE_ID`: Node ID (defaults to 1)
//! - `NEXUS_BIND_ADDR`: Bind address (defaults to 0.0.0.0:2026)
//! - `NEXUS_DATA_DIR`: Data directory (defaults to ./nexus_data)

use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("nexus_raft=debug".parse()?)
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

    // Import and start the server
    #[cfg(feature = "grpc")]
    {
        use _nexus_raft::transport::{RaftServer, ServerConfig};

        let config = ServerConfig {
            bind_address: bind_addr,
            ..Default::default()
        };

        let server = RaftServer::with_config(node_id, data_path.to_str().unwrap(), config)
            .map_err(|e| format!("Failed to create server: {}", e))?;

        tracing::info!("Raft server starting on {}", bind_addr);

        // Handle shutdown signal
        let shutdown = async {
            tokio::signal::ctrl_c()
                .await
                .expect("Failed to install Ctrl+C handler");
            tracing::info!("Shutdown signal received");
        };

        server
            .serve_with_shutdown(shutdown)
            .await
            .map_err(|e| format!("Server error: {}", e))?;

        tracing::info!("Raft server stopped");
    }

    #[cfg(not(feature = "grpc"))]
    {
        eprintln!("Error: This binary requires the 'grpc' feature.");
        eprintln!("Build with: cargo build --features grpc --bin nexus-raft-server");
        std::process::exit(1);
    }

    Ok(())
}
