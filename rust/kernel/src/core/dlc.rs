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
//!
//! DLC owns no mount table of its own — `VFSRouter::entries` is the SSOT
//! for "what mounts exist".  Querying mount existence goes through
//! `Kernel::has_mount` (which delegates to the router); listing mount
//! points goes through `Kernel::get_mount_points`.

use crate::dcache::CachedEntry;
use crate::kernel::{Kernel, KernelError};
use std::sync::Arc;

/// Kernel primitive: driver mount lifecycle.
///
/// Stateless coordinator — `mount()` / `unmount()` thread mutations into
/// the kernel's owned tables (`VFSRouter`, `dcache`, per-mount metastore)
/// rather than caching anything locally.  Created once at `Kernel::new()`.
pub(crate) struct DriverLifecycleCoordinator;

impl DriverLifecycleCoordinator {
    pub fn new() -> Self {
        Self
    }

    /// Mount a backend with full lifecycle: routing + metastore + dcache + lock.
    ///
    /// # Arguments
    /// - `kernel` — back-reference to the owning Kernel (interior mutability)
    /// - `mount_point` — virtual path (e.g. `/`, `/data`)
    /// - `zone_id` — zone identifier
    /// - `backend_name` — backend identifier string
    /// - `backend` — optional Rust backend (None = Python-side backend)
    /// - `metastore` — optional per-mount metastore (ZoneMetaStore or LocalMetaStore)
    /// - `raft_backend` — opaque raft handle for federation DI; downcast by
    ///   the `RaftDistributedCoordinator` impl when wiring distributed locks.
    #[allow(clippy::too_many_arguments)]
    pub fn mount(
        &self,
        kernel: &Kernel,
        mount_point: &str,
        zone_id: &str,
        backend_name: &str,
        backend: Option<Arc<dyn crate::abc::object_store::ObjectStore>>,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<Box<dyn std::any::Any + Send + Sync>>,
        is_external: bool,
    ) -> Result<(), KernelError> {
        // Resolve the PARENT zone's metastore via longest-prefix routing
        // (e.g. `/corp` resolves up to the `/` root-zone mount) and write
        // the DT_MOUNT entry there.  This is the SSOT for federation
        // routing: the parent zone's raft state machine replicates the
        // entry to every peer, and federation's `mount_apply_cb` wired
        // on the parent zone fires on each follower's apply, calling
        // `wire_mount_core` so cross-zone routing lands on every node.
        //
        // `with_metastore(mount_point)` does an exact-match lookup, so
        // it would NOT find the right (parent) zone — use `route()`'s
        // longest-prefix walk to find the enclosing mount, then write
        // through that mount's metastore with the full path as the key.
        let route = kernel.vfs_router_arc().route(mount_point, "root");
        if let Ok(parent_route) = route {
            // RouteResult.mount_point is already a canonical key (e.g. "/root").
            kernel.with_metastore(&parent_route.mount_point, |ms| {
                let meta = crate::meta_store::FileMetadata {
                    path: mount_point.to_string(),
                    size: 0,
                    content_id: None,
                    version: 1,
                    entry_type: 2, // DT_MOUNT
                    zone_id: Some(parent_route.zone_id.clone()),
                    mime_type: None,
                    created_at_ms: None,
                    modified_at_ms: None,
                    last_writer_address: None,
                    // DT_MOUNT routing pointer: the zone this mount points at.
                    target_zone_id: Some(zone_id.to_string()),
                    // DT_LINK target: only meaningful for DT_LINK entries.
                    link_target: None,
                };
                let _ = ms.put(mount_point, meta);
            });
        }

        // Apply-side dcache coherence (the per-zone invalidate callback
        // that fires on every committed metadata mutation) is installed
        // by the ``sys_setattr(DT_MOUNT)`` dispatcher via
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
                content_id: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                link_target: None,
            },
        );

        Ok(())
    }

    /// Unmount with full lifecycle: metastore delete + dcache evict + routing remove.
    ///
    /// Returns `true` if mount was removed, `false` if not found.
    pub fn unmount(&self, kernel: &Kernel, mount_point: &str, zone_id: &str) -> bool {
        // 1. Delete the DT_MOUNT metadata from the PARENT zone (the one
        //    that "owns" `mount_point`).  Symmetric with `mount()`:
        //    federation's apply-cb on the parent zone fires
        //    `unwire_mount_core` on every peer when this raft-replicated
        //    DeleteMetadata applies, so cross-node routing cleanup
        //    propagates the same way it was set up.  Looking up via
        //    `mount_point` itself routes through the new mount (the one
        //    being unmounted) and lands in the wrong state machine.
        //    Walk up to the parent path first so longest-prefix routing
        //    skips this mount and finds the actual parent.
        let parent_path = mount_point
            .rfind('/')
            .filter(|&i| i > 0)
            .map(|i| mount_point[..i].to_string())
            .unwrap_or_else(|| "/".to_string());
        let route = kernel.vfs_router_arc().route(&parent_path, "root");
        if let Ok(parent_route) = route {
            kernel.with_metastore(&parent_route.mount_point, |ms| {
                let _ = ms.delete(mount_point);
            });
        }

        // 2. DCache evict — mount point + all children
        kernel.dcache_evict(mount_point);
        let prefix = if mount_point.ends_with('/') {
            mount_point.to_string()
        } else {
            format!("{}/", mount_point)
        };
        kernel.dcache_evict_prefix(&prefix);

        // 3. Remove from routing table
        kernel.remove_mount(mount_point, zone_id)
    }
}
