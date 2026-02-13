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
    /// Optional TLS config for outbound client connections (shared across all zones).
    tls: Option<TlsConfig>,
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
            tls: None,
        }
    }

    /// Create a new empty registry with TLS configuration.
    pub fn with_tls(base_path: PathBuf, node_id: u64, tls: Option<TlsConfig>) -> Self {
        Self {
            zones: DashMap::new(),
            base_path,
            node_id,
            tls,
        }
    }

    /// Create a new zone with its own Raft group.
    ///
    /// # Arguments
    /// * `zone_id` — Unique zone identifier.
    /// * `peers` — Peer nodes in this zone's Raft group.
    /// * `runtime_handle` — Tokio runtime handle for spawning the transport loop.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
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
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
    pub fn join_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        // Empty peers in config → no ConfState bootstrap. Snapshot overwrites.
        let config = RaftConfig {
            id: self.node_id,
            peers: vec![],
            ..Default::default()
        };

        self.setup_zone(zone_id, config, peers, false, runtime_handle)
    }

    /// Internal: open sled, create ZoneConsensus + driver, spawn transport loop, register zone.
    #[expect(
        clippy::result_large_err,
        reason = "TransportError contains tonic types; will Box in transport refactor"
    )]
    fn setup_zone(
        &self,
        zone_id: &str,
        config: RaftConfig,
        peers: Vec<NodeAddress>,
        campaign: bool,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        if self.zones.contains_key(zone_id) {
            return Err(TransportError::Connection(format!(
                "Zone '{}' already exists",
                zone_id
            )));
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
        let (handle, mut driver) =
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
        let transport_loop =
            TransportLoop::new(driver, shared_peers.clone(), RaftClientPool::with_config(client_config))
                .with_zone_id(zone_id.to_string());

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let transport_handle = runtime_handle.spawn(transport_loop.run(shutdown_rx));

        if campaign {
            let campaign_node = handle.clone();
            runtime_handle.spawn(async move {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                if let Err(e) = campaign_node.campaign().await {
                    tracing::error!("Zone campaign failed: {}", e);
                }
            });
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
    pub fn get_peers(&self, zone_id: &str) -> Option<HashMap<u64, NodeAddress>> {
        self.zones
            .get(zone_id)
            .map(|e| e.peers.read().unwrap().clone())
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
