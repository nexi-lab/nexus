//! Error types for Nexus FUSE client.
//!
//! Provides typed errors that map to FUSE errno codes for correct application-level
//! retry logic (e.g., ETIMEDOUT is retry-able, ENOENT is not).

use std::time::Duration;
use thiserror::Error;

/// Nexus client errors with FUSE errno mapping.
#[derive(Debug, Error)]
pub enum NexusClientError {
    /// File or directory not found (HTTP 404 or "not found" in message).
    #[error("Not found: {0}")]
    NotFound(String),

    /// Network timeout occurred.
    #[error("Network timeout after {duration:?}")]
    Timeout {
        duration: Duration,
        #[source]
        source: reqwest::Error,
    },

    /// Connection refused (server not reachable).
    #[error("Connection refused: {0}")]
    ConnectionRefused(String),

    /// Rate limited by server (HTTP 429).
    #[error("Rate limited (HTTP 429)")]
    RateLimited,

    /// Server error (HTTP 5xx).
    #[error("Server error (HTTP {status}): {message}")]
    ServerError { status: u16, message: String },

    /// Invalid or malformed response from server.
    #[error("Invalid response: {0}")]
    InvalidResponse(String),

    /// HTTP client error.
    #[error("HTTP client error: {0}")]
    HttpError(#[from] reqwest::Error),

    /// JSON parsing error.
    #[error("JSON parse error: {0}")]
    JsonError(#[from] serde_json::Error),

    /// Base64 decode error.
    #[error("Base64 decode error: {0}")]
    Base64Error(#[from] base64::DecodeError),

    /// Other errors not classified above.
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl NexusClientError {
    /// Map error to FUSE errno code.
    ///
    /// This enables correct application-level error handling:
    /// - ENOENT: File not found (don't retry)
    /// - ETIMEDOUT: Network timeout (retry with backoff)
    /// - ECONNREFUSED: Server down (retry later)
    /// - EBUSY: Rate limited (retry with delay)
    /// - EIO: Server error or unknown error (retry cautiously)
    /// - EPROTO: Invalid response format (don't retry, likely bug)
    pub fn to_errno(&self) -> i32 {
        match self {
            Self::NotFound(_) => libc::ENOENT,
            Self::Timeout { .. } => libc::ETIMEDOUT,
            Self::ConnectionRefused(_) => libc::ECONNREFUSED,
            Self::RateLimited => libc::EBUSY,
            Self::ServerError { status, .. } => {
                // Map server errors to EIO
                // 5xx = server error (transient), 4xx = client error (may not be transient)
                if (500..600).contains(status) {
                    libc::EIO
                } else {
                    // 4xx client errors - map to generic EIO for now
                    // Could be refined later (e.g., 401 -> EACCES, 403 -> EPERM)
                    libc::EIO
                }
            }
            Self::InvalidResponse(_) | Self::JsonError(_) | Self::Base64Error(_) => libc::EPROTO,
            Self::HttpError(e) => {
                // Classify reqwest errors
                if e.is_timeout() {
                    libc::ETIMEDOUT
                } else if e.is_connect() {
                    libc::ECONNREFUSED
                } else {
                    libc::EIO
                }
            }
            Self::Other(_) => libc::EIO,
        }
    }

    /// Check if error is transient and potentially retry-able.
    pub fn is_transient(&self) -> bool {
        match self {
            Self::Timeout { .. }
            | Self::ConnectionRefused(_)
            | Self::RateLimited
            | Self::ServerError { .. }
            | Self::HttpError(_) => true,
            Self::NotFound(_)
            | Self::InvalidResponse(_)
            | Self::JsonError(_)
            | Self::Base64Error(_)
            | Self::Other(_) => false,
        }
    }

    /// Check if error indicates resource not found.
    pub fn is_not_found(&self) -> bool {
        matches!(self, Self::NotFound(_))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_not_found_maps_to_enoent() {
        let err = NexusClientError::NotFound("/path".to_string());
        assert_eq!(err.to_errno(), libc::ENOENT);
        assert!(!err.is_transient());
        assert!(err.is_not_found());
    }

    #[test]
    fn test_rate_limited_maps_to_ebusy() {
        let err = NexusClientError::RateLimited;
        assert_eq!(err.to_errno(), libc::EBUSY);
        assert!(err.is_transient());
    }

    #[test]
    fn test_server_error_maps_to_eio() {
        let err = NexusClientError::ServerError {
            status: 500,
            message: "Internal Server Error".to_string(),
        };
        assert_eq!(err.to_errno(), libc::EIO);
        assert!(err.is_transient());
    }

    #[test]
    fn test_invalid_response_maps_to_eproto() {
        let err = NexusClientError::InvalidResponse("bad json".to_string());
        assert_eq!(err.to_errno(), libc::EPROTO);
        assert!(!err.is_transient());
    }
}
