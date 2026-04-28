//! Remote ObjectStore — `ObjectStore` trait impl via tonic gRPC.
//!
//! Replaces Python `backends/storage/remote.py`. Content ops use typed
//! Read/Write/Delete RPCs (raw bytes, no base64). Directory ops and
//! stat fall back to the generic Call RPC.
//!
//! Issue #1134: Rust-first connector routing + REMOTE profile.
//! Issue #3786: read_content uses Call RPC (not native Read) so hub's
//!              Python dispatch builds a full zone_perms context.

use std::sync::Arc;

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::rpc_transport::RpcTransport;

/// ObjectStore backed by a remote Nexus server via gRPC.
///
/// Server-side NexusFS is the SSOT — this backend is a thin proxy.
/// Path resolution: `backend_path` from kernel routing is mount-stripped
/// (e.g. for a mount at `/zone/shared`, the full path
/// `/zone/shared/file.txt` becomes `file.txt`).  The remote server
/// expects the original absolute path, so `to_server_path` re-prepends
/// the mount point.  For REMOTE-profile root mounts (`zone_path="/"`)
/// the backend_path is already the full path — no prefix is added.
pub(crate) struct RemoteBackend {
    transport: Arc<RpcTransport>,
    /// Mount point of this backend (e.g. "/zone/shared" or "/").
    /// Used to reconstruct the absolute path sent to the hub.
    /// Defaults to empty (root mount) until factory threads the
    /// mount path through; see Issue #3786 follow-up.
    zone_path: String,
}

impl RemoteBackend {
    pub fn new(transport: Arc<RpcTransport>) -> Self {
        Self {
            transport,
            zone_path: String::new(),
        }
    }
}

/// Reconstruct the absolute server path from mount point + backend_path.
///
/// * Root mounts (`zone_path` is `""` or `"/"`): backend_path is already
///   absolute — just ensure a leading slash.
/// * Sub-path mounts (e.g. `"/zone/shared"`): prepend zone_path so the
///   hub receives `/zone/shared/file.txt` rather than `/file.txt`.
fn to_server_path(zone_path: &str, backend_path: &str) -> String {
    let bp = if backend_path.is_empty() {
        "/".to_string()
    } else if backend_path.starts_with('/') {
        backend_path.to_string()
    } else {
        format!("/{backend_path}")
    };
    if zone_path.is_empty() || zone_path == "/" {
        bp
    } else {
        format!("{zone_path}{bp}")
    }
}

impl ObjectStore for RemoteBackend {
    fn name(&self) -> &str {
        "remote"
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let path = to_server_path(&self.zone_path, content_id);
        tracing::debug!(
            zone_path = %self.zone_path,
            server_path = %path,
            content_id = %content_id,
            "RemoteBackend::read_content → Call RPC sys_read"
        );

        // Issue #3786: use Call RPC, not native Read RPC.
        //
        // The native Read RPC path on hub calls `resolve_context(token)` which
        // builds a minimal OperationContext with no `zone_perms`.  Hub's
        // enforcer then falls through to the namespace/ReBAC visibility check
        // which raises NexusFileNotFoundError for multi-zone tokens whose
        // subject has no registered namespace.
        //
        // The Call RPC path goes through hub's `dispatch_call_sync` →
        // `authenticate(token)` → full auth dict including `zone_perms` →
        // `get_operation_context(auth_dict)` → Python OperationContext with
        // zone_perms populated.  The enforcer's zone_perms fast-path then
        // correctly grants access to zones listed in the token.
        let payload = serde_json::json!({ "path": path });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (resp, is_error) = self.transport.call("sys_read", &bytes).map_err(|e| {
            tracing::warn!(path = %path, err = %e, "RemoteBackend::read_content transport error");
            StorageError::IOError(std::io::Error::other(e))
        })?;

        if is_error {
            let msg = String::from_utf8_lossy(&resp);
            tracing::warn!(
                path = %path,
                error = %msg,
                "RemoteBackend::read_content hub returned error"
            );
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "sys_read({path}): {msg}"
            ))));
        }

        // Response envelope: {"result": {"__type__": "bytes", "data": "<base64>"}}
        let value: serde_json::Value = serde_json::from_slice(&resp)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let result = value.get("result").ok_or_else(|| {
            StorageError::IOError(std::io::Error::other(format!(
                "sys_read({path}): no result key in response"
            )))
        })?;

        if result.get("__type__").and_then(|t| t.as_str()) == Some("bytes") {
            let data = result
                .get("data")
                .and_then(|d| d.as_str())
                .unwrap_or_default();
            use base64::Engine;
            let content = base64::engine::general_purpose::STANDARD
                .decode(data)
                .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
            tracing::debug!(
                path = %path,
                bytes = content.len(),
                "RemoteBackend::read_content ok (bytes)"
            );
            return Ok(content);
        }

        // Fallback: plain string (text files that bypassed bytes-wrapping)
        if let Some(s) = result.as_str() {
            tracing::debug!(
                path = %path,
                bytes = s.len(),
                "RemoteBackend::read_content ok (string fallback)"
            );
            return Ok(s.as_bytes().to_vec());
        }

        tracing::warn!(
            path = %path,
            result = ?result,
            "RemoteBackend::read_content unexpected result shape"
        );
        Err(StorageError::IOError(std::io::Error::other(format!(
            "sys_read({path}): unexpected result shape"
        ))))
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &kernel::kernel::OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        // Partial writes are not supported by RemoteBackend.
        //
        // For offset == 0 the kernel passes route.backend_path as content_id,
        // so we can correctly map it to an absolute server path.  For
        // offset > 0 the kernel passes the OLD CAS etag as content_id — not
        // a path — which would be silently misrouted through to_server_path
        // and result in a full sys_write to "/zone/<mount>/<old-etag>"
        // (corrupting both the original file's metadata and creating an
        // orphan blob on the hub).  The trait does not yet thread
        // backend_path + offset together for path-addressed backends, so the
        // only correct behaviour is to reject the partial write.
        if offset != 0 {
            tracing::warn!(
                zone_path = %self.zone_path,
                content_id = %content_id,
                offset = offset,
                "RemoteBackend::write_content rejecting partial write \
                 (offset != 0 not supported — content_id is the old etag, not the path)"
            );
            return Err(StorageError::IOError(std::io::Error::other(
                "RemoteBackend does not support partial writes (offset != 0); \
                 use sys_write at offset=0 or read-modify-write at the caller",
            )));
        }
        let path = to_server_path(&self.zone_path, content_id);
        tracing::debug!(
            zone_path = %self.zone_path,
            content_id = %content_id,
            server_path = %path,
            bytes = content.len(),
            "RemoteBackend::write_content → Call RPC sys_write"
        );

        // Issue #3786: use Call RPC, not native Write RPC, so hub's Python
        // dispatch builds a full zone_perms context and can enforce read-only
        // zone restrictions (native Write bypasses Python enforcement).
        // Encode content as RPC codec bytes wrapper: {"__type__":"bytes","data":"<b64>"}
        use base64::Engine;
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        let payload = serde_json::json!({
            "path": path,
            "buf": { "__type__": "bytes", "data": b64 }
        });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;

        let (resp, is_error) = self.transport.call("sys_write", &bytes).map_err(|e| {
            tracing::warn!(path = %path, err = %e, "RemoteBackend::write_content transport error");
            StorageError::IOError(std::io::Error::other(e))
        })?;

        if is_error {
            let msg = String::from_utf8_lossy(&resp);
            tracing::warn!(
                path = %path,
                error = %msg,
                "RemoteBackend::write_content hub returned error"
            );
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "sys_write({path}): {msg}"
            ))));
        }

        // Response envelope: {"result": {"bytes_written": N, "etag": "...", "size": N, ...}}
        let value: serde_json::Value = serde_json::from_slice(&resp)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let result = value.get("result").ok_or_else(|| {
            StorageError::IOError(std::io::Error::other(format!(
                "sys_write({path}): no result key in response"
            )))
        })?;

        let etag = result
            .get("etag")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let size = result
            .get("size")
            .and_then(|v| v.as_u64())
            .unwrap_or(content.len() as u64);

        tracing::debug!(
            path = %path,
            etag = %etag,
            size = size,
            "RemoteBackend::write_content ok"
        );
        Ok(WriteResult {
            content_id: etag.clone(),
            version: etag,
            size,
        })
    }

    fn delete_content(&self, _content_id: &str) -> Result<(), StorageError> {
        // Server handles content deletion via metastore delete (sys_unlink).
        // Content deletion by hash is not meaningful for remote backends.
        Ok(())
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        // Use sys_stat to get size.  For sub-path mounts (e.g. /zone/shared)
        // the kernel passes the mount-stripped backend_path as content_id, so
        // re-prepend zone_path to send the absolute path the hub expects.
        let path = to_server_path(&self.zone_path, content_id);
        let payload = serde_json::json!({ "path": path });
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
        let server_path = to_server_path(&self.zone_path, path);
        let payload = serde_json::json!({
            "path": server_path,
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
                "mkdir failed: {server_path}"
            ))));
        }
        Ok(())
    }

    fn rmdir(&self, path: &str, recursive: bool) -> Result<(), StorageError> {
        let server_path = to_server_path(&self.zone_path, path);
        let payload = serde_json::json!({
            "path": server_path,
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
                "rmdir failed: {server_path}"
            ))));
        }
        Ok(())
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        // Issue #3786: route deletes through Call RPC, not native Delete RPC.
        // Native Delete on the hub uses Rust resolve_context which does NOT
        // carry zone_perms into Python — so a federation token with zone:r
        // would otherwise reach sys_unlink without the read-only enforcement
        // path firing.  Call RPC goes through dispatch_call_sync where the
        // Python OperationContext is rebuilt with full zone_perms and the
        // enforcer's read-only gate runs before the kernel touches anything.
        let server_path = to_server_path(&self.zone_path, path);
        let payload = serde_json::json!({ "path": server_path });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (resp, is_error) = self
            .transport
            .call("sys_unlink", &bytes)
            .map_err(|e| {
                tracing::warn!(path = %server_path, err = %e, "RemoteBackend::delete_file transport error");
                StorageError::IOError(std::io::Error::other(e))
            })?;
        if is_error {
            let msg = String::from_utf8_lossy(&resp);
            tracing::warn!(
                path = %server_path,
                error = %msg,
                "RemoteBackend::delete_file hub returned error"
            );
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "sys_unlink({server_path}): {msg}"
            ))));
        }
        Ok(())
    }

    fn rename(&self, old_path: &str, new_path: &str) -> Result<(), StorageError> {
        // Both endpoints of a rename are mount-stripped backend paths; remap
        // both to absolute server paths.  Cross-mount renames aren't
        // expressible through this trait — both paths share self.zone_path.
        let server_old = to_server_path(&self.zone_path, old_path);
        let server_new = to_server_path(&self.zone_path, new_path);
        let payload = serde_json::json!({
            "old_path": server_old,
            "new_path": server_new,
        });
        let bytes = serde_json::to_vec(&payload)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e.to_string())))?;
        let (_resp, is_error) = self
            .transport
            .call("sys_rename", &bytes)
            .map_err(|e| StorageError::IOError(std::io::Error::other(e)))?;
        if is_error {
            return Err(StorageError::IOError(std::io::Error::other(format!(
                "rename failed: {server_old} -> {server_new}"
            ))));
        }
        Ok(())
    }
}
