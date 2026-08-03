//! Per-zone HNSW vector index for semantic search (Phase 2 of the
//! Python-parity roadmap; see `PARITY_ROADMAP.md` D1).
//!
//! # Storage layout
//!
//! Each zone's ANN index lives at
//! `<root>/<zone_id>/ann-<model>-<version>/`:
//!
//! - `hnsw.hnsw.graph` — graph structure written by `Hnsw::file_dump`
//! - `hnsw.hnsw.data`  — flat vector store, same source
//! - `sidecar.json`    — id → path + shadowed-id set (see below)
//!
//! The model tag in the directory name (`ann-mE5-small-v1`) ties the
//! index to the embedder version — a model swap creates a NEW
//! directory alongside the old one, so operators can roll back by
//! pointing the manager at either.  Directories are per-node derived
//! state; the kernel VFS is the corpus SSOT (same rule as the FTS
//! index in `fts_index.rs`).
//!
//! # Id + path mapping
//!
//! HNSW works on `usize` ids.  We assign monotonic ids and keep
//! `path → id` + `id → path` in the sidecar so:
//!
//! 1. **Search results carry paths.** `hnsw_rs::Neighbour` returns
//!    only the id; the sidecar's reverse map turns each id back into
//!    a path.
//!
//! 2. **Re-indexing the same path is idempotent.** `hnsw_rs` has no
//!    delete primitive — inserting `path_A` twice would leave two
//!    graph nodes, doubling `path_A`'s recall weight.  Instead we
//!    move the OLD id to a `shadowed` set and insert with a new id;
//!    search post-filters shadowed hits before returning.  The
//!    shadowed entries stay in the graph (waste memory), so a
//!    periodic full rebuild is deferred to P5's indexing pipeline —
//!    the same MetaStore-checkpoint moment that already schedules a
//!    tantivy segment merge.
//!
//! # Concurrency
//!
//! `hnsw_rs::Hnsw` takes `&self` for insert AND search (interior
//! parking_lot locks), so multiple threads can call either freely.
//! Our sidecar is behind a `parking_lot::RwLock<Sidecar>`:
//!
//! - Search takes a read lock to look up paths + filter shadows.
//! - Add takes a write lock to bump the counter + rotate mappings.
//!
//! The lock is not held across the HNSW call itself, so a slow
//! search does not stall a concurrent add on the sidecar.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anndists::dist::DistCosine;
use hnsw_rs::hnsw::{Hnsw, Neighbour};
use hnsw_rs::hnswio::{HnswIo, ReloadOptions};
use hnsw_rs::prelude::AnnT;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// Basename used by `Hnsw::file_dump` — produces
/// `<basename>.hnsw.graph` + `<basename>.hnsw.data`.  Matched by
/// [`HnswIo::new`] on reload.
const HNSW_BASENAME: &str = "hnsw";

/// Sidecar filename holding the id/path mapping + shadowed ids.
const SIDECAR_FILE: &str = "sidecar.json";

/// HNSW `M` — max out-edges per node.  16 is the standard tuning
/// from the original paper for cosine distance on ≤10 M points.
const HNSW_M: usize = 16;

/// HNSW `max_layer` — 16 accommodates ~10 M inserts (ceiling ~ log_M N).
const HNSW_MAX_LAYER: usize = 16;

/// HNSW `ef_construction` — build-time neighbour width.  200 is the
/// paper's balanced tuning; increases recall @ index-build cost.
const HNSW_EF_CONSTRUCTION: usize = 200;

/// HNSW `capacity` hint (soft-cap on pre-alloc).  Not a hard limit —
/// inserts past this pay reallocation cost.
const DEFAULT_CAPACITY: usize = 100_000;

/// Result row returned by [`AnnIndex::search`].  Distance is the raw
/// cosine distance (0.0 = identical, 2.0 = opposite); callers convert
/// to a similarity score via `1.0 - distance` if they want the
/// familiar "higher is better" shape.
#[derive(Debug, Clone, PartialEq)]
pub struct AnnHit {
    pub path: String,
    pub distance: f32,
}

/// On-disk state that survives restarts.  `Hnsw` itself is dumped by
/// hnsw_rs into its own two files; this JSON captures everything
/// else (id counter, path ↔ id mappings, shadowed-id set).
#[derive(Debug, Serialize, Deserialize, Default)]
struct Sidecar {
    next_id: usize,
    path_to_id: HashMap<String, usize>,
    /// Ids no longer valid — their doc was re-indexed under a new
    /// id.  Search post-filter drops these before returning hits.
    shadowed: HashSet<usize>,
}

impl Sidecar {
    /// Rebuild the reverse map (`id → path`) from `path_to_id`.  Not
    /// stored to disk — cheap to derive on load, and keeping it out
    /// of the JSON avoids the two mappings drifting when the file is
    /// hand-edited (a debug/support workflow we want to preserve).
    fn id_to_path(&self) -> HashMap<usize, String> {
        self.path_to_id
            .iter()
            .map(|(p, id)| (*id, p.clone()))
            .collect()
    }
}

/// One per-zone HNSW vector index.  Cheap to clone (`Arc` inside).
pub struct AnnIndex {
    dim: usize,
    dir: PathBuf,
    hnsw: Hnsw<'static, f32, DistCosine>,
    sidecar: RwLock<Sidecar>,
    /// Cached reverse map — regenerated on every write to keep search
    /// path lookups O(1) without holding the sidecar RwLock for the
    /// whole search.
    id_to_path: RwLock<HashMap<usize, String>>,
}

impl AnnIndex {
    /// Open the index at `dir` if a prior dump exists, otherwise
    /// create a fresh empty one.  `dim` MUST match what the embedder
    /// produces — hnsw_rs does not validate dimensionality on insert
    /// and a mismatch reads past the end of the input slice.
    pub fn open_or_create(dir: PathBuf, dim: usize) -> Result<Arc<Self>, AnnError> {
        assert!(dim > 0, "AnnIndex dim must be > 0");
        std::fs::create_dir_all(&dir)
            .map_err(|e| AnnError::CreateDir(dir.display().to_string(), e.to_string()))?;

        let graph = dir.join(format!("{HNSW_BASENAME}.hnsw.graph"));
        let sidecar_path = dir.join(SIDECAR_FILE);

        let (hnsw, sidecar) = if graph.exists() && sidecar_path.exists() {
            // Leak the HnswIo so its buffer lives 'static — hnsw_rs's
            // `load_hnsw` returns a `Hnsw<'a>` tied to the loader's
            // internal storage, and Hnsw needs to outlive this function
            // (it's owned by the returned Arc<AnnIndex>).  Leaking
            // costs one struct per zone open, permanent — negligible
            // for a long-running daemon.  A more surgical fix would
            // switch to an owning reload API when hnsw_rs exposes one.
            let io = Box::leak(Box::new(HnswIo::new(&dir, HNSW_BASENAME)));
            io.set_options(ReloadOptions::default());
            let hnsw = io
                .load_hnsw::<f32, DistCosine>()
                .map_err(|e| AnnError::Open(dir.display().to_string(), e.to_string()))?;
            let bytes = std::fs::read(&sidecar_path)
                .map_err(|e| AnnError::Open(sidecar_path.display().to_string(), e.to_string()))?;
            let sidecar: Sidecar = serde_json::from_slice(&bytes)
                .map_err(|e| AnnError::Open(sidecar_path.display().to_string(), e.to_string()))?;
            (hnsw, sidecar)
        } else {
            let hnsw = Hnsw::<f32, DistCosine>::new(
                HNSW_M,
                DEFAULT_CAPACITY,
                HNSW_MAX_LAYER,
                HNSW_EF_CONSTRUCTION,
                DistCosine {},
            );
            (hnsw, Sidecar::default())
        };

        let id_to_path = sidecar.id_to_path();
        Ok(Arc::new(Self {
            dim,
            dir,
            hnsw,
            sidecar: RwLock::new(sidecar),
            id_to_path: RwLock::new(id_to_path),
        }))
    }

    /// Add / replace the embedding for `path`.  Idempotent — a prior
    /// vector under the same path is shadowed instead of duplicated
    /// (see the "Id + path mapping" section of the module doc).
    ///
    /// Errors on:
    /// - `vector.len() != dim`
    /// - all-zero vector (cosine distance is undefined; would NaN
    ///   propagate through the graph)
    pub fn add_vector(&self, path: &str, vector: &[f32]) -> Result<(), AnnError> {
        if vector.len() != self.dim {
            return Err(AnnError::DimMismatch {
                path: path.to_string(),
                got: vector.len(),
                expected: self.dim,
            });
        }
        if is_all_zero(vector) {
            return Err(AnnError::ZeroVector(path.to_string()));
        }

        // Rotate mappings under the write lock.  Short-held — no
        // HNSW ops inside.
        let new_id = {
            let mut side = self.sidecar.write();
            let id = side.next_id;
            side.next_id += 1;
            if let Some(prior) = side.path_to_id.insert(path.to_string(), id) {
                // Old id survives in the graph but no longer
                // resolves to a live path — mark it shadowed so
                // search's post-filter drops it.
                side.shadowed.insert(prior);
            }
            id
        };

        // HNSW insert is thread-safe on `&self`; no lock needed here.
        self.hnsw.insert((vector, new_id));

        // Refresh the cached reverse map so search's path lookup
        // sees the new id immediately.  Cheap on typical zone sizes;
        // a smarter incremental update is a follow-up if this ever
        // shows up on a profile.
        let map = self.sidecar.read().id_to_path();
        *self.id_to_path.write() = map;

        Ok(())
    }

    /// Nearest-`k` search.  Returns hits sorted by cosine distance
    /// ascending (nearest first), with shadowed ids filtered out.
    ///
    /// `ef` (search-time neighbour width) is derived from `k` per
    /// hnsw_rs's guidance: `ef = max(k, HNSW_M * 2)` — high enough
    /// to keep recall stable, low enough to stay sub-ms on ≤100 K
    /// points.  Callers with recall-vs-latency preferences reach
    /// down to [`search_with_ef`](Self::search_with_ef).
    pub fn search(&self, query: &[f32], k: usize) -> Result<Vec<AnnHit>, AnnError> {
        let ef = std::cmp::max(k, HNSW_M * 2);
        self.search_with_ef(query, k, ef)
    }

    pub fn search_with_ef(
        &self,
        query: &[f32],
        k: usize,
        ef: usize,
    ) -> Result<Vec<AnnHit>, AnnError> {
        if query.len() != self.dim {
            return Err(AnnError::DimMismatch {
                path: "<query>".to_string(),
                got: query.len(),
                expected: self.dim,
            });
        }
        if is_all_zero(query) {
            return Err(AnnError::ZeroVector("<query>".to_string()));
        }
        if k == 0 {
            return Ok(Vec::new());
        }

        // Over-fetch so the shadow filter can drop stale hits and
        // still return `k` live results in the typical case (few
        // shadows).  Bounded so a heavily-reindexed zone doesn't
        // scan the whole graph.
        let overfetch = std::cmp::min(k * 4, k + 100);
        let effective_ef = std::cmp::max(ef, overfetch);

        let neighbours: Vec<Neighbour> = self.hnsw.search(query, overfetch, effective_ef);

        let side = self.sidecar.read();
        let id_map = self.id_to_path.read();

        let mut out = Vec::with_capacity(k);
        for n in neighbours {
            if side.shadowed.contains(&n.d_id) {
                continue;
            }
            let Some(path) = id_map.get(&n.d_id) else {
                // Defensive: mapping missing for a live id would
                // mean sidecar drift.  Drop the hit and log rather
                // than surface a bogus "" path.
                tracing::warn!(
                    id = n.d_id,
                    "ann: id in graph but not in sidecar — dropping"
                );
                continue;
            };
            out.push(AnnHit {
                path: path.clone(),
                distance: n.distance,
            });
            if out.len() >= k {
                break;
            }
        }
        Ok(out)
    }

    /// Persist the graph + sidecar to disk.  Callers batch adds and
    /// commit once at end-of-request — same pattern as `FtsIndex`.
    ///
    /// Skips the hnsw file dump entirely when the index is empty —
    /// `hnsw_rs::file_dump` errors on a graph with zero points, and
    /// that's not a useful failure mode for an Index call that
    /// happened to walk a corpus with no valid text (e.g. all files
    /// binary / oversize / empty).  The sidecar still writes so the
    /// next open_or_create knows the manager has seen this zone;
    /// the graph files reappear the next time a real doc arrives.
    pub fn commit(&self) -> Result<(), AnnError> {
        let live = self.sidecar.read().path_to_id.len();
        if live > 0 {
            self.hnsw
                .file_dump(&self.dir, HNSW_BASENAME)
                .map_err(|e| AnnError::Commit(format!("hnsw file_dump: {e}")))?;
        }
        let sidecar_path = self.dir.join(SIDECAR_FILE);
        let side = self.sidecar.read();
        let bytes = serde_json::to_vec_pretty(&*side)
            .map_err(|e| AnnError::Commit(format!("sidecar encode: {e}")))?;
        // Atomic swap via write-and-rename so a mid-write crash
        // never leaves a truncated sidecar file that would break
        // the next open_or_create.
        let tmp = sidecar_path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| AnnError::Commit(format!("sidecar tmp write: {e}")))?;
        std::fs::rename(&tmp, &sidecar_path)
            .map_err(|e| AnnError::Commit(format!("sidecar rename: {e}")))?;
        Ok(())
    }

    /// Number of live documents (indexed, not shadowed).  Used by
    /// tests + operator diagnostics.
    pub fn live_count(&self) -> usize {
        let side = self.sidecar.read();
        // path_to_id holds one entry per live path, so its size IS
        // the live count.  `shadowed` is orthogonal (old ids no
        // longer referenced by any path).
        side.path_to_id.len()
    }

    /// Dimensionality this index was created with.  Callers embed
    /// with the model that produced this dim.
    pub fn dim(&self) -> usize {
        self.dim
    }

    /// Absolute path to the on-disk directory.  Tests use this to
    /// verify the layout contract; operators use it to size zones.
    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

fn is_all_zero(v: &[f32]) -> bool {
    v.iter().all(|&x| x == 0.0)
}

// ── Errors ────────────────────────────────────────────────────────

#[derive(Debug, thiserror::Error)]
pub enum AnnError {
    #[error("create ann dir {0}: {1}")]
    CreateDir(String, String),
    #[error("open ann index at {0}: {1}")]
    Open(String, String),
    #[error("dimension mismatch for {path}: got {got}, expected {expected}")]
    DimMismatch {
        path: String,
        got: usize,
        expected: usize,
    },
    #[error("zero vector for {0}: cosine distance is undefined")]
    ZeroVector(String),
    #[error("commit: {0}")]
    Commit(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    /// Trivial deterministic vector — component `i` = `seed * (i+1)`,
    /// scaled to unit-ish magnitude so the cosine distances between
    /// distinct seeds are non-degenerate.
    fn vec_seed(dim: usize, seed: f32) -> Vec<f32> {
        (0..dim).map(|i| (seed + (i as f32) * 0.01).sin()).collect()
    }

    #[test]
    fn open_creates_empty_index() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir.clone(), 8).expect("open");
        assert!(dir.exists());
        assert_eq!(idx.live_count(), 0);
        assert_eq!(idx.dim(), 8);
        // Search on an empty index returns zero hits, not a panic.
        let hits = idx.search(&vec_seed(8, 1.0), 5).expect("search");
        assert!(hits.is_empty());
    }

    #[test]
    fn add_then_search_finds_nearest() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/a", &vec_seed(4, 1.0)).expect("add a");
        idx.add_vector("/b", &vec_seed(4, 5.0)).expect("add b");
        idx.add_vector("/c", &vec_seed(4, 9.0)).expect("add c");
        assert_eq!(idx.live_count(), 3);

        // Query with /b's exact vector — nearest hit must be /b at
        // distance ~0.
        let hits = idx.search(&vec_seed(4, 5.0), 3).expect("search");
        assert_eq!(hits.len(), 3);
        assert_eq!(hits[0].path, "/b");
        assert!(
            hits[0].distance < 0.001,
            "nearest hit distance too high: {hits:?}"
        );
        // Order is nearest → farthest.
        for pair in hits.windows(2) {
            assert!(
                pair[0].distance <= pair[1].distance,
                "hits not sorted by distance ascending: {hits:?}",
            );
        }
    }

    #[test]
    fn dim_mismatch_is_rejected_not_undefined() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        let err = idx
            .add_vector("/x", &vec_seed(3, 1.0))
            .expect_err("must reject wrong dim");
        assert!(
            matches!(err, AnnError::DimMismatch { .. }),
            "unexpected: {err:?}"
        );
    }

    #[test]
    fn zero_vector_is_rejected() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        let err = idx
            .add_vector("/x", &vec![0.0; 4])
            .expect_err("must reject zero vec");
        assert!(
            matches!(err, AnnError::ZeroVector(_)),
            "unexpected: {err:?}"
        );
    }

    #[test]
    fn readd_same_path_shadows_old_and_returns_new_vector() {
        // Idempotency regression: hnsw_rs has no delete, so a naive
        // re-add would leave TWO graph nodes for the same path.  The
        // shadow-filter path in search must drop the old id and
        // return the new vector's distance.
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/a", &vec_seed(4, 1.0)).expect("add-1");
        idx.add_vector("/other", &vec_seed(4, 10.0))
            .expect("add-other");
        idx.add_vector("/a", &vec_seed(4, 20.0)).expect("re-add");
        assert_eq!(idx.live_count(), 2, "path count stays at 2 after re-add");

        // Query with the NEW vector — /a must come first at ~0
        // distance; the OLD /a id must not appear.
        let hits = idx.search(&vec_seed(4, 20.0), 5).expect("search");
        assert_eq!(
            hits.iter().filter(|h| h.path == "/a").count(),
            1,
            "shadowed dup leaked: {hits:?}"
        );
        assert_eq!(hits[0].path, "/a");
        assert!(hits[0].distance < 0.001);
    }

    #[test]
    fn commit_then_reopen_survives_restart() {
        let dir = tempdir().join("ann");
        {
            let idx = AnnIndex::open_or_create(dir.clone(), 4).expect("open");
            idx.add_vector("/a", &vec_seed(4, 1.0)).expect("add-a");
            idx.add_vector("/b", &vec_seed(4, 5.0)).expect("add-b");
            idx.commit().expect("commit");
        }
        // Fresh open — the graph + sidecar files come back to life.
        let idx2 = AnnIndex::open_or_create(dir, 4).expect("reopen");
        assert_eq!(idx2.live_count(), 2);
        let hits = idx2.search(&vec_seed(4, 5.0), 2).expect("search");
        assert_eq!(hits[0].path, "/b");
    }

    #[test]
    fn top_k_bounded_by_request() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        for i in 0..20 {
            idx.add_vector(&format!("/p{i}"), &vec_seed(4, i as f32 + 1.0))
                .expect("add");
        }
        let hits = idx.search(&vec_seed(4, 3.0), 5).expect("search");
        assert_eq!(hits.len(), 5, "search must not exceed requested k");
    }
}
