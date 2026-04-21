//! Raft node implementation — channel/actor pattern (etcd/tikv style).
//!
//! # Architecture: Single-Owner Actor Pattern
//!
//! raft-rs's `RawNode` is **NOT** thread-safe. All mutating operations (step,
//! propose, tick, ready, advance) must happen sequentially from a single owner.
//! This is the same contract as etcd (single goroutine) and tikv (PeerFsmDelegate).
//!
//! We enforce this at **compile time** by splitting into two types:
//!
//! - [`ZoneConsensus`] — the public **handle** (Clone + Send + Sync). External code
//!   (gRPC handlers, PyO3, tests) uses this. All mutating operations go through
//!   an `mpsc` channel to the driver.
//!
//! - [`ZoneConsensusDriver`] — the private **actor** that exclusively owns `RawNode`.
//!   Only the transport loop's single task may call its methods. `RawNode` is a
//!   private field that cannot be accessed from outside this module.
//!
//! ```text
//! ┌─────────────────────────────────────────────────────────┐
//! │  ZoneConsensusDriver (single owner, runs in TransportLoop)   │
//! │  ┌──────────────┐  ┌────────────────┐                   │
//! │  │ RawNode       │  │ StateMachine   │ ← shared Arc     │
//! │  │ (NO lock)     │  │ (RwLock, read) │                   │
//! │  │ pending map   │  └────────────────┘                   │
//! │  └──────────────┘                                       │
//! └────────┬────────────────────────────────────────────────┘
//!          │ mpsc::UnboundedReceiver<RaftMsg>
//!     ┌────┴──────┐
//!     │ ZoneConsensus   │  ← Clone + Send + Sync (the handle)
//!     │ (tx only)  │
//!     └────┬──────┘
//!          │ mpsc::UnboundedSender<RaftMsg>
//!     ┌────┴──────────────────────────┐
//!     │ gRPC handlers: send Step      │
//!     │ PyO3 propose: send Propose    │
//!     │ startup: send Campaign        │
//!     └───────────────────────────────┘
//! ```
//!
//! # INVARIANT
//!
//! **`RawNode` must NEVER be exposed outside `ZoneConsensusDriver`.** Do not add
//! `pub` to `raw_node`, do not return references to it, do not create methods
//! that bypass the channel. Violating this invariant causes the
//! `"not leader but has new msg after advance"` panic under concurrent load.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use raft::eraftpb::{
    ConfChange, ConfChangeType, ConfChangeV2, ConfState, Entry, EntryType, Message, Snapshot,
};
use raft::{Config, RawNode, Storage};
use slog::{o, Logger};
use tokio::sync::{mpsc, oneshot, RwLock};

use super::replication_log::ReplicationLog;
use super::state_machine::StateMachine;
use super::storage::RaftStorage;
use super::{Command, CommandResult, RaftError, Result};

/// Capacity of the bounded channel between [`ZoneConsensus`] handles and the
/// [`ZoneConsensusDriver`] actor. Provides backpressure under sustained
/// overload or network partitions, preventing unbounded memory growth.
/// 256 aligns with tokio's internal 32-message block allocation.
const DRIVER_CHANNEL_CAPACITY: usize = 256;

/// Convert a bounded channel `TrySendError` to a `RaftError`.
fn channel_try_send_err<T>(e: mpsc::error::TrySendError<T>) -> RaftError {
    match e {
        mpsc::error::TrySendError::Full(_) => {
            tracing::warn!(
                capacity = DRIVER_CHANNEL_CAPACITY,
                "raft driver channel full, applying backpressure"
            );
            RaftError::ChannelFull(DRIVER_CHANNEL_CAPACITY)
        }
        mpsc::error::TrySendError::Closed(_) => RaftError::ChannelClosed,
    }
}

#[cfg(all(feature = "grpc", has_protos))]
use crate::transport::{NodeAddress, RaftClientPool, SharedPeerMap};

/// Configuration for a Raft node.
#[derive(Debug, Clone)]
pub struct RaftConfig {
    /// Unique node ID within the cluster.
    pub id: u64,

    /// IDs of peer nodes in the cluster.
    pub peers: Vec<u64>,

    /// Number of ticks before triggering election.
    /// An election tick is typically 100-500ms.
    pub election_tick: usize,

    /// Number of ticks between heartbeats.
    /// Should be much smaller than election_tick (e.g., election_tick / 3).
    pub heartbeat_tick: usize,

    /// Maximum size of entries in a single append message.
    pub max_size_per_msg: u64,

    /// Maximum number of in-flight append messages.
    pub max_inflight_msgs: usize,

    /// Whether this node is a witness (vote-only, no state machine).
    pub is_witness: bool,

    /// Tick interval (how often to call tick()).
    pub tick_interval: Duration,

    /// Skip ConfState bootstrap for joining nodes.
    ///
    /// When true, the node starts with an empty ConfState and waits for
    /// the leader to send a snapshot with the correct voter set.
    /// Per raft contract: joining nodes must NOT bootstrap themselves.
    pub skip_bootstrap: bool,
}

impl Default for RaftConfig {
    fn default() -> Self {
        Self {
            id: 1,
            peers: vec![],
            election_tick: 10,
            heartbeat_tick: 3,
            max_size_per_msg: 1024 * 1024, // 1MB
            max_inflight_msgs: 256,
            is_witness: false,
            tick_interval: Duration::from_millis(10),
            skip_bootstrap: false,
        }
    }
}

/// Election tick for witness nodes: effectively infinite (~27 hours at 10ms/tick).
///
/// Prevents raft-rs from internally transitioning the witness to Candidate
/// state on election timeout. This is Layer 3 of TiKV-style witness defense:
///   - Layer 1: `priority = -1` (raft-rs native deprioritization)
///   - Layer 2: Drop outgoing campaign messages in `advance()`
///   - Layer 3: Prevent election timeout from ever firing
const WITNESS_ELECTION_TICK: usize = 10_000_000;

impl RaftConfig {
    /// Create a configuration for a witness node.
    pub fn witness(id: u64, peers: Vec<u64>) -> Self {
        Self {
            id,
            peers,
            is_witness: true,
            election_tick: WITNESS_ELECTION_TICK,
            ..Default::default()
        }
    }

    /// Convert to raft-rs Config.
    ///
    /// Witness nodes get `priority = -1` so raft-rs natively deprioritizes
    /// them during leader election (Layer 1 of TiKV-style witness defense).
    fn to_raft_config(&self) -> Config {
        Config {
            id: self.id,
            election_tick: self.election_tick,
            heartbeat_tick: self.heartbeat_tick,
            max_size_per_msg: self.max_size_per_msg,
            max_inflight_msgs: self.max_inflight_msgs,
            priority: if self.is_witness { -1 } else { 0 },
            ..Default::default()
        }
    }
}

/// Role of a Raft node.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeRole {
    /// Follower: accepts log entries from leader.
    Follower,
    /// Candidate: requesting votes for leader election.
    Candidate,
    /// Leader: handles client requests and replicates log.
    Leader,
    /// Pre-candidate: pre-vote phase before becoming candidate.
    PreCandidate,
}

impl From<raft::StateRole> for NodeRole {
    fn from(role: raft::StateRole) -> Self {
        match role {
            raft::StateRole::Follower => NodeRole::Follower,
            raft::StateRole::Candidate => NodeRole::Candidate,
            raft::StateRole::Leader => NodeRole::Leader,
            raft::StateRole::PreCandidate => NodeRole::PreCandidate,
        }
    }
}

/// Pending proposal waiting for commit.
struct PendingProposal {
    /// Channel to send result back.
    tx: oneshot::Sender<Result<CommandResult>>,
}

// ---------------------------------------------------------------------------
// RaftMsg — the message type for the actor channel
// ---------------------------------------------------------------------------

/// Messages sent from the [`ZoneConsensus`] handle to the [`ZoneConsensusDriver`] actor.
///
/// Each variant carries enough data for the driver to execute the operation
/// on `RawNode` sequentially. Request-response variants include a `oneshot`
/// sender for the caller to await the result.
pub enum RaftMsg {
    /// Feed an inbound Raft message (from a peer) into raft-rs.
    Step { msg: Message },
    /// Propose a client command for replication.
    Propose {
        data: Vec<u8>,
        proposal_id: u64,
        tx: oneshot::Sender<Result<CommandResult>>,
    },
    /// Propose a configuration change (add/remove node).
    /// The tx resolves after the ConfChange is **committed and applied** (not just enqueued).
    ProposeConfChange {
        change: ConfChange,
        tx: oneshot::Sender<Result<ConfState>>,
    },
    /// Campaign to become leader.
    Campaign { tx: oneshot::Sender<Result<()>> },
}

// ---------------------------------------------------------------------------
// ZoneConsensus — the public HANDLE (Clone + Send + Sync)
// ---------------------------------------------------------------------------

/// The public API for Raft operations.
///
/// All mutating operations (step, propose, campaign) go through an internal
/// `mpsc` channel to the [`ZoneConsensusDriver`] actor. Read operations (role,
/// term, leader_id) use atomic cached values updated by the driver after
/// each `advance()`. State machine reads use a shared `Arc<RwLock<S>>`.
///
/// This type is `Clone + Send + Sync` and can be freely shared across
/// gRPC handlers, PyO3, and other contexts.
/// Transport context for transparent leader forwarding.
///
/// When a follower receives a propose(), instead of returning NotLeader,
/// it forwards the command to the leader via gRPC. This makes propose()
/// work correctly regardless of which node the caller is connected to.
///
/// Optional: embedded/single-node mode sets this to None (no forwarding).
#[cfg(all(feature = "grpc", has_protos))]
#[derive(Clone)]
struct ForwardContext {
    client_pool: RaftClientPool,
    peers: SharedPeerMap,
    zone_id: String,
    /// Cached API-level client for leader forwarding.
    /// Lazily connected on first use, evicted and reconnected on error.
    /// `Arc<tokio::sync::Mutex<..>>` so `ForwardContext` stays Clone + Send.
    cached_api_client:
        std::sync::Arc<tokio::sync::Mutex<Option<(String, crate::transport::RaftApiClient)>>>,
    /// Cached leader capability: whether endpoint supports raw_result forwarding.
    leader_raw_result_support:
        std::sync::Arc<tokio::sync::Mutex<Option<(String, bool, std::time::Instant)>>>,
}

pub struct ZoneConsensus<S: StateMachine + 'static> {
    /// Bounded channel sender to the driver actor.
    /// Capacity: [`DRIVER_CHANNEL_CAPACITY`]. Provides backpressure when
    /// the driver cannot keep up with incoming messages.
    msg_tx: mpsc::Sender<RaftMsg>,
    /// Shared state machine for read-only queries (no channel needed).
    state_machine: Arc<RwLock<S>>,
    /// Node configuration.
    config: RaftConfig,
    /// Cached role, updated by driver after each advance().
    cached_role: Arc<AtomicU8>,
    /// Cached leader ID, updated by driver after each advance().
    cached_leader_id: Arc<AtomicU64>,
    /// Cached term, updated by driver after each advance().
    cached_term: Arc<AtomicU64>,
    /// EC replication WAL (None for witness nodes that don't store data).
    replication_log: Option<Arc<ReplicationLog>>,
    /// Transport context for forwarding proposals to the leader.
    /// None in embedded/single-node mode.
    #[cfg(all(feature = "grpc", has_protos))]
    forward_ctx: Option<ForwardContext>,
}

impl<S: StateMachine + 'static> Clone for ZoneConsensus<S> {
    fn clone(&self) -> Self {
        Self {
            msg_tx: self.msg_tx.clone(),
            state_machine: self.state_machine.clone(),
            config: self.config.clone(),
            cached_role: self.cached_role.clone(),
            cached_leader_id: self.cached_leader_id.clone(),
            cached_term: self.cached_term.clone(),
            replication_log: self.replication_log.clone(),
            #[cfg(all(feature = "grpc", has_protos))]
            forward_ctx: self.forward_ctx.clone(),
        }
    }
}

// ---------------------------------------------------------------------------
// ZoneConsensusDriver — the private ACTOR (single owner, NOT Clone)
// ---------------------------------------------------------------------------

/// SAFETY: This struct owns the raft-rs `RawNode` **exclusively**.
///
/// DO NOT expose `raw_node` through any public method, add `pub` to any
/// field, or create methods that return references to `raw_node`.
/// Violating this breaks the raft-rs single-owner contract and causes
/// panics under concurrent load.
///
/// See: `"not leader but has new msg after advance"` panic.
///
/// Only the transport loop's single task may call methods on this struct.
pub struct ZoneConsensusDriver<S: StateMachine + 'static> {
    /// PRIVATE — NEVER make pub. raft-rs `RawNode` is NOT thread-safe.
    /// All access must go through the channel ([`RaftMsg`]). Exposing this
    /// field will cause `"not leader but has new msg after advance"` panics.
    raw_node: RawNode<RaftStorage>,
    /// Shared state machine (shared with handle for reads).
    state_machine: Arc<RwLock<S>>,
    /// Node configuration.
    config: RaftConfig,
    /// Pending proposals waiting for commit, keyed by proposal ID.
    pending: HashMap<u64, PendingProposal>,
    /// Pending ConfChanges waiting for commit, keyed by target node_id.
    /// Resolved in `apply_entries` when the ConfChange is committed.
    pending_conf_changes: HashMap<u64, oneshot::Sender<Result<ConfState>>>,
    /// Proposal ID counter (shared with handle for ID generation).
    proposal_id: Arc<AtomicU64>,
    /// Last tick time.
    last_tick: Instant,
    /// Bounded channel receiver — messages from the handle.
    msg_rx: mpsc::Receiver<RaftMsg>,
    /// Cached role (shared with handle for reads).
    cached_role: Arc<AtomicU8>,
    /// Cached leader ID (shared with handle for reads).
    cached_leader_id: Arc<AtomicU64>,
    /// Cached term (shared with handle for reads).
    cached_term: Arc<AtomicU64>,
    /// Shared peer map — updated when ConfChange adds/removes nodes.
    /// Set by `set_peer_map()` before the transport loop starts.
    #[cfg(all(feature = "grpc", has_protos))]
    peer_map: Option<SharedPeerMap>,
    /// EC replication WAL (shared with handle via Arc).
    /// Used by the transport loop for Phase C background replication.
    replication_log: Option<Arc<ReplicationLog>>,
}

// ---------------------------------------------------------------------------
// ZoneConsensus (handle) implementation
// ---------------------------------------------------------------------------

/// Atomic encoding for [`NodeRole`].
const ROLE_FOLLOWER: u8 = 0;
const ROLE_CANDIDATE: u8 = 1;
const ROLE_LEADER: u8 = 2;
const ROLE_PRE_CANDIDATE: u8 = 3;

/// Timeout for proposals and conf changes waiting for commit.
const PROPOSAL_TIMEOUT_SECS: u64 = 10;

impl NodeRole {
    fn to_u8(self) -> u8 {
        match self {
            NodeRole::Follower => ROLE_FOLLOWER,
            NodeRole::Candidate => ROLE_CANDIDATE,
            NodeRole::Leader => ROLE_LEADER,
            NodeRole::PreCandidate => ROLE_PRE_CANDIDATE,
        }
    }

    fn from_u8(v: u8) -> Self {
        match v {
            ROLE_CANDIDATE => NodeRole::Candidate,
            ROLE_LEADER => NodeRole::Leader,
            ROLE_PRE_CANDIDATE => NodeRole::PreCandidate,
            _ => NodeRole::Follower,
        }
    }
}

impl<S: StateMachine + 'static> ZoneConsensus<S> {
    /// Create a new Raft node, returning a (handle, driver) pair.
    ///
    /// The **handle** is Clone + Send + Sync and should be shared with gRPC
    /// handlers, PyO3, etc. The **driver** must be passed to the transport
    /// loop which will call [`ZoneConsensusDriver::process_messages`] and
    /// [`ZoneConsensusDriver::advance`] sequentially from a single task.
    ///
    /// If the storage has no existing ConfState (fresh cluster), initializes
    /// the voter set with this node and all configured peers.
    pub fn new(
        config: RaftConfig,
        storage: RaftStorage,
        state_machine: S,
        replication_log: Option<Arc<ReplicationLog>>,
    ) -> Result<(Self, ZoneConsensusDriver<S>)> {
        // Bootstrap: set initial ConfState if this is a fresh cluster
        let initial_state = storage
            .initial_state()
            .map_err(|e| RaftError::Storage(e.to_string()))?;

        if !config.skip_bootstrap {
            // Build the expected voter set from config.
            let mut voters = vec![config.id];
            voters.extend(config.peers.iter());
            let persisted = &initial_state.conf_state;
            let local_in_conf_state = persisted.voters.contains(&config.id)
                || persisted.learners.contains(&config.id)
                || persisted.voters_outgoing.contains(&config.id)
                || persisted.learners_next.contains(&config.id);
            if !persisted.voters.is_empty() && !local_in_conf_state {
                return Err(RaftError::InvalidState(format!(
                    "Persisted ConfState (voters={:?}, learners={:?}) does not include local node {}",
                    persisted.voters, persisted.learners, config.id
                )));
            }

            let needs_bootstrap = if initial_state.conf_state.voters.is_empty() {
                // Fresh cluster — no ConfState in storage yet.
                true
            } else {
                false
            };

            if needs_bootstrap {
                // Bootstrap: create initial voter set.
                // Joining nodes (skip_bootstrap=true) must NOT bootstrap — they
                // start uninitialized and receive the correct ConfState via
                // snapshot from the leader (per raft contract).
                let cs = ConfState {
                    voters: voters.clone(),
                    ..Default::default()
                };
                storage.set_conf_state(&cs).map_err(|e| {
                    RaftError::Storage(format!("failed to set initial ConfState: {e}"))
                })?;
                tracing::info!("Bootstrapped ConfState with voters: {:?}", voters);
            }
        }

        let raft_config = config.to_raft_config();

        // Create a discard logger for raft-rs (we use tracing for our own logging)
        let logger = Logger::root(slog::Discard, o!());

        // Create the raw node
        let raw_node = RawNode::new(&raft_config, storage, &logger)
            .map_err(|e| RaftError::Raft(e.to_string()))?;

        // Shared state
        let state_machine = Arc::new(RwLock::new(state_machine));
        let proposal_id = Arc::new(AtomicU64::new(0));
        let cached_role = Arc::new(AtomicU8::new(ROLE_FOLLOWER));
        let cached_leader_id = Arc::new(AtomicU64::new(0));
        let cached_term = Arc::new(AtomicU64::new(0));

        // Bounded channel with backpressure
        let (msg_tx, msg_rx) = mpsc::channel(DRIVER_CHANNEL_CAPACITY);

        let handle = ZoneConsensus {
            msg_tx,
            state_machine: state_machine.clone(),
            config: config.clone(),
            cached_role: cached_role.clone(),
            cached_leader_id: cached_leader_id.clone(),
            cached_term: cached_term.clone(),
            replication_log,
            #[cfg(all(feature = "grpc", has_protos))]
            forward_ctx: None,
        };

        let driver = ZoneConsensusDriver {
            raw_node,
            state_machine,
            config,
            pending: HashMap::new(),
            pending_conf_changes: HashMap::new(),
            proposal_id,
            last_tick: Instant::now(),
            msg_rx,
            cached_role,
            cached_leader_id,
            cached_term,
            #[cfg(all(feature = "grpc", has_protos))]
            peer_map: None,
            replication_log: handle.replication_log.clone(),
        };

        Ok((handle, driver))
    }

    /// Set the forwarding context for transparent leader forwarding.
    ///
    /// Called by `setup_zone()` after creating the transport loop.
    /// Once set, `propose()` on a follower will forward to the leader
    /// via gRPC instead of returning `NotLeader`.
    #[cfg(all(feature = "grpc", has_protos))]
    pub fn set_forward_ctx(
        &mut self,
        client_pool: RaftClientPool,
        peers: SharedPeerMap,
        zone_id: String,
    ) {
        self.forward_ctx = Some(ForwardContext {
            client_pool,
            peers,
            zone_id,
            cached_api_client: std::sync::Arc::new(tokio::sync::Mutex::new(None)),
            leader_raw_result_support: std::sync::Arc::new(tokio::sync::Mutex::new(None)),
        });
    }

    /// Get the node ID.
    pub fn id(&self) -> u64 {
        self.config.id
    }

    /// Get the node configuration.
    pub fn config(&self) -> &RaftConfig {
        &self.config
    }

    /// Check if this is a witness node.
    pub fn is_witness(&self) -> bool {
        self.config.is_witness
    }

    /// Get the current role (atomic read, no channel).
    pub fn role(&self) -> NodeRole {
        NodeRole::from_u8(self.cached_role.load(Ordering::Relaxed))
    }

    /// Check if this node is the leader (atomic read, no channel).
    pub fn is_leader(&self) -> bool {
        self.role() == NodeRole::Leader
    }

    /// Get the current leader ID (atomic read, no channel).
    pub fn leader_id(&self) -> Option<u64> {
        let leader = self.cached_leader_id.load(Ordering::Relaxed);
        if leader == 0 {
            None
        } else {
            Some(leader)
        }
    }

    /// Get the current term (atomic read, no channel).
    pub fn term(&self) -> u64 {
        self.cached_term.load(Ordering::Relaxed)
    }

    /// Execute a read-only closure against the state machine.
    ///
    /// This provides safe read access for query operations (e.g., get_metadata)
    /// without going through the Raft log or the channel.
    pub async fn with_state_machine<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&S) -> R,
    {
        let sm = self.state_machine.read().await;
        f(&*sm)
    }

    /// Execute a mutable closure against the state machine.
    ///
    /// Used for operations like snapshot restore that require `&mut S`.
    pub async fn with_state_machine_mut<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&mut S) -> R,
    {
        let mut sm = self.state_machine.write().await;
        f(&mut *sm)
    }

    /// Serialize a command and submit it to the driver channel.
    ///
    /// Returns the oneshot receiver for callers that want to wait for commit
    /// (SC path). EC callers simply drop the receiver.
    pub(crate) fn submit_to_channel(
        &self,
        command: Command,
    ) -> Result<oneshot::Receiver<Result<CommandResult>>> {
        if !self.is_leader() {
            return Err(RaftError::NotLeader {
                leader_hint: self.leader_id(),
            });
        }

        let data = bincode::serialize(&command)?;
        let (tx, rx) = oneshot::channel();

        self.msg_tx
            .try_send(RaftMsg::Propose {
                data,
                proposal_id: 0, // driver assigns real ID
                tx,
            })
            .map_err(channel_try_send_err)?;

        Ok(rx)
    }

    /// Propose a command with Eventual Consistency — fire and forget.
    ///
    /// Submits the command to Raft but does NOT wait for commit confirmation.
    /// The oneshot receiver is dropped immediately, so the driver's
    /// `let _ = proposal.tx.send(Ok(result))` harmlessly discards the result.
    ///
    /// Latency: ~5-10μs (serialize + channel send).
    pub async fn propose_ec(&self, command: Command) -> Result<()> {
        let _rx = self.submit_to_channel(command)?; // drop receiver
        Ok(())
    }

    /// Propose a command for replication (Strong Consistency).
    ///
    /// If this node is the leader, proposes locally and waits for commit.
    /// If this node is a follower with a forwarding context, transparently
    /// forwards the proposal to the leader via gRPC Propose RPC.
    /// If no forwarding context (embedded mode), returns `NotLeader`.
    ///
    /// # Timeout
    /// Proposals time out after 10 seconds.
    pub async fn propose(&self, command: Command) -> Result<CommandResult> {
        match self.submit_to_channel(command.clone()) {
            Ok(rx) => {
                // Leader path: wait for commit
                match tokio::time::timeout(Duration::from_secs(PROPOSAL_TIMEOUT_SECS), rx).await {
                    Ok(Ok(result)) => result,
                    Ok(Err(_)) => Err(RaftError::ProposalDropped),
                    Err(_) => Err(RaftError::Timeout(PROPOSAL_TIMEOUT_SECS)),
                }
            }
            Err(RaftError::NotLeader { .. }) => {
                // Follower: forward to leader if transport is available
                self.forward_to_leader(command).await
            }
            Err(e) => Err(e),
        }
    }

    /// Forward a proposal to the current leader via gRPC.
    ///
    /// Returns `NotLeader` if no forwarding context or no known leader.
    async fn forward_to_leader(&self, command: Command) -> Result<CommandResult> {
        #[cfg(all(feature = "grpc", has_protos))]
        if let Some(ctx) = &self.forward_ctx {
            let leader_id = self
                .leader_id()
                .ok_or(RaftError::NotLeader { leader_hint: None })?;

            let leader_addr = {
                let peers = ctx.peers.read().unwrap();
                peers.get(&leader_id).cloned().ok_or(RaftError::NotLeader {
                    leader_hint: Some(leader_id),
                })?
            };

            tracing::debug!(
                leader = leader_id,
                addr = %leader_addr.endpoint,
                zone = %ctx.zone_id,
                "Forwarding propose to leader"
            );

            // If forwarding fails (leader unreachable), return NotLeader
            // so the caller can retry after election completes.
            return match crate::transport::forward_propose(
                &ctx.client_pool,
                &leader_addr,
                command,
                &ctx.zone_id,
                &ctx.cached_api_client,
                &ctx.leader_raw_result_support,
            )
            .await
            {
                Ok(result) => Ok(result),
                Err(RaftError::Transport(e)) => {
                    tracing::warn!(
                        leader = leader_id,
                        zone = %ctx.zone_id,
                        "Forward to leader failed (unreachable?): {}",
                        e,
                    );
                    Err(RaftError::NotLeader { leader_hint: None })
                }
                Err(e) => Err(e),
            };
        }

        Err(RaftError::NotLeader {
            leader_hint: self.leader_id(),
        })
    }

    /// True Local-First EC write — bypasses Raft entirely.
    ///
    /// Appends to the replication WAL first, then applies to the local state
    /// machine. WAL-first ordering ensures crash safety: if we crash after
    /// WAL append but before local apply, the entry is recoverable via
    /// replication. The reverse order (apply-first) would leave local state
    /// ahead of the WAL, permanently losing the write from replication.
    ///
    /// If local apply fails, the WAL entry is removed to prevent replicating
    /// a write that the caller received as an error ("failed locally,
    /// committed remotely" would violate caller expectations).
    ///
    /// Only metadata operations (SetMetadata, DeleteMetadata) are supported.
    /// Lock operations require linearizability and must use SC ([`propose`]).
    ///
    /// Latency: ~5-50μs (redb write, no network).
    pub async fn propose_ec_local(&self, command: Command) -> Result<u64> {
        let repl_log = self.replication_log.as_ref().ok_or_else(|| {
            RaftError::InvalidState("EC local writes require a ReplicationLog".into())
        })?;

        // Serialize command for WAL before acquiring lock
        let command_bytes = bincode::serialize(&command)?;

        // WAL-first: append to replication log before local apply.
        let seq = repl_log.append(&command_bytes)?;

        // Apply to local state machine (write lock).
        // On failure, compensate by removing the WAL entry so that
        // drain_unreplicated() does not ship a write the caller saw as failed.
        {
            let mut sm = self.state_machine.write().await;
            if let Err(e) = sm.apply_local(&command) {
                if let Err(cleanup_err) = repl_log.remove_entry(seq) {
                    tracing::error!(
                        seq,
                        error = %cleanup_err,
                        "failed to clean up WAL entry after apply_local failure"
                    );
                }
                return Err(e);
            }
        }

        Ok(seq)
    }

    /// Apply an EC entry received from a peer (Phase C receiver side).
    ///
    /// Uses LWW (Last Writer Wins) conflict resolution: compares the incoming
    /// entry's timestamp against the existing metadata to reject stale writes.
    /// Applies to local state machine only — no WAL append (that's the sender's
    /// concern). Used by the gRPC `ReplicateEntries` handler.
    pub async fn apply_ec_from_peer(
        &self,
        command: Command,
        entry_timestamp: u64,
    ) -> Result<CommandResult> {
        let mut sm = self.state_machine.write().await;
        sm.apply_ec_with_lww(&command, entry_timestamp)
    }

    /// Check if an EC write token has been replicated to a majority.
    ///
    /// Returns:
    /// - `Some("committed")` — write has been replicated
    /// - `Some("pending")` — write is local-only, awaiting replication
    /// - `None` — no replication log, or invalid token
    pub fn is_committed(&self, token: u64) -> Option<&str> {
        self.replication_log
            .as_ref()
            .and_then(|log| log.is_committed(token))
    }

    /// Propose a configuration change and wait for it to be committed.
    ///
    /// `context` carries the new node's gRPC address (etcd pattern).
    /// Returns the resulting `ConfState` after the change is applied.
    pub async fn propose_conf_change(
        &self,
        change_type: ConfChangeType,
        node_id: u64,
        context: Vec<u8>,
    ) -> Result<ConfState> {
        if !self.is_leader() {
            return Err(RaftError::NotLeader {
                leader_hint: self.leader_id(),
            });
        }

        let mut cc = ConfChange::default();
        cc.set_change_type(change_type);
        cc.node_id = node_id;
        cc.context = context.into();

        let (tx, rx) = oneshot::channel();
        self.msg_tx
            .try_send(RaftMsg::ProposeConfChange { change: cc, tx })
            .map_err(channel_try_send_err)?;

        match tokio::time::timeout(Duration::from_secs(PROPOSAL_TIMEOUT_SECS), rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(RaftError::ProposalDropped),
            Err(_) => Err(RaftError::Timeout(PROPOSAL_TIMEOUT_SECS)),
        }
    }

    /// Process a message from another node (sends through channel to driver).
    ///
    /// Uses `send().await` (blocking until space is available) instead of
    /// `try_send()` because Step messages carry Raft protocol traffic
    /// (heartbeats, votes, append entries). Dropping them under load
    /// destabilizes elections and replication. True backpressure is the
    /// correct behavior: the peer's gRPC call blocks until the driver
    /// can accept the message.
    pub async fn step(&self, msg: Message) -> Result<()> {
        self.msg_tx
            .send(RaftMsg::Step { msg })
            .await
            .map_err(|_| RaftError::ChannelClosed)
    }

    /// Campaign to become leader (sends through channel to driver).
    pub async fn campaign(&self) -> Result<()> {
        let (tx, rx) = oneshot::channel();
        self.msg_tx
            .try_send(RaftMsg::Campaign { tx })
            .map_err(channel_try_send_err)?;
        rx.await.map_err(|_| RaftError::ProposalDropped)?
    }
}

// ---------------------------------------------------------------------------
// ZoneConsensusDriver implementation
// ---------------------------------------------------------------------------

impl<S: StateMachine + 'static> ZoneConsensusDriver<S> {
    /// Get the node configuration.
    pub fn config(&self) -> &RaftConfig {
        &self.config
    }

    /// Set the shared peer map so ConfChange can update peers at runtime.
    /// Must be called before the transport loop starts.
    #[cfg(all(feature = "grpc", has_protos))]
    pub fn set_peer_map(&mut self, peer_map: SharedPeerMap) {
        self.peer_map = Some(peer_map);
    }

    /// Get the EC replication log (if present).
    /// Used by the transport loop for Phase C background replication.
    pub fn replication_log(&self) -> Option<&Arc<ReplicationLog>> {
        self.replication_log.as_ref()
    }

    /// Drain all pending messages from the channel and process them.
    ///
    /// Each message is executed **sequentially** on `raw_node`, which is the
    /// entire point of this architecture — no concurrent access.
    pub fn process_messages(&mut self) {
        while let Ok(msg) = self.msg_rx.try_recv() {
            match msg {
                RaftMsg::Step { msg } => {
                    tracing::trace!(
                        from = msg.from,
                        to = msg.to,
                        msg_type = ?msg.get_msg_type(),
                        "raft.driver.step"
                    );
                    if let Err(e) = self.raw_node.step(msg) {
                        tracing::warn!("raft step error: {}", e);
                    }
                }
                RaftMsg::Propose { data, tx, .. } => {
                    // Generate the real proposal ID here in the driver
                    let id = self.proposal_id.fetch_add(1, Ordering::SeqCst);

                    // Prepend proposal ID to the data
                    let mut proposal_data = Vec::with_capacity(8 + data.len());
                    proposal_data.extend_from_slice(&id.to_be_bytes());
                    proposal_data.extend_from_slice(&data);

                    tracing::debug!(proposal_id = id, "raft.driver.propose");
                    match self.raw_node.propose(vec![], proposal_data) {
                        Ok(()) => {
                            // Store pending — tx will be resolved in apply_entries
                            self.pending.insert(id, PendingProposal { tx });
                        }
                        Err(e) => {
                            let _ = tx.send(Err(RaftError::Raft(e.to_string())));
                        }
                    }
                }
                RaftMsg::ProposeConfChange { change, tx } => {
                    let target_node_id = change.node_id;
                    tracing::debug!(node_id = target_node_id, "raft.driver.propose_conf_change");
                    match self.raw_node.propose_conf_change(vec![], change) {
                        Ok(()) => {
                            // Store tx — will be resolved in apply_entries when committed
                            self.pending_conf_changes.insert(target_node_id, tx);
                        }
                        Err(e) => {
                            let _ = tx.send(Err(RaftError::Raft(e.to_string())));
                        }
                    }
                }
                RaftMsg::Campaign { tx } => {
                    tracing::debug!("raft.driver.campaign");
                    let result = self
                        .raw_node
                        .campaign()
                        .map_err(|e| RaftError::Raft(e.to_string()));
                    // Sync cached role so handle.is_leader() reflects the
                    // post-campaign state before the next advance() cycle.
                    // For single-node: campaign() grants self-vote → Leader.
                    self.update_cached_status();
                    let _ = tx.send(result);
                }
            }
        }
    }

    /// Advance the Raft state machine: tick, process ready, apply entries.
    ///
    /// Returns outgoing messages to be sent to peers. The transport loop
    /// should call this after [`process_messages`] in each iteration.
    ///
    /// This is the ONLY code path that touches `raw_node.ready()` and
    /// `raw_node.advance()` — no TOCTOU race is possible because we are
    /// the sole owner.
    pub async fn advance(&mut self) -> Result<Vec<Message>> {
        let mut messages = vec![];

        // Tick if needed
        if self.last_tick.elapsed() >= self.config.tick_interval {
            self.raw_node.tick();
            self.last_tick = Instant::now();
        }

        // Process ready state
        if !self.raw_node.has_ready() {
            self.update_cached_status();
            return Ok(messages);
        }

        let mut ready = self.raw_node.ready();

        // Handle messages to send
        if !ready.messages().is_empty() {
            messages.extend(ready.take_messages());
        }

        // Handle persisted messages
        if !ready.persisted_messages().is_empty() {
            messages.extend(ready.take_persisted_messages());
        }

        // Ordering invariant (per raft-rs five_mem_node example / Raft paper §3):
        //   1. Apply snapshot first — apply_snapshot() clears log entries,
        //      so it must run before appending new entries.
        //   2. Persist entries and hard state — durable BEFORE side-effects.
        //   3. Apply committed entries to state machine — safe only after
        //      the log is durable; committed_entries were persisted in a
        //      prior round, so re-apply on crash is idempotent via last_applied.

        // 1. Handle snapshot (received from leader during catch-up / join)
        if !ready.snapshot().is_empty() {
            let snapshot = ready.snapshot();
            tracing::info!(
                index = snapshot.get_metadata().index,
                term = snapshot.get_metadata().term,
                voters = ?snapshot.get_metadata().get_conf_state().voters,
                "Applying snapshot from leader"
            );
            self.raw_node
                .mut_store()
                .apply_snapshot(snapshot)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
            // Restore state machine from snapshot data (raft contract:
            // application must restore its state from the snapshot).
            if !snapshot.data.is_empty() {
                let mut sm = self.state_machine.write().await;
                sm.restore_snapshot(&snapshot.data)
                    .map_err(|e| RaftError::Storage(format!("restore snapshot: {e}")))?;
            }
        }

        // 2. Persist entries and hard state
        if !ready.entries().is_empty() {
            self.raw_node
                .mut_store()
                .append(ready.entries())
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }

        if let Some(hs) = ready.hs() {
            self.raw_node
                .mut_store()
                .set_hard_state(hs)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }

        // 3. Apply committed entries — NO lock drop needed, we own raw_node
        let committed = ready.take_committed_entries();
        if !committed.is_empty() {
            tracing::debug!(count = committed.len(), "raft.apply");
            self.apply_entries(committed).await?;
        }

        // Advance the ready — NO TOCTOU: we never dropped ownership
        let mut light_rd = self.raw_node.advance(ready);

        // Handle light ready
        if !light_rd.messages().is_empty() {
            messages.extend(light_rd.take_messages());
        }

        if !light_rd.committed_entries().is_empty() {
            let committed = light_rd.take_committed_entries();
            self.apply_entries(committed).await?;
        }

        self.raw_node.advance_apply();

        // Update cached status for handle reads
        self.update_cached_status();

        // Layer 2: Witness campaign suppression (TiKV pattern).
        if self.config.is_witness {
            let before = messages.len();
            messages.retain(|m| {
                !matches!(
                    m.get_msg_type(),
                    raft::eraftpb::MessageType::MsgRequestVote
                        | raft::eraftpb::MessageType::MsgRequestPreVote
                )
            });
            let dropped = before - messages.len();
            if dropped > 0 {
                tracing::debug!(
                    "Witness node {} suppressed {} campaign message(s)",
                    self.config.id,
                    dropped
                );
            }
        }

        Ok(messages)
    }

    /// Apply committed entries to the state machine.
    async fn apply_entries(&mut self, entries: Vec<Entry>) -> Result<()> {
        let mut sm = self.state_machine.write().await;

        for entry in entries {
            if entry.data.is_empty() {
                continue;
            }

            match entry.get_entry_type() {
                EntryType::EntryNormal => {
                    if entry.data.len() < 8 {
                        tracing::warn!(
                            "Entry at index {} has data shorter than 8 bytes, skipping",
                            entry.index
                        );
                        continue;
                    }

                    let (id_bytes, cmd_bytes) = entry.data.split_at(8);
                    let proposal_id = u64::from_be_bytes(
                        id_bytes.try_into().expect("split_at(8) guarantees 8 bytes"),
                    );

                    let command: Command = bincode::deserialize(cmd_bytes)?;
                    let result = sm.apply(entry.index, &command)?;

                    // Notify waiting proposal (if any) — direct HashMap, no lock
                    if let Some(proposal) = self.pending.remove(&proposal_id) {
                        let _ = proposal.tx.send(Ok(result));
                    }
                }
                EntryType::EntryConfChange => {
                    let cc: ConfChange = protobuf::Message::parse_from_bytes(&entry.data)
                        .map_err(|e| RaftError::Serialization(e.to_string()))?;

                    let cs = self
                        .raw_node
                        .apply_conf_change(&cc)
                        .map_err(|e| RaftError::Raft(e.to_string()))?;

                    self.raw_node
                        .mut_store()
                        .set_conf_state(&cs)
                        .map_err(|e| RaftError::Storage(e.to_string()))?;

                    // Update peer map from ConfChange context (etcd pattern)
                    #[cfg(all(feature = "grpc", has_protos))]
                    if let Some(ref peer_map) = self.peer_map {
                        match cc.get_change_type() {
                            ConfChangeType::AddNode | ConfChangeType::AddLearnerNode => {
                                if !cc.context.is_empty() {
                                    let address = String::from_utf8_lossy(&cc.context).to_string();
                                    // Normalize: tonic Endpoint requires a URI scheme.
                                    // JoinZone may send bare "host:port" — add http:// if missing.
                                    let endpoint = if address.starts_with("http") {
                                        address
                                    } else {
                                        format!("http://{}", address)
                                    };
                                    peer_map
                                        .write()
                                        .unwrap()
                                        .insert(cc.node_id, NodeAddress::new(cc.node_id, endpoint));
                                }
                            }
                            ConfChangeType::RemoveNode => {
                                peer_map.write().unwrap().remove(&cc.node_id);
                            }
                        }
                    }

                    tracing::info!(
                        index = entry.index,
                        change_type = ?cc.get_change_type(),
                        node_id = cc.node_id,
                        voters = ?cs.voters,
                        "raft.conf_change.applied",
                    );

                    // After AddNode: create snapshot and compact log so raft-rs
                    // sends snapshot (not AppendEntries) to the new follower.
                    // Per raft contract: the initial ConfState is only in the
                    // snapshot — new followers MUST receive a snapshot to learn
                    // about all voters. Without this, the joiner would only see
                    // voters added via ConfChange entries, missing the bootstrap
                    // voters.
                    if matches!(
                        cc.get_change_type(),
                        ConfChangeType::AddNode | ConfChangeType::AddLearnerNode
                    ) {
                        let sm_data = sm.snapshot().map_err(|e| {
                            RaftError::Storage(format!("snapshot for new voter: {e}"))
                        })?;
                        let mut snapshot = Snapshot::new();
                        {
                            let meta = snapshot.mut_metadata();
                            meta.index = entry.index;
                            meta.term = entry.term;
                            *meta.mut_conf_state() = cs.clone();
                        }
                        snapshot.data = sm_data.into();

                        // Store snapshot WITHOUT clearing entries (we are the
                        // leader and need entries for other followers).
                        self.raw_node
                            .mut_store()
                            .store_snapshot(&snapshot)
                            .map_err(|e| RaftError::Storage(format!("store snapshot: {e}")))?;
                        // Compact log up to this entry so raft-rs detects
                        // Compacted when probing the new follower and falls
                        // back to sending the snapshot.
                        self.raw_node
                            .mut_store()
                            .compact(entry.index)
                            .map_err(|e| {
                                RaftError::Storage(format!("compact after AddNode: {e}"))
                            })?;

                        tracing::info!(
                            index = entry.index,
                            node_id = cc.node_id,
                            voters = ?cs.voters,
                            "Created snapshot and compacted log for new voter catch-up"
                        );
                    }

                    // Notify waiting JoinZone caller (if any)
                    if let Some(tx) = self.pending_conf_changes.remove(&cc.node_id) {
                        let _ = tx.send(Ok(cs));
                    }
                }
                EntryType::EntryConfChangeV2 => {
                    let cc: ConfChangeV2 = protobuf::Message::parse_from_bytes(&entry.data)
                        .map_err(|e| RaftError::Serialization(e.to_string()))?;

                    let cs = self
                        .raw_node
                        .apply_conf_change(&cc)
                        .map_err(|e| RaftError::Raft(e.to_string()))?;

                    self.raw_node
                        .mut_store()
                        .set_conf_state(&cs)
                        .map_err(|e| RaftError::Storage(e.to_string()))?;

                    let mut has_add = false;

                    #[cfg(all(feature = "grpc", has_protos))]
                    if let Some(ref peer_map) = self.peer_map {
                        for single in &cc.changes {
                            match single.get_change_type() {
                                ConfChangeType::AddNode | ConfChangeType::AddLearnerNode => {
                                    has_add = true;
                                    if !cc.context.is_empty() {
                                        let address =
                                            String::from_utf8_lossy(&cc.context).to_string();
                                        let endpoint = if address.starts_with("http") {
                                            address.clone()
                                        } else {
                                            format!("http://{}", address)
                                        };
                                        peer_map.write().unwrap().insert(
                                            single.node_id,
                                            NodeAddress::new(single.node_id, endpoint),
                                        );
                                    }
                                }
                                ConfChangeType::RemoveNode => {
                                    peer_map.write().unwrap().remove(&single.node_id);
                                }
                            }
                        }
                    }

                    // Without grpc feature, still detect AddNode for snapshot creation
                    #[cfg(not(all(feature = "grpc", has_protos)))]
                    {
                        for single in &cc.changes {
                            if matches!(
                                single.get_change_type(),
                                ConfChangeType::AddNode | ConfChangeType::AddLearnerNode
                            ) {
                                has_add = true;
                                break;
                            }
                        }
                    }

                    tracing::info!(
                        index = entry.index,
                        num_changes = cc.changes.len(),
                        voters = ?cs.voters,
                        "raft.conf_change_v2.applied",
                    );

                    if has_add {
                        let sm_data = sm.snapshot().map_err(|e| {
                            RaftError::Storage(format!("snapshot for new voter: {e}"))
                        })?;
                        let mut snapshot = Snapshot::new();
                        {
                            let meta = snapshot.mut_metadata();
                            meta.index = entry.index;
                            meta.term = entry.term;
                            *meta.mut_conf_state() = cs.clone();
                        }
                        snapshot.data = sm_data.into();

                        self.raw_node
                            .mut_store()
                            .store_snapshot(&snapshot)
                            .map_err(|e| RaftError::Storage(format!("store snapshot: {e}")))?;
                        self.raw_node
                            .mut_store()
                            .compact(entry.index)
                            .map_err(|e| {
                                RaftError::Storage(format!("compact after AddNode v2: {e}"))
                            })?;
                    }

                    // Notify waiting JoinZone callers for each added node
                    for single in &cc.changes {
                        if let Some(tx) = self.pending_conf_changes.remove(&single.node_id) {
                            let _ = tx.send(Ok(cs.clone()));
                        }
                    }
                }
            }
        }

        Ok(())
    }

    /// Update the atomic cached status values from the current raw_node state.
    fn update_cached_status(&self) {
        let role: NodeRole = self.raw_node.raft.state.into();
        self.cached_role.store(role.to_u8(), Ordering::Relaxed);
        self.cached_leader_id
            .store(self.raw_node.raft.leader_id, Ordering::Relaxed);
        self.cached_term
            .store(self.raw_node.raft.term, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raft::state_machine::{FullStateMachine, WitnessStateMachine};
    use crate::storage::RedbStore;
    use tempfile::TempDir;

    /// Create a test node pair (handle + driver).
    fn create_test_node() -> (
        ZoneConsensus<WitnessStateMachine>,
        ZoneConsensusDriver<WitnessStateMachine>,
        TempDir,
    ) {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("witness")).unwrap();
        let state_machine = WitnessStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let (handle, driver) = ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        (handle, driver, dir)
    }

    #[tokio::test]
    async fn test_node_creation() {
        let (handle, _driver, _dir) = create_test_node();

        assert_eq!(handle.id(), 1);
        assert!(!handle.is_witness());
        assert_eq!(handle.role(), NodeRole::Follower);
    }

    #[tokio::test]
    async fn test_witness_node() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("witness")).unwrap();
        let state_machine = WitnessStateMachine::new(&store).unwrap();

        let config = RaftConfig::witness(1, vec![2, 3]);
        let (handle, _driver) = ZoneConsensus::new(config, storage, state_machine, None).unwrap();

        assert!(handle.is_witness());
    }

    #[tokio::test]
    async fn test_bootstrap_conf_state() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![2, 3],
            ..Default::default()
        };

        let (handle, _driver) = ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        assert_eq!(handle.id(), 1);
        assert_eq!(handle.role(), NodeRole::Follower);
    }

    #[tokio::test]
    async fn test_with_state_machine() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let (handle, _driver) = ZoneConsensus::new(config, storage, state_machine, None).unwrap();

        let result = handle
            .with_state_machine(|sm| sm.get_metadata("/nonexistent"))
            .await;
        assert!(result.unwrap().is_none());
    }

    /// Mini transport loop for tests — mirrors production TransportLoop.
    /// Each driver runs in its own task, routes messages via handles.
    async fn run_test_driver(
        mut driver: ZoneConsensusDriver<FullStateMachine>,
        my_idx: usize,
        all_handles: Vec<ZoneConsensus<FullStateMachine>>,
        mut shutdown_rx: tokio::sync::watch::Receiver<bool>,
    ) {
        let mut interval = tokio::time::interval(Duration::from_millis(10));
        loop {
            tokio::select! {
                _ = interval.tick() => {}
                _ = shutdown_rx.changed() => break,
            }

            driver.process_messages();
            match driver.advance().await {
                Ok(messages) => {
                    for msg in messages {
                        let target_idx = msg.to as usize - 1;
                        if target_idx < all_handles.len() && target_idx != my_idx {
                            let _ = all_handles[target_idx].step(msg).await;
                        }
                    }
                }
                Err(e) => tracing::warn!("test driver advance error: {}", e),
            }
        }
    }

    #[tokio::test]
    async fn test_three_node_consensus() {
        // Phase 1: Create all nodes (handles + drivers)
        let mut handles = Vec::new();
        let mut drivers = Vec::new();
        let mut _dirs = Vec::new();

        for id in 1..=3u64 {
            let dir = TempDir::new().unwrap();
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = RedbStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();

            let peers: Vec<u64> = (1..=3).filter(|&p| p != id).collect();
            let config = RaftConfig {
                id,
                peers,
                tick_interval: Duration::from_millis(10),
                ..Default::default()
            };

            let (handle, driver) =
                ZoneConsensus::new(config, storage, state_machine, None).unwrap();
            handles.push(handle);
            drivers.push(driver);
            _dirs.push(dir);
        }

        // Phase 2: Spawn each driver in its own task (production-like)
        let (shutdown_tx, _) = tokio::sync::watch::channel(false);
        for (i, driver) in drivers.into_iter().enumerate() {
            let all_handles = handles.clone();
            let shutdown_rx = shutdown_tx.subscribe();
            tokio::spawn(run_test_driver(driver, i, all_handles, shutdown_rx));
        }

        // Yield to let spawned driver tasks start
        tokio::task::yield_now().await;

        // Phase 3: Trigger election on node 1
        handles[0].campaign().await.unwrap();

        // Wait for leader election.
        // The drivers run on 10ms intervals; election needs ~3 rounds of
        // message exchange (MsgVote → MsgVoteResp → leader heartbeat).
        let mut leader_elected = false;
        for _ in 0..200 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            if handles.iter().any(|h| h.is_leader()) {
                leader_elected = true;
                break;
            }
        }
        assert!(leader_elected, "Leader election must complete");

        let mut leader_count = 0;
        let mut leader_idx = 0;
        for (i, handle) in handles.iter().enumerate() {
            if handle.is_leader() {
                leader_count += 1;
                leader_idx = i;
            }
        }
        assert_eq!(leader_count, 1, "Expected exactly 1 leader");

        // Phase 4: Propose a command on the leader
        let cmd = Command::SetMetadata {
            key: "/test.txt".into(),
            value: b"hello world".to_vec(),
        };
        let result = handles[leader_idx].propose(cmd).await.unwrap();
        assert!(
            matches!(result, CommandResult::Success),
            "Proposal should succeed"
        );

        // Wait for replication: poll until all nodes have the data,
        // instead of a blanket sleep.
        let mut all_replicated = false;
        for _ in 0..200 {
            tokio::time::sleep(Duration::from_millis(10)).await;
            let mut ok = true;
            for handle in &handles {
                let has_it = handle
                    .with_state_machine(|sm| sm.get_metadata("/test.txt"))
                    .await
                    .map(|v| v.is_some())
                    .unwrap_or(false);
                if !has_it {
                    ok = false;
                    break;
                }
            }
            if ok {
                all_replicated = true;
                break;
            }
        }
        assert!(all_replicated, "Replication to all nodes must complete");

        // Phase 5: EC propose — returns immediately without waiting for commit
        let ec_cmd = Command::SetMetadata {
            key: "/ec-test.txt".into(),
            value: b"eventual".to_vec(),
        };
        let ec_result = handles[leader_idx].propose_ec(ec_cmd).await;
        assert!(ec_result.is_ok(), "EC propose should return Ok immediately");

        // Shutdown all drivers
        let _ = shutdown_tx.send(true);
    }

    /// Regression test: single-node ConfState must include self as voter.
    ///
    /// Before the fix, empty `config.peers` skipped ConfState bootstrap,
    /// leaving the voter set empty.  This violated raft-rs's contract:
    /// `RawNode` expects the node to be in the voter set before `campaign()`.
    /// The result was a panic at `raft.rs:1225` (`unwrap()` on `None`).
    ///
    /// The fix: bootstrap ConfState with `voters=[self.id]` even when
    /// `config.peers` is empty (single-node cluster).
    ///
    /// This test is deterministic: no async, no timers, no polling.
    /// It verifies the persisted ConfState directly after ZoneConsensus::new().
    #[test]
    fn test_single_node_conf_state_includes_self() {
        let dir = TempDir::new().unwrap();

        // Create ZoneConsensus, then drop to release redb lock.
        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = RedbStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();

            let config = RaftConfig {
                id: 1,
                peers: vec![], // single-node: no peers
                ..Default::default()
            };

            // Before the fix, this skipped ConfState bootstrap when peers
            // was empty, leaving voters=[].
            let (_handle, _driver) =
                ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        }

        // Re-open storage (redb lock released) and verify ConfState.
        let storage = RaftStorage::open(dir.path()).unwrap();
        let state = Storage::initial_state(&storage).unwrap();
        assert_eq!(
            state.conf_state.voters,
            vec![1],
            "Single-node ConfState must include self as voter"
        );
    }

    /// Verify multi-node ConfState includes all voters.
    ///
    /// Per raft-rs contract: all initial cluster members must be in
    /// ConfState.voters before RawNode::new().
    #[test]
    fn test_multi_node_conf_state_includes_all_voters() {
        let dir = TempDir::new().unwrap();

        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = RedbStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();

            let config = RaftConfig {
                id: 1,
                peers: vec![2, 3],
                ..Default::default()
            };

            let (_handle, _driver) =
                ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        }

        let storage = RaftStorage::open(dir.path()).unwrap();
        let state = Storage::initial_state(&storage).unwrap();
        let mut voters = state.conf_state.voters.clone();
        voters.sort();
        assert_eq!(
            voters,
            vec![1, 2, 3],
            "Multi-node ConfState must include self and all peers"
        );
    }

    #[test]
    fn test_restart_with_empty_runtime_peers_preserves_persisted_conf_state() {
        let dir = TempDir::new().unwrap();

        // First boot as multi-node to persist a non-singleton ConfState.
        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = RedbStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();

            let config = RaftConfig {
                id: 1,
                peers: vec![2],
                ..Default::default()
            };

            let (_handle, _driver) =
                ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        }

        // Restart with empty runtime peers should reopen and preserve
        // persisted multi-node membership (used during cold recovery).
        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = RedbStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();
            let config = RaftConfig {
                id: 1,
                peers: vec![],
                ..Default::default()
            };

            let (_handle, _driver) =
                ZoneConsensus::new(config, storage, state_machine, None).unwrap();
        }

        let storage = RaftStorage::open(dir.path()).unwrap();
        let state = Storage::initial_state(&storage).unwrap();
        let mut voters = state.conf_state.voters.clone();
        voters.sort();
        assert_eq!(voters, vec![1, 2]);
    }

    #[test]
    fn test_restart_rejects_persisted_conf_state_without_local_node() {
        use raft::eraftpb::ConfState;

        let dir = TempDir::new().unwrap();

        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            storage
                .set_conf_state(&ConfState {
                    voters: vec![2],
                    ..Default::default()
                })
                .unwrap();
        }

        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();
        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let err = match ZoneConsensus::new(config, storage, state_machine, None) {
            Ok(_) => {
                panic!("Expected InvalidState when local node is absent from persisted voters")
            }
            Err(err) => err,
        };
        assert!(matches!(err, RaftError::InvalidState(_)));
    }

    #[tokio::test]
    async fn test_propose_ec_not_leader_returns_error() {
        let (handle, _driver, _dir) = create_test_node();

        // Node is a follower (single node, no campaign), propose_ec should fail
        let cmd = Command::SetMetadata {
            key: "/test".into(),
            value: b"data".to_vec(),
        };
        let result = handle.propose_ec(cmd).await;
        assert!(result.is_err(), "EC propose on non-leader should fail");
        assert!(
            matches!(result.unwrap_err(), RaftError::NotLeader { .. }),
            "Should be NotLeader error"
        );
    }

    /// Regression test: after campaign() on a single-node cluster,
    /// is_leader() must return true IMMEDIATELY — without needing advance().
    ///
    /// Previously, update_cached_status() was only called inside advance(),
    /// so the cached role stayed Follower until the next transport loop tick.
    /// Callers (PyO3 set_metadata) that checked is_leader() right after
    /// create_zone() would get "not leader" errors.
    #[tokio::test]
    async fn test_single_node_is_leader_after_campaign_without_advance() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = RedbStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let (handle, mut driver) =
            ZoneConsensus::new(config, storage, state_machine, None).unwrap();

        // Before campaign: should be Follower
        assert_eq!(handle.role(), NodeRole::Follower);
        assert!(!handle.is_leader());

        // Spawn campaign on a separate task (it blocks waiting for driver response).
        // Then drive process_messages() to dequeue and process the Campaign msg.
        let campaign_handle = handle.clone();
        let campaign_task = tokio::spawn(async move { campaign_handle.campaign().await });

        // Yield to let the campaign task send the message
        tokio::task::yield_now().await;

        // Process the Campaign message in the driver (no advance!)
        driver.process_messages();

        // Wait for campaign to complete
        campaign_task.await.unwrap().unwrap();

        // is_leader() must be true now — the cached status was synced
        // inside the campaign handler, not deferred to advance().
        assert!(
            handle.is_leader(),
            "is_leader() must be true after campaign() + process_messages(), without advance()"
        );
        assert_eq!(handle.role(), NodeRole::Leader);
        assert_eq!(handle.leader_id(), Some(1));
    }
}
