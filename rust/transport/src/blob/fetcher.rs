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
    /// The peer doesn't tell us whether ``content_id`` is a VFS path, a
    /// CAS hash, or a backend-specific handle. We try in order:
    ///
    /// 1. **Path-style local read** — if ``vfs_router.route()`` resolves
    ///    ``content_id`` to a mount, try reading from that mount with
    ///    the local ``FileMetadata.content_id`` (or the route's
    ///    ``backend_path`` as a cold-cache fallback). This handles
    ///    federation reads where the peer asked for a VFS path.
    ///
    /// 2. **CAS hash fan-out** — if the path-style attempt didn't
    ///    return bytes (mount lookup miss, or backend returned an
    ///    error), try every backend's ``read_content(content_id, ctx)``.
    ///    CAS backends recognise their hashes here; PAS / connector
    ///    backends will reject and we move on. This catches:
    ///      * CAS chunk fetches whose ``content_id`` is a raw hash
    ///        with no mount path
    ///      * Federation reads where the peer's local routing of the
    ///        path doesn't reach the same mount as the writer (e.g.
    ///        the writer published into a federation_share zone whose
    ///        mount only exists on the joining node, or a crosslink
    ///        alias whose target zone's storage lives elsewhere on
    ///        the peer)
    ///
    /// Either path the file ends at the same shared storage Arc, so
    /// fall-through is a thin extra try, not a heavy fan-out.
    async fn read(&self, content_id: &str) -> Result<Vec<u8>, String> {
        if content_id.is_empty() {
            return Err("empty content_id".to_string());
        }
        let ctx = OperationContext::new("system", contracts::ROOT_ZONE_ID, true, None, true);

        // Step 1: try path-style routing → local mount read.
        if let Ok(route) = self.vfs_router.route(content_id, contracts::ROOT_ZONE_ID) {
            let local_content_id = self
                .dcache
                .get_entry(content_id)
                .and_then(|e| e.content_id)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| route.backend_path.clone());
            if let Some(bytes) =
                self.vfs_router
                    .read_content(&route.mount_point, &local_content_id, &ctx)
            {
                return Ok(bytes);
            }
        }

        // Step 2: hash-style fan-out across every local backend. CAS
        // backends will recognise a hash; PAS / connector backends will
        // reject and we keep walking. This is also the recovery path
        // for federation-share / crosslink reads where the writer's
        // local routing doesn't carry over verbatim to the peer's
        // mount table.
        let backends = self.vfs_router.backends();
        if backends.is_empty() {
            return Err(format!("read_content({content_id}): no local backends"));
        }
        let mut last_err: Option<String> = None;
        for backend in backends {
            match backend.read_content(content_id, &ctx) {
                Ok(bytes) => return Ok(bytes),
                Err(e) => last_err = Some(format!("{:?}", e)),
            }
        }
        Err(last_err.unwrap_or_else(|| format!("read_content({content_id}): not found")))
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
