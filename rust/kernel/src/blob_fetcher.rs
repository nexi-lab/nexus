//! KernelBlobFetcher — kernel-side impl of `nexus_raft::BlobFetcher`.
//!
//! R20.18.7 co-locates the driver-to-driver `ReadBlob` RPC with
//! `ZoneApiService` on the raft port. The raft crate owns the trait
//! and the gRPC handler; the kernel owns the data plane (mount
//! backends + CAS). This module bridges the two.
//!
//! The fetcher walks every non-empty backend on the kernel's
//! `VFSRouter` and returns the first one that successfully serves
//! `content_hash`. CAS backends ignore `backend_path` and `ctx`, so we
//! pass an empty path and a system `OperationContext`.
//!
//! Installation: `Kernel::wire_blob_fetcher` (called from
//! `init_federation_from_env` once the ZoneManager is up) takes the
//! slot handed back by `ZoneManager::blob_fetcher_slot()` and writes
//! `Arc<KernelBlobFetcher>` into it. From then on, peer `ReadBlob`
//! requests resolve against the local CAS.

use std::sync::Arc;

use nexus_raft::blob_fetcher::BlobFetcher;

use crate::dcache::DCache;
use crate::kernel::OperationContext;
use crate::vfs_router::VFSRouter;

/// Kernel-side `BlobFetcher` — backed by the kernel's `VFSRouter`.
pub(crate) struct KernelBlobFetcher {
    vfs_router: Arc<VFSRouter>,
    dcache: Arc<DCache>,
}

impl KernelBlobFetcher {
    pub(crate) fn new(vfs_router: Arc<VFSRouter>, dcache: Arc<DCache>) -> Self {
        Self { vfs_router, dcache }
    }
}

#[tonic::async_trait]
impl BlobFetcher for KernelBlobFetcher {
    async fn read_blob(&self, content_hash: &str) -> Result<Vec<u8>, String> {
        if content_hash.is_empty() {
            return Err("empty content_hash".to_string());
        }
        let backends = self.vfs_router.backends();
        if backends.is_empty() {
            return Err("no local backends registered".to_string());
        }
        let ctx = OperationContext::new("system", contracts::ROOT_ZONE_ID, true, None, true);
        let mut last_err: Option<String> = None;
        for backend in backends {
            match backend.read_content(content_hash, "", &ctx) {
                Ok(bytes) => return Ok(bytes),
                Err(e) => last_err = Some(format!("{:?}", e)),
            }
        }
        Err(last_err.unwrap_or_else(|| "not found".to_string()))
    }

    /// Path-addressed federation read.
    ///
    /// Routes `path` through `VFSRouter` the same way a local sys_read
    /// would, then asks the resolved backend for its content. The
    /// dcache lookup gives us the etag (CAS hash) so CAS backends can
    /// dedup; PAS backends ignore content_id and use `backend_path`
    /// directly. Either way the answer is the bytes the writer would
    /// hand the local kernel.
    async fn read_path(&self, path: &str) -> Result<Vec<u8>, String> {
        if path.is_empty() {
            return Err("empty path".to_string());
        }
        let route = self
            .vfs_router
            .route(path, contracts::ROOT_ZONE_ID)
            .map_err(|e| format!("route({path}): {e:?}"))?;
        let content_id = self
            .dcache
            .get_entry(path)
            .and_then(|e| e.etag)
            .unwrap_or_default();
        let ctx = OperationContext::new("system", contracts::ROOT_ZONE_ID, true, None, true);
        self.vfs_router
            .read_content(&route.mount_point, &content_id, &route.backend_path, &ctx)
            .ok_or_else(|| format!("read_content({path}): not found"))
    }
}
