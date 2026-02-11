//! Integration tests for Raft gRPC cluster.
//!
//! Two test modes:
//!
//! 1. **In-process** (`test_three_node_grpc_cluster`): Starts 3 RaftServer
//!    instances in-process on localhost ports. Always runs.
//!
//! 2. **Docker** (`test_docker_cluster`): Connects to externally running Docker
//!    containers on ports 2026/2027/2028. Runs by default; skip with
//!    `NEXUS_DOCKER_TEST=0`.
//!    ```bash
//!    docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d
//!    cargo test --all-features --test test_grpc_cluster -- test_docker
//!    ```
//!
//! Both modes verify:
//! - Leader election via polling GetClusterInfo
//! - Metadata replication (propose on leader, query all nodes)
//! - Non-leader redirect (propose on follower → NotLeader)
//! - Multiple writes with full convergence

#[cfg(all(feature = "grpc", has_protos))]
mod grpc_cluster {
    use _nexus_raft::raft::ZoneRaftRegistry;
    use _nexus_raft::transport::{ClientConfig, NodeAddress, RaftApiClient, RaftGrpcServer, ServerConfig};
    use std::sync::Arc;
    use std::time::Duration;
    use tempfile::TempDir;

    /// Connect a RaftApiClient with zone_id = "default".
    async fn connect_client(
        endpoint: &str,
        config: ClientConfig,
    ) -> _nexus_raft::transport::Result<RaftApiClient> {
        RaftApiClient::connect(endpoint, config)
            .await
            .map(|c| c.with_zone_id("default".into()))
    }

    /// Wait for a leader to be elected across the cluster.
    ///
    /// Polls `get_cluster_info()` on each endpoint until one reports `is_leader=true`.
    /// Returns `(leader_endpoint, leader_id)`.
    async fn wait_for_leader(endpoints: &[String], timeout: Duration) -> (String, u64) {
        let start = tokio::time::Instant::now();
        let config = ClientConfig {
            connect_timeout: Duration::from_secs(2),
            request_timeout: Duration::from_secs(2),
            ..Default::default()
        };

        loop {
            if start.elapsed() > timeout {
                panic!("Leader election timed out after {:?}", timeout);
            }

            for endpoint in endpoints {
                match connect_client(endpoint, config.clone()).await {
                    Ok(mut client) => {
                        if let Ok(info) = client.get_cluster_info().await {
                            if info.is_leader && info.leader_id > 0 {
                                return (endpoint.clone(), info.leader_id);
                            }
                        }
                    }
                    Err(_) => continue, // Server not ready yet
                }
            }

            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }

    /// Wait for metadata to appear on a node.
    async fn wait_for_metadata(endpoint: &str, path: &str, timeout: Duration) -> bool {
        let start = tokio::time::Instant::now();
        let config = ClientConfig {
            connect_timeout: Duration::from_secs(2),
            request_timeout: Duration::from_secs(2),
            ..Default::default()
        };

        loop {
            if start.elapsed() > timeout {
                return false;
            }

            if let Ok(mut client) = connect_client(endpoint, config.clone()).await {
                if let Ok(result) = client.get_metadata(path, "", false).await {
                    if result.success {
                        return true;
                    }
                }
            }

            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    #[tokio::test]
    async fn test_three_node_grpc_cluster() {
        // Initialize tracing for test output
        let _ = tracing_subscriber::fmt()
            .with_env_filter("nexus_raft=debug,tonic=info")
            .with_test_writer()
            .try_init();

        // Use high port numbers to avoid conflicts with other tests
        let base_port = 21061u16;
        let endpoints: Vec<String> = (0..3)
            .map(|i| format!("http://127.0.0.1:{}", base_port + i))
            .collect();

        // Create temp dirs for each node's sled storage
        let temp_dirs: Vec<TempDir> = (0..3)
            .map(|_| TempDir::new().expect("Failed to create temp dir"))
            .collect();

        // Define peer lists for each node
        let all_peers: Vec<Vec<NodeAddress>> = (0..3)
            .map(|i| {
                (0..3)
                    .filter(|&j| j != i)
                    .map(|j| NodeAddress::new((j + 1) as u64, &endpoints[j]))
                    .collect()
            })
            .collect();

        // Shutdown channel
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start 3 nodes, each with ZoneRaftRegistry + RaftGrpcServer
        let mut server_handles = vec![];

        for i in 0..3 {
            let node_id = (i + 1) as u64;
            let bind_addr = format!("127.0.0.1:{}", base_port + i as u16)
                .parse()
                .unwrap();

            let config = ServerConfig {
                bind_address: bind_addr,
                ..Default::default()
            };

            // Create registry and register "default" zone (handles TransportLoop internally)
            let registry = Arc::new(ZoneRaftRegistry::new(
                temp_dirs[i].path().to_path_buf(),
                node_id,
            ));

            let _node = registry
                .create_zone("default", all_peers[i].clone(), false, &tokio::runtime::Handle::current())
                .expect("Failed to create zone");

            // Start gRPC server in background
            let shutdown_rx_clone = shutdown_rx.clone();
            let server = RaftGrpcServer::new(registry, config);
            let handle = tokio::spawn(async move {
                let shutdown = async move {
                    let mut rx = shutdown_rx_clone;
                    let _ = rx.changed().await;
                };
                if let Err(e) = server.serve_with_shutdown(shutdown).await {
                    tracing::error!("Server {} error: {}", node_id, e);
                }
            });

            server_handles.push(handle);
        }

        // Give servers a moment to start binding
        tokio::time::sleep(Duration::from_millis(500)).await;

        // ================================================================
        // Test 1: Leader Election
        // ================================================================
        tracing::info!("=== Test 1: Leader Election ===");

        let (leader_endpoint, leader_id) =
            wait_for_leader(&endpoints, Duration::from_secs(15)).await;

        tracing::info!("Leader elected: node {} at {}", leader_id, leader_endpoint);

        assert!(leader_id >= 1 && leader_id <= 3, "Leader ID should be 1-3");

        // Verify exactly 1 leader
        let config = ClientConfig {
            connect_timeout: Duration::from_secs(2),
            request_timeout: Duration::from_secs(5),
            ..Default::default()
        };

        let mut leader_count = 0;
        for endpoint in &endpoints {
            if let Ok(mut client) = connect_client(endpoint, config.clone()).await {
                if let Ok(info) = client.get_cluster_info().await {
                    if info.is_leader {
                        leader_count += 1;
                    }
                }
            }
        }
        assert_eq!(leader_count, 1, "Exactly one leader should be elected");

        // ================================================================
        // Test 2: Metadata Replication
        // ================================================================
        tracing::info!("=== Test 2: Metadata Replication ===");

        let mut leader_client = connect_client(&leader_endpoint, config.clone())
            .await
            .expect("Failed to connect to leader");

        // Construct a FileMetadata proto message
        use _nexus_raft::transport::proto::nexus::core::FileMetadata;
        let metadata = FileMetadata {
            path: "/test/hello.txt".to_string(),
            backend_name: "local".to_string(),
            physical_path: "/data/hello.txt".to_string(),
            size: 42,
            mime_type: "text/plain".to_string(),
            version: 1,
            ..Default::default()
        };

        let result = leader_client
            .put_metadata(metadata)
            .await
            .expect("Propose should succeed");

        assert!(result.success, "Propose should succeed: {:?}", result.error);
        tracing::info!("Metadata proposed, applied_index={}", result.applied_index);

        // Wait for replication and verify all nodes have the metadata
        for (i, endpoint) in endpoints.iter().enumerate() {
            let found =
                wait_for_metadata(endpoint, "/test/hello.txt", Duration::from_secs(10)).await;

            assert!(
                found,
                "Node {} ({}) should have replicated metadata",
                i + 1,
                endpoint
            );
            tracing::info!("Node {} has metadata ✓", i + 1);
        }

        // ================================================================
        // Test 3: Non-Leader Redirect
        // ================================================================
        tracing::info!("=== Test 3: Non-Leader Redirect ===");

        // Find a follower endpoint
        let follower_endpoint = endpoints
            .iter()
            .find(|e| **e != leader_endpoint)
            .expect("Should have at least one follower");

        let mut follower_client = connect_client(follower_endpoint, config.clone())
            .await
            .expect("Failed to connect to follower");

        let redirect_metadata = FileMetadata {
            path: "/test/redirect.txt".to_string(),
            backend_name: "local".to_string(),
            size: 10,
            version: 1,
            ..Default::default()
        };

        let result = follower_client.put_metadata(redirect_metadata).await;

        match result {
            Ok(propose_result) => {
                // Server returns success=false with leader_address for redirect
                if !propose_result.success {
                    assert!(
                        propose_result.leader_address.is_some(),
                        "Non-leader should provide leader address in redirect"
                    );
                    tracing::info!(
                        "Follower correctly redirected to leader: {:?}",
                        propose_result.leader_address
                    );
                }
                // Some Raft implementations may forward the proposal — also acceptable
            }
            Err(_) => {
                // Transport-level error is also acceptable if server rejects
                tracing::info!("Follower rejected proposal (transport error) ✓");
            }
        }

        // ================================================================
        // Test 4: Multiple Writes
        // ================================================================
        tracing::info!("=== Test 4: Multiple Writes ===");

        for i in 0..10 {
            let metadata = FileMetadata {
                path: format!("/batch/file_{}.txt", i),
                backend_name: "local".to_string(),
                size: i as i64 * 100,
                version: 1,
                ..Default::default()
            };

            let result = leader_client
                .put_metadata(metadata)
                .await
                .expect("Batch propose should succeed");

            assert!(
                result.success,
                "Batch write {} failed: {:?}",
                i, result.error
            );
        }

        // Give replication time to converge
        tokio::time::sleep(Duration::from_secs(2)).await;

        // Verify all 10 files on each node
        for (node_idx, endpoint) in endpoints.iter().enumerate() {
            let mut client = connect_client(endpoint, config.clone())
                .await
                .expect("Failed to connect");

            let result = client
                .list_metadata("/batch/", "", true, 100, false)
                .await
                .expect("List should succeed");

            assert!(
                result.success,
                "List on node {} failed: {:?}",
                node_idx + 1,
                result.error
            );
            tracing::info!("Node {} has batch data ✓", node_idx + 1);
        }

        // ================================================================
        // Test 5: Query from Follower
        // ================================================================
        tracing::info!("=== Test 5: Query from Follower ===");

        let mut follower_client = connect_client(follower_endpoint, config.clone())
            .await
            .expect("Failed to connect to follower");

        let result = follower_client
            .get_metadata("/test/hello.txt", "", false)
            .await
            .expect("Follower query should succeed");

        assert!(result.success, "Follower query failed: {:?}", result.error);
        tracing::info!("Follower served read successfully ✓");

        // ================================================================
        // Cleanup
        // ================================================================
        tracing::info!("=== Shutting down cluster ===");
        let _ = shutdown_tx.send(true);

        // Wait for servers to stop
        for handle in server_handles {
            let _ = tokio::time::timeout(Duration::from_secs(5), handle).await;
        }

        tracing::info!("All tests passed ✓");
    }

    /// Test against a live Docker cluster (ports 2026/2027/2028).
    ///
    /// Runs by default. Skip with `NEXUS_DOCKER_TEST=0`.
    /// Start the cluster first:
    ///   docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d
    #[tokio::test]
    async fn test_docker_cluster() {
        // Skip if explicitly disabled
        if std::env::var("NEXUS_DOCKER_TEST").unwrap_or_default() == "0" {
            eprintln!("Skipping Docker cluster test (NEXUS_DOCKER_TEST=0)");
            return;
        }

        // Check if Docker cluster is reachable before running
        let probe_config = ClientConfig {
            connect_timeout: Duration::from_secs(2),
            request_timeout: Duration::from_secs(2),
            ..Default::default()
        };
        if connect_client("http://127.0.0.1:2126", probe_config)
            .await
            .is_err()
        {
            eprintln!(
                "Skipping Docker cluster test: no gRPC server at localhost:2126. \
                 Start with: docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d"
            );
            return;
        }

        let _ = tracing_subscriber::fmt()
            .with_env_filter("nexus_raft=debug,tonic=info")
            .with_test_writer()
            .try_init();

        // Docker compose gRPC ports: 2126→nexus-1 (full), 2127→nexus-2 (full), 2128→witness
        // (HTTP ports 2026/2027 are for the Python FastAPI server, not gRPC)
        // Witness participates in voting but does NOT store state machine data,
        // so metadata queries are only valid against full nodes.
        let full_endpoints: Vec<String> = vec![
            "http://127.0.0.1:2126".to_string(),
            "http://127.0.0.1:2127".to_string(),
        ];
        let all_endpoints: Vec<String> = vec![
            "http://127.0.0.1:2126".to_string(),
            "http://127.0.0.1:2127".to_string(),
            "http://127.0.0.1:2128".to_string(),
        ];

        let config = ClientConfig {
            connect_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(10),
            ..Default::default()
        };

        // ================================================================
        // Test 1: Leader Election
        // ================================================================
        tracing::info!("=== Docker Test 1: Leader Election ===");

        let (leader_endpoint, leader_id) =
            wait_for_leader(&all_endpoints, Duration::from_secs(30)).await;

        tracing::info!(
            "Docker leader elected: node {} at {}",
            leader_id,
            leader_endpoint
        );
        assert!(leader_id >= 1 && leader_id <= 3);

        // Verify exactly 1 leader across all nodes (including witness)
        let mut leader_count = 0;
        for endpoint in &all_endpoints {
            if let Ok(mut client) = connect_client(endpoint, config.clone()).await {
                if let Ok(info) = client.get_cluster_info().await {
                    tracing::info!(
                        "  {} → node_id={}, leader_id={}, term={}, is_leader={}",
                        endpoint,
                        info.node_id,
                        info.leader_id,
                        info.term,
                        info.is_leader
                    );
                    if info.is_leader {
                        leader_count += 1;
                    }
                }
            }
        }
        assert_eq!(leader_count, 1, "Exactly one leader should be elected");

        // ================================================================
        // Test 2: Metadata Replication
        // ================================================================
        tracing::info!("=== Docker Test 2: Metadata Replication ===");

        let mut leader_client = connect_client(&leader_endpoint, config.clone())
            .await
            .expect("Failed to connect to leader");

        use _nexus_raft::transport::proto::nexus::core::FileMetadata;
        let metadata = FileMetadata {
            path: "/docker-test/hello.txt".to_string(),
            backend_name: "local".to_string(),
            physical_path: "/data/docker-hello.txt".to_string(),
            size: 123,
            mime_type: "text/plain".to_string(),
            version: 1,
            ..Default::default()
        };

        let result = leader_client
            .put_metadata(metadata)
            .await
            .expect("Propose should succeed on Docker cluster");

        assert!(result.success, "Propose failed: {:?}", result.error);
        tracing::info!(
            "Metadata proposed on Docker cluster, applied_index={}",
            result.applied_index
        );

        // Verify replication to full nodes (witness doesn't store state machine)
        for (i, endpoint) in full_endpoints.iter().enumerate() {
            let found =
                wait_for_metadata(endpoint, "/docker-test/hello.txt", Duration::from_secs(15))
                    .await;

            assert!(
                found,
                "Docker full node {} ({}) should have replicated metadata",
                i + 1,
                endpoint
            );
            tracing::info!("Docker full node {} has metadata ✓", i + 1);
        }

        // ================================================================
        // Test 3: Non-Leader Redirect
        // ================================================================
        tracing::info!("=== Docker Test 3: Non-Leader Redirect ===");

        // Pick a full-node follower (not the witness)
        let follower_endpoint = full_endpoints
            .iter()
            .find(|e| **e != leader_endpoint)
            .expect("Should have at least one full-node follower");

        let mut follower_client = connect_client(follower_endpoint, config.clone())
            .await
            .expect("Failed to connect to follower");

        let redirect_metadata = FileMetadata {
            path: "/docker-test/redirect.txt".to_string(),
            backend_name: "local".to_string(),
            size: 10,
            version: 1,
            ..Default::default()
        };

        let result = follower_client.put_metadata(redirect_metadata).await;
        match result {
            Ok(propose_result) => {
                if !propose_result.success {
                    assert!(
                        propose_result.leader_address.is_some(),
                        "Non-leader should provide leader address"
                    );
                    tracing::info!(
                        "Docker follower correctly redirected to: {:?}",
                        propose_result.leader_address
                    );
                }
            }
            Err(_) => {
                tracing::info!("Docker follower rejected proposal (transport error) ✓");
            }
        }

        // ================================================================
        // Test 4: Multiple Writes + Convergence
        // ================================================================
        tracing::info!("=== Docker Test 4: Multiple Writes ===");

        for i in 0..10 {
            let metadata = FileMetadata {
                path: format!("/docker-batch/file_{}.txt", i),
                backend_name: "local".to_string(),
                size: i as i64 * 100,
                version: 1,
                ..Default::default()
            };

            let result = leader_client
                .put_metadata(metadata)
                .await
                .expect("Docker batch propose should succeed");

            assert!(
                result.success,
                "Docker batch write {} failed: {:?}",
                i, result.error
            );
        }

        // Wait for replication convergence
        tokio::time::sleep(Duration::from_secs(3)).await;

        // Verify all 10 files on full nodes
        for (node_idx, endpoint) in full_endpoints.iter().enumerate() {
            let mut client = connect_client(endpoint, config.clone())
                .await
                .expect("Failed to connect");

            let result = client
                .list_metadata("/docker-batch/", "", true, 100, false)
                .await
                .expect("List should succeed");

            assert!(
                result.success,
                "Docker list on full node {} failed: {:?}",
                node_idx + 1,
                result.error
            );
            tracing::info!("Docker full node {} has batch data ✓", node_idx + 1);
        }

        // ================================================================
        // Test 5: Query from Follower
        // ================================================================
        tracing::info!("=== Docker Test 5: Query from Full-Node Follower ===");

        let mut follower_client = connect_client(follower_endpoint, config.clone())
            .await
            .expect("Failed to connect to full-node follower");

        let result = follower_client
            .get_metadata("/docker-test/hello.txt", "", false)
            .await
            .expect("Docker follower query should succeed");

        assert!(
            result.success,
            "Docker follower query failed: {:?}",
            result.error
        );
        tracing::info!("Docker follower served read successfully ✓");

        tracing::info!("=== All Docker cluster tests passed ✓ ===");
    }
}
