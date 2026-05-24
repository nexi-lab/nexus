//! Hardening defaults for every tonic gRPC server in nexus.
//!
//! Centralizes the production-sane settings — max-concurrent-streams,
//! HTTP/2 keepalive, TCP keepalive — so individual Server::builder
//! sites in raft / transport don't drift apart. Source values live in
//! `contracts::constants`; this module only translates them into the
//! tonic builder API.

use std::time::Duration;

use contracts::constants::{
    GRPC_HTTP2_KEEPALIVE_INTERVAL_SECS, GRPC_HTTP2_KEEPALIVE_TIMEOUT_SECS,
    GRPC_MAX_CONCURRENT_STREAMS, GRPC_TCP_KEEPALIVE_SECS,
};
use tonic::transport::Server;

/// Apply nexus's standard gRPC server hardening to a fresh
/// `tonic::transport::Server::builder()`. Returns the same builder so
/// callers can chain TLS / extra-services / `.add_service(...)` on top.
///
/// Sets:
///   * `max_concurrent_streams`     — cap per-connection HTTP/2 streams
///   * `http2_keepalive_interval`   — server PING cadence
///   * `http2_keepalive_timeout`    — PING ack deadline
///   * `tcp_keepalive`              — socket-level dead-peer detection
pub fn apply_server_limits(builder: Server) -> Server {
    builder
        .max_concurrent_streams(Some(GRPC_MAX_CONCURRENT_STREAMS))
        .http2_keepalive_interval(Some(Duration::from_secs(
            GRPC_HTTP2_KEEPALIVE_INTERVAL_SECS,
        )))
        .http2_keepalive_timeout(Some(Duration::from_secs(
            GRPC_HTTP2_KEEPALIVE_TIMEOUT_SECS,
        )))
        .tcp_keepalive(Some(Duration::from_secs(GRPC_TCP_KEEPALIVE_SECS)))
}
