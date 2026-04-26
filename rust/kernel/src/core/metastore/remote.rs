//! Remote Metastore — `Metastore` trait impl via tonic gRPC.
//!
//! Replaces Python `storage/remote_metastore.py`. Each method dispatches
//! a Call RPC to the remote server's VFS layer (sys_stat, sys_readdir,
//! sys_setattr, sys_unlink, access).
//!
//! Issue #1134: Rust-first connector routing + REMOTE profile.

use std::sync::Arc;

use crate::metastore::{FileMetadata, Metastore, MetastoreError, PaginatedList};
use crate::rpc_transport::RpcTransport;

/// Metastore backed by a remote Nexus server via gRPC Call RPC.
///
/// All metadata ops serialize to JSON, dispatch via `Call(method, payload)`,
/// and deserialize the response. Server-side NexusFS is the SSOT.
pub(crate) struct RemoteMetastore {
    transport: Arc<RpcTransport>,
}

impl RemoteMetastore {
    pub fn new(transport: Arc<RpcTransport>) -> Self {
        Self { transport }
    }
}

impl Metastore for RemoteMetastore {
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetastoreError> {
        let payload = serde_json::json!({ "path": path });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("sys_stat", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            // Server reported error (path not found, etc.)
            return Ok(None);
        }

        let value: serde_json::Value = serde_json::from_slice(&resp_bytes)
            .map_err(|e| MetastoreError::IOError(format!("decode sys_stat response: {e}")))?;

        // Server returns None/null for missing paths
        if value.is_null() {
            return Ok(None);
        }

        parse_metadata_from_json(&value).map(Some)
    }

    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetastoreError> {
        let payload = serde_json::json!({
            "path": path,
            "entry_type": metadata.entry_type,
            "size": metadata.size,
            "etag": metadata.etag,
            "version": metadata.version,
            "zone_id": metadata.zone_id,
            "mime_type": metadata.mime_type,
            "last_writer_address": metadata.last_writer_address,
        });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (_resp, is_error) = self
            .transport
            .call("sys_setattr", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            return Err(MetastoreError::IOError(format!(
                "sys_setattr failed for {path}"
            )));
        }
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetastoreError> {
        let payload = serde_json::json!({ "path": path });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (_resp, is_error) = self
            .transport
            .call("sys_unlink", &bytes)
            .map_err(MetastoreError::IOError)?;

        Ok(!is_error)
    }

    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetastoreError> {
        let payload = serde_json::json!({
            "path": prefix,
            "recursive": true,
        });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("sys_readdir", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            return Ok(Vec::new());
        }

        let value: serde_json::Value = serde_json::from_slice(&resp_bytes)
            .map_err(|e| MetastoreError::IOError(format!("decode sys_readdir: {e}")))?;

        // Server returns an array of entries (path, entry_type pairs or full metadata)
        let entries = match value.as_array() {
            Some(arr) => arr
                .iter()
                .filter_map(|v| parse_metadata_from_json(v).ok())
                .collect(),
            None => Vec::new(),
        };

        Ok(entries)
    }

    fn exists(&self, path: &str) -> Result<bool, MetastoreError> {
        let payload = serde_json::json!({ "path": path });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("access", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            return Ok(false);
        }

        // Server returns a bool or a JSON object with an "exists" field
        let value: serde_json::Value = serde_json::from_slice(&resp_bytes).unwrap_or_default();
        Ok(value.as_bool().unwrap_or_else(|| {
            value
                .get("exists")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        }))
    }

    fn is_implicit_directory(&self, path: &str) -> Result<bool, MetastoreError> {
        let payload = serde_json::json!({ "path": path });
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("is_directory", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            return Ok(false);
        }

        let value: serde_json::Value = serde_json::from_slice(&resp_bytes).unwrap_or_default();
        Ok(value.as_bool().unwrap_or(false))
    }

    fn list_paginated(
        &self,
        prefix: &str,
        recursive: bool,
        limit: usize,
        cursor: Option<&str>,
    ) -> Result<PaginatedList, MetastoreError> {
        let mut payload = serde_json::json!({
            "path": prefix,
            "recursive": recursive,
            "limit": limit,
        });
        if let Some(c) = cursor {
            payload["cursor"] = serde_json::Value::String(c.to_string());
        }
        let bytes =
            serde_json::to_vec(&payload).map_err(|e| MetastoreError::IOError(e.to_string()))?;

        let (resp_bytes, is_error) = self
            .transport
            .call("sys_readdir", &bytes)
            .map_err(MetastoreError::IOError)?;

        if is_error {
            return Ok(PaginatedList::default());
        }

        let value: serde_json::Value = serde_json::from_slice(&resp_bytes)
            .map_err(|e| MetastoreError::IOError(format!("decode paginated readdir: {e}")))?;

        let items: Vec<FileMetadata> = value
            .get("items")
            .or(Some(&value))
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| parse_metadata_from_json(v).ok())
                    .collect()
            })
            .unwrap_or_default();

        let next_cursor = value
            .get("next_cursor")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let has_more = value
            .get("has_more")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let total_count = value
            .get("total_count")
            .and_then(|v| v.as_u64())
            .unwrap_or(items.len() as u64) as usize;

        Ok(PaginatedList {
            items,
            next_cursor,
            has_more,
            total_count,
        })
    }
}

/// Parse FileMetadata from a JSON value (server sys_stat response).
fn parse_metadata_from_json(value: &serde_json::Value) -> Result<FileMetadata, MetastoreError> {
    let obj = value
        .as_object()
        .ok_or_else(|| MetastoreError::IOError("expected JSON object".into()))?;

    Ok(FileMetadata {
        path: obj
            .get("path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        size: obj.get("size").and_then(|v| v.as_u64()).unwrap_or(0),
        etag: obj
            .get("etag")
            .or_else(|| obj.get("content_id"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        version: obj.get("version").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
        entry_type: obj.get("entry_type").and_then(|v| v.as_u64()).unwrap_or(0) as u8,
        zone_id: obj
            .get("zone_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        mime_type: obj
            .get("mime_type")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        created_at_ms: obj.get("created_at_ms").and_then(|v| v.as_i64()),
        modified_at_ms: obj.get("modified_at_ms").and_then(|v| v.as_i64()),
        last_writer_address: obj
            .get("last_writer_address")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
    })
}
