//! Transport configuration types.

use std::time::Duration;

/// TLS configuration for gRPC transport (mTLS).
///
/// All fields are PEM-encoded bytes (read from files by the caller).
/// Rust core holds bytes, not paths — file I/O happens at the boundary
/// (PyO3 reads files, CLI reads files). This makes the core testable
/// with in-memory certs.
#[derive(Debug, Clone)]
pub struct TlsConfig {
    /// Server/client certificate (PEM).
    pub cert_pem: Vec<u8>,
    /// Private key (PEM).
    pub key_pem: Vec<u8>,
    /// CA certificate for verifying the peer (PEM).
    pub ca_pem: Vec<u8>,
}

/// Client-side connection configuration.
#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// Connection timeout.
    pub connect_timeout: Duration,
    /// Per-request timeout.
    pub request_timeout: Duration,
    /// TCP keepalive interval.
    pub tcp_keepalive: Option<Duration>,
    /// HTTP/2 keepalive interval.
    pub http2_keepalive_interval: Option<Duration>,
    /// HTTP/2 keepalive timeout.
    pub http2_keepalive_timeout: Option<Duration>,
    /// TLS config (None = plaintext).
    pub tls: Option<TlsConfig>,
}

impl Default for ClientConfig {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(10),
            tcp_keepalive: Some(Duration::from_secs(30)),
            http2_keepalive_interval: Some(Duration::from_secs(20)),
            http2_keepalive_timeout: Some(Duration::from_secs(10)),
            tls: None,
        }
    }
}

/// Server-side configuration.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Bind address (e.g. "0.0.0.0:2126").
    pub bind_address: String,
    /// Maximum concurrent connections.
    pub max_connections: Option<usize>,
    /// Maximum message size in bytes (default 64MB).
    pub max_message_size: usize,
    /// TLS config (None = plaintext).
    pub tls: Option<TlsConfig>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind_address: "0.0.0.0:2126".to_string(),
            max_connections: None,
            max_message_size: 64 * 1024 * 1024, // 64MB
            tls: None,
        }
    }
}
