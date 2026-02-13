//! Transport loop — background task that drives the Raft actor event loop.
//!
//! This task owns the [`ZoneConsensusDriver`] exclusively and calls
//! [`process_messages()`] + [`advance()`] sequentially to maintain the
//! raft-rs single-owner invariant.
//!
//! # Architecture
//!
//! ```text
//! ┌──────────────────────┐  process_messages()  ┌──────────────┐
//! │  mpsc channel msgs   │ ───────────────────> │ ZoneConsensusDriver│
//! │  (step/propose/etc.) │                      │ (owns RawNode)│
//! └──────────────────────┘                      └──────┬───────┘
//!                                                      │ advance()
//!                                                      ▼
//!                                              ┌──────────────────┐
//!                                              │  Outgoing msgs   │
//!                                              │  → RaftClientPool│
//!                                              └──────────────────┘
//!                                                      │
//!                                              ┌──────────────────┐
//!                                              │  EC Phase C:     │
//!                                              │  drain WAL →     │
//!                                              │  replicate peers │
//!                                              └──────────────────┘
//! ```

use super::client::RaftClientPool;
use super::proto::nexus::raft::EcReplicationEntry;
use super::{NodeAddress, SharedPeerMap};
use crate::raft::{StateMachine, ZoneConsensusDriver};
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::watch;

/// Base backoff interval for EC replication retries.
const EC_BACKOFF_BASE: Duration = Duration::from_millis(100);
/// Maximum backoff interval (cap) for EC replication retries.
const EC_BACKOFF_CAP: Duration = Duration::from_secs(60);

/// Per-peer EC replication state.
struct PeerReplicationState {
    /// Highest sequence number this peer has acknowledged.
    acked_seq: u64,
    /// Current backoff duration (exponential, capped at [`EC_BACKOFF_CAP`]).
    backoff: Duration,
    /// Earliest time to attempt the next replication to this peer.
    next_attempt: Instant,
    /// Peer has fallen behind compacted WAL — needs full snapshot to catch up.
    needs_snapshot: bool,
}

impl PeerReplicationState {
    fn new() -> Self {
        Self {
            acked_seq: 0,
            backoff: EC_BACKOFF_BASE,
            next_attempt: Instant::now(),
            needs_snapshot: false,
        }
    }

    /// Reset backoff after a successful replication.
    fn reset_backoff(&mut self) {
        self.backoff = EC_BACKOFF_BASE;
        self.next_attempt = Instant::now();
    }

    /// Double the backoff (capped) after a failed replication.
    fn increase_backoff(&mut self) {
        self.backoff = (self.backoff * 2).min(EC_BACKOFF_CAP);
        self.next_attempt = Instant::now() + self.backoff;
    }
}

/// Background task that drives the Raft event loop and sends messages to peers.
///
/// Owns the [`ZoneConsensusDriver`] exclusively — this is the single task that
/// touches `RawNode`.
pub struct TransportLoop<S: StateMachine + 'static> {
    /// The ZoneConsensusDriver to drive (exclusive ownership).
    driver: ZoneConsensusDriver<S>,
    /// Known peers (node_id → address). Shared so ConfChange can add peers at runtime.
    peers: SharedPeerMap,
    /// Connection pool for sending messages to peers.
    client_pool: RaftClientPool,
    /// How often to call advance() (default: 10ms).
    tick_interval: Duration,
    /// Zone ID for multi-zone message routing.
    zone_id: String,
    /// This node's ID (for EC replication sender identification).
    node_id: u64,
    /// Per-peer EC replication tracking (peer_id → state).
    ec_peer_state: HashMap<u64, PeerReplicationState>,
}

impl<S: StateMachine + Send + Sync + 'static> TransportLoop<S> {
    /// Create a new transport loop.
    ///
    /// `peers` is a `SharedPeerMap` shared with the `ZoneConsensusDriver` and `ZoneRaftRegistry`,
    /// so ConfChange can insert new peers visible to the transport loop at runtime.
    pub fn new(
        driver: ZoneConsensusDriver<S>,
        peers: SharedPeerMap,
        client_pool: RaftClientPool,
    ) -> Self {
        let tick_interval = driver.config().tick_interval;
        let node_id = driver.config().id;
        Self {
            driver,
            peers,
            client_pool,
            tick_interval,
            zone_id: String::new(),
            node_id,
            ec_peer_state: HashMap::new(),
        }
    }

    /// Set the zone ID for multi-zone message routing.
    pub fn with_zone_id(mut self, zone_id: String) -> Self {
        self.zone_id = zone_id;
        self
    }

    /// Set the tick interval (default: 10ms).
    pub fn with_tick_interval(mut self, interval: Duration) -> Self {
        self.tick_interval = interval;
        self
    }

    /// Run the transport loop until shutdown is signaled.
    ///
    /// Each iteration: drain channel messages → advance raft → send outgoing → EC replicate.
    pub async fn run(mut self, mut shutdown: watch::Receiver<bool>) {
        let mut interval = tokio::time::interval(self.tick_interval);
        tracing::info!(
            "Transport loop started (zone={}, tick_interval={}ms, peers={})",
            if self.zone_id.is_empty() {
                "<single>"
            } else {
                &self.zone_id
            },
            self.tick_interval.as_millis(),
            self.peers.read().unwrap().len()
        );

        loop {
            tokio::select! {
                _ = interval.tick() => {
                    // Periodic tick — drives heartbeat and election timeouts
                }
                _ = shutdown.changed() => {
                    tracing::info!("Transport loop shutting down");
                    break;
                }
            }

            // 1. Drain all pending channel messages (step, propose, campaign)
            self.driver.process_messages();

            // 2. Advance raft state + apply entries + get outgoing messages
            match self.driver.advance().await {
                Ok(messages) => {
                    for msg in messages {
                        let target_id = msg.to;
                        // Clone address under read lock, then release before async send
                        // (std::sync lock must not be held across .await)
                        let addr = self.peers.read().unwrap().get(&target_id).cloned();
                        if let Some(addr) = addr {
                            self.send_message(target_id, &addr, msg).await;
                        } else {
                            tracing::warn!("No address for peer {} — dropping message", target_id);
                        }
                    }
                }
                Err(e) => {
                    tracing::error!("advance() error: {}", e);
                }
            }

            // 3. EC Phase C: async replication to peers
            self.replicate_ec_entries().await;
        }
    }

    /// Serialize and send an eraftpb::Message to a peer via gRPC.
    async fn send_message(&self, target_id: u64, addr: &NodeAddress, msg: raft::eraftpb::Message) {
        let bytes = match msg.write_to_bytes() {
            Ok(b) => b,
            Err(e) => {
                tracing::error!("Failed to serialize message for node {}: {}", target_id, e);
                return;
            }
        };

        match self.client_pool.get(addr).await {
            Ok(mut client) => {
                if let Err(e) = client.step_message(bytes, self.zone_id.clone()).await {
                    tracing::warn!(
                        "Failed to send message to node {} ({}): {}",
                        target_id,
                        addr.endpoint,
                        e
                    );
                    self.client_pool.remove(target_id).await;
                }
            }
            Err(e) => {
                tracing::warn!(
                    "Failed to connect to node {} ({}): {}",
                    target_id,
                    addr.endpoint,
                    e
                );
            }
        }
    }

    // =========================================================================
    // EC Phase C: Background replication
    // =========================================================================

    /// Drain unreplicated EC entries and send to peers.
    ///
    /// Uses per-peer exponential backoff (base=100ms, cap=60s) to avoid
    /// wasting resources on unreachable peers. Backoff resets on success.
    ///
    /// After sending, computes the majority quorum watermark and advances
    /// the ReplicationLog so `is_committed(token)` returns "committed".
    async fn replicate_ec_entries(&mut self) {
        let repl_log = match self.driver.replication_log() {
            Some(log) => Arc::clone(log),
            None => return, // No replication log (witness node)
        };

        // Drain unreplicated entries
        let entries = match repl_log.drain_unreplicated() {
            Ok(entries) => entries,
            Err(e) => {
                tracing::error!("Failed to drain unreplicated entries: {}", e);
                return;
            }
        };

        // Get current peer snapshot (read lock, released immediately)
        let peer_snapshot: Vec<(u64, NodeAddress)> = self
            .peers
            .read()
            .unwrap()
            .iter()
            .map(|(id, addr)| (*id, addr.clone()))
            .collect();

        let total_voters = peer_snapshot.len() + 1; // peers + self

        // Single node: advance watermark immediately (no peers to replicate to)
        if peer_snapshot.is_empty() {
            let max_seq = repl_log.max_seq();
            if max_seq > 1 {
                let _ = repl_log.advance_watermark(max_seq - 1);
            }
            return;
        }

        // Nothing to replicate
        if entries.is_empty() {
            return;
        }

        let now = Instant::now();

        // Send to each peer (respecting backoff)
        for (peer_id, peer_addr) in &peer_snapshot {
            let state = self
                .ec_peer_state
                .entry(*peer_id)
                .or_insert_with(PeerReplicationState::new);

            // Skip if in backoff period
            if now < state.next_attempt {
                continue;
            }

            // Anti-entropy: check if peer fell behind compacted WAL region
            let earliest = repl_log.earliest_seq();
            if state.acked_seq > 0 && state.acked_seq < earliest {
                if !state.needs_snapshot {
                    tracing::warn!(
                        peer = peer_id,
                        acked = state.acked_seq,
                        earliest,
                        "Peer fell behind compacted WAL — needs snapshot"
                    );
                    state.needs_snapshot = true;
                }
                continue; // Skip — incremental replication impossible
            }

            // Filter entries: only send what this peer hasn't acked yet
            let filtered: Vec<EcReplicationEntry> = entries
                .iter()
                .filter(|(seq, _)| *seq > state.acked_seq)
                .map(|(seq, entry)| EcReplicationEntry {
                    seq: *seq,
                    command: entry.command.clone(),
                    timestamp: entry.timestamp,
                    node_id: entry.node_id,
                })
                .collect();

            if filtered.is_empty() {
                continue;
            }

            let entry_count = filtered.len();

            // Send via gRPC
            match self.client_pool.get(peer_addr).await {
                Ok(mut client) => {
                    match client
                        .replicate_entries(self.zone_id.clone(), filtered, self.node_id)
                        .await
                    {
                        Ok(applied_up_to) => {
                            state.acked_seq = state.acked_seq.max(applied_up_to);
                            state.reset_backoff();
                            tracing::debug!(
                                peer = peer_id,
                                applied_up_to,
                                entries = entry_count,
                                "EC entries replicated to peer"
                            );
                        }
                        Err(e) => {
                            state.increase_backoff();
                            tracing::debug!(
                                peer = peer_id,
                                backoff_ms = state.backoff.as_millis(),
                                "EC replication failed: {}",
                                e
                            );
                        }
                    }
                }
                Err(e) => {
                    state.increase_backoff();
                    tracing::debug!(
                        peer = peer_id,
                        backoff_ms = state.backoff.as_millis(),
                        "EC replication connect failed: {}",
                        e
                    );
                }
            }
        }

        // Compute quorum watermark and advance
        let new_watermark = compute_ec_watermark(&self.ec_peer_state, &peer_snapshot, total_voters);
        if let Some(wm) = new_watermark {
            if let Err(e) = repl_log.advance_watermark(wm) {
                tracing::error!("Failed to advance EC watermark: {}", e);
            }
        }

        // WAL compaction: remove entries consumed by ALL peers (Kafka pattern)
        let min_peer_acked = peer_snapshot
            .iter()
            .filter_map(|(id, _)| self.ec_peer_state.get(id).map(|s| s.acked_seq))
            .filter(|&s| s > 0)
            .min()
            .unwrap_or(0);

        if min_peer_acked > 0 {
            if let Err(e) = repl_log.compact(min_peer_acked) {
                tracing::error!("WAL compaction failed: {}", e);
            }
        }
    }
}

/// Compute the EC replication watermark based on peer acknowledgements.
///
/// Self always counts as having applied all entries. We need `total_voters / 2`
/// additional peer acks for a majority (integer division).
///
/// Returns `None` if quorum cannot be reached (not enough peer acks).
fn compute_ec_watermark(
    peer_state: &HashMap<u64, PeerReplicationState>,
    peer_snapshot: &[(u64, NodeAddress)],
    total_voters: usize,
) -> Option<u64> {
    let needed_peer_acks = total_voters / 2; // self already counted
    if needed_peer_acks == 0 {
        return None; // single node — handled separately
    }

    // Collect acked_seq for known peers
    let mut acks: Vec<u64> = peer_snapshot
        .iter()
        .filter_map(|(id, _)| peer_state.get(id).map(|s| s.acked_seq))
        .collect();

    // Sort descending: highest acks first
    acks.sort_unstable_by(|a, b| b.cmp(a));

    // The (needed_peer_acks - 1)-th element (0-indexed) is the watermark:
    // it's the highest seq that at least `needed_peer_acks` peers have acked.
    acks.get(needed_peer_acks - 1).copied().filter(|&wm| wm > 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_ec_watermark_3_nodes() {
        // 3 nodes: self + 2 peers. Need 1 peer ack for majority.
        let mut peer_state = HashMap::new();
        peer_state.insert(
            2,
            PeerReplicationState {
                acked_seq: 5,
                backoff: EC_BACKOFF_BASE,
                next_attempt: Instant::now(),
                needs_snapshot: false,
            },
        );
        peer_state.insert(
            3,
            PeerReplicationState {
                acked_seq: 3,
                backoff: EC_BACKOFF_BASE,
                next_attempt: Instant::now(),
                needs_snapshot: false,
            },
        );

        let addr = NodeAddress {
            id: 0,
            endpoint: String::new(),
        };
        let peers = vec![(2, addr.clone()), (3, addr)];

        // total_voters = 3 (self + 2 peers), needed = 3/2 = 1
        // sorted acks desc: [5, 3], take index 0 = 5
        let wm = compute_ec_watermark(&peer_state, &peers, 3);
        assert_eq!(wm, Some(5));
    }

    #[test]
    fn test_compute_ec_watermark_5_nodes() {
        // 5 nodes: self + 4 peers. Need 2 peer acks for majority.
        let mut peer_state = HashMap::new();
        let addr = NodeAddress {
            id: 0,
            endpoint: String::new(),
        };
        let mut peers = Vec::new();

        for (id, ack) in [(2, 10), (3, 8), (4, 5), (5, 3)] {
            peer_state.insert(
                id,
                PeerReplicationState {
                    acked_seq: ack,
                    backoff: EC_BACKOFF_BASE,
                    next_attempt: Instant::now(),
                    needs_snapshot: false,
                },
            );
            peers.push((id, addr.clone()));
        }

        // total_voters = 5, needed = 5/2 = 2
        // sorted acks desc: [10, 8, 5, 3], take index 1 = 8
        let wm = compute_ec_watermark(&peer_state, &peers, 5);
        assert_eq!(wm, Some(8));
    }

    #[test]
    fn test_compute_ec_watermark_no_acks() {
        let peer_state = HashMap::new();
        let addr = NodeAddress {
            id: 0,
            endpoint: String::new(),
        };
        let peers = vec![(2, addr.clone()), (3, addr)];

        // No acks yet — should return None
        let wm = compute_ec_watermark(&peer_state, &peers, 3);
        assert_eq!(wm, None);
    }

    #[test]
    fn test_compute_ec_watermark_single_node() {
        let peer_state = HashMap::new();
        let peers: Vec<(u64, NodeAddress)> = vec![];

        // Single node: needed = 0, returns None (handled separately in run loop)
        let wm = compute_ec_watermark(&peer_state, &peers, 1);
        assert_eq!(wm, None);
    }

    #[test]
    fn test_peer_replication_state_backoff() {
        let mut state = PeerReplicationState::new();
        assert_eq!(state.backoff, EC_BACKOFF_BASE);

        state.increase_backoff();
        assert_eq!(state.backoff, EC_BACKOFF_BASE * 2);

        state.increase_backoff();
        assert_eq!(state.backoff, EC_BACKOFF_BASE * 4);

        // Reset
        state.reset_backoff();
        assert_eq!(state.backoff, EC_BACKOFF_BASE);

        // Test cap
        for _ in 0..20 {
            state.increase_backoff();
        }
        assert_eq!(state.backoff, EC_BACKOFF_CAP);
    }
}
