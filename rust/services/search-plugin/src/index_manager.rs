//! Per-zone [`FtsIndex`] cache with lazy open-or-create (Phase 1 of
//! the Python-parity roadmap; see `PARITY_ROADMAP.md` D1).
//!
//! # Rationale
//!
//! `FtsIndex` is the storage primitive — one directory, one schema,
//! one writer lock.  `IndexManager` is the zone router — it maps
//! `zone_id -> Arc<FtsIndex>` and lazily opens an index the first
//! time a Query or Index call names a zone the manager hasn't seen
//! yet.  Separating the two keeps `service.rs` free of directory
//! plumbing: the RPC handler asks the manager for an index and
//! calls into it.
//!
//! # Concurrency
//!
//! The cache is a `parking_lot::Mutex<HashMap<zone_id, Arc<FtsIndex>>>`;
//! the lock is held for a hash lookup (typically ns) or a first-boot
//! `open_or_create` (typically low-ms once per zone).  A `DashMap`
//! would be lower-overhead in principle but zone count is small
//! (single-digit typical, tens max) and Query lookups take zero
//! locks after the first-boot cost — the `Arc<FtsIndex>` is cloned
//! out under the lock and the rest of the request runs lock-free.
//!
//! # Storage roots
//!
//! The directory layout is `<root>/<zone_id>/fts-v2/`.  Root resolves
//! from the `NEXUS_DATA_DIR` env var (same convention as
//! [`nexus-vault`]'s per-node state), falling back to `./nexus-data`
//! when unset — one SSOT lets operators relocate ALL plugins by
//! pointing `NEXUS_DATA_DIR` at a data volume.  Tests pass an
//! explicit tempdir via [`IndexManager::with_root`].
//!
//! Zone-id sanitisation is minimal — the caller (the kernel-tier
//! RPC handler) never lets a client-controlled string reach this
//! API; `zone_id` values are drawn from the raft-managed zone
//! registry.
//!
//! [`nexus-vault`]: ../../../vault/src/lib.rs

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::Mutex;

use crate::ann_index::{AnnError, AnnIndex};
use crate::fts_index::{FtsIndex, IndexError};

/// Directory name inside a per-zone index root for the FTS store.
/// Sibling directories (Phase 2's `ann/` for HNSW, etc.) land next
/// to this one under the same zone root.
///
/// The `v2` bump marks the #4618 schema change (`chunk_text` moved
/// from the default tokenizer to `en_stem`) — same convention as
/// `ann-<tag>-v2`: pre-stemmer `fts/` dirs stay on disk untouched
/// while callers converge on v2.  Reindex to populate (the
/// `index_state` version bump forces exactly that on the next
/// Refresh).
const FTS_SUBDIR: &str = "fts-v2";

/// Environment variable that names the per-node state root.  Same
/// SSOT the sibling `nexus-vault` plugin honours — one variable
/// relocates all plugin state to a data volume.
const DATA_DIR_ENV: &str = "NEXUS_DATA_DIR";

/// Resolve the default per-node storage root — `$NEXUS_DATA_DIR` if
/// set, `./nexus-data` otherwise.  Matches vault's exact fallback so
/// a fresh dev checkout puts vault + search state under the same
/// parent (`./nexus-data/{vault,plugins/search}/`) without any
/// operator setup.
pub fn default_root() -> PathBuf {
    let data_dir = std::env::var(DATA_DIR_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./nexus-data"));
    data_dir.join("plugins/search")
}

/// Zone-id → `Arc<FtsIndex>` + `Arc<AnnIndex>` caches; lazily opens
/// per-zone indices.  ANN indices are keyed by `(zone_id,
/// embedder_tag)` so a model swap (D4) opens a new directory
/// alongside the old one — the manager holds both in memory as
/// long as either is referenced.
pub struct IndexManager {
    root: PathBuf,
    zones: Mutex<HashMap<String, Arc<FtsIndex>>>,
    ann_zones: Mutex<HashMap<AnnKey, Arc<AnnIndex>>>,
    /// Per-zone WRITE locks (review R1).  Index / IndexDocuments /
    /// Refresh / NotifyFileChange each open an independent
    /// `IndexState` and save the whole snapshot at the end, so two
    /// concurrent mutations of the same zone could overwrite each
    /// other's entries (lost update) even with atomic renames.
    /// Holding this lock for the duration of a zone's index
    /// transaction serializes writers; queries never take it.
    zone_write_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    /// Per-zone skeleton snapshots for the title arm (#4628), keyed
    /// implicitly by each snapshot's FTS generation — see
    /// [`Self::get_or_build_skeleton`].
    skeletons: Mutex<HashMap<String, Arc<crate::title_index::ZoneSkeleton>>>,
    /// Per-zone single-flight guards for skeleton builds (review
    /// R1 of #4628's title arm).  A build is a full stored-doc scan;
    /// without this, N concurrent cache misses after a commit would
    /// run N simultaneous corpus scans and retain N snapshot copies.
    /// Exactly one caller builds; the rest serve the prior snapshot
    /// (or run with an empty arm on a cold start).
    skeleton_build_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    /// Zones with an index write IN FLIGHT or a FAILED write behind
    /// them (#4628 review R5).  ANN mutations become search-visible
    /// before the FTS commit bumps the generation, so the epoch
    /// alone can't see a half-applied write: writers mark the zone
    /// dirty before touching any sink and clear it only after every
    /// sink committed.  The query path treats a dirty zone as
    /// "no observable epoch" — cache reads and inserts are skipped,
    /// so mixed-sink state is never pinned for the TTL.  A failed
    /// write leaves the flag set (fail-closed) until a later
    /// successful write clears it.
    dirty_zones: Mutex<std::collections::HashSet<String>>,
}

/// Cache key for the ANN index — one entry per `(zone, embedder tag)`.
/// Different tags = different vector spaces, so a model swap gets its
/// own cached handle without evicting the old one.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct AnnKey {
    zone_id: String,
    embedder_tag: String,
}

/// Outcome of a skeleton fetch (#4628 review R2).  Freshness is part
/// of the contract so callers can decide whether a result computed
/// from this snapshot is CACHEABLE: a ranking built from a stale or
/// absent skeleton must not be stored under the same cache identity
/// as a fresh one, or it outlives the rebuild for the cache TTL.
pub enum SkeletonAccess {
    /// Snapshot matches the index's CURRENT searcher generation.
    Fresh(Arc<crate::title_index::ZoneSkeleton>),
    /// A rebuild is in flight; this is the previous generation's
    /// snapshot (slightly stale titles beat a thundering herd).
    Stale(Arc<crate::title_index::ZoneSkeleton>),
    /// A rebuild is in flight and no prior snapshot exists (cold
    /// start) — callers run with an empty title arm.
    Building,
}

impl IndexManager {
    /// Manager rooted at the platform default (`$NEXUS_DATA_DIR/plugins/search/`).
    pub fn new() -> Self {
        Self::with_root(default_root())
    }

    /// Manager rooted at an explicit path.  Used by tests + any
    /// deployment that overrides the storage location (e.g. an
    /// operator pointing the plugin at a data volume).
    pub fn with_root(root: PathBuf) -> Self {
        Self {
            root,
            zones: Mutex::new(HashMap::new()),
            ann_zones: Mutex::new(HashMap::new()),
            zone_write_locks: Mutex::new(HashMap::new()),
            skeletons: Mutex::new(HashMap::new()),
            skeleton_build_locks: Mutex::new(HashMap::new()),
            dirty_zones: Mutex::new(std::collections::HashSet::new()),
        }
    }

    /// On-disk twin of the in-memory dirty flag (#4628 review R6):
    /// a crash between sink commits would erase a memory-only flag
    /// while leaving the sinks mixed, so the marker also lives as a
    /// sentinel file under the zone root and survives restarts.
    fn zone_dirty_sentinel(&self, zone_id: &str) -> PathBuf {
        self.zone_root(zone_id).join(".write-dirty")
    }

    /// Mark a zone's index write as in flight — call BEFORE the
    /// first sink mutation.  Persists a sentinel file so the mark
    /// survives a crash mid-write (review R6); sentinel I/O failure
    /// degrades to the in-memory flag with a warning (the non-crash
    /// path stays fully protected).
    pub fn mark_zone_dirty(&self, zone_id: &str) {
        self.dirty_zones.lock().insert(zone_id.to_string());
        let sentinel = self.zone_dirty_sentinel(zone_id);
        if let Err(e) = std::fs::create_dir_all(self.zone_root(zone_id))
            .and_then(|_| std::fs::write(&sentinel, b""))
        {
            tracing::warn!(err = %e, zone = %zone_id, "dirty sentinel write failed — restart protection degraded");
        }
    }

    /// Clear the write-in-flight mark — call only after EVERY sink
    /// (FTS + ANN + state) committed successfully.  A failed write
    /// path must NOT call this: the flag then keeps the query cache
    /// bypassed until a subsequent successful write repairs the
    /// zone.
    pub fn clear_zone_dirty(&self, zone_id: &str) {
        self.dirty_zones.lock().remove(zone_id);
        if let Err(e) = std::fs::remove_file(self.zone_dirty_sentinel(zone_id)) {
            if e.kind() != std::io::ErrorKind::NotFound {
                tracing::warn!(err = %e, zone = %zone_id, "dirty sentinel remove failed — zone stays cache-bypassed");
            }
        }
    }

    /// Is a write in flight (or a failed/interrupted write
    /// unrepaired) for this zone?  Query callers skip cache reads
    /// AND inserts while true.  Consults the persistent sentinel
    /// too, so a crash mid-write keeps the cache bypassed after
    /// restart until a successful write clears it (review R6).
    pub fn zone_is_dirty(&self, zone_id: &str) -> bool {
        self.dirty_zones.lock().contains(zone_id) || self.zone_dirty_sentinel(zone_id).exists()
    }

    /// Handle to the given zone's write lock.  Callers hold the
    /// returned mutex for the WHOLE index transaction (open state →
    /// mutate FTS/ANN → commit → save state) so concurrent writers
    /// can't interleave and lose each other's state entries.  Held on
    /// blocking-pool threads only — never across an await.
    pub fn zone_write_lock(&self, zone_id: &str) -> Arc<Mutex<()>> {
        Arc::clone(
            self.zone_write_locks
                .lock()
                .entry(zone_id.to_string())
                .or_default(),
        )
    }

    /// Return the on-disk directory the given zone's FTS index lives
    /// in.  Broken out so tests can assert layout without going
    /// through [`get_or_open`].
    pub fn index_dir(&self, zone_id: &str) -> PathBuf {
        self.root.join(zone_id).join(FTS_SUBDIR)
    }

    /// Return the on-disk directory the given zone's ANN index lives
    /// in for the given embedder tag.  Same shape as [`index_dir`]
    /// but under `ann-<tag>-v2/`.  The `v2` bump at P4 marks the
    /// sidecar schema change from path-keyed to (path, chunk_index)-
    /// keyed — v1 dirs from earlier phases stay on disk (untouched)
    /// while callers converge on v2.  Reindex to populate v2.
    pub fn ann_dir(&self, zone_id: &str, embedder_tag: &str) -> PathBuf {
        self.root
            .join(zone_id)
            .join(format!("ann-{embedder_tag}-v2"))
    }

    /// Return the per-zone root — parent of `fts/` and `ann-*-v2/`.
    /// Used by `IndexState::open_or_create` to place
    /// `index_state.json` at the zone level so the P5 mtime cache
    /// sits alongside the sub-index directories rather than inside
    /// one of them.
    pub fn zone_root(&self, zone_id: &str) -> PathBuf {
        self.root.join(zone_id)
    }

    /// Handle to the storage root — used by callers that need to
    /// resolve sibling per-zone directories (e.g. Phase 2's HNSW
    /// index next to the tantivy one).
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Fetch (or lazily open) the FTS index for `zone_id`.  Once
    /// opened, the `Arc<FtsIndex>` is cached; further calls return
    /// the same handle so writer state (buffered adds) survives
    /// across RPCs.
    pub fn get_or_open(&self, zone_id: &str) -> Result<Arc<FtsIndex>, IndexError> {
        let mut zones = self.zones.lock();
        if let Some(idx) = zones.get(zone_id) {
            return Ok(Arc::clone(idx));
        }
        let idx = FtsIndex::open_or_create(self.index_dir(zone_id))?;
        zones.insert(zone_id.to_string(), Arc::clone(&idx));
        Ok(idx)
    }

    /// Get the zone's skeleton snapshot, rebuilding when the FTS
    /// index has committed since the cached build.  Generation-keyed
    /// (not call-site invalidated): every Index / Refresh /
    /// IndexDocuments / NotifyFileChange mutation ends in an FTS
    /// commit, which bumps the searcher generation — so staleness
    /// detection is structural and new mutation paths can't forget
    /// to invalidate.
    ///
    /// Builds are SINGLE-FLIGHT per zone: a build is a full
    /// stored-doc scan, so exactly one caller runs it while
    /// concurrent callers immediately get [`SkeletonAccess::Stale`]
    /// (prior generation's snapshot) or
    /// [`SkeletonAccess::Building`] (cold start — empty title arm
    /// for that query, fail-soft).  Freshness rides the return so
    /// callers can keep degraded results out of response caches.
    /// The winning builder holds only the per-zone build lock during
    /// the scan, never the snapshot-cache lock.
    pub fn get_or_build_skeleton(&self, zone_id: &str) -> Result<SkeletonAccess, IndexError> {
        let fts = self.get_or_open(zone_id)?;
        let current_gen = fts.generation_id();
        let stale = self.skeletons.lock().get(zone_id).map(Arc::clone);
        if let Some(sk) = &stale {
            if sk.generation_id() == current_gen {
                return Ok(SkeletonAccess::Fresh(Arc::clone(sk)));
            }
        }
        let build_lock = Arc::clone(
            self.skeleton_build_locks
                .lock()
                .entry(zone_id.to_string())
                .or_default(),
        );
        let Some(_build_guard) = build_lock.try_lock() else {
            // Another thread is scanning this zone right now — serve
            // the previous snapshot (or nothing on a cold start)
            // rather than piling on a duplicate corpus scan.
            return Ok(match stale {
                Some(sk) => SkeletonAccess::Stale(sk),
                None => SkeletonAccess::Building,
            });
        };
        // Double-check under build exclusivity: the previous builder
        // may have published the current generation between our cache
        // read and lock acquisition.
        {
            let cache = self.skeletons.lock();
            if let Some(sk) = cache.get(zone_id) {
                if sk.generation_id() == current_gen {
                    return Ok(SkeletonAccess::Fresh(Arc::clone(sk)));
                }
            }
        }
        let built = Arc::new(crate::title_index::ZoneSkeleton::build(&fts)?);
        self.skeletons
            .lock()
            .insert(zone_id.to_string(), Arc::clone(&built));
        Ok(SkeletonAccess::Fresh(built))
    }

    /// Test-only: hold the zone's skeleton build lock so tests can
    /// pin the "builder busy" path deterministically.
    #[cfg(test)]
    pub fn skeleton_build_lock_for_test(&self, zone_id: &str) -> Arc<Mutex<()>> {
        Arc::clone(
            self.skeleton_build_locks
                .lock()
                .entry(zone_id.to_string())
                .or_default(),
        )
    }

    /// Fetch (or lazily open) the ANN index for `zone_id` under the
    /// given embedder tag + dimensionality.  Cached per
    /// `(zone, tag)` so multiple SemanticQuery calls reuse the same
    /// on-disk graph.  `dim` must match the embedder that produced
    /// the vectors in that graph — a mismatch on a fresh index seeds
    /// the wrong dimensionality; on a reload, the underlying
    /// [`AnnIndex::open_or_create`] trusts the caller (no runtime
    /// check because hnsw_rs doesn't expose one).
    pub fn get_or_open_ann(
        &self,
        zone_id: &str,
        embedder_tag: &str,
        dim: usize,
    ) -> Result<Arc<AnnIndex>, AnnError> {
        let key = AnnKey {
            zone_id: zone_id.to_string(),
            embedder_tag: embedder_tag.to_string(),
        };
        let mut zones = self.ann_zones.lock();
        if let Some(idx) = zones.get(&key) {
            return Ok(Arc::clone(idx));
        }
        let idx = AnnIndex::open_or_create(self.ann_dir(zone_id, embedder_tag), dim)?;
        zones.insert(key, Arc::clone(&idx));
        Ok(idx)
    }
}

impl Default for IndexManager {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    #[test]
    fn get_or_open_returns_same_handle_for_repeat_zone() {
        let root = tempdir();
        let mgr = IndexManager::with_root(root);
        let a = mgr.get_or_open("zone-a").expect("open a");
        let b = mgr.get_or_open("zone-a").expect("re-open a");
        // Same Arc = same cached handle; writer state survives.
        assert!(Arc::ptr_eq(&a, &b));
    }

    #[test]
    fn distinct_zones_get_distinct_directories() {
        let root = tempdir();
        let mgr = IndexManager::with_root(root.clone());
        let _ = mgr.get_or_open("zone-a").expect("open a");
        let _ = mgr.get_or_open("zone-b").expect("open b");

        assert!(root.join("zone-a/fts-v2").exists(), "za dir not created");
        assert!(root.join("zone-b/fts-v2").exists(), "zb dir not created");
    }

    // NEXUS_DATA_DIR resolution regression tests.  Use `unsafe { set_var }`
    // per Rust 2024's guard (env is process-global; the tests are
    // serialised on the DATA_DIR_ENV key so a parallel test observing
    // `NEXUS_DATA_DIR=/foo` cannot cross-contaminate a test that
    // expects it unset).  Grouped with a static Mutex so no other test
    // in this file needs to touch env.

    static ENV_LOCK: parking_lot::Mutex<()> = parking_lot::Mutex::new(());

    #[test]
    fn default_root_falls_back_to_local_nexus_data_when_env_unset() {
        let _guard = ENV_LOCK.lock();
        let saved = std::env::var(DATA_DIR_ENV).ok();
        // SAFETY: single-threaded test-lock section; no other thread
        // can race the env read below.
        unsafe { std::env::remove_var(DATA_DIR_ENV) };
        let root = default_root();
        assert_eq!(root, PathBuf::from("./nexus-data").join("plugins/search"));
        if let Some(v) = saved {
            unsafe { std::env::set_var(DATA_DIR_ENV, v) };
        }
    }

    #[test]
    fn default_root_honours_nexus_data_dir_env() {
        let _guard = ENV_LOCK.lock();
        let saved = std::env::var(DATA_DIR_ENV).ok();
        // SAFETY: guarded by ENV_LOCK.
        unsafe { std::env::set_var(DATA_DIR_ENV, "/opt/nexus") };
        let root = default_root();
        assert_eq!(root, PathBuf::from("/opt/nexus/plugins/search"));
        match saved {
            Some(v) => unsafe { std::env::set_var(DATA_DIR_ENV, v) },
            None => unsafe { std::env::remove_var(DATA_DIR_ENV) },
        }
    }

    #[test]
    fn fts_dir_versioned_v2() {
        // #4618's en_stem tokenizer is an index-schema change — same
        // convention as ann-<tag>-v2: new dir alongside the old one,
        // pre-stemmer `fts/` dirs stay on disk untouched, a reindex
        // populates v2.
        let root = tempdir();
        let mgr = IndexManager::with_root(root.clone());
        assert_eq!(mgr.index_dir("za"), root.join("za/fts-v2"));
    }

    #[test]
    fn ann_dir_versioned_by_embedder_tag() {
        // A model swap must NOT clobber the existing index — new tag
        // = new directory alongside the old.  Manager holds both in
        // memory as long as either is referenced.
        let root = tempdir();
        let mgr = IndexManager::with_root(root.clone());
        let d_a = mgr.ann_dir("za", "mock");
        let d_b = mgr.ann_dir("za", "mE5-small-v1");
        assert_eq!(d_a, root.join("za/ann-mock-v2"));
        assert_eq!(d_b, root.join("za/ann-mE5-small-v1-v2"));
        assert_ne!(d_a, d_b);
    }

    #[test]
    fn get_or_open_ann_caches_by_zone_and_tag() {
        let root = tempdir();
        let mgr = IndexManager::with_root(root);
        let a1 = mgr.get_or_open_ann("za", "mock", 8).expect("open a-mock");
        let a2 = mgr
            .get_or_open_ann("za", "mock", 8)
            .expect("re-open a-mock");
        // Same (zone, tag) → same Arc.
        assert!(Arc::ptr_eq(&a1, &a2));

        // Different tag on same zone → different index.
        let b = mgr
            .get_or_open_ann("za", "other-tag", 8)
            .expect("open other");
        assert!(!Arc::ptr_eq(&a1, &b));
    }

    #[test]
    fn zones_write_and_read_independently() {
        // Regression scaffold for D3: cross-zone read isolation stays
        // the caller's job (kernel driver), but the storage layer
        // itself must not bleed docs between zones.
        let root = tempdir();
        let mgr = IndexManager::with_root(root);
        let a = mgr.get_or_open("zone-a").expect("open a");
        let b = mgr.get_or_open("zone-b").expect("open b");

        a.add_document("/x.md", 0, "za only", Some(1)).expect("add");
        a.commit().expect("commit a");
        b.add_document("/y.md", 0, "zb only", Some(2)).expect("add");
        b.commit().expect("commit b");

        let hits_a = a.search("only", 10, None).expect("search a");
        assert_eq!(hits_a.len(), 1);
        assert_eq!(hits_a[0].path, "/x.md");

        let hits_b = b.search("only", 10, None).expect("search b");
        assert_eq!(hits_b.len(), 1);
        assert_eq!(hits_b[0].path, "/y.md");
    }

    #[test]
    fn skeleton_cache_rebuilds_on_generation_change() {
        let root = tempfile::tempdir().expect("tempdir").keep();
        let mgr = IndexManager::with_root(root);
        let fts = mgr.get_or_open("zoneA").expect("open");
        fts.add_document("/a.md", 0, "# Alpha\nbody", Some(1)).expect("add");
        fts.commit().expect("commit");

        let sk1 = expect_fresh(mgr.get_or_build_skeleton("zoneA"));
        assert_eq!(sk1.doc_count(), 1);
        // Same generation → the SAME Arc comes back (cache hit).
        let sk1b = expect_fresh(mgr.get_or_build_skeleton("zoneA"));
        assert!(std::sync::Arc::ptr_eq(&sk1, &sk1b));

        // Mutate + commit → generation bump → rebuild with fresh docs.
        fts.add_document("/b.md", 0, "# Beta\nbody", Some(2)).expect("add");
        fts.commit().expect("commit");
        let sk2 = expect_fresh(mgr.get_or_build_skeleton("zoneA"));
        assert!(!std::sync::Arc::ptr_eq(&sk1, &sk2));
        assert_eq!(sk2.doc_count(), 2);

        // Zone isolation: an unrelated zone builds its own skeleton.
        let sk_other = expect_fresh(mgr.get_or_build_skeleton("zoneB"));
        assert_eq!(sk_other.doc_count(), 0);
    }

    fn expect_fresh(
        access: Result<SkeletonAccess, IndexError>,
    ) -> std::sync::Arc<crate::title_index::ZoneSkeleton> {
        match access.expect("no error") {
            SkeletonAccess::Fresh(sk) => sk,
            SkeletonAccess::Stale(_) => panic!("expected Fresh, got Stale"),
            SkeletonAccess::Building => panic!("expected Fresh, got Building"),
        }
    }

    #[test]
    fn zone_dirty_flag_marks_clears_and_isolates_zones() {
        // Review R5 (#4628): writers mark before the first sink
        // mutation and clear only after every sink committed; a
        // failed write leaves the mark so the query cache stays
        // bypassed until a later successful write.
        let root = tempfile::tempdir().expect("tempdir").keep();
        let mgr = IndexManager::with_root(root);
        assert!(!mgr.zone_is_dirty("zoneA"));
        mgr.mark_zone_dirty("zoneA");
        assert!(mgr.zone_is_dirty("zoneA"));
        assert!(!mgr.zone_is_dirty("zoneB"), "zones must not share the flag");
        mgr.clear_zone_dirty("zoneA");
        assert!(!mgr.zone_is_dirty("zoneA"));
        // Clearing an unmarked zone is a no-op, not a panic.
        mgr.clear_zone_dirty("zoneB");
    }

    #[test]
    fn zone_dirty_flag_survives_restart() {
        // Review R6 (#4628): a crash between sink commits must not
        // erase the cache-bypass signal — the sentinel file carries
        // it across manager restarts.
        let root = tempfile::tempdir().expect("tempdir").keep();
        {
            let mgr = IndexManager::with_root(root.clone());
            mgr.mark_zone_dirty("zoneA");
            // Simulated crash: the manager is dropped without clear.
        }
        let reborn = IndexManager::with_root(root);
        assert!(
            reborn.zone_is_dirty("zoneA"),
            "dirty mark must survive a restart until a successful write clears it"
        );
        reborn.clear_zone_dirty("zoneA");
        assert!(!reborn.zone_is_dirty("zoneA"));
    }

    #[test]
    fn skeleton_build_is_single_flight_busy_serves_stale_or_building() {
        let root = tempfile::tempdir().expect("tempdir").keep();
        let mgr = IndexManager::with_root(root);
        let fts = mgr.get_or_open("zoneA").expect("open");
        fts.add_document("/a.md", 0, "# Alpha\nbody", Some(1)).expect("add");
        fts.commit().expect("commit");

        // Cold start with the build lock held elsewhere: no snapshot
        // exists → callers get Building (empty arm, NOT cacheable)
        // instead of piling onto a second scan.
        let lock = mgr.skeleton_build_lock_for_test("zoneA");
        {
            let _held = lock.lock();
            let got = mgr.get_or_build_skeleton("zoneA").expect("no error");
            assert!(
                matches!(got, SkeletonAccess::Building),
                "cold start + busy builder must report Building"
            );
        }

        // Build once, then stale-serve: bump the generation and hold
        // the lock again — callers get the PRIOR snapshot marked
        // Stale (NOT cacheable), not a duplicate build.
        let sk1 = expect_fresh(mgr.get_or_build_skeleton("zoneA"));
        fts.add_document("/b.md", 0, "# Beta\nbody", Some(2)).expect("add");
        fts.commit().expect("commit");
        {
            let _held = lock.lock();
            match mgr.get_or_build_skeleton("zoneA").expect("no error") {
                SkeletonAccess::Stale(got) => {
                    assert!(
                        std::sync::Arc::ptr_eq(&sk1, &got),
                        "busy builder must serve the stale snapshot"
                    );
                    assert_eq!(got.doc_count(), 1);
                }
                SkeletonAccess::Fresh(_) => panic!("stale snapshot must not be reported Fresh"),
                SkeletonAccess::Building => panic!("prior snapshot exists — must be Stale"),
            }
        }
        // Lock released → the next call rebuilds to the new generation.
        let sk2 = expect_fresh(mgr.get_or_build_skeleton("zoneA"));
        assert_eq!(sk2.doc_count(), 2);
    }
}
