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

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
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

    /// True when this mount is an external connector whose reads/writes
    /// must be handled by Python (no Rust fast path available).
    pub is_external: bool,
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
            is_external: false,
        }
    }

    /// Builder-style external-flag setter.
    pub fn with_is_external(mut self, is_external: bool) -> Self {
        self.is_external = is_external;
        self
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
/// `mount_point` carries the **zone-canonical key** (`/{zone_id}{user_path}`),
/// which is the same form `MountTable` is keyed by. Pass it straight into
/// `MountTable::{read_content, write_content, get_canonical, …}` without
/// re-canonicalizing. Historical name inherited from the pre-migration
/// `router::RustRouteResult`.
#[derive(Debug, Clone)]
pub struct RouteResult {
    /// Zone-canonical key (`/{zone_id}{user_mount_point}`).
    pub mount_point: String,
    /// Path relative to the mount root (no leading slash).
    pub backend_path: String,
    pub readonly: bool,
    pub io_profile: String,
    /// True when the routed mount is an external connector — Python must
    /// dispatch the operation through a Python-side backend adapter.
    pub is_external: bool,
}

/// Legacy alias so kernel/generated code using the pre-migration type name
/// compiles unchanged. Drop once C8 lands and all callers use `RouteResult`.
pub type RustRouteResult = RouteResult;

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
    ///
    /// If an entry already exists under the same canonical key and the
    /// *new* entry has no metastore wired, the previous entry's metastore
    /// is preserved. This makes the `install_metastore` → `add_mount`
    /// ordering (used by federation when `attach_raft_zone_to_kernel`
    /// runs before the root DLC mount) insensitive so the ZoneMetastore
    /// isn't wiped when the backend mount registers afterwards.
    pub fn add(&self, mount_point: &str, zone_id: &str, mut entry: MountEntry) {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        if entry.metastore.is_none() {
            if let Some(existing) = self.entries.get(&canonical) {
                if let Some(ms) = existing.metastore.as_ref() {
                    entry.metastore = Some(Arc::clone(ms));
                }
            }
        }
        self.entries.insert(canonical, entry);
    }

    /// Convenience: build a `MountEntry` from flat args and insert it.
    /// Used by `Kernel::add_mount` so callers don't have to import
    /// `MountEntry` just to register a mount.
    #[allow(clippy::too_many_arguments)]
    pub fn add_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
        backend_name: &str,
        backend: Option<Box<dyn ObjectStore>>,
        is_external: bool,
    ) {
        self.add(
            mount_point,
            zone_id,
            MountEntry::new(backend, readonly, admin_only, io_profile, backend_name)
                .with_is_external(is_external),
        );
    }

    /// Remove a mount. Returns `true` if it existed.
    pub fn remove(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.remove(&canonical).is_some()
    }

    /// Replace (or set) the per-mount metastore on an entry.
    ///
    /// Upsert semantics: if no entry exists under ``canonical_key`` yet,
    /// a bare placeholder entry (no backend) is created and tagged with
    /// the metastore. This lets federation bootstrap attach a
    /// ``ZoneMetastore`` at ``/`` before the root DLC mount registers its
    /// backend — when the backend mount arrives later, ``add`` preserves
    /// the already-installed metastore.
    pub fn install_metastore(&self, canonical_key: &str, metastore: Arc<dyn Metastore>) {
        if let Some(mut entry) = self.entries.get_mut(canonical_key) {
            entry.metastore = Some(metastore);
            return;
        }
        let mut entry = MountEntry::new(None, false, false, "balanced", "federation");
        entry.metastore = Some(metastore);
        self.entries.insert(canonical_key.to_string(), entry);
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

    /// Borrow every entry mutably. Used by ``Kernel::release_metastores``
    /// (Issue #3765 Cat-5/6) to drop per-mount ``Arc<dyn Metastore>`` so the
    /// underlying redb file handles are released on kernel close.
    pub fn entries_iter_mut(
        &self,
    ) -> impl Iterator<Item = dashmap::mapref::multiple::RefMutMulti<'_, String, MountEntry>> {
        self.entries.iter_mut()
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

    /// User-facing mount points whose per-mount metastore reports the
    /// given ``coherence_key`` (R20.6 option B).
    ///
    /// Prior impl (``mount_points_for_metastore``) keyed by
    /// ``Arc::ptr_eq`` on ``Arc<dyn Metastore>``. R20.3 gave every
    /// crosslink its own ``ZoneMetastore`` allocation so Arc identity
    /// no longer groups crosslinks of the same zone — each zone needs
    /// a storage-level identity that survives per-mount wrapping.
    /// ``Metastore::coherence_key`` exposes that identity (stable
    /// integer; state-machine Arc pointer for raft-backed zones,
    /// ``None`` for standalone ``LocalMetastore``).
    ///
    /// Kernel stays federation-agnostic — ``coherence_key`` is just an
    /// opaque ``usize``; the kernel never learns "zone id" or any other
    /// federation concept. Apply-side cache coherence fans out through
    /// this primitive: federation passes the state-machine identity,
    /// kernel returns every surface currently bound to it.
    pub fn mount_points_for_coherence_key(&self, key: usize) -> Vec<String> {
        let mut points: Vec<String> = self
            .entries
            .iter()
            .filter_map(|e| {
                e.value().metastore.as_ref().and_then(|existing| {
                    (existing.coherence_key() == Some(key))
                        .then(|| extract_zone_from_canonical(e.key()).1)
                })
            })
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

                let mount_point = current.to_string();
                let backend_path = strip_mount_prefix(&canonical, current);
                let readonly = entry.readonly;
                let io_profile = entry.io_profile.clone();
                let is_external = entry.is_external;
                drop(entry);

                return Ok(RouteResult {
                    mount_point,
                    backend_path,
                    readonly,
                    io_profile,
                    is_external,
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

    // ── Backend-operation delegation ───────────────────────────────────
    //
    // Thin wrappers that look up a mount by canonical key and call the
    // matching `ObjectStore` method. Kept on `MountTable` (not `Kernel`)
    // so the lookup + call live in one place and the `dashmap::Ref` is
    // held for the shortest possible window. A mount without a backend
    // returns `None` — callers treat this as "no Rust-side backend, fall
    // back to Python connector or cold-path dcache lookup".

    /// Read content from the mount's backend.
    pub fn read_content(
        &self,
        canonical_key: &str,
        content_id: &str,
        backend_path: &str,
        ctx: &OperationContext,
    ) -> Option<Vec<u8>> {
        let entry = self.entries.get(canonical_key)?;
        entry
            .backend
            .as_ref()?
            .read_content(content_id, backend_path, ctx)
            .ok()
    }

    /// Write content to the mount's backend.
    pub fn write_content(
        &self,
        canonical_key: &str,
        content: &[u8],
        content_id: &str,
        ctx: &OperationContext,
    ) -> Result<Option<WriteResult>, StorageError> {
        let Some(entry) = self.entries.get(canonical_key) else {
            // Mount not found — caller treats as a hit=false miss.
            return Ok(None);
        };
        let Some(backend) = entry.backend.as_ref() else {
            return Ok(None);
        };
        backend.write_content(content, content_id, ctx).map(Some)
    }

    /// Delete a file via the mount's backend.
    pub fn delete_file(&self, canonical_key: &str, backend_path: &str) -> Option<()> {
        let entry = self.entries.get(canonical_key)?;
        entry.backend.as_ref()?.delete_file(backend_path).ok()
    }

    /// Rename a file via the mount's backend.
    pub fn rename_file(
        &self,
        canonical_key: &str,
        old_backend_path: &str,
        new_backend_path: &str,
    ) -> Option<()> {
        let entry = self.entries.get(canonical_key)?;
        entry
            .backend
            .as_ref()?
            .rename(old_backend_path, new_backend_path)
            .ok()
    }

    /// Copy a file via the mount's backend (PAS server-side copy).
    pub fn copy_file(
        &self,
        canonical_key: &str,
        src_backend_path: &str,
        dst_backend_path: &str,
    ) -> Option<crate::backend::WriteResult> {
        let entry = self.entries.get(canonical_key)?;
        entry
            .backend
            .as_ref()?
            .copy_file(src_backend_path, dst_backend_path)
            .ok()
    }

    /// Create a directory via the mount's backend.
    pub fn mkdir(
        &self,
        canonical_key: &str,
        backend_path: &str,
        parents: bool,
        exist_ok: bool,
    ) -> Option<()> {
        let entry = self.entries.get(canonical_key)?;
        entry
            .backend
            .as_ref()?
            .mkdir(backend_path, parents, exist_ok)
            .ok()
    }

    /// Remove a directory via the mount's backend.
    pub fn rmdir(&self, canonical_key: &str, backend_path: &str, recursive: bool) -> Option<()> {
        let entry = self.entries.get(canonical_key)?;
        entry.backend.as_ref()?.rmdir(backend_path, recursive).ok()
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

/// Convert a zone-relative path back to a global (user-facing) path using
/// the zone-canonical mount point.
///
/// Inverse of the zone-key transformation performed by
/// [`Kernel::zone_key`](crate::kernel::Kernel::zone_key): given the
/// canonical mount point and a zone-relative metastore key, reconstruct
/// the global path a user would pass to a syscall.
///
/// Examples:
/// - `("/root/corp", "/eng/foo.txt")` → `"/corp/eng/foo.txt"`
/// - `("/root", "/workspace/file.txt")` → `"/workspace/file.txt"`
/// - `("", "/workspace/file.txt")` → `"/workspace/file.txt"` (no-mount fallback)
pub fn zone_to_global(mount_point: &str, zone_path: &str) -> String {
    if mount_point.is_empty() {
        return zone_path.to_string();
    }
    let (_, user_mp) = extract_zone_from_canonical(mount_point);
    if user_mp == "/" {
        zone_path.to_string()
    } else {
        format!("{}{}", user_mp, zone_path)
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
        assert_eq!(r.mount_point, "/root/workspace");
        assert_eq!(r.backend_path, "file.txt");
        assert_eq!(r.io_profile, "fast");
    }

    #[test]
    fn test_route_falls_back_to_root() {
        let table = MountTable::new();
        table.add("/", "root", entry(false, false, "balanced"));

        let r = table.route("/unknown/path", "root", false, false).unwrap();
        assert_eq!(r.mount_point, "/root");
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
        assert_eq!(r.mount_point, "/root/admin");
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
        assert_eq!(r.mount_point, "/root");

        // zone-beta sees its own mount
        let r = table
            .route("/shared/doc.txt", "zone-beta", false, false)
            .unwrap();
        assert_eq!(r.mount_point, "/zone-beta/shared");
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

    // ── zone_to_global tests ─────────────────────────────────────────

    #[test]
    fn zone_to_global_root_mount() {
        // Root zone at "/" → zone-relative = global (no-op)
        assert_eq!(
            zone_to_global("/root", "/workspace/file.txt"),
            "/workspace/file.txt"
        );
        assert_eq!(zone_to_global("/root", "/"), "/");
    }

    #[test]
    fn zone_to_global_non_root_mount() {
        // Mount at "/corp" in root zone → zone-relative "/eng/foo.txt" → global "/corp/eng/foo.txt"
        assert_eq!(
            zone_to_global("/root/corp", "/eng/foo.txt"),
            "/corp/eng/foo.txt"
        );
        assert_eq!(zone_to_global("/root/corp", "/"), "/corp/");
    }

    #[test]
    fn zone_to_global_nested_mount() {
        // Nested mount at "/corp/eng" → zone-relative "/readme.md" → global "/corp/eng/readme.md"
        assert_eq!(
            zone_to_global("/root/corp/eng", "/readme.md"),
            "/corp/eng/readme.md"
        );
    }

    #[test]
    fn zone_to_global_empty_mount_fallback() {
        // No-mount fallback (empty mount_point) → pass-through
        assert_eq!(
            zone_to_global("", "/workspace/file.txt"),
            "/workspace/file.txt"
        );
    }

    #[test]
    fn zone_to_global_round_trip() {
        // canonicalize → route → zone_key → zone_to_global should recover original
        let table = MountTable::new();
        table.add("/corp", "root", entry(false, false, "balanced"));

        let global = "/corp/eng/foo.txt";
        let route = table.route(global, "root", true, false).unwrap();
        let zone_path = if route.backend_path.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", route.backend_path)
        };
        let recovered = zone_to_global(&route.mount_point, &zone_path);
        assert_eq!(recovered, global);
    }

    /// Federation topology: two DISTINCT ``ZoneMetastore`` Arcs (with
    /// different ``mount_point``s) can back the same zone's state
    /// machine — they share the same ``coherence_key``. The reverse
    /// lookup must return every surface with that key, and must NOT
    /// match a metastore with a different key (or ``None`` — single-node).
    #[test]
    fn mount_points_for_coherence_key_finds_direct_and_crosslinks() {
        /// Test stub — reports a caller-configured coherence key.
        /// R20.6 option B keys on ``usize``, not ``Arc`` identity, so
        /// two distinct Arcs that report the same key represent two
        /// surfaces of the same underlying storage.
        struct KeyedStub {
            key: Option<usize>,
        }
        impl crate::metastore::Metastore for KeyedStub {
            fn get(
                &self,
                _: &str,
            ) -> Result<Option<crate::metastore::FileMetadata>, crate::metastore::MetastoreError>
            {
                Ok(None)
            }
            fn put(
                &self,
                _: &str,
                _: crate::metastore::FileMetadata,
            ) -> Result<(), crate::metastore::MetastoreError> {
                Ok(())
            }
            fn delete(&self, _: &str) -> Result<bool, crate::metastore::MetastoreError> {
                Ok(false)
            }
            fn list(
                &self,
                _: &str,
            ) -> Result<Vec<crate::metastore::FileMetadata>, crate::metastore::MetastoreError>
            {
                Ok(Vec::new())
            }
            fn exists(&self, _: &str) -> Result<bool, crate::metastore::MetastoreError> {
                Ok(false)
            }
            fn coherence_key(&self) -> Option<usize> {
                self.key
            }
        }

        const CORP_KEY: usize = 0xC0;
        const FAMILY_KEY: usize = 0xFA;

        let corp_a: Arc<dyn Metastore> = Arc::new(KeyedStub {
            key: Some(CORP_KEY),
        });
        let corp_b: Arc<dyn Metastore> = Arc::new(KeyedStub {
            key: Some(CORP_KEY),
        }); // DISTINCT Arc, same coherence key — crosslink of the same zone.
        let family: Arc<dyn Metastore> = Arc::new(KeyedStub {
            key: Some(FAMILY_KEY),
        });

        let table = MountTable::new();
        table.add(
            "/corp",
            "root",
            MountEntry::new(None, false, false, "balanced", "backend-corp").with_metastore(corp_a),
        );
        table.add(
            "/family/work",
            "root",
            MountEntry::new(None, false, false, "balanced", "backend-corp-xlink")
                .with_metastore(corp_b),
        );
        table.add(
            "/family",
            "root",
            MountEntry::new(None, false, false, "balanced", "backend-family")
                .with_metastore(family),
        );

        let mut corp_points = table.mount_points_for_coherence_key(CORP_KEY);
        corp_points.sort();
        assert_eq!(corp_points, vec!["/corp", "/family/work"]);

        let family_points = table.mount_points_for_coherence_key(FAMILY_KEY);
        assert_eq!(family_points, vec!["/family"]);

        // Unknown key → empty.
        assert!(table.mount_points_for_coherence_key(0xDEAD).is_empty());
    }
}
