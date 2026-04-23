//! Rust gRPC transport for REMOTE profile and remote metastore/backend.
//!
//! Replaces Python `rpc_transport.py` with a pure Rust tonic client.
//! Follows the `federation_client.rs` pattern: shared tokio runtime,
//! per-peer tonic Channel, mTLS support.
//!
//! Issue #1133: Unified gRPC transport.
//! Issue #1202: gRPC for REMOTE profile.

use std::sync::Arc;
use std::time::Duration;

use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

use crate::kernel::vfs_proto;

/// Optional TLS material for the remote connection.
#[derive(Debug, Clone)]
pub(crate) struct TlsConfig {
    pub ca_pem: Vec<u8>,
    pub cert_pem: Option<Vec<u8>>,
    pub key_pem: Option<Vec<u8>>,
}

/// Tonic gRPC transport — single channel to a remote Nexus server.
///
/// Holds a shared tokio runtime and a pre-connected tonic Channel.
/// All RPC methods block on the runtime (GIL released by callers).
pub(crate) struct RpcTransport {
    runtime: Arc<tokio::runtime::Runtime>,
    channel: Channel,
    auth_token: String,
    #[allow(dead_code)]
    timeout: Duration,
}

impl RpcTransport {
    /// Connect to a remote server. `address` is `host:port` or `http(s)://host:port`.
    /// Channel is lazy — actual TCP connection happens on first RPC call.
    pub fn new(
        runtime: Arc<tokio::runtime::Runtime>,
        address: &str,
        auth_token: &str,
        tls: Option<&TlsConfig>,
        timeout: Duration,
    ) -> Result<Self, String> {
        let channel = Self::build_channel(address, tls, timeout)?;

        Ok(Self {
            runtime,
            channel,
            auth_token: auth_token.to_string(),
            timeout,
        })
    }

    /// Shared runtime accessor — other modules that share the transport
    /// can spawn work on the same runtime.
    #[allow(dead_code)]
    pub fn runtime(&self) -> &Arc<tokio::runtime::Runtime> {
        &self.runtime
    }

    fn build_channel(
        address: &str,
        tls: Option<&TlsConfig>,
        timeout: Duration,
    ) -> Result<Channel, String> {
        let scheme = if tls.is_some() { "https" } else { "http" };
        let endpoint_str = if address.starts_with("http://") || address.starts_with("https://") {
            address.to_string()
        } else {
            format!("{scheme}://{address}")
        };

        let mut ep = Endpoint::from_shared(endpoint_str)
            .map_err(|e| format!("invalid endpoint '{address}': {e}"))?
            .connect_timeout(Duration::from_secs(10))
            .timeout(timeout);

        if let Some(tls_cfg) = tls {
            let ca = Certificate::from_pem(&tls_cfg.ca_pem);
            let mut client_tls = ClientTlsConfig::new().ca_certificate(ca);
            if let (Some(cert), Some(key)) = (&tls_cfg.cert_pem, &tls_cfg.key_pem) {
                let identity = Identity::from_pem(cert, key);
                client_tls = client_tls.identity(identity);
            }
            ep = ep
                .tls_config(client_tls)
                .map_err(|e| format!("TLS config for '{address}': {e}"))?;
        }

        // Lazy connection: channel is established on first RPC call, not here.
        // This avoids blocking during NexusFS construction and allows tests to
        // create REMOTE profile NexusFS instances without a running server.
        Ok(ep.connect_lazy())
    }

    fn client(&self) -> vfs_proto::nexus_vfs_service_client::NexusVfsServiceClient<Channel> {
        vfs_proto::nexus_vfs_service_client::NexusVfsServiceClient::new(self.channel.clone())
    }

    // ── RPC methods (blocking — callers release GIL before calling) ──

    /// Generic Call RPC — method name + JSON payload.
    pub fn call(&self, method: &str, payload: &[u8]) -> Result<(Vec<u8>, bool), String> {
        self.runtime.block_on(self.call_async(method, payload))
    }

    async fn call_async(&self, method: &str, payload: &[u8]) -> Result<(Vec<u8>, bool), String> {
        let mut client = self.client();
        let mut retries = 0u8;
        loop {
            let req = tonic::Request::new(vfs_proto::CallRequest {
                method: method.to_string(),
                payload: payload.to_vec(),
                auth_token: self.auth_token.clone(),
            });
            match client.call(req).await {
                Ok(resp) => {
                    let inner = resp.into_inner();
                    return Ok((inner.payload.to_vec(), inner.is_error));
                }
                Err(status) if retries < 2 && is_retryable(&status) => {
                    retries += 1;
                    let delay = Duration::from_millis(100 * (1 << retries));
                    tokio::time::sleep(delay).await;
                }
                Err(status) => {
                    return Err(format!("Call({method}): {status}"));
                }
            }
        }
    }

    /// Typed Read RPC — raw bytes, no base64.
    pub fn read(&self, path: &str, content_id: &str) -> Result<ReadResult, String> {
        self.runtime.block_on(self.read_async(path, content_id))
    }

    async fn read_async(&self, path: &str, content_id: &str) -> Result<ReadResult, String> {
        let mut client = self.client();
        let mut retries = 0u8;
        loop {
            let req = tonic::Request::new(vfs_proto::ReadRequest {
                path: path.to_string(),
                auth_token: self.auth_token.clone(),
                content_id: content_id.to_string(),
            });
            match client.read(req).await {
                Ok(resp) => {
                    let inner = resp.into_inner();
                    if inner.is_error {
                        let err = String::from_utf8_lossy(&inner.error_payload);
                        return Err(format!("Read({path}): server error: {err}"));
                    }
                    return Ok(ReadResult {
                        content: inner.content.to_vec(),
                        etag: inner.etag,
                        size: inner.size as u64,
                    });
                }
                Err(status) if retries < 2 && is_retryable(&status) => {
                    retries += 1;
                    let delay = Duration::from_millis(100 * (1 << retries));
                    tokio::time::sleep(delay).await;
                }
                Err(status) => {
                    return Err(format!("Read({path}): {status}"));
                }
            }
        }
    }

    /// Typed Write RPC — raw bytes, returns (etag, size).
    pub fn write(&self, path: &str, content: &[u8], etag: &str) -> Result<WriteRpcResult, String> {
        self.runtime.block_on(self.write_async(path, content, etag))
    }

    async fn write_async(
        &self,
        path: &str,
        content: &[u8],
        etag: &str,
    ) -> Result<WriteRpcResult, String> {
        let mut client = self.client();
        let mut retries = 0u8;
        loop {
            let req = tonic::Request::new(vfs_proto::WriteRequest {
                path: path.to_string(),
                content: content.to_vec(),
                auth_token: self.auth_token.clone(),
                etag: etag.to_string(),
            });
            match client.write(req).await {
                Ok(resp) => {
                    let inner = resp.into_inner();
                    if inner.is_error {
                        let err = String::from_utf8_lossy(&inner.error_payload);
                        return Err(format!("Write({path}): server error: {err}"));
                    }
                    return Ok(WriteRpcResult {
                        etag: inner.etag,
                        size: inner.size as u64,
                    });
                }
                Err(status) if retries < 2 && is_retryable(&status) => {
                    retries += 1;
                    let delay = Duration::from_millis(100 * (1 << retries));
                    tokio::time::sleep(delay).await;
                }
                Err(status) => {
                    return Err(format!("Write({path}): {status}"));
                }
            }
        }
    }

    /// Typed Delete RPC.
    pub fn delete(&self, path: &str, recursive: bool) -> Result<bool, String> {
        self.runtime.block_on(self.delete_async(path, recursive))
    }

    async fn delete_async(&self, path: &str, recursive: bool) -> Result<bool, String> {
        let mut client = self.client();
        let req = tonic::Request::new(vfs_proto::DeleteRequest {
            path: path.to_string(),
            auth_token: self.auth_token.clone(),
            recursive,
        });
        let resp = client
            .delete(req)
            .await
            .map_err(|e| format!("Delete({path}): {e}"))?
            .into_inner();
        if resp.is_error {
            let err = String::from_utf8_lossy(&resp.error_payload);
            return Err(format!("Delete({path}): server error: {err}"));
        }
        Ok(resp.success)
    }

    /// Health check — returns (version, zone_id, uptime_seconds).
    #[allow(dead_code)]
    pub fn ping(&self) -> Result<(String, String, i64), String> {
        self.runtime.block_on(self.ping_async())
    }

    #[allow(dead_code)]
    async fn ping_async(&self) -> Result<(String, String, i64), String> {
        let mut client = self.client();
        let req = tonic::Request::new(vfs_proto::PingRequest {
            auth_token: self.auth_token.clone(),
        });
        let resp = client
            .ping(req)
            .await
            .map_err(|e| format!("Ping: {e}"))?
            .into_inner();
        Ok((resp.version, resp.zone_id, resp.uptime_seconds))
    }
}

/// Result of a typed Read RPC.
#[allow(dead_code)]
pub(crate) struct ReadResult {
    pub content: Vec<u8>,
    pub etag: String,
    pub size: u64,
}

/// Result of a typed Write RPC.
pub(crate) struct WriteRpcResult {
    pub etag: String,
    pub size: u64,
}

/// Retry only on transient gRPC failures (UNAVAILABLE, DEADLINE_EXCEEDED).
fn is_retryable(status: &tonic::Status) -> bool {
    matches!(
        status.code(),
        tonic::Code::Unavailable | tonic::Code::DeadlineExceeded
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_retryable_covers_expected_codes() {
        assert!(is_retryable(&tonic::Status::unavailable("down")));
        assert!(is_retryable(&tonic::Status::deadline_exceeded("slow")));
        assert!(!is_retryable(&tonic::Status::not_found("gone")));
        assert!(!is_retryable(&tonic::Status::permission_denied("nope")));
    }
}
