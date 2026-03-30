//! Multi-zone Raft registry — manages multiple independent Raft groups per process.
//!
//! Each zone is an independent Raft group with its own:
//! - sled database (at `{base_path}/{zone_id}/`)
//! - ZoneConsensus handle + ZoneConsensusDriver actor
//! - TransportLoop background task
//!
//! The registry is thread-safe (DashMap) and supports dynamic zone creation/removal.
//!
//! # Architecture
//!
//! ```text
//!   ZoneRaftRegistry
//!   ├── "zone-alpha" → ZoneEntry { ZoneConsensus, TransportLoop task, shutdown_tx }
//!   ├── "zone-beta"  → ZoneEntry { ZoneConsensus, TransportLoop task, shutdown_tx }
//!   └── ...
//! ```

use crate::raft::{FullStateMachine, RaftConfig, RaftStorage, ReplicationLog, ZoneConsensus};
use crate::storage::RedbStore;
use crate::transport::{
    ClientConfig, NodeAddress, RaftClientPool, SharedPeerMap, TlsConfig, TransportError,
    TransportLoop,
};
use dashmap::DashMap;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use tokio::sync::watch;
use tokio::task::JoinHandle;

/// A single zone entry in the registry.
struct ZoneEntry {
    /// ZoneConsensus handle (Clone + Send + Sync).
    node: ZoneConsensus<FullStateMachine>,
    /// Known peers for this zone. Shared with TransportLoop for runtime ConfChange updates.
    peers: SharedPeerMap,
    /// This node's ID within the zone.
    #[expect(
        dead_code,
        reason = "needed for ConfChange in Phase 3; remove expect when used"
    )]
    node_id: u64,
    /// Shutdown signal for the transport loop.
    shutdown_tx: watch::Sender<bool>,
    /// Transport loop task handle (for join on removal).
    _transport_handle: JoinHandle<()>,
}

/// Per-zone search capabilities set by the Python search daemon (Issue #3147).
///
/// Stored in the registry so the Rust gRPC handler can return real capabilities
/// instead of static defaults. Python sets these at daemon startup via
/// `ZoneManager.set_search_capabilities()`.
#[derive(Debug, Clone)]
pub struct SearchCapabilitiesInfo {
    pub device_tier: String,
    pub search_modes: Vec<String>,
    pub embedding_model: String,
    pub embedding_dimensions: i32,
    pub has_graph: bool,
}

impl Default for SearchCapabilitiesInfo {
    fn default() -> Self {
        Self {
            device_tier: "server".to_string(),
            search_modes: vec!["keyword".to_string()],
            embedding_model: String::new(),
            embedding_dimensions: 0,
            has_graph: false,
        }
    }
}

/// Registry of multiple Raft zones running in a single process.
///
/// Thread-safe: all operations are safe to call from multiple threads concurrently.
pub struct ZoneRaftRegistry {
    /// zone_id → ZoneEntry
    zones: DashMap<String, ZoneEntry>,
    /// zone_id → SearchCapabilitiesInfo (set by Python search daemon)
    search_capabilities: DashMap<String, SearchCapabilitiesInfo>,
    /// Base path for sled databases. Each zone gets `{base_path}/{zone_id}/`.
    base_path: PathBuf,
    /// This node's global ID (same across all zones on this node).
    node_id: u64,
    /// Shared TLS config — can be updated at runtime for plaintext→mTLS upgrade.
    /// All zones' client pools read from this on new connections.
    tls: Arc<RwLock<Option<TlsConfig>>>,
    /// Per-zone creation guard: tracks zone_ids currently being set up.
    /// Prevents two threads from concurrently opening the same RedbStore
    /// ("Database already open") without a global mutex that would serialize
    /// creation of *different* zones.
    creating: DashMap<String, ()>,
}

impl ZoneRaftRegistry {
    /// Create a new empty registry.
    ///
    /// # Arguments
    /// * `base_path` — Base directory for zone sled databases.
    /// * `node_id` — This node's ID (used across all zones).
    pub fn new(base_path: PathBuf, node_id: u64) -> Self {
        Self {
            zones: DashMap::new(),
            search_capabilities: DashMap::new(),
            base_path,
            node_id,
            tls: Arc::new(RwLock::new(None)),
            creating: DashMap::new(),
        }
    }

    /// Create a new empty registry with TLS configuration.
    pub fn with_tls(base_path: PathBuf, node_id: u64, tls: Option<TlsConfig>) -> Self {
        Self {
            zones: DashMap::new(),
            search_capabilities: DashMap::new(),
            base_path,
            node_id,
            tls: Arc::new(RwLock::new(tls)),
            creating: DashMap::new(),
        }
    }

    /// Get a snapshot of the current TLS config.
    pub fn tls_config(&self) -> Option<TlsConfig> {
        self.tls.read().unwrap().clone()
    }

    /// Create a new zone with its own Raft group.
    ///
    /// # Arguments
    /// * `zone_id` — Unique zone identifier.
    /// * `peers` — Peer nodes in this zone's Raft group.
    /// * `runtime_handle` — Tokio runtime handle for spawning the transport loop.
    #[allow(clippy::result_large_err)]
    pub fn create_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let config = RaftConfig {
            id: self.node_id,
            peers: peer_ids,
            ..Default::default()
        };

        let campaign = peers.is_empty(); // single-node: campaign immediately
        self.setup_zone(zone_id, config, peers, campaign, runtime_handle)
    }

    /// Join an existing zone as a new Voter.
    ///
    /// Unlike `create_zone`, this does NOT bootstrap ConfState and does NOT campaign.
    /// The leader's snapshot will bring the correct voter set after ConfChange commit.
    ///
    /// After calling this, send a JoinZone RPC to the leader.
    #[allow(clippy::result_large_err)]
    pub fn join_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        // Per raft contract: joining nodes start uninitialized (empty ConfState).
        // The leader will send a snapshot with the correct voter set after
        // the ConfChange(AddNode) is committed.
        let config = RaftConfig {
            id: self.node_id,
            peers: vec![],
            skip_bootstrap: true,
            ..Default::default()
        };

        self.setup_zone(zone_id, config, peers, false, runtime_handle)
    }

    /// Internal: open sled, create ZoneConsensus + driver, spawn transport loop, register zone.
    #[allow(clippy::result_large_err)]
    fn setup_zone(
        &self,
        zone_id: &str,
        config: RaftConfig,
        peers: Vec<NodeAddress>,
        campaign: bool,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        // Fast path: zone already exists — no work needed.
        if let Some(entry) = self.zones.get(zone_id) {
            return Ok(entry.node.clone());
        }

        // Per-zone creation guard using DashMap::entry for atomic check-and-insert.
        // Prevents two threads from concurrently opening the same RedbStore
        // ("Database already open") without a global mutex that would serialize
        // creation of *different* zones. No PoisonError — DashMap is lock-free.
        // If another thread is already creating this zone, wait and return its result.
        {
            use dashmap::mapref::entry::Entry;
            match self.creating.entry(zone_id.to_string()) {
                Entry::Occupied(_occupied) => {
                    // Drop the entry ref before sleeping so we don't hold the shard lock.
                    drop(_occupied);
                    std::thread::sleep(std::time::Duration::from_millis(50));
                    return self
                        .zones
                        .get(zone_id)
                        .map(|e| e.node.clone())
                        .ok_or_else(|| {
                            TransportError::Connection(format!(
                                "Zone '{}' creation in progress by another thread",
                                zone_id,
                            ))
                        });
                }
                Entry::Vacant(v) => {
                    v.insert(());
                }
            }
        }

        // Ensure the per-zone guard is removed when we're done (success or failure).
        struct CreatingGuard<'a> {
            creating: &'a DashMap<String, ()>,
            zone_id: String,
        }
        impl<'a> Drop for CreatingGuard<'a> {
            fn drop(&mut self) {
                self.creating.remove(&self.zone_id);
            }
        }
        let _guard = CreatingGuard {
            creating: &self.creating,
            zone_id: zone_id.to_string(),
        };

        // Re-check: zone may have been created between the fast-path check
        // and acquiring the per-zone guard.
        if let Some(entry) = self.zones.get(zone_id) {
            return Ok(entry.node.clone());
        }

        // Open zone-specific redb + state machine
        let zone_path = self.base_path.join(zone_id);
        let store = RedbStore::open(zone_path.join("sm"))
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let raft_storage = RaftStorage::open(zone_path.join("raft")).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;
        let state_machine = FullStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create state machine: {}", e))
        })?;

        // Create EC replication log (non-witness nodes only)
        let replication_log = if !config.is_witness {
            let log = ReplicationLog::new(&store, config.id).map_err(|e| {
                TransportError::Connection(format!("Failed to create ReplicationLog: {}", e))
            })?;
            Some(Arc::new(log))
        } else {
            None
        };

        // Create ZoneConsensus handle + driver
        let (mut handle, mut driver) =
            ZoneConsensus::new(config, raft_storage, state_machine, replication_log).map_err(
                |e| TransportError::Connection(format!("Failed to create ZoneConsensus: {}", e)),
            )?;

        // Peer map — shared between ZoneEntry, TransportLoop, and ZoneConsensusDriver.
        let peer_map: HashMap<u64, NodeAddress> = peers.into_iter().map(|p| (p.id, p)).collect();
        let shared_peers: SharedPeerMap = Arc::new(RwLock::new(peer_map));

        driver.set_peer_map(shared_peers.clone());

        let client_config = ClientConfig {
            tls: self.tls.clone(),
            ..Default::default()
        };

        // Set up transparent leader forwarding on the handle.
        // When propose() is called on a follower, it forwards to the leader
        // via gRPC instead of returning NotLeader.
        handle.set_forward_ctx(
            RaftClientPool::with_config(client_config.clone()),
            shared_peers.clone(),
            zone_id.to_string(),
        );

        let transport_loop = TransportLoop::new(
            driver,
            shared_peers.clone(),
            RaftClientPool::with_config(client_config),
        )
        .with_zone_id(zone_id.to_string());

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let transport_handle = runtime_handle.spawn(transport_loop.run(shutdown_rx));

        if campaign {
            // Block until campaign is processed by the driver.
            // Per raft contract: for single-node, campaign() grants self-vote
            // (quorum=1) and the node becomes leader immediately. For multi-node,
            // campaign() sends MsgVote to peers and returns (election is async).
            // This ensures the node is ready before returning to the caller.
            runtime_handle
                .block_on(async { handle.campaign().await })
                .map_err(|e| TransportError::Connection(format!("Campaign failed: {}", e)))?;
        }

        tracing::info!(
            "Zone '{}' {} (node_id={}, peers={})",
            zone_id,
            if campaign { "created" } else { "joined" },
            self.node_id,
            shared_peers.read().unwrap().len()
        );

        self.zones.insert(
            zone_id.to_string(),
            ZoneEntry {
                node: handle.clone(),
                peers: shared_peers,
                node_id: self.node_id,
                shutdown_tx,
                _transport_handle: transport_handle,
            },
        );

        Ok(handle)
    }

    /// Get the ZoneConsensus handle for a zone.
    pub fn get_node(&self, zone_id: &str) -> Option<ZoneConsensus<FullStateMachine>> {
        self.zones.get(zone_id).map(|e| e.node.clone())
    }

    /// Get a snapshot of the peers map for a zone.
    /// Get the base path for zone storage directories.
    pub fn base_path(&self) -> &PathBuf {
        &self.base_path
    }

    pub fn get_peers(&self, zone_id: &str) -> Option<HashMap<u64, NodeAddress>> {
        self.zones
            .get(zone_id)
            .map(|e| e.peers.read().unwrap().clone())
    }

    /// Get cluster peer addresses from any existing zone (all zones share the same peers).
    /// Used by auto-join for new zones that don't have their own peer map yet.
    pub fn get_all_peers(&self) -> Vec<NodeAddress> {
        for entry in self.zones.iter() {
            let peers = entry.peers.read().unwrap();
            if !peers.is_empty() {
                return peers.values().cloned().collect();
            }
        }
        Vec::new()
    }

    /// Add a peer to a zone's peer map at runtime (called after ConfChange commit).
    ///
    /// The transport loop sees the new peer on its next tick because
    /// it shares the same `SharedPeerMap` via `Arc`.
    pub fn add_peer(&self, zone_id: &str, node_id: u64, address: NodeAddress) -> bool {
        if let Some(entry) = self.zones.get(zone_id) {
            entry.peers.write().unwrap().insert(node_id, address);
            true
        } else {
            false
        }
    }

    /// Get the node_id for a zone (same across all zones on this node).
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    /// Remove a zone, shutting down its transport loop.
    #[allow(clippy::result_large_err)]
    pub fn remove_zone(&self, zone_id: &str) -> Result<(), TransportError> {
        match self.zones.remove(zone_id) {
            Some((_, entry)) => {
                // Signal shutdown to transport loop
                let _ = entry.shutdown_tx.send(true);
                tracing::info!("Zone '{}' removed", zone_id);
                Ok(())
            }
            None => Err(TransportError::Connection(format!(
                "Zone '{}' not found",
                zone_id
            ))),
        }
    }

    /// List all zone IDs.
    pub fn list_zones(&self) -> Vec<String> {
        self.zones.iter().map(|e| e.key().clone()).collect()
    }

    /// Set search capabilities for a zone (Issue #3147).
    ///
    /// Called by Python search daemon at startup to register what search
    /// backends are available for each zone. The Rust gRPC handler reads
    /// these to respond to GetSearchCapabilities RPCs from remote nodes.
    pub fn set_search_capabilities(&self, zone_id: &str, caps: SearchCapabilitiesInfo) {
        self.search_capabilities.insert(zone_id.to_string(), caps);
    }

    /// Get search capabilities for a zone, or None if not set.
    pub fn get_search_capabilities(&self, zone_id: &str) -> Option<SearchCapabilitiesInfo> {
        self.search_capabilities.get(zone_id).map(|v| v.clone())
    }

    /// Shutdown all zones.
    pub fn shutdown_all(&self) {
        for entry in self.zones.iter() {
            let _ = entry.shutdown_tx.send(true);
        }
        self.zones.clear();
        tracing::info!("All zones shut down");
    }
}
