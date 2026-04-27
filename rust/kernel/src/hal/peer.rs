//! `PeerBlobClient` HAL trait — abstract peer-blob fetch.
//!
//! Kernel code holds an `Arc<dyn PeerBlobClient>` rather than a
//! concrete struct so the implementation can live in a peer crate
//! (`transport::blob::peer_client::PeerBlobClient`) without closing a
//! Cargo cycle through the kernel rlib.
//!
//! Linux analogue: an LSM-style hook (`security_operations`) — a
//! kernel-defined extension surface that a parallel crate plugs into,
//! sitting alongside but separate from the §3 ABC pillars
//! (ObjectStore / MetaStore / CacheStore).
//!
//! Consumers (`crate::replication`, `crate::kernel::Kernel`,
//! `crate::raft_meta_store`) reach the implementor through this
//! trait object exclusively.

/// Result type used by every peer-blob fetch method.
///
/// String errors carry gRPC status messages and timeout descriptions
/// verbatim from the underlying tonic client.
pub type PeerBlobResult<T> = Result<T, String>;

/// Abstract peer-blob fetch surface.
///
/// Implementor: `transport::blob::peer_client::PeerBlobClient` —
/// speaks gRPC to remote nodes and returns the bytes for a path-keyed
/// or content-keyed blob.
///
/// `Send + Sync` so the `Arc<dyn PeerBlobClient>` can travel between
/// the kernel's tokio worker pool and the raft replication apply
/// task.
///
/// Methods stay narrowly typed (`Vec<u8>` for blob payloads; not
/// `Bytes` or `&[u8]`) so impls can either own the buffer (most
/// common) or arrange ownership through a copy.
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

    /// Install TLS config (PEM bundle).  Default impl no-ops so
    /// non-TLS callers (tests, Noop fallback) don't carry the
    /// burden.  Production `transport::blob::peer_client::PeerBlobClient`
    /// overrides.
    fn install_tls(&self, _ca_pem: &[u8], _cert_pem: Option<&[u8]>, _key_pem: Option<&[u8]>) {}
}

/// No-op fallback used at `Kernel::new` so the `peer_client` field is
/// never `None` — non-cdylib Rust tests / WASM builds keep the same
/// call shape.  Always errors out (no peer available); the cdylib's
/// `nexus_cdylib::install_transport(&kernel)` boot path replaces this
/// with the real `transport::blob::peer_client::PeerBlobClient`.
pub struct NoopPeerBlobClient;

impl PeerBlobClient for NoopPeerBlobClient {
    fn fetch_path(&self, _addr: &str, _path: &str) -> PeerBlobResult<Vec<u8>> {
        Err("PeerBlobClient not installed (non-cdylib build)".into())
    }
}

impl NoopPeerBlobClient {
    pub fn arc() -> Arc<dyn PeerBlobClient> {
        Arc::new(NoopPeerBlobClient)
    }
}

use std::sync::Arc;
