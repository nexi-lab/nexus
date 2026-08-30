//! `ReBACTupleStore` impl backed by a Raft `ZoneConsensus` — the
//! production tuple backend for the cluster deployment.
//!
//! # Same shape as `RaftAuthKeyStore`
//!
//! A thin typed wrapper over [`raft::control_state_store::ControlStateStore`]
//! (namespace [`contracts::CONTROL_NS_AUTH`] there; namespace `"rebac"`
//! here — the one below), mapping the shared layer's `String` error
//! into the HAL type.  The propose + read-your-writes + local read
//! boilerplate lives in `ControlStateStore` once and is reused across
//! `RaftAuthKeyStore`, the foreign-CA anchor registry, and now this.
//!
//! # Why the control zone, not per-node `root`
//!
//! Tuples are cluster-wide identity: a grant of `reader` on `zone1|
//! doc:x` written on any node must resolve on every node.  The
//! control zone is replicated to every member (per the `ZoneManager`
//! contract), so a wrapper backed by the control zone's consensus
//! shares one tuple namespace across the whole cluster.  Backing this
//! with per-node `root` would silently give each node its own tuple
//! space — a grant on node A would not resolve on node B, and a
//! federated grant tool would appear intermittently broken.
//!
//! # zone_revision is `Ok(0)` for now — deliberate
//!
//! [`ReBACTupleStore::zone_revision`] is the per-zone cache-freshness
//! key the (PR-3) graph cache uses to skip a full-scan rebuild.  For
//! the raft-backed impl a cross-node revision counter is subtle: two
//! concurrent writers proposing at different indices need to serialise
//! their revision bumps through the log (not `get`+`put` — TOCTOU
//! would drop one bump under contention).
//!
//! Rather than ship a subtle counter here, this impl returns `Ok(0)`
//! and the (PR-3) graph cache treats `0` as **always-stale** — a
//! guaranteed correct fail-safe.  Cost: every check on a raft-backed
//! deployment pays the graph rebuild (an `AHashMap`-populate over
//! `list()`; ~O(ms) for O(100k) tuples, per the Python design point).
//! Not a hot-path regression — permission checks are behind
//! `PermissionLeaseCache` upstream, so the rebuild cost is amortised
//! across a lease window rather than per-syscall.
//!
//! The follow-up is a raft-observer hook that bumps a local
//! `AtomicU64` on every applied `PutControlState` with our namespace —
//! cross-node correct because every node sees every apply.  Not in
//! this PR to keep the surface small; the fail-safe posture means
//! landing it later is a pure perf win, not a correctness fix.

use std::sync::Arc;

use nexus_raft::control_state_store::ControlStateStore;
use nexus_raft::prelude::{FullStateMachine, ZoneConsensus};

use crate::store::{ReBACTupleStore, ReBACTupleStoreError};

/// The `PutControlState` namespace this store reads and writes under.
///
/// Not upstream in `contracts::CONTROL_NS_*` yet — added there in a
/// follow-up so all control-plane namespaces stay centralised.
/// Local `pub const` (not `&'static str` literal) keeps the SSOT
/// discoverable at `nexus_rebac::raft_store::CONTROL_NS_REBAC` so
/// admin tooling that spelunks the keyspace has one name to grep for.
pub const CONTROL_NS_REBAC: &str = "rebac";

/// Raft-backed `ReBACTupleStore` — the `CONTROL_NS_REBAC` view of the
/// control store.
///
/// Construct against the **control zone's** `ZoneConsensus` (via the
/// composition root at boot) so every node shares one tuple namespace.
/// Per-node `root` would silently give each node its own tuple space.
pub struct RaftReBACTupleStore {
    inner: ControlStateStore,
}

impl RaftReBACTupleStore {
    /// Construct from the control zone's running `ZoneConsensus` + its
    /// runtime.  Same signature shape as upstream `RaftAuthKeyStore::
    /// new` so the composition root can hand both wrappers the same
    /// `(node, runtime)` pair.
    pub fn new(node: ZoneConsensus<FullStateMachine>, runtime: tokio::runtime::Handle) -> Self {
        Self {
            inner: ControlStateStore::new(node, runtime, CONTROL_NS_REBAC),
        }
    }

    /// Return an `Arc<dyn ReBACTupleStore>` ready to inject into the
    /// enforcer at the composition root.  Convenience — every real
    /// caller already holds the trait object.
    pub fn new_arc(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) -> Arc<dyn ReBACTupleStore> {
        Arc::new(Self::new(node, runtime))
    }
}

// Pure delegation, `#[inline]` so the wrapper is zero-cost; the only
// work here is mapping the shared layer's `String` error into
// `ReBACTupleStoreError::Backend(String)`.
impl ReBACTupleStore for RaftReBACTupleStore {
    #[inline]
    fn put(&self, key: &str, value: &[u8]) -> Result<(), ReBACTupleStoreError> {
        self.inner
            .put(key, value)
            .map_err(ReBACTupleStoreError::Backend)
    }

    #[inline]
    fn delete(&self, key: &str) -> Result<bool, ReBACTupleStoreError> {
        self.inner
            .delete(key)
            .map_err(ReBACTupleStoreError::Backend)
    }

    #[inline]
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, ReBACTupleStoreError> {
        self.inner.get(key).map_err(ReBACTupleStoreError::Backend)
    }

    #[inline]
    fn list(&self) -> Result<Vec<(String, Vec<u8>)>, ReBACTupleStoreError> {
        self.inner.list().map_err(ReBACTupleStoreError::Backend)
    }

    // zone_revision defaults to Ok(0) via the trait's default impl —
    // the graph cache in PR-3 treats 0 as "always stale, always
    // rebuild".  See the module docstring for the rationale (subtle
    // cross-node counter deferred; fail-safe posture in the meantime).
}

#[cfg(test)]
mod tests {
    use super::*;
    use nexus_raft::raft::ZoneRaftRegistry;
    use tempfile::TempDir;

    /// Full lifecycle against a live 1-voter zone, exercised from
    /// inside a multi-thread tokio runtime — the shape every real
    /// caller has (the http-api handler thread is a runtime worker),
    /// which is what the `block_in_place` bridge in `ControlStateStore`
    /// exists to survive.
    ///
    /// Covers put → get → upsert → list → delete → re-delete, and
    /// asserts the deleted key stops resolving.  An enforcer that
    /// fails closed on `Ok(None)` therefore denies a revoked grant.
    ///
    /// Mirrors `raft::auth_key_store::tests::record_roundtrips_through_
    /// consensus` — same skeleton, one namespace over.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn tuple_roundtrips_through_consensus() {
        let tmp = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp.path().to_path_buf(), 1);
        let runtime = tokio::runtime::Handle::current();
        let node = registry
            .create_zone("root", vec![], &runtime)
            .expect("create test zone");
        node.campaign().await.expect("campaign test zone");
        let store = RaftReBACTupleStore::new(node, runtime);

        // Absent key resolves as `Ok(None)`, not `Err` — the enforcer
        // must tell "no such tuple" apart from "cannot read the store".
        assert_eq!(
            store
                .get("root|doc:a|reader|user|alice")
                .expect("get absent"),
            None,
        );

        // Grant + resolve.
        store
            .put("root|doc:a|reader|user|alice", b"")
            .expect("put grant");
        assert_eq!(
            store
                .get("root|doc:a|reader|user|alice")
                .expect("get grant")
                .as_deref(),
            Some(&b""[..]),
        );

        // Upsert (grant metadata carries a v2 payload — same tuple,
        // richer value).  Store never interprets the bytes.
        store
            .put("root|doc:a|reader|user|alice", b"granter=root,ts=2026")
            .expect("upsert grant");
        assert_eq!(
            store
                .get("root|doc:a|reader|user|alice")
                .expect("get upsert")
                .as_deref(),
            Some(&b"granter=root,ts=2026"[..]),
        );

        // A second grant, different zone.
        store
            .put("shared|doc:b|writer|user|bob", b"")
            .expect("put bob grant");
        let mut listed = store.list().expect("list");
        listed.sort_by(|l, r| l.0.cmp(&r.0));
        assert_eq!(
            listed,
            vec![
                (
                    "root|doc:a|reader|user|alice".to_string(),
                    b"granter=root,ts=2026".to_vec()
                ),
                ("shared|doc:b|writer|user|bob".to_string(), b"".to_vec()),
            ],
        );

        // Revoke: reports it removed something.  A second revoke is
        // idempotent (reports nothing was there) and does not error.
        assert!(store
            .delete("root|doc:a|reader|user|alice")
            .expect("revoke grant"));
        assert_eq!(
            store
                .get("root|doc:a|reader|user|alice")
                .expect("get after revoke"),
            None,
        );
        assert!(!store
            .delete("root|doc:a|reader|user|alice")
            .expect("re-revoke grant"));
        assert_eq!(store.list().expect("list after revoke").len(), 1);

        registry.shutdown_all();
    }

    /// Tuples are a kernel-internal control-plane record, not files:
    /// they live under `TREE_CONTROL_STATE` in the state machine's
    /// `"rebac"` namespace, so nothing that walks the file-metadata
    /// tree can reach them.  Pinned at the *store* boundary here — a
    /// `ZoneMetaStore` over the same consensus cannot see a tuple
    /// under any plausible path spelling, and `list("/")` stays
    /// empty of ReBAC records.
    ///
    /// Mirrors the auth-key store's leak-containment test — same
    /// invariant, one namespace over.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn tuples_are_unreachable_through_the_file_metadata_store() {
        use kernel::meta_store::MetaStore;

        let tmp = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp.path().to_path_buf(), 1);
        let runtime = tokio::runtime::Handle::current();
        let node = registry
            .create_zone("root", vec![], &runtime)
            .expect("create test zone");
        node.campaign().await.expect("campaign test zone");

        let rebac = RaftReBACTupleStore::new(node.clone(), runtime.clone());
        rebac
            .put("root|doc:a|reader|user|alice", b"grant")
            .expect("put grant");

        let files = nexus_raft::zone_meta_store::ZoneMetaStore::new(node, runtime, "/".to_string());
        for spelling in [
            "root|doc:a|reader|user|alice",
            "/root|doc:a|reader|user|alice",
            "/__sys__/rebac/tuples/root|doc:a|reader|user|alice",
            "/sm_control_state/root|doc:a|reader|user|alice",
        ] {
            assert!(
                files.get(spelling).expect("metastore get").is_none(),
                "rebac tuple leaked into the file-metadata tree at {spelling}"
            );
        }
        assert!(
            files.list("/").expect("metastore list").is_empty(),
            "rebac tuple leaked into a file-metadata listing"
        );

        registry.shutdown_all();
    }

    /// Two typed views over the same consensus reach two disjoint
    /// key spaces — a tuple put through the ReBAC view is invisible
    /// to a key put through the auth view at the same string key,
    /// and vice versa.  Pins the namespace isolation invariant of
    /// `ControlStateStore` for the ReBAC/auth pair: a rebac grant
    /// cannot be exfiltrated by asking the auth store for it.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn rebac_and_auth_namespaces_do_not_alias() {
        use kernel::hal::auth_key_store::AuthKeyStore;
        use nexus_raft::auth_key_store::RaftAuthKeyStore;

        let tmp = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp.path().to_path_buf(), 1);
        let runtime = tokio::runtime::Handle::current();
        let node = registry
            .create_zone("root", vec![], &runtime)
            .expect("create test zone");
        node.campaign().await.expect("campaign test zone");

        let rebac = RaftReBACTupleStore::new(node.clone(), runtime.clone());
        let auth = RaftAuthKeyStore::new(node, runtime);

        // Same string key, disjoint namespaces.  A collision at the
        // string level must not conflate a tuple with an auth record.
        rebac.put("collision", b"rebac-value").expect("rebac put");
        auth.put("collision", b"auth-value").expect("auth put");

        assert_eq!(
            rebac.get("collision").expect("rebac get").as_deref(),
            Some(&b"rebac-value"[..]),
            "rebac view returns rebac's value",
        );
        assert_eq!(
            auth.get("collision").expect("auth get").as_deref(),
            Some(&b"auth-value"[..]),
            "auth view returns auth's value — namespaces isolated",
        );

        // Deletion in one namespace does not affect the other.
        assert!(rebac.delete("collision").expect("rebac delete"));
        assert_eq!(rebac.get("collision").expect("rebac after delete"), None);
        assert_eq!(
            auth.get("collision")
                .expect("auth after rebac delete")
                .as_deref(),
            Some(&b"auth-value"[..]),
            "deleting from rebac namespace must not touch auth namespace",
        );

        registry.shutdown_all();
    }

    /// Sanity: `zone_revision` returns `Ok(0)` deliberately (see the
    /// module docstring — always-stale posture is a fail-safe until
    /// the raft-observer counter lands).  Pin here so a future
    /// refactor that starts returning the wrong shape (a nonzero
    /// value that is NOT actually a real revision) trips this test
    /// instead of silently miscaching the graph.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn zone_revision_returns_zero_as_always_stale_sentinel() {
        let tmp = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp.path().to_path_buf(), 1);
        let runtime = tokio::runtime::Handle::current();
        let node = registry
            .create_zone("root", vec![], &runtime)
            .expect("create test zone");
        node.campaign().await.expect("campaign test zone");
        let store = RaftReBACTupleStore::new(node, runtime);

        assert_eq!(store.zone_revision("root").expect("zone_revision"), 0);

        // A put does not bump the revision (the sentinel is
        // deliberate; the graph cache treats 0 as always-stale).
        store.put("root|doc:a|reader|user|alice", b"").expect("put");
        assert_eq!(
            store.zone_revision("root").expect("zone_revision post-put"),
            0,
            "zone_revision must stay 0 — the graph cache relies on this \
             sentinel to always-rebuild until the raft-observer counter lands",
        );

        registry.shutdown_all();
    }
}
