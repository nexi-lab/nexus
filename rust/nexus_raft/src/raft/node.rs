//! Raft node implementation.
//!
//! This module provides the `RaftNode` wrapper around tikv/raft-rs's `RawNode`,
//! handling the event loop, message passing, and state machine application.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use raft::eraftpb::{ConfChange, ConfChangeType, Entry, EntryType, Message};
use raft::{Config, RawNode};
use slog::{o, Logger};
use tokio::sync::{mpsc, oneshot, RwLock};

use super::state_machine::StateMachine;
use super::storage::RaftStorage;
use super::{Command, CommandResult, RaftError, Result};

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
            tick_interval: Duration::from_millis(100),
        }
    }
}

impl RaftConfig {
    /// Create a configuration for a witness node.
    pub fn witness(id: u64, peers: Vec<u64>) -> Self {
        Self {
            id,
            peers,
            is_witness: true,
            ..Default::default()
        }
    }

    /// Convert to raft-rs Config.
    fn to_raft_config(&self) -> Config {
        Config {
            id: self.id,
            election_tick: self.election_tick,
            heartbeat_tick: self.heartbeat_tick,
            max_size_per_msg: self.max_size_per_msg,
            max_inflight_msgs: self.max_inflight_msgs,
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
    /// The command data.
    #[allow(dead_code)]
    data: Vec<u8>,
}

/// A Raft consensus node.
///
/// This wraps tikv/raft-rs's `RawNode` and provides:
/// - Async proposal API
/// - Automatic tick handling
/// - Message sending through transport
/// - State machine application
///
/// # Example
///
/// ```rust,ignore
/// let config = RaftConfig {
///     id: 1,
///     peers: vec![2, 3],
///     ..Default::default()
/// };
///
/// let storage = RaftStorage::open("/var/lib/nexus/raft")?;
/// let state_machine = MyStateMachine::new();
/// let node = RaftNode::new(config, storage, state_machine)?;
///
/// // Start the node
/// node.run().await?;
/// ```
pub struct RaftNode<S: StateMachine> {
    /// Node configuration.
    config: RaftConfig,
    /// The underlying raft-rs node.
    raw_node: RwLock<RawNode<RaftStorage>>,
    /// The state machine.
    state_machine: RwLock<S>,
    /// Pending proposals waiting for commit.
    pending: RwLock<HashMap<u64, PendingProposal>>,
    /// Proposal ID counter.
    proposal_id: std::sync::atomic::AtomicU64,
    /// Channel for outgoing messages.
    msg_tx: mpsc::Sender<Message>,
    /// Channel for receiving messages (used by transport layer).
    _msg_rx: RwLock<mpsc::Receiver<Message>>,
    /// Last tick time.
    last_tick: RwLock<Instant>,
}

impl<S: StateMachine + 'static> RaftNode<S> {
    /// Create a new Raft node.
    ///
    /// # Arguments
    /// * `config` - Node configuration
    /// * `storage` - Persistent storage for Raft log
    /// * `state_machine` - Application state machine
    pub fn new(config: RaftConfig, storage: RaftStorage, state_machine: S) -> Result<Arc<Self>> {
        let raft_config = config.to_raft_config();

        // Create a discard logger for raft-rs (we use tracing for our own logging)
        let logger = Logger::root(slog::Discard, o!());

        // Create the raw node
        let raw_node = RawNode::new(&raft_config, storage, &logger)
            .map_err(|e| RaftError::Raft(e.to_string()))?;

        // Create message channel
        let (msg_tx, msg_rx) = mpsc::channel(1024);

        Ok(Arc::new(Self {
            config,
            raw_node: RwLock::new(raw_node),
            state_machine: RwLock::new(state_machine),
            pending: RwLock::new(HashMap::new()),
            proposal_id: std::sync::atomic::AtomicU64::new(0),
            msg_tx,
            _msg_rx: RwLock::new(msg_rx),
            last_tick: RwLock::new(Instant::now()),
        }))
    }

    /// Get the node ID.
    pub fn id(&self) -> u64 {
        self.config.id
    }

    /// Check if this is a witness node.
    pub fn is_witness(&self) -> bool {
        self.config.is_witness
    }

    /// Get the current role.
    pub async fn role(&self) -> NodeRole {
        let node = self.raw_node.read().await;
        node.raft.state.into()
    }

    /// Check if this node is the leader.
    pub async fn is_leader(&self) -> bool {
        self.role().await == NodeRole::Leader
    }

    /// Get the current leader ID (if known).
    pub async fn leader_id(&self) -> Option<u64> {
        let node = self.raw_node.read().await;
        let leader = node.raft.leader_id;
        if leader == 0 {
            None
        } else {
            Some(leader)
        }
    }

    /// Get the current term.
    pub async fn term(&self) -> u64 {
        let node = self.raw_node.read().await;
        node.raft.term
    }

    /// Propose a command for replication.
    ///
    /// This is the main API for clients to submit commands. The command
    /// will be replicated through Raft and applied to the state machine
    /// once committed.
    ///
    /// # Returns
    /// Result of applying the command, or error if proposal failed.
    pub async fn propose(&self, command: Command) -> Result<CommandResult> {
        if !self.is_leader().await {
            return Err(RaftError::NotLeader {
                leader_hint: self.leader_id().await,
            });
        }

        // Serialize the command
        let data = bincode::serialize(&command)?;

        // Generate proposal ID
        let id = self
            .proposal_id
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);

        // Create response channel
        let (tx, rx) = oneshot::channel();

        // Store pending proposal
        {
            let mut pending = self.pending.write().await;
            pending.insert(
                id,
                PendingProposal {
                    tx,
                    data: data.clone(),
                },
            );
        }

        // Propose to raft
        {
            let mut node = self.raw_node.write().await;
            node.propose(vec![], data)
                .map_err(|e| RaftError::Raft(e.to_string()))?;
        }

        // Wait for commit
        rx.await.map_err(|_| RaftError::ProposalDropped)?
    }

    /// Propose a configuration change.
    pub async fn propose_conf_change(
        &self,
        change_type: ConfChangeType,
        node_id: u64,
    ) -> Result<()> {
        if !self.is_leader().await {
            return Err(RaftError::NotLeader {
                leader_hint: self.leader_id().await,
            });
        }

        let mut cc = ConfChange::default();
        cc.set_change_type(change_type);
        cc.node_id = node_id;

        let mut node = self.raw_node.write().await;
        node.propose_conf_change(vec![], cc)
            .map_err(|e| RaftError::Raft(e.to_string()))?;

        Ok(())
    }

    /// Process a message from another node.
    pub async fn step(&self, msg: Message) -> Result<()> {
        let mut node = self.raw_node.write().await;
        node.step(msg).map_err(|e| RaftError::Raft(e.to_string()))?;
        Ok(())
    }

    /// Advance the raft state machine.
    ///
    /// This should be called periodically (e.g., every tick) to:
    /// - Process ready state (committed entries, messages to send)
    /// - Apply entries to state machine
    /// - Send messages to peers
    pub async fn advance(&self) -> Result<Vec<Message>> {
        let mut messages = vec![];

        // Check if we need to tick
        {
            let mut last_tick = self.last_tick.write().await;
            if last_tick.elapsed() >= self.config.tick_interval {
                let mut node = self.raw_node.write().await;
                node.tick();
                *last_tick = Instant::now();
            }
        }

        // Process ready state
        let mut node = self.raw_node.write().await;
        if !node.has_ready() {
            return Ok(messages);
        }

        let mut ready = node.ready();

        // Handle messages to send
        if !ready.messages().is_empty() {
            messages.extend(ready.take_messages());
        }

        // Handle persisted messages
        if !ready.persisted_messages().is_empty() {
            messages.extend(ready.take_persisted_messages());
        }

        // Handle committed entries
        let committed = ready.take_committed_entries();
        if !committed.is_empty() {
            drop(node); // Release lock before applying
            self.apply_entries(committed).await?;
            node = self.raw_node.write().await;
        }

        // Handle snapshot
        if !ready.snapshot().is_empty() {
            let snapshot = ready.snapshot();
            node.mut_store()
                .apply_snapshot(snapshot)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }

        // Persist entries and hard state
        if !ready.entries().is_empty() {
            node.mut_store()
                .append(ready.entries())
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }

        if let Some(hs) = ready.hs() {
            node.mut_store()
                .set_hard_state(hs)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }

        // Advance the ready
        let mut light_rd = node.advance(ready);

        // Handle light ready
        if !light_rd.messages().is_empty() {
            messages.extend(light_rd.take_messages());
        }

        if !light_rd.committed_entries().is_empty() {
            let committed = light_rd.take_committed_entries();
            drop(node);
            self.apply_entries(committed).await?;
            node = self.raw_node.write().await;
        }

        node.advance_apply();

        Ok(messages)
    }

    /// Apply committed entries to the state machine.
    async fn apply_entries(&self, entries: Vec<Entry>) -> Result<()> {
        let mut sm = self.state_machine.write().await;

        for entry in entries {
            if entry.data.is_empty() {
                // Empty entry (e.g., leader election)
                continue;
            }

            match entry.get_entry_type() {
                EntryType::EntryNormal => {
                    // Deserialize and apply command
                    let command: Command = bincode::deserialize(&entry.data)?;
                    let result = sm.apply(entry.index, &command)?;

                    // Notify waiting proposal (if any)
                    // Note: We use index as proposal ID for simplicity
                    // In production, you'd want a proper proposal tracking mechanism
                    let mut pending = self.pending.write().await;
                    if let Some(proposal) = pending.remove(&entry.index) {
                        let _ = proposal.tx.send(Ok(result));
                    }
                }
                EntryType::EntryConfChange | EntryType::EntryConfChangeV2 => {
                    // Configuration change - handled by raft-rs
                    tracing::info!("Applied config change at index {}", entry.index);
                }
            }
        }

        Ok(())
    }

    /// Get a channel sender for incoming messages.
    pub fn message_sender(&self) -> mpsc::Sender<Message> {
        self.msg_tx.clone()
    }

    /// Campaign to become leader.
    pub async fn campaign(&self) -> Result<()> {
        let mut node = self.raw_node.write().await;
        node.campaign()
            .map_err(|e| RaftError::Raft(e.to_string()))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raft::state_machine::WitnessStateMachine;
    use crate::storage::SledStore;
    use tempfile::TempDir;

    async fn create_test_node() -> (Arc<RaftNode<WitnessStateMachine>>, TempDir) {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = SledStore::open(dir.path().join("witness")).unwrap();
        let state_machine = WitnessStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let node = RaftNode::new(config, storage, state_machine).unwrap();
        (node, dir)
    }

    #[tokio::test]
    async fn test_node_creation() {
        let (node, _dir) = create_test_node().await;

        assert_eq!(node.id(), 1);
        assert!(!node.is_witness());
        assert_eq!(node.role().await, NodeRole::Follower);
    }

    #[tokio::test]
    async fn test_witness_node() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = SledStore::open(dir.path().join("witness")).unwrap();
        let state_machine = WitnessStateMachine::new(&store).unwrap();

        let config = RaftConfig::witness(1, vec![2, 3]);
        let node = RaftNode::new(config, storage, state_machine).unwrap();

        assert!(node.is_witness());
    }
}
