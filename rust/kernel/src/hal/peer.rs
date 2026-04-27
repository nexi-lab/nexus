//! `PeerBlobClient` HAL trait — abstract peer-blob fetch.
//!
//! Phase 1 introduces this trait so kernel code can hold an
//! `Arc<dyn PeerBlobClient>` rather than the concrete
//! `crate::peer_blob_client::PeerBlobClient` struct. That decoupling
//! is what lets Phase 4 move `peer_blob_client.rs` out of `kernel/src/`
//! into `transport/src/blob/peer_client.rs` without dragging the
//! kernel ↔ transport edge across the workspace twice.
//!
//! Linux analogue: this is an LSM-style hook (`security_operations`)
//! — kernel-defined extension surface that a parallel crate plugs
//! into, not a §3 ABC pillar.
//!
//! Until Phase 4 actually performs the move, the concrete
//! `crate::peer_blob_client::PeerBlobClient` impls this trait inside
//! the kernel crate; consumers (`crate::replication`,
//! `crate::kernel::Kernel`, `crate::raft_meta_store`, …) hold the
//! trait object so the Phase 4 swap is a no-op at the call sites.

/// Result type used by every peer-blob fetch method.
///
/// The error half is a string today because the existing concrete
/// `PeerBlobClient` returns `String` errors (gRPC status messages,
/// timeout descriptions, etc.). Once Phase 4 lands and we have a
/// stable error surface across the transport crate, this can be
/// upgraded to a structured `PeerBlobError` enum.
pub type PeerBlobResult<T> = Result<T, String>;

/// Abstract peer-blob fetch surface.
///
/// Implementors (today: `crate::peer_blob_client::PeerBlobClient`;
/// Phase 4: `transport::blob::peer_client::PeerBlobClient`) speak
/// gRPC to remote nodes and return the bytes for a path-keyed or
/// content-keyed blob.
///
/// `Send + Sync` so the `Arc<dyn PeerBlobClient>` can travel between
/// the kernel's tokio worker pool and the raft replication apply
/// task.
///
/// Methods stay narrowly typed (`Vec<u8>` for blob payloads; not
/// `Bytes` or `&[u8]`) so impls can either own the buffer (most
/// common) or arrange ownership through a copy. This matches the
/// existing `peer_blob_client::PeerBlobClient` surface verbatim.
pub trait PeerBlobClient: Send + Sync {
    /// Fetch a blob keyed by path from a remote peer (`addr` is
    /// `host:port`, the same string stored in
    /// `FileMetadata.last_writer_address`).
    ///
    /// Returns the blob bytes or an error string. Backed by
    /// `NexusVFSService.Read` on the remote node today.
    fn fetch_path(&self, addr: &str, path: &str) -> PeerBlobResult<Vec<u8>>;

    /// Fetch a CAS-addressed blob from a remote peer. `etag` is the
    /// content hash the remote stores under its CAS pillar.
    ///
    /// Returns the blob bytes or an error string. Default impl returns
    /// an error — concrete impls override; the default exists so
    /// trait objects can omit the method until the Phase 4 transport
    /// crate lands.
    fn fetch_etag(&self, addr: &str, etag: &str) -> PeerBlobResult<Vec<u8>> {
        let _ = (addr, etag);
        Err("fetch_etag not implemented for this PeerBlobClient".into())
    }
}
