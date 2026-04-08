//! Transport error types.

/// Transport-layer error.
#[derive(Debug, thiserror::Error)]
#[allow(clippy::result_large_err)]
pub enum TransportError {
    /// Connection failed.
    #[error("connection error: {0}")]
    Connection(String),

    /// RPC call failed.
    #[error("rpc error: {0}")]
    Rpc(String),

    /// Invalid address.
    #[error("invalid address: {0}")]
    InvalidAddress(String),

    /// Timeout.
    #[error("timeout after {0:?}")]
    Timeout(std::time::Duration),

    /// Server not running.
    #[error("server not running")]
    ServerNotRunning,

    /// Tonic transport error.
    #[error("tonic error: {0}")]
    Tonic(#[from] tonic::transport::Error),

    /// Tonic status error.
    #[error("status: {0}")]
    Status(#[from] tonic::Status),
}

pub type Result<T> = std::result::Result<T, TransportError>;
