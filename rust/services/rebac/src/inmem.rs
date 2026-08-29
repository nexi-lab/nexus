//! `InMemoryReBACTupleStore` — the real (non-noop) impl backed by a
//! `parking_lot::RwLock<HashMap>`.
//!
//! # When to use
//!
//! * **Tests** — the enforcer + graph cache + HTTP handler tests
//!   all build their fixture graph through this.  Same shape
//!   real callers use, no boot-order dance.
//! * **Dev / single-node** — a nexusd built without the raft
//!   feature (a future opt-out) can install this at the
//!   composition root.  Grants persist for the process lifetime
//!   and vanish on restart — CORRECT for dev; NEVER production.
//!
//! # Cross-thread safety
//!
//! `RwLock<HashMap>`: unbounded readers, at-most-one writer.
//! Every method takes the lock briefly and returns owned data —
//! no lock is held across the caller's compute.  The `list()`
//! call clones the whole map; this is O(N) where N is the tuple
//! count — the same "one full scan per graph rebuild" cost
//! model the real raft-backed impl carries.
//!
//! # Zone revision tracking
//!
//! An in-memory `HashMap<String, u64>` bumps on every `put` /
//! `delete` for the zone extracted from the tuple key's first
//! `|`-delimited segment (the tuple key convention this crate
//! adopts — documented on [`InMemoryReBACTupleStore`]).  A key
//! that does not encode a zone bumps a `""` global counter — safe
//! (over-invalidates the enforcer's cache) but the tuple-key
//! convention is stable enough this branch is never taken in
//! production.

use std::collections::HashMap;

use parking_lot::RwLock;

use crate::store::{ReBACTupleStore, ReBACTupleStoreError};

/// In-memory tuple store.  Wraps a `RwLock<HashMap>`; every method
/// takes the lock briefly and returns owned data (no borrow-across-
/// caller).
///
/// # Tuple key convention
///
/// Keys are expected to be pipe-delimited `<zone>|<rest>` — the
/// zone-revision tracker keys off the first segment.  Any key
/// without a `|` bumps the empty-string revision counter, which
/// the enforcer treats as a global cache bust (safe over-
/// invalidate, no correctness impact).  The full production key
/// shape lands in the enforcer PR: `<zone>|<object_type>|
/// <object_id>|<relation>|<subject_type>|<subject_id>[|<subject_
/// relation>]`.
#[derive(Debug, Default)]
pub struct InMemoryReBACTupleStore {
    entries: RwLock<HashMap<String, Vec<u8>>>,
    zone_revs: RwLock<HashMap<String, u64>>,
}

impl InMemoryReBACTupleStore {
    /// Fresh empty store.  Equivalent to `Default::default()` but
    /// spelled out for readable call-sites.
    pub fn new() -> Self {
        Self::default()
    }

    /// Extract the zone from a tuple key (first `|`-delimited
    /// segment).  Returns `""` for a key with no `|` — see the
    /// struct doc for the safe-over-invalidate rationale.
    fn zone_of(key: &str) -> &str {
        key.split_once('|').map(|(z, _)| z).unwrap_or("")
    }

    fn bump_zone_rev(&self, zone: &str) {
        let mut revs = self.zone_revs.write();
        let entry = revs.entry(zone.to_string()).or_insert(0);
        *entry = entry.saturating_add(1);
    }
}

impl ReBACTupleStore for InMemoryReBACTupleStore {
    fn put(&self, key: &str, value: &[u8]) -> Result<(), ReBACTupleStoreError> {
        // Bump the zone revision on EVERY put — even a same-value
        // write, matching the `RaftReBACTupleStore` posture (a
        // raft propose commits regardless of value equality).
        // Cheap: one lock + one hash insert.
        self.entries.write().insert(key.to_string(), value.to_vec());
        self.bump_zone_rev(Self::zone_of(key));
        Ok(())
    }

    fn delete(&self, key: &str) -> Result<bool, ReBACTupleStoreError> {
        let existed = self.entries.write().remove(key).is_some();
        // Bump the revision only when the delete actually removed
        // something — a delete-on-missing does not change graph
        // state, so the enforcer cache stays fresh.  Matches the
        // idempotent-delete contract.
        if existed {
            self.bump_zone_rev(Self::zone_of(key));
        }
        Ok(existed)
    }

    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, ReBACTupleStoreError> {
        Ok(self.entries.read().get(key).cloned())
    }

    fn list(&self) -> Result<Vec<(String, Vec<u8>)>, ReBACTupleStoreError> {
        Ok(self
            .entries
            .read()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect())
    }

    fn zone_revision(&self, zone: &str) -> Result<u64, ReBACTupleStoreError> {
        Ok(self.zone_revs.read().get(zone).copied().unwrap_or(0))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_then_get_returns_the_written_value() {
        let s = InMemoryReBACTupleStore::new();
        s.put("root|doc:a|reader|user|alice", b"grant")
            .expect("put");
        assert_eq!(
            s.get("root|doc:a|reader|user|alice").expect("get"),
            Some(b"grant".to_vec()),
        );
    }

    #[test]
    fn get_absent_key_returns_none() {
        let s = InMemoryReBACTupleStore::new();
        assert_eq!(s.get("root|missing|reader|user|alice").expect("get"), None);
    }

    #[test]
    fn delete_returns_true_when_key_existed() {
        let s = InMemoryReBACTupleStore::new();
        s.put("root|doc:a|reader|user|alice", b"grant")
            .expect("put");
        assert!(s.delete("root|doc:a|reader|user|alice").expect("delete"));
        assert!(!s
            .delete("root|doc:a|reader|user|alice")
            .expect("delete idempotent"));
    }

    #[test]
    fn list_returns_snapshot_of_all_entries() {
        let s = InMemoryReBACTupleStore::new();
        s.put("root|doc:a|reader|user|alice", b"1").expect("put a");
        s.put("shared|doc:b|writer|user|bob", b"2").expect("put b");
        let mut got = s.list().expect("list");
        got.sort();
        assert_eq!(
            got,
            vec![
                ("root|doc:a|reader|user|alice".to_string(), b"1".to_vec()),
                ("shared|doc:b|writer|user|bob".to_string(), b"2".to_vec()),
            ],
        );
    }

    #[test]
    fn put_bumps_zone_revision_for_the_written_zone() {
        // The enforcer keys its per-zone graph cache on this
        // counter — a bump on write is what makes a stale cache
        // notice it needs to rebuild.  Regression pin: a delete
        // must also bump so a revoke invalidates every reader.
        let s = InMemoryReBACTupleStore::new();
        assert_eq!(s.zone_revision("root").expect("rev0"), 0);

        s.put("root|doc:a|reader|user|alice", b"g").expect("put");
        let rev1 = s.zone_revision("root").expect("rev1");
        assert!(rev1 > 0);

        s.put("root|doc:b|reader|user|alice", b"g")
            .expect("put again");
        let rev2 = s.zone_revision("root").expect("rev2");
        assert!(rev2 > rev1);

        s.delete("root|doc:a|reader|user|alice").expect("del");
        let rev3 = s.zone_revision("root").expect("rev3");
        assert!(rev3 > rev2);
    }

    #[test]
    fn delete_on_missing_key_does_not_bump_zone_revision() {
        // Idempotent delete + no-op cache bust — a caller that
        // retries a delete does not spuriously invalidate every
        // reader's cache in the zone.
        let s = InMemoryReBACTupleStore::new();
        assert_eq!(s.zone_revision("root").expect("rev0"), 0);
        assert!(!s.delete("root|missing|reader|user|alice").expect("delete"));
        assert_eq!(
            s.zone_revision("root").expect("rev unchanged"),
            0,
            "delete on missing key must NOT bump the revision — a retry \
             storm would otherwise invalidate every reader's cache in the zone",
        );
    }

    #[test]
    fn zone_revisions_are_independent_across_zones() {
        // Writing to zone A must not bust zone B's cache.
        let s = InMemoryReBACTupleStore::new();
        s.put("A|doc:x|reader|user|alice", b"g").expect("put A");
        let rev_a_1 = s.zone_revision("A").expect("A rev1");
        let rev_b_1 = s.zone_revision("B").expect("B rev1");

        s.put("A|doc:y|reader|user|alice", b"g")
            .expect("put A again");
        assert!(s.zone_revision("A").expect("A rev2") > rev_a_1);
        assert_eq!(
            s.zone_revision("B").expect("B unchanged"),
            rev_b_1,
            "write to zone A must not bump zone B's revision",
        );
    }

    #[test]
    fn key_without_zone_prefix_bumps_empty_string_revision() {
        // Safe over-invalidate posture — a malformed key (no `|`)
        // is treated as a global cache bust rather than silently
        // dropped.  Real callers use the documented key shape;
        // this branch is a safety net.
        let s = InMemoryReBACTupleStore::new();
        s.put("legacy_no_zone_prefix", b"g").expect("put");
        assert_eq!(
            s.zone_revision("").expect("empty-zone rev"),
            1,
            "key without `|` must bump the empty-zone counter, not silently drop",
        );
    }

    #[test]
    fn concurrent_writes_from_multiple_threads_all_land() {
        // parking_lot RwLock serializes writers — every put lands.
        // Regression pin against a future refactor that
        // accidentally introduces a lock-free path with lost
        // updates.
        use std::sync::Arc;
        use std::thread;

        let s = Arc::new(InMemoryReBACTupleStore::new());
        let handles: Vec<_> = (0..8)
            .map(|w| {
                let s = Arc::clone(&s);
                thread::spawn(move || {
                    for i in 0..100 {
                        let key = format!("root|doc:{w}-{i}|reader|user|alice");
                        s.put(&key, b"g").expect("put");
                    }
                })
            })
            .collect();
        for h in handles {
            h.join().expect("thread joined");
        }
        assert_eq!(
            s.list().expect("list").len(),
            8 * 100,
            "all 800 concurrent writes must land — no lost updates",
        );
    }
}
