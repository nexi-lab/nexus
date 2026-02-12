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
}

impl From<crate::storage::StorageError> for RaftError {
    fn from(e: crate::storage::StorageError) -> Self {
        RaftError::Storage(e.to_string())
    }
}

impl From<crate::storage::RedbStorageError> for RaftError {
    fn from(e: crate::storage::RedbStorageError) -> Self {
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
