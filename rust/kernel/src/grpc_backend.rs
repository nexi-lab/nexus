//! gRPC-based ObjectStore adapter — zero GIL during remote I/O.
//!
//! Implements the `ObjectStore` trait via tonic gRPC client.
//! Connects to a Python `ConnectorGrpcServer` that wraps any
//! `ObjectStoreABC` backend (GDrive, Gmail, Slack, S3, GCS).
//!
//! Benefit: GIL freed during remote I/O. Other syscalls proceed
//! without contention. Network latency dominates anyway.
//!
//! Issue #1868: Phase 11 — Direction 3 gRPC adapter.

use std::io;
use std::time::Duration;

use parking_lot::Mutex;

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;

/// Per-request deadline. Applied on every tonic request so a stuck sidecar
/// cannot hang the kernel indefinitely (§ review fix #9).
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

/// Generated protobuf + gRPC client stubs.
pub(crate) mod proto {
    tonic::include_proto!("nexus.storage");
}

use proto::object_store_service_client::ObjectStoreServiceClient;

/// gRPC-based ObjectStore — Rust kernel makes gRPC calls, zero GIL.
///
/// Holds a lazily-connected gRPC channel to a Python sidecar server.
/// Each method reuses the same channel — tonic multiplexes requests over
/// HTTP/2 streams, so cloning is cheap. Previous behaviour created a new
/// channel per call, paying a full HTTP/2 handshake each time.
#[allow(dead_code)]
pub(crate) struct GrpcObjectStoreAdapter {
    /// gRPC endpoint (e.g. "http://127.0.0.1:50051")
    endpoint: String,
    /// Backend name returned by name()
    name: String,
    /// Shared tokio runtime for blocking gRPC calls
    runtime: tokio::runtime::Runtime,
    /// Lazily-established channel. Wrapped in `Mutex<Option<_>>` so we can
    /// drop and reconnect if a call fails with a transport error (e.g.
    /// sidecar restart) without taking the slow path on every request.
    channel: Mutex<Option<tonic::transport::Channel>>,
}

impl GrpcObjectStoreAdapter {
    /// Create a new gRPC adapter for the given endpoint. The underlying
    /// channel is connected lazily on first use.
    pub fn new(endpoint: &str, name: &str) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| io::Error::other(format!("tokio runtime: {e}")))?;
        Ok(Self {
            endpoint: endpoint.to_string(),
            name: name.to_string(),
            runtime,
            channel: Mutex::new(None),
        })
    }

    /// Return a cloned, connected gRPC channel. Connects once and reuses
    /// for subsequent calls.
    fn channel(&self) -> Result<tonic::transport::Channel, StorageError> {
        if let Some(ch) = self.channel.lock().as_ref() {
            return Ok(ch.clone());
        }
        // Slow path — connect and cache. We must not hold the mutex across
        // the await, so build outside the lock and install under it.
        let endpoint = self.endpoint.clone();
        let ch = self.runtime.block_on(async move {
            let ep = tonic::transport::Endpoint::from_shared(endpoint)
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("gRPC endpoint: {e}")))
                })?
                .timeout(REQUEST_TIMEOUT)
                .connect_timeout(Duration::from_secs(10))
                .tcp_keepalive(Some(Duration::from_secs(30)));
            ep.connect()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC connect: {e}"))))
        })?;
        *self.channel.lock() = Some(ch.clone());
        Ok(ch)
    }

    /// Get a connected gRPC client (blocking). Reuses the cached channel.
    fn client(&self) -> Result<ObjectStoreServiceClient<tonic::transport::Channel>, StorageError> {
        Ok(ObjectStoreServiceClient::new(self.channel()?))
    }

    /// Build a tonic request with the standard per-request timeout.
    fn request<T>(&self, msg: T) -> tonic::Request<T> {
        let mut req = tonic::Request::new(msg);
        req.set_timeout(REQUEST_TIMEOUT);
        req
    }

    /// Classify transport-level errors so we can drop the cached channel and
    /// reconnect on next call (e.g. after the sidecar restarts).
    fn reset_channel_on_transport_error(&self, status: &tonic::Status) {
        match status.code() {
            tonic::Code::Unavailable | tonic::Code::Unknown | tonic::Code::Internal => {
                *self.channel.lock() = None;
            }
            _ => {}
        }
    }
}

impl ObjectStore for GrpcObjectStoreAdapter {
    fn name(&self) -> &str {
        &self.name
    }

    fn read_content(
        &self,
        content_id: &str,
        backend_path: &str,
        ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::ReadContentRequest {
            content_id: content_id.to_string(),
            backend_path: backend_path.to_string(),
            user_id: ctx.user_id.clone(),
            zone_id: ctx.zone_id.clone(),
            is_admin: ctx.is_admin,
        });
        let response = self
            .runtime
            .block_on(client.read_content(request))
            .map_err(|e| {
                self.reset_channel_on_transport_error(&e);
                if e.code() == tonic::Code::NotFound {
                    StorageError::NotFound(backend_path.to_string())
                } else {
                    StorageError::IOError(io::Error::other(format!("gRPC read: {e}")))
                }
            })?;
        Ok(response.into_inner().data)
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        ctx: &OperationContext,
    ) -> Result<WriteResult, StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::WriteContentRequest {
            content: content.to_vec(),
            content_id: content_id.to_string(),
            user_id: ctx.user_id.clone(),
            zone_id: ctx.zone_id.clone(),
            is_admin: ctx.is_admin,
        });
        let response = self
            .runtime
            .block_on(client.write_content(request))
            .map_err(|e| {
                self.reset_channel_on_transport_error(&e);
                StorageError::IOError(io::Error::other(format!("gRPC write: {e}")))
            })?;
        let resp = response.into_inner();
        Ok(WriteResult {
            content_id: resp.content_id,
            version: resp.version,
            size: resp.size,
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::DeleteFileRequest {
            path: path.to_string(),
        });
        self.runtime
            .block_on(client.delete_file(request))
            .map_err(|e| {
                self.reset_channel_on_transport_error(&e);
                StorageError::IOError(io::Error::other(format!("gRPC delete: {e}")))
            })?;
        Ok(())
    }

    fn mkdir(&self, path: &str, parents: bool, exist_ok: bool) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::MkdirRequest {
            path: path.to_string(),
            parents,
            exist_ok,
        });
        self.runtime.block_on(client.mkdir(request)).map_err(|e| {
            self.reset_channel_on_transport_error(&e);
            StorageError::IOError(io::Error::other(format!("gRPC mkdir: {e}")))
        })?;
        Ok(())
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::RmdirRequest {
            path: path.to_string(),
            recursive,
        });
        self.runtime.block_on(client.rmdir(request)).map_err(|e| {
            self.reset_channel_on_transport_error(&e);
            StorageError::IOError(io::Error::other(format!("gRPC rmdir: {e}")))
        })?;
        Ok(())
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = self.request(proto::RenameRequest {
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
        });
        self.runtime.block_on(client.rename(request)).map_err(|e| {
            self.reset_channel_on_transport_error(&e);
            StorageError::IOError(io::Error::other(format!("gRPC rename: {e}")))
        })?;
        Ok(())
    }
}
