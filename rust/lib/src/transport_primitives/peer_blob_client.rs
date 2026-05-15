//! `PeerBlobClient` — transport-layer abstraction for peer-blob fetch.
//!
//! Lowest shared layer: kernel holds an `Arc<dyn PeerBlobClient>`
//! through the peer_client slot, raft holds it through the blob
//! fetcher path, rpc impls it on the client side. Living in
//! `shared/transport-primitives` lets every consumer name the trait
//! without closing a Cargo cycle through any peer crate.
//!
//! Store-and-forward: the trait carries a single ``fetch`` method
//! taking an opaque ``content_id`` string. The caller does not
//! pre-classify whether ``content_id`` is a VFS path, a CAS hash, or
//! some backend-specific handle — bytes forward to the peer, whose
//! own data-plane (VFSRouter / CAS pillar) resolves them.

/// Result type used by the peer-blob fetch method.
///
/// String errors carry gRPC status messages and timeout descriptions
/// verbatim from the underlying tonic client.
pub type PeerBlobResult<T> = Result<T, String>;

/// Abstract peer-blob fetch surface.
///
/// Reference impl: `rpc::peer_blob::PeerBlobClient` — speaks gRPC to
/// remote nodes and returns the bytes for an opaque ``content_id``.
///
/// `Send + Sync` so the `Arc<dyn PeerBlobClient>` can travel between
/// the kernel's tokio worker pool and the raft replication apply
/// task.
///
/// Methods stay narrowly typed (`Vec<u8>` for blob payloads; not
/// `Bytes` or `&[u8]`) so impls can either own the buffer (most
/// common) or arrange ownership through a copy.
pub trait PeerBlobClient: Send + Sync {
    /// Fetch a blob from a remote peer (`addr` is `host:port`, the
    /// same string stored in `FileMetadata.last_writer_address`).
    ///
    /// `content_id` is opaque — VFS path for federation reads, CAS
    /// hash for chunk-dedup pulls, or a backend-specific handle. The
    /// peer's own data-plane resolves it.
    ///
    /// Returns the blob bytes or an error string.
    fn fetch(&self, addr: &str, content_id: &str) -> PeerBlobResult<Vec<u8>>;

    /// Install TLS config (PEM bundle). Default impl no-ops so
    /// non-TLS callers (tests, Noop fallback) skip the burden.
    /// Production rpc-tier client impl overrides.
    fn install_tls(&self, _ca_pem: &[u8], _cert_pem: Option<&[u8]>, _key_pem: Option<&[u8]>) {}
}

/// No-op fallback used at `Kernel::new` so the `peer_client` field
/// always carries an `Arc<dyn PeerBlobClient>` — non-cdylib Rust tests
/// and WASM builds keep the same call shape. Each method errors out
/// with "PeerBlobClient not installed"; the cdylib's transport-tier
/// install hook replaces this with the real rpc-side impl.
pub struct NoopPeerBlobClient;

impl PeerBlobClient for NoopPeerBlobClient {
    fn fetch(&self, _addr: &str, _content_id: &str) -> PeerBlobResult<Vec<u8>> {
        Err("PeerBlobClient not installed (non-cdylib build)".into())
    }
}

impl NoopPeerBlobClient {
    pub fn arc() -> Arc<dyn PeerBlobClient> {
        Arc::new(NoopPeerBlobClient)
    }
}

use std::sync::Arc;
