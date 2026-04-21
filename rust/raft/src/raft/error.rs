//! Error types for Raft consensus module.

use thiserror::Error;

/// Raft-specific errors.
#[derive(Debug, Error)]
pub enum RaftError {
    /// Storage operation failed.
    #[error("storage error: {0}")]
    Storage(String),

    /// Raft protocol error.
    #[error("raft error: {0}")]
    Raft(String),

    /// Node is not the leader.
    #[error("not leader, leader hint: {leader_hint:?}")]
    NotLeader {
        /// Hint about who the leader might be.
        leader_hint: Option<u64>,
    },

    /// Proposal was dropped (e.g., leader changed).
    #[error("proposal dropped")]
    ProposalDropped,

    /// Proposal timed out waiting for consensus.
    #[error("proposal timed out after {0} seconds")]
    Timeout(u64),

    /// Configuration error.
    #[error("config error: {0}")]
    Config(String),

    /// Serialization/deserialization error.
    #[error("serialization error: {0}")]
    Serialization(String),

    /// Node not initialized.
    #[error("node not initialized")]
    NotInitialized,

    /// Invalid state transition.
    #[error("invalid state: {0}")]
    InvalidState(String),

    /// The actor channel was closed (driver dropped).
    #[error("raft actor channel closed")]
    ChannelClosed,

    /// The actor channel is full — backpressure.
    #[error("raft actor channel full (capacity {0}), driver overloaded")]
    ChannelFull(usize),

    /// Transport error (gRPC forwarding failed).
    #[error("transport error: {0}")]
    Transport(String),

    /// Command was committed but caller did not receive a decodable result.
    #[error("command committed at index {applied_index} but result is undecodable: {details}")]
    CommittedUnknown {
        /// Applied log index reported by the leader.
        applied_index: u64,
        /// Human-readable reconciliation guidance.
        details: String,
    },
}

impl From<crate::storage::StorageError> for RaftError {
    fn from(e: crate::storage::StorageError) -> Self {
        RaftError::Storage(e.to_string())
    }
}

impl From<bincode::Error> for RaftError {
    fn from(e: bincode::Error) -> Self {
        RaftError::Serialization(e.to_string())
    }
}

/// Result type for Raft operations.
pub type Result<T> = std::result::Result<T, RaftError>;
