//! Peer-plugin registry — env-driven address list + fan-out-zone
//! allowlist consumed by the peer-fanout dispatcher.
//!
//! # Why env, not the kernel
//!
//! `KernelHandle` currently exposes only file-level syscalls
//! (`sys_read`, `sys_readdir`, `sys_stat`, …).  There is no
//! `sys_list_peers` / `sys_get_zones` today, and the plan explicitly
//! carves peer discovery as a plugin-owned concern so the fan-out
//! roll-out does not need a kernel-ABI bump.  A future kernel-team
//! item may add dynamic discovery; when that lands we'll opt into it
//! behind the same [`PeerRegistry`] shape, and the env path stays as
//! the manual override.
//!
//! # Contract
//!
//! - `NEXUS_SEARCH_PEER_PLUGINS` — comma-separated `host:port`
//!   entries (e.g. `win.tailnet:2126,mac.tailnet:2126`).  Empty /
//!   unset ⇒ no peers ⇒ fan-out is a no-op (queries stay local).
//!
//! - `NEXUS_SEARCH_PEER_FANOUT_ZONES` — comma-separated zone id
//!   allowlist.  A query whose `zone_id` is in the list fans out to
//!   peers; queries against any other zone stay local (backwards-
//!   compatible with today's single-node behaviour).  Empty / unset
//!   ⇒ no zone fans out.
//!
//! - `NEXUS_SEARCH_PEER_TLS=true` — opt into TLS to peers.  Off by
//!   default because a plugin fleet on a private overlay (Tailnet)
//!   typically stays plaintext; the [`PeerRegistry::require_tls`]
//!   flag surfaces the choice for the dispatcher's URL builder.
//!
//! - `NEXUS_SEARCH_ALLOW_INSECURE_PEER=true` — escape hatch that
//!   suppresses the standing "refuse plaintext off-loopback" gate.
//!   The gate applies at DIAL time in [`crate::peer_fanout`] — this
//!   module just exposes the flag through the registry so callers use
//!   one surface.
//!
//! # Fail-loud posture
//!
//! Any address entry that fails to parse errors LOUD via
//! [`RegistryError`].  A silent drop of a mis-typed peer would look
//! like "the peer is offline" to an operator and mask a real config
//! bug — the plugin refuses to hand back a partial registry.

use std::collections::HashSet;

/// Environment variable — comma-separated peer `host:port` list.
pub const PEER_PLUGINS_ENV: &str = "NEXUS_SEARCH_PEER_PLUGINS";

/// Environment variable — comma-separated zone id allowlist.
pub const FANOUT_ZONES_ENV: &str = "NEXUS_SEARCH_PEER_FANOUT_ZONES";

/// Environment variable — opt into TLS on peer dials.
pub const PEER_TLS_ENV: &str = "NEXUS_SEARCH_PEER_TLS";

/// Environment variable — allow plaintext to a non-loopback peer.
/// Off by default: the standing "refuse plaintext off-loopback" rule
/// blocks such dials unless this is explicitly set.
pub const ALLOW_INSECURE_PEER_ENV: &str = "NEXUS_SEARCH_ALLOW_INSECURE_PEER";

/// Parsed peer address — `host` (DNS name or IP literal) + `port`.
///
/// Kept a plain data struct so tests can construct them directly
/// without touching the env path.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PeerAddress {
    pub host: String,
    pub port: u16,
}

impl PeerAddress {
    /// Wire URL for the peer, tls-aware.  Returns
    /// `http://host:port` or `https://host:port`.  gRPC's
    /// `tonic::transport::Endpoint::from_shared` accepts either.
    pub fn url(&self, tls: bool) -> String {
        let scheme = if tls { "https" } else { "http" };
        format!(
            "{scheme}://{host}:{port}",
            host = self.host,
            port = self.port
        )
    }

    /// Loopback detection — used by the caller's dial-time gate to
    /// know whether the `NEXUS_SEARCH_ALLOW_INSECURE_PEER` escape
    /// applies.  Matches literal `localhost` and standard IPv4/IPv6
    /// loopback ranges; DNS names that RESOLVE to loopback are not
    /// covered — the check is intentionally cheap and conservative
    /// (better to error on a wrapped DNS name than to punch through
    /// the gate on a misconfigured resolver).
    pub fn is_loopback(&self) -> bool {
        let h = self.host.trim().to_ascii_lowercase();
        if h == "localhost" {
            return true;
        }
        if let Ok(ip) = h.parse::<std::net::IpAddr>() {
            return ip.is_loopback();
        }
        false
    }
}

/// Errors from registry construction.
#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    /// A `host:port` entry did not parse.  Includes the offending
    /// substring for an operator to fix.
    #[error("bad peer entry {entry:?}: {reason}")]
    BadPeer { entry: String, reason: String },
}

/// Snapshot of the peer-fanout topology as configured by env at
/// plugin startup.  Immutable after construction — a `RwLock` or
/// dynamic-discovery mechanism would need to REPLACE the entire
/// registry, not mutate this struct in place.
#[derive(Debug, Clone)]
pub struct PeerRegistry {
    peers: Vec<PeerAddress>,
    fanout_zones: HashSet<String>,
    require_tls: bool,
    allow_insecure_peer: bool,
}

impl PeerRegistry {
    /// Parse the registry from process env.
    pub fn from_env() -> Result<Self, RegistryError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Env-shape parser with an injectable lookup so unit tests never
    /// race process-global env across parallel test threads.
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, RegistryError> {
        let peers = parse_peer_list(get(PEER_PLUGINS_ENV).as_deref().unwrap_or(""))?;
        let fanout_zones: HashSet<String> = get(FANOUT_ZONES_ENV)
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .collect();
        let require_tls = parse_bool(get(PEER_TLS_ENV).as_deref().unwrap_or(""));
        let allow_insecure_peer = parse_bool(get(ALLOW_INSECURE_PEER_ENV).as_deref().unwrap_or(""));
        Ok(Self {
            peers,
            fanout_zones,
            require_tls,
            allow_insecure_peer,
        })
    }

    /// Construct directly — for tests that need explicit control over
    /// every field without going through env.
    pub fn new(
        peers: Vec<PeerAddress>,
        fanout_zones: HashSet<String>,
        require_tls: bool,
        allow_insecure_peer: bool,
    ) -> Self {
        Self {
            peers,
            fanout_zones,
            require_tls,
            allow_insecure_peer,
        }
    }

    /// Configured peers.  Empty when `NEXUS_SEARCH_PEER_PLUGINS` was
    /// unset — callers should treat that as "no fan-out" and skip
    /// dispatch entirely.
    pub fn peers(&self) -> &[PeerAddress] {
        &self.peers
    }

    /// True if the given zone id is in the allowlist.  Callers gate
    /// peer fan-out on this — zones outside the allowlist keep the
    /// single-node behaviour.
    pub fn zone_fans_out(&self, zone_id: &str) -> bool {
        self.fanout_zones.contains(zone_id)
    }

    /// Whether peer dials must use TLS.
    pub fn require_tls(&self) -> bool {
        self.require_tls
    }

    /// Whether the caller has explicitly opted out of the "refuse
    /// plaintext off-loopback" gate.  The gate itself lives at the
    /// dial site so this module stays a pure config sink.
    pub fn allow_insecure_peer(&self) -> bool {
        self.allow_insecure_peer
    }

    /// Convenience — peer fan-out runs iff there are peers AND at
    /// least one zone is in the allowlist.  Query-time hot path uses
    /// this before the zone check to skip a HashSet lookup when
    /// fan-out is turned off entirely.
    pub fn is_active(&self) -> bool {
        !self.peers.is_empty() && !self.fanout_zones.is_empty()
    }
}

fn parse_peer_list(raw: &str) -> Result<Vec<PeerAddress>, RegistryError> {
    raw.split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(parse_peer_entry)
        .collect()
}

fn parse_peer_entry(entry: &str) -> Result<PeerAddress, RegistryError> {
    // Accept "host:port" — no scheme, no path.  Deliberately strict:
    // a stray "http://" or trailing "/foo" is almost certainly a
    // misconfig (the wrapper adds the scheme itself); fail loud
    // rather than silently accepting an ambiguous form.
    let bad = |reason: &str| RegistryError::BadPeer {
        entry: entry.to_string(),
        reason: reason.to_string(),
    };
    if entry.contains("://") {
        return Err(bad("do not include a scheme (http:// / https://)"));
    }
    if entry.contains('/') {
        return Err(bad("do not include a path segment"));
    }
    // IPv6 literals are written as "[::1]:2126" — carve the literal
    // out of the brackets, then split on the trailing colon+port.
    let (host, port_str) = if let Some(rest) = entry.strip_prefix('[') {
        let close = rest
            .find(']')
            .ok_or_else(|| bad("IPv6 literal missing closing ']'"))?;
        let host = &rest[..close];
        let tail = &rest[close + 1..];
        let port = tail
            .strip_prefix(':')
            .ok_or_else(|| bad("expected ':port' after IPv6 literal"))?;
        (host.to_string(), port)
    } else {
        let (h, p) = entry
            .rsplit_once(':')
            .ok_or_else(|| bad("expected host:port"))?;
        (h.to_string(), p)
    };
    if host.is_empty() {
        return Err(bad("host must not be empty"));
    }
    let port: u16 = port_str
        .parse()
        .map_err(|_| bad(&format!("port {port_str:?} is not a u16")))?;
    if port == 0 {
        return Err(bad("port must not be 0"));
    }
    Ok(PeerAddress { host, port })
}

fn parse_bool(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |k| {
            owned
                .iter()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
        }
    }

    #[test]
    fn empty_env_is_inactive() {
        let r = PeerRegistry::from_lookup(lookup(&[])).unwrap();
        assert!(r.peers().is_empty());
        assert!(!r.zone_fans_out("root"));
        assert!(!r.is_active());
    }

    #[test]
    fn peers_and_zones_parse_and_trim() {
        let r = PeerRegistry::from_lookup(lookup(&[
            (PEER_PLUGINS_ENV, " win.tailnet:2126 , mac.tailnet:2127 , "),
            (FANOUT_ZONES_ENV, "root, shared , "),
        ]))
        .unwrap();
        assert_eq!(
            r.peers(),
            &[
                PeerAddress {
                    host: "win.tailnet".into(),
                    port: 2126,
                },
                PeerAddress {
                    host: "mac.tailnet".into(),
                    port: 2127,
                },
            ]
        );
        assert!(r.zone_fans_out("root"));
        assert!(r.zone_fans_out("shared"));
        assert!(!r.zone_fans_out("other"));
        assert!(r.is_active());
    }

    #[test]
    fn ipv6_literal_parses() {
        let r = PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "[fe80::1]:2126")])).unwrap();
        assert_eq!(
            r.peers(),
            &[PeerAddress {
                host: "fe80::1".into(),
                port: 2126,
            }]
        );
    }

    #[test]
    fn bad_scheme_is_rejected() {
        let err =
            PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "http://win.tailnet:2126")]))
                .unwrap_err();
        assert!(matches!(err, RegistryError::BadPeer { .. }));
    }

    #[test]
    fn bad_port_is_rejected() {
        let err = PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "win.tailnet:abc")]))
            .unwrap_err();
        assert!(matches!(err, RegistryError::BadPeer { reason, .. } if reason.contains("port")));
        let err =
            PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "win.tailnet:0")])).unwrap_err();
        assert!(matches!(err, RegistryError::BadPeer { reason, .. } if reason.contains("port")));
    }

    #[test]
    fn missing_port_is_rejected() {
        let err =
            PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "win.tailnet")])).unwrap_err();
        assert!(matches!(err, RegistryError::BadPeer { .. }));
    }

    #[test]
    fn is_active_requires_both_peers_and_zones() {
        let peers_only = PeerRegistry::from_lookup(lookup(&[(PEER_PLUGINS_ENV, "x:1")])).unwrap();
        assert!(!peers_only.is_active(), "no zones ⇒ inactive");
        let zones_only =
            PeerRegistry::from_lookup(lookup(&[(FANOUT_ZONES_ENV, "root")])).unwrap();
        assert!(!zones_only.is_active(), "no peers ⇒ inactive");
    }

    #[test]
    fn tls_flags_parse() {
        let r = PeerRegistry::from_lookup(lookup(&[
            (PEER_PLUGINS_ENV, "x:1"),
            (FANOUT_ZONES_ENV, "root"),
            (PEER_TLS_ENV, "true"),
            (ALLOW_INSECURE_PEER_ENV, "no"),
        ]))
        .unwrap();
        assert!(r.require_tls());
        assert!(!r.allow_insecure_peer());
    }

    #[test]
    fn peer_url_switches_scheme() {
        let p = PeerAddress {
            host: "example.internal".into(),
            port: 2126,
        };
        assert_eq!(p.url(false), "http://example.internal:2126");
        assert_eq!(p.url(true), "https://example.internal:2126");
    }

    #[test]
    fn loopback_detection_covers_literal_and_ips() {
        assert!(PeerAddress {
            host: "localhost".into(),
            port: 1,
        }
        .is_loopback());
        assert!(PeerAddress {
            host: "127.0.0.1".into(),
            port: 1,
        }
        .is_loopback());
        assert!(PeerAddress {
            host: "::1".into(),
            port: 1,
        }
        .is_loopback());
        assert!(!PeerAddress {
            host: "example.com".into(),
            port: 1,
        }
        .is_loopback());
    }
}
