//! DriverLifecycleCoordinator — kernel-internal mount lifecycle primitive.
//!
//! Linux analogue: `register_filesystem()` + `kern_mount()` + `kill_sb()`.
//!
//! Pure kernel internal — zero `#[pymethods]`. Python callers go through
//! `Kernel::sys_setattr(DT_MOUNT)` (codegen ABI). Rust callers (factory,
//! zone_manager) will call DLC directly when Rust-ified.
//!
//! Responsibilities:
//!   1. Add/remove backend in kernel MountTable via `Kernel::add_mount`
//!   2. Write DT_MOUNT metadata to per-mount metastore
//!   3. Populate dcache with mount point entry
//!   4. Upgrade LockManager to distributed for root zone federation mounts
//!   5. Own a map of `MountInfo` records for kernel-internal queries

use crate::dcache::CachedEntry;
use crate::kernel::{Kernel, KernelError};
use crate::mount_table::canonicalize_mount_path as canonicalize;
use dashmap::DashMap;
use std::sync::Arc;

/// Kernel-internal mount metadata tracked by the DLC.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct MountInfo {
    pub zone_id: String,
    pub readonly: bool,
    pub admin_only: bool,
    pub io_profile: String,
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
    /// - `readonly`, `admin_only`, `io_profile` — access flags
    /// - `backend_name` — backend identifier string
    /// - `backend` — optional Rust backend (None = Python-side backend)
    /// - `metastore` — optional per-mount metastore (ZoneMetastore or RedbMetastore)
    /// - `raft_backend` — optional (ZoneConsensus, Handle) for federation DI
    #[allow(clippy::too_many_arguments)]
    pub fn mount(
        &self,
        kernel: &Kernel,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
        backend_name: &str,
        backend: Option<Box<dyn crate::backend::ObjectStore>>,
        metastore: Option<Arc<dyn crate::metastore::Metastore>>,
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
        is_external: bool,
    ) -> Result<(), KernelError> {
        // Snapshot the consensus handle + metastore Arc before moving
        // them into add_mount so we can install the dcache-invalidation
        // callback after routing is wired. ZoneConsensus is Clone
        // (wraps shared Arcs); the metastore Arc is a cheap ref bump
        // and is the *identity* the reverse-lookup keys on.
        let raft_snapshot = raft_backend.as_ref().map(|(c, _)| c.clone());
        let ms_for_cb: Option<Arc<dyn crate::metastore::Metastore>> =
            metastore.as_ref().map(Arc::clone);

        // 1. Routing table + per-mount metastore + lock manager upgrade
        kernel.add_mount(
            mount_point,
            zone_id,
            readonly,
            admin_only,
            io_profile,
            backend_name,
            backend,
            metastore,
            raft_backend,
            is_external,
        )?;

        // 1b. Apply-side cache coherence: install a callback on the zone's
        // state machine that evicts the kernel DCache entry for each key
        // mutated by a committed metadata command. Without this, nodes
        // that didn't originate a write (leader-forwarded follower
        // writes, catch-up replication) keep serving stale sys_stat /
        // sys_read from their local dcache even after raft applies the
        // new state — a textbook distributed-cache-coherence hole.
        //
        // A single federation zone can surface at multiple mount points
        // (direct mount + crosslink, e.g. ``corp`` mounted at both
        // ``/corp`` and ``/family/work``). The callback resolves ALL
        // current mount points whose per-mount metastore is THIS Arc, so
        // a write through one crosslink evicts the mirror entries cached
        // under the other. Keying by ``Arc::ptr_eq`` (not caller zone_id
        // or target zone_id) keeps the kernel primitive free of any
        // federation concept — R20.6 will move the install itself out
        // of DLC once federation-side wiring is plumbed.
        if let (Some(consensus), Some(ms_arc)) = (raft_snapshot, ms_for_cb) {
            if let Some(slot) = consensus.invalidate_cb_slot() {
                let dcache = kernel.dcache_handle();
                let mount_table = kernel.mount_table_handle();
                let cb: Arc<dyn Fn(&str) + Send + Sync> =
                    Arc::new(move |zone_relative_key: &str| {
                        let trimmed = zone_relative_key.trim_start_matches('/');
                        for mp in mount_table.mount_points_for_metastore(&ms_arc) {
                            let global = if trimmed.is_empty() {
                                mp.clone()
                            } else if mp.ends_with('/') {
                                format!("{}{}", mp, trimmed)
                            } else {
                                format!("{}/{}", mp, trimmed)
                            };
                            dcache.evict(&global);
                        }
                    });
                *slot.write() = Some(cb);
            }
        }

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
                backend_name: backend_name.to_string(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(mount_point, meta);
        });

        // 3. DCache entry for mount point
        kernel.dcache_put_entry(
            mount_point,
            CachedEntry {
                backend_name: backend_name.to_string(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        // 4. Store in DLC mounts map
        self.mounts.insert(
            canonical,
            MountInfo {
                zone_id: zone_id.to_string(),
                readonly,
                admin_only,
                io_profile: io_profile.to_string(),
                backend_name: backend_name.to_string(),
            },
        );

        Ok(())
    }

    /// Unmount with full lifecycle: metastore delete + dcache evict + routing remove.
    ///
    /// Returns `true` if mount was removed, `false` if not found.
    #[allow(dead_code)]
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
