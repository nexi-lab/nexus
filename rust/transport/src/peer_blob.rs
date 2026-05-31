//! PeerBlobClient — shared gRPC infrastructure for CAS-level peer fetch.
//!
//! Holds a `tokio::runtime::Handle` (not an `Arc<Runtime>`) plus a
//! tonic `Channel` pool (one per peer address) so every peer RPC
//! reuses its channel instead of building an HTTP/2 connection per
//! call.
//!
//! Runtime ownership lives with `Kernel` — `PeerBlobClient` only
//! borrows the executor via the `Handle`.  Dropping this client never
//! triggers a runtime shutdown, so the client can safely outlive the
//! kernel and drop in any context (including from an async worker
//! during process shutdown).  When the kernel drops its
//! `Arc<Runtime>` the `Handle` here invalidates on next use; that's
//! the correct ownership shape — peer fetches must not survive their
//! runtime.
//!
//! Thread-safety: `DashMap` guards the channel pool; per-peer + global
//! semaphores cap concurrent RPCs.

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use tokio::sync::Semaphore;

use nexus_raft::transport::proto::nexus::raft::{
    zone_api_service_client::ZoneApiServiceClient, ReadBlobRequest,
};

/// Default per-peer permit count — caps outstanding RPCs per peer so one
/// slow origin cannot monopolise the client. 8 matches Python
/// `CASRemoteContentFetcher`'s default worker count.
const DEFAULT_PER_PEER_PERMITS: usize = 8;
/// Default global permit count — caps total concurrent blob fetches to keep
/// aggregate outbound bandwidth bounded on small nodes.
const DEFAULT_GLOBAL_PERMITS: usize = 16;
/// Default per-RPC timeout. Matches Python `PeerBlobClient.timeout` default.
const DEFAULT_RPC_TIMEOUT: Duration = Duration::from_secs(30);

/// Shared peer-RPC client. Construct once per kernel, clone the `Arc` into
/// any caller that needs to fetch blobs from peers.
#[allow(dead_code)]
pub struct PeerBlobClient {
    runtime: tokio::runtime::Handle,
    channels: DashMap<String, tonic::transport::Channel>,
    per_peer_semaphores: DashMap<String, Arc<Semaphore>>,
    global_semaphore: Arc<Semaphore>,
    timeout: Duration,
    per_peer_permits: usize,
    /// Late-bound mTLS material. Populated by the kernel via
    /// `install_tls_config` once the leader / joiner has resolved the
    /// on-disk `ca.pem` / `node.pem` / `node-key.pem` triplet. When
    /// present, peer channels are built as `https://…` with full mTLS
    /// (same cert material that `ZoneManager` uses for raft RPCs — one
    /// trust anchor per cluster). When absent, plaintext HTTP/2 — the
    /// docker federation test intentionally sets `NEXUS_RAFT_TLS=false`.
    tls: parking_lot::RwLock<Option<lib::transport_primitives::TlsConfig>>,
}

#[allow(dead_code)]
impl PeerBlobClient {
    /// Build a peer-blob client borrowing the executor via `Handle`.
    ///
    /// The kernel owns the runtime; this client only needs spawn /
    /// block_on access.  Holding a `Handle` rather than `Arc<Runtime>`
    /// keeps runtime shutdown a kernel-side responsibility and makes
    /// drops here side-effect-free.
    pub fn new(runtime: tokio::runtime::Handle) -> Self {
        Self {
            runtime,
            channels: DashMap::new(),
            per_peer_semaphores: DashMap::new(),
            global_semaphore: Arc::new(Semaphore::new(DEFAULT_GLOBAL_PERMITS)),
            timeout: DEFAULT_RPC_TIMEOUT,
            per_peer_permits: DEFAULT_PER_PEER_PERMITS,
            tls: parking_lot::RwLock::new(None),
        }
    }

    /// Install mTLS material so subsequent channel builds use TLS.
    ///
    /// Drops any cached plaintext channels — the next RPC to each peer
    /// reconnects over TLS. Called by the boot installer once the
    /// leader / joiner has resolved the cluster CA + node cert.
    pub fn install_tls_config(&self, tls: lib::transport_primitives::TlsConfig) {
        *self.tls.write() = Some(tls);
        self.channels.clear();
    }

    /// Fetch or build a tonic `Channel` for `address`.
    ///
    /// `tonic::transport::Channel` is `Clone` and internally reference-counted
    /// (wraps a `tower` service). We cache one per peer so concurrent callers
    /// share a single HTTP/2 connection.
    async fn channel_for(&self, address: &str) -> Result<tonic::transport::Channel, String> {
        if let Some(ch) = self.channels.get(address) {
            return Ok(ch.clone());
        }
        let tls = self.tls.read().clone();
        let scheme = if tls.is_some() { "https" } else { "http" };
        let endpoint = if address.starts_with("http://") || address.starts_with("https://") {
            address.to_string()
        } else {
            format!("{}://{}", scheme, address)
        };
        let client_cfg = lib::transport_primitives::ClientConfig {
            tls,
            ..Default::default()
        };
        let channel = lib::transport_primitives::create_channel(&endpoint, &client_cfg)
            .await
            .map_err(|e| format!("peer channel {}: {}", address, e))?;
        self.channels
            .entry(address.to_string())
            .or_insert_with(|| channel.clone());
        Ok(channel)
    }

    /// Resolve (or create) the per-peer semaphore gating outstanding RPCs.
    fn per_peer_semaphore(&self, address: &str) -> Arc<Semaphore> {
        if let Some(s) = self.per_peer_semaphores.get(address) {
            return Arc::clone(&s);
        }
        let entry = self
            .per_peer_semaphores
            .entry(address.to_string())
            .or_insert_with(|| Arc::new(Semaphore::new(self.per_peer_permits)));
        Arc::clone(&entry)
    }

    /// Fetch content from ``address`` asynchronously, using the
    /// store-and-forward ``ReadBlob`` RPC: send opaque ``content_id``,
    /// receive bytes. The peer's data plane decides locally whether
    /// ``content_id`` is a VFS path (federation read) or a CAS hash
    /// (chunk dedup) — caller doesn't care.
    ///
    /// Returns ``Err(..)`` on transport errors OR when the peer reports
    /// ``error`` (content not found / not routable on that peer).
    pub(crate) async fn fetch_async(
        &self,
        address: &str,
        content_id: &str,
    ) -> Result<Vec<u8>, String> {
        // Global cap: total concurrent fetches across all peers.
        let _global_permit = self
            .global_semaphore
            .clone()
            .acquire_owned()
            .await
            .map_err(|e| format!("global semaphore closed: {e}"))?;
        // Per-peer cap: one peer cannot monopolise the pool.
        let per_peer = self.per_peer_semaphore(address);
        let _peer_permit = per_peer
            .acquire_owned()
            .await
            .map_err(|e| format!("per-peer semaphore closed: {e}"))?;

        let channel = self.channel_for(address).await?;
        // ReadBlob lives on the raft ``ZoneApiService`` (co-located
        // with consensus on the advertised raft port — inherits
        // cluster mTLS). Message caps match the server: tonic's
        // default 4 MiB decode cap would reject any CAS chunk above
        // that threshold (16 MiB CDC boundary).
        // SSOT: ``contracts::MAX_GRPC_MESSAGE_BYTES``.
        let mut client = ZoneApiServiceClient::new(channel)
            .max_decoding_message_size(contracts::MAX_GRPC_MESSAGE_BYTES)
            .max_encoding_message_size(contracts::MAX_GRPC_MESSAGE_BYTES);
        let mut request = tonic::Request::new(ReadBlobRequest {
            content_id: content_id.to_string(),
        });
        request.set_timeout(self.timeout);

        let resp = client
            .read_blob(request)
            .await
            .map_err(|e| format!("ReadBlob {}: {}", address, e))?
            .into_inner();
        if !resp.error.is_empty() {
            return Err(format!("ReadBlob {} error: {}", address, resp.error));
        }
        Ok(resp.content)
    }

    /// Blocking sync wrapper — drives ``fetch_async`` via the shared
    /// runtime. Safe to call from any thread.
    pub(crate) fn fetch(&self, address: &str, content_id: &str) -> Result<Vec<u8>, String> {
        let fut = self.fetch_async(address, content_id);
        self.runtime.block_on(fut)
    }
}

/// Build the kernel-owned multi-threaded runtime. Two workers is plenty for
/// IO-bound peer RPCs; increase only if a workload saturates both.
#[allow(dead_code)]
pub fn build_kernel_runtime() -> Arc<tokio::runtime::Runtime> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("nexus-kernel-peer")
        .enable_all()
        .build()
        .expect("failed to build kernel tokio runtime");
    Arc::new(rt)
}

// ── HAL trait wiring ─────────────────────────────────────────────────
//
// Kernel holds an `Arc<dyn kernel::hal::peer::PeerBlobClient>`. The
// impl block adapts the inherent `fetch` / `install_tls_config`
// methods to the trait's `fetch` / `install_tls` shape.

impl kernel::hal::peer::PeerBlobClient for PeerBlobClient {
    fn fetch(&self, addr: &str, content_id: &str) -> kernel::hal::peer::PeerBlobResult<Vec<u8>> {
        // Inherent method shadows the trait method — fully-qualify.
        PeerBlobClient::fetch(self, addr, content_id)
    }

    fn install_tls(&self, ca_pem: &[u8], cert_pem: Option<&[u8]>, key_pem: Option<&[u8]>) {
        // mTLS requires *both* a client cert and key — if either is
        // missing the trait caller is in CA-only / server-auth mode,
        // which the underlying `lib::transport_primitives::TlsConfig`
        // does not yet model (its `cert_pem`/`key_pem` are `Vec<u8>`,
        // not `Option<Vec<u8>>`). Drop the install in that case so the
        // peer client stays plaintext rather than constructing an
        // invalid mTLS bundle with empty client cert/key.
        let (Some(cert), Some(key)) = (cert_pem, key_pem) else {
            tracing::warn!(
                target = "peer_blob_client",
                "install_tls called without cert_pem+key_pem; staying plaintext (CA-only mTLS not yet supported)",
            );
            return;
        };
        PeerBlobClient::install_tls_config(
            self,
            lib::transport_primitives::TlsConfig {
                ca_pem: ca_pem.to_vec(),
                cert_pem: cert.to_vec(),
                key_pem: key.to_vec(),
            },
        );
    }
}

/// Install hook called during kernel process boot —
/// constructs a `PeerBlobClient` that borrows the kernel's tokio
/// runtime via `Handle` and installs it via `Kernel::set_peer_client`,
/// replacing the `NoopPeerBlobClient` default.
pub fn install(kernel: &kernel::kernel::Kernel) {
    let client = Arc::new(PeerBlobClient::new(kernel.runtime().handle().clone()));
    kernel.set_peer_client(client as Arc<dyn kernel::hal::peer::PeerBlobClient>);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_runtime_succeeds() {
        let rt = build_kernel_runtime();
        // Can drive a simple future.
        rt.block_on(async { 1 + 1 });
        assert!(Arc::strong_count(&rt) >= 1);
    }

    #[test]
    fn test_client_constructs_with_handle() {
        let rt = build_kernel_runtime();
        let client = PeerBlobClient::new(rt.handle().clone());
        // Smoke: drives a trivial future through the borrowed Handle.
        let _ = client
            .runtime
            .block_on(async { 1 + 1 });
    }

    #[test]
    fn test_fetch_unreachable_peer_errors() {
        // Use a port we know is unbound so we test the error path without
        // needing a live peer. Short timeout = fast test.
        let rt = build_kernel_runtime();
        let mut client = PeerBlobClient::new(rt.handle().clone());
        client.timeout = Duration::from_millis(200);
        let result = client.fetch(
            "127.0.0.1:1",
            "0000000000000000000000000000000000000000000000000000000000000000",
        );
        assert!(result.is_err());
    }
}
