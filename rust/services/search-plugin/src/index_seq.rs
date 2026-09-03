//! Plugin-wide monotonic index sequence (#4736).
//!
//! Every COMMITTED index mutation — `Index`, `IndexDocuments`,
//! `Refresh`, `NotifyFileChange{delete}` — advances the counter and
//! stamps the wall clock.  `IndexDocuments` hands the new value back as
//! `index_seq`; `Stats` reports the latest as `last_index_seq` +
//! `last_successful_index_at_ms`.  A caller holding `index_seq = N`
//! treats `last_index_seq >= N` as proof its batch is served: the
//! increment happens strictly AFTER the commit, so the implication holds
//! even when writers for different zones interleave.
//!
//! Persisted at `<plugin-root>/index_seq.json` (atomic write-then-rename,
//! same posture as `index_state.rs`) so the sequence survives plugin-host
//! restarts — a tenant comparing a seq captured before a restart against
//! stats after it must never see the counter rewind.  Persistence is
//! best-effort: a failed save logs a warning and the in-memory value stays
//! authoritative for the process lifetime.  A missing or unparseable file
//! starts the counter at zero rather than refusing to boot — the sequence
//! is a visibility signal, not a source of truth for index contents.

use std::path::{Path, PathBuf};

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

const SEQ_FILE: &str = "index_seq.json";

/// Unique-per-save temp suffix — see `index_state::SAVE_COUNTER` for the
/// torn-snapshot rationale.
static SAVE_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Point-in-time view of the counter.  `at_ms` is ms-since-epoch of the
/// advance that produced `seq`; both zero when nothing was ever indexed.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexSeqSnapshot {
    #[serde(default)]
    pub seq: u64,
    #[serde(default)]
    pub at_ms: u64,
}

/// Process-wide counter with a persisted mirror.
pub struct IndexSeq {
    path: PathBuf,
    inner: Mutex<IndexSeqSnapshot>,
}

impl IndexSeq {
    /// Load `<root>/index_seq.json` when present, else start at zero.
    pub fn open_or_create(root: &Path) -> Self {
        let path = root.join(SEQ_FILE);
        let snapshot = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<IndexSeqSnapshot>(&bytes) {
                Ok(s) => s,
                Err(e) => {
                    tracing::warn!(
                        path = %path.display(),
                        err = %e,
                        "index_seq file unparseable — restarting sequence at 0",
                    );
                    IndexSeqSnapshot::default()
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => IndexSeqSnapshot::default(),
            Err(e) => {
                tracing::warn!(
                    path = %path.display(),
                    err = %e,
                    "index_seq file unreadable — restarting sequence at 0",
                );
                IndexSeqSnapshot::default()
            }
        };
        Self {
            path,
            inner: Mutex::new(snapshot),
        }
    }

    /// Record one committed index mutation.  Returns the new snapshot.
    ///
    /// The mutex is held across the persist so two concurrent advancers
    /// cannot publish the file out of order — the on-disk value is always
    /// the highest seq any caller has been handed (modulo a failed save,
    /// which is logged).
    pub fn advance(&self) -> IndexSeqSnapshot {
        let mut guard = self.inner.lock();
        guard.seq += 1;
        guard.at_ms = now_ms();
        let snap = *guard;
        if let Err(e) = self.persist(&snap) {
            tracing::warn!(
                path = %self.path.display(),
                seq = snap.seq,
                err = %e,
                "index_seq persist failed — sequence stays in-memory only until the next successful save",
            );
        }
        snap
    }

    /// Current value without advancing.
    pub fn snapshot(&self) -> IndexSeqSnapshot {
        *self.inner.lock()
    }

    /// Absolute path of the sidecar file — tests + operator diagnostics.
    pub fn file(&self) -> &Path {
        &self.path
    }

    fn persist(&self, snap: &IndexSeqSnapshot) -> Result<(), String> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("create {}: {e}", parent.display()))?;
        }
        let bytes = serde_json::to_vec(snap).map_err(|e| format!("encode: {e}"))?;
        let tmp = self.path.with_extension(format!(
            "json.tmp.{}.{:x}",
            std::process::id(),
            SAVE_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
        ));
        std::fs::write(&tmp, &bytes).map_err(|e| format!("write {}: {e}", tmp.display()))?;
        std::fs::rename(&tmp, &self.path)
            .map_err(|e| format!("rename {} → {}: {e}", tmp.display(), self.path.display()))?;
        Ok(())
    }
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_root_starts_at_zero() {
        let dir = tempfile::TempDir::new().unwrap();
        let seq = IndexSeq::open_or_create(dir.path());
        assert_eq!(seq.snapshot(), IndexSeqSnapshot::default());
        assert!(!seq.file().exists(), "no file until the first advance");
    }

    #[test]
    fn advance_is_monotonic_and_stamps_clock() {
        let dir = tempfile::TempDir::new().unwrap();
        let seq = IndexSeq::open_or_create(dir.path());
        let a = seq.advance();
        let b = seq.advance();
        assert_eq!(a.seq, 1);
        assert_eq!(b.seq, 2);
        assert!(a.at_ms > 0);
        assert!(b.at_ms >= a.at_ms);
        assert_eq!(seq.snapshot(), b);
    }

    #[test]
    fn reopen_resumes_from_persisted_value() {
        let dir = tempfile::TempDir::new().unwrap();
        {
            let seq = IndexSeq::open_or_create(dir.path());
            seq.advance();
            seq.advance();
            seq.advance();
        }
        let reopened = IndexSeq::open_or_create(dir.path());
        assert_eq!(
            reopened.snapshot().seq,
            3,
            "restart must not rewind the sequence"
        );
        assert_eq!(reopened.advance().seq, 4);
    }

    #[test]
    fn corrupt_file_restarts_at_zero_without_panicking() {
        let dir = tempfile::TempDir::new().unwrap();
        std::fs::write(dir.path().join(SEQ_FILE), b"{not json").unwrap();
        let seq = IndexSeq::open_or_create(dir.path());
        assert_eq!(seq.snapshot().seq, 0);
        assert_eq!(seq.advance().seq, 1);
        // The next save overwrites the corrupt file with a valid one.
        let again = IndexSeq::open_or_create(dir.path());
        assert_eq!(again.snapshot().seq, 1);
    }

    #[test]
    fn nested_root_is_created_on_first_advance() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = dir.path().join("plugins").join("search");
        let seq = IndexSeq::open_or_create(&root);
        seq.advance();
        assert!(seq.file().exists());
    }
}
