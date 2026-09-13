//! [`list_accessible_zones`] — enumerate zones a subject can read.
//!
//! Walks the tuple store's snapshot and returns every zone the
//! subject holds one of the read-granting relations
//! ([`READ_GRANTING_RELATIONS`]) on.  Deduped, stable order.
//!
//! # Consumers
//!
//! The federated search dispatcher (upcoming) calls this once per
//! request to discover the zone set to fan out to, cached per subject
//! for the TTL window on [`AccessibleZonesCache`].
//!
//! # Why not `PermissionProvider`?
//!
//! `PermissionProvider` asks "may this subject perform action X on
//! object Y" — one point-in-time check.  This is the dual: "list every
//! zone the subject may read from" — a projection query.  Splitting
//! them keeps the enforcer's per-check hot path free of enumeration
//! logic and lets the dispatcher own its own cache TTL.

use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::RwLock;

use crate::store::{ReBACTupleStore, ReBACTupleStoreError};
use crate::tuple_key;

/// Zanzibar relations we treat as "the subject can read objects of
/// this type".  The federated dispatcher needs to know the set of
/// zones the subject can query — every one of these grants that.
///
/// `member` covers group-style access, `viewer` covers read-only
/// tenancy, `admin` / `owner` are strict supersets.  A caller with
/// pure `w` on a zone (no `r`) is intentionally NOT in the set —
/// matches the write-only-zone-perms rule from the http-api zone
/// audit (see PR #4788).
pub const READ_GRANTING_RELATIONS: &[&str] = &["member", "owner", "admin", "viewer"];

/// A `(subject_type, subject_id)` identity, matching the Zanzibar
/// tuple convention.
pub type Subject<'a> = (&'a str, &'a str);

/// Return every zone id the subject holds a read-granting relation
/// on.  Deduped, sorted for determinism (a cross-machine dispatch
/// running the same query on two nodes must produce the same zone
/// list — the sort makes the order a wire contract instead of a
/// snapshot-order coincidence).
///
/// Uses the store's `list()` snapshot as the source of truth; a
/// caller that runs this on the request hot path should wrap it in
/// [`AccessibleZonesCache`] rather than paying the O(N) scan per
/// request.
pub fn list_accessible_zones<S: ReBACTupleStore + ?Sized>(
    store: &S,
    subject: Subject<'_>,
) -> Result<Vec<String>, ReBACTupleStoreError> {
    let (want_type, want_id) = subject;
    let mut zones = std::collections::BTreeSet::new();
    for (key, _) in store.list()? {
        let Some((_zone_ns, tuple)) = tuple_key::decode(&key) else {
            continue;
        };
        // Zone-level grant = object_type is literally "zone", the
        // subject matches, and the relation is one we read as
        // "grants access to the object" (see the const doc).
        if tuple.object_type == "zone"
            && tuple.subject_type == want_type
            && tuple.subject_id == want_id
            && READ_GRANTING_RELATIONS.contains(&tuple.relation.as_str())
        {
            zones.insert(tuple.object_id);
        }
    }
    Ok(zones.into_iter().collect())
}

// ── TTL cache ─────────────────────────────────────────────────────

/// Cache-freshness TTL for [`AccessibleZonesCache::lookup`].  Matches
/// the Python `_config.zone_cache_ttl_seconds` default so a Python-
/// to-Rust dispatcher swap does not shift the cache-miss window
/// observably.
pub const DEFAULT_ZONE_CACHE_TTL: Duration = Duration::from_secs(10);

/// Per-subject TTL cache of the zone set — the same shape the Python
/// federated dispatcher uses (`_zone_cache: dict[subject_key -> (zones,
/// expiry)]`).
///
/// The cache stores `(zones, expiry_instant)`; a lookup after expiry
/// re-runs the O(N) `list_accessible_zones` scan.  A tuple write
/// invalidates the cache the NEXT time the caller looks up — safe
/// because the dispatcher on the mutating side always sees the fresh
/// tuple table on its next request, and the TTL bounds staleness for
/// every other caller.
///
/// Cache invalidation is NOT wired to `store.put/delete` on purpose:
/// tightening to zero TTL would defeat the cache's whole point (the
/// dispatcher exists to defend p99 against the O(N) scan).  If a
/// caller needs a synchronous invalidate, add an explicit
/// `invalidate_subject` method — none does today.
#[derive(Debug, Default)]
pub struct AccessibleZonesCache {
    entries: RwLock<std::collections::HashMap<String, (Vec<String>, Instant)>>,
    ttl: Duration,
}

impl AccessibleZonesCache {
    /// Build a cache with the default TTL.
    pub fn new() -> Self {
        Self::with_ttl(DEFAULT_ZONE_CACHE_TTL)
    }

    /// Build a cache with an explicit TTL — tests use a zero TTL to
    /// force cache misses.
    pub fn with_ttl(ttl: Duration) -> Self {
        Self {
            entries: RwLock::new(std::collections::HashMap::new()),
            ttl,
        }
    }

    /// Look up `subject`'s zone set — cache hit returns the stored
    /// list; miss (or expired) re-runs `list_accessible_zones` and
    /// caches the fresh result.
    pub fn lookup<S: ReBACTupleStore + ?Sized>(
        &self,
        store: &S,
        subject: Subject<'_>,
    ) -> Result<Vec<String>, ReBACTupleStoreError> {
        let key = cache_key(subject);
        let now = Instant::now();
        // Fast path — read lock only.
        if let Some((zones, expiry)) = self.entries.read().get(&key) {
            if now < *expiry {
                return Ok(zones.clone());
            }
        }
        // Miss or expired — recompute; single scan, then upgrade to
        // write lock only for the final insert.  Racing lookups both
        // recompute; the last write wins (BTreeSet dedup means the
        // stored value is identical either way).
        let zones = list_accessible_zones(store, subject)?;
        let expiry = now + self.ttl;
        self.entries.write().insert(key, (zones.clone(), expiry));
        Ok(zones)
    }

    /// Drop every cache entry.  Test-only affordance — production
    /// callers rely on TTL expiry, not manual invalidation.
    pub fn clear(&self) {
        self.entries.write().clear();
    }
}

/// Wrap [`AccessibleZonesCache`] in an `Arc` for shared ownership
/// across the dispatcher's fanout tasks.
pub type SharedAccessibleZonesCache = Arc<AccessibleZonesCache>;

fn cache_key(subject: Subject<'_>) -> String {
    format!("{}:{}", subject.0, subject.1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inmem::InMemoryReBACTupleStore;
    use crate::store::ReBACTupleStore;
    use lib::types::ReBACTuple;

    fn seed(store: &InMemoryReBACTupleStore, zone: &str, tup: &ReBACTuple) {
        let key = tuple_key::encode(zone, tup).expect("encode");
        store.put(&key, b"").expect("put");
    }

    fn tuple(
        object_type: &str,
        object_id: &str,
        relation: &str,
        subject_type: &str,
        subject_id: &str,
    ) -> ReBACTuple {
        ReBACTuple {
            object_type: object_type.into(),
            object_id: object_id.into(),
            relation: relation.into(),
            subject_type: subject_type.into(),
            subject_id: subject_id.into(),
            subject_relation: None,
        }
    }

    #[test]
    fn lists_zones_the_subject_has_read_relations_on() {
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        seed(
            &s,
            "root",
            &tuple("zone", "legal", "viewer", "user", "alice"),
        );
        seed(&s, "root", &tuple("zone", "ops", "admin", "user", "alice"));
        // Noise: a doc-level relation, and a zone relation for a
        // different subject — both must be filtered out.
        seed(&s, "root", &tuple("doc", "/x", "editor", "user", "alice"));
        seed(
            &s,
            "root",
            &tuple("zone", "finance", "owner", "user", "bob"),
        );
        let out = list_accessible_zones(&s, ("user", "alice")).unwrap();
        assert_eq!(
            out,
            vec!["eng".to_string(), "legal".to_string(), "ops".to_string()]
        );
    }

    #[test]
    fn write_only_zone_relation_is_not_read_visible() {
        // Same rule the http-api zone audit landed (PR #4788):
        // pure-write grants do not surface a zone as read-accessible.
        //
        // We model that here by using an EXPLICIT non-read relation
        // (`editor`) rather than a "w" perm-string — nexus-rebac
        // stores zone grants under relation names, and only the
        // members of `READ_GRANTING_RELATIONS` count as read.
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "editor", "user", "alice"));
        assert!(list_accessible_zones(&s, ("user", "alice"))
            .unwrap()
            .is_empty());
    }

    #[test]
    fn dedup_across_multiple_read_relations_on_the_same_zone() {
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        seed(&s, "root", &tuple("zone", "eng", "viewer", "user", "alice"));
        seed(&s, "root", &tuple("zone", "eng", "admin", "user", "alice"));
        let out = list_accessible_zones(&s, ("user", "alice")).unwrap();
        assert_eq!(out, vec!["eng".to_string()]);
    }

    #[test]
    fn stable_sort_across_scan_order() {
        // BTreeSet dedup + Vec collection MUST produce a sorted
        // result — cross-machine dispatch running the same query on
        // two nodes must produce the same fanout list.
        let s = InMemoryReBACTupleStore::new();
        for zone in ["ops", "eng", "legal", "finance"] {
            seed(&s, "root", &tuple("zone", zone, "member", "user", "alice"));
        }
        let out = list_accessible_zones(&s, ("user", "alice")).unwrap();
        assert_eq!(
            out,
            vec![
                "eng".to_string(),
                "finance".to_string(),
                "legal".to_string(),
                "ops".to_string(),
            ]
        );
    }

    #[test]
    fn empty_store_returns_empty_list() {
        let s = InMemoryReBACTupleStore::new();
        assert!(list_accessible_zones(&s, ("user", "alice"))
            .unwrap()
            .is_empty());
    }

    // ── Cache ─────────────────────────────────────────────────────

    #[test]
    fn cache_hit_returns_stored_list_without_scanning() {
        // Seed once, prime the cache, then MUTATE the store — the
        // cache must still return the OLD list within the TTL.
        // Documents the "TTL bounds staleness; invalidation on the
        // mutating node is out of scope" contract.
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        let cache = AccessibleZonesCache::new();
        let first = cache.lookup(&s, ("user", "alice")).unwrap();
        assert_eq!(first, vec!["eng".to_string()]);
        seed(
            &s,
            "root",
            &tuple("zone", "legal", "member", "user", "alice"),
        );
        let cached = cache.lookup(&s, ("user", "alice")).unwrap();
        assert_eq!(
            cached,
            vec!["eng".to_string()],
            "cache must NOT see the fresh row"
        );
    }

    #[test]
    fn cache_miss_after_ttl_expiry_recomputes() {
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        let cache = AccessibleZonesCache::with_ttl(Duration::from_millis(1));
        let _ = cache.lookup(&s, ("user", "alice")).unwrap();
        seed(
            &s,
            "root",
            &tuple("zone", "legal", "member", "user", "alice"),
        );
        std::thread::sleep(Duration::from_millis(5));
        let out = cache.lookup(&s, ("user", "alice")).unwrap();
        assert_eq!(out, vec!["eng".to_string(), "legal".to_string()]);
    }

    #[test]
    fn cache_scopes_by_subject_key() {
        // alice + bob cache independently.
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        seed(&s, "root", &tuple("zone", "legal", "member", "user", "bob"));
        let cache = AccessibleZonesCache::new();
        assert_eq!(cache.lookup(&s, ("user", "alice")).unwrap(), vec!["eng"]);
        assert_eq!(cache.lookup(&s, ("user", "bob")).unwrap(), vec!["legal"]);
    }

    #[test]
    fn clear_forces_a_recompute_on_next_lookup() {
        let s = InMemoryReBACTupleStore::new();
        seed(&s, "root", &tuple("zone", "eng", "member", "user", "alice"));
        let cache = AccessibleZonesCache::new();
        let _ = cache.lookup(&s, ("user", "alice")).unwrap();
        seed(
            &s,
            "root",
            &tuple("zone", "legal", "member", "user", "alice"),
        );
        cache.clear();
        let out = cache.lookup(&s, ("user", "alice")).unwrap();
        assert_eq!(out, vec!["eng".to_string(), "legal".to_string()]);
    }
}
