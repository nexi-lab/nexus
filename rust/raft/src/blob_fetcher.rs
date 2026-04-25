//! BlobFetcher — trait abstracting CAS blob reads so the Raft gRPC
//! server can serve `ReadBlob` without depending on kernel types.
//!
//! R20.18.7 co-locates the driver-to-driver `ReadBlob` RPC with
//! `ZoneApiService` on the raft port. The kernel crate provides the
//! implementation (wired over `VFSRouter`'s root backend); the raft
//! crate only sees this trait.

#![cfg(all(feature = "grpc", has_protos))]

use std::sync::Arc;

/// Peer-facing blob read.
///
/// Two addressing modes:
///   - `read_blob(content_hash)` resolves the hash against local CAS
///     backends — used for chunk fetch and CAS-backed federation.
///   - `read_path(path)` drives the local VFSRouter at that global path
///     — used for PAS-backed federation where the writer's file lives
///     on disk / S3 / … under a path-addressed mount.
#[tonic::async_trait]
pub trait BlobFetcher: Send + Sync {
    /// Return the raw blob bytes for `content_hash` or a `String` error
    /// (e.g. `"not found"`). Transport framing is the caller's job.
    async fn read_blob(&self, content_hash: &str) -> Result<Vec<u8>, String>;

    /// Serve a peer's path-addressed read by routing through the local
    /// VFSRouter. Used for federation cross-node reads when the writer's
    /// mount is path-addressed — the reader has no content hash, so it
    /// asks the writer to read its own local file by path.
    async fn read_path(&self, path: &str) -> Result<Vec<u8>, String>;
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
