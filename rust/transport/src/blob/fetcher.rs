//! KernelBlobFetcher — kernel-side impl of `nexus_raft::BlobFetcher`.
//!
//! R20.18.7 co-locates the driver-to-driver `ReadBlob` RPC with
//! `ZoneApiService` on the raft port. The raft crate owns the trait
//! and the gRPC handler; the kernel owns the data plane (mount
//! backends). This module bridges the two.
//!
//! Store-and-forward: ``content_id`` is opaque. The fetcher resolves
//! it via the local ``VFSRouter`` — for federation reads ``content_id``
//! is a global VFS path, the router picks the matching mount, and the
//! mount's backend interprets the locally-stored
//! ``FileMetadata.content_id`` (hash for CAS, backend_path for PAS).
//! The kernel never inspects the string.
//!
//! Installation: `Kernel::wire_blob_fetcher` (called from
//! `init_federation_from_env` once the ZoneManager is up) takes the
//! slot handed back by `ZoneManager::blob_fetcher_slot()` and writes
//! `Arc<KernelBlobFetcher>` into it. From then on, peer `ReadBlob`
//! requests resolve against the local data plane.

use std::sync::Arc;

use nexus_raft::blob_fetcher::BlobFetcher;

use kernel::core::dcache::DCache;
use kernel::kernel::OperationContext;
use kernel::vfs_router::VFSRouter;

/// Kernel-side `BlobFetcher` — backed by the kernel's `VFSRouter`.
pub struct KernelBlobFetcher {
    vfs_router: Arc<VFSRouter>,
    dcache: Arc<DCache>,
}

impl KernelBlobFetcher {
    pub fn new(vfs_router: Arc<VFSRouter>, dcache: Arc<DCache>) -> Self {
        Self { vfs_router, dcache }
    }
}

#[tonic::async_trait]
impl BlobFetcher for KernelBlobFetcher {
    /// Resolve ``content_id`` against the local data plane.
    ///
    /// Two cases — peer side picks based on what the local mount table
    /// can route:
    ///
    /// 1. **VFS path** — federation reads. ``content_id`` is the global
    ///    VFS path the peer asked for. We re-route locally (peer self-
    ///    routing — same as ``sys_read`` would), then read whatever the
    ///    local backend stored at that path. The local
    ///    ``FileMetadata.content_id`` (CAS hash or PAS backend_path,
    ///    kernel-opaque) is the actual identifier we pass into
    ///    ``read_content``; the backend interprets it.
    ///
    /// 2. **CAS chunk hash** — chunked dedup fetches. ``content_id`` is
    ///    a content-address hash with no associated mount path; the
    ///    ``vfs_router.route()`` call returns ``NotMounted`` so we fall
    ///    through to a try-each-CAS-backend probe. The first backend
    ///    that recognises the hash returns the bytes.
    ///
    /// The kernel does not pre-classify which case ``content_id`` is —
    /// the mount table answers that question. Caller-side (``kernel``,
    /// ``peer_blob_client``, raft transport) stays purely transparent;
    /// the dispatch is local to this peer's data plane, where it must
    /// be (we are not a generic byte-server, we are this node's
    /// VFSRouter).
    async fn read(&self, content_id: &str) -> Result<Vec<u8>, String> {
        if content_id.is_empty() {
            return Err("empty content_id".to_string());
        }
        let ctx = OperationContext::new("system", contracts::ROOT_ZONE_ID, true, None, true);

        // Case 1: VFS path → standard local read.
        if let Ok(route) = self.vfs_router.route(content_id, contracts::ROOT_ZONE_ID) {
            let local_content_id = self
                .dcache
                .get_entry(content_id)
                .and_then(|e| e.content_id)
                .unwrap_or_default();
            return self
                .vfs_router
                .read_content(
                    &route.mount_point,
                    &local_content_id,
                    &route.backend_path,
                    &ctx,
                )
                .ok_or_else(|| format!("read_content({content_id}): not found"));
        }

        // Case 2: not a routable path → try each backend by raw
        // content_id (CAS chunk-hash dedup fetch path).
        let backends = self.vfs_router.backends();
        if backends.is_empty() {
            return Err("no local backends registered".to_string());
        }
        let mut last_err: Option<String> = None;
        for backend in backends {
            match backend.read_content(content_id, "", &ctx) {
                Ok(bytes) => return Ok(bytes),
                Err(e) => last_err = Some(format!("{:?}", e)),
            }
        }
        Err(last_err.unwrap_or_else(|| "not found".to_string()))
    }
}

/// Phase 4 (full) install hook.  Called from `nexus-cdylib`'s
/// `#[pymodule]` boot after `kernel::python::register` so that, by
/// the time Python starts firing federation reads, the raft server's
/// `BlobFetcherSlot` already has a kernel-backed fetcher.
///
/// No-op if `Kernel::pending_blob_fetcher_slot` is empty (federation
/// disabled — `NEXUS_HOSTNAME` was unset).
///
/// Phase 5: kernel hands back the slot as `Box<dyn Any + Send + Sync>`
/// — transport downcasts to the concrete `BlobFetcherSlot` type
/// here because transport already depends on raft (the kernel side
/// no longer does).
pub fn install(kernel: &kernel::kernel::Kernel) {
    let Some(any_slot) = kernel.take_pending_blob_fetcher_slot() else {
        return;
    };
    let slot = match any_slot.downcast::<nexus_raft::blob_fetcher::BlobFetcherSlot>() {
        Ok(boxed) => *boxed,
        Err(_) => {
            tracing::error!(
                "transport::blob::fetcher::install: pending slot type mismatch \
                 (expected nexus_raft::blob_fetcher::BlobFetcherSlot)"
            );
            return;
        }
    };
    let fetcher = Arc::new(KernelBlobFetcher::new(
        kernel.vfs_router_arc(),
        kernel.dcache_arc(),
    ));
    *slot.write() = Some(fetcher as Arc<dyn BlobFetcher>);
}
