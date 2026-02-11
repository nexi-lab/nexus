//! Multi-zone Raft registry — manages multiple independent Raft groups per process.
//!
//! Each zone is an independent Raft group with its own:
//! - sled database (at `{base_path}/{zone_id}/`)
//! - RaftNode handle + RaftNodeDriver actor
//! - TransportLoop background task
//!
//! The registry is thread-safe (DashMap) and supports dynamic zone creation/removal.
//!
//! # Architecture
//!
//! ```text
//!   ZoneRaftRegistry
//!   ├── "zone-alpha" → ZoneEntry { RaftNode, TransportLoop task, shutdown_tx }
//!   ├── "zone-beta"  → ZoneEntry { RaftNode, TransportLoop task, shutdown_tx }
//!   └── ...
//! ```

use crate::raft::{FullStateMachine, RaftConfig, RaftNode, RaftStorage};
use crate::storage::SledStore;
use crate::transport::{NodeAddress, RaftClientPool, TransportError, TransportLoop};
use dashmap::DashMap;
use std::collections::HashMap;
use std::path::PathBuf;
use tokio::sync::watch;
use tokio::task::JoinHandle;

/// A single zone entry in the registry.
struct ZoneEntry {
    /// RaftNode handle (Clone + Send + Sync).
    node: RaftNode<FullStateMachine>,
    /// Known peers for this zone.
    peers: HashMap<u64, NodeAddress>,
    /// This node's ID within the zone.
    #[expect(dead_code, reason = "needed for ConfChange in Phase 3; remove expect when used")]
    node_id: u64,
    /// Shutdown signal for the transport loop.
    shutdown_tx: watch::Sender<bool>,
    /// Transport loop task handle (for join on removal).
    _transport_handle: JoinHandle<()>,
}

/// Registry of multiple Raft zones running in a single process.
///
/// Thread-safe: all operations are safe to call from multiple threads concurrently.
pub struct ZoneRaftRegistry {
    /// zone_id → ZoneEntry
    zones: DashMap<String, ZoneEntry>,
    /// Base path for sled databases. Each zone gets `{base_path}/{zone_id}/`.
    base_path: PathBuf,
    /// This node's global ID (same across all zones on this node).
    node_id: u64,
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
            base_path,
            node_id,
        }
    }

    /// Create a new zone with its own Raft group.
    ///
    /// This creates a new sled database, RaftNode, and TransportLoop for the zone.
    /// The transport loop is spawned on the provided Tokio runtime.
    ///
    /// # Arguments
    /// * `zone_id` — Unique zone identifier.
    /// * `peers` — Peer nodes in this zone's Raft group.
    /// * `lazy` — If true, use EC mode (lazy consensus) for metadata writes.
    /// * `runtime_handle` — Tokio runtime handle for spawning the transport loop.
    ///
    /// # Returns
    /// The RaftNode handle for the new zone.
    ///
    /// # Errors
    /// Returns error if zone already exists or if creation fails.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
    pub fn create_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        lazy: bool,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<RaftNode<FullStateMachine>, TransportError> {
        // Check for duplicate
        if self.zones.contains_key(zone_id) {
            return Err(TransportError::Connection(format!(
                "Zone '{}' already exists",
                zone_id
            )));
        }

        // Create zone-specific sled paths
        let zone_path = self.base_path.join(zone_id);
        let sm_path = zone_path.join("sm");
        let raft_path = zone_path.join("raft");

        // Open sled + state machine
        let store = SledStore::open(&sm_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;

        let raft_storage = RaftStorage::open(&raft_path)
            .map_err(|e| TransportError::Connection(format!("Failed to open raft storage: {}", e)))?;

        let state_machine = FullStateMachine::new(&store)
            .map_err(|e| TransportError::Connection(format!("Failed to create state machine: {}", e)))?;

        // Configure Raft node
        let peer_ids: Vec<u64> = peers.iter().map(|p| p.id).collect();
        let mut config = RaftConfig {
            id: self.node_id,
            peers: peer_ids,
            ..Default::default()
        };
        if lazy {
            // EC mode marker — the actual lazy behavior is handled in PyO3 layer
            // For now, the RaftNode itself is always SC; EC wraps it with local-apply-first
            let _ = &mut config; // placeholder for future EC config
        }

        // Create RaftNode handle + driver
        let (handle, driver) = RaftNode::new(config, raft_storage, state_machine)
            .map_err(|e| TransportError::Connection(format!("Failed to create RaftNode: {}", e)))?;

        // Peer map for transport loop
        let peer_map: HashMap<u64, NodeAddress> =
            peers.iter().cloned().map(|p| (p.id, p)).collect();

        // Create transport loop with zone_id for message routing
        let transport_loop = TransportLoop::new(driver, peer_map.clone(), RaftClientPool::new())
            .with_zone_id(zone_id.to_string());

        // Shutdown signal
        let (shutdown_tx, shutdown_rx) = watch::channel(false);

        // Spawn transport loop
        let transport_handle = runtime_handle.spawn(transport_loop.run(shutdown_rx));

        // Single-node: campaign immediately
        if peers.is_empty() {
            let campaign_node = handle.clone();
            runtime_handle.spawn(async move {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                if let Err(e) = campaign_node.campaign().await {
                    tracing::error!("Zone campaign failed: {}", e);
                }
            });
        }

        tracing::info!(
            "Zone '{}' created (node_id={}, peers={})",
            zone_id,
            self.node_id,
            peer_map.len()
        );

        let entry = ZoneEntry {
            node: handle.clone(),
            peers: peer_map,
            node_id: self.node_id,
            shutdown_tx,
            _transport_handle: transport_handle,
        };

        self.zones.insert(zone_id.to_string(), entry);

        Ok(handle)
    }

    /// Get the RaftNode handle for a zone.
    pub fn get_node(&self, zone_id: &str) -> Option<RaftNode<FullStateMachine>> {
        self.zones.get(zone_id).map(|e| e.node.clone())
    }

    /// Get the peers map for a zone.
    pub fn get_peers(&self, zone_id: &str) -> Option<HashMap<u64, NodeAddress>> {
        self.zones.get(zone_id).map(|e| e.peers.clone())
    }

    /// Get the node_id for a zone (same across all zones on this node).
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    /// Remove a zone, shutting down its transport loop.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
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

    /// Shutdown all zones.
    pub fn shutdown_all(&self) {
        for entry in self.zones.iter() {
            let _ = entry.shutdown_tx.send(true);
        }
        self.zones.clear();
        tracing::info!("All zones shut down");
    }
}