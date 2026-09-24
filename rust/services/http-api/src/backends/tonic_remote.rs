//! [`TonicRemoteSearchBackend`] — the concrete
//! [`nexus_federated_search::RemoteSearchBackend`] impl the axum
//! daemon wires when a zone's [`nexus_search_common::ZoneSearchRegistry`]
//! entry names a peer daemon.
//!
//! # Shape
//!
//! * Dials the peer daemon's `nexus.search.v1.SearchService.Query`
//!   over tonic gRPC, one Channel-per-peer cached by the shared
//!   [`nexus_search_common::transport::PeerChannelCache`] (the same
//!   cache `nexus-search-plugin::peer_fanout` uses — DRY across the
//!   two callers so the TLS + timeout + loopback rules stay in
//!   lockstep).
//! * Stamps the [`nexus_federated_search::RoutingBackend`]-minted
//!   [`SearchDelegation`] onto tonic metadata under
//!   [`DELEGATION_METADATA_KEY`].  The wire format is JSON-serialised
//!   bytes; the `-bin` metadata suffix tells tonic to base64-encode
//!   on the wire per the gRPC metadata spec, so the receiving
//!   daemon reads back raw bytes and deserialises directly.
//! * Converts the peer's [`crate::search_proto::QueryResponse`] into
//!   [`Vec<Hit>`] via the shared [`crate::backends::proto_bridge::hit_from_proto`]
//!   so attribution ends up on `Hit::extras` under the SAME keys
//!   [`crate::handlers::search_bridge`] decodes — two places to
//!   keep in sync, not three.
//!
//! # Why the full delegation on the wire, not just the id
//!
//! Every alternative (delegation-id-only, id → shared store) needs a
//! store synchronised across mint side + verify side.  The
//! [`SearchDelegation`] struct is self-contained (~150 bytes JSON):
//! source-zone id, target-zone allowlist, subject, TTL, mint
//! timestamp.  Sending the whole blob means the receiver's servicer
//! can validate WITHOUT a lookup — one wire round-trip per remote
//! leg, matching Python's contract while removing a class of stores-
//! out-of-sync bugs.
//!
//! # Auth surface
//!
//! The [`SearchDelegation`] is orthogonal to the request's own
//! `auth_token` field: `auth_token` remains reserved for a bearer
//! `sk-` key, delegation rides on metadata.  The receiving daemon's
//! servicer inspects metadata FIRST — if a valid delegation is
//! present, it runs the search as the delegation's subject; if
//! not, it falls back to whatever `auth_token` (or transport-layer
//! mTLS peer identity) says.  That mapping lands in stage 3 of the
//! task #57 arc (this file is client-only).

use std::sync::Arc;

use async_trait::async_trait;
use nexus_federated_search::{BackendError, RemoteSearchBackend, SearchRequest};
use nexus_search_common::transport::PeerChannelCache;
use nexus_search_common::{Hit, SearchDelegation, DELEGATION_METADATA_KEY};
use tonic::metadata::MetadataValue;

use crate::backends::proto_bridge::hit_from_proto;
use crate::search_proto::search_service_client::SearchServiceClient;
use crate::search_proto::{QueryRequest, QueryType};

/// Dials peer daemons over tonic, stamps a [`SearchDelegation`] on
/// every leg.  Cheap to clone — the underlying
/// [`PeerChannelCache`] is `Arc`-wrapped.
///
/// # Sharing the cache
///
/// The single [`PeerChannelCache`] passed at construction is
/// meant to be shared with the plugin's own peer-fanout dispatcher
/// (`nexus-search-plugin::peer_fanout` reads through the same
/// abstraction).  A composition root that co-hosts both a daemon
/// and its plugin can hand ONE `Arc<PeerChannelCache>` to both,
/// so both dial paths share the same Channel cache + config.  A
/// daemon that only runs the axum surface (no in-process plugin)
/// builds a cache dedicated to remote dials.
pub struct TonicRemoteSearchBackend {
    channels: Arc<PeerChannelCache>,
}

impl TonicRemoteSearchBackend {
    /// Wrap a shared [`PeerChannelCache`].  No I/O runs here; the
    /// first dial happens on the first
    /// [`RemoteSearchBackend::search_remote_zone`] call for a
    /// target that isn't cached yet.
    pub fn new(channels: Arc<PeerChannelCache>) -> Self {
        Self { channels }
    }
}

#[async_trait]
impl RemoteSearchBackend for TonicRemoteSearchBackend {
    async fn search_remote_zone(
        &self,
        target: &str,
        delegation: &SearchDelegation,
        zone_id: &str,
        req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError> {
        // 1. Dial (or hit cache).  Dial-time gate failures
        // (plaintext off-loopback, bad endpoint) surface as
        // Transport — every failure here is BEFORE the request
        // leaves the wire.
        let channel = self
            .channels
            .get_or_dial(target)
            .await
            .map_err(|e| BackendError::Transport(e.to_string()))?;

        // 2. Build the tonic request.  The delegation rides on
        // metadata (see DELEGATION_METADATA_KEY above); the
        // request body carries the search parameters only.
        let proto = build_query_request(zone_id, req);
        let mut request = tonic::Request::new(proto);
        let blob = serde_json::to_vec(delegation).map_err(|e| {
            BackendError::Config(format!("delegation serialise for zone {zone_id:?}: {e}"))
        })?;
        // `_bin`-suffixed metadata is base64-encoded transparently
        // by tonic on the wire.  `MetadataValue::from_bytes` is the
        // typed constructor for that shape.
        let key: tonic::metadata::MetadataKey<tonic::metadata::Binary> =
            DELEGATION_METADATA_KEY.parse().map_err(|e| {
                BackendError::Config(format!(
                    "delegation metadata key {DELEGATION_METADATA_KEY:?}: {e}"
                ))
            })?;
        request
            .metadata_mut()
            .insert_bin(key, MetadataValue::from_bytes(&blob));

        // 3. Send.  Map RPC failures onto the two `BackendError`
        // buckets the federated dispatcher's `ZoneFailure` layer
        // consumes:
        //   * Auth-shaped failures (Unauthenticated /
        //     PermissionDenied) — a delegation the peer rejected;
        //     the leg's fault, but distinguish from network faults
        //     so an operator diagnosing "why did this leg fail"
        //     sees "backend refused" instead of "transport hiccup".
        //   * Everything else (transport, timeout, backend
        //     Internal, …) — Transport.  The peer-side error
        //     message rides through verbatim.
        let mut client =
            SearchServiceClient::new(channel).max_decoding_message_size(64 * 1024 * 1024);
        let resp = client
            .query(request)
            .await
            .map_err(|s| match s.code() {
                tonic::Code::Unauthenticated | tonic::Code::PermissionDenied => {
                    BackendError::Backend(format!("peer refused: {}", s.message()))
                }
                _ => BackendError::Transport(format!("peer {target}: {}", s.message())),
            })?
            .into_inner();

        // 4. Convert.  A peer-side application error rides on
        // `QueryResponse.error` even on gRPC-OK; surface it as
        // Backend so the dispatcher lands the leg in `zones_failed`
        // rather than a silently-empty hit list (matches
        // `PluginLocalSearchBackend` behaviour).
        if let Some(err) = resp.error {
            return Err(BackendError::Backend(err));
        }
        Ok(resp.results.into_iter().map(hit_from_proto).collect())
    }
}

/// Build the proto request body for a per-leg cross-daemon Query.
/// Same as `PluginLocalSearchBackend`'s inline builder — extracted
/// out of the trait fn body only for readability; not shared with
/// the local backend because that impl deliberately keeps
/// `auth_token` reserved for a bearer key, whereas the cross-daemon
/// path leaves `auth_token` empty (delegation rides on metadata,
/// see the module docstring).
fn build_query_request(zone_id: &str, req: &SearchRequest) -> QueryRequest {
    let query_type = match req.search_type.as_str() {
        "semantic" => QueryType::Semantic,
        "hybrid" => QueryType::Hybrid,
        _ => QueryType::Keyword,
    };
    QueryRequest {
        q: req.query.clone(),
        zone_id: zone_id.to_string(),
        limit: u32::try_from(req.limit).unwrap_or(u32::MAX),
        path_filter: req.path_filter.clone().unwrap_or_default(),
        query_type: query_type as i32,
        // Leave the wire `auth_token` field EMPTY on cross-daemon
        // legs — the delegation rides on tonic metadata under
        // `DELEGATION_METADATA_KEY`.  Overloading auth_token would
        // fight the receiver's servicer, which (stage 3) will
        // check metadata FIRST and fall back to auth_token as an
        // sk-key.
        auth_token: String::new(),
        alpha: 0.0,
        fusion_method: 0,
        rrf_k: 0,
        chunks_per_page: 0,
        expand: String::new(),
        recency_mode: String::new(),
        recency_weight: 0.0,
        recency_half_life_days: 0.0,
        path_prefix_boosts: std::collections::HashMap::new(),
        path_filters: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search_proto::search_service_server::{SearchService, SearchServiceServer};
    use crate::search_proto::{
        AddIndexedDirectoryRequest, AddIndexedDirectoryResponse, BatchQueryRequest,
        BatchQueryResponse, GlobRequest, GlobResponse, GrepRequest, GrepResponse, HealthRequest,
        HealthResponse, IndexDocumentsRequest, IndexDocumentsResponse, IndexRequest, IndexResponse,
        ListIndexedDirectoriesRequest, ListIndexedDirectoriesResponse,
        ListZoneIndexingModesRequest, ListZoneIndexingModesResponse, LocateRequest, LocateResponse,
        NotifyFileChangeRequest, NotifyFileChangeResponse, ParkedDiscardRequest,
        ParkedDiscardResponse, ParkedListRequest, ParkedListResponse, ParkedRetryRequest,
        ParkedRetryResponse, QueryResponse, QueryResult, RefreshRequest, RefreshResponse,
        RemoveIndexedDirectoryRequest, RemoveIndexedDirectoryResponse, SetZoneIndexingModeRequest,
        SetZoneIndexingModeResponse, StatsRequest, StatsResponse,
    };
    use nexus_search_common::transport::PeerChannelConfig;
    use nexus_search_common::SearchDelegation;
    use std::sync::Mutex;
    use tokio::net::TcpListener;
    use tokio_stream::wrappers::TcpListenerStream;
    use tonic::{Request, Response, Status};

    /// One recorded call: the proto request body plus the raw
    /// delegation bytes lifted off `x-nexus-search-delegation-bin`
    /// metadata (`None` when the caller sent no delegation).
    /// Named type so the [`clippy::type_complexity`] lint on
    /// `Arc<Mutex<Vec<(_, _)>>>` doesn't fire, AND so the wire
    /// contract this test asserts against reads as one thing.
    type SeenCall = (QueryRequest, Option<Vec<u8>>);

    /// Mock peer daemon: records every incoming request's metadata +
    /// body so a test can assert the wire shape the client produced.
    #[derive(Default, Clone)]
    struct RecordingMock {
        seen: Arc<Mutex<Vec<SeenCall>>>,
        error_response: Arc<Mutex<Option<String>>>,
        rpc_status: Arc<Mutex<Option<tonic::Code>>>,
    }

    #[tonic::async_trait]
    impl SearchService for RecordingMock {
        async fn query(
            &self,
            req: Request<QueryRequest>,
        ) -> Result<Response<QueryResponse>, Status> {
            // Extract the delegation blob from metadata (bin-suffix).
            let delegation_bytes = req
                .metadata()
                .get_bin(DELEGATION_METADATA_KEY)
                .and_then(|v| v.to_bytes().ok())
                .map(|b| b.to_vec());
            let inner = req.into_inner();
            self.seen
                .lock()
                .unwrap()
                .push((inner.clone(), delegation_bytes));

            if let Some(code) = *self.rpc_status.lock().unwrap() {
                return Err(Status::new(code, "mock refused"));
            }
            let error = self.error_response.lock().unwrap().clone();
            Ok(Response::new(QueryResponse {
                results: vec![QueryResult {
                    path: format!("/{}/hit.md", inner.zone_id),
                    chunk_index: 0,
                    chunk_text: "hi".into(),
                    score: 1.0,
                    zone_id: inner.zone_id,
                    mtime_ms: None,
                    expanded_context: String::new(),
                    title_score: None,
                    keyword_score: None,
                    vector_score: None,
                    tier_boost: None,
                    recency_boost: None,
                    expansion_variant_index: None,
                }],
                error,
            }))
        }

        // Every other RPC unreachable — cross-daemon backend only
        // calls Query.
        async fn glob(&self, _: Request<GlobRequest>) -> Result<Response<GlobResponse>, Status> {
            unreachable!()
        }
        async fn grep(&self, _: Request<GrepRequest>) -> Result<Response<GrepResponse>, Status> {
            unreachable!()
        }
        async fn index(&self, _: Request<IndexRequest>) -> Result<Response<IndexResponse>, Status> {
            unreachable!()
        }
        async fn refresh(
            &self,
            _: Request<RefreshRequest>,
        ) -> Result<Response<RefreshResponse>, Status> {
            unreachable!()
        }
        async fn batch_query(
            &self,
            _: Request<BatchQueryRequest>,
        ) -> Result<Response<BatchQueryResponse>, Status> {
            unreachable!()
        }
        async fn index_documents(
            &self,
            _: Request<IndexDocumentsRequest>,
        ) -> Result<Response<IndexDocumentsResponse>, Status> {
            unreachable!()
        }
        async fn notify_file_change(
            &self,
            _: Request<NotifyFileChangeRequest>,
        ) -> Result<Response<NotifyFileChangeResponse>, Status> {
            unreachable!()
        }
        async fn locate(
            &self,
            _: Request<LocateRequest>,
        ) -> Result<Response<LocateResponse>, Status> {
            unreachable!()
        }
        async fn parked_list(
            &self,
            _: Request<ParkedListRequest>,
        ) -> Result<Response<ParkedListResponse>, Status> {
            unreachable!()
        }
        async fn parked_retry(
            &self,
            _: Request<ParkedRetryRequest>,
        ) -> Result<Response<ParkedRetryResponse>, Status> {
            unreachable!()
        }
        async fn parked_discard(
            &self,
            _: Request<ParkedDiscardRequest>,
        ) -> Result<Response<ParkedDiscardResponse>, Status> {
            unreachable!()
        }
        async fn add_indexed_directory(
            &self,
            _: Request<AddIndexedDirectoryRequest>,
        ) -> Result<Response<AddIndexedDirectoryResponse>, Status> {
            unreachable!()
        }
        async fn remove_indexed_directory(
            &self,
            _: Request<RemoveIndexedDirectoryRequest>,
        ) -> Result<Response<RemoveIndexedDirectoryResponse>, Status> {
            unreachable!()
        }
        async fn list_indexed_directories(
            &self,
            _: Request<ListIndexedDirectoriesRequest>,
        ) -> Result<Response<ListIndexedDirectoriesResponse>, Status> {
            unreachable!()
        }
        async fn set_zone_indexing_mode(
            &self,
            _: Request<SetZoneIndexingModeRequest>,
        ) -> Result<Response<SetZoneIndexingModeResponse>, Status> {
            unreachable!()
        }
        async fn list_zone_indexing_modes(
            &self,
            _: Request<ListZoneIndexingModesRequest>,
        ) -> Result<Response<ListZoneIndexingModesResponse>, Status> {
            unreachable!()
        }
        async fn health(
            &self,
            _: Request<HealthRequest>,
        ) -> Result<Response<HealthResponse>, Status> {
            unreachable!()
        }
        async fn stats(&self, _: Request<StatsRequest>) -> Result<Response<StatsResponse>, Status> {
            unreachable!()
        }
    }

    async fn spawn_mock(mock: RecordingMock) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(SearchServiceServer::new(mock))
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await
                .expect("serve");
        });
        format!("http://{addr}")
    }

    fn subject() -> (String, String) {
        ("user".into(), "alice".into())
    }

    fn req() -> SearchRequest {
        SearchRequest {
            query: "widgets".into(),
            search_type: "hybrid".into(),
            limit: 7,
            path_filter: Some("/docs".into()),
            subject: subject(),
        }
    }

    fn delegation(zone: &str) -> SearchDelegation {
        SearchDelegation::new_from_now("sd_test0000ab", "local", [zone.to_string()], subject())
    }

    fn backend_for(target: String) -> TonicRemoteSearchBackend {
        // Allow plaintext off-loopback so a test target that is not
        // literally 127.0.0.1 (some CIs bind DNS names) still dials.
        // The gate is exercised in the shared cache's own tests.
        let cache = Arc::new(PeerChannelCache::new(PeerChannelConfig {
            allow_insecure_peer: true,
            ..PeerChannelConfig::default()
        }));
        // Pre-warm: we can't inspect the cache directly, but the
        // dial is lazy, so the first call will populate.
        let _ = target;
        TonicRemoteSearchBackend::new(cache)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn happy_path_sends_delegation_on_metadata_and_returns_hits() {
        let mock = RecordingMock::default();
        let target = spawn_mock(mock.clone()).await;
        let backend = backend_for(target.clone());

        let d = delegation("legal");
        let hits = backend
            .search_remote_zone(&target, &d, "legal", &req())
            .await
            .expect("ok");

        // Response conversion.
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "/legal/hit.md");
        assert_eq!(hits[0].zone_id.as_deref(), Some("legal"));

        // Wire shape.
        let seen = mock.seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        let (proto_req, delegation_bytes) = &seen[0];
        assert_eq!(proto_req.q, "widgets");
        assert_eq!(proto_req.zone_id, "legal");
        assert_eq!(proto_req.limit, 7);
        assert_eq!(proto_req.path_filter, "/docs");
        // Cross-daemon backend leaves auth_token EMPTY — delegation
        // rides on metadata (see module docstring).  Regression pin
        // against silently overloading auth_token.
        assert!(
            proto_req.auth_token.is_empty(),
            "auth_token must be empty on cross-daemon legs"
        );

        // Delegation ↔ metadata round-trip.
        let bytes = delegation_bytes
            .as_ref()
            .expect("delegation must ride on metadata");
        let decoded: SearchDelegation = serde_json::from_slice(bytes).expect("json");
        assert_eq!(decoded.delegation_id, d.delegation_id);
        assert_eq!(decoded.source_zone_id, "local");
        assert_eq!(decoded.target_zones, vec!["legal".to_string()]);
        assert_eq!(decoded.subject, ("user".into(), "alice".into()));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn peer_application_error_maps_to_backend_error() {
        // Regression pin: an application-level error on the peer
        // (populated `QueryResponse.error`) MUST NOT silently
        // surface as an empty hit list — the dispatcher's
        // `zones_failed` bucket depends on the error propagating.
        let mock = RecordingMock::default();
        *mock.error_response.lock().unwrap() = Some("no index for zone legal".into());
        let target = spawn_mock(mock).await;
        let backend = backend_for(target.clone());

        let err = backend
            .search_remote_zone(&target, &delegation("legal"), "legal", &req())
            .await
            .unwrap_err();
        assert!(matches!(err, BackendError::Backend(_)), "{err:?}");
        let msg = err.to_string();
        assert!(msg.contains("no index for zone legal"), "{msg}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn peer_unauthenticated_maps_to_backend_error_not_transport() {
        // Regression pin: an auth-shaped RPC status (delegation
        // refused, expired, method-not-permitted) surfaces as
        // `Backend`, distinct from network errors.  An operator
        // triaging a leg failure sees "backend refused" instead of
        // "transport hiccup".
        let mock = RecordingMock::default();
        *mock.rpc_status.lock().unwrap() = Some(tonic::Code::Unauthenticated);
        let target = spawn_mock(mock).await;
        let backend = backend_for(target.clone());

        let err = backend
            .search_remote_zone(&target, &delegation("legal"), "legal", &req())
            .await
            .unwrap_err();
        assert!(matches!(err, BackendError::Backend(_)), "{err:?}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn transport_failure_maps_to_transport_error() {
        // Dial a port nothing's bound on — must surface as
        // Transport, not Backend.
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let dead_addr = listener.local_addr().unwrap();
        drop(listener); // release
        let target = format!("http://{dead_addr}");
        let backend = backend_for(target.clone());

        let err = backend
            .search_remote_zone(&target, &delegation("legal"), "legal", &req())
            .await
            .unwrap_err();
        assert!(matches!(err, BackendError::Transport(_)), "{err:?}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn query_type_wire_string_maps_to_proto_enum() {
        // Regression pin: the string→enum map here MUST match the
        // one in `PluginLocalSearchBackend`.  A drift would send
        // one search type to the local plugin and another to a
        // peer for the same request.
        for (wire, expected) in [
            ("keyword", QueryType::Keyword),
            ("semantic", QueryType::Semantic),
            ("hybrid", QueryType::Hybrid),
            ("unknown_falls_back", QueryType::Keyword),
        ] {
            let mock = RecordingMock::default();
            let target = spawn_mock(mock.clone()).await;
            let backend = backend_for(target.clone());
            let mut r = req();
            r.search_type = wire.into();
            let _ = backend
                .search_remote_zone(&target, &delegation("z"), "z", &r)
                .await;
            let seen = mock.seen.lock().unwrap();
            assert_eq!(
                seen[0].0.query_type, expected as i32,
                "wire {wire:?} must map to {expected:?}",
            );
        }
    }

    #[test]
    fn delegation_metadata_key_matches_grpc_bin_suffix_convention() {
        // The `-bin` suffix tells tonic (and any gRPC library) to
        // treat the value as binary — that is what
        // `MetadataValue::from_bytes` + `insert_bin` require.  A
        // typo here would silently break the wire (tonic would
        // route the value through the ASCII path, base64 gets
        // double-encoded, receiver's decode fails).  Also
        // regression-pins the const's location (shared in
        // nexus-search-common so the servicer-side extractor reads
        // through the same symbol).
        assert!(DELEGATION_METADATA_KEY.ends_with("-bin"));
        assert_eq!(
            DELEGATION_METADATA_KEY,
            nexus_search_common::DELEGATION_METADATA_KEY,
            "the crate-local re-export must match the SSOT const",
        );
    }
}
