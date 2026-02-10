//! Raft node implementation.
//!
//! This module provides the `RaftNode` wrapper around tikv/raft-rs's `RawNode`,
//! handling the event loop, message passing, and state machine application.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use raft::eraftpb::{ConfChange, ConfChangeType, ConfState, Entry, EntryType, Message};
use raft::{Config, RawNode, Storage};
use slog::{o, Logger};
use tokio::sync::{oneshot, RwLock};

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
}

/// A Raft consensus node.
///
/// This wraps tikv/raft-rs's `RawNode` and provides:
/// - Async proposal API with correct proposal tracking
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
/// // Drive the event loop
/// loop {
///     let messages = node.advance().await?;
///     for msg in messages {
///         transport.send(msg).await?;
///     }
/// }
/// ```
pub struct RaftNode<S: StateMachine> {
    /// Node configuration.
    config: RaftConfig,
    /// The underlying raft-rs node.
    raw_node: RwLock<RawNode<RaftStorage>>,
    /// The state machine.
    state_machine: RwLock<S>,
    /// Pending proposals waiting for commit, keyed by proposal ID.
    pending: RwLock<HashMap<u64, PendingProposal>>,
    /// Proposal ID counter.
    proposal_id: std::sync::atomic::AtomicU64,
    /// Last tick time.
    last_tick: RwLock<Instant>,
}

impl<S: StateMachine + 'static> RaftNode<S> {
    /// Create a new Raft node.
    ///
    /// If the storage has no existing ConfState (fresh cluster), initializes
    /// the voter set with this node and all configured peers.
    ///
    /// # Arguments
    /// * `config` - Node configuration
    /// * `storage` - Persistent storage for Raft log
    /// * `state_machine` - Application state machine
    pub fn new(
        config: RaftConfig,
        storage: RaftStorage,
        state_machine: S,
    ) -> Result<Arc<Self>> {
        // Bootstrap: set initial ConfState if this is a fresh cluster
        let initial_state = storage
            .initial_state()
            .map_err(|e| RaftError::Storage(e.to_string()))?;

        if initial_state.conf_state.voters.is_empty() && !config.peers.is_empty() {
            let mut voters = vec![config.id];
            voters.extend(config.peers.iter());
            let cs = ConfState {
                voters: voters.clone(),
                ..Default::default()
            };
            storage
                .set_conf_state(&cs)
                .map_err(|e| RaftError::Storage(format!("failed to set initial ConfState: {e}")))?;
            tracing::info!("Bootstrapped ConfState with voters: {:?}", voters);
        }

        let raft_config = config.to_raft_config();

        // Create a discard logger for raft-rs (we use tracing for our own logging)
        let logger = Logger::root(slog::Discard, o!());

        // Create the raw node
        let raw_node = RawNode::new(&raft_config, storage, &logger)
            .map_err(|e| RaftError::Raft(e.to_string()))?;

        Ok(Arc::new(Self {
            config,
            raw_node: RwLock::new(raw_node),
            state_machine: RwLock::new(state_machine),
            pending: RwLock::new(HashMap::new()),
            proposal_id: std::sync::atomic::AtomicU64::new(0),
            last_tick: RwLock::new(Instant::now()),
        }))
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

    /// Execute a read-only closure against the state machine.
    ///
    /// This provides safe read access for query operations (e.g., get_metadata)
    /// without going through the Raft log.
    pub async fn with_state_machine<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&S) -> R,
    {
        let sm = self.state_machine.read().await;
        f(&*sm)
    }

    /// Execute a mutable closure against the state machine.
    ///
    /// Used for operations like snapshot restore that require `&mut self`.
    pub async fn with_state_machine_mut<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&mut S) -> R,
    {
        let mut sm = self.state_machine.write().await;
        f(&mut *sm)
    }

    /// Propose a command for replication.
    ///
    /// This is the main API for clients to submit commands. The command
    /// will be replicated through Raft and applied to the state machine
    /// once committed.
    ///
    /// The proposal ID is prepended to the serialized data so that
    /// `apply_entries()` can match committed entries back to waiting callers.
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

        // Prepend proposal ID to the data so apply_entries() can match it back
        let mut proposal_data = Vec::with_capacity(8 + data.len());
        proposal_data.extend_from_slice(&id.to_be_bytes());
        proposal_data.extend_from_slice(&data);

        // Create response channel
        let (tx, rx) = oneshot::channel();

        // Store pending proposal keyed by proposal ID
        {
            let mut pending = self.pending.write().await;
            pending.insert(id, PendingProposal { tx });
        }

        // Propose to raft
        {
            let mut node = self.raw_node.write().await;
            node.propose(vec![], proposal_data)
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
    ///
    /// Each entry's data is prefixed with an 8-byte proposal ID (set by `propose()`).
    /// After applying the command, the pending proposal channel is resolved.
    async fn apply_entries(&self, entries: Vec<Entry>) -> Result<()> {
        let mut sm = self.state_machine.write().await;

        for entry in entries {
            if entry.data.is_empty() {
                // Empty entry (e.g., leader election noop)
                continue;
            }

            match entry.get_entry_type() {
                EntryType::EntryNormal => {
                    // Data format: [8 bytes proposal_id][bincode command]
                    if entry.data.len() < 8 {
                        tracing::warn!(
                            "Entry at index {} has data shorter than 8 bytes, skipping",
                            entry.index
                        );
                        continue;
                    }

                    let (id_bytes, cmd_bytes) = entry.data.split_at(8);
                    let proposal_id = u64::from_be_bytes(
                        id_bytes
                            .try_into()
                            .expect("split_at(8) guarantees 8 bytes"),
                    );

                    // Deserialize and apply command
                    let command: Command = bincode::deserialize(cmd_bytes)?;
                    let result = sm.apply(entry.index, &command)?;

                    // Notify waiting proposal (if any)
                    let mut pending = self.pending.write().await;
                    if let Some(proposal) = pending.remove(&proposal_id) {
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
    use crate::raft::state_machine::{FullStateMachine, WitnessStateMachine};
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

    #[tokio::test]
    async fn test_bootstrap_conf_state() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = SledStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![2, 3],
            ..Default::default()
        };

        let node = RaftNode::new(config, storage, state_machine).unwrap();
        assert_eq!(node.id(), 1);
        // Node should start as follower with peers configured
        assert_eq!(node.role().await, NodeRole::Follower);
    }

    #[tokio::test]
    async fn test_with_state_machine() {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        let store = SledStore::open(dir.path().join("sm")).unwrap();
        let state_machine = FullStateMachine::new(&store).unwrap();

        let config = RaftConfig {
            id: 1,
            peers: vec![],
            ..Default::default()
        };

        let node = RaftNode::new(config, storage, state_machine).unwrap();

        // Read from state machine via with_state_machine
        let result = node
            .with_state_machine(|sm| sm.get_metadata("/nonexistent"))
            .await;
        assert!(result.unwrap().is_none());
    }

    #[tokio::test]
    async fn test_three_node_consensus() {
        // Create 3 nodes
        let mut nodes = Vec::new();
        let mut _dirs = Vec::new();

        for id in 1..=3u64 {
            let dir = TempDir::new().unwrap();
            let storage = RaftStorage::open(dir.path()).unwrap();
            let store = SledStore::open(dir.path().join("sm")).unwrap();
            let state_machine = FullStateMachine::new(&store).unwrap();

            let peers: Vec<u64> = (1..=3).filter(|&p| p != id).collect();
            let config = RaftConfig {
                id,
                peers,
                tick_interval: Duration::from_millis(10),
                ..Default::default()
            };

            let node = RaftNode::new(config, storage, state_machine).unwrap();
            nodes.push(node);
            _dirs.push(dir);
        }

        // Trigger election on node 1
        nodes[0].campaign().await.unwrap();

        // Drive the event loop, routing messages between nodes
        for _ in 0..100 {
            let mut all_messages = vec![];
            for node in &nodes {
                match node.advance().await {
                    Ok(msgs) => all_messages.extend(msgs),
                    Err(e) => tracing::warn!("advance error: {}", e),
                }
            }

            // Route messages to target nodes
            for msg in all_messages {
                let target_idx = msg.to as usize - 1; // node IDs are 1-indexed
                if target_idx < nodes.len() {
                    let _ = nodes[target_idx].step(msg).await;
                }
            }

            tokio::time::sleep(Duration::from_millis(5)).await;
        }

        // Verify exactly one leader
        let mut leader_count = 0;
        let mut leader_idx = 0;
        for (i, node) in nodes.iter().enumerate() {
            if node.is_leader().await {
                leader_count += 1;
                leader_idx = i;
            }
        }
        assert_eq!(leader_count, 1, "Expected exactly 1 leader");

        // Propose a command on the leader.
        // propose() awaits the oneshot receiver internally, so we spawn it
        // in a background task while the advance loop continues to drive
        // commit and apply — just like production where advance runs as a
        // background tokio task.
        let leader = nodes[leader_idx].clone();
        let propose_handle = tokio::spawn(async move {
            let cmd = Command::SetMetadata {
                key: "/test.txt".into(),
                value: b"hello world".to_vec(),
            };
            leader.propose(cmd).await
        });

        // Drive more rounds — commit the proposal + replicate to followers
        for _ in 0..100 {
            let mut all_messages = vec![];
            for node in &nodes {
                match node.advance().await {
                    Ok(msgs) => all_messages.extend(msgs),
                    Err(e) => tracing::warn!("advance error: {}", e),
                }
            }
            for msg in all_messages {
                let target_idx = msg.to as usize - 1;
                if target_idx < nodes.len() {
                    let _ = nodes[target_idx].step(msg).await;
                }
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }

        // Wait for the proposal to complete
        let result = propose_handle.await.unwrap().unwrap();
        assert!(
            matches!(result, CommandResult::Success),
            "Proposal should succeed"
        );

        // Verify all nodes have the metadata
        for (i, node) in nodes.iter().enumerate() {
            let value = node
                .with_state_machine(|sm| sm.get_metadata("/test.txt"))
                .await
                .unwrap();
            assert!(
                value.is_some(),
                "Node {} should have /test.txt metadata after replication",
                i + 1
            );
        }
    }
}