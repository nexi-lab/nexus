//! gRPC transport layer for Raft consensus.
//!
//! This module provides the network transport for Raft messages using gRPC.
//! It is built on [tonic](https://github.com/hyperium/tonic), a pure Rust
//! gRPC implementation.
//!
//! # Why gRPC?
//!
//! - **Streaming**: Native support for bidirectional streams (ideal for heartbeats)
//! - **Efficiency**: HTTP/2 multiplexing, long-lived connections
//! - **Code generation**: Less boilerplate than manual HTTP
//! - **Compatibility**: Works with tikv/raft-rs message patterns
//!
//! # Architecture
//!
//! All raft-rs message types (~15 types including votes, heartbeats, appends)
//! are multiplexed through a single `StepMessage` RPC as opaque protobuf v2
//! bytes (etcd/tikv pattern). EC replication uses a separate `ReplicateEntries`
//! RPC for async peer sync.
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::transport::{RaftClient, ClientConfig};
//!
//! // Create a client to talk to another node
//! let mut client = RaftClient::connect("http://10.0.0.2:2026", ClientConfig::default()).await?;
//!
//! // Send a raw raft-rs message via step_message
//! client.step_message(message_bytes, "my-zone".to_string()).await?;
//! ```
//!
//! # Feature Flag
//!
//! This module requires the `grpc` feature:
//!
//! ```toml
//! [dependencies]
//! nexus_raft = { version = "0.1", features = ["grpc"] }
//! ```

#[cfg(all(feature = "grpc", has_protos))]
pub(crate) mod certgen;
#[cfg(all(feature = "grpc", has_protos))]
pub use certgen::{bootstrap_tls, generate_join_token, generate_zone_ca, BootstrapTls};
#[cfg(all(feature = "grpc", has_protos))]
mod client;
#[cfg(all(feature = "grpc", has_protos))]
mod server;
#[cfg(all(feature = "grpc", has_protos))]
mod transport_loop;

#[cfg(all(feature = "grpc", has_protos))]
pub use client::{
    call_join_cluster, ClientConfig, ClusterInfoResult, JoinClusterResult, ProposeResult,
    QueryResult, RaftApiClient, RaftClient, RaftClientPool,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use server::{RaftGrpcServer, RaftWitnessServer, ServerConfig, WitnessZoneRegistry};
#[cfg(all(feature = "grpc", has_protos))]
pub use transport_loop::TransportLoop;

/// Forward a Raft proposal to a leader node via gRPC Propose RPC.
///
/// Used by `ZoneConsensus::propose()` when the local node is a follower.
/// Serializes the command with bincode and sends as `raw_command` bytes
/// in `ProposeRequest` — avoids double serialization (bincode→proto→bincode).
///
/// `cached_client` provides a reusable `RaftApiClient` across calls.
/// On first use (or after eviction), a new connection is established and
/// cached. On transport error, the cached client is evicted so the next
/// call reconnects.
#[cfg(all(feature = "grpc", has_protos))]
pub(crate) async fn forward_propose(
    client_pool: &RaftClientPool,
    leader_addr: &NodeAddress,
    command: crate::raft::Command,
    zone_id: &str,
    cached_client: &tokio::sync::Mutex<Option<(String, RaftApiClient)>>,
) -> crate::raft::Result<crate::raft::CommandResult> {
    use crate::raft::RaftError;

    let raw_bytes =
        bincode::serialize(&command).map_err(|e| RaftError::Serialization(e.to_string()))?;

    // Get or create a cached API client. Evict if the leader endpoint changed.
    let mut api_client = {
        let mut guard = cached_client.lock().await;
        match guard.take() {
            Some((endpoint, client)) if endpoint == leader_addr.endpoint => client,
            _ => {
                // Connect with short timeouts — fail fast on unreachable leader.
                let mut forward_config = client_pool.config().clone();
                forward_config.connect_timeout = std::time::Duration::from_secs(2);
                forward_config.request_timeout = std::time::Duration::from_secs(5);

                RaftApiClient::connect(&leader_addr.endpoint, forward_config)
                    .await
                    .map_err(|e| RaftError::Transport(e.to_string()))?
            }
        }
    };

    let request = tonic::Request::new(proto::nexus::raft::ProposeRequest {
        command: None,
        request_id: String::new(),
        zone_id: zone_id.to_string(),
        raw_command: raw_bytes,
        forwarded: true,
    });

    let result = api_client
        .inner_mut()
        .propose(request)
        .await
        .map_err(|e| RaftError::Transport(e.to_string()));

    match result {
        Ok(response) => {
            // Success — cache the client for reuse.
            let mut guard = cached_client.lock().await;
            *guard = Some((leader_addr.endpoint.clone(), api_client));

            let resp = response.into_inner();
            if !resp.success {
                if let Some(ref err) = resp.error {
                    if err.contains("Not the leader") || err.contains("not leader") {
                        return Err(RaftError::NotLeader { leader_hint: None });
                    } else {
                        return Err(RaftError::Raft(err.clone()));
                    }
                }
                return Err(RaftError::Raft("Propose failed".to_string()));
            }

            // Decode the typed RaftResponse so lock/metadata commands see
            // the same CommandResult variant the leader computed. Without
            // this, AcquireLock/ReleaseLock/ExtendLock hit the
            // "Unexpected result type" branch in PyZoneHandle (Issue F2 C7).
            Ok(proto_result_to_command_result(resp.result))
        }
        Err(e) => {
            // Transport error — evict cached client (already taken above).
            // Next call will reconnect.
            Err(e)
        }
    }
}

/// Decode a proto `RaftResponse` back into an internal `CommandResult`.
///
/// Only the variants that the server actually emits (Success / LockResult)
/// are handled; other commands carry no typed result and collapse to
/// `Success`, matching the old single-node path.
#[cfg(all(feature = "grpc", has_protos))]
fn proto_result_to_command_result(
    result: Option<proto::nexus::raft::RaftResponse>,
) -> crate::raft::CommandResult {
    use crate::raft::{CommandResult, HolderInfo, LockAcquireResult, LockMode};
    use proto::nexus::raft::raft_response::Result as ProtoVariant;

    let Some(resp) = result else {
        return CommandResult::Success;
    };

    match resp.result {
        Some(ProtoVariant::LockResult(lr)) => {
            let holders = if lr.acquired {
                vec![HolderInfo {
                    lock_id: String::new(),
                    holder_info: lr.current_holder.clone().unwrap_or_default(),
                    // Witness-path gRPC response has no per-holder
                    // mode field — predates F4 C1. Default to
                    // Exclusive (the only mode witnesses could
                    // observe before F4).
                    mode: LockMode::Exclusive,
                    acquired_at: 0,
                    expires_at: (lr.expires_at_ms / 1000) as u64,
                }]
            } else {
                Vec::new()
            };
            CommandResult::LockResult(LockAcquireResult {
                acquired: lr.acquired,
                current_holders: holders.len() as u32,
                max_holders: 0,
                holders,
            })
        }
        Some(ProtoVariant::MetadataResult(_)) | None => CommandResult::Success,
    }
}

// ---------------------------------------------------------------------------
// Re-export shared transport types from transport.
// These were previously defined locally but are now canonical in transport.
// The entire `transport` module is behind `#[cfg(feature = "grpc")]` in lib.rs,
// so `transport` is always available here.
// ---------------------------------------------------------------------------
#[cfg(feature = "grpc")]
pub use transport_primitives::{
    hostname_to_node_id, NodeAddress, PeerAddress, TlsConfig, TransportError,
};
#[cfg(feature = "grpc")]
pub type Result<T> = transport_primitives::Result<T>;

/// Shared peer map that can be updated at runtime (e.g., when new nodes join via ConfChange).
///
/// Uses `std::sync::RwLock` (not tokio) because:
/// - Read/write operations are very fast (HashMap insert/lookup)
/// - Accessed from both sync (DashMap guard) and async (transport loop) contexts
/// - Write-rarely, read-often pattern — no contention in practice
pub type SharedPeerMap =
    std::sync::Arc<std::sync::RwLock<std::collections::HashMap<u64, NodeAddress>>>;

// Re-export generated types when grpc feature is enabled and protos were compiled
#[cfg(all(feature = "grpc", has_protos))]
pub mod proto {
    //! Generated protobuf types and gRPC services.
    //!
    //! This module contains the Rust types generated from proto files.
    //! Structure mirrors the proto package hierarchy:
    //!   - nexus::core - FileMetadata, PaginatedResult
    //!   - nexus::raft - ZoneTransportService, ZoneApiService, commands, transport messages

    /// Core types (FileMetadata, etc.)
    pub mod nexus {
        pub mod core {
            include!(concat!(env!("OUT_DIR"), "/nexus.core.rs"));
        }
        #[allow(
            clippy::large_enum_variant,
            reason = "generated proto code; will configure prost boxing when variants are stabilized"
        )]
        pub mod raft {
            include!(concat!(env!("OUT_DIR"), "/nexus.raft.rs"));
        }
    }

    // Re-export for convenience
    pub use nexus::core::*;
    pub use nexus::raft::*;
}

// Tests for PeerAddress, NodeAddress, hostname_to_node_id now live in transport.
