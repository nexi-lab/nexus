//! BlobFetcher — trait abstracting CAS blob reads so the Raft gRPC
//! server can serve `ReadBlob` without depending on kernel types.
//!
//! The driver-to-driver `ReadBlob` RPC is co-located with
//! `ZoneApiService` on the raft port. The kernel crate provides the
//! implementation (wired over `VFSRouter`'s root backend); the raft
//! crate only sees this trait.

#![cfg(all(feature = "grpc", has_protos))]

use std::sync::Arc;

/// Peer-facing content read (store-and-forward).
///
/// One addressing mode: ``content_id`` is opaque to the kernel and the
/// raft transport. Each ``BlobFetcher`` impl interprets it however its
/// underlying backend does — for the kernel impl that means routing
/// ``content_id`` (a global VFS path) through the local ``VFSRouter``
/// exactly like a local ``sys_read``, so CAS backends see a hash and
/// PAS backends see a path without the kernel ever picking the branch.
#[tonic::async_trait]
pub trait BlobFetcher: Send + Sync {
    /// Return the raw bytes for ``content_id`` or a ``String`` error
    /// (e.g. ``"not found"``). Transport framing is the caller's job.
    async fn read(&self, content_id: &str) -> Result<Vec<u8>, String>;
}

/// Late-bindable slot for the fetcher.
///
/// `ZoneManager::new` constructs the gRPC server before the kernel has
/// its root mount backend ready, so the slot is created empty and the
/// kernel installs a `BlobFetcher` later via `install`. Lock-free reads
/// on the hot path; `parking_lot::RwLock` keeps the writer side cheap
/// and re-entrant-safe.
pub type BlobFetcherSlot = Arc<parking_lot::RwLock<Option<Arc<dyn BlobFetcher>>>>;

/// Construct an unbound slot. Equivalent to
/// `Arc::new(parking_lot::RwLock::new(None))` but spelt once in the
/// trait module so callers don't have to import parking_lot.
pub fn new_blob_fetcher_slot() -> BlobFetcherSlot {
    Arc::new(parking_lot::RwLock::new(None))
}
