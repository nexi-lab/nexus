//! Per-zone parked-documents queue (Phase 8 of the Python-parity
//! roadmap; see `PARITY_ROADMAP.md`).
//!
//! # What this stores
//!
//! Documents that hit a transient failure during IndexDocuments
//! (writer full, projection race, embedder blip) get "parked" for
//! later retry.  The queue is a per-zone JSON sidecar at
//! `<root>/<zone>/parked.json`.  Mirrors Python's parked-queue
//! surface (`GET /parked`, `POST /parked/retry`,
//! `POST /parked/discard`) so the router can flip
//! `SEARCH_BACKEND=rust` without losing the operator UI.
//!
//! # SSOT + persistence posture
//!
//! Per-node derived state.  A parked doc is a signal to retry —
//! not a durable fact.  Dropping the sidecar is safe: the next
//! Refresh + user re-submit surfaces the same failures again.
//!
//! # Concurrency
//!
//! `parking_lot::RwLock<HashMap<path, ParkedEntry>>` per zone;
//! same shape as `IndexState`.  Read + write lock windows are
//! short (a HashMap op).

use std::collections::HashMap;
use std::path::PathBuf;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

/// Sidecar filename.  Sibling of index_state.json under the
/// per-zone root.
const PARKED_FILE: &str = "parked.json";

/// Schema version — bump semantics identical to IndexState /
/// AnnIndex sidecar.
const PARKED_VERSION: u32 = 1;

fn parked_version_default() -> u32 {
    PARKED_VERSION
}

/// One parked doc.  `parked_at_ms` is wall-clock — operator UI
/// sorts by "oldest first" so recent failures don't crowd out
/// long-neglected ones.
#[derive(Debug, Serialize, Deserialize, Clone, PartialEq, Eq)]
pub struct ParkedEntry {
    pub path: String,
    pub parked_at_ms: i64,
    pub reason: String,
}

/// On-disk shape.
#[derive(Debug, Serialize, Deserialize, Default)]
struct Persisted {
    #[serde(default = "parked_version_default")]
    version: u32,
    #[serde(default)]
    entries: Vec<ParkedEntry>,
}

/// Zone-scoped parked queue.
pub struct ParkedQueue {
    dir: PathBuf,
    inner: RwLock<HashMap<String, ParkedEntry>>,
}

impl ParkedQueue {
    /// Load the sidecar at `<zone_root>/parked.json` if it exists;
    /// otherwise start empty.
    pub fn open_or_create(zone_root: PathBuf) -> Result<Self, ParkedError> {
        std::fs::create_dir_all(&zone_root)
            .map_err(|e| ParkedError::CreateDir(zone_root.display().to_string(), e.to_string()))?;
        let path = zone_root.join(PARKED_FILE);
        let mut map: HashMap<String, ParkedEntry> = HashMap::new();
        if path.exists() {
            let bytes = std::fs::read(&path)
                .map_err(|e| ParkedError::Read(path.display().to_string(), e.to_string()))?;
            let persisted: Persisted = serde_json::from_slice(&bytes)
                .map_err(|e| ParkedError::Parse(path.display().to_string(), e.to_string()))?;
            if persisted.version != PARKED_VERSION {
                return Err(ParkedError::Parse(
                    path.display().to_string(),
                    format!(
                        "parked version {} != expected {}",
                        persisted.version, PARKED_VERSION
                    ),
                ));
            }
            for entry in persisted.entries {
                map.insert(entry.path.clone(), entry);
            }
        }
        Ok(Self {
            dir: zone_root,
            inner: RwLock::new(map),
        })
    }

    /// Park a doc.  Overwrites any prior entry for the same path
    /// with the fresh `reason` + `parked_at_ms` — a re-park bumps
    /// the age so operators see current failures at the top.
    pub fn park(&self, path: &str, reason: &str, now_ms: i64) {
        self.inner.write().insert(
            path.to_string(),
            ParkedEntry {
                path: path.to_string(),
                parked_at_ms: now_ms,
                reason: reason.to_string(),
            },
        );
    }

    /// Drop a doc from the queue (used by retry_success + discard).
    /// Returns whether the entry was present.
    pub fn remove(&self, path: &str) -> bool {
        self.inner.write().remove(path).is_some()
    }

    /// Every parked entry, sorted by `parked_at_ms` ascending
    /// (oldest first).  Cheap clone; the queue is small
    /// (single-digit typical).
    pub fn list(&self) -> Vec<ParkedEntry> {
        let mut out: Vec<ParkedEntry> = self.inner.read().values().cloned().collect();
        out.sort_by_key(|e| e.parked_at_ms);
        out
    }

    /// Number of parked entries — used by Stats + operator UI.
    pub fn len(&self) -> usize {
        self.inner.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.read().is_empty()
    }

    /// Persist to disk.  Atomic write-then-rename; same shape as
    /// IndexState.save.
    pub fn save(&self) -> Result<(), ParkedError> {
        let path = self.dir.join(PARKED_FILE);
        let entries: Vec<ParkedEntry> = self.inner.read().values().cloned().collect();
        let persisted = Persisted {
            version: PARKED_VERSION,
            entries,
        };
        let bytes = serde_json::to_vec_pretty(&persisted)
            .map_err(|e| ParkedError::Encode(e.to_string()))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| ParkedError::Write(tmp.display().to_string(), e.to_string()))?;
        std::fs::rename(&tmp, &path)
            .map_err(|e| ParkedError::Write(path.display().to_string(), e.to_string()))?;
        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ParkedError {
    #[error("create parked dir {0}: {1}")]
    CreateDir(String, String),
    #[error("read parked file {0}: {1}")]
    Read(String, String),
    #[error("parse parked file {0}: {1}")]
    Parse(String, String),
    #[error("encode parked: {0}")]
    Encode(String),
    #[error("write parked file {0}: {1}")]
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
        let q = ParkedQueue::open_or_create(tempdir()).expect("open");
        assert_eq!(q.len(), 0);
        assert!(q.is_empty());
    }

    #[test]
    fn park_then_list_returns_entry() {
        let q = ParkedQueue::open_or_create(tempdir()).expect("open");
        q.park("/x.md", "writer full", 1_700_000_000_000);
        let entries = q.list();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].path, "/x.md");
        assert_eq!(entries[0].reason, "writer full");
        assert_eq!(entries[0].parked_at_ms, 1_700_000_000_000);
    }

    #[test]
    fn park_bumps_prior_entry_reason_and_time() {
        // Re-park with a fresher reason overwrites; ensures the
        // operator UI shows the current failure, not the ancient one.
        let q = ParkedQueue::open_or_create(tempdir()).expect("open");
        q.park("/x.md", "writer full", 1);
        q.park("/x.md", "embed failed", 2);
        assert_eq!(q.len(), 1);
        let entries = q.list();
        assert_eq!(entries[0].reason, "embed failed");
        assert_eq!(entries[0].parked_at_ms, 2);
    }

    #[test]
    fn remove_returns_true_only_when_present() {
        let q = ParkedQueue::open_or_create(tempdir()).expect("open");
        q.park("/x.md", "r", 1);
        assert!(q.remove("/x.md"));
        assert!(!q.remove("/x.md"), "second remove is a no-op");
        assert!(!q.remove("/never-parked.md"));
    }

    #[test]
    fn list_sorted_by_parked_at_ascending() {
        let q = ParkedQueue::open_or_create(tempdir()).expect("open");
        q.park("/b.md", "r", 100);
        q.park("/a.md", "r", 50);
        q.park("/c.md", "r", 200);
        let paths: Vec<&str> = q.list().iter().map(|e| e.path.as_str()).collect();
        assert_eq!(paths, vec!["/a.md", "/b.md", "/c.md"]);
    }

    #[test]
    fn save_then_reopen_survives_restart() {
        let dir = tempdir();
        {
            let q = ParkedQueue::open_or_create(dir.clone()).expect("open");
            q.park("/x.md", "r1", 1);
            q.park("/y.md", "r2", 2);
            q.save().expect("save");
        }
        let q2 = ParkedQueue::open_or_create(dir).expect("reopen");
        assert_eq!(q2.len(), 2);
        let entries = q2.list();
        assert_eq!(entries[0].path, "/x.md");
        assert_eq!(entries[1].path, "/y.md");
    }
}
