//! Transport loop — background task that drives the Raft actor event loop.
//!
//! This task owns the [`RaftNodeDriver`] exclusively and calls
//! [`process_messages()`] + [`advance()`] sequentially to maintain the
//! raft-rs single-owner invariant.
//!
//! # Architecture
//!
//! ```text
//! ┌──────────────────────┐  process_messages()  ┌──────────────┐
//! │  mpsc channel msgs   │ ───────────────────> │ RaftNodeDriver│
//! │  (step/propose/etc.) │                      │ (owns RawNode)│
//! └──────────────────────┘                      └──────┬───────┘
//!                                                      │ advance()
//!                                                      ▼
//!                                              ┌──────────────────┐
//!                                              │  Outgoing msgs   │
//!                                              │  → RaftClientPool│
//!                                              └──────────────────┘
//! ```

use super::client::RaftClientPool;
use super::NodeAddress;
use crate::raft::{RaftNodeDriver, StateMachine};
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;

/// Background task that drives the Raft event loop and sends messages to peers.
///
/// Owns the [`RaftNodeDriver`] exclusively — this is the single task that
/// touches `RawNode`.
pub struct TransportLoop<S: StateMachine + 'static> {
    /// The RaftNodeDriver to drive (exclusive ownership).
    driver: RaftNodeDriver<S>,
    /// Known peers (node_id → address).
    peers: Arc<HashMap<u64, NodeAddress>>,
    /// Connection pool for sending messages to peers.
    client_pool: RaftClientPool,
    /// How often to call advance() (default: 10ms).
    tick_interval: Duration,
    /// Zone ID for multi-zone message routing.
    zone_id: String,
}

impl<S: StateMachine + Send + Sync + 'static> TransportLoop<S> {
    /// Create a new transport loop.
    pub fn new(
        driver: RaftNodeDriver<S>,
        peers: HashMap<u64, NodeAddress>,
        client_pool: RaftClientPool,
    ) -> Self {
        let tick_interval = driver.config().tick_interval;
        Self {
            driver,
            peers: Arc::new(peers),
            client_pool,
            tick_interval,
            zone_id: String::new(),
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
    /// Each iteration: drain channel messages → advance raft → send outgoing.
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
            self.peers.len()
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
                        if let Some(addr) = self.peers.get(&target_id) {
                            self.send_message(target_id, addr, msg).await;
                        } else {
                            tracing::warn!("No address for peer {} — dropping message", target_id);
                        }
                    }
                }
                Err(e) => {
                    tracing::error!("advance() error: {}", e);
                }
            }
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
}
