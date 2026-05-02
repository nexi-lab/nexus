//! Federation syscalls + Control-Plane HAL §3.B.1 wiring.
//!
//! Per-family submodule extracted from the monolithic `kernel/mod.rs`
//! (consistent with other syscall-family submodules `io.rs`, `ipc.rs`,
//! `locks.rs`, `dispatch.rs`, `observability.rs`). Methods stay
//! members of [`Kernel`] via `impl Kernel { ... }` blocks — the split
//! is a file-organization change, not an API change.
//!
//! Owns:
//!
//! * `DistributedCoordinator` slot accessors
//!   ([`Kernel::distributed_coordinator`],
//!   [`Kernel::set_distributed_coordinator`]).
//! * `/__sys__/zones/` procfs synthesisers
//!   ([`Kernel::zones_procfs_stat`], [`Kernel::zones_procfs_readdir`]).
//! * Blob-fetcher slot plumbing — boot-time stash for the raft-tier
//!   handler to drain ([`Kernel::stash_blob_fetcher_slot`],
//!   [`Kernel::take_pending_blob_fetcher_slot`]).

use std::sync::Arc;

use super::{Kernel, StatResult};

impl Kernel {
    /// Replace the kernel's coordinator slot with a concrete
    /// `DistributedCoordinator` impl. Kernel boots with
    /// `NoopDistributedCoordinator`; the cdylib boot path calls this
    /// with the real `nexus_raft::distributed_coordinator` impl once
    /// per kernel. Mirrors `set_peer_client`.
    pub fn set_distributed_coordinator(
        &self,
        coordinator: Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator>,
    ) {
        *self.distributed_coordinator.write() = coordinator;
    }

    /// Borrow the current distributed coordinator — read-locked snapshot.
    /// Internal callers use this to issue federation calls without
    /// holding the lock across `.await`. After `set_distributed_coordinator`
    /// runs (cdylib boot), this returns the real raft-backed impl;
    /// before then, a `NoopDistributedCoordinator` that errors on every
    /// call.
    pub fn distributed_coordinator(
        &self,
    ) -> Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator> {
        Arc::clone(&self.distributed_coordinator.read())
    }

    /// Federation procfs: synthesise a `StatResult` for paths under the
    /// `/__sys__/zones/` virtual namespace.  Read-only — like Linux
    /// `/proc`, callers cannot create / remove a zone by writing to
    /// this path.  Returns `Some` for `/__sys__/zones/` (directory
    /// marker) and `/__sys__/zones/<id>` (per-zone synthesised entry);
    /// `None` otherwise so the caller falls through to normal routing.
    pub(crate) fn zones_procfs_stat(&self, path: &str) -> Option<StatResult> {
        let suffix = path.strip_prefix("/__sys__/zones")?;
        let provider = self.distributed_coordinator();
        // Directory marker.
        if suffix.is_empty() || suffix == "/" {
            return Some(StatResult {
                path: path.to_string(),
                size: 4096,
                content_id: None,
                mime_type: "inode/directory".to_string(),
                is_directory: true,
                entry_type: crate::dcache::DT_DIR,
                mode: 0o555, // r-x — read-only namespace
                version: 0,
                zone_id: Some("root".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                lock: None,
                link_target: None,
            });
        }
        // /__sys__/zones/<id>: synthesise from federation list.
        let zone_id = suffix.trim_start_matches('/');
        if zone_id.is_empty() || zone_id.contains('/') {
            return None;
        }
        if !provider.list_zones(self).iter().any(|z| z == zone_id) {
            return None;
        }
        Some(StatResult {
            path: path.to_string(),
            size: 0,
            content_id: None,
            mime_type: "application/x-nexus-zone".to_string(),
            is_directory: false,
            entry_type: crate::dcache::DT_REG,
            mode: 0o444,
            version: 0,
            zone_id: Some(zone_id.to_string()),
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: self.self_address_string(),
            lock: None,
            link_target: None,
        })
    }

    /// Federation procfs: list zones for `/__sys__/zones/` directory
    /// reads.  Returns `None` for paths outside the namespace so the
    /// caller falls through to normal routing.
    pub(crate) fn zones_procfs_readdir(&self, path: &str) -> Option<Vec<String>> {
        let suffix = path.strip_prefix("/__sys__/zones")?;
        if !suffix.is_empty() && suffix != "/" {
            return None;
        }
        Some(self.distributed_coordinator().list_zones(self))
    }

    /// Stash the raft-tier blob-fetcher slot. Drained by
    /// `nexus_raft::blob_fetcher_handler::install` at cdylib boot.
    /// Typed as `Box<dyn Any>` so kernel does not name the raft-side
    /// `BlobFetcherSlot` concrete type.
    pub fn stash_blob_fetcher_slot(&self, slot: Box<dyn std::any::Any + Send + Sync>) {
        *self.pending_blob_fetcher_slot.lock() = Some(slot);
    }

    /// Drain the previously stashed blob-fetcher slot. Returns `None`
    /// after the first drain so re-imports of the cdylib stay safe.
    pub fn take_pending_blob_fetcher_slot(&self) -> Option<Box<dyn std::any::Any + Send + Sync>> {
        self.pending_blob_fetcher_slot.lock().take()
    }
}
