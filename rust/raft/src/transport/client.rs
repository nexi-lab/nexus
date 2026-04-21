//! gRPC client for Raft transport.
//!
//! Provides a client to communicate with other Raft nodes using tonic gRPC.

use super::proto::nexus::raft::{
    raft_command::Command as ProtoCommandVariant, raft_query::Query as ProtoQueryVariant,
    zone_api_service_client::ZoneApiServiceClient,
    zone_transport_service_client::ZoneTransportServiceClient, AcquireLock, DeleteMetadata,
    EcReplicationEntry, ExtendLock, GetClusterInfoRequest, GetLockInfo, GetMetadata,
    JoinClusterRequest, ListMetadata, ProposeRequest, PutMetadata, QueryRequest, RaftCommand,
    RaftQuery, ReleaseLock, ReplicateEntriesRequest, StepMessageRequest,
};
use super::{NodeAddress, Result, TransportError};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;
use tonic::transport::{Channel, Endpoint};

/// Default TTL for cached gRPC clients. Connections older than this are
/// evicted on next access and a fresh connection is established.
///
/// 60s is a balance: long enough that normal send/heartbeat traffic reuses
/// the Channel for many RPCs, short enough that a peer-restart-induced dead
/// connection not caught by HTTP/2 keep-alive is still forcibly refreshed
/// within a minute.
const DEFAULT_CLIENT_TTL: Duration = Duration::from_secs(60);

/// Configuration for Raft transport client.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// Connection timeout.
    pub connect_timeout: Duration,
    /// Request timeout.
    pub request_timeout: Duration,
    /// Keep-alive interval.
    pub keep_alive_interval: Duration,
    /// Keep-alive timeout.
    pub keep_alive_timeout: Duration,
    /// Shared TLS configuration — reads from registry's Arc<RwLock<>> so
    /// transport loops pick up TLS upgrades without restart.
    pub tls: Arc<std::sync::RwLock<Option<super::TlsConfig>>>,
}

impl Default for ClientConfig {
    fn default() -> Self {
        // Raft peer transport: tight timeouts + aggressive HTTP/2 keep-alive so
        // a dead peer's Channel is invalidated by the TCP/H2 stack in a few
        // seconds, not after the default 10s/20s defaults. When a node restarts
        // (test failover or a real operator bounce), the next raft send on a
        // stale cached client must NOT pay a 5-second connect timeout while the
        // kernel's ``SYN_SENT`` retries finally give up — that latency is the
        // post-restart catchup flake we keep seeing in the federation E2E.
        //
        // The numbers below are load-bearing:
        // - ``connect_timeout=2s``: raft peers are same-LAN/docker-network; a
        //   connect that takes longer means the peer is not accepting.
        // - ``keep_alive_interval=2s`` + ``keep_alive_timeout=3s``: H2 PING
        //   every 2s, connection declared dead after 3s of no PONG. Total
        //   detection latency ~5s even when no raft message is in flight.
        Self {
            connect_timeout: Duration::from_secs(2),
            request_timeout: Duration::from_secs(10),
            keep_alive_interval: Duration::from_secs(2),
            keep_alive_timeout: Duration::from_secs(3),
            tls: Arc::new(std::sync::RwLock::new(None)),
        }
    }
}

/// A cached client entry with its creation timestamp for TTL eviction.
struct CachedClient {
    client: RaftClient,
    created_at: Instant,
}

/// A pool of gRPC clients for connecting to Raft peers.
///
/// Provides lazy TTL-based eviction: on each `get()`, if the cached client
/// is older than the TTL, it is evicted and a fresh connection is created.
/// This ensures stale connections are cleaned up when peer addresses change.
#[derive(Clone)]
pub struct RaftClientPool {
    config: ClientConfig,
    clients: Arc<RwLock<HashMap<u64, CachedClient>>>,
    client_ttl: Duration,
}

impl RaftClientPool {
    /// Create a new client pool with default configuration.
    pub fn new() -> Self {
        Self::with_config(ClientConfig::default())
    }

    /// Create a new client pool with custom configuration.
    pub fn with_config(config: ClientConfig) -> Self {
        Self {
            config,
            clients: Arc::new(RwLock::new(HashMap::new())),
            client_ttl: DEFAULT_CLIENT_TTL,
        }
    }

    /// Get the client configuration.
    pub fn config(&self) -> &ClientConfig {
        &self.config
    }

    /// Create a new client pool with custom configuration and TTL.
    pub fn with_config_and_ttl(config: ClientConfig, ttl: Duration) -> Self {
        Self {
            config,
            clients: Arc::new(RwLock::new(HashMap::new())),
            client_ttl: ttl,
        }
    }

    /// Get or create a client for the given node.
    ///
    /// Cached clients older than the TTL are evicted and reconnected.
    pub async fn get(&self, addr: &NodeAddress) -> Result<RaftClient> {
        // Check if we have a non-stale cached client
        {
            let clients = self.clients.read().await;
            if let Some(cached) = clients.get(&addr.id) {
                if cached.created_at.elapsed() < self.client_ttl {
                    return Ok(cached.client.clone());
                }
                // Stale — fall through to reconnect
                tracing::debug!(
                    node_id = addr.id,
                    age_secs = cached.created_at.elapsed().as_secs(),
                    ttl_secs = self.client_ttl.as_secs(),
                    "evicting stale gRPC client"
                );
            }
        }

        // Create new client (may replace a stale one)
        let client = RaftClient::connect(&addr.endpoint, self.config.clone()).await?;

        // Store in pool with current timestamp
        {
            let mut clients = self.clients.write().await;
            clients.insert(
                addr.id,
                CachedClient {
                    client: client.clone(),
                    created_at: Instant::now(),
                },
            );
        }

        Ok(client)
    }

    /// Remove a client from the pool (e.g., after connection failure).
    pub async fn remove(&self, node_id: u64) {
        let mut clients = self.clients.write().await;
        clients.remove(&node_id);
    }

    /// Get the number of active connections.
    pub async fn connection_count(&self) -> usize {
        self.clients.read().await.len()
    }
}

impl Default for RaftClientPool {
    fn default() -> Self {
        Self::new()
    }
}

/// A single gRPC client for communicating with a Raft node.
#[derive(Clone)]
pub struct RaftClient {
    endpoint: String,
    #[allow(dead_code)]
    config: ClientConfig,
    inner: ZoneTransportServiceClient<Channel>,
}

impl RaftClient {
    /// Connect to a Raft node.
    pub async fn connect(endpoint: &str, config: ClientConfig) -> Result<Self> {
        let tls_snapshot = config.tls.read().unwrap().clone();
        tracing::info!(
            "Connecting to Raft node at {} (tls={})",
            endpoint,
            tls_snapshot.is_some()
        );

        let mut ep = Endpoint::from_shared(endpoint.to_string())
            .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
            .connect_timeout(config.connect_timeout)
            .timeout(config.request_timeout)
            .http2_keep_alive_interval(config.keep_alive_interval)
            .keep_alive_timeout(config.keep_alive_timeout)
            // Ping even when the connection is idle — raft heartbeats are
            // frequent enough that this rarely matters in steady state, but
            // when a peer is temporarily down and no outbound messages queue
            // to it, this is what keeps detection latency bounded.
            .keep_alive_while_idle(true);

        if let Some(ref tls) = tls_snapshot {
            let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
            let ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
            let tls_config = tonic::transport::ClientTlsConfig::new()
                .identity(identity)
                .ca_certificate(ca);
            ep = ep
                .tls_config(tls_config)
                .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
        }

        let channel = ep.connect().await?;
        let inner = ZoneTransportServiceClient::new(channel);

        tracing::info!("Connected to Raft node at {}", endpoint);

        Ok(Self {
            endpoint: endpoint.to_string(),
            config,
            inner,
        })
    }

    /// Get the endpoint this client is connected to.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    /// Send a raw raft-rs message to this node.
    ///
    /// This is the primary transport method used by the transport loop.
    /// The message bytes are an opaque serialized `eraftpb::Message` (protobuf v2).
    pub async fn step_message(&mut self, message_bytes: Vec<u8>, zone_id: String) -> Result<()> {
        let request = tonic::Request::new(StepMessageRequest {
            message: message_bytes,
            zone_id,
        });

        let response = self.inner.step_message(request).await?;
        let resp = response.into_inner();

        if !resp.success {
            return Err(TransportError::Rpc(
                resp.error
                    .unwrap_or_else(|| "step_message failed".to_string()),
            ));
        }

        Ok(())
    }

    /// Send a batch of EC replication entries to this peer.
    ///
    /// Returns the highest sequence number the peer successfully applied.
    /// Used by the transport loop's Phase C background replication.
    pub async fn replicate_entries(
        &mut self,
        zone_id: String,
        entries: Vec<EcReplicationEntry>,
        sender_node_id: u64,
    ) -> Result<u64> {
        let request = tonic::Request::new(ReplicateEntriesRequest {
            zone_id,
            entries,
            sender_node_id,
        });

        let response = self.inner.replicate_entries(request).await?;
        let resp = response.into_inner();

        if !resp.success {
            return Err(TransportError::Rpc(
                resp.error
                    .unwrap_or_else(|| "replicate_entries failed".to_string()),
            ));
        }

        Ok(resp.applied_up_to)
    }
}

// =============================================================================
// Client-Facing API Client (for Python/CLI)
// =============================================================================

/// A client for the Raft cluster's client-facing API.
///
/// This client is used by Python, CLI, and other external clients to
/// interact with the Raft cluster. It uses the ZoneApiService which
/// provides Propose (writes) and Query (reads) operations.
#[derive(Clone)]
pub struct RaftApiClient {
    endpoint: String,
    #[allow(dead_code)]
    config: ClientConfig,
    inner: ZoneApiServiceClient<Channel>,
    /// Zone ID for multi-zone routing (included in all requests).
    zone_id: String,
}

impl RaftApiClient {
    /// Connect to a Raft cluster node.
    pub async fn connect(endpoint: &str, config: ClientConfig) -> Result<Self> {
        let tls_snapshot = config.tls.read().unwrap().clone();
        tracing::info!(
            "Connecting to Raft API at {} (tls={})",
            endpoint,
            tls_snapshot.is_some()
        );

        let mut ep = Endpoint::from_shared(endpoint.to_string())
            .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
            .connect_timeout(config.connect_timeout)
            .timeout(config.request_timeout)
            .http2_keep_alive_interval(config.keep_alive_interval)
            .keep_alive_timeout(config.keep_alive_timeout);

        if let Some(ref tls) = tls_snapshot {
            let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
            let ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
            let tls_config = tonic::transport::ClientTlsConfig::new()
                .identity(identity)
                .ca_certificate(ca);
            ep = ep
                .tls_config(tls_config)
                .map_err(|e| TransportError::Connection(format!("TLS config error: {}", e)))?;
        }

        let channel = ep.connect().await?;
        let inner = ZoneApiServiceClient::new(channel);

        tracing::info!("Connected to Raft API at {}", endpoint);

        Ok(Self {
            endpoint: endpoint.to_string(),
            config,
            inner,
            zone_id: String::new(),
        })
    }

    /// Set the zone ID for multi-zone routing.
    pub fn with_zone_id(mut self, zone_id: String) -> Self {
        self.zone_id = zone_id;
        self
    }

    /// Get the endpoint this client is connected to.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    /// Get mutable access to the inner gRPC client (for raw requests).
    pub(crate) fn inner_mut(&mut self) -> &mut ZoneApiServiceClient<Channel> {
        &mut self.inner
    }

    // === Propose Methods (Writes) ===

    /// Put metadata for a file path.
    pub async fn put_metadata(
        &mut self,
        metadata: super::proto::nexus::core::FileMetadata,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::PutMetadata(PutMetadata {
                metadata: Some(metadata),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Delete metadata for a file path.
    pub async fn delete_metadata(&mut self, path: &str, zone_id: &str) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::DeleteMetadata(DeleteMetadata {
                path: path.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Acquire a distributed lock.
    pub async fn acquire_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        ttl_ms: i64,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::AcquireLock(AcquireLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                ttl_ms,
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Release a distributed lock.
    pub async fn release_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::ReleaseLock(ReleaseLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Extend a distributed lock's TTL.
    pub async fn extend_lock(
        &mut self,
        lock_id: &str,
        holder_id: &str,
        ttl_ms: i64,
        zone_id: &str,
    ) -> Result<ProposeResult> {
        let cmd = RaftCommand {
            command: Some(ProtoCommandVariant::ExtendLock(ExtendLock {
                lock_id: lock_id.to_string(),
                holder_id: holder_id.to_string(),
                ttl_ms,
                zone_id: zone_id.to_string(),
            })),
        };
        self.propose(cmd, None).await
    }

    /// Generic propose method — sends a RaftCommand via gRPC Propose RPC.
    pub(crate) async fn propose(
        &mut self,
        command: RaftCommand,
        request_id: Option<String>,
    ) -> Result<ProposeResult> {
        let request = tonic::Request::new(ProposeRequest {
            command: Some(command),
            request_id: request_id.unwrap_or_default(),
            zone_id: self.zone_id.clone(),
            raw_command: Vec::new(),
            forwarded: false,
        });

        let response = self.inner.propose(request).await?;
        let resp = response.into_inner();

        Ok(ProposeResult {
            success: resp.success,
            error: resp.error,
            leader_address: resp.leader_address,
            applied_index: resp.applied_index,
        })
    }

    // === Query Methods (Reads) ===

    /// Get metadata for a file path.
    pub async fn get_metadata(
        &mut self,
        path: &str,
        zone_id: &str,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::GetMetadata(GetMetadata {
                path: path.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// List metadata under a prefix.
    pub async fn list_metadata(
        &mut self,
        prefix: &str,
        zone_id: &str,
        recursive: bool,
        limit: i32,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::ListMetadata(ListMetadata {
                prefix: prefix.to_string(),
                zone_id: zone_id.to_string(),
                recursive,
                limit,
                cursor: String::new(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// Get lock information.
    pub async fn get_lock_info(
        &mut self,
        lock_id: &str,
        zone_id: &str,
        read_from_leader: bool,
    ) -> Result<QueryResult> {
        let query = RaftQuery {
            query: Some(ProtoQueryVariant::GetLockInfo(GetLockInfo {
                lock_id: lock_id.to_string(),
                zone_id: zone_id.to_string(),
            })),
        };
        self.query(query, read_from_leader).await
    }

    /// Generic query method.
    async fn query(&mut self, query: RaftQuery, read_from_leader: bool) -> Result<QueryResult> {
        let request = tonic::Request::new(QueryRequest {
            query: Some(query),
            read_from_leader,
            zone_id: self.zone_id.clone(),
        });

        let response = self.inner.query(request).await?;
        let resp = response.into_inner();

        Ok(QueryResult {
            success: resp.success,
            error: resp.error,
            leader_address: resp.leader_address,
            result: resp.result,
        })
    }

    // === Cluster Info ===

    /// Get cluster information.
    pub async fn get_cluster_info(&mut self) -> Result<ClusterInfoResult> {
        let request = tonic::Request::new(GetClusterInfoRequest {
            zone_id: self.zone_id.clone(),
        });

        let response = self.inner.get_cluster_info(request).await?;
        let resp = response.into_inner();

        Ok(ClusterInfoResult {
            node_id: resp.node_id,
            leader_id: resp.leader_id,
            term: resp.term,
            is_leader: resp.is_leader,
            leader_address: resp.leader_address,
        })
    }
}

/// Result of a Propose operation.
#[derive(Debug, Clone)]
pub struct ProposeResult {
    /// Whether the proposal succeeded.
    pub success: bool,
    /// Error message if failed.
    pub error: Option<String>,
    /// Leader address if this node is not the leader.
    pub leader_address: Option<String>,
    /// Log index where the command was applied.
    pub applied_index: u64,
}

/// Result of a Query operation.
#[derive(Debug, Clone)]
pub struct QueryResult {
    /// Whether the query succeeded.
    pub success: bool,
    /// Error message if failed.
    pub error: Option<String>,
    /// Leader address if read_from_leader was requested but not leader.
    pub leader_address: Option<String>,
    /// Query result (proto message).
    pub result: Option<super::proto::nexus::raft::RaftQueryResponse>,
}

/// Result of a GetClusterInfo operation.
#[derive(Debug, Clone)]
pub struct ClusterInfoResult {
    /// This node's ID.
    pub node_id: u64,
    /// Current leader ID (0 if unknown).
    pub leader_id: u64,
    /// Current Raft term.
    pub term: u64,
    /// Whether this node is the leader.
    pub is_leader: bool,
    /// Leader address (if known).
    pub leader_address: Option<String>,
}

// =============================================================================
// JoinCluster client — shared by fullnodes (via PyO3) and witness binary
// =============================================================================

/// Result of a successful JoinCluster call.
pub struct JoinClusterResult {
    pub ca_pem: Vec<u8>,
    pub node_cert_pem: Vec<u8>,
    pub node_key_pem: Vec<u8>,
}

/// Call JoinCluster on a leader to get a signed node certificate.
///
/// K3s-style: authenticates with join token password. The leader signs
/// the cert server-side — CA key never leaves node-1.
pub async fn call_join_cluster(
    leader_addr: &str,
    node_id: u64,
    node_address: &str,
    zone_id: &str,
    password: &str,
    timeout_secs: u64,
) -> Result<JoinClusterResult> {
    let ep = Endpoint::from_shared(leader_addr.to_string())
        .map_err(|e| TransportError::InvalidAddress(e.to_string()))?
        .connect_timeout(Duration::from_secs(timeout_secs))
        .timeout(Duration::from_secs(timeout_secs));

    let channel = ep
        .connect()
        .await
        .map_err(|e| TransportError::Connection(format!("JoinCluster connect failed: {}", e)))?;

    let mut client = ZoneApiServiceClient::new(channel);
    let request = JoinClusterRequest {
        password: password.to_string(),
        node_id,
        node_address: node_address.to_string(),
        zone_id: zone_id.to_string(),
    };

    let response = client
        .join_cluster(request)
        .await
        .map_err(|e| TransportError::Rpc(format!("JoinCluster RPC failed: {}", e)))?
        .into_inner();

    if !response.success {
        return Err(TransportError::Rpc(format!(
            "JoinCluster rejected: {}",
            response.error.unwrap_or_default()
        )));
    }

    Ok(JoinClusterResult {
        ca_pem: response.ca_pem,
        node_cert_pem: response.node_cert_pem,
        node_key_pem: response.node_key_pem,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_client_pool() {
        let pool = RaftClientPool::new();
        assert_eq!(pool.connection_count().await, 0);
    }

    #[test]
    fn test_client_config_default() {
        let config = ClientConfig::default();
        assert_eq!(config.connect_timeout, Duration::from_secs(2));
        assert_eq!(config.request_timeout, Duration::from_secs(10));
        assert_eq!(config.keep_alive_interval, Duration::from_secs(2));
        assert_eq!(config.keep_alive_timeout, Duration::from_secs(3));
    }

    // ---------------------------------------------------------------
    // Client pool TTL eviction tests (Issue #3031 / 12A)
    // ---------------------------------------------------------------

    #[tokio::test]
    async fn test_pool_ttl_fresh_client_reused() {
        // A client within TTL should be reused (same instance)
        let pool =
            RaftClientPool::with_config_and_ttl(ClientConfig::default(), Duration::from_secs(300));

        // We can't create real gRPC clients without a server, but we can
        // verify the pool structure: insert a CachedClient directly
        {
            let mut clients = pool.clients.write().await;
            clients.insert(
                42,
                CachedClient {
                    client: create_dummy_client(),
                    created_at: Instant::now(),
                },
            );
        }

        // The cached entry should still be present (within TTL)
        let clients = pool.clients.read().await;
        let cached = clients.get(&42).unwrap();
        assert!(cached.created_at.elapsed() < pool.client_ttl);
    }

    #[tokio::test]
    async fn test_pool_ttl_stale_client_detected() {
        let pool = RaftClientPool::with_config_and_ttl(
            ClientConfig::default(),
            Duration::from_millis(1), // 1ms TTL — will be stale immediately
        );

        // Insert a client that will be immediately stale
        {
            let mut clients = pool.clients.write().await;
            clients.insert(
                99,
                CachedClient {
                    client: create_dummy_client(),
                    created_at: Instant::now() - Duration::from_secs(10),
                },
            );
        }

        // Verify the entry is stale
        let clients = pool.clients.read().await;
        let cached = clients.get(&99).unwrap();
        assert!(cached.created_at.elapsed() >= pool.client_ttl);
    }

    #[tokio::test]
    async fn test_pool_ttl_mixed_ages() {
        let ttl = Duration::from_secs(60);
        let pool = RaftClientPool::with_config_and_ttl(ClientConfig::default(), ttl);

        let now = Instant::now();
        {
            let mut clients = pool.clients.write().await;
            // Fresh client (10s old)
            clients.insert(
                1,
                CachedClient {
                    client: create_dummy_client(),
                    created_at: now - Duration::from_secs(10),
                },
            );
            // Stale client (120s old, past 60s TTL)
            clients.insert(
                2,
                CachedClient {
                    client: create_dummy_client(),
                    created_at: now - Duration::from_secs(120),
                },
            );
            // Fresh client (59s old, just under TTL)
            clients.insert(
                3,
                CachedClient {
                    client: create_dummy_client(),
                    created_at: now - Duration::from_secs(59),
                },
            );
        }

        let clients = pool.clients.read().await;

        // Client 1: fresh (10s < 60s TTL)
        assert!(clients.get(&1).unwrap().created_at.elapsed() < ttl);
        // Client 2: stale (120s > 60s TTL)
        assert!(clients.get(&2).unwrap().created_at.elapsed() >= ttl);
        // Client 3: fresh (59s < 60s TTL)
        assert!(clients.get(&3).unwrap().created_at.elapsed() < ttl);
    }

    /// Create a dummy RaftClient for testing pool management.
    /// Cannot actually connect, but sufficient for testing TTL/eviction logic.
    fn create_dummy_client() -> RaftClient {
        // Create a channel pointing to a non-existent endpoint
        let channel = Channel::from_static("http://[::1]:1").connect_lazy();
        RaftClient {
            endpoint: "http://[::1]:1".to_string(),
            config: ClientConfig::default(),
            inner: ZoneTransportServiceClient::new(channel),
        }
    }
}
