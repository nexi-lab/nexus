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

/// Process-wide save sequence — combined with the pid it makes every
/// save's temp file name unique (see `save` for why that matters).
static SAVE_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

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

/// Per-DT_STREAM cache entry (append-incremental indexing, P4).
///
/// A DT_STREAM is an append-only log — full re-chunking on every
/// refresh is O(K·N) across K refreshes on a stream growing with
/// each refresh, i.e. O(N²) total cost for the sudocode transcript
/// use case.  This cache records HOW MUCH of the stream we already
/// chunked so a refresh can chunk only the tail bytes past the last
/// checkpoint and continue chunk_index from where the prior pass
/// left off.  With this in place the total cost drops to O(N) across
/// all refreshes.
///
/// Correctness invariants (see [`IndexState::stream_state`] +
/// [`IndexState::stream_advance`]):
///   * `indexed_byte_len` is a byte-count into the deframed content
///     the host-shim `sys_read` returns for a DT_STREAM (nexus-vfs
///     #235 posture).  A retention-trim on the stream (sudden
///     shrink of the returned blob) invalidates the checkpoint —
///     the caller detects `new_len < indexed_byte_len` and calls
///     [`IndexState::forget_stream`] to force a full re-chunk.
///   * `next_chunk_index` is monotonic — the plugin's chunker
///     assigns 0-based chunk indices, so appending starts at
///     `next_chunk_index` and produces the next-N indices for the
///     newly-chunked tail.  The chunk keys under (path, index) in
///     FTS + ANN never collide with prior chunks.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq)]
pub struct StreamState {
    /// Bytes into the deframed content already chunked + indexed.
    /// A stream that has never been indexed has no entry here (the
    /// caller reads `stream_state().unwrap_or_default()` and the
    /// default is zero, which correctly triggers a full first pass).
    pub indexed_byte_len: u64,
    /// The chunk_index the NEXT append-chunk will use.  Equals the
    /// total chunk count at the last successful indexing pass.
    pub next_chunk_index: u32,
    /// Kernel-reported `modified_at_ms` at last successful index —
    /// same shape as [`FileEntry::mtime_ms`], enabling the same
    /// Refresh mtime-verdict pathway.  A stream whose last-append
    /// time has not moved since this stamp needs no work.
    pub mtime_ms: Option<i64>,
}

/// On-disk shape of the state cache.
#[derive(Debug, Serialize, Deserialize, Default)]
struct Persisted {
    #[serde(default = "state_version_default")]
    version: u32,
    #[serde(default)]
    files: HashMap<String, FileEntry>,
    /// Append-incremental checkpoints for DT_STREAM paths (P4).
    /// Empty on legacy state files; older readers ignore the field
    /// via `#[serde(default)]` on write / read.  Sibling map to
    /// `files` — paths never overlap because DT_STREAM vs DT_REG is
    /// enforced upstream at the entry-type gate.
    #[serde(default)]
    streams: HashMap<String, StreamState>,
    /// Embedder generation the recorded mtimes were completed under
    /// (review R8).  ANN storage is keyed by embedder tag, so a model
    /// swap opens a FRESH ann directory — mtimes recorded under the
    /// old tag must not read Unchanged against it, or the new index
    /// never populates.  None on legacy files / keyword-only zones.
    #[serde(default)]
    embedder_tag: Option<String>,
}

/// Zone-scoped mtime cache.  Cheap to clone through Arc for
/// service.rs's use in Refresh.
pub struct IndexState {
    dir: PathBuf,
    inner: RwLock<HashMap<String, FileEntry>>,
    /// Sibling map for append-incremental DT_STREAM checkpoints
    /// (see [`StreamState`]).  Loaded from + saved to the same
    /// on-disk file as `inner`; separate map so DT_REG vs
    /// DT_STREAM paths never collide on lookup.
    streams: RwLock<HashMap<String, StreamState>>,
    embedder_tag: RwLock<Option<String>>,
}

impl IndexState {
    /// Load the state file at `<zone_root>/index_state.json` if it
    /// exists; otherwise start empty.  A missing file is normal on
    /// first Index for a fresh zone.
    pub fn open_or_create(zone_root: PathBuf) -> Result<Self, StateError> {
        std::fs::create_dir_all(&zone_root)
            .map_err(|e| StateError::CreateDir(zone_root.display().to_string(), e.to_string()))?;
        let path = zone_root.join(STATE_FILE);
        let mut persisted_tag: Option<String> = None;
        let (inner, streams) = if path.exists() {
            let bytes = std::fs::read(&path)
                .map_err(|e| StateError::Read(path.display().to_string(), e.to_string()))?;
            let persisted: Persisted = serde_json::from_slice(&bytes)
                .map_err(|e| StateError::Parse(path.display().to_string(), e.to_string()))?;
            persisted_tag = persisted.embedder_tag.clone();
            if persisted.version != STATE_VERSION {
                // Stale layout generation (see STATE_VERSION doc).
                // KEEP the path keys but null every mtime instead of
                // starting empty (review R2): only the FTS moved to a
                // new directory in the fts-v2 bump — the ANN index is
                // still the live one, and this state is its ONLY
                // deletion set.  Dropping the keys would make vectors
                // for files deleted before the upgrade invisible to
                // Refresh's stale sweep forever.  Null mtimes verdict
                // Changed, so the next Refresh re-indexes every
                // present file into the new layout AND sweeps the
                // absent ones out of ANN.  The next save() rewrites
                // the file at the new version.
                tracing::warn!(
                    on_disk = persisted.version,
                    expected = STATE_VERSION,
                    path = %path.display(),
                    "index_state version mismatch — nulling mtimes; next refresh reindexes + sweeps",
                );
                let files: HashMap<String, FileEntry> = persisted
                    .files
                    .into_iter()
                    .map(|(p, mut entry)| {
                        entry.mtime_ms = None;
                        (p, entry)
                    })
                    .collect();
                // DT_STREAM checkpoints are a strict speed-up over a
                // full re-chunk; a version bump doesn't change the
                // chunk layout so we could keep them, but nulling
                // them here mirrors the file behaviour — the next
                // refresh does a full pass anyway, which will
                // rebuild the checkpoints cleanly.
                (files, HashMap::new())
            } else {
                (persisted.files, persisted.streams)
            }
        } else {
            (HashMap::new(), HashMap::new())
        };
        Ok(Self {
            dir: zone_root,
            inner: RwLock::new(inner),
            streams: RwLock::new(streams),
            embedder_tag: RwLock::new(persisted_tag),
        })
    }

    /// Align the state with the ACTIVE embedder generation (review
    /// R8).  When the recorded tag differs from `tag`, every cached
    /// mtime is nulled in memory — the ANN directory for the new tag
    /// starts empty (or stale relative to edits made under another
    /// tag), so every file must verdict Changed and re-embed into it.
    /// Records `tag` as the current generation either way.  Returns
    /// true when an invalidation happened.
    pub fn ensure_embedder_generation(&self, tag: &str) -> bool {
        let mut current = self.embedder_tag.write();
        if current.as_deref() == Some(tag) {
            return false;
        }
        let had_prior = current.is_some();
        *current = Some(tag.to_string());
        drop(current);
        if had_prior {
            let mut files = self.inner.write();
            for entry in files.values_mut() {
                entry.mtime_ms = None;
            }
            // Same reasoning for streams — chunks land in the
            // per-tag ann-<tag> dir, so a model swap must force a
            // full re-embed.  Drop the whole checkpoint (not just
            // the mtime) so the next refresh does a fresh full
            // pass and repopulates from the new embedder.
            self.streams.write().clear();
            true
        } else {
            // First generation stamp (legacy state / fresh zone):
            // nothing was completed under a DIFFERENT tag, so the
            // recorded mtimes stay valid.
            false
        }
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
    ///
    /// Consults both the [`FileEntry`] map (DT_REG paths) and the
    /// [`StreamState`] map (DT_STREAM paths) — a path is only in
    /// one at a time (upstream entry-type gate enforces).  The
    /// stream check short-circuits Refresh's per-path sys_read
    /// entirely for a quiescent stream, which matters because a
    /// DT_STREAM's sys_read is a whole-log memcopy — much more
    /// expensive than a regular-file sys_stat.
    pub fn verdict(&self, path: &str, fresh_mtime: Option<i64>) -> RefreshVerdict {
        let cached_file_mtime = self.inner.read().get(path).and_then(|e| e.mtime_ms);
        if let (Some(cached_ms), Some(fresh_ms)) = (cached_file_mtime, fresh_mtime) {
            if cached_ms == fresh_ms {
                return RefreshVerdict::Unchanged;
            }
        }
        let cached_stream_mtime = self.streams.read().get(path).and_then(|s| s.mtime_ms);
        if let (Some(cached_ms), Some(fresh_ms)) = (cached_stream_mtime, fresh_mtime) {
            if cached_ms == fresh_ms {
                return RefreshVerdict::Unchanged;
            }
        }
        RefreshVerdict::Changed
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
        // A path removed from the corpus can't stay in the stream
        // checkpoint map either — otherwise a fresh sys_setattr
        // creating a new DT_STREAM at the same path would inherit
        // stale offsets and skip chunking the initial content.
        self.streams.write().remove(path);
    }

    // ── DT_STREAM append-incremental checkpoints (P4) ─────────────

    /// Current checkpoint for `path` — `None` when the stream has
    /// never been indexed on this zone.  The caller uses the
    /// returned `indexed_byte_len` to slice the tail bytes and
    /// `next_chunk_index` to continue the chunk-index sequence.
    pub fn stream_state(&self, path: &str) -> Option<StreamState> {
        self.streams.read().get(path).cloned()
    }

    /// Record a successful append-index pass — new absolute
    /// byte-length + chunk count.  Called AFTER FTS + ANN adds
    /// land, same posture as [`Self::record`] for regular files.
    /// Never regresses the checkpoint — a stale caller trying to
    /// write a smaller value must call [`Self::forget_stream`]
    /// first, which is the retention-trim recovery path.
    pub fn stream_advance(
        &self,
        path: &str,
        new_indexed_byte_len: u64,
        new_next_chunk_index: u32,
        mtime_ms: Option<i64>,
    ) {
        self.streams.write().insert(
            path.to_string(),
            StreamState {
                indexed_byte_len: new_indexed_byte_len,
                next_chunk_index: new_next_chunk_index,
                mtime_ms,
            },
        );
    }

    /// Drop the checkpoint for `path` — retention-trim recovery
    /// (kernel-side stream retention advanced past our
    /// checkpoint's byte offset) and full re-index paths.
    pub fn forget_stream(&self, path: &str) {
        self.streams.write().remove(path);
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
            streams: self.streams.read().clone(),
            embedder_tag: self.embedder_tag.read().clone(),
        };
        let bytes =
            serde_json::to_vec_pretty(&persisted).map_err(|e| StateError::Encode(e.to_string()))?;
        // Unique temp file per save: a FIXED .tmp path lets two
        // concurrent savers interleave write/rename and publish a
        // torn or stale snapshot (review R1).  The zone write lock
        // serializes indexing ops in-process, but a unique name also
        // keeps a crashed writer's leftover from being renamed over
        // fresh state by a later save.
        let tmp = path.with_extension(format!(
            "json.tmp.{}.{:x}",
            std::process::id(),
            SAVE_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
        ));
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
    fn stale_on_disk_version_nulls_mtimes_but_keeps_deletion_set() {
        // Index-layout bumps (fts → fts-v2, #4618) must invalidate
        // this cache so the fresh index repopulates — but the ANN
        // index did NOT move, and these path keys are its only
        // deletion set (review R2).  Dropping them would leave
        // vectors for pre-upgrade-deleted files undiscoverable by
        // Refresh's stale sweep forever.  So: keys survive, mtimes
        // null (⇒ every present file re-indexes, every absent one
        // sweeps).
        let dir = tempdir();
        std::fs::write(
            dir.join(STATE_FILE),
            r#"{"version":1,"files":{"/a.md":{"mtime_ms":123}}}"#,
        )
        .expect("write stale-version state");
        let s = IndexState::open_or_create(dir).expect("open must not error");
        assert!(!s.is_empty(), "migration must keep the deletion set");
        // Path known, mtime nulled — always verdicts Changed.
        assert_eq!(s.cached_mtime("/a.md"), Some(None));
        assert_eq!(s.known_paths(), vec!["/a.md".to_string()]);
        assert!(matches!(
            s.verdict("/a.md", Some(123)),
            RefreshVerdict::Changed
        ));
    }

    #[test]
    fn embedder_generation_change_invalidates_mtimes() {
        // Review R8: ANN storage is keyed by embedder tag — a model
        // swap must null the mtime cache or the fresh ann-<new-tag>
        // directory never populates (everything reads Unchanged).
        let dir = tempdir();
        let s = IndexState::open_or_create(dir.clone()).expect("open");
        s.record("/a.md", Some(100));
        assert!(
            !s.ensure_embedder_generation("tag-a"),
            "first stamp keeps mtimes"
        );
        assert_eq!(s.cached_mtime("/a.md"), Some(Some(100)));
        s.save().expect("save");

        // Same tag on reopen: no invalidation.
        let s = IndexState::open_or_create(dir.clone()).expect("reopen");
        assert!(!s.ensure_embedder_generation("tag-a"));
        assert_eq!(s.cached_mtime("/a.md"), Some(Some(100)));

        // Different tag: every mtime nulls -> always Changed.
        assert!(
            s.ensure_embedder_generation("tag-b"),
            "tag change must invalidate"
        );
        assert_eq!(s.cached_mtime("/a.md"), Some(None));
        assert!(matches!(
            s.verdict("/a.md", Some(100)),
            RefreshVerdict::Changed
        ));
        s.save().expect("save-2");

        // Persisted: reopen under tag-b is quiet, back to tag-a invalidates again.
        let s = IndexState::open_or_create(dir).expect("reopen-2");
        assert!(!s.ensure_embedder_generation("tag-b"));
        assert!(s.ensure_embedder_generation("tag-a"));
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
