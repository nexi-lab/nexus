//! Transport loop — background task that drives the Raft event loop.
//!
//! This task periodically calls `RaftNode::advance()` to process ticks,
//! persist state, apply committed entries, and send outgoing messages
//! to peer nodes via gRPC `StepMessage` RPC.
//!
//! # Architecture
//!
//! ```text
//! ┌──────────────┐     advance()      ┌──────────────┐
//! │  RaftNode    │ ──────────────────> │  Messages    │
//! │  (raft-rs)   │                    │  to send     │
//! └──────────────┘                    └──────┬───────┘
//!                                            │
//!                                            ▼
//!                                   ┌──────────────────┐
//!                                   │  RaftClientPool  │
//!                                   │  .step_message() │
//!                                   └──────────────────┘
//! ```

use super::client::RaftClientPool;
use super::NodeAddress;
use crate::raft::{RaftNode, StateMachine};
use protobuf::Message as ProtobufV2Message;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;

/// Background task that drives the Raft event loop and sends messages to peers.
pub struct TransportLoop<S: StateMachine + 'static> {
    /// The RaftNode to drive.
    node: Arc<RaftNode<S>>,
    /// Known peers (node_id → address).
    peers: Arc<HashMap<u64, NodeAddress>>,
    /// Connection pool for sending messages to peers.
    client_pool: RaftClientPool,
    /// How often to call advance() (default: 100ms).
    tick_interval: Duration,
}

impl<S: StateMachine + Send + Sync + 'static> TransportLoop<S> {
    /// Create a new transport loop.
    pub fn new(
        node: Arc<RaftNode<S>>,
        peers: HashMap<u64, NodeAddress>,
        client_pool: RaftClientPool,
    ) -> Self {
        Self {
            node,
            peers: Arc::new(peers),
            client_pool,
            tick_interval: Duration::from_millis(100),
        }
    }

    /// Set the tick interval (default: 100ms).
    pub fn with_tick_interval(mut self, interval: Duration) -> Self {
        self.tick_interval = interval;
        self
    }

    /// Run the transport loop until shutdown is signaled.
    ///
    /// This drives the Raft event loop:
    /// 1. Calls `node.advance()` to process ticks, persist state, apply entries
    /// 2. Sends outgoing messages to peers via `RaftClientPool::step_message()`
    /// 3. Repeats at `tick_interval`
    pub async fn run(self, mut shutdown: watch::Receiver<bool>) {
        let mut interval = tokio::time::interval(self.tick_interval);
        tracing::info!(
            "Transport loop started (tick_interval={}ms, peers={})",
            self.tick_interval.as_millis(),
            self.peers.len()
        );

        loop {
            tokio::select! {
                _ = interval.tick() => {
                    match self.node.advance().await {
                        Ok(messages) => {
                            for msg in messages {
                                let target_id = msg.to;
                                if let Some(addr) = self.peers.get(&target_id) {
                                    self.send_message(target_id, addr, msg).await;
                                } else {
                                    tracing::warn!(
                                        "No address for peer {} — dropping message",
                                        target_id
                                    );
                                }
                            }
                        }
                        Err(e) => {
                            tracing::error!("advance() error: {}", e);
                        }
                    }
                }
                _ = shutdown.changed() => {
                    tracing::info!("Transport loop shutting down");
                    break;
                }
            }
        }
    }

    /// Serialize and send an eraftpb::Message to a peer via gRPC.
    async fn send_message(
        &self,
        target_id: u64,
        addr: &NodeAddress,
        msg: raft::eraftpb::Message,
    ) {
        // Serialize the eraftpb::Message to protobuf v2 bytes
        let bytes = match msg.write_to_bytes() {
            Ok(b) => b,
            Err(e) => {
                tracing::error!(
                    "Failed to serialize message for node {}: {}",
                    target_id,
                    e
                );
                return;
            }
        };

        // Get a client from the pool and send
        match self.client_pool.get(addr).await {
            Ok(mut client) => {
                if let Err(e) = client.step_message(bytes).await {
                    tracing::warn!(
                        "Failed to send message to node {} ({}): {}",
                        target_id,
                        addr.endpoint,
                        e
                    );
                    // Remove failed connection so pool creates a fresh one next time
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