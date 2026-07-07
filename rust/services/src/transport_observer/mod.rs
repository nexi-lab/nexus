//! TransportObserverService — data-privacy warning on cross-node reads
//! that traverse a relay hop instead of a direct P2P transport.
//!
//! ## Motivation
//!
//! Nexus is a distributed VFS.  Cross-node reads (`sys_read` via
//! `try_remote_fetch`) travel over whichever substrate the operator
//! configured — today Tailscale-over-WireGuard, tomorrow possibly other
//! overlay networks or direct SSH tunnels.  Tailscale's design falls
//! back to a **DERP relay** when direct NAT-punch fails; the operator
//! wants to know when their bytes are transiting a third-party relay
//! (data-privacy concern — the WireGuard encryption still applies, but
//! traffic patterns / SPOF / lag / SLA changes are visible signals).
//!
//! ## Design
//!
//! `TransportObserverService` implements the kernel's existing
//! [`MutationObserver`] trait, filtering on
//! [`FileEventType::RemoteFetch`] events (nexus-vfs PR #121).  For each
//! such event it consults a [`TransportPathResolver`] — an abstract
//! substrate-lookup — to classify the current path to `remote_addr` as
//! [`TransportPath::Direct`], [`TransportPath::Relay`], or
//! [`TransportPath::Unknown`].  Relay classification emits a warning
//! (`tracing::warn!`) and increments an atomic counter that operators
//! can scrape via any future metrics endpoint.
//!
//! ### Layering discipline
//!
//! The kernel emits `RemoteFetch` events with `remote_addr` as an
//! **opaque string** — kernel knows nothing about Tailscale.  This
//! service owns Tailscale semantics via the concrete
//! [`TailscaleResolver`] impl of the [`TransportPathResolver`] trait.
//! Alternative substrates (Nebula, WireGuard mesh without Tailscale,
//! WebRTC, S3-tier tracking) plug in via alternate resolvers — same
//! service, swap the resolver.

use std::collections::HashMap;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use parking_lot::RwLock;

use kernel::core::dispatch::{FileEvent, FileEventType, MutationObserver};
use kernel::kernel::{Kernel, KernelError};

/// Service tag used for `Kernel::register_service_observer` bookkeeping.
/// Matches the crate feature flag (`service-transport-observer`) minus the
/// `service-` prefix so operator-visible surfaces (logs, future
/// unregister API) share a single identifier with the build gate.
pub(crate) const SERVICE_NAME: &str = "transport-observer";

/// Observer-registry key.  Distinct from the service tag because
/// `ObserverRegistry` unregisters by observer name; kept identical
/// here since transport-observer registers exactly one observer.
pub(crate) const OBSERVER_NAME: &str = "transport-observer";

/// Poll cadence for `TailscaleResolver`.  30s matches Tailscale's own
/// endpoint-change notification interval closely enough that operators
/// won't observe stale classifications for realistic connection
/// lifetimes.
const TAILSCALE_REFRESH_INTERVAL: Duration = Duration::from_secs(30);

/// Classification of the network path an opaque remote address is
/// currently reachable through.  Consumer of the substrate lookup.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TransportPath {
    /// Direct P2P transport (e.g. Tailscale WireGuard direct).  No
    /// third-party relay involved.  Ideal for data-privacy.
    Direct,
    /// Bytes route through a third-party relay (e.g. Tailscale DERP).
    /// `via` names the relay for operator diagnostics.
    Relay { via: String },
    /// Substrate has no information about this address (peer not in
    /// the tailnet, resolver failed, tailscale CLI absent, etc.).
    /// Treated as "not proven direct" — the observer emits a warning
    /// under the `Warn` policy just like a relay, since we can't
    /// certify privacy.
    Unknown,
}

/// Substrate-specific transport-path lookup.  The kernel emits opaque
/// `remote_addr` strings; concrete impls interpret them (a Tailscale
/// resolver keys on `"host:port"` where host is a `100.64.0.x` IP; a
/// future S3 resolver keys on bucket names; etc.).
pub trait TransportPathResolver: Send + Sync {
    /// Classify the current network path to `remote_addr`.  Called on
    /// every `RemoteFetch` event inline on the sys_read caller thread
    /// (see nexus-vfs `kernel/observability.rs::dispatch_observers`),
    /// so implementations MUST stay fast: cache aggressively (RwLock +
    /// pre-computed map) and never issue I/O in the resolver body.
    /// Network fetches / RPCs belong on a background poller thread
    /// that populates the cache the resolver reads from.
    fn resolve(&self, remote_addr: &str) -> TransportPath;
}

/// Operator-selectable behaviour when a `RemoteFetch` event's path is
/// classified as Relay or Unknown (not proven direct).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransportPolicy {
    /// Warn (log + metric) on relay/unknown paths; allow the read.
    /// Fail-open — the read already returned data by the time the
    /// observer fires (post-mutation dispatch is fire-and-forget).
    Warn,
    /// Never emit warnings; silently accept every path.  Use when
    /// operator has other observability and doesn't want log noise.
    AllowAll,
}

/// Cached tailscale-status snapshot keyed by peer `100.64.0.x` IP
/// (without port).  Populated by [`TailscaleResolver::refresh`] on the
/// background poller thread; read lock-free-ish via `RwLock` on the
/// observer thread.
type PathMap = HashMap<String, TransportPath>;

/// Concrete [`TransportPathResolver`] impl for Tailscale.  Polls
/// `tailscale status --json` at construction and every
/// `refresh_interval`, populating an internal cache indexed by peer
/// tailnet IP.
///
/// **Threading model**: the periodic refresh runs on a dedicated OS
/// thread (not tokio — this crate targets both async and sync
/// consumers).  30s cadence matches Tailscale's own endpoint-change
/// notification interval closely enough that operators won't observe
/// stale classifications for realistic connection lifetimes.
pub struct TailscaleResolver {
    cache: Arc<RwLock<PathMap>>,
}

impl TailscaleResolver {
    /// Spawn the poller and return a resolver.  Caller must hold the
    /// returned [`Arc`] for the lifetime of the daemon — dropping the
    /// last strong reference stops the poller.
    pub fn spawn(refresh_interval: Duration) -> Arc<Self> {
        let resolver = Arc::new(Self {
            cache: Arc::new(RwLock::new(PathMap::new())),
        });
        Self::spawn_poller(Arc::clone(&resolver.cache), refresh_interval);
        // Prime the cache once synchronously so early reads don't see
        // an empty map for the first refresh_interval.
        Self::refresh_cache(&resolver.cache, None);
        resolver
    }

    #[cfg(test)]
    pub(crate) fn with_static_status(status_json: String) -> Arc<Self> {
        let resolver = Arc::new(Self {
            cache: Arc::new(RwLock::new(PathMap::new())),
        });
        Self::refresh_cache(&resolver.cache, Some(&status_json));
        resolver
    }

    fn spawn_poller(cache: Arc<RwLock<PathMap>>, refresh_interval: Duration) {
        std::thread::Builder::new()
            .name("transport-observer/tailscale-poller".into())
            .spawn(move || {
                // Weak downgrade so poller exits when observer service drops.
                let weak = Arc::downgrade(&cache);
                loop {
                    std::thread::sleep(refresh_interval);
                    let Some(strong) = weak.upgrade() else {
                        break;
                    };
                    Self::refresh_cache(&strong, None);
                }
            })
            .ok();
    }

    fn refresh_cache(cache: &RwLock<PathMap>, override_json: Option<&str>) {
        let json = match override_json {
            Some(s) => s.to_string(),
            None => match Self::query_tailscale_status() {
                Ok(s) => s,
                Err(_) => return, // silent — offline / not-installed is normal
            },
        };
        let map = Self::parse_status_json(&json);
        *cache.write() = map;
    }

    fn query_tailscale_status() -> Result<String, String> {
        let out = Command::new("tailscale")
            .args(["status", "--json"])
            .output()
            .map_err(|e| format!("spawn tailscale: {e}"))?;
        if !out.status.success() {
            return Err(format!("tailscale exit {:?}", out.status.code()));
        }
        String::from_utf8(out.stdout).map_err(|e| format!("utf8: {e}"))
    }

    /// Parse `tailscale status --json` output into a peer-IP → path map.
    ///
    /// Tailscale's schema per peer under `.Peer`:
    /// - `TailscaleIPs: [String]` — the `100.64.0.x` addresses
    /// - `Relay: String` — DERP relay name (empty when direct)
    /// - `CurAddr: String` — current direct endpoint (empty when relay)
    ///
    /// Interpretation: `CurAddr` non-empty ⇒ Direct.  Otherwise `Relay`
    /// non-empty ⇒ Relay { via }.  Both empty ⇒ Unknown (not yet
    /// connected or offline).
    fn parse_status_json(json: &str) -> PathMap {
        let mut map = PathMap::new();
        let Ok(v): Result<serde_json::Value, _> = serde_json::from_str(json) else {
            return map;
        };
        let Some(peers) = v.get("Peer").and_then(|p| p.as_object()) else {
            return map;
        };
        for (_key, peer) in peers.iter() {
            let ips = peer
                .get("TailscaleIPs")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default();
            let cur_addr = peer
                .get("CurAddr")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let relay = peer
                .get("Relay")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let path = if !cur_addr.is_empty() {
                TransportPath::Direct
            } else if !relay.is_empty() {
                TransportPath::Relay {
                    via: relay.to_string(),
                }
            } else {
                TransportPath::Unknown
            };
            for ip in ips {
                if let Some(ip_str) = ip.as_str() {
                    map.insert(ip_str.to_string(), path.clone());
                }
            }
        }
        map
    }
}

impl TransportPathResolver for TailscaleResolver {
    fn resolve(&self, remote_addr: &str) -> TransportPath {
        // Kernel stamps `remote_addr` as the operator-facing "host:port"
        // form; strip the port for tailscale IP lookup.
        let host = remote_addr.rsplit_once(':').map_or(remote_addr, |(h, _)| h);
        // Strip leading "http://" / "https://" if the substrate stamped
        // a URL (raft transport uses URL form for gRPC endpoints).
        let host = host
            .trim_start_matches("http://")
            .trim_start_matches("https://");
        self.cache
            .read()
            .get(host)
            .cloned()
            .unwrap_or(TransportPath::Unknown)
    }
}

/// TransportObserverService — the actual [`MutationObserver`] plugged
/// into kernel dispatch.  Filters `RemoteFetch` events and produces a
/// warning per configured policy.
pub struct TransportObserverService {
    resolver: Arc<dyn TransportPathResolver>,
    policy: TransportPolicy,
    warn_count: AtomicU64,
    direct_count: AtomicU64,
    unknown_count: AtomicU64,
}

impl TransportObserverService {
    /// Wire the service with a policy and a substrate-specific
    /// resolver.  Caller registers via `Kernel::register_service_observer`.
    pub fn new(resolver: Arc<dyn TransportPathResolver>, policy: TransportPolicy) -> Self {
        Self {
            resolver,
            policy,
            warn_count: AtomicU64::new(0),
            direct_count: AtomicU64::new(0),
            unknown_count: AtomicU64::new(0),
        }
    }

    /// Number of RemoteFetch events classified as Relay whose warning
    /// fired (post-policy).  Exposed for Prometheus-scrape callers.
    pub fn relay_warn_count(&self) -> u64 {
        self.warn_count.load(Ordering::Relaxed)
    }

    /// Number of RemoteFetch events classified as Direct.  Ratio with
    /// [`relay_warn_count`] answers "what fraction of my data traffic
    /// was point-to-point?"
    pub fn direct_count(&self) -> u64 {
        self.direct_count.load(Ordering::Relaxed)
    }

    /// Number of RemoteFetch events where the resolver returned
    /// Unknown (peer not in tailnet, cache miss, tailscale absent).
    pub fn unknown_count(&self) -> u64 {
        self.unknown_count.load(Ordering::Relaxed)
    }
}

/// Boot-time install for `TransportObserverService`.
///
/// Constructs a [`TailscaleResolver`] with the crate's default 30s
/// refresh cadence, wraps it with [`TransportPolicy::Warn`], and
/// registers the service with kernel dispatch under the
/// [`SERVICE_NAME`] tag filtering [`FileEventType::RemoteFetch`].
///
/// Mirrors the shape of [`services::audit::install_root`] — same
/// LSM-style boot flow, called once by the cluster main during daemon
/// bring-up when the `service-transport-observer` feature is enabled.
/// Discards the service `Arc`; use [`install_with`] instead when the
/// caller needs to hold a reference (tests, custom bootstrappers).
pub fn install(kernel: &Arc<Kernel>) -> Result<(), KernelError> {
    let resolver = TailscaleResolver::spawn(TAILSCALE_REFRESH_INTERVAL);
    install_with(
        kernel,
        resolver as Arc<dyn TransportPathResolver>,
        TransportPolicy::Warn,
    )
    .map(|_| ())
}

/// DI variant of [`install`] — accepts a pre-built resolver + policy so
/// tests can drive the service through kernel dispatch without spawning
/// the Tailscale poller thread, and custom bootstrappers can plug in
/// alternate substrate resolvers.  Returns the service `Arc` so the
/// caller can read the counters after dispatch.
pub fn install_with(
    kernel: &Arc<Kernel>,
    resolver: Arc<dyn TransportPathResolver>,
    policy: TransportPolicy,
) -> Result<Arc<TransportObserverService>, KernelError> {
    let svc = Arc::new(TransportObserverService::new(resolver, policy));
    // Discriminant IS the bit value (see kernel `FileEventType`
    // `= 1 << N` variants); matches the `as u32` idiom in
    // `services::audit::install_root`.
    kernel.register_service_observer(
        SERVICE_NAME,
        Arc::clone(&svc) as Arc<dyn MutationObserver>,
        OBSERVER_NAME.to_string(),
        FileEventType::RemoteFetch as u32,
    );
    Ok(svc)
}

impl MutationObserver for TransportObserverService {
    fn on_mutation(&self, event: &FileEvent) {
        if event.event_type != FileEventType::RemoteFetch {
            return;
        }
        let remote_addr = match event.remote_addr() {
            Some(addr) if !addr.is_empty() => addr,
            _ => return,
        };
        let path = self.resolver.resolve(remote_addr);
        match &path {
            TransportPath::Direct => {
                self.direct_count.fetch_add(1, Ordering::Relaxed);
            }
            TransportPath::Relay { via } => {
                self.warn_count.fetch_add(1, Ordering::Relaxed);
                if self.policy == TransportPolicy::Warn {
                    tracing::warn!(
                        target: "transport_observer",
                        remote_addr = %remote_addr,
                        via = %via,
                        path = %event.path(),
                        bytes = event.size().unwrap_or(0),
                        "distributed-VFS remote-fetch traversed relay — data-privacy caution",
                    );
                }
            }
            TransportPath::Unknown => {
                self.unknown_count.fetch_add(1, Ordering::Relaxed);
                if self.policy == TransportPolicy::Warn {
                    tracing::warn!(
                        target: "transport_observer",
                        remote_addr = %remote_addr,
                        path = %event.path(),
                        bytes = event.size().unwrap_or(0),
                        "distributed-VFS remote-fetch path unclassified (substrate lookup returned Unknown)",
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic resolver for unit tests — no OS deps.
    struct StaticResolver(HashMap<String, TransportPath>);

    impl TransportPathResolver for StaticResolver {
        fn resolve(&self, addr: &str) -> TransportPath {
            let host = addr
                .rsplit_once(':')
                .map_or(addr, |(h, _)| h)
                .trim_start_matches("http://")
                .trim_start_matches("https://");
            self.0.get(host).cloned().unwrap_or(TransportPath::Unknown)
        }
    }

    fn make_event(remote_addr: &str, bytes: u64) -> FileEvent {
        FileEvent::remote_fetch("/shared/x.json", remote_addr, bytes)
    }

    #[test]
    fn ignores_non_remote_fetch_events() {
        // Verifies event-type discrimination — a service registered on
        // the general observer stream must reject other event types
        // silently (no counter change, no warning).  Uses `with_zone`
        // (public ctor) with FileWrite; even though remote_addr is
        // unpopulated, we never reach the resolver because event_type
        // filters out.
        let mut map = HashMap::new();
        map.insert("100.64.0.21".to_string(), TransportPath::Direct);
        let svc =
            TransportObserverService::new(Arc::new(StaticResolver(map)), TransportPolicy::Warn);
        let event = FileEvent::with_zone(FileEventType::FileWrite, "/w.json", "root");
        svc.on_mutation(&event);
        assert_eq!(svc.direct_count(), 0);
        assert_eq!(svc.relay_warn_count(), 0);
        assert_eq!(svc.unknown_count(), 0);
    }

    #[test]
    fn direct_path_counts_but_does_not_warn() {
        let mut map = HashMap::new();
        map.insert("100.64.0.21".to_string(), TransportPath::Direct);
        let svc =
            TransportObserverService::new(Arc::new(StaticResolver(map)), TransportPolicy::Warn);
        svc.on_mutation(&make_event("100.64.0.21:2126", 4096));
        assert_eq!(svc.direct_count(), 1);
        assert_eq!(svc.relay_warn_count(), 0);
        assert_eq!(svc.unknown_count(), 0);
    }

    #[test]
    fn relay_path_increments_warn_counter() {
        let mut map = HashMap::new();
        map.insert(
            "100.64.0.21".to_string(),
            TransportPath::Relay {
                via: "headscale".to_string(),
            },
        );
        let svc =
            TransportObserverService::new(Arc::new(StaticResolver(map)), TransportPolicy::Warn);
        svc.on_mutation(&make_event("100.64.0.21:2126", 4096));
        assert_eq!(svc.relay_warn_count(), 1);
        assert_eq!(svc.direct_count(), 0);
    }

    #[test]
    fn unknown_path_increments_unknown_counter() {
        // Peer not in the tailnet cache — treated as unknown, warned
        // under Warn policy (we can't certify direct).
        let svc = TransportObserverService::new(
            Arc::new(StaticResolver(HashMap::new())),
            TransportPolicy::Warn,
        );
        svc.on_mutation(&make_event("192.0.2.5:2126", 512));
        assert_eq!(svc.unknown_count(), 1);
        assert_eq!(svc.direct_count(), 0);
        assert_eq!(svc.relay_warn_count(), 0);
    }

    #[test]
    fn allow_all_policy_still_counts_but_stays_silent() {
        // Counters must still increment under AllowAll so operators
        // that use metrics-only observability still get data.  Only
        // the log emission is gated.
        let mut map = HashMap::new();
        map.insert(
            "100.64.0.21".to_string(),
            TransportPath::Relay {
                via: "derp".to_string(),
            },
        );
        let svc =
            TransportObserverService::new(Arc::new(StaticResolver(map)), TransportPolicy::AllowAll);
        svc.on_mutation(&make_event("100.64.0.21:2126", 8192));
        assert_eq!(svc.relay_warn_count(), 1);
    }

    #[test]
    fn resolver_strips_port_and_url_prefix() {
        // Kernel may stamp `remote_addr` as either bare `IP:port` (from
        // raft peer table) or `http://IP:port` (from gRPC transport
        // endpoint form).  Both must resolve to the same tailnet IP.
        let mut map = HashMap::new();
        map.insert("100.64.0.21".to_string(), TransportPath::Direct);
        let resolver = StaticResolver(map);
        assert_eq!(resolver.resolve("100.64.0.21:2126"), TransportPath::Direct);
        assert_eq!(
            resolver.resolve("http://100.64.0.21:2126"),
            TransportPath::Direct
        );
        assert_eq!(
            resolver.resolve("https://100.64.0.21:2126"),
            TransportPath::Direct
        );
    }

    #[test]
    fn tailscale_resolver_parses_direct_and_relay() {
        // Fixture matches the shape of `tailscale status --json` on a
        // live tailnet with one direct peer and one DERP-relayed peer.
        let json = r#"{
            "Peer": {
                "nodekey:aaaa": {
                    "TailscaleIPs": ["100.64.0.21"],
                    "CurAddr": "192.168.1.3:41641",
                    "Relay": "headscale"
                },
                "nodekey:bbbb": {
                    "TailscaleIPs": ["100.64.0.22"],
                    "CurAddr": "",
                    "Relay": "headscale"
                },
                "nodekey:cccc": {
                    "TailscaleIPs": ["100.64.0.23"],
                    "CurAddr": "",
                    "Relay": ""
                }
            }
        }"#;
        let resolver = TailscaleResolver::with_static_status(json.to_string());
        assert_eq!(
            resolver.resolve("100.64.0.21:2126"),
            TransportPath::Direct,
            "peer with CurAddr must classify as Direct"
        );
        assert_eq!(
            resolver.resolve("http://100.64.0.22:2126"),
            TransportPath::Relay {
                via: "headscale".to_string()
            },
            "peer without CurAddr but with Relay must classify as Relay",
        );
        assert_eq!(
            resolver.resolve("100.64.0.23:2126"),
            TransportPath::Unknown,
            "peer with neither CurAddr nor Relay must classify as Unknown",
        );
        assert_eq!(
            resolver.resolve("100.64.0.99:2126"),
            TransportPath::Unknown,
            "peer not in the tailnet must classify as Unknown",
        );
    }

    #[test]
    fn tailscale_resolver_survives_malformed_json() {
        // `tailscale status --json` output can vary across versions;
        // parser must not panic on unexpected shapes.
        let resolver = TailscaleResolver::with_static_status("not json".to_string());
        assert_eq!(resolver.resolve("100.64.0.21:2126"), TransportPath::Unknown);
    }

    // ── Multi-step workflow tests (mirror the ../.claude/skills/
    //    integration-test-generator standard: sequential steps with
    //    data-flow between them, not independent operations) ──

    #[test]
    fn workflow_operator_boot_and_sees_mixed_direct_and_relay_traffic() {
        // Simulates a full operator workflow:
        //   1. Boot: Tailscale reports one peer direct, one via DERP relay
        //      (typical mixed home-LAN + mobile-hotspot scenario).
        //   2. Cross-node reads happen — several direct-peer fetches, one
        //      relay-peer fetch, one unknown-peer fetch.
        //   3. Operator queries counters — must reflect exact per-path totals.
        // This mirrors what the operator sees in real production, and
        // catches integration bugs unit tests miss (e.g. resolver caching
        // the wrong peer, counter aliasing between paths).
        let json = r#"{
            "Peer": {
                "nk:aaa": {
                    "TailscaleIPs": ["100.64.0.21"],
                    "CurAddr": "192.168.1.3:41641",
                    "Relay": "headscale"
                },
                "nk:bbb": {
                    "TailscaleIPs": ["100.64.0.22"],
                    "CurAddr": "",
                    "Relay": "headscale"
                }
            }
        }"#;
        let resolver = TailscaleResolver::with_static_status(json.to_string());
        let svc = TransportObserverService::new(resolver, TransportPolicy::Warn);

        // Direct peer — 3 fetches
        for _ in 0..3 {
            svc.on_mutation(&make_event("100.64.0.21:2126", 1024));
        }
        // Relay peer — 1 fetch (this is the operator-visible warning)
        svc.on_mutation(&make_event("100.64.0.22:2126", 2048));
        // Unknown peer (not in tailnet) — 2 fetches
        for _ in 0..2 {
            svc.on_mutation(&make_event("100.64.0.99:2126", 512));
        }

        assert_eq!(svc.direct_count(), 3, "3 direct fetches");
        assert_eq!(svc.relay_warn_count(), 1, "1 relay warn");
        assert_eq!(svc.unknown_count(), 2, "2 unknown-peer fetches");
    }

    #[test]
    fn workflow_concurrent_events_produce_correct_counter_totals() {
        // The kernel's `dispatch_observers` runs on a ThreadPool — the
        // observer's `on_mutation` MUST tolerate concurrent invocation
        // without lost counter increments (atomic ordering) or
        // interior-mutability panics.  Simulate: 8 threads × 100 events
        // each = 800 total events, all Direct.  Final `direct_count()`
        // must equal 800.  Regression guard for accidentally introducing
        // a Mutex or non-atomic counter later.
        let mut map = HashMap::new();
        map.insert("100.64.0.21".to_string(), TransportPath::Direct);
        let svc = Arc::new(TransportObserverService::new(
            Arc::new(StaticResolver(map)),
            TransportPolicy::AllowAll,
        ));

        let mut handles = Vec::new();
        for _ in 0..8 {
            let svc = Arc::clone(&svc);
            handles.push(std::thread::spawn(move || {
                for _ in 0..100 {
                    svc.on_mutation(&make_event("100.64.0.21:2126", 128));
                }
            }));
        }
        for h in handles {
            h.join().expect("thread panic");
        }

        assert_eq!(
            svc.direct_count(),
            800,
            "concurrent dispatch must not lose or double-count events",
        );
        assert_eq!(svc.relay_warn_count(), 0);
        assert_eq!(svc.unknown_count(), 0);
    }

    #[test]
    fn workflow_tailscale_status_refresh_updates_classification() {
        // Simulates the operator's home-LAN → mobile-hotspot transition
        // that motivated this service in the first place:
        //   1. Initial state: peer is direct (both on same LAN).
        //   2. Peer moves to mobile hotspot; tailscale reclassifies to relay.
        //   3. Subsequent reads must classify as relay, not stale-direct.
        // This is the "SPOF/lag concerns become visible" workflow the
        // service exists to signal.
        let initial_json = r#"{"Peer":{"nk:x":{
            "TailscaleIPs":["100.64.0.21"],
            "CurAddr":"192.168.1.3:41641",
            "Relay":""
        }}}"#;
        let resolver = TailscaleResolver::with_static_status(initial_json.to_string());
        assert_eq!(
            resolver.resolve("100.64.0.21:2126"),
            TransportPath::Direct,
            "initial state — LAN direct",
        );

        // Peer switches network — tailscale refreshes status.
        let after_hotspot_json = r#"{"Peer":{"nk:x":{
            "TailscaleIPs":["100.64.0.21"],
            "CurAddr":"",
            "Relay":"headscale"
        }}}"#;
        TailscaleResolver::refresh_cache(&resolver.cache, Some(after_hotspot_json));

        assert_eq!(
            resolver.resolve("100.64.0.21:2126"),
            TransportPath::Relay {
                via: "headscale".to_string()
            },
            "after hotspot transition — must reclassify to relay",
        );

        // Wire through observer — the reclassification must reach the
        // service's counters on subsequent events (not stuck on cached
        // Direct verdict).
        let svc = TransportObserverService::new(resolver, TransportPolicy::Warn);
        svc.on_mutation(&make_event("100.64.0.21:2126", 4096));
        assert_eq!(svc.relay_warn_count(), 1);
        assert_eq!(
            svc.direct_count(),
            0,
            "must not classify as Direct post-refresh"
        );
    }

    // ── Integration test: install() through kernel dispatch ──────────
    //
    // Verifies the full boot-wire path — install_with() registers the
    // observer with the correct event mask, kernel.dispatch_observers
    // fans out to it, and only RemoteFetch events reach on_mutation.
    // Uses install_with (StaticResolver) so the test does not spawn the
    // TailscaleResolver poller thread or shell out to `tailscale`.
    #[test]
    fn install_wires_observer_and_receives_dispatched_remote_fetch() {
        use kernel::kernel::Kernel;

        let kernel = Arc::new(Kernel::new());
        let mut map = HashMap::new();
        map.insert("100.64.0.21".to_string(), TransportPath::Direct);
        map.insert(
            "100.64.0.22".to_string(),
            TransportPath::Relay {
                via: "headscale".to_string(),
            },
        );
        let resolver: Arc<dyn TransportPathResolver> = Arc::new(StaticResolver(map));
        let svc =
            install_with(&kernel, resolver, TransportPolicy::Warn).expect("install_with succeeds");

        // Non-RemoteFetch events routed through kernel dispatch must be
        // filtered by the event mask before reaching the service — the
        // observer is registered with RemoteFetch.bit() only, so
        // FileWrite must not increment any counter.
        // FileEvent::with_zone is the public constructor for peer-crate
        // observer tests (FileEvent::new is pub(crate)).
        kernel.dispatch_observers(&FileEvent::with_zone(
            FileEventType::FileWrite,
            "/x.json",
            "root",
        ));
        assert_eq!(svc.direct_count(), 0);
        assert_eq!(svc.relay_warn_count(), 0);
        assert_eq!(svc.unknown_count(), 0);

        // Direct-peer RemoteFetch must reach on_mutation and increment
        // the direct counter.
        kernel.dispatch_observers(&FileEvent::remote_fetch(
            "/shared/x.json",
            "100.64.0.21:2126",
            4096,
        ));
        assert_eq!(svc.direct_count(), 1);

        // Relay-peer RemoteFetch must increment the warn counter.
        kernel.dispatch_observers(&FileEvent::remote_fetch(
            "/shared/y.json",
            "100.64.0.22:2126",
            2048,
        ));
        assert_eq!(svc.relay_warn_count(), 1);
        assert_eq!(svc.direct_count(), 1, "direct counter unchanged by relay");
    }
}
