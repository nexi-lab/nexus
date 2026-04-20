//! OpenAI-compatible connector — CAS-backed HTTP client (R10d-LLM-Port).
//!
//! Owns a `CASEngine` wired with `MessageBoundaryStrategy` so every
//! conversation JSON we persist gets per-message chunk dedup for free. HTTP
//! runs on a kernel-shared tokio runtime (passed in at construction) so
//! workers shut down cleanly on kernel drop. The streaming entry point lives
//! in `openai_streaming.rs` as an `impl` block on this struct — splitting the
//! SSE state machine away from the storage + config surface.

#![allow(dead_code)]

use std::path::Path;
use std::sync::Arc;

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::cas_chunking::MessageBoundaryStrategy;
use crate::cas_engine::CASEngine;
use crate::cas_transport::LocalCASTransport;
use crate::kernel::OperationContext;

/// OpenAI-compatible backend — CAS-backed blob storage + HTTP inference.
///
/// Storage: `CASEngine(LocalCASTransport, MessageBoundaryStrategy)`. Writes
/// conversation JSON as per-message chunks — two sessions sharing the first
/// N messages reuse the same N chunk blobs in the spool.
pub(crate) struct OpenAIBackend {
    pub(crate) backend_name: String,
    pub(crate) base_url: String,
    pub(crate) api_key: String,
    pub(crate) default_model: String,
    /// CAS engine rooted at the per-mount spool dir. `as_cas()` exposes this
    /// to the `PyKernel::cas_*` surface so Python callers can read/write on
    /// LLM mounts without the legacy `CASOpenAIBackend` Python wrapper.
    pub(crate) engine: CASEngine,
    /// Shared reqwest HTTP client — one TCP/H2 pool is reused across every
    /// chat completion + streaming call on this mount.
    pub(crate) http: reqwest::Client,
    /// Kernel-owned tokio runtime. Cloned from `Kernel::peer_client.runtime()`
    /// so scatter-gather fetches and LLM calls share workers instead of each
    /// backend spawning its own pool.
    pub(crate) runtime: Arc<tokio::runtime::Runtime>,
}

impl OpenAIBackend {
    /// Build an OpenAI-compatible backend.
    ///
    /// `blob_root` is the spool dir under which CAS blobs are stored — the
    /// caller (`sys_setattr`) derives this from the kernel root so per-mount
    /// state is cleanly namespaced and can be removed on unmount.
    pub(crate) fn new(
        name: &str,
        base_url: &str,
        api_key: &str,
        default_model: &str,
        blob_root: &Path,
        runtime: Arc<tokio::runtime::Runtime>,
    ) -> std::io::Result<Self> {
        let transport = LocalCASTransport::new(blob_root, false)?;
        let engine = CASEngine::with_strategy(transport, Arc::new(MessageBoundaryStrategy));
        let http = reqwest::Client::builder()
            .pool_idle_timeout(std::time::Duration::from_secs(60))
            .build()
            .map_err(|e| std::io::Error::other(format!("reqwest build: {e}")))?;
        Ok(Self {
            backend_name: name.to_string(),
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            default_model: default_model.to_string(),
            engine,
            http,
            runtime,
        })
    }

    /// Expose the CASEngine to the `PyKernel::cas_*` surface.
    pub(crate) fn engine(&self) -> &CASEngine {
        &self.engine
    }
}

// ── ObjectStore impl — all storage goes through CASEngine ──────────────

impl ObjectStore for OpenAIBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    #[allow(private_interfaces)]
    fn as_cas(&self) -> Option<&CASEngine> {
        Some(&self.engine)
    }

    fn as_llm_streaming(&self) -> Option<&dyn crate::openai_streaming::LlmStreamingBackend> {
        Some(self)
    }

    fn write_content(
        &self,
        content: &[u8],
        _content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "openai backend does not support offset writes",
            ));
        }
        let hash = self
            .engine
            .write_content(content)
            .map_err(StorageError::from)?;
        Ok(WriteResult {
            version: hash.clone(),
            size: content.len() as u64,
            content_id: hash,
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        self.engine
            .read_content(content_id)
            .map_err(StorageError::from)
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        if self.engine.is_chunked(content_id) {
            self.engine
                .delete_chunked(content_id)
                .map_err(StorageError::from)
        } else {
            self.engine
                .delete_content(content_id)
                .map_err(StorageError::from)
        }
    }

    fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
        self.engine.get_size(content_id).map_err(StorageError::from)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn test_ctx() -> OperationContext {
        OperationContext::new("test", "root", false, None, false)
    }

    fn build(tmp: &TempDir) -> OpenAIBackend {
        let rt = crate::peer_blob_client::build_kernel_runtime();
        OpenAIBackend::new(
            "openai_compatible",
            "https://api.openai.com/v1",
            "sk-test",
            "gpt-4o",
            tmp.path(),
            rt,
        )
        .unwrap()
    }

    #[test]
    fn test_name() {
        let tmp = TempDir::new().unwrap();
        let b = build(&tmp);
        assert_eq!(b.name(), "openai_compatible");
    }

    #[test]
    fn test_write_and_read_content_roundtrip() {
        let tmp = TempDir::new().unwrap();
        let b = build(&tmp);
        let ctx = test_ctx();
        let payload = br#"hello llm backend"#;
        let wr = b.write_content(payload, "", &ctx, 0).unwrap();
        assert_eq!(wr.size, payload.len() as u64);
        let back = b.read_content(&wr.content_id, "", &ctx).unwrap();
        assert_eq!(back, payload);
    }

    #[test]
    fn test_openai_backend_cas_ops_use_message_boundary() {
        // Two conversations sharing the first two messages must dedup those
        // chunks — validates that MessageBoundaryStrategy is wired.
        let tmp = TempDir::new().unwrap();
        let b = build(&tmp);
        let ctx = test_ctx();

        let conv_a = br#"[{"role":"user","content":"hi"},{"role":"assistant","content":"hello"},{"role":"user","content":"A"}]"#;
        let conv_b = br#"[{"role":"user","content":"hi"},{"role":"assistant","content":"hello"},{"role":"user","content":"B"}]"#;

        let wr_a = b.write_content(conv_a, "", &ctx, 0).unwrap();
        let wr_b = b.write_content(conv_b, "", &ctx, 0).unwrap();
        assert_ne!(wr_a.content_id, wr_b.content_id);

        // Both must be chunked manifests (MessageBoundary accepts any valid
        // role-carrying JSON array).
        assert!(b.engine.is_chunked(&wr_a.content_id));
        assert!(b.engine.is_chunked(&wr_b.content_id));

        // Dedup check — the shared prefix chunks exist exactly once.
        let manifest_a_bytes = b.engine.transport().read_blob(&wr_a.content_id).unwrap();
        let manifest_a: serde_json::Value = serde_json::from_slice(&manifest_a_bytes).unwrap();
        let manifest_b_bytes = b.engine.transport().read_blob(&wr_b.content_id).unwrap();
        let manifest_b: serde_json::Value = serde_json::from_slice(&manifest_b_bytes).unwrap();

        let chunks_a = manifest_a["chunks"].as_array().unwrap();
        let chunks_b = manifest_b["chunks"].as_array().unwrap();
        assert_eq!(chunks_a.len(), 3);
        assert_eq!(chunks_b.len(), 3);
        // First two chunks (shared prefix) match by hash.
        assert_eq!(chunks_a[0]["chunk_hash"], chunks_b[0]["chunk_hash"]);
        assert_eq!(chunks_a[1]["chunk_hash"], chunks_b[1]["chunk_hash"]);
        // Third chunk (divergent last message) differs.
        assert_ne!(chunks_a[2]["chunk_hash"], chunks_b[2]["chunk_hash"]);
    }

    #[test]
    fn test_delete_chunked_content_sweeps_chunks() {
        let tmp = TempDir::new().unwrap();
        let b = build(&tmp);
        let ctx = test_ctx();
        let conv = br#"[{"role":"user","content":"hi"},{"role":"assistant","content":"hello"}]"#;
        let wr = b.write_content(conv, "", &ctx, 0).unwrap();
        assert!(b.engine.is_chunked(&wr.content_id));
        b.delete_content(&wr.content_id).unwrap();
        assert!(!b.engine.content_exists(&wr.content_id));
    }

    #[test]
    fn test_as_cas_exposes_engine() {
        let tmp = TempDir::new().unwrap();
        let b = build(&tmp);
        assert!(b.as_cas().is_some());
    }
}
