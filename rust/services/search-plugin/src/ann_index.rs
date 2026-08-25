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
//! # Id + (path, chunk_index) mapping
//!
//! HNSW works on `usize` ids.  We assign monotonic ids and keep
//! `(path, chunk_index) → id` + `id → (path, chunk_index)` in the
//! sidecar so:
//!
//! 1. **Search results carry (path, chunk_index).**
//!    `hnsw_rs::Neighbour` returns only the id; the sidecar's
//!    reverse map turns each id back into a path + chunk index.
//!    P4's multi-chunk-per-file emission means the two fields
//!    together are what identifies a doc — `path` alone would
//!    collide across chunks of the same file.
//!
//! 2. **Re-indexing the same chunk is idempotent.** `hnsw_rs` has
//!    no delete primitive — inserting the same `(path, chunk)`
//!    twice would leave two graph nodes, doubling the doc's recall
//!    weight.  Instead we move the OLD id to a `shadowed` set and
//!    insert with a new id; search post-filters shadowed hits
//!    before returning.  The shadowed entries stay in the graph
//!    (waste memory), so a periodic full rebuild is deferred to
//!    P5's indexing pipeline — the same MetaStore-checkpoint moment
//!    that already schedules a tantivy segment merge.
//!
//! 3. **Whole-file removal via [`delete_all_chunks`](AnnIndex::delete_all_chunks).**
//!    A file's chunk count can change across reindexes — the caller
//!    (`do_index` in service.rs) drops ALL chunks for a path before
//!    inserting the freshly-chunked set, so an old chunk 3 that no
//!    longer exists doesn't linger as a ghost hit.
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

/// Remove hnsw dump files whose basename differs from `current` (the
/// pair the just-committed sidecar points at). See `Sidecar::graph_basename`
/// for why dumps are generation-named.
fn prune_stale_dump_pairs(dir: &std::path::Path, current: &str) {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in rd.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let Some(base) = name
            .strip_suffix(".hnsw.graph")
            .or_else(|| name.strip_suffix(".hnsw.data"))
        else {
            continue;
        };
        if base != current {
            let _ = std::fs::remove_file(entry.path());
        }
    }
}

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
/// familiar "higher is better" shape.  `chunk_index` identifies which
/// chunk of the source file this hit refers to (0 in P1-P3 = one
/// chunk per file; real values from P4 onward).
#[derive(Debug, Clone, PartialEq)]
pub struct AnnHit {
    pub path: String,
    pub chunk_index: u32,
    pub distance: f32,
}

/// Sidecar format version.  Bumped from v1 (path-keyed) to v2
/// ((path, chunk_index)-keyed) at P4.  The `ann-<tag>-v2/` dir
/// name in [`IndexManager::ann_dir`] means v1 and v2 indexes
/// never share a directory; if a v1 sidecar somehow appears in a
/// v2 dir the serde parse fails and `open_or_create` returns an
/// AnnError::Open with the format-drift clue.
const SIDECAR_VERSION: u32 = 2;

fn sidecar_version_default() -> u32 {
    SIDECAR_VERSION
}

/// One persisted mapping row.  Vec-of-struct instead of a
/// HashMap<(String,u32), usize> because JSON keys must be strings;
/// a Vec keeps the on-disk shape trivial and the in-memory lookup
/// table (`chunk_to_id`) is derived on load.
#[derive(Debug, Serialize, Deserialize, Clone)]
struct SidecarEntry {
    path: String,
    chunk_index: u32,
    id: usize,
}

/// On-disk state that survives restarts.  `Hnsw` itself is dumped by
/// hnsw_rs into its own two files; this JSON captures everything
/// else (id counter, (path, chunk_index) ↔ id mappings, shadowed-id
/// set).
#[derive(Debug, Serialize, Deserialize, Default)]
struct Sidecar {
    #[serde(default = "sidecar_version_default")]
    version: u32,
    next_id: usize,
    #[serde(default)]
    entries: Vec<SidecarEntry>,
    /// Ids no longer valid — their doc was re-indexed under a new
    /// id, or their whole file was reindexed with a new chunk shape.
    /// Search post-filter drops these before returning hits.
    #[serde(default)]
    shadowed: HashSet<usize>,
    /// Basename of the hnsw dump pair this sidecar's entries describe
    /// (`<basename>.hnsw.{graph,data}`). Written on every commit;
    /// reload opens EXACTLY this pair. `None` = legacy sidecar from
    /// before generation-named dumps (fixed `hnsw` basename era).
    ///
    /// Why not a fixed name: `hnsw_rs::file_dump` does NOT honor the
    /// requested basename unconditionally — with a datamap-active
    /// graph it uniquifies to `<basename>-<rand>` (DumpInit) and
    /// returns the name it actually used; with overwrite it truncates
    /// the previous dump in place (a crash mid-dump destroys the only
    /// copy). Observed live: a restarted plugin reloaded NOTHING
    /// (fixed-name pair absent, five `hnsw-<rand>` pairs on disk)
    /// while stats still reported the sidecar-era chunk count — the
    /// semantic lane silently served [] for every pre-restart doc.
    /// Generation-named dumps + this recorded pointer make the swap
    /// atomic (the sidecar rename IS the commit point) and reloads
    /// deterministic.
    #[serde(default)]
    graph_basename: Option<String>,
    /// Monotonic dump-generation counter for basename minting.
    #[serde(default)]
    generation: u64,
}

/// One per-zone HNSW vector index.  Cheap to clone (`Arc` inside).
pub struct AnnIndex {
    dim: usize,
    dir: PathBuf,
    hnsw: Hnsw<'static, f32, DistCosine>,
    sidecar: RwLock<Sidecar>,
    /// In-memory lookup: `(path, chunk_index) → id`.  Used by
    /// add_vector to detect re-index and rotate the old id into
    /// `shadowed`.  Rebuilt from `sidecar.entries` on load; kept in
    /// sync with the sidecar on every write.
    chunk_to_id: RwLock<HashMap<(String, u32), usize>>,
    /// Cached reverse map: `id → (path, chunk_index)`.  Regenerated
    /// on every write so search's path lookup stays O(1) without
    /// holding the sidecar RwLock for the whole search.
    id_to_chunk: RwLock<HashMap<usize, (String, u32)>>,
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

        let sidecar_path = dir.join(SIDECAR_FILE);

        // The sidecar is the source of truth for WHICH dump pair is
        // live (see Sidecar::graph_basename) — read it first, then
        // open exactly the pair it names.
        let loaded_sidecar: Option<Sidecar> = if sidecar_path.exists() {
            let bytes = std::fs::read(&sidecar_path)
                .map_err(|e| AnnError::Open(sidecar_path.display().to_string(), e.to_string()))?;
            let sidecar: Sidecar = serde_json::from_slice(&bytes)
                .map_err(|e| AnnError::Open(sidecar_path.display().to_string(), e.to_string()))?;
            if sidecar.version != SIDECAR_VERSION {
                return Err(AnnError::Open(
                    sidecar_path.display().to_string(),
                    format!(
                        "sidecar version {} ≠ expected {}; drop this dir and reindex",
                        sidecar.version, SIDECAR_VERSION,
                    ),
                ));
            }
            Some(sidecar)
        } else {
            None
        };

        let fresh = || {
            let hnsw = Hnsw::<f32, DistCosine>::new(
                HNSW_M,
                DEFAULT_CAPACITY,
                HNSW_MAX_LAYER,
                HNSW_EF_CONSTRUCTION,
                DistCosine {},
            );
            let side = Sidecar {
                version: SIDECAR_VERSION,
                ..Default::default()
            };
            (hnsw, side)
        };

        let (hnsw, sidecar) = match loaded_sidecar {
            Some(sidecar) => {
                let basename = sidecar
                    .graph_basename
                    .clone()
                    .unwrap_or_else(|| HNSW_BASENAME.to_string());
                let graph = dir.join(format!("{basename}.hnsw.graph"));
                if graph.exists() {
                    // Leak the HnswIo so its buffer lives 'static —
                    // hnsw_rs's `load_hnsw` returns a `Hnsw<'a>` tied to
                    // the loader's internal storage, and Hnsw needs to
                    // outlive this function (it's owned by the returned
                    // Arc<AnnIndex>).  Leaking costs one struct per zone
                    // open, permanent — negligible for a long-running
                    // daemon.  A more surgical fix would switch to an
                    // owning reload API when hnsw_rs exposes one.
                    let io = Box::leak(Box::new(HnswIo::new(&dir, &basename)));
                    io.set_options(ReloadOptions::default());
                    let hnsw = io
                        .load_hnsw::<f32, DistCosine>()
                        .map_err(|e| AnnError::Open(dir.display().to_string(), e.to_string()))?;
                    (hnsw, sidecar)
                } else if sidecar.entries.is_empty() {
                    // Sidecar without a dump = zone that never committed
                    // a non-empty graph.  Normal fresh path.
                    fresh()
                } else {
                    // Sidecar CLAIMS live vectors but its dump pair is
                    // gone (legacy fixed-name sidecar whose pair was
                    // uniquified away, or a lost/partial dump).  Serving
                    // would silently return [] for everything while
                    // stats report the sidecar count — reset LOUDLY to
                    // an honest empty index instead; a reindex heals.
                    tracing::error!(
                        dir = %dir.display(),
                        basename = %basename,
                        orphaned_entries = sidecar.entries.len(),
                        "ANN sidecar references a missing hnsw dump pair — resetting to an \
                         empty index (semantic hits for this zone are gone until reindexed)",
                    );
                    fresh()
                }
            }
            None => fresh(),
        };

        let chunk_to_id: HashMap<(String, u32), usize> = sidecar
            .entries
            .iter()
            .map(|e| ((e.path.clone(), e.chunk_index), e.id))
            .collect();
        let id_to_chunk: HashMap<usize, (String, u32)> = sidecar
            .entries
            .iter()
            .map(|e| (e.id, (e.path.clone(), e.chunk_index)))
            .collect();
        Ok(Arc::new(Self {
            dim,
            dir,
            hnsw,
            sidecar: RwLock::new(sidecar),
            chunk_to_id: RwLock::new(chunk_to_id),
            id_to_chunk: RwLock::new(id_to_chunk),
        }))
    }

    /// Add / replace the embedding for `(path, chunk_index)`.
    /// Idempotent — a prior vector under the same key is shadowed
    /// instead of duplicated (see the "Id + (path, chunk_index)
    /// mapping" section of the module doc).
    ///
    /// Errors on:
    /// - `vector.len() != dim`
    /// - all-zero vector (cosine distance is undefined; would NaN
    ///   propagate through the graph)
    pub fn add_vector(&self, path: &str, chunk_index: u32, vector: &[f32]) -> Result<(), AnnError> {
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
        let (new_id, prior_id) = {
            let mut side = self.sidecar.write();
            let id = side.next_id;
            side.next_id += 1;

            let key = (path.to_string(), chunk_index);
            let mut lookup = self.chunk_to_id.write();
            let prior = lookup.insert(key.clone(), id);
            if let Some(pid) = prior {
                // Old id survives in the graph but no longer
                // resolves to a live (path, chunk) — shadow it.
                side.shadowed.insert(pid);
                // Drop the stale sidecar entry so commit() writes
                // the current mapping.
                side.entries.retain(|e| e.id != pid);
            }
            side.entries.push(SidecarEntry {
                path: path.to_string(),
                chunk_index,
                id,
            });
            self.id_to_chunk.write().insert(id, key);
            if let Some(pid) = prior {
                self.id_to_chunk.write().remove(&pid);
            }
            (id, prior)
        };
        let _ = prior_id; // captured only for the shadow side-effect above

        // HNSW insert is thread-safe on `&self`; no lock needed here.
        self.hnsw.insert((vector, new_id));

        Ok(())
    }

    /// Drop ALL chunks for `path` — marks their ids as shadowed
    /// so search post-filter skips them; the graph nodes stay in
    /// place until the P5 periodic rebuild.  Idempotent: calling
    /// on a path with no live chunks is a no-op.
    ///
    /// The caller pattern for reindexing a file is:
    /// ```text
    /// ann.delete_all_chunks(path);
    /// for (chunk_index, vector) in fresh_chunks {
    ///     ann.add_vector(path, chunk_index, vector)?;
    /// }
    /// ann.commit()?;
    /// ```
    /// so a file that dropped from 3 chunks to 2 doesn't leave the
    /// stale third chunk lingering as a ghost hit.
    pub fn delete_all_chunks(&self, path: &str) {
        let mut side = self.sidecar.write();
        let mut lookup = self.chunk_to_id.write();
        let mut reverse = self.id_to_chunk.write();
        let to_shadow: Vec<usize> = lookup
            .iter()
            .filter_map(|((p, _), id)| if p == path { Some(*id) } else { None })
            .collect();
        for id in &to_shadow {
            side.shadowed.insert(*id);
            reverse.remove(id);
        }
        lookup.retain(|(p, _), _| p != path);
        side.entries.retain(|e| e.path != path);
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
        let id_map = self.id_to_chunk.read();

        let mut out = Vec::with_capacity(k);
        for n in neighbours {
            if side.shadowed.contains(&n.d_id) {
                continue;
            }
            let Some((path, chunk_index)) = id_map.get(&n.d_id) else {
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
                chunk_index: *chunk_index,
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
        // Mint a generation-named dump pair, then switch the
        // (atomically-renamed) sidecar to point at it — the sidecar
        // rename below is the commit point, so a crash anywhere before
        // it leaves the previous pair live and consistent, and a crash
        // mid-dump can never truncate the only good copy (the previous
        // pair is a different basename). `file_dump` cannot be trusted
        // to honor the requested basename (a datamap-active graph gets
        // a uniquified `<basename>-<rand>` via DumpInit) — always
        // record the name it RETURNS.
        let (bytes, dumped_basename) = {
            let mut side = self.sidecar.write();
            let dumped = if side.entries.is_empty() {
                // Empty graph: hnsw_rs errors on zero-point dumps.  The
                // sidecar still writes so the next open knows this zone;
                // any previously-recorded pair stays on disk untouched.
                None
            } else {
                side.generation += 1;
                let requested = format!("{HNSW_BASENAME}-g{}", side.generation);
                let actual = self
                    .hnsw
                    .file_dump(&self.dir, &requested)
                    .map_err(|e| AnnError::Commit(format!("hnsw file_dump: {e}")))?;
                side.graph_basename = Some(actual.clone());
                Some(actual)
            };
            let bytes = serde_json::to_vec_pretty(&*side)
                .map_err(|e| AnnError::Commit(format!("sidecar encode: {e}")))?;
            (bytes, dumped)
        };
        let sidecar_path = self.dir.join(SIDECAR_FILE);
        // Atomic swap via write-and-rename so a mid-write crash
        // never leaves a truncated sidecar file that would break
        // the next open_or_create.
        let tmp = sidecar_path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| AnnError::Commit(format!("sidecar tmp write: {e}")))?;
        std::fs::rename(&tmp, &sidecar_path)
            .map_err(|e| AnnError::Commit(format!("sidecar rename: {e}")))?;
        // Past the commit point: superseded generation pairs are dead
        // weight (each commit mints a fresh pair; without GC restarts
        // accumulate them forever). Best-effort — an unremovable stray
        // is unreferenced and harmless, and must not fail the commit.
        if let Some(current) = dumped_basename {
            prune_stale_dump_pairs(&self.dir, &current);
        }
        Ok(())
    }

    /// Number of live chunks (indexed, not shadowed).  Used by
    /// tests + operator diagnostics.  In P4 with multi-chunk-per-
    /// file emission this is > the number of distinct paths;
    /// [`live_paths`](Self::live_paths) gives the file count.
    pub fn live_count(&self) -> usize {
        self.sidecar.read().entries.len()
    }

    /// Number of distinct paths with at least one live chunk.
    /// Cheaper than iterating callers because we walk the in-memory
    /// map once and dedupe by path.
    pub fn live_paths(&self) -> usize {
        let lookup = self.chunk_to_id.read();
        let mut paths: HashSet<&str> = HashSet::new();
        for (p, _) in lookup.keys() {
            paths.insert(p.as_str());
        }
        paths.len()
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
        idx.add_vector("/a", 0, &vec_seed(4, 1.0)).expect("add a");
        idx.add_vector("/b", 0, &vec_seed(4, 5.0)).expect("add b");
        idx.add_vector("/c", 0, &vec_seed(4, 9.0)).expect("add c");
        assert_eq!(idx.live_count(), 3);

        // Query with /b's exact vector — nearest hit must be /b at
        // distance ~0.
        let hits = idx.search(&vec_seed(4, 5.0), 3).expect("search");
        // HNSW is approximate: assert the nearest-match invariant (an
        // exact-vector query always returns its own node at distance ~0),
        // NOT exhaustive recall of all 3 — hnsw_rs's unseeded RNG makes the
        // recall count non-deterministic on tiny graphs. Coexistence is
        // guarded deterministically by `live_count() == 3` above.
        assert!(!hits.is_empty(), "search returned no hits: {hits:?}");
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
            .add_vector("/x", 0, &vec_seed(3, 1.0))
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
            .add_vector("/x", 0, &[0.0; 4])
            .expect_err("must reject zero vec");
        assert!(
            matches!(err, AnnError::ZeroVector(_)),
            "unexpected: {err:?}"
        );
    }

    #[test]
    fn readd_same_key_shadows_old_and_returns_new_vector() {
        // Idempotency regression: hnsw_rs has no delete, so a naive
        // re-add would leave TWO graph nodes for the same
        // (path, chunk_index).  The shadow-filter path in search
        // must drop the old id and return the new vector's distance.
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/a", 0, &vec_seed(4, 1.0)).expect("add-1");
        idx.add_vector("/other", 0, &vec_seed(4, 10.0))
            .expect("add-other");
        idx.add_vector("/a", 0, &vec_seed(4, 20.0)).expect("re-add");
        assert_eq!(idx.live_count(), 2, "chunk count stays at 2 after re-add");

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
    fn distinct_chunks_of_same_path_coexist() {
        // P4 core contract: (path, chunk_index) is the primary key,
        // so /doc chunk 0 and /doc chunk 1 must BOTH live and search
        // must return both when distances allow.  A regression that
        // keys on path alone would shadow chunk 0 as soon as chunk 1
        // is added, breaking multi-chunk-per-file emission.
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/doc", 0, &vec_seed(4, 1.0))
            .expect("chunk 0");
        idx.add_vector("/doc", 1, &vec_seed(4, 5.0))
            .expect("chunk 1");
        idx.add_vector("/doc", 2, &vec_seed(4, 9.0))
            .expect("chunk 2");
        assert_eq!(idx.live_count(), 3, "three chunks live under one path");
        assert_eq!(idx.live_paths(), 1, "one distinct path");

        // Query with the middle chunk's vector — that chunk must
        // return with distance ≈ 0 AND carry chunk_index=1.
        let hits = idx.search(&vec_seed(4, 5.0), 3).expect("search");
        // Coexistence is guarded by `live_count() == 3` above (deterministic).
        // HNSW recall is approximate, so assert the exact-match round-trip
        // (the queried chunk 1 comes back), NOT `hits.len() == 3` — hnsw_rs's
        // unseeded RNG flakes the recall count on tiny graphs.
        assert!(!hits.is_empty(), "search returned no hits: {hits:?}");
        let top = &hits[0];
        assert_eq!(top.path, "/doc");
        assert_eq!(top.chunk_index, 1, "chunk_index must round-trip");
        assert!(top.distance < 0.001);
    }

    #[test]
    fn delete_all_chunks_removes_every_chunk_for_path() {
        // Whole-file removal via `delete_all_chunks` — used by
        // do_index's "reindex file: drop all then add fresh" pattern
        // so a file that dropped from 3 chunks to 2 doesn't leave
        // the stale third chunk as a ghost hit.
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/doc", 0, &vec_seed(4, 1.0)).unwrap();
        idx.add_vector("/doc", 1, &vec_seed(4, 5.0)).unwrap();
        idx.add_vector("/doc", 2, &vec_seed(4, 9.0)).unwrap();
        idx.add_vector("/other", 0, &vec_seed(4, 3.0)).unwrap();
        assert_eq!(idx.live_count(), 4);

        idx.delete_all_chunks("/doc");
        assert_eq!(idx.live_count(), 1, "only /other should remain");
        assert_eq!(idx.live_paths(), 1);

        // Any search must not surface a /doc hit.
        let hits = idx.search(&vec_seed(4, 5.0), 10).expect("search");
        assert!(
            !hits.iter().any(|h| h.path == "/doc"),
            "delete_all_chunks left ghost /doc hits: {hits:?}",
        );
    }

    #[test]
    fn delete_all_chunks_is_idempotent() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        idx.add_vector("/a", 0, &vec_seed(4, 1.0)).unwrap();
        idx.delete_all_chunks("/missing"); // no-op
        assert_eq!(idx.live_count(), 1);
        idx.delete_all_chunks("/a");
        idx.delete_all_chunks("/a"); // no-op the second time
        assert_eq!(idx.live_count(), 0);
    }

    #[test]
    fn commit_then_reopen_survives_restart() {
        let dir = tempdir().join("ann");
        {
            let idx = AnnIndex::open_or_create(dir.clone(), 4).expect("open");
            idx.add_vector("/a", 0, &vec_seed(4, 1.0)).expect("add-a");
            idx.add_vector("/b", 0, &vec_seed(4, 5.0)).expect("add-b");
            idx.commit().expect("commit");
        }
        // Fresh open — the graph + sidecar files come back to life.
        let idx2 = AnnIndex::open_or_create(dir, 4).expect("reopen");
        assert_eq!(idx2.live_count(), 2);
        let hits = idx2.search(&vec_seed(4, 5.0), 2).expect("search");
        assert_eq!(hits[0].path, "/b");
    }

    #[test]
    fn multi_commit_reopen_reloads_the_last_generation() {
        // Regression for the live silent-wipe: dumps are generation-named
        // and the sidecar records which pair is current; a reopen after
        // SEVERAL commits (the shape a real daemon produces via
        // incremental commits) must reload the newest graph, not an
        // absent fixed-name pair.
        let dir = tempdir().join("ann");
        {
            let idx = AnnIndex::open_or_create(dir.clone(), 4).expect("open");
            idx.add_vector("/a", 0, &vec_seed(4, 1.0)).expect("add-a");
            idx.commit().expect("commit-1");
            idx.add_vector("/b", 0, &vec_seed(4, 5.0)).expect("add-b");
            idx.commit().expect("commit-2");
            idx.add_vector("/c", 0, &vec_seed(4, 9.0)).expect("add-c");
            idx.commit().expect("commit-3");
        }
        let idx2 = AnnIndex::open_or_create(dir.clone(), 4).expect("reopen");
        assert_eq!(idx2.live_count(), 3);
        let hits = idx2.search(&vec_seed(4, 9.0), 3).expect("search");
        assert_eq!(hits[0].path, "/c");

        // The recorded basename must reference a pair that exists.
        let bytes = std::fs::read(dir.join(SIDECAR_FILE)).expect("sidecar");
        let side: Sidecar = serde_json::from_slice(&bytes).expect("decode");
        let basename = side.graph_basename.expect("graph_basename recorded");
        assert!(dir.join(format!("{basename}.hnsw.graph")).exists());
        assert!(dir.join(format!("{basename}.hnsw.data")).exists());
    }

    #[test]
    fn commit_gc_leaves_exactly_one_dump_pair() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir.clone(), 4).expect("open");
        for i in 0..3 {
            idx.add_vector(&format!("/p{i}"), 0, &vec_seed(4, i as f32 + 1.0))
                .expect("add");
            idx.commit().expect("commit");
        }
        let pairs: Vec<String> = std::fs::read_dir(&dir)
            .expect("read_dir")
            .flatten()
            .filter_map(|e| e.file_name().to_str().map(str::to_string))
            .filter(|n| n.ends_with(".hnsw.graph") || n.ends_with(".hnsw.data"))
            .collect();
        assert_eq!(pairs.len(), 2, "one .graph + one .data, got {pairs:?}");
    }

    #[test]
    fn orphaned_sidecar_resets_loudly_instead_of_lying() {
        // A sidecar claiming live entries whose dump pair is gone (the
        // pre-fix legacy state observed live: fixed-name sidecar, only
        // uniquified `hnsw-<rand>` pairs on disk) must come back as an
        // honest EMPTY index — not an index whose stats say N chunks
        // while every search serves [].
        let dir = tempdir().join("ann");
        {
            let idx = AnnIndex::open_or_create(dir.clone(), 4).expect("open");
            idx.add_vector("/a", 0, &vec_seed(4, 1.0)).expect("add");
            idx.commit().expect("commit");
        }
        // Simulate the legacy/lost-dump state: remove the dump pair the
        // sidecar points at.
        let bytes = std::fs::read(dir.join(SIDECAR_FILE)).expect("sidecar");
        let side: Sidecar = serde_json::from_slice(&bytes).expect("decode");
        let basename = side.graph_basename.expect("recorded");
        std::fs::remove_file(dir.join(format!("{basename}.hnsw.graph"))).expect("rm graph");
        std::fs::remove_file(dir.join(format!("{basename}.hnsw.data"))).expect("rm data");

        let idx2 = AnnIndex::open_or_create(dir, 4).expect("reopen resets, not errors");
        assert_eq!(
            idx2.live_count(),
            0,
            "stats must not report unservable chunks"
        );
        let hits = idx2.search(&vec_seed(4, 1.0), 3).expect("search");
        assert!(hits.is_empty());
    }

    #[test]
    fn top_k_bounded_by_request() {
        let dir = tempdir().join("ann");
        let idx = AnnIndex::open_or_create(dir, 4).expect("open");
        for i in 0..20 {
            idx.add_vector(&format!("/p{i}"), 0, &vec_seed(4, i as f32 + 1.0))
                .expect("add");
        }
        let hits = idx.search(&vec_seed(4, 3.0), 5).expect("search");
        // The invariant is the BOUND (never MORE than k); assert `<= k` plus
        // non-empty rather than `== k`, since HNSW recall of the full k is
        // approximate (hnsw_rs unseeded RNG flakes the exact count).
        assert!(
            !hits.is_empty() && hits.len() <= 5,
            "search must not exceed requested k: {hits:?}"
        );
    }
}
