//! Per-zone indexed-directory registry (Phase 8 of the Python-
//! parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! # What this stores
//!
//! The UI-facing "which directories are we indexing" list.  A
//! per-zone JSON sidecar at `<root>/<zone>/indexed_dirs.json`
//! recording every path an operator added via
//! `AddIndexedDirectory`.  Mirrors the Python `SearchDaemon`
//! surface (`GET /index-dirs`, `POST /index-dirs`,
//! `DELETE /index-dirs/{path}`).
//!
//! # SSOT + persistence posture
//!
//! Per-node derived state — operators drive the registry via the
//! plugin RPC.  Dropping the sidecar loses the "we said to index
//! this" declaration but the FTS + ANN indexes themselves survive;
//! the next operator add re-registers the intent.
//!
//! # Concurrency
//!
//! Same `parking_lot::Mutex<HashMap>` shape as `parked_state` and
//! `index_state`.

use std::collections::HashMap;
use std::path::PathBuf;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

const DIRS_FILE: &str = "indexed_dirs.json";
const DIRS_VERSION: u32 = 1;

fn dirs_version_default() -> u32 {
    DIRS_VERSION
}

#[derive(Debug, Serialize, Deserialize, Clone, PartialEq, Eq)]
pub struct IndexedDirEntry {
    pub path: String,
    pub added_at_ms: i64,
}

#[derive(Debug, Serialize, Deserialize, Default)]
struct Persisted {
    #[serde(default = "dirs_version_default")]
    version: u32,
    #[serde(default)]
    entries: Vec<IndexedDirEntry>,
}

/// Zone-scoped indexed-directory registry.
pub struct IndexedDirsRegistry {
    dir: PathBuf,
    inner: Mutex<HashMap<String, IndexedDirEntry>>,
}

impl IndexedDirsRegistry {
    pub fn open_or_create(zone_root: PathBuf) -> Result<Self, DirsError> {
        std::fs::create_dir_all(&zone_root)
            .map_err(|e| DirsError::CreateDir(zone_root.display().to_string(), e.to_string()))?;
        let path = zone_root.join(DIRS_FILE);
        let mut map: HashMap<String, IndexedDirEntry> = HashMap::new();
        if path.exists() {
            let bytes = std::fs::read(&path)
                .map_err(|e| DirsError::Read(path.display().to_string(), e.to_string()))?;
            let persisted: Persisted = serde_json::from_slice(&bytes)
                .map_err(|e| DirsError::Parse(path.display().to_string(), e.to_string()))?;
            if persisted.version != DIRS_VERSION {
                return Err(DirsError::Parse(
                    path.display().to_string(),
                    format!(
                        "indexed_dirs version {} != expected {}",
                        persisted.version, DIRS_VERSION
                    ),
                ));
            }
            for entry in persisted.entries {
                map.insert(entry.path.clone(), entry);
            }
        }
        Ok(Self {
            dir: zone_root,
            inner: Mutex::new(map),
        })
    }

    /// Add a directory to the registry.  Returns whether this call
    /// was the one that added it (false = already registered).
    /// Idempotent — re-add doesn't bump `added_at_ms`.
    pub fn add(&self, path: &str, now_ms: i64) -> bool {
        let mut inner = self.inner.write();
        if inner.contains_key(path) {
            return false;
        }
        inner.insert(
            path.to_string(),
            IndexedDirEntry {
                path: path.to_string(),
                added_at_ms: now_ms,
            },
        );
        true
    }

    /// Remove a directory from the registry.  Returns whether the
    /// call removed anything (false = wasn't registered).
    pub fn remove(&self, path: &str) -> bool {
        self.inner.write().remove(path).is_some()
    }

    /// List every registered directory, sorted by `added_at_ms`
    /// ascending (chronological — operator UI shows oldest first
    /// so newly-added dirs appear at the bottom).
    pub fn list(&self) -> Vec<IndexedDirEntry> {
        let mut out: Vec<IndexedDirEntry> = self.inner.read().values().cloned().collect();
        out.sort_by_key(|e| e.added_at_ms);
        out
    }

    pub fn len(&self) -> usize {
        self.inner.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.read().is_empty()
    }

    pub fn save(&self) -> Result<(), DirsError> {
        let path = self.dir.join(DIRS_FILE);
        let entries: Vec<IndexedDirEntry> = self.inner.read().values().cloned().collect();
        let persisted = Persisted {
            version: DIRS_VERSION,
            entries,
        };
        let bytes =
            serde_json::to_vec_pretty(&persisted).map_err(|e| DirsError::Encode(e.to_string()))?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, &bytes)
            .map_err(|e| DirsError::Write(tmp.display().to_string(), e.to_string()))?;
        std::fs::rename(&tmp, &path)
            .map_err(|e| DirsError::Write(path.display().to_string(), e.to_string()))?;
        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum DirsError {
    #[error("create dirs dir {0}: {1}")]
    CreateDir(String, String),
    #[error("read indexed_dirs {0}: {1}")]
    Read(String, String),
    #[error("parse indexed_dirs {0}: {1}")]
    Parse(String, String),
    #[error("encode indexed_dirs: {0}")]
    Encode(String),
    #[error("write indexed_dirs {0}: {1}")]
    Write(String, String),
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        tempfile::tempdir().expect("tempdir").keep()
    }

    #[test]
    fn open_or_create_returns_empty() {
        let r = IndexedDirsRegistry::open_or_create(tempdir()).expect("open");
        assert!(r.is_empty());
        assert_eq!(r.len(), 0);
    }

    #[test]
    fn add_is_idempotent_and_reports_new_status() {
        let r = IndexedDirsRegistry::open_or_create(tempdir()).expect("open");
        assert!(r.add("/docs", 100));
        assert!(
            !r.add("/docs", 200),
            "second add on same path should be no-op"
        );
        assert_eq!(r.list()[0].added_at_ms, 100, "re-add must not bump ts");
    }

    #[test]
    fn remove_returns_true_only_when_present() {
        let r = IndexedDirsRegistry::open_or_create(tempdir()).expect("open");
        r.add("/docs", 100);
        assert!(r.remove("/docs"));
        assert!(!r.remove("/docs"));
        assert!(!r.remove("/never-added"));
    }

    #[test]
    fn list_sorted_oldest_first() {
        let r = IndexedDirsRegistry::open_or_create(tempdir()).expect("open");
        r.add("/c", 300);
        r.add("/a", 100);
        r.add("/b", 200);
        let paths: Vec<&str> = r.list().iter().map(|e| e.path.as_str()).collect();
        assert_eq!(paths, vec!["/a", "/b", "/c"]);
    }

    #[test]
    fn save_then_reopen_survives_restart() {
        let dir = tempdir();
        {
            let r = IndexedDirsRegistry::open_or_create(dir.clone()).expect("open");
            r.add("/docs", 100);
            r.add("/notes", 200);
            r.save().expect("save");
        }
        let r2 = IndexedDirsRegistry::open_or_create(dir).expect("reopen");
        assert_eq!(r2.len(), 2);
        assert_eq!(r2.list()[0].path, "/docs");
    }
}
