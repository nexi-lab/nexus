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

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;

/// Generated protobuf + gRPC client stubs.
pub(crate) mod proto {
    tonic::include_proto!("nexus.storage");
}

use proto::object_store_service_client::ObjectStoreServiceClient;

/// gRPC-based ObjectStore — Rust kernel makes gRPC calls, zero GIL.
///
/// Holds a blocking gRPC channel to a Python sidecar server.
/// Each method creates a one-shot tokio runtime for the async gRPC call.
#[allow(dead_code)]
pub(crate) struct GrpcObjectStoreAdapter {
    /// gRPC endpoint (e.g. "http://127.0.0.1:50051")
    endpoint: String,
    /// Backend name returned by name()
    name: String,
    /// Shared tokio runtime for blocking gRPC calls
    runtime: tokio::runtime::Runtime,
}

impl GrpcObjectStoreAdapter {
    /// Create a new gRPC adapter connecting to the given endpoint.
    pub fn new(endpoint: &str, name: &str) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| io::Error::other(format!("tokio runtime: {e}")))?;
        Ok(Self {
            endpoint: endpoint.to_string(),
            name: name.to_string(),
            runtime,
        })
    }

    /// Get a connected gRPC client (blocking).
    fn client(&self) -> Result<ObjectStoreServiceClient<tonic::transport::Channel>, StorageError> {
        self.runtime.block_on(async {
            ObjectStoreServiceClient::connect(self.endpoint.clone())
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC connect: {e}"))))
        })
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
        let request = tonic::Request::new(proto::ReadContentRequest {
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
        let request = tonic::Request::new(proto::WriteContentRequest {
            content: content.to_vec(),
            content_id: content_id.to_string(),
            user_id: ctx.user_id.clone(),
            zone_id: ctx.zone_id.clone(),
            is_admin: ctx.is_admin,
        });
        let response = self
            .runtime
            .block_on(client.write_content(request))
            .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC write: {e}"))))?;
        let resp = response.into_inner();
        Ok(WriteResult {
            content_id: resp.content_id,
            version: resp.version,
            size: resp.size,
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = tonic::Request::new(proto::DeleteFileRequest {
            path: path.to_string(),
        });
        self.runtime
            .block_on(client.delete_file(request))
            .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC delete: {e}"))))?;
        Ok(())
    }

    fn mkdir(&self, path: &str, parents: bool, exist_ok: bool) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = tonic::Request::new(proto::MkdirRequest {
            path: path.to_string(),
            parents,
            exist_ok,
        });
        self.runtime
            .block_on(client.mkdir(request))
            .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC mkdir: {e}"))))?;
        Ok(())
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = tonic::Request::new(proto::RmdirRequest {
            path: path.to_string(),
            recursive,
        });
        self.runtime
            .block_on(client.rmdir(request))
            .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC rmdir: {e}"))))?;
        Ok(())
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let mut client = self.client()?;
        let request = tonic::Request::new(proto::RenameRequest {
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
        });
        self.runtime
            .block_on(client.rename(request))
            .map_err(|e| StorageError::IOError(io::Error::other(format!("gRPC rename: {e}"))))?;
        Ok(())
    }
}
