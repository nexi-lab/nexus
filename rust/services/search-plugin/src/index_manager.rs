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
//! The directory layout is `<root>/<zone_id>/fts/`.  Root defaults
//! to `~/.nexus/plugins/search/` (see [`default_root`]); tests pass
//! a tempdir.  Zone-id sanitisation is minimal — the caller (the
//! kernel-tier RPC handler) never lets a client-controlled string
//! reach this API; `zone_id` values are drawn from the raft-managed
//! zone registry.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::Mutex;

use crate::fts_index::{FtsIndex, IndexError};

/// Directory name inside a per-zone index root for the FTS store.
/// Sibling directories (Phase 2's `ann/` for HNSW, etc.) land next
/// to this one under the same zone root.
const FTS_SUBDIR: &str = "fts";

/// Resolve the default per-node storage root.  Follows the same
/// per-platform convention `dirs::data_local_dir` gives — the
/// sibling vault plugin uses the same rule for its own state.
///
/// Fallback: relative `nexus/plugins/search/` if the home directory
/// cannot be resolved.  This only fires on a broken user profile;
/// callers wanting deterministic paths pass their own root to
/// [`IndexManager::with_root`].
pub fn default_root() -> PathBuf {
    dirs::home_dir()
        .map(|h| h.join(".nexus/plugins/search"))
        .unwrap_or_else(|| PathBuf::from("nexus/plugins/search"))
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
