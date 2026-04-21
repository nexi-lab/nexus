//! Nexus Witness Node — Multi-Zone
//!
//! A lightweight Raft witness that participates in leader election
//! but doesn't apply state machine. This enables cost-effective high availability
//! with only 2 full nodes + 1 witness.
//!
//! # What is a Witness?
//!
//! - Votes in leader elections (standard Raft protocol)
//! - Stores Raft log (for vote validation)
//! - Does NOT apply state machine
//! - Does NOT serve reads
//! - Cannot become leader
//!
//! # TLS Bootstrap
//!
//! Same 2-phase flow as fullnodes:
//! 1. Existing certs on disk → use them (normal restart)
//! 2. No certs + NEXUS_PEERS → start plaintext, call JoinCluster on leader,
//!    save certs, restart server with mTLS
//!
//! Uses the shared Rust `call_join_cluster()` — same code path as fullnodes.
//!
//! # Usage
//!
//! ```bash
//! NEXUS_BIND_ADDR=0.0.0.0:2126 \
//!   NEXUS_PEERS=nexus-1:2126,nexus-2:2126,witness:2126 \
//!   NEXUS_FEDERATION_ZONES=corp,corp-eng,corp-sales,family \
//!   nexus-witness
//! ```

use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

#[tokio::main]
#[allow(unreachable_code)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("_nexus_raft=debug".parse()?)
                .add_directive("tonic=info".parse()?),
        )
        .init();

    let hostname = env::var("NEXUS_HOSTNAME")
        .unwrap_or_else(|_| gethostname::gethostname().to_string_lossy().into_owned());
    let node_id = _nexus_raft::transport::hostname_to_node_id(&hostname);

    let bind_addr: SocketAddr = env::var("NEXUS_BIND_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:2126".to_string())
        .parse()
        .expect("NEXUS_BIND_ADDR must be a valid socket address");

    let data_dir =
        env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| "./nexus_witness_data".to_string());
    let data_path = PathBuf::from(&data_dir);
    std::fs::create_dir_all(&data_path)?;

    let federation_zones: Vec<String> = env::var("NEXUS_FEDERATION_ZONES")
        .unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    let max_auto_join_zones: usize = env::var("NEXUS_WITNESS_MAX_AUTO_ZONES")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(128);

    tracing::info!(
        "Starting Nexus Witness Node\n  Hostname: {}\n  Node ID: {}\n  Bind: {}\n  Data: {}\n  Federation zones: {:?}\n  Max auto-join zones: {}",
        hostname,
        node_id,
        bind_addr,
        data_path.display(),
        federation_zones,
        max_auto_join_zones,
    );

    #[cfg(all(feature = "grpc", has_protos))]
    {
        use _nexus_raft::transport::{
            parse_join_token, NodeAddress, RaftWitnessServer, ServerConfig, WitnessZoneRegistry,
        };

        let tls_dir = data_path.join("tls");

        // TLS: check disk for existing certs (same logic as fullnodes)
        let tls_config = load_tls_from_disk(&tls_dir);
        let use_tls = tls_config.is_some();
        let needs_bootstrap = tls_config.is_none();

        if use_tls {
            tracing::info!("TLS: using existing certs from {}", tls_dir.display());
        } else {
            tracing::info!("TLS: no certs — starting plaintext (2-phase bootstrap)");
        }

        // Parse peers (plaintext initially if no certs)
        let peers: Vec<NodeAddress> = env::var("NEXUS_PEERS")
            .unwrap_or_default()
            .split(',')
            .filter(|s| !s.is_empty())
            .map(|s| {
                NodeAddress::parse(s.trim(), use_tls)
                    .unwrap_or_else(|e| panic!("Invalid peer address '{}': {}", s, e))
            })
            .collect();

        if !peers.is_empty() {
            tracing::info!(
                "Peers: {}",
                peers
                    .iter()
                    .map(|p| p.to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }

        // Create registry + zones
        let mut registry = WitnessZoneRegistry::new(data_path.clone(), node_id, tls_config.clone());
        registry.set_peers(peers.clone());
        registry.set_max_auto_join_zones(max_auto_join_zones);
        let registry = Arc::new(registry);
        let runtime_handle = tokio::runtime::Handle::current();

        registry
            .create_zone("root", peers.clone(), &runtime_handle)
            .map_err(|e| format!("Failed to create root zone: {}", e))?;
        for zone_id in &federation_zones {
            registry
                .create_zone(zone_id, peers.clone(), &runtime_handle)
                .map_err(|e| format!("Failed to create zone '{}': {}", zone_id, e))?;
        }

        tracing::info!(
            "Witness bootstrap complete: {} zones",
            1 + federation_zones.len(),
        );

        // Start server (plaintext or mTLS depending on cert availability)
        let server_config = ServerConfig {
            bind_address: bind_addr,
            tls: tls_config,
            ..Default::default()
        };
        let server = RaftWitnessServer::new(registry.clone(), server_config);

        // If no certs, read join token from file and spawn TLS bootstrap task.
        // Token is file-only (no env var) — consistent with file-based design.
        if needs_bootstrap && !peers.is_empty() {
            let token_path = tls_dir.join("join-token");
            let join_token = if token_path.exists() {
                let token = std::fs::read_to_string(&token_path).unwrap_or_default();
                let token = token.trim().to_string();
                if token.is_empty() {
                    tracing::warn!("Join token file is empty: {}", token_path.display());
                }
                token
            } else {
                tracing::warn!(
                    "No join token at {} — cannot provision TLS certs",
                    token_path.display()
                );
                String::new()
            };

            if !join_token.is_empty() {
                let parsed = match parse_join_token(&join_token) {
                    Ok(parsed) => Some(parsed),
                    Err(e) => {
                        tracing::warn!("Join token file has invalid format: {}", e);
                        None
                    }
                };
                if let Some(parsed) = parsed {
                    let password = parsed.password;
                    let expected_ca_fingerprint = parsed.ca_fingerprint;
                    let tls_dir_bg = tls_dir.clone();
                    let peers_bg = peers.clone();
                    let my_addr = peers
                        .iter()
                        .find(|p| p.id == node_id)
                        .map(|p| p.endpoint.clone())
                        .unwrap_or_else(|| format!("http://{}", bind_addr));
                    tokio::spawn(async move {
                        tls_bootstrap_loop(
                            node_id,
                            &my_addr,
                            &peers_bg,
                            &tls_dir_bg,
                            &password,
                            &expected_ca_fingerprint,
                        )
                        .await;
                    });
                }
            }
        }

        tracing::info!("Witness server starting on {}", bind_addr);

        let shutdown_registry = registry.clone();
        let shutdown = async move {
            tokio::signal::ctrl_c()
                .await
                .expect("Failed to install Ctrl+C handler");
            tracing::info!("Shutdown signal received");
            shutdown_registry.shutdown_all();
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

/// Load TLS certs from disk if all three files exist.
#[cfg(all(feature = "grpc", has_protos))]
fn load_tls_from_disk(tls_dir: &std::path::Path) -> Option<_nexus_raft::transport::TlsConfig> {
    use _nexus_raft::transport::TlsConfig;

    let ca = tls_dir.join("ca.pem");
    let cert = tls_dir.join("node.pem");
    let key = tls_dir.join("node-key.pem");

    if ca.exists() && cert.exists() && key.exists() {
        Some(TlsConfig {
            ca_pem: std::fs::read(&ca).expect("read CA cert"),
            cert_pem: std::fs::read(&cert).expect("read node cert"),
            key_pem: std::fs::read(&key).expect("read node key"),
        })
    } else {
        None
    }
}

/// Background loop: try JoinCluster on each peer until one succeeds (leader).
/// After success, save certs to disk and exit. The witness must be restarted
/// to pick up the new certs (Docker will restart it, or the orchestrator will).
#[cfg(all(feature = "grpc", has_protos))]
async fn tls_bootstrap_loop(
    node_id: u64,
    node_address: &str,
    peers: &[_nexus_raft::transport::NodeAddress],
    tls_dir: &std::path::Path,
    password: &str,
    expected_ca_fingerprint: &str,
) {
    use _nexus_raft::transport::call_join_cluster;

    // Wait a bit for leader election
    tokio::time::sleep(std::time::Duration::from_secs(5)).await;

    loop {
        // Check if certs appeared (another process might have written them)
        if tls_dir.join("node.pem").exists() {
            tracing::info!("TLS certs found on disk — bootstrap complete");
            return;
        }

        // Try each peer as potential leader
        for peer in peers {
            if peer.id == node_id {
                continue; // Skip self
            }

            tracing::debug!("Trying JoinCluster on peer {} ({})", peer.id, peer.endpoint);

            match call_join_cluster(
                &peer.endpoint,
                node_id,
                node_address,
                "root",
                password,
                10, // timeout
            )
            .await
            {
                Ok(result) => {
                    let ca_fingerprint =
                        match _nexus_raft::transport::ca_fingerprint_from_pem(&result.ca_pem) {
                            Ok(fingerprint) => fingerprint,
                            Err(e) => {
                                tracing::warn!(
                                "TLS bootstrap: failed to compute CA fingerprint from peer {}: {}",
                                peer.id,
                                e
                            );
                                continue;
                            }
                        };

                    if ca_fingerprint != expected_ca_fingerprint {
                        tracing::error!(
                            "TLS bootstrap: CA fingerprint mismatch from peer {} (expected '{}', got '{}')",
                            peer.id,
                            expected_ca_fingerprint,
                            ca_fingerprint
                        );
                        continue;
                    }

                    // Save certs to disk
                    std::fs::create_dir_all(tls_dir).expect("create tls dir");
                    std::fs::write(tls_dir.join("ca.pem"), &result.ca_pem).expect("write CA");
                    std::fs::write(tls_dir.join("node.pem"), &result.node_cert_pem)
                        .expect("write cert");

                    // Write key with restricted permissions
                    let key_path = tls_dir.join("node-key.pem");
                    std::fs::write(&key_path, &result.node_key_pem).expect("write key");
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        std::fs::set_permissions(&key_path, std::fs::Permissions::from_mode(0o600))
                            .ok();
                    }

                    tracing::info!(
                        "TLS bootstrap: received signed cert from peer {} — restart to enable mTLS",
                        peer.id
                    );
                    // Certs saved. The witness needs a restart to use them.
                    // The Raft-coordinated upgrade signal will eventually trigger
                    // this via the fullnode leader's __system__/tls/upgrade proposal.
                    // For now, the witness continues in plaintext until restart.
                    return;
                }
                Err(e) => {
                    tracing::debug!("JoinCluster on peer {} failed: {}", peer.id, e);
                }
            }
        }

        // Retry after delay
        tokio::time::sleep(std::time::Duration::from_secs(5)).await;
    }
}
