//! [`ZoneSearchRegistry`] — the dispatcher's per-zone daemon target
//! lookup.  A federation deployment routes each zone's queries to
//! that zone's search daemon; the registry names the mapping.
//!
//! # Shape
//!
//! Production today runs one search-plugin serving every zone, so
//! every zone maps to the same target string.  A future deployment
//! that runs a per-zone plugin fleet swaps in a registry that returns
//! per-zone endpoints — the trait is stable across both.
//!
//! # SRP
//!
//! Kept trait-narrow — `resolve` only.  Capability advertising
//! (`get_capabilities` in the Python side) is a separate concern the
//! dispatcher composes on top; capability shape is deployment-
//! specific enough that pushing it into the trait would force every
//! future impl to fabricate an answer.  Add it as a second trait
//! when a caller needs it.

use std::collections::BTreeMap;
use std::sync::Arc;

/// Where a zone's search daemon lives.  A plain string so the caller
/// can plug in whatever transport contract they already use — gRPC
/// URI (`http://plugin.internal:2126`), Unix socket path, in-process
/// stub.  This crate does not interpret it.
pub type SearchDaemonTarget = Arc<str>;

/// Per-zone daemon target resolver.  Called by the dispatcher on
/// every fanout leg; must be cheap (BTreeMap lookup in every impl
/// today).
pub trait ZoneSearchRegistry: Send + Sync + 'static {
    /// The daemon target for `zone_id`, or `None` when the caller
    /// has no daemon wired for that zone (the dispatcher then routes
    /// that leg to the default target from the caller's own config
    /// — the "no per-zone endpoint, use the shared plugin" default
    /// path).
    fn resolve(&self, zone_id: &str) -> Option<SearchDaemonTarget>;
}

/// In-memory registry backed by a fixed `(zone -> target)` map.
/// Matches today's production shape: the composition root builds one
/// at boot from `NEXUS_SEARCH_PLUGIN_TARGET` and every zone answers
/// with that single target.
///
/// `Arc<str>` for the target so the dispatcher fanning out to N
/// zones clones a pointer, not the whole string, per leg.
#[derive(Debug, Clone, Default)]
pub struct InMemoryZoneSearchRegistry {
    by_zone: BTreeMap<String, SearchDaemonTarget>,
}

impl InMemoryZoneSearchRegistry {
    /// Empty registry — every `resolve` returns `None`.  Used by
    /// tests + as the safe default when the composition root has not
    /// yet wired a mapping.
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert or replace the target for `zone_id`.  Idempotent.
    pub fn insert(&mut self, zone_id: impl Into<String>, target: impl Into<Arc<str>>) {
        self.by_zone.insert(zone_id.into(), target.into());
    }

    /// Convenience: register the SAME target for every zone in
    /// `zones`.  Matches the current-production "one plugin, all
    /// zones" wiring so a caller does not have to loop insert.
    pub fn shared_for_all(
        zones: impl IntoIterator<Item = String>,
        target: impl Into<Arc<str>>,
    ) -> Self {
        let target = target.into();
        let mut r = Self::new();
        for zone in zones {
            r.by_zone.insert(zone, Arc::clone(&target));
        }
        r
    }
}

impl ZoneSearchRegistry for InMemoryZoneSearchRegistry {
    fn resolve(&self, zone_id: &str) -> Option<SearchDaemonTarget> {
        self.by_zone.get(zone_id).cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_registry_resolves_none_for_every_zone() {
        let r = InMemoryZoneSearchRegistry::new();
        assert!(r.resolve("eng").is_none());
        assert!(r.resolve("root").is_none());
    }

    #[test]
    fn insert_then_resolve_round_trips() {
        let mut r = InMemoryZoneSearchRegistry::new();
        r.insert("eng", "http://plugin-eng:2126");
        r.insert("legal", "http://plugin-legal:2126");
        assert_eq!(r.resolve("eng").as_deref(), Some("http://plugin-eng:2126"),);
        assert_eq!(
            r.resolve("legal").as_deref(),
            Some("http://plugin-legal:2126"),
        );
        assert!(r.resolve("finance").is_none());
    }

    #[test]
    fn insert_is_idempotent_last_write_wins() {
        let mut r = InMemoryZoneSearchRegistry::new();
        r.insert("eng", "http://a:1");
        r.insert("eng", "http://b:1");
        assert_eq!(r.resolve("eng").as_deref(), Some("http://b:1"));
    }

    #[test]
    fn shared_for_all_maps_every_zone_to_the_same_target() {
        // Matches today's production wiring: one search-plugin, every
        // zone routes to it.
        let r = InMemoryZoneSearchRegistry::shared_for_all(
            ["root".to_string(), "eng".to_string(), "legal".to_string()],
            "http://plugin.internal:2126",
        );
        for zone in ["root", "eng", "legal"] {
            assert_eq!(
                r.resolve(zone).as_deref(),
                Some("http://plugin.internal:2126"),
                "zone {zone} must resolve to the shared target",
            );
        }
        assert!(r.resolve("unknown").is_none());
    }
}
