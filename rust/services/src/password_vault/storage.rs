//! Shared test utilities for the password_vault module.
//!
//! Production storage is handled by `GenericSecretsServiceImpl` (via
//! `SecretStorage`) — this module only provides the in-memory backend
//! for test constructors.

use std::collections::HashMap;

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::OperationContext;

// ── In-memory ObjectStore for vault content ──────────────────────────

/// Minimal in-memory ObjectStore — stores vault entry blobs in a
/// HashMap keyed by content_id. Thread-safe via `parking_lot::Mutex`.
/// Suitable for tests only; production uses a persistent backend
/// (PathLocalBackend) injected by the vault binary.
pub(crate) struct MemBackend {
    data: parking_lot::Mutex<HashMap<String, Vec<u8>>>,
}

impl MemBackend {
    pub(crate) fn new() -> Self {
        Self {
            data: parking_lot::Mutex::new(HashMap::new()),
        }
    }
}

impl ObjectStore for MemBackend {
    fn name(&self) -> &str {
        "vault-mem"
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        let size = content.len() as u64;
        self.data
            .lock()
            .insert(content_id.to_string(), content.to_vec());
        Ok(WriteResult {
            content_id: content_id.to_string(),
            version: content_id.to_string(),
            size,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.data
            .lock()
            .get(content_id)
            .cloned()
            .ok_or_else(|| StorageError::NotFound(content_id.to_string()))
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        self.data.lock().remove(content_id);
        Ok(())
    }
}
