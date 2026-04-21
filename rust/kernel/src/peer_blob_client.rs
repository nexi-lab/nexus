//! PeerBlobClient — shared gRPC infrastructure for CAS-level peer fetch.
//!
//! Owns a single multi-threaded tokio runtime plus a tonic `Channel` pool
//! (one per peer address) so every peer RPC reuses its channel instead of
//! building an HTTP/2 connection per call. Supersedes the one-shot
//! `tokio::runtime::Builder::new_current_thread()` that used to live inline in
//! `Kernel::try_remote_fetch` and the Python `nexus.remote.peer_blob_client`
//! module (which `R10f` deletes).
//!
//! The runtime is constructed once at `Kernel::new` and handed out as
//! `Arc<Runtime>`. `Kernel::shutdown` drops the owning Arc so tokio's
//! background workers shut down cleanly (addresses R11 hypothesis #2 — a
//! stuck tokio task blocking `docker stop`).
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
pub(crate) struct PeerBlobClient {
    runtime: Arc<tokio::runtime::Runtime>,
    channels: DashMap<String, tonic::transport::Channel>,
    per_peer_semaphores: DashMap<String, Arc<Semaphore>>,
    global_semaphore: Arc<Semaphore>,
    timeout: Duration,
    per_peer_permits: usize,
    /// R20.18.7: late-bound mTLS material. Populated by the kernel via
    /// `install_tls_config` once `init_federation_from_env` reads the
    /// on-disk `ca.pem` / `node.pem` / `node-key.pem` triplet. When
    /// present, peer channels are built as `https://…` with full mTLS
    /// (same cert material that `ZoneManager` uses for raft RPCs — one
    /// trust anchor per cluster). When absent, plaintext HTTP/2 — the
    /// docker federation test intentionally sets `NEXUS_RAFT_TLS=false`.
    tls: parking_lot::RwLock<Option<transport::TlsConfig>>,
}

#[allow(dead_code)]
impl PeerBlobClient {
    /// Build a peer-blob client backed by a shared runtime.
    pub(crate) fn new(runtime: Arc<tokio::runtime::Runtime>) -> Self {
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
    /// reconnects over TLS. Called from `Kernel::init_federation_from_env`
    /// once the leader / joiner has resolved the cluster CA + node
    /// cert.
    pub(crate) fn install_tls_config(&self, tls: transport::TlsConfig) {
        *self.tls.write() = Some(tls);
        self.channels.clear();
    }

    /// Exposed runtime handle — kernel-owned code paths (e.g. the migrated
    /// `try_remote_fetch`) call `runtime.handle().block_on(...)` to execute
    /// async work without reconstructing a runtime per call.
    pub(crate) fn runtime(&self) -> &Arc<tokio::runtime::Runtime> {
        &self.runtime
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
        let client_cfg = transport::ClientConfig {
            tls,
            ..Default::default()
        };
        let channel = transport::create_channel(&endpoint, &client_cfg)
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

    /// Fetch a blob (chunk or manifest) from `address` asynchronously.
    ///
    /// Returns `Err(..)` on transport errors OR when the peer reports
    /// `is_error=true` (blob not found on that peer).
    pub(crate) async fn fetch_blob_async(
        &self,
        address: &str,
        content_hash: &str,
    ) -> Result<Vec<u8>, String> {
        // Global cap: total concurrent chunk fetches across all peers.
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
        // R20.18.7: ReadBlob now lives on the raft `ZoneApiService`
        // (co-located with consensus on the advertised raft port —
        // inherits cluster mTLS). Message caps match the server:
        // tonic's default 4 MiB decode cap would reject any CAS
        // chunk above that threshold (16 MiB CDC boundary).
        // SSOT: `contracts::MAX_GRPC_MESSAGE_BYTES`.
        let mut client = ZoneApiServiceClient::new(channel)
            .max_decoding_message_size(contracts::MAX_GRPC_MESSAGE_BYTES)
            .max_encoding_message_size(contracts::MAX_GRPC_MESSAGE_BYTES);
        let mut request = tonic::Request::new(ReadBlobRequest {
            content_hash: content_hash.to_string(),
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

    /// Blocking sync wrapper — drives `fetch_blob_async` via the shared
    /// runtime. Safe to call from any thread.
    pub(crate) fn fetch_blob(&self, address: &str, content_hash: &str) -> Result<Vec<u8>, String> {
        let fut = self.fetch_blob_async(address, content_hash);
        self.runtime.block_on(fut)
    }
}

/// Build the kernel-owned multi-threaded runtime. Two workers is plenty for
/// IO-bound peer RPCs; increase only if a workload saturates both.
#[allow(dead_code)]
pub(crate) fn build_kernel_runtime() -> Arc<tokio::runtime::Runtime> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("nexus-kernel-peer")
        .enable_all()
        .build()
        .expect("failed to build kernel tokio runtime");
    Arc::new(rt)
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
    fn test_client_constructs_and_exposes_runtime() {
        let rt = build_kernel_runtime();
        let client = PeerBlobClient::new(Arc::clone(&rt));
        assert!(Arc::ptr_eq(client.runtime(), &rt));
    }

    #[test]
    fn test_fetch_blob_unreachable_peer_errors() {
        // Use a port we know is unbound so we test the error path without
        // needing a live peer. Short timeout = fast test.
        let rt = build_kernel_runtime();
        let mut client = PeerBlobClient::new(Arc::clone(&rt));
        client.timeout = Duration::from_millis(200);
        let result = client.fetch_blob(
            "127.0.0.1:1",
            "0000000000000000000000000000000000000000000000000000000000000000",
        );
        assert!(result.is_err());
    }
}
