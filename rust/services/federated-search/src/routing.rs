//! [`RoutingBackend`] — the [`LocalSearchBackend`] impl the axum
//! handler wires into the dispatcher.  It composes:
//!
//! * a **local backend** (one impl serving every zone this daemon
//!   owns — a thin wrapper around the local plugin's
//!   `SearchService.Query`);
//! * a **remote backend** (dials another daemon's plugin over
//!   whatever transport the deployment uses — tonic gRPC in the
//!   default build; a fake in tests);
//! * a **registry** — [`ZoneSearchRegistry`] — that answers
//!   "which daemon owns this zone".
//!
//! # Routing rule
//!
//! For each `search_zone(zone_id, req)` call the dispatcher makes:
//!
//! 1. Ask the registry `resolve(zone_id)`.
//! 2. `None` OR "resolves to my own target" (per `local_target`) ⇒
//!    call the local backend directly.
//! 3. Otherwise ⇒ mint a [`SearchDelegation`] scoped to
//!    `[zone_id]`, hand it + the target to the remote backend.
//!
//! Delegation minting stays inside this crate — the remote backend
//! trait only sees an already-minted credential.  Two consequences
//! that matter:
//!
//! * a future transport does not have to learn TTL / id-generation
//!   policy;
//! * a test can construct a delegation directly and skip the whole
//!   mint path.
//!
//! # Why not two dispatchers
//!
//! An alternative shape had the dispatcher hold `local` and `remote`
//! backends directly.  Rejected: the dispatcher already owns
//! concurrency + fusion + envelope; adding routing to it would fatten
//! one type with two orthogonal concerns.  The routing concern lives
//! here, behind the same `LocalSearchBackend` seam the dispatcher
//! already knows about — the dispatcher sees ONE backend, and this
//! type answers "local or remote" transparently.

use std::collections::HashSet;
use std::sync::Arc;

use async_trait::async_trait;
use nexus_search_common::{SearchDelegation, ZoneSearchRegistry};
use tracing::debug;
use uuid::Uuid;

use crate::backend::{BackendError, LocalSearchBackend, RemoteSearchBackend, SearchRequest};
use nexus_search_common::Hit;

/// Composes a local backend + a remote backend + a
/// [`ZoneSearchRegistry`] into one [`LocalSearchBackend`] the
/// dispatcher can call for any zone.
///
/// See the module docstring for the routing rule.  This struct is
/// what a composition root (the axum handler wiring) hands the
/// dispatcher in production; unit tests wire it with fakes for both
/// backends to exercise the routing decisions.
pub struct RoutingBackend<L, R>
where
    L: LocalSearchBackend + 'static,
    R: RemoteSearchBackend + 'static,
{
    local: Arc<L>,
    remote: Arc<R>,
    registry: Arc<dyn ZoneSearchRegistry>,
    /// Zone id this daemon self-identifies as — stamped on every
    /// minted delegation's `source_zone_id`.  Callers pass their
    /// own daemon's zone id here.
    source_zone_id: String,
    /// Optional short-circuit: if the registry hands back a target
    /// that matches ONE OF these strings, we treat the zone as local
    /// and skip the remote dial (the registry pointed us at
    /// ourselves).  A daemon that runs one plugin serving every
    /// zone in `NEXUS_SEARCH_PLUGIN_TARGET` passes that string here
    /// so a `shared_for_all(...)` registry does not accidentally
    /// spin up a remote dial back to loopback.
    local_targets: HashSet<String>,
    /// Delegation TTL — carried onto every minted credential.
    /// [`None`] uses [`nexus_search_common::DEFAULT_TTL_SECONDS`].
    delegation_ttl_seconds: Option<u64>,
}

impl<L, R> RoutingBackend<L, R>
where
    L: LocalSearchBackend + 'static,
    R: RemoteSearchBackend + 'static,
{
    /// Assemble a routing backend.  Every argument is required —
    /// this type has no meaningful default (the registry, targets,
    /// and source-zone identity are all deployment-specific).
    pub fn new(
        local: Arc<L>,
        remote: Arc<R>,
        registry: Arc<dyn ZoneSearchRegistry>,
        source_zone_id: impl Into<String>,
        local_targets: impl IntoIterator<Item = String>,
    ) -> Self {
        Self {
            local,
            remote,
            registry,
            source_zone_id: source_zone_id.into(),
            local_targets: local_targets.into_iter().collect(),
            delegation_ttl_seconds: None,
        }
    }

    /// Override the delegation TTL (default:
    /// [`nexus_search_common::DEFAULT_TTL_SECONDS`]).  Deployments that
    /// need a longer credential lifetime for a wide fan-out pass a
    /// larger value here.
    pub fn with_delegation_ttl_seconds(mut self, ttl: u64) -> Self {
        self.delegation_ttl_seconds = Some(ttl);
        self
    }

    /// Returns `true` when `target` names this daemon (i.e. the
    /// registry entry points at loopback / the plugin this daemon
    /// hosts locally).
    fn is_local_target(&self, target: &str) -> bool {
        self.local_targets.iter().any(|t| t == target)
    }

    /// Mint a fresh [`SearchDelegation`] for `zone_id` scoped to the
    /// current subject.  One delegation per remote leg — different
    /// zones get different delegations so a single leaked credential
    /// cannot widen scope to a sibling zone.
    fn mint_delegation(&self, zone_id: &str, req: &SearchRequest) -> SearchDelegation {
        let delegation_id = format!("sd_{}", short_uuid());
        match self.delegation_ttl_seconds {
            Some(ttl) => SearchDelegation::new_with_ttl(
                delegation_id,
                &self.source_zone_id,
                [zone_id.to_string()],
                req.subject.clone(),
                ttl,
            ),
            None => SearchDelegation::new_from_now(
                delegation_id,
                &self.source_zone_id,
                [zone_id.to_string()],
                req.subject.clone(),
            ),
        }
    }
}

/// Twelve-hex-char id — matches Python's `uuid.uuid4().hex[:12]`
/// convention (`_mint_search_delegation` in
/// `bricks/search/federated_search.py`) so mixed-language deployments
/// have one delegation-id shape in logs.
fn short_uuid() -> String {
    let uuid = Uuid::new_v4();
    let hex = uuid.as_simple().to_string();
    hex[..12].to_string()
}

#[async_trait]
impl<L, R> LocalSearchBackend for RoutingBackend<L, R>
where
    L: LocalSearchBackend + 'static,
    R: RemoteSearchBackend + 'static,
{
    async fn search_zone(
        &self,
        zone_id: &str,
        req: &SearchRequest,
    ) -> Result<Vec<Hit>, BackendError> {
        // Registry lookup drives the routing decision.  A `None`
        // entry means the deployment has no per-zone endpoint for
        // this zone — the shared plugin covers it, so fall through
        // to the local backend.
        let target = match self.registry.resolve(zone_id) {
            None => {
                debug!(zone_id, "routing: no registry entry — local dispatch");
                return self.local.search_zone(zone_id, req).await;
            }
            Some(t) => t,
        };
        if self.is_local_target(&target) {
            debug!(zone_id, target = %target, "routing: local target — local dispatch");
            return self.local.search_zone(zone_id, req).await;
        }
        // Remote leg: mint a delegation, hand it to the remote
        // backend along with the resolved target.  The remote
        // backend serialises the delegation onto whatever auth
        // context its transport uses.
        let delegation = self.mint_delegation(zone_id, req);
        debug!(
            zone_id,
            target = %target,
            delegation_id = %delegation.delegation_id,
            "routing: remote dispatch",
        );
        self.remote
            .search_remote_zone(&target, &delegation, zone_id, req)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nexus_search_common::registry::InMemoryZoneSearchRegistry;
    use std::sync::Mutex;
    use tokio::sync::Mutex as AsyncMutex;

    fn hit(path: &str, zone: &str, score: f64) -> Hit {
        Hit {
            path: path.into(),
            chunk_index: 0,
            chunk_text: format!("body of {path}"),
            score,
            zone_id: Some(zone.into()),
            extras: Default::default(),
        }
    }

    fn req() -> SearchRequest {
        SearchRequest {
            query: "q".into(),
            search_type: "hybrid".into(),
            limit: 10,
            path_filter: None,
            subject: ("user".into(), "alice".into()),
        }
    }

    #[derive(Default)]
    struct FakeLocal {
        by_zone: std::collections::HashMap<String, Vec<Hit>>,
        calls: Mutex<Vec<String>>,
    }

    #[async_trait]
    impl LocalSearchBackend for FakeLocal {
        async fn search_zone(
            &self,
            zone_id: &str,
            _req: &SearchRequest,
        ) -> Result<Vec<Hit>, BackendError> {
            self.calls.lock().unwrap().push(zone_id.to_string());
            Ok(self.by_zone.get(zone_id).cloned().unwrap_or_default())
        }
    }

    #[derive(Debug, Clone)]
    struct RemoteCall {
        target: String,
        zone_id: String,
        delegation_id: String,
        source_zone_id: String,
        target_zones: Vec<String>,
        subject: (String, String),
    }

    #[derive(Default)]
    struct FakeRemote {
        by_zone: std::collections::HashMap<String, Vec<Hit>>,
        error_zones: std::collections::HashSet<String>,
        calls: AsyncMutex<Vec<RemoteCall>>,
    }

    #[async_trait]
    impl RemoteSearchBackend for FakeRemote {
        async fn search_remote_zone(
            &self,
            target: &str,
            delegation: &SearchDelegation,
            zone_id: &str,
            req: &SearchRequest,
        ) -> Result<Vec<Hit>, BackendError> {
            self.calls.lock().await.push(RemoteCall {
                target: target.into(),
                zone_id: zone_id.into(),
                delegation_id: delegation.delegation_id.clone(),
                source_zone_id: delegation.source_zone_id.clone(),
                target_zones: delegation.target_zones.clone(),
                subject: req.subject.clone(),
            });
            if self.error_zones.contains(zone_id) {
                return Err(BackendError::Transport(format!("dial {target} refused")));
            }
            Ok(self.by_zone.get(zone_id).cloned().unwrap_or_default())
        }
    }

    fn build(
        local: FakeLocal,
        remote: FakeRemote,
        registry: InMemoryZoneSearchRegistry,
        local_targets: impl IntoIterator<Item = String>,
    ) -> (
        RoutingBackend<FakeLocal, FakeRemote>,
        Arc<FakeLocal>,
        Arc<FakeRemote>,
    ) {
        let local = Arc::new(local);
        let remote = Arc::new(remote);
        let router = RoutingBackend::new(
            Arc::clone(&local),
            Arc::clone(&remote),
            Arc::new(registry),
            "self",
            local_targets,
        );
        (router, local, remote)
    }

    #[tokio::test]
    async fn unmapped_zone_routes_to_the_local_backend() {
        // Registry has no entry for `eng` — the shared local plugin
        // covers it, so the router falls through.
        let mut local = FakeLocal::default();
        local
            .by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", "eng", 5.0)]);
        let (router, local_arc, remote_arc) = build(
            local,
            FakeRemote::default(),
            InMemoryZoneSearchRegistry::new(),
            [],
        );
        let out = router.search_zone("eng", &req()).await.unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(local_arc.calls.lock().unwrap().as_slice(), ["eng"]);
        assert!(remote_arc.calls.lock().await.is_empty());
    }

    #[tokio::test]
    async fn registry_entry_pointing_at_local_target_stays_local() {
        // "shared_for_all" wiring: every zone → loopback plugin.  A
        // naive impl would spawn a remote dial back to ourselves;
        // the local_targets set is the escape valve.
        let mut local = FakeLocal::default();
        local
            .by_zone
            .insert("eng".into(), vec![hit("/eng/a.md", "eng", 5.0)]);
        let registry = InMemoryZoneSearchRegistry::shared_for_all(
            ["eng".into(), "legal".into()],
            "http://loopback:2126",
        );
        let (router, local_arc, remote_arc) = build(
            local,
            FakeRemote::default(),
            registry,
            ["http://loopback:2126".into()],
        );
        let _ = router.search_zone("eng", &req()).await.unwrap();
        assert_eq!(local_arc.calls.lock().unwrap().as_slice(), ["eng"]);
        assert!(
            remote_arc.calls.lock().await.is_empty(),
            "self-target must not spawn a remote dial",
        );
    }

    #[tokio::test]
    async fn remote_target_mints_a_scoped_delegation() {
        let mut registry = InMemoryZoneSearchRegistry::new();
        registry.insert("legal", "http://peer-legal:2126");
        let mut remote = FakeRemote::default();
        remote
            .by_zone
            .insert("legal".into(), vec![hit("/legal/x.md", "legal", 4.0)]);
        let (router, local_arc, remote_arc) = build(FakeLocal::default(), remote, registry, []);

        let out = router.search_zone("legal", &req()).await.unwrap();
        assert_eq!(out.len(), 1);
        assert!(
            local_arc.calls.lock().unwrap().is_empty(),
            "remote zone must not touch the local backend",
        );
        let calls = remote_arc.calls.lock().await;
        assert_eq!(calls.len(), 1);
        let call = &calls[0];
        assert_eq!(call.target, "http://peer-legal:2126");
        assert_eq!(call.zone_id, "legal");
        assert_eq!(call.source_zone_id, "self");
        assert_eq!(call.target_zones, vec!["legal".to_string()]);
        assert_eq!(call.subject, ("user".into(), "alice".into()));
        assert!(
            call.delegation_id.starts_with("sd_"),
            "delegation id must match Python's sd_<hex12> shape, got {}",
            call.delegation_id,
        );
        assert_eq!(
            call.delegation_id.len(),
            "sd_".len() + 12,
            "delegation id shape is `sd_<12 hex>`",
        );
    }

    #[tokio::test]
    async fn two_remote_zones_get_two_different_delegation_ids() {
        // Regression pin: mint per leg, not per dispatcher — a
        // leaked delegation from one zone cannot be replayed
        // against a sibling zone the dispatcher happened to also
        // query.
        let mut registry = InMemoryZoneSearchRegistry::new();
        registry.insert("legal", "http://peer-legal:2126");
        registry.insert("finance", "http://peer-finance:2126");
        let mut remote = FakeRemote::default();
        remote
            .by_zone
            .insert("legal".into(), vec![hit("/l.md", "legal", 1.0)]);
        remote
            .by_zone
            .insert("finance".into(), vec![hit("/f.md", "finance", 1.0)]);
        let (router, _local_arc, remote_arc) = build(FakeLocal::default(), remote, registry, []);
        let _ = router.search_zone("legal", &req()).await.unwrap();
        let _ = router.search_zone("finance", &req()).await.unwrap();
        let calls = remote_arc.calls.lock().await;
        assert_eq!(calls.len(), 2);
        assert_ne!(
            calls[0].delegation_id, calls[1].delegation_id,
            "each remote leg gets its own delegation",
        );
        // Zone allowlist is single-zone: a leaked legal delegation
        // cannot query finance and vice versa.
        assert_eq!(calls[0].target_zones, vec!["legal".to_string()]);
        assert_eq!(calls[1].target_zones, vec!["finance".to_string()]);
    }

    #[tokio::test]
    async fn remote_transport_failure_surfaces_as_backend_error() {
        // Errors on the remote side become BackendError::Transport;
        // the dispatcher above turns those into ZoneFailure entries.
        let mut registry = InMemoryZoneSearchRegistry::new();
        registry.insert("legal", "http://peer-legal:2126");
        let mut remote = FakeRemote::default();
        remote.error_zones.insert("legal".into());
        let (router, _local_arc, _remote_arc) = build(FakeLocal::default(), remote, registry, []);
        let err = router.search_zone("legal", &req()).await.unwrap_err();
        assert!(matches!(err, BackendError::Transport(_)), "{err:?}");
    }

    #[tokio::test]
    async fn ttl_override_flows_onto_the_minted_delegation() {
        // A caller passing a longer TTL for a wide-fanout request
        // must see it on the wire, not silently defaulted.
        let mut registry = InMemoryZoneSearchRegistry::new();
        registry.insert("legal", "http://peer-legal:2126");
        let (local, remote) = (FakeLocal::default(), FakeRemote::default());
        let local = Arc::new(local);
        let remote = Arc::new(remote);
        let router = RoutingBackend::new(
            Arc::clone(&local),
            Arc::clone(&remote),
            Arc::new(registry),
            "self",
            Vec::<String>::new(),
        )
        .with_delegation_ttl_seconds(300);

        // Rather than inspecting the mint TTL through the fake
        // (which does not expose ttl_seconds), inspect that the
        // delegation is not expired after > default TTL — impossible
        // to observe cleanly at test time.  Instead, mint directly
        // via the router's public API and read the TTL back.
        // (Testing the field on the mint path.)
        let d = router.mint_delegation("legal", &req());
        assert_eq!(d.ttl_seconds, 300);
    }

    #[tokio::test]
    async fn noop_remote_fails_loud_when_a_remote_zone_is_dialed() {
        // Regression pin: a single-daemon composition root that
        // wires NoOpRemoteSearchBackend + accidentally registers a
        // remote target must NOT silently drop the leg — that's the
        // "green-suite-proving-nothing" antipattern.  A caller in
        // that state gets BackendError::Config so the misconfig is
        // loud at the first request.
        use crate::backend::NoOpRemoteSearchBackend;

        let mut registry = InMemoryZoneSearchRegistry::new();
        registry.insert("legal", "http://peer-legal:2126");
        let local = Arc::new(FakeLocal::default());
        let remote = Arc::new(NoOpRemoteSearchBackend);
        let router = RoutingBackend::new(
            local,
            remote,
            Arc::new(registry),
            "self",
            Vec::<String>::new(),
        );
        let err = router.search_zone("legal", &req()).await.unwrap_err();
        match err {
            BackendError::Config(msg) => {
                assert!(msg.contains("legal"), "{msg}");
                assert!(msg.contains("NoOpRemoteSearchBackend"), "{msg}");
            }
            other => panic!("expected Config, got {other:?}"),
        }
    }

    #[test]
    fn short_uuid_shape_is_twelve_hex_chars() {
        for _ in 0..64 {
            let id = short_uuid();
            assert_eq!(id.len(), 12);
            assert!(
                id.chars().all(|c| c.is_ascii_hexdigit()),
                "expected lowercase hex, got {id:?}",
            );
        }
    }
}
