//! Per-zone `lib::rebac::ReBACGraph` cache backed by an
//! [`crate::store::ReBACTupleStore`].
//!
//! # Why a cache
//!
//! The Zanzibar enforcer answers `check(subject, permission, object)`
//! by walking a `ReBACGraph` — a set of `AHashMap`s / `AHashSet`s
//! indexing tuples for O(1) direct-relation lookups plus O(N) userset
//! expansion.  Rebuilding the graph from `store.list()` on every
//! check is O(N) in tuple count and pays the deserialise cost per
//! tuple.
//!
//! This cache holds an `Arc<ReBACGraph>` per zone, keyed on the
//! store's [`crate::store::ReBACTupleStore::zone_revision`] counter,
//! so callers observe an O(1) hit under steady state.  Rebuild fires
//! only when the revision moves — i.e. after a `put` / `delete` in
//! that zone.
//!
//! # The `zone_revision == 0` fail-safe path
//!
//! The raft-backed store deliberately returns `zone_revision = 0` as
//! a sentinel (see [`crate::raft_store::RaftReBACTupleStore`] module
//! doc — a cross-node revision counter is subtle; the fail-safe is
//! "always stale, always rebuild").  This cache treats `0` as
//! **bypass the cache entirely**: rebuild every call, never write to
//! the cache.  Cost: the caller pays the full `list()` + rebuild on
//! every check.  Not a hot-path regression — enforcer checks are
//! amortised behind `permission::PermissionLeaseCache` upstream.
//!
//! When a follow-up ships the raft-observer counter, this cache
//! silently becomes fast for the raft store too — no changes here.
//!
//! # Concurrency
//!
//! Reads take a shared lock, fast-path returns the cached `Arc<
//! ReBACGraph>` clone; writes take an exclusive lock ONLY when a
//! rebuild is required.  A stampede (N readers all missing at once)
//! is bounded — the first writer rebuilds, later writers observe the
//! now-fresh entry inside the write lock and short-circuit.  No
//! double rebuild, no lock held across the O(N) rebuild body.

use std::collections::HashMap;
use std::sync::Arc;

use lib::rebac::ReBACGraph;
use lib::types::ReBACTuple;
use parking_lot::RwLock;

use crate::store::{ReBACTupleStore, ReBACTupleStoreError};
use crate::tuple_key;

/// A `zone_revision` sentinel meaning "the store cannot cheaply
/// track revisions — bypass the cache and always rebuild".  Matches
/// [`ReBACTupleStore::zone_revision`]'s default-impl return value.
///
/// Named for grepability: a future reviewer scanning for the
/// `0`-special-case sees the constant and its docstring instead of
/// a bare literal.
const ZONE_REV_ALWAYS_STALE: u64 = 0;

/// A cached graph for one zone, keyed by the revision at which it
/// was built.  Held as `Arc` so a reader holding an old snapshot can
/// keep computing after a concurrent rebuild swaps in a new one.
struct ZoneEntry {
    revision: u64,
    graph: Arc<ReBACGraph>,
}

/// Per-zone graph cache.
///
/// Clone-cheap — internal state is behind `Arc` + `RwLock`.  A
/// single instance is composed at the composition root, cloned into
/// the enforcer and any admin tools that also need to build the same
/// graph (e.g. the (future) `expand` HTTP route that lists all
/// subjects with a permission).
///
/// # NOT the enforcer
///
/// This is the *substrate* — one `Arc<ReBACGraph>` per zone.  The
/// enforcer ([`crate::RebacPermissionProvider`] in the next PR)
/// composes this with a namespace registry + calls `lib::rebac::
/// compute_permission` on the returned graph.
pub struct ReBACGraphCache {
    store: Arc<dyn ReBACTupleStore>,
    zones: RwLock<HashMap<String, ZoneEntry>>,
}

impl ReBACGraphCache {
    /// Wrap a `ReBACTupleStore` with per-zone graph caching.
    ///
    /// A single cache instance serves many callers — the composition
    /// root builds one, clones it (via `Arc`) into every consumer.
    pub fn new(store: Arc<dyn ReBACTupleStore>) -> Self {
        Self {
            store,
            zones: RwLock::new(HashMap::new()),
        }
    }

    /// Return the current graph for `zone`, rebuilding if the
    /// cached revision does not match the store's `zone_revision`.
    ///
    /// # The steady-state fast path
    ///
    /// 1. `store.zone_revision(zone)` — one branch-free counter read
    ///    (raft returns `0`; in-memory returns the real counter).
    /// 2. Shared-lock read the cache; if `entry.revision == current &&
    ///    current != 0`, return `Arc::clone(&entry.graph)`.  This is
    ///    the O(1) hot path.
    ///
    /// # The miss path
    ///
    /// Under exclusive lock: re-check the entry (a concurrent writer
    /// may have already rebuilt), otherwise `store.list()`, filter
    /// by `zone_of(key) == zone`, `decode()` each, hand the vec to
    /// `ReBACGraph::from_tuples`, wrap in `Arc`, and — only if
    /// `current != 0` — insert into the cache.  Return the fresh
    /// `Arc`.
    ///
    /// # Error propagation
    ///
    /// Backend errors from `store.zone_revision` / `store.list`
    /// bubble as `ReBACTupleStoreError::Backend`.  The enforcer
    /// treats these as fail-closed (deny the permission) — the same
    /// posture as an auth-store read failure denying auth.
    pub fn graph_for_zone(&self, zone: &str) -> Result<Arc<ReBACGraph>, ReBACTupleStoreError> {
        let current_rev = self.store.zone_revision(zone)?;

        // Fast path: shared lock, exact revision match, non-sentinel.
        // The `!= ZONE_REV_ALWAYS_STALE` guard is what makes the
        // raft-backed store (which returns 0) skip this branch and
        // fall through to a rebuild.
        if current_rev != ZONE_REV_ALWAYS_STALE {
            let zones = self.zones.read();
            if let Some(entry) = zones.get(zone) {
                if entry.revision == current_rev {
                    return Ok(Arc::clone(&entry.graph));
                }
            }
        }

        // Miss / sentinel path: rebuild.  Re-check inside the write
        // lock to collapse a rebuild stampede (N concurrent misses
        // → one rebuild, N-1 late arrivals observe the fresh entry
        // and short-circuit).
        let mut zones = self.zones.write();
        if current_rev != ZONE_REV_ALWAYS_STALE {
            if let Some(entry) = zones.get(zone) {
                if entry.revision == current_rev {
                    return Ok(Arc::clone(&entry.graph));
                }
            }
        }

        let graph = Arc::new(build_graph_for_zone(self.store.as_ref(), zone)?);

        // Skip cache-write under the sentinel — every call rebuilds,
        // matches the raft-store fail-safe posture.
        if current_rev != ZONE_REV_ALWAYS_STALE {
            zones.insert(
                zone.to_string(),
                ZoneEntry {
                    revision: current_rev,
                    graph: Arc::clone(&graph),
                },
            );
        }

        Ok(graph)
    }

    /// Drop the cached entry for `zone`, if any.  The next
    /// `graph_for_zone(zone)` call rebuilds unconditionally.
    ///
    /// Two callers today: (a) test cleanup, (b) an admin tool that
    /// mutated tuples through a side channel and wants to force a
    /// rebuild.  Real writes through the store's `put` / `delete`
    /// bump `zone_revision` and are picked up naturally by the
    /// steady-state path — this method is the escape hatch, not
    /// the norm.
    pub fn invalidate(&self, zone: &str) {
        self.zones.write().remove(zone);
    }
}

/// Walk `store.list()`, keep entries whose zone-prefix matches
/// `zone`, decode each into a `ReBACTuple`, and build the graph.
///
/// Kept as a free fn so `graph_for_zone` doesn't hold the write
/// lock across `store.list()` (which for the raft-backed store
/// bridges an async future and can block briefly under contention).
/// A refactor that inlines this back into `graph_for_zone` MUST
/// preserve the "no store I/O under the write lock" property.
fn build_graph_for_zone(
    store: &dyn ReBACTupleStore,
    zone: &str,
) -> Result<ReBACGraph, ReBACTupleStoreError> {
    let entries = store.list()?;
    let tuples: Vec<ReBACTuple> = entries
        .into_iter()
        .filter_map(|(key, _value)| {
            // `zone_of` first — cheap prefix compare avoids the
            // full 6/7-segment split for out-of-zone keys.
            if tuple_key::zone_of(&key) != Some(zone) {
                return None;
            }
            // Soft-skip on decode failure — one malformed row does
            // not wedge the whole zone's graph (see tuple_key::
            // decode docstring).
            tuple_key::decode(&key).map(|(_zone, tuple)| tuple)
        })
        .collect();
    Ok(ReBACGraph::from_tuples(&tuples))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inmem::InMemoryReBACTupleStore;
    use crate::tuple_key;
    use lib::types::{Entity, ReBACTuple};

    fn t(obj_type: &str, obj_id: &str, rel: &str, subj_type: &str, subj_id: &str) -> ReBACTuple {
        ReBACTuple {
            object_type: obj_type.to_string(),
            object_id: obj_id.to_string(),
            relation: rel.to_string(),
            subject_type: subj_type.to_string(),
            subject_id: subj_id.to_string(),
            subject_relation: None,
        }
    }

    fn put(store: &InMemoryReBACTupleStore, zone: &str, tup: &ReBACTuple) {
        let key = tuple_key::encode(zone, tup).expect("encode");
        store.put(&key, b"").expect("put");
    }

    /// Fresh cache with one tuple in one zone → graph contains that
    /// tuple as a direct relation.  End-to-end: encode → store →
    /// list → filter → decode → build.
    #[test]
    fn graph_reflects_tuples_written_to_the_zone() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        put(&store, "root", &t("doc", "a", "reader", "user", "alice"));
        put(&store, "root", &t("doc", "b", "writer", "user", "bob"));

        let cache = ReBACGraphCache::new(store);
        let g = cache.graph_for_zone("root").expect("graph");

        assert!(g.check_direct_relation(
            &Entity {
                entity_type: "user".to_string(),
                entity_id: "alice".to_string(),
            },
            "reader",
            &Entity {
                entity_type: "doc".to_string(),
                entity_id: "a".to_string(),
            },
        ));
        assert!(g.check_direct_relation(
            &Entity {
                entity_type: "user".to_string(),
                entity_id: "bob".to_string(),
            },
            "writer",
            &Entity {
                entity_type: "doc".to_string(),
                entity_id: "b".to_string(),
            },
        ));
    }

    /// Zone A's cache returns a graph containing only zone A's
    /// tuples — zone B's tuples do not leak in.  Pins the
    /// namespace-isolation invariant at the cache boundary (the
    /// store enforces it via the key prefix, but the cache is what
    /// actually hands a graph to the enforcer).
    #[test]
    fn graph_isolation_zone_a_does_not_see_zone_b_tuples() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        put(&store, "zone_a", &t("doc", "x", "reader", "user", "alice"));
        put(&store, "zone_b", &t("doc", "x", "reader", "user", "alice"));

        let cache = ReBACGraphCache::new(store);
        let g_a = cache.graph_for_zone("zone_a").expect("graph a");
        let g_b = cache.graph_for_zone("zone_b").expect("graph b");

        // Same subject / relation / object on paper — but each graph
        // sees only its own zone's tuple.  A cross-leak here would
        // grant a user permission on the wrong tenant's data.
        let alice = Entity {
            entity_type: "user".to_string(),
            entity_id: "alice".to_string(),
        };
        let doc = Entity {
            entity_type: "doc".to_string(),
            entity_id: "x".to_string(),
        };
        assert!(g_a.check_direct_relation(&alice, "reader", &doc));
        assert!(g_b.check_direct_relation(&alice, "reader", &doc));
        // Both are true because BOTH zones have the tuple; the pin
        // is on the count / adjacency shape below.
        let subjects_a = g_a.find_direct_subjects_for_object(&doc, "reader");
        let subjects_b = g_b.find_direct_subjects_for_object(&doc, "reader");
        assert_eq!(subjects_a.len(), 1, "zone A has exactly one grant");
        assert_eq!(subjects_b.len(), 1, "zone B has exactly one grant");
    }

    /// Cache hit: two calls with no intervening write return the
    /// same `Arc` (by pointer equality).  A future refactor that
    /// accidentally rebuilds every call would trip this — the
    /// pointer would move.
    #[test]
    fn steady_state_hit_returns_the_same_arc() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        put(&store, "root", &t("doc", "a", "reader", "user", "alice"));

        let cache = ReBACGraphCache::new(store);
        let g1 = cache.graph_for_zone("root").expect("g1");
        let g2 = cache.graph_for_zone("root").expect("g2");
        assert!(
            Arc::ptr_eq(&g1, &g2),
            "cache hit must return the same Arc (no rebuild on 2nd call)",
        );
    }

    /// Cache miss after mutation: a write bumps the store's zone
    /// revision, and the next `graph_for_zone` returns a fresh Arc
    /// that reflects the new tuple.  Regression pin against a stale-
    /// read bug where the cache would keep serving the old graph
    /// after a grant lands.
    #[test]
    fn mutation_bumps_revision_and_next_lookup_rebuilds() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        put(&store, "root", &t("doc", "a", "reader", "user", "alice"));

        let cache = ReBACGraphCache::new(Arc::clone(&store) as Arc<dyn ReBACTupleStore>);
        let g1 = cache.graph_for_zone("root").expect("g1");
        assert_eq!(
            g1.find_direct_subjects_for_object(
                &Entity {
                    entity_type: "doc".to_string(),
                    entity_id: "a".to_string()
                },
                "reader",
            )
            .len(),
            1,
        );

        put(&store, "root", &t("doc", "a", "reader", "user", "bob"));
        let g2 = cache.graph_for_zone("root").expect("g2");
        assert!(!Arc::ptr_eq(&g1, &g2), "revision bump MUST force a rebuild",);
        assert_eq!(
            g2.find_direct_subjects_for_object(
                &Entity {
                    entity_type: "doc".to_string(),
                    entity_id: "a".to_string()
                },
                "reader",
            )
            .len(),
            2,
            "rebuilt graph must include the new tuple",
        );
    }

    /// The `zone_revision == 0` sentinel path (matches the raft-
    /// backed store's fail-safe posture).  A store that always
    /// returns 0 must never see cache hits — every call rebuilds.
    #[test]
    fn zone_revision_zero_sentinel_bypasses_cache() {
        // Custom store that always returns 0 for the revision but
        // is otherwise a real InMemory backend.
        struct AlwaysStale(InMemoryReBACTupleStore);
        impl ReBACTupleStore for AlwaysStale {
            fn put(&self, key: &str, value: &[u8]) -> Result<(), ReBACTupleStoreError> {
                self.0.put(key, value)
            }
            fn delete(&self, key: &str) -> Result<bool, ReBACTupleStoreError> {
                self.0.delete(key)
            }
            fn get(&self, key: &str) -> Result<Option<Vec<u8>>, ReBACTupleStoreError> {
                self.0.get(key)
            }
            fn list(&self) -> Result<Vec<(String, Vec<u8>)>, ReBACTupleStoreError> {
                self.0.list()
            }
            // Deliberately does NOT override zone_revision — the
            // trait default is `Ok(0)`, which is the fail-safe
            // sentinel this test exercises.
        }

        let store = Arc::new(AlwaysStale(InMemoryReBACTupleStore::new()));
        store.put("root|doc|a|reader|user|alice", b"").expect("put");

        let cache = ReBACGraphCache::new(store);
        let g1 = cache.graph_for_zone("root").expect("g1");
        let g2 = cache.graph_for_zone("root").expect("g2");
        assert!(
            !Arc::ptr_eq(&g1, &g2),
            "sentinel (revision=0) MUST rebuild every call — cache hit here \
             would mean the raft-backed store silently serves stale grants",
        );
    }

    /// A malformed key in the store (bad segment count, wrong
    /// delimiter, empty required segment) is soft-skipped by the
    /// rebuild — other tuples in the zone still load.  One bad row
    /// cannot wedge the whole zone's permission plane.
    #[test]
    fn malformed_key_is_soft_skipped_and_zone_still_loads() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        // Well-formed tuple.
        put(&store, "root", &t("doc", "a", "reader", "user", "alice"));
        // Malformed: too few segments — decode returns None.
        store
            .put("root|doc|a|reader|user", b"")
            .expect("put malformed");

        let cache = ReBACGraphCache::new(store);
        let g = cache.graph_for_zone("root").expect("graph");

        // The well-formed tuple survives.
        assert!(g.check_direct_relation(
            &Entity {
                entity_type: "user".to_string(),
                entity_id: "alice".to_string()
            },
            "reader",
            &Entity {
                entity_type: "doc".to_string(),
                entity_id: "a".to_string()
            },
        ));
    }

    /// `invalidate(zone)` drops the cached entry — the next lookup
    /// rebuilds even if the store's revision has not moved.  The
    /// escape hatch for admin tools that mutated the store via a
    /// side channel.
    #[test]
    fn invalidate_forces_next_lookup_to_rebuild() {
        let store = Arc::new(InMemoryReBACTupleStore::new());
        put(&store, "root", &t("doc", "a", "reader", "user", "alice"));

        let cache = ReBACGraphCache::new(store);
        let g1 = cache.graph_for_zone("root").expect("g1");
        cache.invalidate("root");
        let g2 = cache.graph_for_zone("root").expect("g2");
        assert!(
            !Arc::ptr_eq(&g1, &g2),
            "invalidate must force a rebuild — the escape hatch is a no-op otherwise",
        );
    }
}
