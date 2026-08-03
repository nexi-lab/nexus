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
//! The directory layout is `<root>/<zone_id>/fts/`.  Root resolves
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

use crate::fts_index::{FtsIndex, IndexError};

/// Directory name inside a per-zone index root for the FTS store.
/// Sibling directories (Phase 2's `ann/` for HNSW, etc.) land next
/// to this one under the same zone root.
const FTS_SUBDIR: &str = "fts";

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

/// Zone-id → `Arc<FtsIndex>` cache; lazily opens per-zone indices.
pub struct IndexManager {
    root: PathBuf,
    zones: Mutex<HashMap<String, Arc<FtsIndex>>>,
}

impl IndexManager {
    /// Manager rooted at the platform default (`~/.nexus/plugins/search/`).
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
        }
    }

    /// Return the on-disk directory the given zone's FTS index lives
    /// in.  Broken out so tests can assert layout without going
    /// through [`get_or_open`].
    pub fn index_dir(&self, zone_id: &str) -> PathBuf {
        self.root.join(zone_id).join(FTS_SUBDIR)
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

        assert!(root.join("zone-a/fts").exists(), "za dir not created");
        assert!(root.join("zone-b/fts").exists(), "zb dir not created");
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
}
