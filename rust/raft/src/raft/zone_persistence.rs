//! Per-zone on-disk lifecycle with RAII-backed crash safety (R20.13).
//!
//! Disk-dir existence is the authoritative answer to "does this node host
//! zone X?". The `ZoneRaftRegistry.zones` DashMap is a pure live-handle
//! index derived from disk + runtime state — it never claims independent
//! membership authority.
//!
//! Two failure modes this module makes unrepresentable:
//!
//! 1. **Partial create** — `setup_zone` opens redb, creates `ZoneConsensus`,
//!    spawns the transport loop, inserts into the DashMap. Any `?` return
//!    between "dir created" and "DashMap insert" used to leave the zone
//!    dir behind. `ZonePersistence` is armed on `create()` and disarmed on
//!    `commit()`; Drop rolls back the dir while armed.
//!
//! 2. **Incomplete remove** — the previous `remove_zone` only cleared the
//!    in-memory DashMap. The dir stayed on disk and `open_existing_zones_
//!    from_disk` resurrected it as a zombie zone on every restart. The
//!    tombstone (`.removed` marker) is the durable commit point: once it
//!    exists, startup MUST complete the teardown instead of opening.

use std::io;
use std::path::{Path, PathBuf};

const TOMBSTONE_NAME: &str = ".removed";

/// Owns the on-disk dir for a single zone. See module doc.
#[derive(Debug)]
pub struct ZonePersistence {
    zone_path: PathBuf,
    tombstone_path: PathBuf,
    /// When `true`, `Drop` rolls back the zone dir via `remove_dir_all`.
    /// Set by `create()`, cleared by `commit()`.
    armed: bool,
}

impl ZonePersistence {
    /// Create a fresh zone dir. Returns an armed handle — if `Drop` runs
    /// before `commit()`, the dir is rolled back.
    ///
    /// Errors if the dir already exists: a caller trying to `create` an
    /// existing zone indicates a bug (the registry's `creating` guard +
    /// fast-path check should have caught it upstream).
    pub fn create(base: &Path, zone_id: &str) -> io::Result<Self> {
        let zone_path = base.join(zone_id);
        let tombstone_path = zone_path.join(TOMBSTONE_NAME);
        if zone_path.exists() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!(
                    "Zone dir '{}' already exists — caller must use open()",
                    zone_path.display()
                ),
            ));
        }
        std::fs::create_dir_all(&zone_path)?;
        Ok(Self {
            zone_path,
            tombstone_path,
            armed: true,
        })
    }

    /// Open an existing zone dir. The returned handle is not armed — the
    /// dir is already persisted state, Drop must not remove it on
    /// transient errors in the rest of setup.
    pub fn open(base: &Path, zone_id: &str) -> io::Result<Self> {
        let zone_path = base.join(zone_id);
        let tombstone_path = zone_path.join(TOMBSTONE_NAME);
        if !zone_path.exists() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("Zone dir '{}' does not exist", zone_path.display()),
            ));
        }
        Ok(Self {
            zone_path,
            tombstone_path,
            armed: false,
        })
    }

    /// True iff `{zone_id}/.removed` exists. Startup sees this and runs
    /// `cleanup_tombstoned` instead of opening.
    pub fn has_tombstone(base: &Path, zone_id: &str) -> bool {
        base.join(zone_id).join(TOMBSTONE_NAME).exists()
    }

    pub fn raft_path(&self) -> PathBuf {
        self.zone_path.join("raft")
    }

    pub fn sm_path(&self) -> PathBuf {
        self.zone_path.join("sm")
    }

    pub fn zone_path(&self) -> &Path {
        &self.zone_path
    }

    /// Disarm rollback. Call after the zone is fully registered in the
    /// in-memory map. Any error thereafter is a runtime error, not a
    /// setup-rollback scenario.
    pub fn commit(&mut self) {
        self.armed = false;
    }

    /// Write the tombstone. Must be called before tearing down the raft
    /// group — it is the single observable commit point of "this zone is
    /// being removed". Any crash between `write_tombstone` and `destroy`
    /// leaves a tombstoned dir that startup deterministically cleans up.
    ///
    /// Written atomically via `fs::write` on a zero-byte file. A crash
    /// during the write leaves the file missing; caller re-tries from
    /// the beginning of `remove_zone`.
    pub fn write_tombstone(&self) -> io::Result<()> {
        std::fs::write(&self.tombstone_path, b"")?;
        Ok(())
    }

    /// Delete the zone dir. Caller MUST have released all handles to
    /// files inside (redb snapshots, raft storage, etc.) — consumes self
    /// so the type system prevents use-after-destroy.
    pub fn destroy(mut self) -> io::Result<()> {
        self.armed = false; // disarm before the explicit rmdir
        if self.zone_path.exists() {
            std::fs::remove_dir_all(&self.zone_path)?;
        }
        Ok(())
    }

    /// Best-effort cleanup of a zone dir that still has a tombstone after
    /// restart. Called from `open_existing_zones_from_disk`.
    pub fn cleanup_tombstoned(base: &Path, zone_id: &str) -> io::Result<()> {
        let zone_path = base.join(zone_id);
        if zone_path.exists() {
            std::fs::remove_dir_all(&zone_path)?;
        }
        Ok(())
    }
}

impl Drop for ZonePersistence {
    fn drop(&mut self) {
        if self.armed {
            let _ = std::fs::remove_dir_all(&self.zone_path);
            tracing::warn!(
                zone_path = %self.zone_path.display(),
                "ZonePersistence dropped while armed — rolled back partial zone dir",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_create_then_commit_leaves_dir() {
        let tmp = TempDir::new().unwrap();
        {
            let mut p = ZonePersistence::create(tmp.path(), "z1").unwrap();
            p.commit();
        }
        assert!(tmp.path().join("z1").exists());
    }

    #[test]
    fn test_create_without_commit_rolls_back() {
        let tmp = TempDir::new().unwrap();
        {
            let _p = ZonePersistence::create(tmp.path(), "z1").unwrap();
            // drop without commit
        }
        assert!(!tmp.path().join("z1").exists());
    }

    #[test]
    fn test_create_existing_errors() {
        let tmp = TempDir::new().unwrap();
        std::fs::create_dir_all(tmp.path().join("z1")).unwrap();
        let err = ZonePersistence::create(tmp.path(), "z1").unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);
    }

    #[test]
    fn test_open_nonexistent_errors() {
        let tmp = TempDir::new().unwrap();
        let err = ZonePersistence::open(tmp.path(), "z1").unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn test_tombstone_roundtrip() {
        let tmp = TempDir::new().unwrap();
        let mut p = ZonePersistence::create(tmp.path(), "z1").unwrap();
        p.commit();
        assert!(!ZonePersistence::has_tombstone(tmp.path(), "z1"));
        p.write_tombstone().unwrap();
        assert!(ZonePersistence::has_tombstone(tmp.path(), "z1"));
    }

    #[test]
    fn test_destroy_removes_dir() {
        let tmp = TempDir::new().unwrap();
        let mut p = ZonePersistence::create(tmp.path(), "z1").unwrap();
        p.commit();
        // Drop some content inside to exercise recursive remove.
        std::fs::write(tmp.path().join("z1").join("marker"), b"x").unwrap();
        p.destroy().unwrap();
        assert!(!tmp.path().join("z1").exists());
    }

    #[test]
    fn test_cleanup_tombstoned_removes_dir() {
        let tmp = TempDir::new().unwrap();
        let zone_path = tmp.path().join("z1");
        std::fs::create_dir_all(&zone_path).unwrap();
        std::fs::write(zone_path.join(TOMBSTONE_NAME), b"").unwrap();
        std::fs::write(zone_path.join("some-data"), b"x").unwrap();
        ZonePersistence::cleanup_tombstoned(tmp.path(), "z1").unwrap();
        assert!(!zone_path.exists());
    }

    #[test]
    fn test_open_does_not_rollback_on_drop() {
        let tmp = TempDir::new().unwrap();
        std::fs::create_dir_all(tmp.path().join("z1")).unwrap();
        {
            let _p = ZonePersistence::open(tmp.path(), "z1").unwrap();
            // drop — must NOT remove the dir (not armed).
        }
        assert!(tmp.path().join("z1").exists());
    }
}
