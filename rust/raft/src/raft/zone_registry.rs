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

use crate::raft::{
    FullStateMachine, RaftConfig, RaftStorage, ReplicationLog, StateMachine, ZoneConsensus,
    ZonePersistence,
};
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

/// Per-zone concurrent-op guard. Prevents concurrent `setup_zone` and
/// `remove_zone` calls for the same zone_id from interleaving their
/// disk-dir ops. Different zone_ids proceed in parallel.
#[derive(Clone, Copy, PartialEq, Eq)]
enum ZoneOp {
    Creating,
    Removing,
}

/// A single zone entry in the registry.
struct ZoneEntry {
    /// ZoneConsensus handle (Clone + Send + Sync).
    node: ZoneConsensus<FullStateMachine>,
    /// Known peers for this zone. Shared with TransportLoop for runtime ConfChange updates.
    peers: SharedPeerMap,
    /// This node's ID within the zone.
    #[expect(
        dead_code,
        reason = "reserved for future ConfChange use; remove expect when used"
    )]
    node_id: u64,
    /// Shutdown signal for the transport loop.
    shutdown_tx: watch::Sender<bool>,
    /// Transport loop task handle (for join on removal).
    transport_handle: JoinHandle<()>,
    /// On-disk lifecycle owner. Committed (not armed) post-insert —
    /// Drop on process shutdown is a no-op; explicit `destroy()` during
    /// `remove_zone` deletes the dir.
    persistence: ZonePersistence,
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
    /// Shared TLS config — can be updated at runtime for plaintext→mTLS upgrade.
    /// All zones' client pools read from this on new connections.
    tls: Arc<RwLock<Option<TlsConfig>>>,
    /// Per-zone concurrent-op guard: tracks zone_ids currently undergoing
    /// setup or removal. Prevents two threads from concurrently opening
    /// the same RedbStore ("Database already open") and from racing a
    /// removal against a re-create. Not a global mutex, so different
    /// zone_ids proceed in parallel.
    creating: DashMap<String, ZoneOp>,
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
            tls: Arc::new(RwLock::new(None)),
            creating: DashMap::new(),
        }
    }

    /// Create a new empty registry with TLS configuration.
    pub fn with_tls(base_path: PathBuf, node_id: u64, tls: Option<TlsConfig>) -> Self {
        Self {
            zones: DashMap::new(),
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
    /// * `peers` — The full cluster roster for this zone (may include
    ///   this node's own `NodeAddress`; self is filtered out before
    ///   passing to raft-rs per the `RaftConfig.peers` contract).
    /// * `runtime_handle` — Tokio runtime handle for spawning the transport loop.
    #[allow(clippy::result_large_err)]
    pub async fn create_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        // Filter self out of the voter ID list. Callers (federation bootstrap,
        // zone_manager) commonly pass the full cluster roster from NEXUS_PEERS
        // which includes this node's own address; raft-rs expects
        // `config.peers` to list OTHER peers only, so including self would
        // produce a duplicate voter ID in ConfState.
        let peer_ids: Vec<u64> = peers
            .iter()
            .map(|p| p.id)
            .filter(|&id| id != self.node_id)
            .collect();
        let config = RaftConfig {
            id: self.node_id,
            peers: peer_ids,
            ..Default::default()
        };

        // Single-node vs multi-node: campaign immediately only if we are
        // the sole voter. peers may contain self, so check the filtered
        // ID list instead of the raw NodeAddress vec.
        let campaign = config.peers.is_empty();
        self.setup_zone(zone_id, config, peers, campaign, runtime_handle)
            .await
    }

    /// Join an existing zone as a Voter or Learner.
    ///
    /// Unlike `create_zone`, this does NOT bootstrap ConfState and does NOT campaign.
    /// The leader's snapshot will bring the correct voter set after ConfChange commit.
    ///
    /// `learner` is informational here — the actual Voter/Learner classification is
    /// determined by the ConfChange the leader proposes (AddNode vs AddLearnerNode).
    /// Callers must send a JoinZone RPC to the leader with the same learner flag via
    /// PyFederationClient::request_join_zone.
    #[allow(clippy::result_large_err)]
    pub async fn join_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        _learner: bool,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        // Per raft contract: joining nodes start uninitialized (empty ConfState).
        // The leader will send a snapshot with the correct voter set after
        // the ConfChange(AddNode/AddLearnerNode) is committed.
        let config = RaftConfig {
            id: self.node_id,
            peers: vec![],
            skip_bootstrap: true,
            ..Default::default()
        };

        self.setup_zone(zone_id, config, peers, false, runtime_handle)
            .await
    }

    /// Open a previously-persisted zone from disk WITHOUT bootstrapping.
    ///
    /// Used by `open_existing_zones_from_disk` at startup. Unlike
    /// `create_zone`, this uses `skip_bootstrap=true` so the ConfState
    /// restored from `RaftStorage::initial_state()` is the authority —
    /// no new voters are written and no campaign is triggered.
    ///
    /// R15.e: replaces the old `step_message` auto-reopen-from-disk
    /// side-effect. Enumeration at startup runs before the gRPC server
    /// accepts traffic, so by the time a vote/append arrives the zone
    /// is already registered.
    #[allow(clippy::result_large_err)]
    pub async fn open_persisted_zone(
        &self,
        zone_id: &str,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<ZoneConsensus<FullStateMachine>, TransportError> {
        let config = RaftConfig {
            id: self.node_id,
            peers: vec![],
            skip_bootstrap: true,
            ..Default::default()
        };
        self.setup_zone(zone_id, config, peers, false, runtime_handle)
            .await
    }

    /// Enumerate `base_path/*/raft/` and reopen every previously-persisted zone.
    ///
    /// Called once from `PyZoneManager::new` before the gRPC server starts
    /// accepting RPCs. Subsequent step_message traffic for unknown zones
    /// returns `NotFound` — dynamic zones arrive via `federation_create_zone`
    /// or the leader's snapshot delivery, never via a side-effectful
    /// step_message branch.
    ///
    /// This is the etcd / CockroachDB / TiKV pattern: local storage is the
    /// source of truth for "which groups does this node host?".
    ///
    /// Idempotent — re-enumeration fast-paths zones already in `self.zones`.
    #[allow(clippy::result_large_err)]
    pub async fn open_existing_zones_from_disk(
        &self,
        peers: Vec<NodeAddress>,
        runtime_handle: &tokio::runtime::Handle,
    ) -> Result<usize, TransportError> {
        if !self.base_path.exists() {
            return Ok(0);
        }
        let entries = std::fs::read_dir(&self.base_path).map_err(|e| {
            TransportError::Connection(format!(
                "Failed to read base_path {}: {}",
                self.base_path.display(),
                e
            ))
        })?;
        let mut count: usize = 0;
        for entry in entries {
            let entry = entry.map_err(|e| {
                TransportError::Connection(format!("Failed to read dir entry: {}", e))
            })?;
            // Only consider directories: each zone lives under its own
            // `{base_path}/{zone_id}/` subdir.
            if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let zone_id = entry.file_name().to_string_lossy().into_owned();

            // A tombstone means the prior run started removing this
            // zone but died before `destroy()`. Finish the cleanup rather
            // than resurrecting a zombie zone that would send raft messages
            // to peers who (correctly) return NotFound.
            if ZonePersistence::has_tombstone(&self.base_path, &zone_id) {
                if let Err(e) = ZonePersistence::cleanup_tombstoned(&self.base_path, &zone_id) {
                    tracing::warn!(
                        zone = %zone_id,
                        error = %e,
                        "Failed to clean up tombstoned zone dir at startup",
                    );
                } else {
                    tracing::info!(
                        zone = %zone_id,
                        "Cleaned up tombstoned zone dir at startup",
                    );
                }
                continue;
            }

            // Existence check: if `{zone}/raft/` doesn't exist, this dir
            // wasn't a persisted zone — skip. Matches the pattern used by
            // RaftStorage::open (which creates this subdir).
            let raft_dir = entry.path().join("raft");
            if !raft_dir.exists() {
                continue;
            }
            self.open_persisted_zone(&zone_id, peers.clone(), runtime_handle)
                .await?;
            count += 1;
        }

        // Invariant: post-enumeration, the in-memory zone count must
        // match the on-disk zone count. Violation means we failed to
        // open something that should have been opened — a regression
        // of the "disk is SSOT for zone membership" rule.
        debug_assert_eq!(
            self.zones.len(),
            count,
            "zones DashMap length ({}) != on-disk zone count ({}) after enumeration",
            self.zones.len(),
            count,
        );
        Ok(count)
    }

    /// Internal: open sled, create ZoneConsensus + driver, spawn transport loop, register zone.
    #[allow(clippy::result_large_err)]
    async fn setup_zone(
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

        // Per-zone concurrent-op guard using DashMap::entry for atomic
        // check-and-insert. Prevents (a) two threads concurrently opening the
        // same RedbStore ("Database already open") and (b) a fresh setup
        // racing an in-progress remove on the same zone_id. Different
        // zone_ids proceed in parallel — no global mutex.
        {
            use dashmap::mapref::entry::Entry;
            match self.creating.entry(zone_id.to_string()) {
                Entry::Occupied(_occupied) => {
                    drop(_occupied);
                    // Another setup is in progress, or a remove is tearing the
                    // zone down. Wait briefly and return whatever we see.
                    std::thread::sleep(std::time::Duration::from_millis(50));
                    return self
                        .zones
                        .get(zone_id)
                        .map(|e| e.node.clone())
                        .ok_or_else(|| {
                            TransportError::Connection(format!(
                                "Zone '{}' concurrent op in progress",
                                zone_id,
                            ))
                        });
                }
                Entry::Vacant(v) => {
                    v.insert(ZoneOp::Creating);
                }
            }
        }

        // Release the guard on any exit path (success or failure).
        struct CreatingGuard<'a> {
            creating: &'a DashMap<String, ZoneOp>,
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

        // Open the zone dir via ZonePersistence. Existing dir →
        // `open()` (not armed). Fresh zone → `create()` (armed; rolled back
        // on any `?` return between here and the DashMap insert). Tombstone
        // check is redundant in practice — `open_existing_zones_from_disk`
        // cleans these up at startup before setup_zone is called for them
        // — but the guard below means a crash mid-remove produces a clean
        // error on the next create attempt.
        if ZonePersistence::has_tombstone(&self.base_path, zone_id) {
            return Err(TransportError::Connection(format!(
                "Zone '{}' has a pending tombstone; cleanup before recreate",
                zone_id
            )));
        }
        let zone_dir = self.base_path.join(zone_id);
        let mut persistence = if zone_dir.exists() {
            ZonePersistence::open(&self.base_path, zone_id).map_err(|e| {
                TransportError::Connection(format!(
                    "Failed to open existing zone dir for '{}': {}",
                    zone_id, e
                ))
            })?
        } else {
            ZonePersistence::create(&self.base_path, zone_id).map_err(|e| {
                TransportError::Connection(format!(
                    "Failed to create zone dir for '{}': {}",
                    zone_id, e
                ))
            })?
        };

        // Open zone-specific redb + state machine
        let store = RedbStore::open(persistence.sm_path())
            .map_err(|e| TransportError::Connection(format!("Failed to open store: {}", e)))?;
        let raft_storage = RaftStorage::open(persistence.raft_path()).map_err(|e| {
            TransportError::Connection(format!("Failed to open raft storage: {}", e))
        })?;
        let mut state_machine = FullStateMachine::new(&store).map_err(|e| {
            TransportError::Connection(format!("Failed to create state machine: {}", e))
        })?;

        // R14 raft-rs contract fix: rehydrate advisory lock state from
        // any persisted snapshot before raft-rs gets the state machine.
        //
        // raft-rs's RaftLog::new sets `applied = first_index - 1`. If
        // the log was compacted at index X, first_index = X+1 and
        // raft-rs will only re-emit committed entries in [X+1..commit]
        // on startup. It does NOT re-emit the stored snapshot itself
        // — Ready's `snapshot` field is only populated by a *new*
        // snapshot received from the leader at runtime.
        //
        // Pre-R14 this didn't matter: advisory lock state was persisted
        // row-by-row in redb, so FullStateMachine::new loaded it from
        // there. After R14 the BTreeMap is in-memory only; without
        // this rehydration, any advisory holders committed before the
        // last compact would be lost on restart. Rehydrating here
        // keeps the post-restart state machine consistent with other
        // replicas that are caught up via the normal log-replay path.
        use raft::Storage;
        if let Ok(snap) = raft_storage.snapshot(0, 0) {
            let meta = snap.get_metadata();
            if meta.index > 0 && !snap.data.is_empty() {
                state_machine.restore_snapshot(&snap.data).map_err(|e| {
                    TransportError::Connection(format!(
                        "Failed to rehydrate state machine from stored snapshot at index {}: {}",
                        meta.index, e
                    ))
                })?;
                tracing::info!(
                    zone = %zone_id,
                    snapshot_index = meta.index,
                    snapshot_term = meta.term,
                    "Rehydrated advisory lock state from stored snapshot on startup",
                );
            }
        }

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
            // Per raft contract: for single-node, campaign() grants self-vote
            // (quorum=1) and the node becomes leader immediately. For multi-node,
            // campaign() sends MsgVote to peers and returns (election is async).
            // Awaiting it here ensures the node is ready before we return.
            //
            // History note: this used to be a sync `setup_zone` that called
            // `runtime_handle.block_on(handle.campaign())`. That violated
            // tokio's contract — `Handle::block_on` panics when called from an
            // async context, and the gRPC step_message handler's auto-join path
            // IS async. The panic silently unwound through setup_zone, dropped
            // the local `shutdown_tx`, and the transport loop we had just
            // spawned shut down before the zone was registered — which
            // manifested as the failover catch-up timeout (node-1 never
            // actually became a voter of its own zones after restart).
            //
            // The proper fix is to respect tokio's sync/async boundary: keep
            // setup_zone async, let sync callers bridge at their own boundary
            // (PyO3 uses its runtime_handle.block_on), and let async callers
            // just await. No `block_on` inside setup_zone.
            handle
                .campaign()
                .await
                .map_err(|e| TransportError::Connection(format!("Campaign failed: {}", e)))?;
        }

        tracing::info!(
            "Zone '{}' {} (node_id={}, peers={})",
            zone_id,
            if campaign { "created" } else { "joined" },
            self.node_id,
            shared_peers.read().unwrap().len()
        );

        // Commit the on-disk handle before publishing the entry.
        // Post-commit, Drop is a no-op on disk (process shutdown preserves
        // persisted zones). Only explicit `destroy()` in `remove_zone`
        // deletes the dir.
        persistence.commit();

        self.zones.insert(
            zone_id.to_string(),
            ZoneEntry {
                node: handle.clone(),
                peers: shared_peers,
                node_id: self.node_id,
                shutdown_tx,
                transport_handle,
                persistence,
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

    /// Remove a zone — shut down its transport loop and delete its on-disk
    /// dir atomically via tombstone.
    ///
    /// Sequence:
    /// 1. Take the entry out of the DashMap (further `get_node` returns None).
    /// 2. Write the tombstone file — the durable commit point of "this zone
    ///    is being torn down". A crash after this leaves a tombstoned dir;
    ///    next startup's `open_existing_zones_from_disk` completes cleanup.
    /// 3. Signal shutdown to the transport loop, await its JoinHandle so
    ///    the spawned task has fully exited before we drop `ZoneConsensus`.
    /// 4. Drop `ZoneConsensus` (entry goes out of scope). Driver task
    ///    exits, closing all redb table handles so `remove_dir_all` can
    ///    succeed on Windows (which refuses to delete open-handle files).
    /// 5. `persistence.destroy()` — the `rmdir -r`.
    #[allow(clippy::result_large_err)]
    pub async fn remove_zone(&self, zone_id: &str) -> Result<(), TransportError> {
        // Serialize against setup_zone on the same zone_id.
        {
            use dashmap::mapref::entry::Entry;
            match self.creating.entry(zone_id.to_string()) {
                Entry::Occupied(_occupied) => {
                    drop(_occupied);
                    return Err(TransportError::Connection(format!(
                        "Zone '{}' concurrent op in progress; retry remove shortly",
                        zone_id,
                    )));
                }
                Entry::Vacant(v) => {
                    v.insert(ZoneOp::Removing);
                }
            }
        }
        struct RemovingGuard<'a> {
            creating: &'a DashMap<String, ZoneOp>,
            zone_id: String,
        }
        impl<'a> Drop for RemovingGuard<'a> {
            fn drop(&mut self) {
                self.creating.remove(&self.zone_id);
            }
        }
        let _guard = RemovingGuard {
            creating: &self.creating,
            zone_id: zone_id.to_string(),
        };

        let (_, entry) = self
            .zones
            .remove(zone_id)
            .ok_or_else(|| TransportError::Connection(format!("Zone '{}' not found", zone_id)))?;

        let ZoneEntry {
            node,
            peers: _,
            node_id: _,
            shutdown_tx,
            transport_handle,
            persistence,
        } = entry;

        // Commit point: the tombstone is what makes teardown crash-safe.
        // If this write fails, the caller sees the error and the zone is
        // re-registered (we already did `zones.remove`). Accept this edge
        // case: the zone is gone from memory, dir is still on disk; on
        // next restart `open_existing_zones_from_disk` reopens it. No
        // zombie — no remote peers were told this zone is dying.
        if let Err(e) = persistence.write_tombstone() {
            // Best-effort: put the zone back so state isn't lost from memory.
            self.zones.insert(
                zone_id.to_string(),
                ZoneEntry {
                    node,
                    peers: Arc::new(RwLock::new(HashMap::new())),
                    node_id: self.node_id,
                    shutdown_tx,
                    transport_handle,
                    persistence,
                },
            );
            return Err(TransportError::Connection(format!(
                "Failed to write tombstone for zone '{}': {}",
                zone_id, e
            )));
        }

        // Signal transport shutdown and await its exit so Windows release
        // of file handles completes before we try to rmdir.
        let _ = shutdown_tx.send(true);
        if let Err(e) = transport_handle.await {
            tracing::warn!(
                zone = %zone_id,
                error = %e,
                "Transport loop task failed during remove_zone; continuing with destroy",
            );
        }

        // Explicitly drop the ZoneConsensus handle so the driver task's
        // last reference goes away. On Windows, any surviving redb handle
        // would fail remove_dir_all with PermissionDenied.
        drop(node);

        // Short yield to let the driver task observe the dropped handle
        // and exit before we attempt rmdir. The driver uses an internal
        // channel with this as the only external reference (besides the
        // clones given to the zone's own transport/gRPC surfaces, all of
        // which are already gone by this point).
        tokio::task::yield_now().await;

        if let Err(e) = persistence.destroy() {
            // Dir deletion failed — log but don't resurrect the zone.
            // Tombstone is still on disk; next startup will retry cleanup.
            tracing::warn!(
                zone = %zone_id,
                error = %e,
                "Failed to delete zone dir; tombstone preserved for startup cleanup",
            );
        }

        tracing::info!("Zone '{}' removed", zone_id);
        Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[tokio::test]
    async fn test_open_existing_zones_empty_base_path() {
        // Empty (nonexistent) base_path returns Ok(0) — no zones to open.
        let tmp = TempDir::new().unwrap();
        let missing = tmp.path().join("does-not-exist");
        let reg = ZoneRaftRegistry::new(missing, 1);
        let n = reg
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(n, 0);
        assert!(reg.list_zones().is_empty());
    }

    /// Wait for a transport task's held Arc<RedbStore> to be released
    /// after `shutdown_all`. The transport loop's tick period is ~100ms,
    /// so 500ms is a generous margin. Test-only — production paths use
    /// the explicit transport shutdown handshake, not a sleep.
    async fn await_shutdown_cleanup() {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }

    #[tokio::test]
    async fn test_open_existing_zones_from_disk_restores_confstate() {
        // Create a single-voter zone (campaign=true), confirm it's registered,
        // drop the registry, reopen via open_existing_zones_from_disk, assert
        // the zone is restored without a fresh campaign (skip_bootstrap=true)
        // — the ConfState from RaftStorage::initial_state() is authoritative.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();

        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("corp-eng", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(reg.list_zones(), vec!["corp-eng".to_string()]);
        // Simulate process restart: shutdown tasks, release file locks.
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        // New registry, same base_path — enumerate from disk.
        let reg2 = ZoneRaftRegistry::new(base, 1);
        let n = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(n, 1);
        let zones = reg2.list_zones();
        assert_eq!(zones, vec!["corp-eng".to_string()]);
        assert!(reg2.get_node("corp-eng").is_some());

        reg2.shutdown_all();
        await_shutdown_cleanup().await;
    }

    #[tokio::test]
    async fn test_open_existing_zones_idempotent() {
        // Second enumeration is a no-op: setup_zone fast-paths zones
        // already registered in self.zones.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();
        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("zone-a", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        let reg2 = ZoneRaftRegistry::new(base, 1);
        let first = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        let second = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(first, 1);
        assert_eq!(second, 1);
        assert_eq!(reg2.list_zones().len(), 1);

        reg2.shutdown_all();
        await_shutdown_cleanup().await;
    }

    // Zone lifecycle regression tests — zone lifecycle is crash-safe
    // and disk-dir existence is the authoritative answer to "does
    // this node host zone X?".

    #[tokio::test]
    async fn test_remove_zone_deletes_disk_dir() {
        // remove_zone() must delete {base}/{zone_id}/ so the next
        // open_existing_zones_from_disk doesn't resurrect it as a zombie.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();
        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("temp-zone", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert!(
            base.join("temp-zone").exists(),
            "zone dir should exist after create"
        );

        reg.remove_zone("temp-zone").await.unwrap();
        assert!(
            !base.join("temp-zone").exists(),
            "zone dir must be gone after remove_zone",
        );
        assert!(reg.get_node("temp-zone").is_none());
        assert!(reg.list_zones().is_empty());

        reg.shutdown_all();
        await_shutdown_cleanup().await;
    }

    #[tokio::test]
    async fn test_remove_then_reopen_existing_excludes_removed_zone() {
        // After a remove, a fresh registry on the same base_path must not
        // resurrect the removed zone — matching the zombie-zone fix.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();
        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("keep", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        reg.create_zone("gone", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        reg.remove_zone("gone").await.unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        let reg2 = ZoneRaftRegistry::new(base.clone(), 1);
        let n = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(n, 1);
        assert_eq!(reg2.list_zones(), vec!["keep".to_string()]);
        assert!(!base.join("gone").exists());

        reg2.shutdown_all();
        await_shutdown_cleanup().await;
    }

    #[tokio::test]
    async fn test_tombstone_cleanup_on_startup() {
        // Simulate a crash between write_tombstone() and destroy(): the
        // zone dir is still on disk along with a .removed marker. Startup
        // must finish the cleanup instead of resurrecting the zombie.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();
        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("crash-zone", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        // Plant a tombstone by hand to mimic a crashed-mid-teardown run.
        std::fs::write(base.join("crash-zone").join(".removed"), b"").unwrap();
        assert!(base.join("crash-zone").exists());

        let reg2 = ZoneRaftRegistry::new(base.clone(), 1);
        let n = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        assert_eq!(n, 0, "tombstoned zone must not be reopened");
        assert!(
            !base.join("crash-zone").exists(),
            "tombstoned dir must be cleaned up on startup",
        );
        assert!(reg2.list_zones().is_empty());

        reg2.shutdown_all();
        await_shutdown_cleanup().await;
    }

    #[tokio::test]
    async fn test_shutdown_all_preserves_disk() {
        // Regression guard: process shutdown must NOT delete zone dirs
        // (post-commit `armed == false`; Drop is a no-op on disk).
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();
        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("persist", vec![], &tokio::runtime::Handle::current())
            .await
            .unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        assert!(
            base.join("persist").exists(),
            "shutdown must preserve zone dir"
        );
    }
}
