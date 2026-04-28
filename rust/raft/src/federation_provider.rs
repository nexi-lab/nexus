//! Concrete `FederationProvider` implementation.
//!
//! Phase H of the rust-workspace restructure put the `RaftFederationProvider`
//! impl here in the raft crate (after the `kernel → raft` Cargo edge
//! flipped to `raft → kernel`).  The kernel installs an
//! `Arc<dyn FederationProvider>` into its `federation` slot via the
//! cdylib boot path, and federation-aware syscalls dispatch through
//! the trait.
//!
//! ## Provider shape
//!
//! `RaftFederationProvider` is a unit struct that owns no state.
//! Every trait method takes `kernel: &Kernel` so the impl can reach
//! kernel-side primitives (peer_client, self_address) without holding
//! back-references.  Mirrors the Phase 4 `PeerBlobClient` DI pattern.
//!
//! Federation methods that need raft-side state (zone_manager,
//! zone_registry, ZoneConsensus) construct it lazily here; the
//! Phase H follow-up (in this same PR) moves the full federation
//! init pipeline (`init_from_env`, `wire_mount`,
//! `install_mount_coherence`, etc.) into this impl.

use std::sync::Arc;

use contracts::lock_state::Locks;
use kernel::abc::meta_store::MetaStore;
use kernel::hal::federation::{BlobFetcherSlot, FederationProvider, FederationResult};
use kernel::kernel::Kernel;

/// Raft-backed `FederationProvider` impl.
pub struct RaftFederationProvider;

impl FederationProvider for RaftFederationProvider {
    fn init_from_env(&self, _kernel: &Kernel) -> FederationResult<bool> {
        // Phase H follow-up: absorb the full init body (NEXUS_PEERS
        // parsing → ZoneManager construction → blob_fetcher_slot
        // stash) here.  Until then, federation init is no-op (returns
        // Ok(false) = "already initialized" so callers don't retry).
        Ok(false)
    }

    fn is_initialized(&self, kernel: &Kernel) -> bool {
        kernel.self_address_string().is_some()
    }

    fn bind_address(&self, kernel: &Kernel) -> Option<String> {
        kernel.self_address_string()
    }

    fn hostname(&self, kernel: &Kernel) -> Option<String> {
        kernel.self_address_string()
    }

    fn list_zones(&self, _kernel: &Kernel) -> Vec<String> {
        Vec::new()
    }

    fn metastore_for_zone(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
    ) -> FederationResult<Arc<dyn MetaStore>> {
        Err("metastore_for_zone: not yet wired through trait".into())
    }

    fn locks_for_zone(&self, _kernel: &Kernel, _zone_id: &str) -> FederationResult<Arc<dyn Locks>> {
        Err("locks_for_zone: not yet wired through trait".into())
    }

    fn wal_stream_for_zone(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _prefix: &str,
    ) -> FederationResult<Arc<dyn kernel::stream::StreamBackend>> {
        Err("wal_stream_for_zone: not yet wired through trait".into())
    }

    fn remote_read_blob(
        &self,
        kernel: &Kernel,
        _zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> FederationResult<Vec<u8>> {
        let client = kernel.peer_client_arc();
        let key = if !content_id.is_empty() {
            content_id
        } else {
            path
        };
        client.fetch("", key)
    }

    fn wire_mount(
        &self,
        _kernel: &Kernel,
        _parent_zone: &str,
        _mount_path: &str,
        _target_zone: &str,
    ) -> FederationResult<()> {
        Err("wire_mount: not yet wired through trait".into())
    }

    fn start_replication_scanner(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _policies_json: &str,
        _interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>> {
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
/// process from the cdylib boot path.  Idempotent — the kernel slot
/// is simply replaced so re-imports stay safe.
pub fn install(kernel: &Kernel) {
    kernel.set_federation(Arc::new(RaftFederationProvider));
}
