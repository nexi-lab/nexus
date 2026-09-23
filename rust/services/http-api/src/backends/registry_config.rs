//! [`registry_from_env`] — composition-root reader that turns the
//! `NEXUS_SEARCH_REMOTE_ZONE_TARGETS` env var into a populated
//! [`nexus_search_common::InMemoryZoneSearchRegistry`] the axum
//! daemon hands to [`nexus_federated_search::RoutingBackend`].
//!
//! # Wire format
//!
//! `NEXUS_SEARCH_REMOTE_ZONE_TARGETS = "zone1=url1,zone2=url2,..."`
//!
//! * comma-separated entries;
//! * each entry: `<zone_id>=<gRPC-endpoint-URL>`;
//! * `<gRPC-endpoint-URL>` is what
//!   [`nexus_search_common::transport::PeerChannelCache::get_or_dial`]
//!   accepts — full URL with scheme (`http://` / `https://`).
//!
//! Absent / empty env → empty registry → every zone routes local
//! (the current single-daemon default).
//!
//! # Fail-loud posture
//!
//! Any malformed entry (missing `=`, empty zone id, empty URL,
//! duplicate zone id) errors LOUD via [`RegistryConfigError`].
//! The composition root propagates this out of `install_impl` so a
//! typo becomes a boot failure instead of a silently-empty registry
//! that makes every remote-zone query fall through to the local
//! plugin (which would silently mask the misconfig — the standing
//! "fail loud on partial config" memory rule applies).
//!
//! # Why the composition-root reader lives here
//!
//! The daemon (`nexus-http-api`) is the ONLY consumer of the
//! registry today — the plugin's own peer_fanout has its own
//! separate env vars (`NEXUS_SEARCH_PEER_PLUGINS`, etc.) for a
//! different concern (plugin-to-plugin content fanout, not
//! daemon-to-daemon per-zone dispatch).  Keeping the reader inside
//! this crate matches the "env parsing is a composition-root
//! concern" convention every existing daemon-side env reader
//! follows.

use nexus_search_common::InMemoryZoneSearchRegistry;

/// Environment variable — comma-separated `zone_id=url` entries.
/// Absent / empty → no remote zones registered.
pub const REMOTE_ZONE_TARGETS_ENV: &str = "NEXUS_SEARCH_REMOTE_ZONE_TARGETS";

/// Errors [`registry_from_env`] can surface.  Every variant names
/// the offending input so an operator fixing the env var sees
/// exactly what went wrong.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum RegistryConfigError {
    /// An entry did not contain `=` — cannot split into
    /// `zone_id`/`url`.  Signals a typo (e.g. `zone1:url1`
    /// instead of `zone1=url1`).
    #[error("bad remote-zone entry {entry:?}: expected 'zone_id=url', missing '='")]
    MissingSeparator { entry: String },

    /// An entry's `zone_id` (LHS of `=`) is empty.  A blank zone
    /// id can never match a real zone — refuse boot rather than
    /// silently drop it.
    #[error("bad remote-zone entry {entry:?}: empty zone_id before '='")]
    EmptyZoneId { entry: String },

    /// An entry's `url` (RHS of `=`) is empty.  A remote target
    /// with no URL cannot dial — refuse boot rather than register
    /// a target the dispatcher will hit and error on.
    #[error("bad remote-zone entry {entry:?}: empty url after '='")]
    EmptyUrl { entry: String },

    /// The same `zone_id` appears twice.  Deployment ambiguity;
    /// refuse boot rather than silently pick one.
    #[error("duplicate remote-zone entry for zone_id {zone_id:?} (last URL {url:?}); each zone must appear at most once in {env})", env = REMOTE_ZONE_TARGETS_ENV)]
    DuplicateZone { zone_id: String, url: String },
}

/// Parse the env var (or the caller's own string, for tests) into
/// a populated [`InMemoryZoneSearchRegistry`].  Empty / whitespace
/// input → empty registry.
pub fn registry_from_env_str(
    value: &str,
) -> Result<InMemoryZoneSearchRegistry, RegistryConfigError> {
    let mut registry = InMemoryZoneSearchRegistry::new();
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Ok(registry);
    }
    // Deduplicate check via a small set — cheaper than a
    // second linear scan, and the fail-loud rule wants the FIRST
    // duplicate to name the offending zone.
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for raw in trimmed.split(',') {
        let entry = raw.trim();
        if entry.is_empty() {
            // Tolerate trailing / doubled commas — a common shell
            // paste artefact; no reason to fail on `zone1=url1,`.
            continue;
        }
        let (zone_id, url) =
            entry
                .split_once('=')
                .ok_or_else(|| RegistryConfigError::MissingSeparator {
                    entry: entry.to_string(),
                })?;
        let zone_id = zone_id.trim();
        let url = url.trim();
        if zone_id.is_empty() {
            return Err(RegistryConfigError::EmptyZoneId {
                entry: entry.to_string(),
            });
        }
        if url.is_empty() {
            return Err(RegistryConfigError::EmptyUrl {
                entry: entry.to_string(),
            });
        }
        if !seen.insert(zone_id.to_string()) {
            return Err(RegistryConfigError::DuplicateZone {
                zone_id: zone_id.to_string(),
                url: url.to_string(),
            });
        }
        registry.insert(zone_id, url);
    }
    Ok(registry)
}

/// Read the process env, parse the result, return a populated
/// [`InMemoryZoneSearchRegistry`].  Absent env → empty registry.
///
/// This is the shape the composition root
/// ([`crate::install_impl`]) calls.
pub fn registry_from_env() -> Result<InMemoryZoneSearchRegistry, RegistryConfigError> {
    registry_from_env_str(&std::env::var(REMOTE_ZONE_TARGETS_ENV).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;
    use nexus_search_common::ZoneSearchRegistry;

    #[test]
    fn empty_string_yields_empty_registry() {
        let r = registry_from_env_str("").unwrap();
        assert!(r.resolve("eng").is_none());
    }

    #[test]
    fn whitespace_only_string_yields_empty_registry() {
        // Regression pin: a caller passing `NEXUS_SEARCH_REMOTE_ZONE_TARGETS=" "`
        // (whitespace only) must NOT trigger MissingSeparator on
        // the whitespace "entry" — treat as absent.
        let r = registry_from_env_str("   \t  ").unwrap();
        assert!(r.resolve("eng").is_none());
    }

    #[test]
    fn single_entry_parses_and_resolves() {
        let r = registry_from_env_str("eng=http://peer-eng:2126").unwrap();
        assert_eq!(r.resolve("eng").as_deref(), Some("http://peer-eng:2126"),);
        assert!(r.resolve("legal").is_none());
    }

    #[test]
    fn multiple_entries_each_map_correctly() {
        let r = registry_from_env_str("eng=http://peer-eng:2126,legal=https://peer-legal:2126")
            .unwrap();
        assert_eq!(r.resolve("eng").as_deref(), Some("http://peer-eng:2126"));
        assert_eq!(
            r.resolve("legal").as_deref(),
            Some("https://peer-legal:2126"),
        );
    }

    #[test]
    fn whitespace_around_entries_and_around_kv_is_tolerated() {
        // Regression pin: a caller pasting from a doc with stray
        // spaces gets a working registry.
        let r = registry_from_env_str(
            "  eng = http://peer-eng:2126  ,  legal =  http://peer-legal:2126  ",
        )
        .unwrap();
        assert_eq!(r.resolve("eng").as_deref(), Some("http://peer-eng:2126"));
        assert_eq!(
            r.resolve("legal").as_deref(),
            Some("http://peer-legal:2126")
        );
    }

    #[test]
    fn trailing_or_doubled_commas_are_tolerated() {
        // A common shell-paste artefact; refusing here would just
        // annoy operators without buying safety.
        let r = registry_from_env_str("eng=http://peer:2126,,").unwrap();
        assert_eq!(r.resolve("eng").as_deref(), Some("http://peer:2126"));
    }

    #[test]
    fn missing_separator_fails_loud() {
        // Regression pin: `zone1:url1` (colon instead of equals)
        // is a common typo — refuse boot, name the entry.
        let err = registry_from_env_str("eng:http://peer:2126").unwrap_err();
        match err {
            RegistryConfigError::MissingSeparator { entry } => {
                assert_eq!(entry, "eng:http://peer:2126");
            }
            other => panic!("expected MissingSeparator, got {other:?}"),
        }
    }

    #[test]
    fn empty_zone_id_fails_loud() {
        let err = registry_from_env_str("=http://peer:2126").unwrap_err();
        assert!(matches!(err, RegistryConfigError::EmptyZoneId { .. }));
    }

    #[test]
    fn empty_url_fails_loud() {
        let err = registry_from_env_str("eng=").unwrap_err();
        assert!(matches!(err, RegistryConfigError::EmptyUrl { .. }));
    }

    #[test]
    fn duplicate_zone_id_fails_loud() {
        // Regression pin: two entries for the same zone is
        // ambiguous (which URL wins?) — refuse rather than
        // silently pick one (last-wins would silently mask a
        // typo in the FIRST entry).
        let err = registry_from_env_str("eng=http://a:1,eng=http://b:1").unwrap_err();
        match err {
            RegistryConfigError::DuplicateZone { zone_id, url } => {
                assert_eq!(zone_id, "eng");
                // The URL named is the SECOND (offending) entry.
                assert_eq!(url, "http://b:1");
            }
            other => panic!("expected DuplicateZone, got {other:?}"),
        }
    }

    #[test]
    fn registry_from_env_reads_process_env() {
        // Guard against a race with another test tweaking the same
        // env var by using a fresh scoped set/unset via a mutex
        // — plain `set_var` is unsafe in parallel tests.  Since
        // this test only asserts the FUNCTION shape (env absent →
        // empty), we unset explicitly and inspect.
        //
        // SAFETY: we're clearing our OWN env key here; race with
        // a test using the same key is not possible because no
        // other test in this file sets it.
        unsafe {
            std::env::remove_var(REMOTE_ZONE_TARGETS_ENV);
        }
        let r = registry_from_env().unwrap();
        assert!(r.resolve("eng").is_none());
    }

    #[test]
    fn error_display_names_the_offending_entry() {
        // Regression pin: the operator-facing message MUST name
        // the exact bad input.  A `MissingSeparator` without the
        // entry text sends operators looking through their whole
        // config for the typo.
        let err = registry_from_env_str("bad-entry-no-equals").unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("bad-entry-no-equals"),
            "error message must name the entry: {msg}",
        );
    }
}
