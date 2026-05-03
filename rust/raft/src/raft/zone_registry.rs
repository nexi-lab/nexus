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
use raft::eraftpb::ConfState;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use tokio::sync::watch;
use tokio::task::JoinHandle;

/// Reconcile a static peer roster with persisted Raft membership.
///
/// Dynamic voter replacement can preserve a node's hostname/port while changing
/// its Raft node ID. On restart, NEXUS_PEERS still contains the cold ID, but
/// persisted ConfState is authoritative. If there is exactly one stale ID and
/// one persisted ID missing from the transport map, carry the known address over
/// to the persisted ID so membership checks and message routing agree.
pub(crate) fn reconcile_peers_with_conf_state(
    zone_id: &str,
    peers: &mut [NodeAddress],
    conf_state: &ConfState,
) {
    let conf_ids: HashSet<u64> = conf_state
        .voters
        .iter()
        .chain(conf_state.voters_outgoing.iter())
        .chain(conf_state.learners.iter())
        .chain(conf_state.learners_next.iter())
        .copied()
        .collect();
    if conf_ids.is_empty() {
        return;
    }

    let peer_ids: HashSet<u64> = peers.iter().map(|peer| peer.id).collect();
    let missing_conf_ids: Vec<u64> = conf_ids.difference(&peer_ids).copied().collect();
    let stale_peer_ids: Vec<u64> = peer_ids.difference(&conf_ids).copied().collect();
    if missing_conf_ids.len() != 1 || stale_peer_ids.len() != 1 {
        return;
    }

    let old_id = stale_peer_ids[0];
    let new_id = missing_conf_ids[0];
    if let Some(peer) = peers.iter_mut().find(|peer| peer.id == old_id) {
        tracing::warn!(
            zone = %zone_id,
            old_id,
            new_id,
            endpoint = %peer.endpoint,
            "Reconciled peer ID from persisted Raft ConfState",
        );
        peer.id = new_id;
    }
}

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
    /// This node's advertise address — carried in outbound StepMessage
    /// `sender_address` so peers learn `(self.node_id -> address)` on
    /// inbound contact.  Set once at boot via [`Self::set_self_address`];
    /// transport tasks read it when they spawn.  Empty disables
    /// advertisement.
    self_address: Arc<RwLock<String>>,
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
            self_address: Arc::new(RwLock::new(String::new())),
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
            self_address: Arc::new(RwLock::new(String::new())),
            creating: DashMap::new(),
        }
    }

    /// Set this node's advertise address — see [`Self::self_address`].
    /// Idempotent; may be called multiple times if the operator
    /// updates the advertise address at runtime.
    pub fn set_self_address(&self, address: String) {
        *self.self_address.write().unwrap() = address;
    }

    /// Get this node's advertise address (empty when unset).
    pub fn self_address(&self) -> String {
        self.self_address.read().unwrap().clone()
    }

    /// Get a snapshot of the current TLS config.
    pub fn tls_config(&self) -> Option<TlsConfig> {
        self.tls.read().unwrap().clone()
    }

    /// Create a new zone with its own Raft group.
    ///
    /// Sync — `setup_zone` does only sync work (open redb, construct
    /// `ZoneConsensus`, `Handle::spawn` the transport loop, register in
    /// the DashMap). `Handle::spawn` is callable from any thread that
    /// has a runtime handle, regardless of whether the calling thread
    /// is itself running inside a tokio runtime, so this fn is safe
    /// from both `#[tokio::main]` async callers (e.g. `nexusd-cluster`)
    /// and bare-sync callers (e.g. PyO3 `#[pymethod]` constructors).
    ///
    /// raft contract: leader election is owned by raft-rs's `tick()`
    /// loop. For single-voter clusters, the election timer fires once
    /// and the sole voter self-elects. For multi-voter, the standard
    /// randomized timeout + MsgVote dance picks one. We do not call
    /// `campaign()` externally; the loop converges on its own.
    ///
    /// # Arguments
    /// * `zone_id` — Unique zone identifier.
    /// * `peers` — The full cluster roster for this zone (may include
    ///   this node's own `NodeAddress`; self is filtered out before
    ///   passing to raft-rs per the `RaftConfig.peers` contract).
    /// * `runtime_handle` — Tokio runtime handle for spawning the transport loop.
    #[allow(clippy::result_large_err)]
    pub fn create_zone(
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

        self.setup_zone(zone_id, config, peers, runtime_handle)
    }

    /// Join an existing zone as a Voter or Learner.
    ///
    /// Unlike `create_zone`, this does NOT bootstrap ConfState. The
    /// leader's snapshot will bring the correct voter set after
    /// ConfChange commit.
    ///
    /// `learner` is informational here — the actual Voter/Learner classification is
    /// determined by the ConfChange the leader proposes (AddNode vs AddLearnerNode).
    /// Callers must send a JoinZone RPC to the leader with the same learner flag via
    /// PyFederationClient::request_join_zone.
    #[allow(clippy::result_large_err)]
    pub fn join_zone(
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

        self.setup_zone(zone_id, config, peers, runtime_handle)
    }

    /// Open a previously-persisted zone from disk WITHOUT bootstrapping.
    ///
    /// Used by `open_existing_zones_from_disk` at startup. Unlike
    /// `create_zone`, this uses `skip_bootstrap=true` so the ConfState
    /// restored from `RaftStorage::initial_state()` is the authority —
    /// no new voters are written.
    ///
    /// R15.e: replaces the old `step_message` auto-reopen-from-disk
    /// side-effect. Enumeration at startup runs before the gRPC server
    /// accepts traffic, so by the time a vote/append arrives the zone
    /// is already registered.
    #[allow(clippy::result_large_err)]
    pub fn open_persisted_zone(
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
        self.setup_zone(zone_id, config, peers, runtime_handle)
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
    ///
    /// Sync — see [`create_zone`] for the rationale. `nexusd-cluster`
    /// calls this from inside its `#[tokio::main]` async runtime via
    /// `ZoneManager::new`; an `async fn` here would force a nested
    /// `block_on` on the outer runtime's worker thread and panic.
    #[allow(clippy::result_large_err)]
    pub fn open_existing_zones_from_disk(
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
            self.open_persisted_zone(&zone_id, peers.clone(), runtime_handle)?;
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
    ///
    /// Sync — every operation here is sync-callable: redb open, raft
    /// storage open, `FullStateMachine::new`, optional snapshot
    /// rehydration, `ZoneConsensus::new` (no I/O), and
    /// `runtime_handle.spawn` (submits the future without requiring
    /// the calling thread to be a runtime worker). Leader election is
    /// driven by raft-rs's tick loop inside the spawned transport
    /// task — no external `campaign()` call is needed; raft-rs
    /// converges on its own (single voter self-elects on the first
    /// election timeout, multi-voter runs the standard randomized
    /// MsgVote dance).
    #[allow(clippy::result_large_err)]
    fn setup_zone(
        &self,
        zone_id: &str,
        config: RaftConfig,
        mut peers: Vec<NodeAddress>,
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
        let setup_wait_started = std::time::Instant::now();
        loop {
            use dashmap::mapref::entry::Entry;
            match self.creating.entry(zone_id.to_string()) {
                Entry::Occupied(_occupied) => {
                    drop(_occupied);
                    // Another setup is in progress, or a remove is tearing the
                    // zone down. Dynamic zone creation can race with the
                    // transport auto-join path on followers, so wait for the
                    // first setup to publish the handle before treating it as
                    // an actual conflict.
                    if let Some(entry) = self.zones.get(zone_id) {
                        return Ok(entry.node.clone());
                    }
                    if setup_wait_started.elapsed() >= std::time::Duration::from_secs(3) {
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
                    // sync sleep — `setup_zone` is now sync (no campaign
                    // dependency means no `.await`). Contention is rare
                    // (only when concurrent `setup_zone` calls collide on
                    // the same zone_id) and capped at 3s, so a worker
                    // thread blocking here is acceptable.
                    std::thread::sleep(std::time::Duration::from_millis(25));
                }
                Entry::Vacant(v) => {
                    v.insert(ZoneOp::Creating);
                    break;
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
        use raft::Storage;
        if let Ok(initial_state) = raft_storage.initial_state() {
            reconcile_peers_with_conf_state(zone_id, &mut peers, &initial_state.conf_state);
        }
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
        .with_zone_id(zone_id.to_string())
        .with_self_address(self.self_address());

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let transport_handle = runtime_handle.spawn(transport_loop.run(shutdown_rx));

        // Leader election is owned by raft-rs's tick loop inside the
        // spawned transport task. We do NOT call `campaign()` here —
        // that was an optimisation to short-circuit the election timer
        // for single-voter zones, but it forced setup_zone to be async
        // (and via the `nexusd-cluster` `#[tokio::main]` async path
        // that produced a nested-runtime `block_on` panic at startup).
        // Letting raft-rs handle election keeps the protocol contract
        // pure: single-voter self-elects on the first election timeout,
        // multi-voter runs the standard randomized MsgVote dance.
        // Callers that need leader-confirmed semantics should poll via
        // `ZoneConsensus::is_leader` / `leader_id` after returning.

        tracing::info!(
            "Zone '{}' registered (node_id={}, peers={})",
            zone_id,
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

    /// Record a peer's advertise address learned from an inbound
    /// `StepMessage`.  The transport peer-map's runtime SSOT under
    /// the opaque-ID contract: every received raft message proves
    /// the sender's reachable address, so we update on every
    /// arrival rather than persist + hope.  Returns `true` if the
    /// map changed (insert or address update), `false` otherwise.
    ///
    /// Empty `endpoint` is treated as "no advertise — keep existing
    /// entry".  The caller is responsible for verifying `peer_id`
    /// is a legitimate cluster member (zone authorization upstream
    /// gates that).
    pub fn learn_peer_address(&self, zone_id: &str, peer_id: u64, endpoint: &str) -> bool {
        if endpoint.is_empty() || peer_id == 0 {
            return false;
        }
        let Some(entry) = self.zones.get(zone_id) else {
            return false;
        };
        let mut peers = entry.peers.write().unwrap();
        if let Some(existing) = peers.get(&peer_id) {
            if existing.endpoint == endpoint {
                return false;
            }
        }
        let use_tls = endpoint.starts_with("https://");
        let parsed = match NodeAddress::parse(endpoint, use_tls) {
            Ok(mut p) => {
                p.id = peer_id;
                p
            }
            Err(_) => return false,
        };
        peers.insert(peer_id, parsed);
        true
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

    #[test]
    fn test_reconcile_peers_with_conf_state_repairs_single_rotated_id() {
        let mut peers = vec![
            NodeAddress::parse("nexus-1:2126", false).unwrap(),
            NodeAddress::parse("nexus-2:2126", false).unwrap(),
            NodeAddress::parse("witness:2126", false).unwrap(),
        ];
        let stale_id = peers[1].id;
        let rotated_id = 13569616949052319723;
        let conf_state = ConfState {
            voters: vec![peers[0].id, rotated_id, peers[2].id],
            ..Default::default()
        };

        reconcile_peers_with_conf_state("root", &mut peers, &conf_state);

        assert!(!peers.iter().any(|peer| peer.id == stale_id));
        let repaired = peers
            .iter()
            .find(|peer| peer.id == rotated_id)
            .expect("rotated peer id should be present");
        assert_eq!(repaired.hostname, "nexus-2");
        assert_eq!(repaired.endpoint, "http://nexus-2:2126");
    }

    #[test]
    fn test_reconcile_peers_with_conf_state_ignores_ambiguous_mismatch() {
        let mut peers = vec![
            NodeAddress::parse("nexus-1:2126", false).unwrap(),
            NodeAddress::parse("nexus-2:2126", false).unwrap(),
            NodeAddress::parse("witness:2126", false).unwrap(),
        ];
        let original = peers.clone();
        let conf_state = ConfState {
            voters: vec![peers[0].id, 42],
            ..Default::default()
        };

        reconcile_peers_with_conf_state("root", &mut peers, &conf_state);

        assert_eq!(peers, original);
    }

    #[tokio::test]
    async fn test_open_existing_zones_from_disk_restores_confstate() {
        // Create a single-voter zone, confirm it's registered, drop the
        // registry, reopen via open_existing_zones_from_disk, assert the
        // zone is restored (skip_bootstrap=true) — the ConfState from
        // RaftStorage::initial_state() is authoritative.
        let tmp = TempDir::new().unwrap();
        let base = tmp.path().to_path_buf();

        let reg = ZoneRaftRegistry::new(base.clone(), 1);
        reg.create_zone("corp-eng", vec![], &tokio::runtime::Handle::current())
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
            .unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        let reg2 = ZoneRaftRegistry::new(base, 1);
        let first = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
            .unwrap();
        let second = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
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
            .unwrap();
        reg.create_zone("gone", vec![], &tokio::runtime::Handle::current())
            .unwrap();
        reg.remove_zone("gone").await.unwrap();
        reg.shutdown_all();
        drop(reg);
        await_shutdown_cleanup().await;

        let reg2 = ZoneRaftRegistry::new(base.clone(), 1);
        let n = reg2
            .open_existing_zones_from_disk(vec![], &tokio::runtime::Handle::current())
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
