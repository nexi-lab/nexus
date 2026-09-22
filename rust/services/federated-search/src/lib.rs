//! Cross-zone search fanout dispatcher.
//!
//! Consumer contract: an axum `/v2/search/query` handler that finds
//! the caller's token grants more than one zone hands the request to
//! [`FederatedSearchDispatcher::search`], which returns one
//! [`FederatedSearchResponse`] covering every zone the caller may
//! read from.
//!
//! # What this crate owns
//!
//! * **Zone discovery** — the dispatcher asks
//!   [`AccessibleZonesCache`] for the caller's zone set (per-subject
//!   TTL cache on top of `nexus_rebac::list_accessible_zones`).
//! * **Concurrent fanout** — one tokio task per zone, bounded by a
//!   `Semaphore` (`max_concurrent_zones`), with a per-zone
//!   `tokio::time::timeout` so one slow zone does not starve the
//!   whole request.
//! * **Fusion** — collected per-zone hit lists fuse through
//!   [`rrf_multi_fusion`], producing a single ranked list.
//! * **Envelope** — [`FederatedSearchResponse`] carries the fused
//!   hits, `zones_searched` / `zones_failed` / `zones_skipped`, and
//!   an aggregate `latency_ms`.  Callers stamp `semantic_degraded`
//!   downstream if the deployment profile asks.
//!
//! # What this crate deliberately does NOT own
//!
//! * **Cross-zone gRPC** — every zone routes to the SAME local
//!   backend today; the cross-zone leg with `SearchDelegation`
//!   arrives in the next PR of the arc.  A [`ZoneSearchRegistry`] is
//!   accepted so the dispatcher can already tell "local vs remote"
//!   apart, but a remote zone currently falls through to the shared
//!   local backend — behaviour matches Python's Phase-1 dispatcher.
//! * **Per-file ReBAC filtering** — composed on top by the caller
//!   AFTER fusion (Python does the same via
//!   `filter_federated_results`).  Keeps this crate one concern.
//! * **Recency / cap / result-cache knobs** — omitted for the first
//!   pass; the tunables land as they become measurable.

use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::future::join_all;
use nexus_rebac::list_zones::{AccessibleZonesCache, Subject};
use nexus_rebac::store::ReBACTupleStore;
use nexus_search_common::{
    is_all_peers_failed, rrf_multi_fusion, FederatedSearchResponse, Hit, ZoneFailure,
    ZoneSearchRegistry,
};
use tokio::sync::Semaphore;
use tracing::warn;

pub mod backend;
pub mod routing;
pub use backend::{
    BackendError, LocalSearchBackend, NoOpRemoteSearchBackend, RemoteSearchBackend, SearchRequest,
};
pub use routing::RoutingBackend;

/// Knobs a caller may tune per dispatcher instance.  Defaults match
/// the Python `FederatedSearchConfig` so a Python-to-Rust swap does
/// not shift observable timing.
#[derive(Debug, Clone, Copy)]
pub struct DispatcherConfig {
    /// Upper bound on concurrent per-zone requests in flight.
    /// Prevents one wide fanout from monopolising the shared HTTP /
    /// gRPC client pool the backend hides.
    pub max_concurrent_zones: usize,
    /// Per-zone deadline.  A slow zone cannot delay the whole
    /// request past this; it lands in `zones_failed` instead.
    pub zone_timeout: Duration,
    /// RRF constant.  60 in the paper; every tuning story confirms
    /// this default holds.
    pub rrf_k: u32,
    /// Enable the top-rank bonus on fusion (see the
    /// `rrf_multi_fusion` docstring for the reasoning).
    pub rrf_top_rank_bonus: bool,
}

impl Default for DispatcherConfig {
    fn default() -> Self {
        Self {
            max_concurrent_zones: 8,
            zone_timeout: Duration::from_secs(5),
            rrf_k: 60,
            rrf_top_rank_bonus: true,
        }
    }
}

/// The federated dispatcher.
///
/// Owns:
/// * an `Arc<dyn LocalSearchBackend>` — the actual per-zone search
///   call (typed as a trait so tests can wire a fake without the
///   plugin gRPC stack);
/// * an `Arc<dyn ReBACTupleStore>` — for zone discovery via the
///   `AccessibleZonesCache`;
/// * an `Arc<AccessibleZonesCache>` — one cache per dispatcher so
///   two parallel HTTP requests share cache warm-up;
/// * an `Arc<dyn ZoneSearchRegistry>` — the local-vs-remote map;
///   consumed only for logging in this PR, wired for PR 4.
///
/// `Send + Sync + 'static` because axum handlers stash it on
/// `AppState`.
pub struct FederatedSearchDispatcher<B: LocalSearchBackend> {
    backend: Arc<B>,
    rebac_store: Arc<dyn ReBACTupleStore>,
    zone_cache: Arc<AccessibleZonesCache>,
    #[allow(dead_code)]
    // Consumed in PR 4 (cross-zone gRPC) — accepting it here so
    // callers already wire the mapping.  Attribute silences the
    // "unused field" clippy under -D warnings until then.
    registry: Arc<dyn ZoneSearchRegistry>,
    config: DispatcherConfig,
}

impl<B: LocalSearchBackend + 'static> FederatedSearchDispatcher<B> {
    pub fn new(
        backend: Arc<B>,
        rebac_store: Arc<dyn ReBACTupleStore>,
        zone_cache: Arc<AccessibleZonesCache>,
        registry: Arc<dyn ZoneSearchRegistry>,
        config: DispatcherConfig,
    ) -> Self {
        Self {
            backend,
            rebac_store,
            zone_cache,
            registry,
            config,
        }
    }

    /// Fan `req` out to every zone the caller can read from, fuse
    /// the per-zone results, and return one envelope.
    ///
    /// `zone_filter` — optional upper bound on the fan-out set.  A
    /// zone-scoped token passes its allow-list here so the dispatch
    /// cannot widen the caller's effective scope beyond what the
    /// token grants (intersected with the ReBAC-derived readable
    /// set).  `None` = no upper bound (still ReBAC-scoped).
    pub async fn search(
        &self,
        subject: Subject<'_>,
        mut req: SearchRequest,
        zone_filter: Option<&[String]>,
    ) -> FederatedSearchResponse {
        let start = Instant::now();

        // Stamp the subject onto the request so the routing
        // backend has it available for delegation minting on
        // remote legs (see `RoutingBackend::mint_delegation`).
        // Cheap owned strings — the per-leg spawn clones the
        // request anyway.
        req.subject = (subject.0.to_string(), subject.1.to_string());

        // 1. Zone discovery.  Failure here is unusual (store
        // unreachable); we treat it as "no accessible zones" — the
        // response envelope carries an empty result set, matching
        // the Python fallback.
        let accessible = match self.zone_cache.lookup(&*self.rebac_store, subject) {
            Ok(z) => z,
            Err(e) => {
                warn!(error = %e, "federated: zone discovery failed");
                return FederatedSearchResponse {
                    latency_ms: elapsed_ms(start),
                    ..Default::default()
                };
            }
        };
        let searchable = intersect_filter(accessible, zone_filter);
        if searchable.is_empty() {
            return FederatedSearchResponse {
                latency_ms: elapsed_ms(start),
                ..Default::default()
            };
        }

        // 2. Concurrent fanout with a semaphore + per-zone timeout.
        // We collect `Result` per zone so a single failure does not
        // sink the response.
        let semaphore = Arc::new(Semaphore::new(self.config.max_concurrent_zones));
        let futures: Vec<_> = searchable
            .iter()
            .map(|zone_id| {
                let zone_id = zone_id.clone();
                let backend = Arc::clone(&self.backend);
                let sem = Arc::clone(&semaphore);
                let req = req.clone();
                let timeout = self.config.zone_timeout;
                async move {
                    let _permit = sem.acquire().await.expect("semaphore not closed");
                    let leg = backend.search_zone(&zone_id, &req);
                    let outcome = tokio::time::timeout(timeout, leg).await;
                    (zone_id, outcome)
                }
            })
            .collect();
        let legs = join_all(futures).await;

        // 3. Split successes / failures.  Successes contribute a
        // ranked hit list to fusion; failures land in
        // `zones_failed` with the transport / backend / timeout
        // error verbatim.
        let mut zones_searched: Vec<String> = Vec::with_capacity(legs.len());
        let mut zones_failed: Vec<ZoneFailure> = Vec::new();
        // BTreeMap key preserves deterministic per-source order in
        // the fusion input — cross-node parity requirement.
        let mut per_zone_hits: std::collections::BTreeMap<String, Vec<Hit>> =
            std::collections::BTreeMap::new();
        for (zone_id, outcome) in legs {
            match outcome {
                Ok(Ok(hits)) => {
                    zones_searched.push(zone_id.clone());
                    per_zone_hits.insert(zone_id, hits);
                }
                Ok(Err(e)) => zones_failed.push(ZoneFailure {
                    zone_id,
                    error: e.to_string(),
                }),
                Err(_elapsed) => zones_failed.push(ZoneFailure {
                    zone_id,
                    error: format!(
                        "zone timed out after {} ms",
                        self.config.zone_timeout.as_millis()
                    ),
                }),
            }
        }

        // 4. Fuse.  `rrf_multi_fusion` takes `(source_name, hits)`
        // tuples — the source name is the zone id so the per-zone
        // score attribution lands on `Hit::extras` as `<zone>_score`
        // for a debug / regression reader.
        let sources: Vec<(&str, Vec<Hit>)> = per_zone_hits
            .iter()
            .map(|(zone, hits)| (zone.as_str(), hits.clone()))
            .collect();
        let fused = rrf_multi_fusion(
            &sources,
            self.config.rrf_k,
            req.limit,
            self.config.rrf_top_rank_bonus,
        );

        FederatedSearchResponse {
            results: fused,
            zones_searched,
            zones_failed,
            zones_skipped: Vec::new(),
            latency_ms: elapsed_ms(start),
            cached: false,
            search_timing: Default::default(),
            semantic_degraded: false,
        }
    }
}

/// `true` when the response reports "all peers failed" (either no
/// searchable zones or every leg errored).  Re-exported so a caller
/// composing the SANDBOX degrade-guard can check it without pulling
/// `nexus-search-common` in directly.
pub fn all_peers_failed(response: &FederatedSearchResponse) -> bool {
    is_all_peers_failed(response)
}

fn intersect_filter(zones: Vec<String>, filter: Option<&[String]>) -> Vec<String> {
    match filter {
        None => zones,
        Some(allow) => {
            let allow_set: std::collections::HashSet<&str> =
                allow.iter().map(String::as_str).collect();
            zones
                .into_iter()
                .filter(|z| allow_set.contains(z.as_str()))
                .collect()
        }
    }
}

fn elapsed_ms(start: Instant) -> f64 {
    let d = start.elapsed();
    (d.as_secs_f64()) * 1_000.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::{BackendError, LocalSearchBackend, SearchRequest};
    use async_trait::async_trait;
    use nexus_rebac::inmem::InMemoryReBACTupleStore;
    use nexus_rebac::store::ReBACTupleStore;
    use nexus_rebac::tuple_key;
    use nexus_search_common::registry::InMemoryZoneSearchRegistry;

    // Tiny lib::types::ReBACTuple constructor.
    fn tuple(
        object_type: &str,
        object_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> lib::types::ReBACTuple {
        lib::types::ReBACTuple {
            object_type: object_type.into(),
            object_id: object_id.into(),
            relation: relation.into(),
            subject_type: subject_type.into(),
            subject_id: subject_id.into(),
            subject_relation: None,
        }
    }

    fn grant_zone(store: &InMemoryReBACTupleStore, zone: &str, subject_id: &str) {
        let t = tuple("zone", zone, "member", "user", subject_id);
        let key = tuple_key::encode("root", &t).unwrap();
        store.put(&key, b"").unwrap();
    }

    fn hit(path: &str, score: f64, zone: &str) -> Hit {
        Hit {
            path: path.into(),
            chunk_index: 0,
            chunk_text: format!("body of {path}"),
            score,
            zone_id: Some(zone.into()),
            extras: Default::default(),
        }
    }

    /// Simple deterministic backend: per-zone hit lists baked in.
    /// Failure and delay knobs mirror the two failure modes the
    /// dispatcher must handle — one leg errors, one leg times out —
    /// so a single fake exercises every arm.
    #[derive(Default, Clone)]
    struct FakeBackend {
        by_zone: std::collections::HashMap<String, Vec<Hit>>,
        error_zones: std::collections::HashSet<String>,
        delay_zones: std::collections::HashMap<String, Duration>,
    }

    #[async_trait]
    impl LocalSearchBackend for FakeBackend {
        async fn search_zone(
            &self,
            zone_id: &str,
            _req: &SearchRequest,
        ) -> Result<Vec<Hit>, BackendError> {
            if let Some(d) = self.delay_zones.get(zone_id) {
                tokio::time::sleep(*d).await;
            }
            if self.error_zones.contains(zone_id) {
                return Err(BackendError::Backend(format!("zone {zone_id} refused")));
            }
            Ok(self.by_zone.get(zone_id).cloned().unwrap_or_default())
        }
    }

    fn dispatcher(
        backend: FakeBackend,
        rebac: Arc<InMemoryReBACTupleStore>,
        config: DispatcherConfig,
    ) -> FederatedSearchDispatcher<FakeBackend> {
        FederatedSearchDispatcher::new(
            Arc::new(backend),
            rebac,
            Arc::new(AccessibleZonesCache::new()),
            Arc::new(InMemoryZoneSearchRegistry::new()),
            config,
        )
    }

    fn req() -> SearchRequest {
        SearchRequest {
            query: "any".into(),
            search_type: "hybrid".into(),
            limit: 10,
            path_filter: None,
            // Overwritten by the dispatcher before spawning legs
            // (see `FederatedSearchDispatcher::search`) — the
            // fixture value is a placeholder proving that path.
            subject: (String::new(), String::new()),
        }
    }

    #[tokio::test]
    async fn empty_accessible_zone_set_returns_empty_envelope() {
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        let d = dispatcher(FakeBackend::default(), rebac, DispatcherConfig::default());
        let out = d.search(("user", "alice"), req(), None).await;
        assert!(out.results.is_empty());
        assert!(out.zones_searched.is_empty());
        assert!(out.zones_failed.is_empty());
    }

    #[tokio::test]
    async fn happy_path_fuses_per_zone_hits_into_one_ranking() {
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let mut b = FakeBackend::default();
        b.by_zone.insert(
            "eng".into(),
            vec![hit("/eng/a.md", 5.0, "eng"), hit("/eng/b.md", 3.0, "eng")],
        );
        b.by_zone
            .insert("legal".into(), vec![hit("/legal/x.md", 4.0, "legal")]);
        let d = dispatcher(b, rebac, DispatcherConfig::default());
        let out = d.search(("user", "alice"), req(), None).await;
        assert_eq!(out.zones_searched.len(), 2, "{out:?}");
        assert!(out.zones_failed.is_empty(), "{out:?}");
        // Every unique hit crosses fusion.
        let paths: Vec<&str> = out.results.iter().map(|h| h.path.as_str()).collect();
        assert!(paths.contains(&"/eng/a.md"));
        assert!(paths.contains(&"/eng/b.md"));
        assert!(paths.contains(&"/legal/x.md"));
    }

    #[tokio::test]
    async fn one_failing_zone_does_not_sink_the_others() {
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let mut b = FakeBackend::default();
        b.by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", 5.0, "eng")]);
        b.error_zones.insert("legal".into());
        let d = dispatcher(b, rebac, DispatcherConfig::default());
        let out = d.search(("user", "alice"), req(), None).await;
        assert_eq!(out.zones_searched, vec!["eng".to_string()]);
        assert_eq!(out.zones_failed.len(), 1);
        assert_eq!(out.zones_failed[0].zone_id, "legal");
        assert_eq!(out.results.len(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_slow_zone_times_out_and_lands_in_zones_failed() {
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let mut b = FakeBackend::default();
        b.by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", 5.0, "eng")]);
        // legal takes 10 s to answer; dispatcher timeout is 100 ms.
        b.delay_zones
            .insert("legal".into(), Duration::from_secs(10));
        b.by_zone
            .insert("legal".into(), vec![hit("/legal/x.md", 1.0, "legal")]);
        let cfg = DispatcherConfig {
            zone_timeout: Duration::from_millis(100),
            ..DispatcherConfig::default()
        };
        let d = dispatcher(b, rebac, cfg);
        let out = d.search(("user", "alice"), req(), None).await;
        assert_eq!(out.zones_searched, vec!["eng".to_string()]);
        assert_eq!(out.zones_failed.len(), 1);
        assert!(
            out.zones_failed[0].error.contains("timed out"),
            "{}",
            out.zones_failed[0].error,
        );
    }

    #[tokio::test]
    async fn zone_filter_narrows_the_fanout_set_but_never_widens_it() {
        // Token grants eng + legal, but zone_filter passes only eng
        // — legal must not be queried.
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let mut b = FakeBackend::default();
        b.by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", 5.0, "eng")]);
        b.by_zone
            .insert("legal".into(), vec![hit("/legal/x.md", 5.0, "legal")]);
        let d = dispatcher(b, rebac, DispatcherConfig::default());
        let filter = vec!["eng".to_string()];
        let out = d.search(("user", "alice"), req(), Some(&filter)).await;
        assert_eq!(out.zones_searched, vec!["eng".to_string()]);
        assert!(out
            .results
            .iter()
            .all(|h| h.zone_id.as_deref() == Some("eng")));
    }

    #[tokio::test]
    async fn zone_filter_cannot_widen_beyond_the_rebac_readable_set() {
        // Token grants eng ONLY; zone_filter tries to also enable
        // legal — the readable set stays eng-only.
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        let mut b = FakeBackend::default();
        b.by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", 5.0, "eng")]);
        b.by_zone
            .insert("legal".into(), vec![hit("/legal/never.md", 5.0, "legal")]);
        let d = dispatcher(b, rebac, DispatcherConfig::default());
        let filter = vec!["eng".to_string(), "legal".to_string()];
        let out = d.search(("user", "alice"), req(), Some(&filter)).await;
        assert_eq!(out.zones_searched, vec!["eng".to_string()]);
        assert!(
            out.results
                .iter()
                .all(|h| h.zone_id.as_deref() == Some("eng")),
            "legal must not appear — token does not grant it",
        );
    }

    #[tokio::test]
    async fn dispatcher_stamps_the_caller_subject_onto_every_leg_request() {
        // Regression pin for PR 4: `RoutingBackend::mint_delegation`
        // reads `req.subject` when producing a per-remote-leg
        // credential — the dispatcher MUST stamp the caller's
        // subject before spawning legs, or the credential lands
        // with the fixture's empty subject and audit trails record
        // the wrong actor.
        use std::sync::Mutex;

        #[derive(Default)]
        struct SubjectRecorder {
            seen: Mutex<Vec<(String, String)>>,
        }
        #[async_trait]
        impl LocalSearchBackend for SubjectRecorder {
            async fn search_zone(
                &self,
                _zone_id: &str,
                req: &SearchRequest,
            ) -> Result<Vec<Hit>, BackendError> {
                self.seen.lock().unwrap().push(req.subject.clone());
                Ok(vec![])
            }
        }

        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let recorder = Arc::new(SubjectRecorder::default());
        let d = FederatedSearchDispatcher::new(
            Arc::clone(&recorder),
            rebac,
            Arc::new(AccessibleZonesCache::new()),
            Arc::new(InMemoryZoneSearchRegistry::new()),
            DispatcherConfig::default(),
        );
        let _ = d.search(("user", "alice"), req(), None).await;
        let seen = recorder.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        for subj in seen.iter() {
            assert_eq!(subj, &("user".to_string(), "alice".to_string()));
        }
    }

    #[tokio::test]
    async fn all_peers_failed_predicate_flags_all_error_response() {
        let rebac = Arc::new(InMemoryReBACTupleStore::new());
        grant_zone(&rebac, "eng", "alice");
        grant_zone(&rebac, "legal", "alice");
        let mut b = FakeBackend::default();
        b.error_zones.insert("eng".into());
        b.error_zones.insert("legal".into());
        let d = dispatcher(b, rebac, DispatcherConfig::default());
        let out = d.search(("user", "alice"), req(), None).await;
        assert!(all_peers_failed(&out));
    }
}
