//! DriverLifecycleCoordinator — kernel-internal mount lifecycle primitive.
//!
//! Linux analogue: `register_filesystem()` + `kern_mount()` + `kill_sb()`.
//!
//! Pure kernel internal — zero `#[pymethods]`. Python callers go through
//! `Kernel::sys_setattr(DT_MOUNT)` (codegen ABI). Rust callers (factory,
//! zone_manager) will call DLC directly when Rust-ified.
//!
//! Responsibilities:
//!   1. Add/remove backend in kernel VFSRouter via `Kernel::add_mount`
//!   2. Write DT_MOUNT metadata to per-mount metastore
//!   3. Populate dcache with mount point entry
//!   4. Upgrade LockManager to distributed for root zone federation mounts
//!   5. Own a map of `MountInfo` records for kernel-internal queries

use crate::dcache::CachedEntry;
use crate::kernel::{Kernel, KernelError};
use crate::vfs_router::canonicalize_mount_path as canonicalize;
use dashmap::DashMap;
use std::sync::Arc;

/// Kernel-internal mount metadata tracked by the DLC.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct MountInfo {
    pub zone_id: String,
    pub backend_name: String,
}

/// Kernel primitive: driver mount lifecycle.
///
/// Manages routing table + metastore + dcache + lock manager upgrade.
/// Created once at `Kernel::new()` — always available after construction.
pub(crate) struct DriverLifecycleCoordinator {
    mounts: DashMap<String, MountInfo>,
}

impl DriverLifecycleCoordinator {
    pub fn new() -> Self {
        Self {
            mounts: DashMap::new(),
        }
    }

    /// Mount a backend with full lifecycle: routing + metastore + dcache + lock.
    ///
    /// # Arguments
    /// - `kernel` — back-reference to the owning Kernel (interior mutability)
    /// - `mount_point` — virtual path (e.g. `/`, `/data`)
    /// - `zone_id` — zone identifier
    /// - `backend_name` — backend identifier string
    /// - `backend` — optional Rust backend (None = Python-side backend)
    /// - `metastore` — optional per-mount metastore (ZoneMetastore or LocalMetastore)
    /// - `raft_backend` — optional (ZoneConsensus, Handle) for federation DI
    #[allow(clippy::too_many_arguments)]
    pub fn mount(
        &self,
        kernel: &Kernel,
        mount_point: &str,
        zone_id: &str,
        backend_name: &str,
        backend: Option<Arc<dyn crate::backend::ObjectStore>>,
        metastore: Option<Arc<dyn crate::metastore::Metastore>>,
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
        is_external: bool,
    ) -> Result<(), KernelError> {
        // 1. Routing table + per-mount metastore + lock manager upgrade
        //
        // R20.6: apply-side dcache coherence (the per-zone invalidate
        // callback that fires on every committed metadata mutation) is
        // no longer wired here — it's installed by the
        // ``sys_setattr(DT_MOUNT)`` dispatcher via
        // ``Kernel::install_federation_dcache_coherence`` AFTER this
        // mount() returns. DLC stays federation-unaware; the install
        // sees only kernel primitives (dcache handle + metastore Arc
        // identity).
        kernel.add_mount(
            mount_point,
            zone_id,
            backend,
            metastore,
            raft_backend,
            is_external,
        )?;
        let _ = backend_name; // accepted for ABI compat; no longer plumbed.

        // 2. Write DT_MOUNT metadata entry (best-effort).
        // R20.3: the ZoneMetastore (per-mount) and LocalMetastore (global
        // fallback) both accept full paths at the trait boundary.
        // ZoneMetastore translates to its zone-relative root "/" internally;
        // LocalMetastore stores the full path directly. Either way the
        // caller passes ``mount_point`` (the VFS-global DT_MOUNT key).
        let canonical = canonicalize(mount_point, zone_id);
        kernel.with_metastore(&canonical, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: mount_point.to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
            };
            let _ = ms.put(mount_point, meta);
        });
        // ``backend_name`` (the legacy parameter to this fn) is kept for
        // API compatibility with callers but no longer persisted in the
        // metadata record — each node decides the backend from its own
        // mount table at read time.
        let _ = backend_name;

        // 3. DCache entry for mount point
        kernel.dcache_put_entry(
            mount_point,
            CachedEntry {
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
            },
        );

        // 4. Store in DLC mounts map
        self.mounts.insert(
            canonical,
            MountInfo {
                zone_id: zone_id.to_string(),
                backend_name: backend_name.to_string(),
            },
        );

        Ok(())
    }

    /// Unmount with full lifecycle: metastore delete + dcache evict + routing remove.
    ///
    /// Returns `true` if mount was removed, `false` if not found.
    pub fn unmount(&self, kernel: &Kernel, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);

        // 1. Delete metastore entry (best-effort) — full path
        // (ZoneMetastore translates internally to its zone-relative root).
        kernel.with_metastore(&canonical, |ms| {
            let _ = ms.delete(mount_point);
        });

        // 2. DCache evict — mount point + all children
        kernel.dcache_evict(mount_point);
        let prefix = if mount_point.ends_with('/') {
            mount_point.to_string()
        } else {
            format!("{}/", mount_point)
        };
        kernel.dcache_evict_prefix(&prefix);

        // 3. Remove from routing table
        let removed = kernel.remove_mount(mount_point, zone_id);

        // 4. Remove from DLC mounts map
        self.mounts.remove(&canonical);

        removed
    }

    /// Check if a mount exists in the DLC map.
    #[allow(dead_code)]
    pub fn has_mount(&self, canonical_key: &str) -> bool {
        self.mounts.contains_key(canonical_key)
    }

    /// Return all canonical mount keys.
    #[allow(dead_code)]
    pub fn mount_keys(&self) -> Vec<String> {
        self.mounts.iter().map(|r| r.key().clone()).collect()
    }
}
