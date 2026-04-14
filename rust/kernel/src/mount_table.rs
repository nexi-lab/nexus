//! Kernel mount table — single SSOT for mount entries (Rust port).
//!
//! Mirrors Python `nexus.core.mount_table` (the canonical kernel-namespace
//! placement for this data type). Each `MountEntry` is the in-memory record
//! for one mount: storage backend, optional per-mount metastore, mount-level
//! access flags, and IO profile. Together they form `MountTable`, an LPM-
//! routable container keyed by zone-canonical paths.
//!
//! Replaces the older split where:
//!   - `rust/kernel/src/router.rs::PathRouter` owned a `HashMap<String,
//!     MountEntry>` of backends-only
//!   - `rust/kernel/src/kernel.rs::Kernel::mount_metastores` was a *separate*
//!     `DashMap<String, Arc<dyn Metastore>>` keyed by the same canonical paths
//!
//! Both halves now live in `MountEntry`, so callers no longer have to keep
//! the two maps consistent. The Python side's `MountTable._entries` Python-
//! ref shadow cache disappears as `_write_content` and friends migrate to
//! call `kernel.sys_*` directly (no need to hold Python backend/metastore
//! refs anymore).
//!
//! Concurrency: `DashMap` for lock-free reads on the syscall hot path. Add/
//! remove are rare (mount-lifecycle events) so the per-shard write lock is
//! invisible in practice.

use dashmap::DashMap;
use std::sync::Arc;

use crate::backend::ObjectStore;
use crate::metastore::Metastore;

// ---------------------------------------------------------------------------
// MountEntry — runtime record for a single mount
// ---------------------------------------------------------------------------

/// Per-mount runtime record.
///
/// `backend` is `Box` because backends are moved into the table at mount
/// time and never need to be shared with external code.
///
/// `metastore` is `Arc` because the same metastore instance may be handed
/// in from a separate crate (e.g. `rust/raft::ZoneMetastore`) via
/// `install_metastore`, and that crate keeps its own `Arc` reference to
/// the underlying state machine. Shared ownership is required.
pub struct MountEntry {
    /// Storage backend (CAS local, S3, OpenAI, gRPC remote, …).
    /// `None` means "no Rust backend available" — sys_read/sys_write fall
    /// back to caller-side handling (e.g. Python connector).
    pub backend: Option<Box<dyn ObjectStore>>,

    /// Per-mount metastore for metadata operations. `None` means use the
    /// kernel's global `Kernel::metastore` instead. Federation mode wires a
    /// `ZoneMetastore` here per zone.
    pub metastore: Option<Arc<dyn Metastore>>,

    /// Mount-level access control: read-only mount.
    pub readonly: bool,

    /// Mount-level access control: admin-only mount (e.g. `/__sys__`).
    pub admin_only: bool,

    /// IO tuning profile (`balanced`, `latency`, `throughput`, …).
    pub io_profile: String,

    /// Cosmetic name reported by introspection / logs.
    pub backend_name: String,
}

impl MountEntry {
    /// Construct a new entry. `metastore` is typically `None` at mount time
    /// and installed later via `MountTable::install_metastore` (federation),
    /// or set up-front via `with_metastore` (standalone redb).
    pub fn new(
        backend: Option<Box<dyn ObjectStore>>,
        readonly: bool,
        admin_only: bool,
        io_profile: impl Into<String>,
        backend_name: impl Into<String>,
    ) -> Self {
        Self {
            backend,
            metastore: None,
            readonly,
            admin_only,
            io_profile: io_profile.into(),
            backend_name: backend_name.into(),
        }
    }

    /// Builder-style metastore setter. Used when the metastore is known at
    /// mount-creation time (standalone redb path).
    pub fn with_metastore(mut self, ms: Arc<dyn Metastore>) -> Self {
        self.metastore = Some(ms);
        self
    }
}

// ---------------------------------------------------------------------------
// RouteError — failures during LPM routing
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub enum RouteError {
    /// No mount entry covers this path.
    NotMounted(String),
    /// Mount-level access control rejected the request.
    AccessDenied(String),
}

// ---------------------------------------------------------------------------
// RouteResult — returned by MountTable::route
// ---------------------------------------------------------------------------

/// Result of a successful LPM route lookup.
///
/// The caller can use `canonical_key` to fetch the full `MountEntry`
/// (backend + metastore) via `MountTable::get_canonical`. Most callers only
/// need the routing-decision fields below.
#[derive(Debug, Clone)]
pub struct RouteResult {
    /// Zone-canonical key (`/{zone_id}{mount_point}`) — for direct lookup.
    pub canonical_key: String,
    /// User-facing mount point with the zone prefix stripped.
    pub mount_point: String,
    /// Path relative to the mount root (no leading slash).
    pub backend_path: String,
    pub readonly: bool,
    pub io_profile: String,
}

// ---------------------------------------------------------------------------
// MountTable — kernel-owned mount registry
// ---------------------------------------------------------------------------

pub struct MountTable {
    entries: DashMap<String, MountEntry>,
}

impl Default for MountTable {
    fn default() -> Self {
        Self::new()
    }
}

impl MountTable {
    pub fn new() -> Self {
        Self {
            entries: DashMap::new(),
        }
    }

    // ── Write ops (called by DLC.mount/unmount) ────────────────────────

    /// Insert a mount entry under its zone-canonical key.
    pub fn add(&self, mount_point: &str, zone_id: &str, entry: MountEntry) {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.insert(canonical, entry);
    }

    /// Remove a mount. Returns `true` if it existed.
    pub fn remove(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.remove(&canonical).is_some()
    }

    /// Replace (or set) the per-mount metastore on an existing entry.
    ///
    /// Used by `rust/raft::PyZoneHandle::attach_to_kernel_mount` after the
    /// federation mount is registered, to install a `ZoneMetastore` backed
    /// by Raft consensus on this mount. No-op if the canonical key isn't
    /// present (caller should ensure ordering: add first, then install).
    pub fn install_metastore(&self, canonical_key: &str, metastore: Arc<dyn Metastore>) {
        if let Some(mut entry) = self.entries.get_mut(canonical_key) {
            entry.metastore = Some(metastore);
        }
    }

    // ── Read ops ───────────────────────────────────────────────────────
    //
    // Returning `dashmap::mapref::one::Ref` (lifetime-tied to the table)
    // keeps the syscall hot path zero-allocation. Callers use the guard
    // immediately and let it drop.

    /// Borrow the entry under an exact canonical key.
    pub fn get_canonical(
        &self,
        canonical_key: &str,
    ) -> Option<dashmap::mapref::one::Ref<'_, String, MountEntry>> {
        self.entries.get(canonical_key)
    }

    /// Borrow the entry for `(mount_point, zone_id)`.
    pub fn get(
        &self,
        mount_point: &str,
        zone_id: &str,
    ) -> Option<dashmap::mapref::one::Ref<'_, String, MountEntry>> {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.get(&canonical)
    }

    /// True if a mount exists under `(mount_point, zone_id)`.
    pub fn has(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.contains_key(&canonical)
    }

    /// All registered canonical keys (sorted). Cheap copy — mounts are rare.
    pub fn canonical_keys(&self) -> Vec<String> {
        let mut keys: Vec<String> = self.entries.iter().map(|e| e.key().clone()).collect();
        keys.sort();
        keys
    }

    /// All user-facing mount points (zone prefix stripped, sorted).
    pub fn mount_points(&self) -> Vec<String> {
        let mut points: Vec<String> = self
            .entries
            .iter()
            .map(|e| extract_zone_from_canonical(e.key()).1)
            .collect();
        points.sort();
        points
    }

    /// Number of mounted entries.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// True if the table has no entries.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    // ── LPM routing ────────────────────────────────────────────────────

    /// Longest-prefix-match routing within a zone.
    ///
    /// Walks zone-canonical mount keys from deepest to shallowest until one
    /// is found. Enforces mount-level access control (`admin_only`,
    /// `readonly`).
    pub fn route(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> Result<RouteResult, RouteError> {
        let canonical = canonicalize_mount_path(path, zone_id);
        let mut current = canonical.as_str();

        loop {
            if let Some(entry) = self.entries.get(current) {
                if entry.admin_only && !is_admin {
                    return Err(RouteError::AccessDenied(format!(
                        "Mount '{}' requires admin privileges",
                        current
                    )));
                }
                if entry.readonly && check_write {
                    return Err(RouteError::AccessDenied(format!(
                        "Mount '{}' is read-only",
                        current
                    )));
                }

                let canonical_key = current.to_string();
                let mount_point = extract_zone_from_canonical(current).1;
                let backend_path = strip_mount_prefix(&canonical, current);
                let readonly = entry.readonly;
                let io_profile = entry.io_profile.clone();
                drop(entry);

                return Ok(RouteResult {
                    canonical_key,
                    mount_point,
                    backend_path,
                    readonly,
                    io_profile,
                });
            }

            if current == "/" {
                break;
            }
            match current.rfind('/') {
                Some(0) => current = "/",
                Some(pos) => current = &canonical[..pos],
                None => break,
            }
        }

        Err(RouteError::NotMounted(format!(
            "No mount found for path: {}",
            path
        )))
    }
}

// ---------------------------------------------------------------------------
// Path helpers — kernel-public so external crates (e.g. `rust/raft`) can
// produce keys consistent with the table.
// ---------------------------------------------------------------------------

/// Build the zone-canonical key `/{zone_id}{mount_point}`.
///
/// Examples:
/// - `("/workspace/file.txt", "root")` → `"/root/workspace/file.txt"`
/// - `("/", "zone-beta")` → `"/zone-beta"`
pub fn canonicalize_mount_path(path: &str, zone_id: &str) -> String {
    let stripped = path.trim_start_matches('/');
    if stripped.is_empty() {
        format!("/{}", zone_id)
    } else {
        format!("/{}/{}", zone_id, stripped)
    }
}

/// Inverse of [`canonicalize_mount_path`]: split a canonical key back into
/// `(zone_id, mount_point)`.
///
/// Examples:
/// - `"/root/workspace/file.txt"` → `("root", "/workspace/file.txt")`
/// - `"/zone-beta"` → `("zone-beta", "/")`
pub fn extract_zone_from_canonical(canonical: &str) -> (String, String) {
    let trimmed = canonical.trim_start_matches('/');
    match trimmed.split_once('/') {
        Some((zone, rest)) => (zone.to_string(), format!("/{}", rest)),
        None => (trimmed.to_string(), "/".to_string()),
    }
}

/// Strip a mount-point prefix from a canonical path to get the
/// backend-relative path (without leading slash).
fn strip_mount_prefix(path: &str, mount_point: &str) -> String {
    if path == mount_point {
        String::new()
    } else if mount_point == "/" {
        path.trim_start_matches('/').to_string()
    } else {
        path[mount_point.len()..]
            .trim_start_matches('/')
            .to_string()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(readonly: bool, admin_only: bool, io_profile: &str) -> MountEntry {
        MountEntry::new(None, readonly, admin_only, io_profile, "test")
    }

    #[test]
    fn test_canonicalize_mount_path() {
        assert_eq!(
            canonicalize_mount_path("/workspace/file.txt", "root"),
            "/root/workspace/file.txt"
        );
        assert_eq!(canonicalize_mount_path("/", "root"), "/root");
        assert_eq!(canonicalize_mount_path("/a/b/c", "zone-1"), "/zone-1/a/b/c");
    }

    #[test]
    fn test_extract_zone_from_canonical() {
        assert_eq!(
            extract_zone_from_canonical("/root/workspace/file.txt"),
            ("root".into(), "/workspace/file.txt".into())
        );
        assert_eq!(
            extract_zone_from_canonical("/root"),
            ("root".into(), "/".into())
        );
        assert_eq!(
            extract_zone_from_canonical("/zone-1/a/b"),
            ("zone-1".into(), "/a/b".into())
        );
    }

    #[test]
    fn test_strip_mount_prefix() {
        assert_eq!(
            strip_mount_prefix("/root/workspace/data/file.txt", "/root/workspace"),
            "data/file.txt"
        );
        assert_eq!(strip_mount_prefix("/root/workspace", "/root/workspace"), "");
        assert_eq!(strip_mount_prefix("/root/a/b", "/root"), "a/b");
    }

    #[test]
    fn test_basic_route() {
        let table = MountTable::new();
        table.add("/", "root", entry(false, false, "balanced"));
        table.add("/workspace", "root", entry(false, false, "fast"));

        let r = table
            .route("/workspace/file.txt", "root", false, false)
            .unwrap();
        assert_eq!(r.canonical_key, "/root/workspace");
        assert_eq!(r.mount_point, "/workspace");
        assert_eq!(r.backend_path, "file.txt");
        assert_eq!(r.io_profile, "fast");
    }

    #[test]
    fn test_route_falls_back_to_root() {
        let table = MountTable::new();
        table.add("/", "root", entry(false, false, "balanced"));

        let r = table.route("/unknown/path", "root", false, false).unwrap();
        assert_eq!(r.mount_point, "/");
        assert_eq!(r.backend_path, "unknown/path");
    }

    #[test]
    fn test_route_readonly_blocks_writes() {
        let table = MountTable::new();
        table.add("/system", "root", entry(true, false, "balanced"));

        let err = table
            .route("/system/config", "root", false, true)
            .unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));
    }

    #[test]
    fn test_route_admin_only() {
        let table = MountTable::new();
        table.add("/admin", "root", entry(false, true, "balanced"));

        let err = table
            .route("/admin/secrets", "root", false, false)
            .unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));

        let r = table.route("/admin/secrets", "root", true, false).unwrap();
        assert_eq!(r.canonical_key, "/root/admin");
    }

    #[test]
    fn test_cross_zone_isolation() {
        let table = MountTable::new();
        table.add("/", "root", entry(false, false, "balanced"));
        table.add("/shared", "zone-beta", entry(false, false, "balanced"));

        // root zone falls back to root mount
        let r = table
            .route("/workspace/file.txt", "root", false, false)
            .unwrap();
        assert_eq!(r.canonical_key, "/root");

        // zone-beta sees its own mount
        let r = table
            .route("/shared/doc.txt", "zone-beta", false, false)
            .unwrap();
        assert_eq!(r.canonical_key, "/zone-beta/shared");
    }

    #[test]
    fn test_install_metastore_late() {
        use crate::metastore::{FileMetadata, MetastoreError};

        // Trivial in-memory Metastore impl for the test.
        struct DummyMs;
        impl Metastore for DummyMs {
            fn get(&self, _: &str) -> Result<Option<FileMetadata>, MetastoreError> {
                Ok(None)
            }
            fn put(&self, _: &str, _: FileMetadata) -> Result<(), MetastoreError> {
                Ok(())
            }
            fn delete(&self, _: &str) -> Result<bool, MetastoreError> {
                Ok(false)
            }
            fn list(&self, _: &str) -> Result<Vec<FileMetadata>, MetastoreError> {
                Ok(vec![])
            }
            fn exists(&self, _: &str) -> Result<bool, MetastoreError> {
                Ok(false)
            }
        }

        let table = MountTable::new();
        table.add("/data", "root", entry(false, false, "balanced"));
        let canonical = canonicalize_mount_path("/data", "root");

        // Initially no metastore.
        assert!(table.get_canonical(&canonical).unwrap().metastore.is_none());

        table.install_metastore(&canonical, Arc::new(DummyMs));
        assert!(table.get_canonical(&canonical).unwrap().metastore.is_some());
    }

    #[test]
    fn test_mount_management() {
        let table = MountTable::new();
        table.add("/data", "root", entry(false, false, "balanced"));
        assert!(table.has("/data", "root"));
        assert!(!table.has("/data", "other"));

        assert_eq!(table.canonical_keys(), vec!["/root/data"]);

        assert!(table.remove("/data", "root"));
        assert!(!table.has("/data", "root"));
    }
}
