//! Remote ObjectStore — `ObjectStore` trait impl via tonic gRPC.
//!
//! Replaces Python `backends/storage/remote.py`. Content ops use typed
//! Read/Write/Delete RPCs (raw bytes, no base64). Directory ops and
//! stat fall back to the generic Call RPC.
//!
//! Issue #1134: Rust-first connector routing + REMOTE profile.

use std::sync::Arc;

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::rpc_transport::RpcTransport;

/// ObjectStore backed by a remote Nexus server via gRPC.
///
/// Server-side NexusFS is the SSOT — this backend is a thin proxy.
/// Path resolution: `backend_path` from kernel routing is the
/// server-relative path; for REMOTE profile the root mount maps
/// backend_path == user_path.
pub(crate) struct RemoteBackend {
    transport: Arc<RpcTransport>,
}

impl RemoteBackend {
    pub fn new(transport: Arc<RpcTransport>) -> Self {
        Self { transport }
    }
}

/// Derive server path from the kernel-provided `backend_path`.
///
/// For REMOTE profile root mount, backend_path == user_path (e.g.
/// "/docs/readme.md"). Ensure leading slash.
fn to_server_path(backend_path: &str) -> String {
    if backend_path.is_empty() {
        "/".to_string()
    } else if backend_path.starts_with('/') {
        backend_path.to_string()
    } else {
        format!("/{backend_path}")
    }
}

impl ObjectStore for RemoteBackend {
    fn name(&self) -> &str {
        "remote"
    }

    fn read_content(
        &self,
        content_id: &str,
        backend_path: &str,
        _ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let path = to_server_path(backend_path);
        self.transport
            .read(&path, content_id)
            .map(|r| r.content)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let path = to_server_path(content_id);
        let result = self
            .transport
            .write(&path, content, "")
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        Ok(WriteResult {
            content_id: result.etag.clone(),
            version: result.etag,
            size: result.size,
        })
    }

    fn delete_content(&self, _content_id: &str) -> Result<(), StorageError> {
        // Server handles content deletion via metastore delete (sys_unlink).
        // Content deletion by hash is not meaningful for remote backends.
        Ok(())
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        // Use sys_stat to get size — content_id is the path for remote.
        let payload = serde_json::json!({ "path": content_id });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (resp, is_error) = self
            .transport
            .call("sys_stat", &bytes)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        if is_error {
            return Err(StorageError::NotFound(content_id.to_string()));
        }
        let value: serde_json::Value = serde_json::from_slice(&resp)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        value
            .get("size")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }

    fn mkdir(&self, path: &str, parents: bool, exist_ok: bool) -> Result<(), StorageError> {
        let payload = serde_json::json!({
            "path": path,
            "parents": parents,
            "exist_ok": exist_ok,
        });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (_resp, is_error) = self
            .transport
            .call("mkdir", &bytes)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        if is_error {
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "mkdir failed: {path}"
            ))));
        }
        Ok(())
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let payload = serde_json::json!({
            "path": path,
            "recursive": recursive,
        });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (_resp, is_error) = self
            .transport
            .call("sys_rmdir", &bytes)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        if is_error {
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "rmdir failed: {path}"
            ))));
        }
        Ok(())
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.transport
            .delete(path, false)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        Ok(())
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        let payload = serde_json::json!({
            "old_path": old_path,
            "new_path": new_path,
        });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (_resp, is_error) = self
            .transport
            .call("sys_rename", &bytes)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        if is_error {
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "rename failed: {old_path} -> {new_path}"
            ))));
        }
        Ok(())
    }
}
