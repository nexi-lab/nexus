//! Per-zone incremental-refresh checkpoint (Phase 5 of the Python-
//! parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! # What this stores
//!
//! For every path the FTS + ANN indexes currently hold, we record
//! its `modified_at_ms` from the kernel at Index time.  On Refresh
//! the walker compares the current kernel mtime against this cache
//! and only re-indexes files that:
//!
//! - are NEW (in the walk but not the cache),
//! - have a NEWER mtime than the cache remembers (edited since
//!   last Index), or conversely
//! - are STALE — in the cache but missing from the current walk
//!   (deleted); those get dropped from both FTS + ANN.
//!
//! Files whose mtime matches the cache exactly are left alone —
//! the whole point of P5 is that a Refresh over a mostly-unchanged
//! corpus is O(walk + a couple of small file reads) rather than
//! O(full reindex).
//!
//! # SSOT + persistence posture
//!
//! Corpus SSOT stays the kernel VFS (per D1).  This cache is
//! **derived state** — dropping the file is always safe, the next
//! Refresh (or Index) rebuilds it.  Per-node, not replicated:
//! each node maintains its own indexes and its own cache; the
//! MetaStore isn't involved because the plugin doesn't currently
//! have a metastore callback in KernelHandle and adding one would
//! violate principle 1b (don't lightly modify kernel ABI).
//!
//! # File layout
//!
//! `<root>/<zone_id>/index_state.json` — a sibling of the `fts-v2/` +
//! `ann-*-v2/` directories under the per-zone root.  Atomic rewrite
//! via write-then-rename so a mid-write crash never leaves a
//! truncated file that would break the next open.
//!
//! # Concurrency
//!
//! `IndexState` is behind a `parking_lot::RwLock`.  Reads (Refresh
//! diff pass) take the read lock; writes (record after successful
//! per-file index) take the write lock briefly.  Save happens at
//! end-of-Refresh so the diff loop doesn't take a lock per file.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// Sidecar filename for the per-zone mtime cache.  Same
/// per-zone-root neighbourhood as `sidecar.json` inside the ANN
/// dir, but this one lives one level up (the FTS + ANN dirs are
/// derived-state; this is meta about them).
const STATE_FILE: &str = "index_state.json";

/// On-disk schema version.  A mismatched on-disk version loads as
/// EMPTY (not an error): the cache is derived state, and a version
/// bump signals the indices it describes were invalidated — an empty
/// cache makes the next Refresh re-index everything into the new
/// layout.  Preserving the old cache instead would report Unchanged
/// for every file and leave the fresh index silently empty.
///
/// v1 → v2: #4618 `en_stem` schema change moved the FTS store from
/// `fts/` to `fts-v2/`; v1 caches describe the abandoned v1 dir.
const STATE_VERSION: u32 = 2;

fn state_version_default() -> u32 {
    STATE_VERSION
}

/// Per-file cache entry.  Two fields today; extended in future
/// phases (e.g. chunk_count for pooling diagnostics).
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct FileEntry {
    /// Kernel-reported mtime at last successful index.  Compared
    /// against fresh sys_stat to detect edits.  `None` means the
    /// kernel didn't hand us an mtime — those files always
    /// re-index on Refresh (defensive: no mtime, no dedup).
    pub mtime_ms: Option<i64>,
}

/// On-disk shape of the state cache.
#[derive(Debug, Serialize, Deserialize, Default)]
struct Persisted {
    #[serde(default = "state_version_default")]
    version: u32,
    #[serde(default)]
    files: HashMap<String, FileEntry>,
}

/// Zone-scoped mtime cache.  Cheap to clone through Arc for
/// service.rs's use in Refresh.
pub struct IndexState {
    dir: PathBuf,
    inner: RwLock<HashMap<String, FileEntry>>,
}

impl IndexState {
    /// Load the state file at `<zone_root>/index_state.json` if it
    /// exists; otherwise start empty.  A missing file is normal on
    /// first Index for a fresh zone.
    pub fn open_or_create(zone_root: PathBuf) -> Result<Self, StateError> {
        std::fs::create_dir_all(&zone_root)
            .map_err(|e| StateError::CreateDir(zone_root.display().to_string(), e.to_string()))?;
        let path = zone_root.join(STATE_FILE);
        let inner = if path.exists() {
            let bytes = std::fs::read(&path)
                .map_err(|e| StateError::Read(path.display().to_string(), e.to_string()))?;
            let persisted: Persisted = serde_json::from_slice(&bytes)
                .map_err(|e| StateError::Parse(path.display().to_string(), e.to_string()))?;
            if persisted.version != STATE_VERSION {
                // Stale layout generation (see STATE_VERSION doc):
                // start empty so the next Refresh re-indexes the
                // whole corpus into the current index layout.  The
                // next save() rewrites the file at the new version.
                tracing::warn!(
                    on_disk = persisted.version,
                    expected = STATE_VERSION,
                    path = %path.display(),
                    "index_state version mismatch — resetting mtime cache; next refresh reindexes",
                );
                HashMap::new()
            } else {
                persisted.files
            }
        } else {
            HashMap::new()
        };
        Ok(Self {
            dir: zone_root,
            inner: RwLock::new(inner),
        })
    }

    /// Snapshot of every recorded path.  Callers use this to detect
    /// stale (deleted-from-VFS) entries during the Refresh diff.
    /// Read-lock short-held; the returned `Vec<String>` is a copy.
    pub fn known_paths(&self) -> Vec<String> {
        self.inner.read().keys().cloned().collect()
    }

    /// Look up a path's cached mtime — used in the mtime-changed
    /// check during Refresh.
    pub fn cached_mtime(&self, path: &str) -> Option<Option<i64>> {
        self.inner.read().get(path).map(|e| e.mtime_ms)
    }

    /// Compare the cached mtime for `path` against `fresh_mtime`.
    /// Returns:
    /// - `RefreshVerdict::Unchanged` if the mtime matches AND we
    ///   have a concrete mtime for the file.
    /// - `RefreshVerdict::Changed` if new / edited / has-no-mtime
    ///   (fail-open: re-index rather than risk staleness).
    pub fn verdict(&self, path: &str, fresh_mtime: Option<i64>) -> RefreshVerdict {
        let cached = self.inner.read().get(path).cloned();
        match (cached, fresh_mtime) {
            (
                Some(FileEntry {
                    mtime_ms: Some(cached_ms),
                }),
                Some(fresh_ms),
            ) if cached_ms == fresh_ms => RefreshVerdict::Unchanged,
            _ => RefreshVerdict::Changed,
        }
    }

    /// Record a successful index of `path` at `mtime_ms`.  Called
    /// per-file after both FTS and ANN adds land, before the
    /// commit — so a mid-Refresh crash leaves the cache reflecting
    /// only files that made it through the writer transaction.
    pub fn record(&self, path: &str, mtime_ms: Option<i64>) {
        self.inner
            .write()
            .insert(path.to_string(), FileEntry { mtime_ms });
    }

    /// Drop a path from the cache — called after the corresponding
    /// FTS + ANN deletes when a file was removed from the corpus.
    pub fn forget(&self, path: &str) {
        self.inner.write().remove(path);
    }

    /// Number of cached entries — used by tests + operator
    /// diagnostics.
    pub fn len(&self) -> usize {
        self.inner.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.read().is_empty()
    }

    /// Persist the cache to disk.  Atomic write-then-rename so a
    /// mid-write crash never leaves a truncated JSON file that
    /// would fail the next `open_or_create`.  Called at
    /// end-of-Refresh / end-of-Index.
    pub fn save(&self) -> Result<(), StateError> {
        let path = self.dir.join(STATE_FILE);
        let persisted = Persisted {
            version: STATE_VERSION,
            files: self.inner.read().clone(),
        };
        let bytes =
            serde_json::to_vec_pretty(&persisted).map_err(|e| StateError::Encode(e.to_string()))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| StateError::Write(tmp.display().to_string(), e.to_string()))?;
        std::fs::rename(&tmp, &path)
            .map_err(|e| StateError::Write(path.display().to_string(), e.to_string()))?;
        Ok(())
    }

    /// Absolute path to the sidecar file — used by tests to check
    /// layout.
    pub fn state_file(&self) -> PathBuf {
        self.dir.join(STATE_FILE)
    }

    /// Zone root the state lives under.  For tests + operator
    /// diagnostics.
    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

/// Verdict from `verdict()` — three-way but modelled as two
/// variants because callers uniformly branch on "reindex needed
/// or not".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefreshVerdict {
    /// File's cached mtime matches the fresh kernel mtime — safe
    /// to skip re-indexing.
    Unchanged,
    /// File is new, edited, or has no mtime — needs re-index.
    Changed,
}

#[derive(Debug, thiserror::Error)]
pub enum StateError {
    #[error("create state dir {0}: {1}")]
    CreateDir(String, String),
    #[error("read state file {0}: {1}")]
    Read(String, String),
    #[error("parse state file {0}: {1}")]
    Parse(String, String),
    #[error("encode state: {0}")]
    Encode(String),
    #[error("write state file {0}: {1}")]
    Write(String, String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    #[test]
    fn open_or_create_returns_empty_on_fresh_zone() {
        let s = IndexState::open_or_create(tempdir()).expect("open");
        assert_eq!(s.len(), 0);
        assert!(s.is_empty());
        assert!(s.known_paths().is_empty());
    }

    #[test]
    fn record_then_verdict_reports_unchanged_on_match() {
        let s = IndexState::open_or_create(tempdir()).expect("open");
        s.record("/a.md", Some(1_700_000_000_000));
        assert_eq!(
            s.verdict("/a.md", Some(1_700_000_000_000)),
            RefreshVerdict::Unchanged,
        );
    }

    #[test]
    fn verdict_reports_changed_when_mtime_advances() {
        let s = IndexState::open_or_create(tempdir()).expect("open");
        s.record("/a.md", Some(1_700_000_000_000));
        assert_eq!(
            s.verdict("/a.md", Some(1_700_000_000_500)),
            RefreshVerdict::Changed,
        );
    }

    #[test]
    fn verdict_reports_changed_on_missing_from_cache() {
        let s = IndexState::open_or_create(tempdir()).expect("open");
        assert_eq!(
            s.verdict("/never-seen.md", Some(1_700_000_000_000)),
            RefreshVerdict::Changed,
        );
    }

    #[test]
    fn verdict_reports_changed_when_no_mtime_available() {
        // Defensive: if the kernel didn't hand us a fresh mtime,
        // fall open to Changed (re-index) rather than silently
        // treat as Unchanged and let a real edit slip.
        let s = IndexState::open_or_create(tempdir()).expect("open");
        s.record("/a.md", Some(1_700_000_000_000));
        assert_eq!(s.verdict("/a.md", None), RefreshVerdict::Changed);
    }

    #[test]
    fn save_then_open_survives_restart() {
        let dir = tempdir();
        {
            let s = IndexState::open_or_create(dir.clone()).expect("open");
            s.record("/a.md", Some(1_700_000_000_000));
            s.record("/b.md", None); // no-mtime doc
            s.save().expect("save");
        }
        let s2 = IndexState::open_or_create(dir).expect("reopen");
        assert_eq!(s2.len(), 2);
        assert_eq!(s2.cached_mtime("/a.md"), Some(Some(1_700_000_000_000)));
        assert_eq!(s2.cached_mtime("/b.md"), Some(None));
    }

    #[test]
    fn forget_removes_from_cache_and_survives_save() {
        let dir = tempdir();
        let s = IndexState::open_or_create(dir.clone()).expect("open");
        s.record("/a.md", Some(1));
        s.record("/b.md", Some(2));
        s.forget("/a.md");
        s.save().expect("save");

        let s2 = IndexState::open_or_create(dir).expect("reopen");
        assert_eq!(s2.len(), 1);
        assert_eq!(s2.cached_mtime("/b.md"), Some(Some(2)));
        assert_eq!(s2.cached_mtime("/a.md"), None);
    }

    #[test]
    fn known_paths_is_snapshot_not_borrow() {
        // Regression guard: `known_paths` returns a copy so the
        // caller can iterate + call `verdict` (which read-locks)
        // without deadlocking on a nested read-lock.  If the API
        // ever changes to return `&[String]` (borrowed from the
        // read lock guard), this test surfaces the deadlock risk.
        let s = IndexState::open_or_create(tempdir()).expect("open");
        s.record("/a.md", Some(1));
        let paths = s.known_paths();
        for p in &paths {
            let _ = s.verdict(p, Some(1));
        }
        assert_eq!(paths, vec!["/a.md".to_string()]);
    }

    #[test]
    fn stale_on_disk_version_loads_as_empty_not_error() {
        // Index-layout bumps (fts → fts-v2, #4618) must invalidate
        // this cache: a preserved v1 cache reports Unchanged for
        // every file and the fresh (empty) index never repopulates.
        // The cache is derived state — an old version loads as EMPTY
        // (full reindex on next Refresh) rather than erroring.
        let dir = tempdir();
        std::fs::write(
            dir.join(STATE_FILE),
            r#"{"version":1,"files":{"/a.md":{"mtime_ms":123}}}"#,
        )
        .expect("write stale-version state");
        let s = IndexState::open_or_create(dir).expect("open must not error");
        assert!(s.is_empty(), "stale-version cache must reset to empty");
        assert_eq!(s.cached_mtime("/a.md"), None);
    }

    #[test]
    fn atomic_rewrite_never_leaves_truncated_state() {
        let dir = tempdir();
        let s = IndexState::open_or_create(dir.clone()).expect("open");
        s.record("/a.md", Some(1));
        s.save().expect("save-1");
        // Second save should also succeed (rename overwrites).
        s.record("/b.md", Some(2));
        s.save().expect("save-2");
        // No lingering .tmp file left behind.
        let tmp = dir.join("index_state.json.tmp");
        assert!(!tmp.exists(), "leftover tmp file: {}", tmp.display());
    }
}
