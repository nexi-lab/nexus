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
//!                                              │  EC bg replicate │
//!                                              │  drain WAL →     │
//!                                              │  replicate peers │
//!                                              └──────────────────┘
//! ```

use super::client::RaftClientPool;
use super::proto::nexus::raft::EcReplicationEntry;
use super::{NodeAddress, SharedPeerMap};
use crate::raft::{StateMachine, ZoneConsensusDriver};
use protobuf::Message as ProtobufV2Message;
use raft::eraftpb::MessageType;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinSet;

use std::time::Duration as StdDuration;

/// A transport send that failed — surfaced back from a spawned send task
/// to the driver loop so raft-rs can be told and the affected peer's
/// `Progress` tracker can leave its stuck state.
///
/// `is_snapshot` distinguishes a `MsgSnapshot` failure (needs
/// `report_snapshot(peer, Failure)`) from any other message-type
/// failure (needs `report_unreachable(peer)`).  Both are required
/// to keep raft-rs's state machine honest under flaky transports.
#[derive(Debug, Clone, Copy)]
struct TransportFailure {
    peer_id: u64,
    is_snapshot: bool,
}

/// Base backoff interval for EC replication retries.
const EC_BACKOFF_BASE: Duration = Duration::from_millis(100);
/// Maximum backoff interval (cap) for EC replication retries.
const EC_BACKOFF_CAP: Duration = Duration::from_secs(60);

/// Timeout for individual Raft consensus message sends.
///
/// Sized to absorb a `MsgSnapshot` transfer (KB-MB of state machine
/// bytes over a WAN path) plus initial connect setup, not just a
/// bare heartbeat round-trip.  The previous 5s was tuned for "raft
/// heartbeats must be fast" and is correct for that case alone, but
/// the same `send_raft_message` call also delivers snapshots to a
/// newly joined Learner — those routinely exceed 5s over Tailscale
/// or DERP, leading to client-side cancellation that the *peer*
/// never sees as a failure (the server-side receive may complete
/// after our timeout already aborted the RPC), keeping replication
/// permanently stuck after Mac (Learner) join.  30 s aligns with
/// the keep-alive interval (PR #4234) and gives the snapshot path
/// adequate room while still bounding the spawned task lifetime.
///
/// Real unreachable-peer detection lives elsewhere: tonic's H2
/// keep-alive (30 s ping / 10 s timeout) and raft-rs `Progress`
/// state managed via `report_unreachable` (PR #4230) handle it on
/// their own time scales without depending on this per-call cap.
const RAFT_SEND_TIMEOUT: StdDuration = StdDuration::from_secs(30);

/// Timeout for a single EC replication send.
///
/// EC replication is background data movement; it must not stall the transport
/// loop that drives Raft ticks and elections. Unreachable peers are retried by
/// the per-peer backoff below.
const EC_SEND_TIMEOUT: StdDuration = StdDuration::from_millis(250);

/// Maximum entries to send per peer per EC replication cycle.
/// Caps memory and prevents the tonic request timeout from being exceeded
/// on large backlogs.
const EC_MAX_ENTRIES_PER_BATCH: usize = 500;

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
    /// This node's advertise address.  Carried in every outbound
    /// `StepMessageRequest.sender_address` so receivers learn
    /// `(self.node_id -> self_address)` on first contact — the
    /// transport peer-map's runtime SSOT under the opaque-ID
    /// contract.  Empty string means "no advertise address known"
    /// and disables learning on the receiver.
    self_address: String,
    /// Per-peer EC replication tracking (peer_id → state).
    ec_peer_state: HashMap<u64, PeerReplicationState>,
    /// Sender half of the transport-failure channel.  Cloned into each
    /// `send_messages_fire_and_forget` task so a transport-level send
    /// error (network down, HTTP/2 keepalive timeout, tonic transport
    /// error) can be reported back to this loop without holding a
    /// reference to the driver across `await` points.
    failure_tx: mpsc::UnboundedSender<TransportFailure>,
    /// Receiver half of the transport-failure channel.  Drained at the
    /// top of each [`Self::run`] iteration and translated into
    /// `driver.report_unreachable` / `driver.report_snapshot(_, Failure)`
    /// calls — without those, raft-rs's `Progress` tracker stays in
    /// `Replicate` / `Snapshot` state forever after a single failure.
    failure_rx: mpsc::UnboundedReceiver<TransportFailure>,
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
        let (failure_tx, failure_rx) = mpsc::unbounded_channel();
        Self {
            driver,
            peers,
            client_pool,
            tick_interval,
            zone_id: String::new(),
            node_id,
            self_address: String::new(),
            ec_peer_state: HashMap::new(),
            failure_tx,
            failure_rx,
        }
    }

    /// Set the zone ID for multi-zone message routing.
    pub fn with_zone_id(mut self, zone_id: String) -> Self {
        self.zone_id = zone_id;
        self
    }

    /// Set this node's advertise address — see [`Self::self_address`].
    pub fn with_self_address(mut self, self_address: String) -> Self {
        self.self_address = self_address;
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

            // 0. Drain any transport send failures reported by previously
            //    spawned send tasks and forward them to raft-rs.  Must
            //    run before `process_messages` / `advance` so the next
            //    tick's outgoing-message set reflects the updated
            //    `Progress` state (peer in Probe rather than stuck in
            //    Replicate / Snapshot).  Cheap: non-blocking try_recv
            //    drain, returns immediately when the channel is empty
            //    (the steady-state happy path).
            while let Ok(failure) = self.failure_rx.try_recv() {
                self.driver.report_unreachable(failure.peer_id);
                if failure.is_snapshot {
                    self.driver
                        .report_snapshot(failure.peer_id, raft::SnapshotStatus::Failure);
                }
            }

            // 1. Drain all pending channel messages (step, propose, campaign)
            self.driver.process_messages();

            // 2. Advance raft state + apply entries + get outgoing messages
            match self.driver.advance().await {
                Ok(messages) => {
                    if !messages.is_empty() {
                        self.send_messages_fire_and_forget(messages);
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

    /// Take a point-in-time snapshot of the peer map (read lock, released immediately).
    ///
    /// Used by both `send_messages_fire_and_forget` and `replicate_ec_entries` to
    /// get a consistent view of peers without holding the read lock across async work.
    fn snapshot_peers(&self) -> HashMap<u64, NodeAddress> {
        self.peers
            .read()
            .unwrap()
            .iter()
            .map(|(id, addr)| (*id, addr.clone()))
            .collect()
    }

    /// Send Raft messages to peers concurrently using JoinSet.
    ///
    /// Each peer send gets its own task with an independent timeout.
    /// Slow peers don't block fast peers. If a send fails or times out,
    /// the error is logged and the client is evicted from the pool.
    /// Send Raft messages to peers — fire and forget.
    ///
    /// Each message is spawned as an independent task with its own timeout.
    /// The transport loop does NOT await results — this ensures tick() is
    /// never blocked by slow/unreachable peers.  Raft handles retransmission
    /// via its own heartbeat/election timeout mechanism.
    ///
    /// Failed sends log a warning and evict the client from the pool
    /// (reconnected on next attempt).
    fn send_messages_fire_and_forget(&self, messages: Vec<raft::eraftpb::Message>) {
        let peers_snapshot = self.snapshot_peers();

        for msg in messages {
            let target_id = msg.to;
            let addr = match peers_snapshot.get(&target_id) {
                Some(a) => a.clone(),
                None => {
                    tracing::warn!("No address for peer {} — dropping message", target_id);
                    continue;
                }
            };

            // Capture the message type *before* the move into the spawned
            // task so we can report a `MsgSnapshot` failure correctly.
            // A failed snapshot send needs both `report_unreachable` and
            // `report_snapshot(_, Failure)` — without the latter, the
            // peer's Progress stays in the `Snapshot` state and raft-rs
            // never retries.
            let is_snapshot = msg.get_msg_type() == MessageType::MsgSnapshot;

            let client_pool = self.client_pool.clone();
            let zone_id = self.zone_id.clone();
            let self_address = self.self_address.clone();
            let failure_tx = self.failure_tx.clone();

            tokio::spawn(async move {
                let result = tokio::time::timeout(
                    RAFT_SEND_TIMEOUT,
                    send_raft_message(&client_pool, target_id, &addr, msg, zone_id, self_address),
                )
                .await;

                let send_failed = match result {
                    Ok(Ok(())) => false,
                    Ok(Err(e)) => {
                        tracing::warn!(peer = target_id, "Raft message send failed: {}", e);
                        client_pool.remove(target_id).await;
                        true
                    }
                    Err(_elapsed) => {
                        tracing::warn!(
                            peer = target_id,
                            "Raft message send timeout after {:?}",
                            RAFT_SEND_TIMEOUT,
                        );
                        client_pool.remove(target_id).await;
                        true
                    }
                };

                if send_failed {
                    // Best-effort signal back to the driver loop.  If the
                    // receiver was dropped (loop is shutting down) the
                    // send is a no-op; that is fine because the driver
                    // is on its way out anyway.
                    let _ = failure_tx.send(TransportFailure {
                        peer_id: target_id,
                        is_snapshot,
                    });
                }
            });
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
        let peer_map = self.snapshot_peers();
        let peer_snapshot: Vec<(u64, NodeAddress)> = peer_map.into_iter().collect();

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

        // Collect work items for eligible peers
        struct EcSendTask {
            peer_id: u64,
            peer_addr: NodeAddress,
            entries: Vec<EcReplicationEntry>,
        }

        let mut tasks: Vec<EcSendTask> = Vec::new();

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

            // Clear needs_snapshot if peer has caught up (e.g., via external
            // snapshot delivery or WAL re-expansion)
            if state.needs_snapshot {
                if state.acked_seq >= earliest {
                    tracing::info!(
                        peer = peer_id,
                        acked = state.acked_seq,
                        earliest,
                        "Peer caught up — clearing needs_snapshot"
                    );
                    state.needs_snapshot = false;
                } else {
                    tracing::warn!(
                        peer = peer_id,
                        acked = state.acked_seq,
                        earliest,
                        "Peer needs snapshot (not yet implemented) — skipping"
                    );
                    continue;
                }
            }

            if state.acked_seq > 0 && state.acked_seq < earliest {
                tracing::warn!(
                    peer = peer_id,
                    acked = state.acked_seq,
                    earliest,
                    "Peer fell behind compacted WAL — needs snapshot"
                );
                state.needs_snapshot = true;
                continue; // Skip — incremental replication impossible
            }

            // Filter entries: only send what this peer hasn't acked yet.
            // Cap at EC_MAX_ENTRIES_PER_BATCH to bound memory and stay
            // within the tonic request timeout on large backlogs.
            let filtered: Vec<EcReplicationEntry> = entries
                .iter()
                .filter(|(seq, _)| *seq > state.acked_seq)
                .take(EC_MAX_ENTRIES_PER_BATCH)
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

            tasks.push(EcSendTask {
                peer_id: *peer_id,
                peer_addr: peer_addr.clone(),
                entries: filtered,
            });
        }

        // Fan out EC replication sends in parallel
        if !tasks.is_empty() {
            let mut join_set: JoinSet<(u64, std::result::Result<u64, String>)> = JoinSet::new();

            for task in tasks {
                let client_pool = self.client_pool.clone();
                let zone_id = self.zone_id.clone();
                let node_id = self.node_id;

                join_set.spawn(async move {
                    let send = send_ec_entries(
                        &client_pool,
                        &task.peer_addr,
                        zone_id,
                        task.entries,
                        node_id,
                    );
                    match tokio::time::timeout(EC_SEND_TIMEOUT, send).await {
                        Ok(Ok(applied_up_to)) => (task.peer_id, Ok(applied_up_to)),
                        Ok(Err(e)) => (task.peer_id, Err(e)),
                        Err(_) => (
                            task.peer_id,
                            Err(format!(
                                "EC replication timed out after {:?}",
                                EC_SEND_TIMEOUT
                            )),
                        ),
                    }
                });
            }

            // Collect results and update per-peer state
            while let Some(result) = join_set.join_next().await {
                match result {
                    Ok((peer_id, Ok(applied_up_to))) => {
                        if let Some(state) = self.ec_peer_state.get_mut(&peer_id) {
                            state.acked_seq = state.acked_seq.max(applied_up_to);
                            state.reset_backoff();
                            tracing::debug!(
                                peer = peer_id,
                                applied_up_to,
                                "EC entries replicated to peer"
                            );
                        }
                    }
                    Ok((peer_id, Err(e))) => {
                        if let Some(state) = self.ec_peer_state.get_mut(&peer_id) {
                            state.increase_backoff();
                            tracing::debug!(
                                peer = peer_id,
                                backoff_ms = state.backoff.as_millis(),
                                "EC replication failed: {}",
                                e
                            );
                        }
                    }
                    Err(join_err) => {
                        tracing::error!("EC replication task panicked: {}", join_err);
                    }
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

/// Send a single Raft message to a peer (used by parallel send tasks).
async fn send_raft_message(
    client_pool: &RaftClientPool,
    target_id: u64,
    addr: &NodeAddress,
    msg: raft::eraftpb::Message,
    zone_id: String,
    self_address: String,
) -> std::result::Result<(), String> {
    let bytes = msg
        .write_to_bytes()
        .map_err(|e| format!("serialize error for node {}: {}", target_id, e))?;

    let mut client = client_pool
        .get(addr)
        .await
        .map_err(|e| format!("connect to node {} ({}): {}", target_id, addr.endpoint, e))?;

    client
        .step_message(bytes, zone_id, self_address)
        .await
        .map_err(|e| format!("send to node {} ({}): {}", target_id, addr.endpoint, e))
}

/// Send EC replication entries to a peer (used by parallel EC tasks).
async fn send_ec_entries(
    client_pool: &RaftClientPool,
    peer_addr: &NodeAddress,
    zone_id: String,
    entries: Vec<EcReplicationEntry>,
    sender_node_id: u64,
) -> std::result::Result<u64, String> {
    let mut client = client_pool.get(peer_addr).await.map_err(|e| {
        format!(
            "connect to {} ({}): {}",
            peer_addr.id, peer_addr.endpoint, e
        )
    })?;

    client
        .replicate_entries(zone_id, entries, sender_node_id)
        .await
        .map_err(|e| {
            format!(
                "replicate to {} ({}): {}",
                peer_addr.id, peer_addr.endpoint, e
            )
        })
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

        let addr = NodeAddress::new(0, "");
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
        let addr = NodeAddress::new(0, "");
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
        let addr = NodeAddress::new(0, "");
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

    #[test]
    fn test_peer_send_timeout_constant() {
        // Verify timeout is reasonable
        assert!(RAFT_SEND_TIMEOUT.as_secs() >= 1);
        assert!(RAFT_SEND_TIMEOUT.as_secs() <= 30);
    }
}
