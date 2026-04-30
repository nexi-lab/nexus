//! Kernel mount table — single SSOT for mount entries (Rust port).
//!
//! Mirrors Python `nexus.core.vfs_router` (the canonical kernel-namespace
//! placement for this data type). Each `MountEntry` is the in-memory record
//! for one mount: storage backend and optional per-mount metastore.
//! Together they form `VFSRouter`, an LPM-routable container keyed by
//! zone-canonical paths. Access control lives one layer up (rebac); the
//! mount table is pure routing.
//!
//! Replaces the older split where:
//!   - `rust/kernel/src/router.rs::PathRouter` owned a `HashMap<String,
//!     MountEntry>` of backends-only
//!   - `rust/kernel/src/kernel.rs::Kernel::mount_metastores` was a *separate*
//!     `DashMap<String, Arc<dyn MetaStore>>` keyed by the same canonical paths
//!
//! Both halves now live in `MountEntry`, so callers no longer have to keep
//! the two maps consistent. The Python side's `VFSRouter._entries` Python-
//! ref shadow cache disappears as `_write_content` and friends migrate to
//! call `kernel.sys_*` directly (no need to hold Python backend/metastore
//! refs anymore).
//!
//! Concurrency: `DashMap` for lock-free reads on the syscall hot path. Add/
//! remove are rare (mount-lifecycle events) so the per-shard write lock is
//! invisible in practice.

use dashmap::DashMap;
use std::sync::Arc;

use crate::abc::object_store::{ExternalTransport, ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use crate::meta_store::MetaStore;

// ---------------------------------------------------------------------------
// MountEntry — runtime record for a single mount
// ---------------------------------------------------------------------------

/// Per-mount runtime record.
///
/// `backend` is `Arc` so the root-zone mount's backend can be cheaply
/// cloned into every federation child-zone mount that reuses it
/// (R20.16). Before that refactor it was `Box`, because backends were
/// never shared; federation now needs shared ownership without
/// re-initialising the backend per child zone.
///
/// `metastore` is `Arc` because the same metastore instance may be handed
/// in from a separate crate (e.g. `rust/raft::ZoneMetaStore`) via
/// `install_metastore`, and that crate keeps its own `Arc` reference to
/// the underlying state machine. Shared ownership is required.
pub struct MountEntry {
    /// Storage backend (CAS local, S3, OpenAI, gRPC remote, …).
    /// `None` means "no Rust backend available" — sys_read/sys_write fall
    /// back to caller-side handling (e.g. Python connector).
    pub backend: Option<Arc<dyn ObjectStore>>,

    /// Per-mount metastore for metadata operations. `None` means use the
    /// kernel's global `Kernel::metastore` instead. Federation mode wires a
    /// `ZoneMetaStore` here per zone.
    pub metastore: Option<Arc<dyn MetaStore>>,

    /// True when this mount is an external connector whose reads/writes
    /// must be handled by Python (no Rust fast path available).
    pub is_external: bool,

    /// For federation mounts: the zone id this mount points INTO.
    /// Populated by `wire_federation_mount_impl`. `None` for plain local
    /// mounts (backend-only, non-federation). Carried through
    /// [`RouteResult::zone_id`] so writes tag inode metadata with the
    /// owning zone rather than the caller's ambient zone, and
    /// `federation_share` can derive `(parent_zone, zone-relative prefix)`
    /// from a global path via the existing routing table.
    pub target_zone_id: Option<String>,

    /// Transport-layer capability for generating direct-access URLs
    /// (presigned/signed). Only cloud backends (S3, GCS) implement this.
    /// Populated at mount time alongside `backend` — same Arc allocation,
    /// separate vtable pointer.
    pub external_transport: Option<Arc<dyn ExternalTransport>>,
}

impl MountEntry {
    /// Construct a new entry. `metastore` is typically `None` at mount time
    /// and installed later via `VFSRouter::install_metastore` (federation),
    /// or set up-front via `with_metastore` (standalone redb).
    pub fn new(backend: Option<Arc<dyn ObjectStore>>) -> Self {
        Self {
            backend,
            metastore: None,
            is_external: false,
            target_zone_id: None,
            external_transport: None,
        }
    }

    /// Builder-style external-transport setter (S3/GCS mounts).
    pub fn with_external_transport(mut self, transport: Arc<dyn ExternalTransport>) -> Self {
        self.external_transport = Some(transport);
        self
    }

    /// Builder-style target-zone setter (federation mounts only).
    pub fn with_target_zone(mut self, target_zone_id: impl Into<String>) -> Self {
        self.target_zone_id = Some(target_zone_id.into());
        self
    }

    /// Builder-style external-flag setter.
    pub fn with_is_external(mut self, is_external: bool) -> Self {
        self.is_external = is_external;
        self
    }

    /// Builder-style metastore setter. Used when the metastore is known at
    /// mount-creation time (standalone redb path).
    pub fn with_metastore(mut self, ms: Arc<dyn MetaStore>) -> Self {
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
}

// ---------------------------------------------------------------------------
// RouteResult — returned by VFSRouter::route
// ---------------------------------------------------------------------------

/// Result of a successful LPM route lookup.
///
/// `mount_point` carries the **zone-canonical key** (`/{zone_id}{user_path}`),
/// which is the same form `VFSRouter` is keyed by. Pass it straight into
/// `VFSRouter::{read_content, write_content, get_canonical, …}` without
/// re-canonicalizing. Historical name inherited from the pre-migration
/// `router::RustRouteResult`.
#[derive(Debug, Clone)]
pub struct RouteResult {
    /// Zone-canonical key (`/{zone_id}{user_mount_point}`).
    pub mount_point: String,
    /// Path relative to the mount root (no leading slash).
    pub backend_path: String,
    /// Destination zone id for the routed mount. Populated from the
    /// canonicalized key's leading segment so metadata writes use the
    /// mount's zone, not the caller's ambient zone.
    pub zone_id: String,
    /// True when the routed mount is an external connector — Python must
    /// dispatch the operation through a Python-side backend adapter.
    pub is_external: bool,
    /// True when the routed backend is content-addressed (CAS).
    ///
    /// Derived from the backend trait's `as_cas()` downcast — single
    /// source of truth, no string-prefix sniffing on a label.
    pub is_cas: bool,
}

/// Legacy alias so kernel/generated code using the pre-migration type name
/// compiles unchanged. Drop once C8 lands and all callers use `RouteResult`.
pub type RustRouteResult = RouteResult;

// ---------------------------------------------------------------------------
// VFSRouter — kernel-owned mount registry
// ---------------------------------------------------------------------------

pub struct VFSRouter {
    entries: DashMap<String, MountEntry>,
}

impl Default for VFSRouter {
    fn default() -> Self {
        Self::new()
    }
}

impl VFSRouter {
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
    /// runs before the root DLC mount) insensitive so the ZoneMetaStore
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
    pub fn add_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        backend: Option<Arc<dyn ObjectStore>>,
        is_external: bool,
    ) {
        self.add(
            mount_point,
            zone_id,
            MountEntry::new(backend).with_is_external(is_external),
        );
    }

    /// Federation variant: install a mount that carries an explicit
    /// `target_zone_id`. Routing through this mount resolves to the
    /// target zone, not the caller's ambient one — so writes tag inode
    /// metadata with the owning zone and `federation_share` can derive
    /// `(parent_zone, zone-relative prefix)` from a global path.
    pub fn add_federation_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        backend: Option<Arc<dyn ObjectStore>>,
        target_zone_id: &str,
        is_external: bool,
    ) {
        self.add(
            mount_point,
            zone_id,
            MountEntry::new(backend)
                .with_is_external(is_external)
                .with_target_zone(target_zone_id),
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
    /// ``ZoneMetaStore`` at ``/`` before the root DLC mount registers its
    /// backend — when the backend mount arrives later, ``add`` preserves
    /// the already-installed metastore.
    pub fn install_metastore(&self, canonical_key: &str, metastore: Arc<dyn MetaStore>) {
        if let Some(mut entry) = self.entries.get_mut(canonical_key) {
            entry.metastore = Some(metastore);
            return;
        }
        let mut entry = MountEntry::new(None);
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

    /// Borrow the entry mutably under an exact canonical key.
    pub fn entries_get_mut(
        &self,
        canonical_key: &str,
    ) -> Option<dashmap::mapref::one::RefMut<'_, String, MountEntry>> {
        self.entries.get_mut(canonical_key)
    }

    /// True if a mount exists under `(mount_point, zone_id)`.
    pub fn has(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize_mount_path(mount_point, zone_id);
        self.entries.contains_key(&canonical)
    }

    /// Borrow every entry mutably. Used by ``Kernel::release_metastores``
    /// (Issue #3765 Cat-5/6) to drop per-mount ``Arc<dyn MetaStore>`` so the
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

    /// Snapshot every non-empty backend registered on the mount table.
    ///
    /// Used by R20.18.7 `KernelBlobFetcher` to resolve `ReadBlob` by
    /// content hash without caring which mount the blob originally
    /// landed on — CAS backends are content-addressed, so any local
    /// mount with the right blob satisfies the request.
    pub fn backends(&self) -> Vec<Arc<dyn ObjectStore>> {
        self.entries
            .iter()
            .filter_map(|e| e.backend.as_ref().map(Arc::clone))
            .collect()
    }

    /// Rebind every federation mount (entries with a metastore but no
    /// backend) to `new_backend`.
    ///
    /// Fixes a boot-order bug: on node restart, `reconcile_mounts_from_zones`
    /// replays DT_MOUNT entries BEFORE Python installs the root mount
    /// (which carries this node's CAS backend). Each federation mount
    /// gets cloned with `backend=None` at replay time, so subsequent
    /// writes return `Ok(None)` (miss) — they look like they succeeded
    /// at the Python layer but never persisted. Calling this from
    /// `Kernel::add_mount` when the root mount arrives wires the
    /// correct backend into every previously-stranded federation mount.
    ///
    /// Only touches entries with `metastore = Some(..)` — that's the
    /// federation-mount marker. Plain local mounts (backend-only, no
    /// metastore) are left untouched.
    pub fn rebind_missing_backends(&self, new_backend: &Arc<dyn ObjectStore>) -> usize {
        let mut rebound = 0;
        for mut entry in self.entries.iter_mut() {
            if entry.backend.is_none() && entry.metastore.is_some() {
                entry.backend = Some(Arc::clone(new_backend));
                rebound += 1;
            }
        }
        rebound
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
    /// ``Arc::ptr_eq`` on ``Arc<dyn MetaStore>``. R20.3 gave every
    /// crosslink its own ``ZoneMetaStore`` allocation so Arc identity
    /// no longer groups crosslinks of the same zone — each zone needs
    /// a storage-level identity that survives per-mount wrapping.
    /// ``MetaStore::coherence_key`` exposes that identity (stable
    /// integer; state-machine Arc pointer for raft-backed zones,
    /// ``None`` for standalone ``LocalMetaStore``).
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

    /// Generate a direct-access download URL for the given path, if the
    /// routed mount's backend supports ExternalTransport.
    ///
    /// Returns `Ok(Some(url))` when the backend can sign, `Ok(None)` when
    /// the mount has no ExternalTransport capability, or `Err` on signing
    /// failure.
    pub fn generate_download_url(
        &self,
        canonical_key: &str,
        backend_path: &str,
        expires_seconds: u64,
    ) -> Result<Option<String>, StorageError> {
        let entry = self
            .entries
            .get(canonical_key)
            .ok_or_else(|| StorageError::NotFound(format!("mount not found: {canonical_key}")))?;
        match &entry.external_transport {
            Some(transport) => transport
                .generate_download_url(backend_path, expires_seconds)
                .map(Some),
            None => Ok(None),
        }
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

    /// Longest-prefix-match routing within a zone, with fallback to the
    /// root zone's mounts.
    ///
    /// Walks zone-canonical mount keys from deepest to shallowest. If the
    /// caller's zone has no matching mount, retries once under the root
    /// zone — root mounts are the global default visible to every zone
    /// (federation or standalone). Pure routing — access control
    /// (read-only, admin-only, RBAC) lives in rebac, not here.
    pub fn route(&self, path: &str, zone_id: &str) -> Result<RouteResult, RouteError> {
        if let Some(result) = self.route_in_zone(path, zone_id) {
            return Ok(result);
        }
        if zone_id != contracts::ROOT_ZONE_ID {
            if let Some(result) = self.route_in_zone(path, contracts::ROOT_ZONE_ID) {
                return Ok(result);
            }
        }
        Err(RouteError::NotMounted(format!(
            "No mount found for path: {}",
            path
        )))
    }

    fn route_in_zone(&self, path: &str, zone_id: &str) -> Option<RouteResult> {
        let canonical = canonicalize_mount_path(path, zone_id);
        let mut current = canonical.as_str();

        loop {
            if let Some(entry) = self.entries.get(current) {
                let mount_point = current.to_string();
                let backend_path = strip_mount_prefix(&canonical, current);
                let is_external = entry.is_external;
                // CAS detection via trait downcast — no string sniffing.
                // Mounts with no Rust backend (Python-side connector) are
                // not CAS.
                let is_cas = entry
                    .backend
                    .as_ref()
                    .map(|b| b.as_cas().is_some())
                    .unwrap_or(false);
                let resolved_zone = entry
                    .target_zone_id
                    .clone()
                    .unwrap_or_else(|| zone_id.to_string());
                drop(entry);

                return Some(RouteResult {
                    mount_point,
                    backend_path,
                    zone_id: resolved_zone,
                    is_external,
                    is_cas,
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
        None
    }

    // ── Backend-operation delegation ───────────────────────────────────
    //
    // Thin wrappers that look up a mount by canonical key and call the
    // matching `ObjectStore` method. Kept on `VFSRouter` (not `Kernel`)
    // so the lookup + call live in one place and the `dashmap::Ref` is
    // held for the shortest possible window. A mount without a backend
    // returns `None` — callers treat this as "no Rust-side backend, fall
    // back to Python connector or cold-path dcache lookup".

    /// Read content from the mount's backend.
    pub fn read_content(
        &self,
        canonical_key: &str,
        content_id: &str,
        ctx: &OperationContext,
    ) -> Option<Vec<u8>> {
        let entry = self.entries.get(canonical_key)?;
        entry.backend.as_ref()?.read_content(content_id, ctx).ok()
    }

    /// Write content to the mount's backend.
    ///
    /// `offset == 0` is full-file write (current behavior). `offset > 0`
    /// is POSIX pwrite(2) — see `ObjectStore::write_content` for the
    /// zero-fill / NotSupported contract.
    pub fn write_content(
        &self,
        canonical_key: &str,
        content: &[u8],
        content_id: &str,
        ctx: &OperationContext,
        offset: u64,
    ) -> Result<Option<WriteResult>, StorageError> {
        let Some(entry) = self.entries.get(canonical_key) else {
            // Mount not found — caller treats as a hit=false miss.
            return Ok(None);
        };
        let Some(backend) = entry.backend.as_ref() else {
            return Ok(None);
        };
        backend
            .write_content(content, content_id, ctx, offset)
            .map(Some)
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
    ) -> Option<Result<(), crate::abc::object_store::StorageError>> {
        let entry = self.entries.get(canonical_key)?;
        let backend = entry.backend.as_ref()?;
        Some(backend.rename(old_backend_path, new_backend_path))
    }

    /// Copy a file via the mount's backend (PAS server-side copy).
    ///
    /// Returns `None` when the mount has no Rust backend (Python-side connector).
    /// Returns `Some(Ok(_))` on success, `Some(Err(_))` on backend error.
    /// Callers should fall back to read+write only for `StorageError::NotSupported`;
    /// other errors indicate a real failure and should be propagated.
    pub fn copy_file(
        &self,
        canonical_key: &str,
        src_backend_path: &str,
        dst_backend_path: &str,
    ) -> Option<Result<crate::abc::object_store::WriteResult, crate::abc::object_store::StorageError>>
    {
        let entry = self.entries.get(canonical_key)?;
        Some(
            entry
                .backend
                .as_ref()?
                .copy_file(src_backend_path, dst_backend_path),
        )
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

    /// List directory entries via the mount's backend.
    pub fn list_dir(
        &self,
        canonical_key: &str,
        backend_path: &str,
    ) -> Result<Vec<String>, StorageError> {
        let entry = self
            .entries
            .get(canonical_key)
            .ok_or_else(|| StorageError::NotFound(format!("mount not found: {canonical_key}")))?;
        let backend = entry
            .backend
            .as_ref()
            .ok_or(StorageError::NotSupported("no backend for mount"))?;
        backend.list_dir(backend_path)
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

    fn entry() -> MountEntry {
        MountEntry::new(None)
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
        let table = VFSRouter::new();
        table.add("/", "root", entry());
        table.add("/workspace", "root", entry());

        let r = table.route("/workspace/file.txt", "root").unwrap();
        assert_eq!(r.mount_point, "/root/workspace");
        assert_eq!(r.backend_path, "file.txt");
    }

    #[test]
    fn test_route_falls_back_to_root() {
        let table = VFSRouter::new();
        table.add("/", "root", entry());

        let r = table.route("/unknown/path", "root").unwrap();
        assert_eq!(r.mount_point, "/root");
        assert_eq!(r.backend_path, "unknown/path");
    }

    #[test]
    fn test_cross_zone_isolation() {
        let table = VFSRouter::new();
        table.add("/", "root", entry());
        table.add("/shared", "zone-beta", entry());

        // root zone falls back to root mount
        let r = table.route("/workspace/file.txt", "root").unwrap();
        assert_eq!(r.mount_point, "/root");

        // zone-beta sees its own mount
        let r = table.route("/shared/doc.txt", "zone-beta").unwrap();
        assert_eq!(r.mount_point, "/zone-beta/shared");
    }

    #[test]
    fn test_install_metastore_late() {
        use crate::meta_store::{FileMetadata, MetaStoreError};

        // Trivial in-memory MetaStore impl for the test.
        struct DummyMs;
        impl MetaStore for DummyMs {
            fn get(&self, _: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
                Ok(None)
            }
            fn put(&self, _: &str, _: FileMetadata) -> Result<(), MetaStoreError> {
                Ok(())
            }
            fn delete(&self, _: &str) -> Result<bool, MetaStoreError> {
                Ok(false)
            }
            fn list(&self, _: &str) -> Result<Vec<FileMetadata>, MetaStoreError> {
                Ok(vec![])
            }
            fn exists(&self, _: &str) -> Result<bool, MetaStoreError> {
                Ok(false)
            }
        }

        let table = VFSRouter::new();
        table.add("/data", "root", entry());
        let canonical = canonicalize_mount_path("/data", "root");

        // Initially no metastore.
        assert!(table.get_canonical(&canonical).unwrap().metastore.is_none());

        table.install_metastore(&canonical, Arc::new(DummyMs));
        assert!(table.get_canonical(&canonical).unwrap().metastore.is_some());
    }

    #[test]
    fn test_mount_management() {
        let table = VFSRouter::new();
        table.add("/data", "root", entry());
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
        let table = VFSRouter::new();
        table.add("/corp", "root", entry());

        let global = "/corp/eng/foo.txt";
        let route = table.route(global, "root").unwrap();
        let zone_path = if route.backend_path.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", route.backend_path)
        };
        let recovered = zone_to_global(&route.mount_point, &zone_path);
        assert_eq!(recovered, global);
    }

    /// Federation topology: two DISTINCT ``ZoneMetaStore`` Arcs (with
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
        impl crate::meta_store::MetaStore for KeyedStub {
            fn get(
                &self,
                _: &str,
            ) -> Result<Option<crate::meta_store::FileMetadata>, crate::meta_store::MetaStoreError>
            {
                Ok(None)
            }
            fn put(
                &self,
                _: &str,
                _: crate::meta_store::FileMetadata,
            ) -> Result<(), crate::meta_store::MetaStoreError> {
                Ok(())
            }
            fn delete(&self, _: &str) -> Result<bool, crate::meta_store::MetaStoreError> {
                Ok(false)
            }
            fn list(
                &self,
                _: &str,
            ) -> Result<Vec<crate::meta_store::FileMetadata>, crate::meta_store::MetaStoreError>
            {
                Ok(Vec::new())
            }
            fn exists(&self, _: &str) -> Result<bool, crate::meta_store::MetaStoreError> {
                Ok(false)
            }
            fn coherence_key(&self) -> Option<usize> {
                self.key
            }
        }

        const CORP_KEY: usize = 0xC0;
        const FAMILY_KEY: usize = 0xFA;

        let corp_a: Arc<dyn MetaStore> = Arc::new(KeyedStub {
            key: Some(CORP_KEY),
        });
        let corp_b: Arc<dyn MetaStore> = Arc::new(KeyedStub {
            key: Some(CORP_KEY),
        }); // DISTINCT Arc, same coherence key — crosslink of the same zone.
        let family: Arc<dyn MetaStore> = Arc::new(KeyedStub {
            key: Some(FAMILY_KEY),
        });

        let table = VFSRouter::new();
        table.add(
            "/corp",
            "root",
            MountEntry::new(None).with_metastore(corp_a),
        );
        table.add(
            "/family/work",
            "root",
            MountEntry::new(None).with_metastore(corp_b),
        );
        table.add(
            "/family",
            "root",
            MountEntry::new(None).with_metastore(family),
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
