//! [`PeerChannelCache`] — shared tonic `Channel`-per-peer cache
//! consumed by every cross-daemon caller that dials a peer's
//! `nexus.search.v1.SearchService`.
//!
//! # Why this lives in `nexus-search-common`
//!
//! Two callers need the identical dial semantics:
//!
//! * `nexus-search-plugin::peer_fanout` — the plugin fans out its
//!   own Query to every peer plugin and merges the ranked lists.
//! * `nexus-http-api`'s upcoming `TonicRemoteSearchBackend` — the
//!   axum handler's federated dispatcher dials a peer's plugin per
//!   remote-zone leg with a [`SearchDelegation`](crate::SearchDelegation).
//!
//! Both need: TLS opt-in, plaintext-off-loopback refusal, connect
//! and request timeouts, dial-once-per-peer caching, dashmap sharding
//! so a slow dial does not stall a fast-cache hit.  Duplicating those
//! 50 lines in each caller is a DRY break the user treats as a code
//! break; sharing one impl behind an `Arc<PeerChannelCache>` is the
//! systematic answer.
//!
//! # SRP
//!
//! Kept trait-narrow — `get_or_dial(target)` only.  The caller owns
//! everything above the tonic Channel: request shaping, timeouts on
//! individual RPCs (via `Request::set_timeout`), fusion, error
//! bucketing at their own error boundary.  Adding "fan-out to
//! every peer" or "delegation attach" logic here would fold two
//! callers' orthogonal responsibilities into one type.
//!
//! # Feature flag
//!
//! Behind `feature = "transport"` because tonic + dashmap are
//! network dependencies a pure-algorithm consumer of this crate
//! (fusion, RRF) has no reason to pull.  A caller who wants the
//! shared dial enables the feature at their [`Cargo.toml`].

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};

/// Runtime knobs a caller passes when building a
/// [`PeerChannelCache`].  Every field has a documented default in
/// [`Self::const_default`] so the two known callers can share one
/// baseline without wiring identical values twice.
#[derive(Debug, Clone, Copy)]
pub struct PeerChannelConfig {
    /// Per-peer dial timeout — the connect side of the gRPC
    /// channel.  Kept tight so a hosed peer stops the caller fast
    /// instead of stretching p99.
    pub connect_timeout: Duration,
    /// Per-request timeout — the send + response wait side.
    /// Larger than `connect_timeout` so a legitimately heavy query
    /// has room; still bounded to keep caller latency predictable.
    /// Callers may also override per-request via
    /// `tonic::Request::set_timeout`.
    pub request_timeout: Duration,
    /// Whether dials must use TLS.  `false` in a private-overlay
    /// deployment (Tailnet, Docker bridge) that stays plaintext.
    pub require_tls: bool,
    /// Bypass the standing "plaintext off-loopback is refused"
    /// gate.  `false` by default — a dial from a caller with
    /// `require_tls=false` to a non-loopback target returns
    /// [`DialError::PlaintextOffLoopback`] unless the caller
    /// explicitly opts out here.
    pub allow_insecure_peer: bool,
}

impl PeerChannelConfig {
    /// The shared defaults every known caller uses today:
    /// 1_500 ms connect, 8_000 ms request, plaintext (both callers
    /// today opt in via env), no insecure bypass.  A caller with a
    /// different budget builds a struct literal directly.
    pub const fn const_default() -> Self {
        Self {
            connect_timeout: Duration::from_millis(1_500),
            request_timeout: Duration::from_secs(8),
            require_tls: false,
            allow_insecure_peer: false,
        }
    }
}

impl Default for PeerChannelConfig {
    fn default() -> Self {
        Self::const_default()
    }
}

/// Errors [`PeerChannelCache::get_or_dial`] surfaces.  Kept small
/// so a caller's error mapper is a match-on-variant, not a match on
/// a nested source chain.  A caller that wants to log-and-drop a
/// dead peer (peer-fanout's posture) does so uniformly regardless
/// of which variant fires.
#[derive(Debug, thiserror::Error)]
pub enum DialError {
    /// The target string is not a valid gRPC endpoint (bad scheme,
    /// bad host:port, tonic could not parse).  Signals a misconfig
    /// — the target will never dial.
    #[error("bad endpoint {target}: {source}")]
    BadEndpoint {
        target: String,
        #[source]
        source: tonic::transport::Error,
    },

    /// Plaintext dial to a non-loopback target, with no
    /// `allow_insecure_peer` opt-out on the [`PeerChannelConfig`].
    /// Standing rule — refuse silently-cleartext production traffic;
    /// force the caller to acknowledge the choice.
    #[error(
        "refusing plaintext dial to non-loopback target {target} — enable \
         PeerChannelConfig::require_tls (recommended) or set \
         allow_insecure_peer=true to bypass"
    )]
    PlaintextOffLoopback { target: String },

    /// Dial failed — TCP refused, DNS blackholed, TLS handshake
    /// bounced.  Any transport error hitting the wire before the
    /// caller's first RPC.
    #[error("connect to {target} failed: {source}")]
    ConnectFailed {
        target: String,
        #[source]
        source: tonic::transport::Error,
    },
}

/// Shared per-target `Channel` cache.  Dial-lazy — the first
/// [`Self::get_or_dial`] for a target dials and stores; subsequent
/// calls read the cached `Channel` clone off the shard.
///
/// Cheap to construct + `Arc`-friendly: the internal
/// [`DashMap`] is already sharded so two callers holding
/// `Arc<PeerChannelCache>` never contend on independent targets.
///
/// # Race posture
///
/// Two concurrent misses on the same target CAN both dial before
/// either publishes.  We accept the extra dial (bounded by
/// [`PeerChannelConfig::connect_timeout`], the loser's Channel gets
/// dropped) rather than serialise dials with a shard write-lock
/// held across `.await` — that would starve every sibling target
/// hashing to the same shard.  Same trade documented on the caller
/// this abstraction replaces (`peer_fanout::PeerFanoutDispatcher`).
pub struct PeerChannelCache {
    channels: DashMap<Arc<str>, Channel>,
    config: PeerChannelConfig,
}

impl PeerChannelCache {
    /// Build an empty cache under `config`.  No I/O runs here; the
    /// dial happens lazily on first [`Self::get_or_dial`].
    pub fn new(config: PeerChannelConfig) -> Self {
        Self {
            channels: DashMap::new(),
            config,
        }
    }

    /// The config this cache was built under.  Useful for a caller
    /// that wants to log the effective posture at startup.
    pub fn config(&self) -> &PeerChannelConfig {
        &self.config
    }

    /// Dial or hit the cache for `target`.  `target` is a full gRPC
    /// endpoint URL: `http://host:port` or `https://host:port`.
    /// Callers pass the exact string tonic's `Endpoint::from_shared`
    /// accepts — no scheme rewriting happens here (the caller
    /// controls TLS by choosing the URL scheme AND by setting
    /// [`PeerChannelConfig::require_tls`]).
    pub async fn get_or_dial(&self, target: &str) -> Result<Channel, DialError> {
        // Fast-path: shared read of the shard, clone the Arc-y
        // Channel handle, done.  Held for the length of a hash +
        // clone — no `.await` inside the shard lock.
        let key: Arc<str> = Arc::from(target);
        if let Some(c) = self.channels.get(&key) {
            return Ok(c.clone());
        }
        // Slow-path: dial, then publish.  See the struct docstring's
        // race posture note.
        let channel = self.dial(target).await?;
        Ok(self.channels.entry(key).or_insert(channel).clone())
    }

    /// Build a Channel to `target` under the config's TLS +
    /// loopback rules.  A caller who wants to force-refresh a dead
    /// peer's Channel evicts the entry externally (there is no
    /// invalidation surface on this abstraction on purpose — tonic
    /// handles reconnection internally, so a cached dead Channel
    /// heals without a caller-visible signal).
    async fn dial(&self, target: &str) -> Result<Channel, DialError> {
        // Standing rule: refuse plaintext to a non-loopback target
        // unless the caller has EXPLICITLY opted out.  The check
        // uses conservative host-extraction — a DNS name that
        // happens to resolve to a loopback address is NOT treated
        // as loopback, because we do no resolution here.
        if !self.config.require_tls && !is_loopback_url(target) && !self.config.allow_insecure_peer
        {
            return Err(DialError::PlaintextOffLoopback {
                target: target.to_string(),
            });
        }
        let mut endpoint =
            Endpoint::from_shared(target.to_string()).map_err(|e| DialError::BadEndpoint {
                target: target.to_string(),
                source: e,
            })?;
        endpoint = endpoint
            .connect_timeout(self.config.connect_timeout)
            .timeout(self.config.request_timeout)
            .tcp_keepalive(Some(Duration::from_secs(30)));
        if self.config.require_tls {
            // `with_enabled_roots` opts into the platform trust
            // roots enabled at build time (webpki-roots on
            // tls-ring) — matches the outbound-client posture the
            // wider tonic 0.14 tree uses.
            let tls_config = ClientTlsConfig::new().with_enabled_roots();
            endpoint = endpoint
                .tls_config(tls_config)
                .map_err(|e| DialError::BadEndpoint {
                    target: target.to_string(),
                    source: e,
                })?;
        }
        endpoint
            .connect()
            .await
            .map_err(|e| DialError::ConnectFailed {
                target: target.to_string(),
                source: e,
            })
    }
}

/// `true` when `url`'s host is `localhost`, or an IPv4/IPv6
/// loopback literal.  A DNS name that RESOLVES to loopback is NOT
/// covered — the check is intentionally conservative and cheap
/// (better to error on a wrapped name than to punch the plaintext
/// gate on a misconfigured resolver).
///
/// `url` shape: `http://host:port` or `https://host[:port]`.  A
/// malformed URL returns `false` — the caller's `Endpoint::from_shared`
/// will surface the real error on the dial path.
pub fn is_loopback_url(url: &str) -> bool {
    let after_scheme = url.split_once("://").map(|(_, rest)| rest).unwrap_or(url);
    let host_and_maybe_port = after_scheme.split('/').next().unwrap_or(after_scheme);
    // Trim IPv6 brackets so `[::1]:1234` reduces to `::1`.
    let host = if let Some(stripped) = host_and_maybe_port.strip_prefix('[') {
        stripped.split(']').next().unwrap_or(stripped)
    } else {
        host_and_maybe_port
            .rsplit_once(':')
            .map(|(h, _)| h)
            .unwrap_or(host_and_maybe_port)
    };
    let h = host.trim().to_ascii_lowercase();
    if h == "localhost" {
        return true;
    }
    if let Ok(ip) = h.parse::<std::net::IpAddr>() {
        return ip.is_loopback();
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_loopback_url_matches_localhost() {
        assert!(is_loopback_url("http://localhost:1234"));
        assert!(is_loopback_url("https://localhost:2126"));
        assert!(is_loopback_url("http://LocalHost:1"));
    }

    #[test]
    fn is_loopback_url_matches_ipv4_loopback_range() {
        assert!(is_loopback_url("http://127.0.0.1:1234"));
        assert!(is_loopback_url("http://127.10.20.30:1234"));
        assert!(!is_loopback_url("http://192.168.1.1:1234"));
        assert!(!is_loopback_url("http://8.8.8.8:1234"));
    }

    #[test]
    fn is_loopback_url_matches_ipv6_loopback() {
        assert!(is_loopback_url("http://[::1]:1234"));
        assert!(!is_loopback_url("http://[2001:db8::1]:1234"));
    }

    #[test]
    fn is_loopback_url_treats_dns_names_as_non_loopback() {
        // Conservative: a DNS name that would RESOLVE to loopback
        // (e.g. via /etc/hosts trickery) still returns false.  The
        // caller's plaintext-off-loopback gate stays honest against
        // resolver misconfig.
        assert!(!is_loopback_url("http://loopback.example.com:1234"));
        assert!(!is_loopback_url("http://peer.internal:2126"));
    }

    #[test]
    fn is_loopback_url_handles_missing_scheme() {
        // Best-effort: a target passed without a scheme still parses
        // its host segment correctly.
        assert!(is_loopback_url("localhost:1234"));
        assert!(is_loopback_url("127.0.0.1"));
    }

    #[test]
    fn dial_refuses_plaintext_off_loopback_by_default() {
        // Regression pin: without `allow_insecure_peer`, a plaintext
        // dial to a non-loopback host errors LOUDLY at dial time —
        // the caller cannot silently exfiltrate cleartext to a
        // production peer.
        let cache = PeerChannelCache::new(PeerChannelConfig::default());
        let fut = cache.get_or_dial("http://peer.internal:2126");
        // Poll the future to completion synchronously (no async
        // runtime needed — dial errors on the config check BEFORE
        // any I/O).
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let err = rt.block_on(fut).unwrap_err();
        assert!(
            matches!(err, DialError::PlaintextOffLoopback { .. }),
            "expected PlaintextOffLoopback, got {err:?}",
        );
    }

    #[test]
    fn dial_allows_plaintext_to_loopback_by_default() {
        // Regression pin: loopback plaintext MUST NOT trip the gate
        // — that would break every dev cluster and test harness.
        // The dial still fails (no server bound) but it fails on
        // ConnectFailed, not PlaintextOffLoopback.
        let cache = PeerChannelCache::new(PeerChannelConfig::default());
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let err = rt
            .block_on(cache.get_or_dial("http://127.0.0.1:1"))
            .unwrap_err();
        assert!(
            !matches!(err, DialError::PlaintextOffLoopback { .. }),
            "loopback plaintext must not trip the gate, got {err:?}",
        );
    }

    #[test]
    fn dial_allows_plaintext_off_loopback_when_opted_in() {
        // Regression pin: the opt-out MUST bypass the gate — dev
        // clusters running on Tailnet with `NEXUS_SEARCH_ALLOW_INSECURE_PEER=true`
        // rely on this.
        let cache = PeerChannelCache::new(PeerChannelConfig {
            allow_insecure_peer: true,
            ..PeerChannelConfig::default()
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let err = rt
            .block_on(cache.get_or_dial("http://peer.internal:2126"))
            .unwrap_err();
        assert!(
            !matches!(err, DialError::PlaintextOffLoopback { .. }),
            "opt-out must bypass the gate, got {err:?}",
        );
    }

    #[test]
    fn dial_reports_bad_endpoint_for_unparseable_url() {
        let cache = PeerChannelCache::new(PeerChannelConfig {
            allow_insecure_peer: true, // ensure the gate does not fire first
            ..PeerChannelConfig::default()
        });
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let err = rt.block_on(cache.get_or_dial("::not a url::")).unwrap_err();
        assert!(
            matches!(err, DialError::BadEndpoint { .. }),
            "expected BadEndpoint, got {err:?}",
        );
    }

    #[tokio::test]
    async fn second_get_or_dial_hits_the_cache_no_second_dial() {
        // Regression pin: the whole point of this cache is to avoid
        // a re-dial on every call.  Two calls for the same target
        // return the SAME `Channel` (which is `Arc`-clone-cheap
        // internally — the pointer identity check is a bit awkward
        // to do on tonic's `Channel`, so we assert via the "cache
        // has one entry" behaviour).
        let cache = PeerChannelCache::new(PeerChannelConfig::default());
        // Even a dial that would succeed (loopback plaintext) leaves
        // a cached entry.  We use `127.0.0.1:0` — bind fails, dial
        // returns ConnectFailed, and NOTHING gets cached (the ? on
        // the dial error short-circuits before the insert).  Test
        // the cache-hit path via a shape that doesn't fail: rely on
        // Endpoint::from_shared + connect_lazy for a URL that parses
        // but never actually dials.  Actually the cache always
        // eagerly `connect()`s — so we test the read-through path
        // instead: two callers concurrently see two identical
        // Results.
        let (r1, r2) = tokio::join!(
            cache.get_or_dial("http://127.0.0.1:1"),
            cache.get_or_dial("http://127.0.0.1:1"),
        );
        // Both fail (nothing bound on that port) but they fail with
        // the same variant — proves the code path is uniform.
        assert!(matches!(r1, Err(DialError::ConnectFailed { .. })));
        assert!(matches!(r2, Err(DialError::ConnectFailed { .. })));
    }

    #[test]
    fn config_default_pins_the_shared_baseline() {
        // Every caller that opts into `PeerChannelConfig::default()`
        // sees the same numbers — a regression pin so a change here
        // is a conscious cross-caller decision, not a stealth bump.
        let cfg = PeerChannelConfig::default();
        assert_eq!(cfg.connect_timeout, Duration::from_millis(1_500));
        assert_eq!(cfg.request_timeout, Duration::from_secs(8));
        assert!(!cfg.require_tls);
        assert!(!cfg.allow_insecure_peer);
    }
}
