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
mod client;
#[cfg(all(feature = "grpc", has_protos))]
mod server;
#[cfg(all(feature = "grpc", has_protos))]
mod transport_loop;

#[cfg(all(feature = "grpc", has_protos))]
pub use certgen::{ca_fingerprint_from_pem, parse_join_token, ParsedJoinToken};
#[cfg(all(feature = "grpc", has_protos))]
pub use client::{
    call_join_cluster, ClientConfig, ClusterInfoResult, JoinClusterResult, ProposeResult,
    QueryResult, RaftApiClient, RaftClient, RaftClientPool,
};
#[cfg(all(feature = "grpc", has_protos))]
pub use server::{RaftGrpcServer, RaftWitnessServer, ServerConfig, WitnessZoneRegistry};
#[cfg(all(feature = "grpc", has_protos))]
pub use transport_loop::TransportLoop;

#[cfg(all(feature = "grpc", has_protos))]
fn command_name(command: &crate::raft::Command) -> &'static str {
    use crate::raft::Command;
    match command {
        Command::SetMetadata { .. } => "SetMetadata",
        Command::DeleteMetadata { .. } => "DeleteMetadata",
        Command::AcquireLock { .. } => "AcquireLock",
        Command::ReleaseLock { .. } => "ReleaseLock",
        Command::ExtendLock { .. } => "ExtendLock",
        Command::CasSetMetadata { .. } => "CasSetMetadata",
        Command::AdjustCounter { .. } => "AdjustCounter",
        Command::Noop => "Noop",
    }
}

#[cfg(all(feature = "grpc", has_protos))]
fn result_name(result: &crate::raft::CommandResult) -> &'static str {
    use crate::raft::CommandResult;
    match result {
        CommandResult::Success => "Success",
        CommandResult::Value(_) => "Value",
        CommandResult::LockResult(_) => "LockResult",
        CommandResult::CasResult { .. } => "CasResult",
        CommandResult::Error(_) => "Error",
    }
}

#[cfg(all(feature = "grpc", has_protos))]
const MAX_FORWARDED_RAW_RESULT_BYTES: u64 = 4 * 1024 * 1024;
#[cfg(all(feature = "grpc", has_protos))]
const RAW_RESULT_NEGATIVE_CACHE_TTL: std::time::Duration = std::time::Duration::from_secs(30);
#[cfg(all(feature = "grpc", has_protos))]
const RAW_RESULT_POSITIVE_CACHE_TTL: std::time::Duration = std::time::Duration::from_secs(30);
#[cfg(all(feature = "grpc", has_protos))]
const FORWARDED_RESULT_PROTOCOL_VERSION: &str = "nexus-raft-forward-v1";

#[cfg(all(feature = "grpc", has_protos))]
fn requires_lossless_forward_result(command: &crate::raft::Command) -> bool {
    matches!(
        command,
        crate::raft::Command::AcquireLock { .. }
            | crate::raft::Command::CasSetMetadata { .. }
            | crate::raft::Command::AdjustCounter { .. }
    )
}

#[cfg(all(feature = "grpc", has_protos))]
fn validate_forwarded_result(
    command: &crate::raft::Command,
    result: &crate::raft::CommandResult,
) -> crate::raft::Result<()> {
    use crate::raft::{Command, CommandResult, RaftError};

    let expected = match command {
        Command::SetMetadata { .. }
        | Command::DeleteMetadata { .. }
        | Command::ReleaseLock { .. }
        | Command::ExtendLock { .. }
        | Command::Noop => matches!(result, CommandResult::Success | CommandResult::Error(_)),
        Command::AcquireLock { .. } => {
            matches!(
                result,
                CommandResult::LockResult(_) | CommandResult::Error(_)
            )
        }
        Command::CasSetMetadata { .. } => {
            matches!(
                result,
                CommandResult::CasResult { .. } | CommandResult::Error(_)
            )
        }
        Command::AdjustCounter { .. } => {
            matches!(result, CommandResult::Value(_) | CommandResult::Error(_))
        }
    };

    if expected {
        return Ok(());
    }

    Err(RaftError::Serialization(format!(
        "Forwarded {} returned incompatible result variant {}",
        command_name(command),
        result_name(result),
    )))
}

#[cfg(all(feature = "grpc", has_protos))]
fn lock_state_from_proto(lock: proto::nexus::raft::LockResult) -> crate::raft::LockState {
    let proto::nexus::raft::LockResult {
        acquired,
        current_holder,
        expires_at_ms,
    } = lock;
    let expires_at_secs = (expires_at_ms.max(0) as u64) / 1000;

    let holders: Vec<crate::raft::HolderInfo> = current_holder
        .into_iter()
        .map(|holder_info| crate::raft::HolderInfo {
            lock_id: String::new(),
            holder_info,
            acquired_at: 0,
            expires_at: expires_at_secs,
        })
        .collect();

    crate::raft::LockState {
        acquired,
        current_holders: holders.len() as u32,
        max_holders: 1,
        holders,
    }
}

#[cfg(all(feature = "grpc", has_protos))]
fn decode_legacy_forwarded_result(
    command: &crate::raft::Command,
    response: proto::nexus::raft::RaftResponse,
) -> crate::raft::Result<crate::raft::CommandResult> {
    use crate::raft::{Command, CommandResult, RaftError};
    use proto::nexus::raft::raft_response::Result as ProtoResponseResultVariant;

    if !response.success {
        if let Some(err) = response.error {
            if matches!(command, Command::CasSetMetadata { .. }) {
                return Err(RaftError::Serialization(format!(
                    "Forwarded {} response omitted CAS version details: {}",
                    command_name(command),
                    err
                )));
            }
            return Ok(CommandResult::Error(err));
        }
        return Err(RaftError::Serialization(format!(
            "Forwarded {} failed without details",
            command_name(command)
        )));
    }

    match response.result {
        Some(ProtoResponseResultVariant::LockResult(lock)) => {
            Ok(CommandResult::LockResult(lock_state_from_proto(lock)))
        }
        Some(ProtoResponseResultVariant::MetadataResult(_)) => {
            if matches!(command, Command::AdjustCounter { .. }) {
                Err(RaftError::Serialization(
                    "Forwarded AdjustCounter response omitted counter bytes".to_string(),
                ))
            } else {
                Ok(CommandResult::Success)
            }
        }
        None => Ok(CommandResult::Success),
    }
}

#[cfg(all(feature = "grpc", has_protos))]
fn decode_forwarded_propose_response(
    command: &crate::raft::Command,
    response: proto::nexus::raft::ProposeResponse,
) -> crate::raft::Result<crate::raft::CommandResult> {
    use crate::raft::{CommandResult, RaftError};

    let proto::nexus::raft::ProposeResponse {
        success,
        error,
        leader_address,
        result,
        raw_result,
        applied_index,
        ..
    } = response;
    let mut legacy_result = result;

    if !success {
        if let Some(err) = error {
            if err.contains("Not the leader") || err.contains("not leader") {
                return Err(RaftError::NotLeader { leader_hint: None });
            }
            return Err(RaftError::Raft(err));
        }
        if leader_address.is_some() {
            return Err(RaftError::NotLeader { leader_hint: None });
        }
        return Err(RaftError::Raft(format!(
            "Forwarded {} failed without details",
            command_name(command)
        )));
    }

    let result = if !raw_result.is_empty() {
        if raw_result.len() as u64 > MAX_FORWARDED_RAW_RESULT_BYTES {
            if requires_lossless_forward_result(command) {
                return Err(RaftError::Raft(format!(
                    "Forwarded {} appears committed at index {} but returned an oversized result ({} bytes > {} bytes); \
                     fetch authoritative state from leader before retrying",
                    command_name(command),
                    applied_index,
                    raw_result.len(),
                    MAX_FORWARDED_RAW_RESULT_BYTES
                )));
            }
            if let Some(proto_result) = legacy_result.take() {
                tracing::warn!(
                    command = command_name(command),
                    size = raw_result.len(),
                    limit = MAX_FORWARDED_RAW_RESULT_BYTES,
                    "raw_result exceeds size limit; falling back to legacy result"
                );
                decode_legacy_forwarded_result(command, proto_result)?
            } else {
                return Err(RaftError::Serialization(format!(
                    "Forwarded {} raw_result too large ({} bytes > {} bytes)",
                    command_name(command),
                    raw_result.len(),
                    MAX_FORWARDED_RAW_RESULT_BYTES
                )));
            }
        } else {
            match bincode::deserialize::<CommandResult>(&raw_result) {
                Ok(decoded) => decoded,
                Err(e) => {
                    if requires_lossless_forward_result(command) {
                        return Err(RaftError::Raft(format!(
                            "Forwarded {} appears committed at index {} but result decoding failed ({}); \
                             fetch authoritative state from leader before retrying",
                            command_name(command),
                            applied_index,
                            e
                        )));
                    }
                    if let Some(proto_result) = legacy_result.take() {
                        tracing::warn!(
                            command = command_name(command),
                            "Failed to decode raw_result ({}), falling back to legacy result",
                            e
                        );
                        decode_legacy_forwarded_result(command, proto_result)?
                    } else {
                        return Err(RaftError::Serialization(format!(
                            "Failed to deserialize forwarded {} result: {}",
                            command_name(command),
                            e
                        )));
                    }
                }
            }
        }
    } else if let Some(proto_result) = legacy_result.take() {
        decode_legacy_forwarded_result(command, proto_result)?
    } else {
        CommandResult::Success
    };

    validate_forwarded_result(command, &result)?;
    Ok(result)
}

#[cfg(all(feature = "grpc", has_protos))]
async fn probe_leader_raw_result_support(
    api_client: &mut RaftApiClient,
    leader_addr: &NodeAddress,
    zone_id: &str,
    raw_result_support: &tokio::sync::Mutex<Option<(String, bool, std::time::Instant)>>,
) -> crate::raft::Result<bool> {
    use crate::raft::RaftError;
    use std::time::Instant;

    let cached = {
        let guard = raw_result_support.lock().await;
        guard.as_ref().and_then(|(endpoint, supports, checked_at)| {
            if endpoint != &leader_addr.endpoint {
                return None;
            }
            if *supports {
                if checked_at.elapsed() < RAW_RESULT_POSITIVE_CACHE_TTL {
                    return Some(true);
                }
                return None;
            }
            if checked_at.elapsed() < RAW_RESULT_NEGATIVE_CACHE_TTL {
                return Some(false);
            }
            None
        })
    };
    if let Some(supports) = cached {
        return Ok(supports);
    }

    let request = tonic::Request::new(proto::nexus::raft::GetClusterInfoRequest {
        zone_id: zone_id.to_string(),
    });
    let response = api_client
        .inner_mut()
        .get_cluster_info(request)
        .await
        .map_err(|e| RaftError::Transport(e.to_string()))?
        .into_inner();

    let supports = response.supports_raw_result
        && response.protocol_version == FORWARDED_RESULT_PROTOCOL_VERSION;
    if response.supports_raw_result && !supports {
        tracing::warn!(
            leader = %leader_addr.endpoint,
            reported_version = %response.protocol_version,
            expected_version = FORWARDED_RESULT_PROTOCOL_VERSION,
            "Leader reported incompatible forwarded-result protocol version",
        );
    }
    let mut guard = raw_result_support.lock().await;
    *guard = Some((leader_addr.endpoint.clone(), supports, Instant::now()));
    Ok(supports)
}

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
    raw_result_support: &tokio::sync::Mutex<Option<(String, bool, std::time::Instant)>>,
) -> crate::raft::Result<crate::raft::CommandResult> {
    use crate::raft::RaftError;
    use std::time::Instant;

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

    if requires_lossless_forward_result(&command) {
        let supports = probe_leader_raw_result_support(
            &mut api_client,
            leader_addr,
            zone_id,
            raw_result_support,
        )
        .await?;
        if !supports {
            // Keep the warmed client cached even when capability is missing.
            let mut guard = cached_client.lock().await;
            *guard = Some((leader_addr.endpoint.clone(), api_client));
            tracing::warn!(
                leader = %leader_addr.endpoint,
                command = command_name(&command),
                "Leader lacks lossless forwarding capability; redirecting caller to leader",
            );
            return Err(RaftError::NotLeader {
                leader_hint: Some(leader_addr.id),
            });
        }
    }

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

            let response = response.into_inner();
            if response.success {
                if !response.raw_result.is_empty() {
                    let mut capability = raw_result_support.lock().await;
                    *capability = Some((leader_addr.endpoint.clone(), true, Instant::now()));
                } else if requires_lossless_forward_result(&command) {
                    let mut capability = raw_result_support.lock().await;
                    *capability = None;
                    return Err(RaftError::CommittedUnknown {
                        applied_index: response.applied_index,
                        details: format!(
                            "Command {} committed on leader {}, but forwarded response was not lossless. Reconcile state from leader before retrying.",
                            command_name(&command),
                            leader_addr.endpoint
                        ),
                    });
                }
            } else {
                let mut capability = raw_result_support.lock().await;
                if capability
                    .as_ref()
                    .map(|(endpoint, _, _)| endpoint == &leader_addr.endpoint)
                    .unwrap_or(false)
                {
                    *capability = None;
                }
            }

            decode_forwarded_propose_response(&command, response)
        }
        Err(e) => {
            // Transport error — evict cached client (already taken above).
            // Next call will reconnect.
            Err(e)
        }
    }
}

// ---------------------------------------------------------------------------
// Re-export shared transport types from transport.
// These were previously defined locally but are now canonical in transport.
// The entire `transport` module is behind `#[cfg(feature = "grpc")]` in lib.rs,
// so `transport` is always available here.
// ---------------------------------------------------------------------------
#[cfg(feature = "grpc")]
pub use transport::{hostname_to_node_id, NodeAddress, PeerAddress, TlsConfig, TransportError};
#[cfg(feature = "grpc")]
pub type Result<T> = transport::Result<T>;

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
        #[expect(
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

#[cfg(all(test, feature = "grpc", has_protos))]
mod tests {
    use super::*;
    use crate::raft::{Command, CommandResult};
    use proto::nexus::raft::raft_response::Result as ProtoResponseResultVariant;

    #[test]
    fn test_requires_lossless_forward_result_classification() {
        assert!(requires_lossless_forward_result(&Command::AcquireLock {
            path: "/locks/test".to_string(),
            lock_id: "lock-1".to_string(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "holder-a".to_string(),
            now_secs: 1,
        }));
        assert!(requires_lossless_forward_result(&Command::CasSetMetadata {
            key: "/meta".to_string(),
            value: vec![1],
            expected_version: 0,
        }));
        assert!(requires_lossless_forward_result(&Command::AdjustCounter {
            key: "__counter__".to_string(),
            delta: 1,
        }));
        assert!(!requires_lossless_forward_result(&Command::ReleaseLock {
            path: "/locks/test".to_string(),
            lock_id: "lock-1".to_string(),
        }));
    }

    #[test]
    fn test_decode_forwarded_propose_falls_back_to_legacy_on_raw_decode_error() {
        let command = Command::ReleaseLock {
            path: "/locks/test".to_string(),
            lock_id: "lock-1".to_string(),
        };
        let response = proto::nexus::raft::ProposeResponse {
            success: true,
            error: None,
            leader_address: None,
            result: Some(proto::nexus::raft::RaftResponse {
                success: true,
                error: None,
                result: Some(ProtoResponseResultVariant::MetadataResult(
                    proto::nexus::raft::MetadataResult { metadata: None },
                )),
            }),
            applied_index: 0,
            raw_result: vec![0x00, 0xff, 0x01], // invalid bincode payload
        };

        let decoded = decode_forwarded_propose_response(&command, response).unwrap();
        assert!(matches!(decoded, CommandResult::Success));
    }

    #[test]
    fn test_decode_forwarded_propose_prefers_raw_result_when_valid() {
        let command = Command::AdjustCounter {
            key: "__counter__".to_string(),
            delta: 1,
        };
        let expected = CommandResult::Value(42_i64.to_be_bytes().to_vec());
        let raw_result = bincode::serialize(&expected).unwrap();
        let response = proto::nexus::raft::ProposeResponse {
            success: true,
            error: None,
            leader_address: None,
            result: None,
            applied_index: 0,
            raw_result,
        };

        let decoded = decode_forwarded_propose_response(&command, response).unwrap();
        assert!(matches!(decoded, CommandResult::Value(v) if v == 42_i64.to_be_bytes().to_vec()));
    }

    #[test]
    fn test_decode_forwarded_propose_rejects_oversized_raw_result_without_legacy() {
        let command = Command::ReleaseLock {
            path: "/locks/test".to_string(),
            lock_id: "lock-1".to_string(),
        };
        let response = proto::nexus::raft::ProposeResponse {
            success: true,
            error: None,
            leader_address: None,
            result: None,
            applied_index: 0,
            raw_result: vec![0u8; (MAX_FORWARDED_RAW_RESULT_BYTES as usize) + 1],
        };

        let err = decode_forwarded_propose_response(&command, response).unwrap_err();
        assert!(matches!(err, crate::raft::RaftError::Serialization(_)));
    }

    #[test]
    fn test_decode_forwarded_propose_rejects_lossy_fallback_for_lock_results() {
        let command = Command::AcquireLock {
            path: "/locks/test".to_string(),
            lock_id: "lock-1".to_string(),
            max_holders: 2,
            ttl_secs: 30,
            holder_info: "holder-a".to_string(),
            now_secs: 1,
        };
        let response = proto::nexus::raft::ProposeResponse {
            success: true,
            error: None,
            leader_address: None,
            result: Some(proto::nexus::raft::RaftResponse {
                success: true,
                error: None,
                result: Some(ProtoResponseResultVariant::LockResult(
                    proto::nexus::raft::LockResult {
                        acquired: true,
                        current_holder: Some("holder-a".to_string()),
                        expires_at_ms: 1_000,
                    },
                )),
            }),
            applied_index: 0,
            raw_result: vec![0u8; (MAX_FORWARDED_RAW_RESULT_BYTES as usize) + 1],
        };

        let err = decode_forwarded_propose_response(&command, response).unwrap_err();
        assert!(matches!(err, crate::raft::RaftError::Raft(_)));
    }
}
