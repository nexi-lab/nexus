//! Concrete `FederationProvider` implementation.
//!
//! Phase 5 anchor: `Kernel.federation` slot now holds a non-Noop impl
//! that delegates each trait method to the corresponding kernel-side
//! primitive.  Federation-aware syscalls dispatch via
//! `kernel.federation_arc().<method>(kernel, …)` instead of calling
//! the internal helpers directly.
//!
//! ## Provider shape
//!
//! `RaftFederationProvider` is a unit struct.  It owns no state — every
//! method takes `kernel: &Kernel` and reaches into kernel-owned
//! federation state (`zone_manager`, `peer_client`,
//! `pending_blob_fetcher_slot`).  This mirrors the Phase 4
//! `PeerBlobClient` DI pattern: kernel keeps the runtime state,
//! the provider exposes a stable trait surface.
//!
//! ## Phase H follow-up
//!
//! The provider lives in `kernel/` while the workspace still has the
//! `kernel → raft` Cargo edge.  A subsequent pass moves the file and
//! its raft-typed call sites (`init_from_env` → `nexus_raft::ZoneManager::new`,
//! `wire_mount` → `Kernel::wire_federation_mount`) into the raft crate
//! once the Cargo flip clears the cycle.

use std::sync::Arc;

use crate::abc::meta_store::MetaStore;
use crate::hal::federation::{BlobFetcherSlot, FederationProvider, FederationResult};
use crate::kernel::Kernel;
use contracts::lock_state::Locks;

/// Raft-backed `FederationProvider` impl.
pub struct RaftFederationProvider;

impl FederationProvider for RaftFederationProvider {
    fn init_from_env(&self, kernel: &Kernel) -> FederationResult<bool> {
        // Idempotency: zone manager set already → no-op success.
        let was_initialized = kernel.zone_manager_arc().is_some();
        kernel
            .init_federation_from_env()
            .map_err(|e| format!("init_from_env: {e:?}"))?;
        Ok(!was_initialized && kernel.zone_manager_arc().is_some())
    }

    fn is_initialized(&self, kernel: &Kernel) -> bool {
        kernel.zone_manager_arc().is_some()
    }

    fn bind_address(&self, kernel: &Kernel) -> Option<String> {
        // ZoneManager owns the bound socket internally (see ServerConfig
        // in raft/src/zone_manager.rs); kernel exposes its own
        // `self_address` for federation content origin.
        kernel.self_address_string()
    }

    fn hostname(&self, kernel: &Kernel) -> Option<String> {
        // Kernel does not store hostname directly today.  Surface what
        // `init_federation_from_env` derived as the self_address — the
        // hostname half can be computed by callers from
        // `host:port` when needed.
        kernel.self_address_string()
    }

    fn list_zones(&self, kernel: &Kernel) -> Vec<String> {
        kernel
            .zone_manager_arc()
            .map(|zm| zm.list_zones())
            .unwrap_or_default()
    }

    fn metastore_for_zone(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
    ) -> FederationResult<Arc<dyn MetaStore>> {
        // Direct construction lives inside `Kernel::resolve_federation_mount_backing`
        // today; the trait-side accessor is a Phase H follow-up.
        Err("metastore_for_zone: not yet wired through trait".into())
    }

    fn locks_for_zone(&self, _kernel: &Kernel, _zone_id: &str) -> FederationResult<Arc<dyn Locks>> {
        // `kernel.install_federation_locks(node, runtime)` consumes the
        // ZoneConsensus directly today; the trait-side accessor is a
        // Phase H follow-up.
        Err("locks_for_zone: not yet wired through trait".into())
    }

    fn wal_stream_for_zone(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _prefix: &str,
    ) -> FederationResult<Arc<dyn crate::stream::StreamBackend>> {
        // `kernel.prepare_audit_stream(zone_id, stream_path)` builds
        // the WalStreamCore today; the trait-side accessor is a Phase
        // H follow-up.
        Err("wal_stream_for_zone: not yet wired through trait".into())
    }

    fn remote_read_blob(
        &self,
        kernel: &Kernel,
        _zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> FederationResult<Vec<u8>> {
        // Forward via the existing `peer_client` slot.  Kernel's
        // `try_remote_fetch` takes the same path/content_id pair and
        // routes by `last_writer_address`.  For a direct trait-side
        // call, ride the underlying client; the caller is responsible
        // for selecting the origin.
        let client = kernel.peer_client_arc();
        if !content_id.is_empty() {
            // We need the origin address to call fetch_etag/fetch_path.
            // Without it, fall back to "unknown origin" error so the
            // caller decides whether to retry through metadata lookup.
            client.fetch_etag("", content_id)
        } else {
            client.fetch_path("", path)
        }
    }

    fn wire_mount(
        &self,
        kernel: &Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> FederationResult<()> {
        kernel
            .wire_federation_mount(parent_zone, mount_path, target_zone)
            .map_err(|e| format!("wire_mount: {e:?}"))
    }

    fn start_replication_scanner(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _policies_json: &str,
        _interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>> {
        // The replication scanner construction in
        // `kernel/src/replication.rs` is currently dead-code-flagged
        // pending wiring; the trait-side accessor surfaces an
        // unambiguous error until that lands.
        Err("start_replication_scanner: not yet wired through trait".into())
    }

    fn stash_blob_fetcher_slot(&self, kernel: &Kernel, slot: BlobFetcherSlot) {
        kernel.stash_blob_fetcher_slot(slot);
    }

    fn take_blob_fetcher_slot(&self, kernel: &Kernel) -> Option<BlobFetcherSlot> {
        kernel.take_pending_blob_fetcher_slot()
    }
}

/// Install `RaftFederationProvider` into the kernel's federation slot.
///
/// Mirrors `transport::blob::peer_client::install` — called once per
/// process from the cdylib boot path
/// (`nexus-cdylib::install_federation_wiring`).  Idempotent — the
/// kernel slot is simply replaced so re-imports stay safe.
pub fn install(kernel: &Kernel) {
    kernel.set_federation(Arc::new(RaftFederationProvider));
}
