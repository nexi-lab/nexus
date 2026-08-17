//! Federation dispatcher — fan a Query out to peer plugins,
//! collect their responses, and merge the ranked lists.
//!
//! # Where it plugs in
//!
//! `SearchServiceImpl::query` checks [`PeerRegistry::is_active`]
//! after the empty-q gate; if federation is on AND `zone_id` is in
//! the allowlist, the dispatcher runs alongside the local Query
//! (both branches fire concurrently via `tokio::join!`), then fuses
//! the union with `fusion::rrf_multi`.  When federation is off or
//! the zone isn't allowlisted, the wrapper is a straight no-op — the
//! current local-only pipeline runs unchanged.
//!
//! # Failure posture
//!
//! Per-peer failures are LOGGED as warnings and the offending peer
//! drops out of the fusion.  A total-federation-outage (every peer
//! dead) returns local-only results — federation must never make a
//! query WORSE than the pre-federation baseline.
//!
//! # Transport
//!
//! `tonic::transport::Channel` per peer, built on first use and
//! cached.  A dead peer's channel stays cached — reconnection is
//! `tonic`'s job under the hood.  TLS is opt-in via
//! `NEXUS_SEARCH_PEER_TLS=true`; plaintext to a non-loopback peer is
//! REFUSED unless `NEXUS_SEARCH_ALLOW_INSECURE_PEER=true` per the
//! standing "refuse plaintext off-loopback" rule.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};

use crate::peer_registry::{PeerAddress, PeerRegistry};
use crate::search_proto::search_service_client::SearchServiceClient;
use crate::search_proto::{QueryRequest, QueryResponse, QueryResult};

/// gRPC metadata header set on outgoing peer fan-out requests.
/// Receiving plugins skip federation when this header is present,
/// so a two-node fleet cannot loop forever with each node fanning
/// its own query out to the other.  Header is opaque to gRPC —
/// tonic lower-cases and forwards it transparently.
pub const FEDERATION_MARKER_HEADER: &str = "x-nexus-search-federated";

/// Per-peer dial timeout — the connect side of the gRPC channel.
/// Kept tight so a hosed peer stops the federation fan-out fast
/// instead of stretching the caller's p99.
const CONNECT_TIMEOUT: Duration = Duration::from_millis(1_500);

/// Per-peer request timeout — the send + response wait side.
/// Larger than CONNECT so a legitimately heavy semantic query has
/// room to complete, but still bounded to keep the fan-out latency
/// predictable.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(8);

/// Errors surfaced to callers of [`FederationDispatcher::query`].
/// Kept simple — all callers log-and-drop, so the enum is a debugging
/// aid rather than a control-flow signal.
#[derive(Debug, thiserror::Error)]
pub enum FederationError {
    /// A peer was configured with a scheme/URL that tonic couldn't
    /// turn into a valid Endpoint.  Signals a misconfig — treat as
    /// permanent (this peer will never dial) but keep the rest of
    /// the federation alive.
    #[error("bad peer endpoint {peer}: {source}")]
    BadEndpoint {
        peer: String,
        #[source]
        source: tonic::transport::Error,
    },

    /// Plaintext dial to a non-loopback peer, with no explicit opt-in
    /// via `NEXUS_SEARCH_ALLOW_INSECURE_PEER=true`.  Per the standing
    /// TLS rule.
    #[error(
        "refusing plaintext dial to non-loopback peer {peer} — set \
         NEXUS_SEARCH_PEER_TLS=true (recommended) or \
         NEXUS_SEARCH_ALLOW_INSECURE_PEER=true to bypass"
    )]
    PlaintextOffLoopback { peer: String },

    /// Transport / gRPC error at request time.  Peer offline, dial
    /// timeout, TLS handshake failure — anything the runtime layer
    /// throws.
    #[error("peer {peer} unreachable: {source}")]
    Unreachable {
        peer: String,
        #[source]
        source: tonic::Status,
    },

    /// Connect-side transport error (before the RPC leaves the wire).
    /// Separate variant so callers can distinguish "connection setup"
    /// from "server responded with an error" in logs.
    #[error("peer {peer} connect failed: {source}")]
    ConnectFailed {
        peer: String,
        #[source]
        source: tonic::transport::Error,
    },
}

/// Live federation dispatcher — one per [`SearchServiceImpl`].
/// Cheap to construct; expensive work (dialing peers, holding
/// channels) is lazy and cached.
pub struct FederationDispatcher {
    registry: PeerRegistry,
    channels: Mutex<HashMap<PeerAddress, Channel>>,
}

impl FederationDispatcher {
    /// Wrap a registry.  No I/O happens here.
    pub fn new(registry: PeerRegistry) -> Self {
        Self {
            registry,
            channels: Mutex::new(HashMap::new()),
        }
    }

    /// Whether federation should run for the given zone.  Hot-path
    /// callers use this to skip the whole dispatcher when the query
    /// is against a local-only zone.
    pub fn should_federate(&self, zone_id: &str) -> bool {
        self.registry.is_active() && self.registry.zone_is_federated(zone_id)
    }

    /// Registry accessor for tests that need to inspect config.
    #[cfg(test)]
    pub fn registry(&self) -> &PeerRegistry {
        &self.registry
    }

    /// Send `req` to every peer in the registry, in parallel.
    /// Returns one `QueryResponse` per SUCCESSFULLY reached peer;
    /// unreachable peers log a warning and drop out of the returned
    /// vec entirely.  An empty return value = every peer failed;
    /// callers treat that as "local-only" and continue.
    pub async fn fan_out(&self, req: &QueryRequest) -> Vec<QueryResponse> {
        let peers = self.registry.peers().to_vec();
        if peers.is_empty() {
            return Vec::new();
        }
        let mut handles = Vec::with_capacity(peers.len());
        for peer in peers {
            let req_clone = req.clone();
            let peer_url_for_log = format!("{}:{}", peer.host, peer.port);
            let channel = match self.get_or_dial(&peer).await {
                Ok(c) => c,
                Err(e) => {
                    tracing::warn!(
                        peer = %peer_url_for_log,
                        err = %e,
                        "search-plugin federation: peer connect failed — dropping from fusion",
                    );
                    continue;
                }
            };
            handles.push(tokio::spawn(async move {
                let mut client =
                    SearchServiceClient::new(channel).max_decoding_message_size(64 * 1024 * 1024);
                let mut request = tonic::Request::new(req_clone);
                request.set_timeout(REQUEST_TIMEOUT);
                // Stamp the federation-marker header so the receiving
                // plugin's query() wrapper skips its own federation
                // fan-out — a fleet must not loop.
                if let Ok(v) = tonic::metadata::MetadataValue::try_from("1") {
                    request.metadata_mut().insert(FEDERATION_MARKER_HEADER, v);
                }
                let resp = client.query(request).await;
                (peer_url_for_log, resp)
            }));
        }
        let mut out = Vec::with_capacity(handles.len());
        for h in handles {
            match h.await {
                Ok((_peer, Ok(resp))) => out.push(resp.into_inner()),
                Ok((peer, Err(status))) => {
                    tracing::warn!(
                        peer = %peer,
                        err = %status,
                        "search-plugin federation: peer RPC failed — dropping from fusion",
                    );
                }
                Err(join) => {
                    tracing::warn!(
                        err = %join,
                        "search-plugin federation: peer task joined with error",
                    );
                }
            }
        }
        out
    }

    /// Lazy channel lookup — dial once per peer, reuse the Channel
    /// forever.  Locking is coarse (a single Mutex around the whole
    /// map) but the critical section is HashMap lookup + optional
    /// build, both microsecond-scale; per-peer sharding is over-kill
    /// for the ≤ handful of peers a federation typically has.
    async fn get_or_dial(&self, peer: &PeerAddress) -> Result<Channel, FederationError> {
        {
            let g = self.channels.lock();
            if let Some(c) = g.get(peer) {
                return Ok(c.clone());
            }
        }
        let channel = self.dial(peer).await?;
        let mut g = self.channels.lock();
        // Race — another task may have dialled while we were awaiting.
        if let Some(existing) = g.get(peer) {
            return Ok(existing.clone());
        }
        g.insert(peer.clone(), channel.clone());
        Ok(channel)
    }

    /// Build a Channel to `peer`, gated by the TLS + loopback rules.
    async fn dial(&self, peer: &PeerAddress) -> Result<Channel, FederationError> {
        let tls = self.registry.require_tls();
        // Standing rule — plaintext to a non-loopback peer is
        // REFUSED unless explicitly opted out.  The escape hatch
        // exists because tests + short-lived dev clusters on the
        // loopback interface must not fight the gate.
        if !tls && !peer.is_loopback() && !self.registry.allow_insecure_peer() {
            return Err(FederationError::PlaintextOffLoopback {
                peer: format!("{}:{}", peer.host, peer.port),
            });
        }
        let url = peer.url(tls);
        let mut endpoint =
            Endpoint::from_shared(url.clone()).map_err(|e| FederationError::BadEndpoint {
                peer: url.clone(),
                source: e,
            })?;
        endpoint = endpoint
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(REQUEST_TIMEOUT)
            .tcp_keepalive(Some(Duration::from_secs(30)));
        if tls {
            // `with_enabled_roots` opts in to the platform trust
            // roots enabled at build time (webpki-roots on tls-ring)
            // — the same posture the wider tonic 0.14 tree uses for
            // outbound clients on this repo.
            let tls_config = ClientTlsConfig::new().with_enabled_roots();
            endpoint =
                endpoint
                    .tls_config(tls_config)
                    .map_err(|e| FederationError::BadEndpoint {
                        peer: url.clone(),
                        source: e,
                    })?;
        }
        let channel = endpoint
            .connect()
            .await
            .map_err(|e| FederationError::ConnectFailed {
                peer: url,
                source: e,
            })?;
        Ok(channel)
    }
}

/// Merge a set of ranked lists from local + peers.  Wraps the
/// existing [`crate::fusion::rrf_multi`] with the small mapping
/// glue federation needs (each source list is a full [`QueryResult`]
/// arm; every arm registers as `ArmKind::Chunk` since federation
/// does not distinguish title vs body — that's a within-node fusion
/// concern).
pub fn merge_ranked(
    lists: &[Vec<QueryResult>],
    rrf_k: u32,
    chunks_per_page: u32,
    limit: usize,
) -> Vec<QueryResult> {
    if lists.is_empty() {
        return Vec::new();
    }
    let arms: Vec<(crate::fusion::ArmKind, &[QueryResult])> = lists
        .iter()
        .map(|l| (crate::fusion::ArmKind::Chunk, l.as_slice()))
        .collect();
    let mut fused = crate::fusion::rrf_multi(&arms, rrf_k);
    if chunks_per_page > 0 {
        fused = crate::fusion::pool_by_document(fused, chunks_per_page);
    }
    if fused.len() > limit {
        fused.truncate(limit);
    }
    fused
}

/// Shared handle so the service builds the dispatcher once and hands
/// out `Arc` clones (matches [`crate::embedder::Embedder`] posture).
pub type SharedFederationDispatcher = Arc<FederationDispatcher>;

/// Best-effort builder — reads the registry from env, wraps it in a
/// dispatcher.  Returns `Ok(None)` when the registry is inactive so
/// the caller can skip wiring the dispatcher entirely (zero-cost
/// path for the common single-node deployment).
pub fn build_default_dispatcher(
) -> Result<Option<SharedFederationDispatcher>, crate::peer_registry::RegistryError> {
    let registry = PeerRegistry::from_env()?;
    if !registry.is_active() {
        return Ok(None);
    }
    tracing::info!(
        peers = registry.peers().len(),
        require_tls = registry.require_tls(),
        allow_insecure_peer = registry.allow_insecure_peer(),
        "search-plugin: federation active",
    );
    Ok(Some(Arc::new(FederationDispatcher::new(registry))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::peer_registry::{PeerAddress, PeerRegistry};
    use std::collections::HashSet;

    fn registry_with(
        peers: Vec<(&str, u16)>,
        zones: Vec<&str>,
        tls: bool,
        allow: bool,
    ) -> PeerRegistry {
        let peers = peers
            .into_iter()
            .map(|(h, p)| PeerAddress {
                host: h.to_string(),
                port: p,
            })
            .collect();
        let zones: HashSet<String> = zones.into_iter().map(String::from).collect();
        PeerRegistry::new(peers, zones, tls, allow)
    }

    #[test]
    fn should_federate_requires_active_and_allowlisted_zone() {
        let reg = registry_with(vec![("a", 1)], vec!["root"], false, true);
        let d = FederationDispatcher::new(reg);
        assert!(d.should_federate("root"));
        assert!(!d.should_federate("other"));
    }

    #[test]
    fn should_federate_false_when_inactive() {
        let reg = registry_with(vec![], vec!["root"], false, true);
        let d = FederationDispatcher::new(reg);
        assert!(!d.should_federate("root"), "no peers ⇒ never federate");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dial_refuses_plaintext_off_loopback_by_default() {
        // No allow-insecure flag, plaintext, non-loopback host → the
        // standing TLS rule kicks in.  We assert the dial() gate
        // directly rather than driving fan_out — fan_out would just
        // log-and-drop, hiding the specific error variant.
        let reg = registry_with(vec![("example.internal", 2126)], vec!["root"], false, false);
        let d = FederationDispatcher::new(reg);
        let peer = &d.registry.peers()[0].clone();
        let err = d.dial(peer).await.unwrap_err();
        assert!(matches!(err, FederationError::PlaintextOffLoopback { .. }));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dial_allows_plaintext_on_loopback() {
        // Loopback host, plaintext, no allow-insecure flag — the gate
        // permits it (loopback is inherently a same-host channel).
        // The connect itself will fail (nothing listens on port 1),
        // but the failure is ConnectFailed, NOT PlaintextOffLoopback.
        let reg = registry_with(vec![("127.0.0.1", 1)], vec!["root"], false, false);
        let d = FederationDispatcher::new(reg);
        let peer = &d.registry.peers()[0].clone();
        let err = d.dial(peer).await.unwrap_err();
        assert!(
            matches!(err, FederationError::ConnectFailed { .. }),
            "loopback plaintext must pass the gate; got {err:?}"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dial_allows_plaintext_off_loopback_with_escape_flag() {
        let reg = registry_with(vec![("example.internal", 2126)], vec!["root"], false, true);
        let d = FederationDispatcher::new(reg);
        let peer = &d.registry.peers()[0].clone();
        let err = d.dial(peer).await.unwrap_err();
        // With allow_insecure_peer=true we PASS the gate and try to
        // connect; the connect fails (unresolvable host), which is
        // the expected downstream failure.
        assert!(
            matches!(
                err,
                FederationError::ConnectFailed { .. } | FederationError::BadEndpoint { .. }
            ),
            "escape flag must pass the gate; got {err:?}"
        );
    }

    #[test]
    fn merge_ranked_deduplicates_across_arms() {
        // Two peers report the same path — the fused list must
        // contain it once, with a boosted RRF score reflecting both
        // arms.  A third disjoint result stays too.
        let a = vec![QueryResult {
            path: "/a.md".into(),
            chunk_index: 0,
            score: 0.9,
            ..Default::default()
        }];
        let b = vec![
            QueryResult {
                path: "/a.md".into(),
                chunk_index: 0,
                score: 0.7,
                ..Default::default()
            },
            QueryResult {
                path: "/b.md".into(),
                chunk_index: 0,
                score: 0.6,
                ..Default::default()
            },
        ];
        let fused = merge_ranked(&[a, b], 60, 0, 100);
        let paths: Vec<&str> = fused.iter().map(|r| r.path.as_str()).collect();
        assert!(paths.contains(&"/a.md"));
        assert!(paths.contains(&"/b.md"));
        // Deduplicated on (path, chunk_index).
        assert_eq!(paths.iter().filter(|p| **p == "/a.md").count(), 1);
    }

    #[test]
    fn merge_ranked_empty_input_is_empty_output() {
        let fused = merge_ranked(&[], 60, 0, 100);
        assert!(fused.is_empty());
    }

    #[test]
    fn merge_ranked_respects_limit() {
        let a: Vec<QueryResult> = (0..20)
            .map(|i| QueryResult {
                path: format!("/p{i}.md"),
                chunk_index: 0,
                score: 1.0 - (i as f32) * 0.01,
                ..Default::default()
            })
            .collect();
        let fused = merge_ranked(&[a], 60, 0, 5);
        assert_eq!(fused.len(), 5);
    }
}
