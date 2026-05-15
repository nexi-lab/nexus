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

    /// The caller is not authenticated or the credentials are invalid
    /// (HTTP 401 or JSON-RPC `-32003` `ACCESS_DENIED`). Mapped to
    /// `EACCES` so FUSE clients see "permission denied" rather than
    /// generic I/O failure. Distinct from `PermissionDenied` because
    /// "no/invalid credentials" is recoverable by re-authenticating,
    /// while "valid credentials, denied by ReBAC" is not.
    #[error("Access denied: {0}")]
    AccessDenied(String),

    /// The caller is authenticated but the server's policy denied the
    /// operation (HTTP 403 or JSON-RPC `-32004` `PERMISSION_ERROR`).
    /// Mapped to `EPERM`. Surfaces as "operation not permitted" to
    /// FUSE callers — different remediation than `AccessDenied`
    /// (which is auth-layer), so retry / surface logic stays correct.
    #[error("Permission denied: {0}")]
    PermissionDenied(String),

    /// Target already exists (JSON-RPC `-32001` `FILE_EXISTS`). Mapped
    /// to `EEXIST` so FUSE `mkdir` / `create` surface the standard
    /// errno rather than EPROTO (#4056 R4).
    #[error("File exists: {0}")]
    AlreadyExists(String),

    /// Invalid path (JSON-RPC `-32002` `INVALID_PATH`). Mapped to
    /// `EINVAL` (#4056 R4).
    #[error("Invalid path: {0}")]
    InvalidPath(String),

    /// Validation failure on the request (JSON-RPC `-32005`
    /// `VALIDATION_ERROR`). Mapped to `EINVAL` (#4056 R4).
    #[error("Validation error: {0}")]
    ValidationError(String),

    /// Optimistic-concurrency conflict (JSON-RPC `-32006` `CONFLICT`).
    /// Mapped to `EAGAIN` — the caller can retry with a fresh
    /// generation number (#4056 R4).
    #[error("Conflict: {0}")]
    Conflict(String),

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

    /// Server explicitly declared a filesystem capability unsupported.
    #[error("Unsupported capability {capability} for {path}")]
    UnsupportedCapability { capability: String, path: String },

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
            Self::AccessDenied(_) => libc::EACCES,
            Self::PermissionDenied(_) => libc::EPERM,
            Self::AlreadyExists(_) => libc::EEXIST,
            Self::InvalidPath(_) | Self::ValidationError(_) => libc::EINVAL,
            Self::Conflict(_) => libc::EAGAIN,
            Self::Timeout { .. } => libc::ETIMEDOUT,
            Self::ConnectionRefused(_) => libc::ECONNREFUSED,
            Self::RateLimited => libc::EBUSY,
            Self::UnsupportedCapability { .. } => libc::EOPNOTSUPP,
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
            Self::Timeout { .. } | Self::ConnectionRefused(_) | Self::RateLimited => true,
            Self::HttpError(e) => e.is_timeout() || e.is_connect(),
            Self::ServerError { status, .. } => (500..600).contains(status),
            Self::Conflict(_) => true, // optimistic-concurrency retry
            Self::NotFound(_)
            | Self::AccessDenied(_)
            | Self::PermissionDenied(_)
            | Self::AlreadyExists(_)
            | Self::InvalidPath(_)
            | Self::ValidationError(_)
            | Self::InvalidResponse(_)
            | Self::UnsupportedCapability { .. }
            | Self::JsonError(_)
            | Self::Base64Error(_)
            | Self::Other(_) => false,
        }
    }

    /// Check if error indicates resource not found.
    pub fn is_not_found(&self) -> bool {
        matches!(self, Self::NotFound(_))
    }

    /// Check if error indicates the caller lacks authorization
    /// (either unauthenticated/credentials-rejected `AccessDenied`
    /// or authenticated-but-policy-denied `PermissionDenied`). FUSE
    /// callers use this to short-circuit retry/cache-probing logic
    /// that only makes sense for transient backend trouble.
    pub fn is_permission_denied(&self) -> bool {
        matches!(self, Self::AccessDenied(_) | Self::PermissionDenied(_))
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
    fn test_client_error_server_response_is_not_transient() {
        let err = NexusClientError::ServerError {
            status: 403,
            message: "Forbidden".to_string(),
        };
        assert_eq!(err.to_errno(), libc::EIO);
        assert!(!err.is_transient());
    }

    #[test]
    fn test_invalid_response_maps_to_eproto() {
        let err = NexusClientError::InvalidResponse("bad json".to_string());
        assert_eq!(err.to_errno(), libc::EPROTO);
        assert!(!err.is_transient());
    }
}
