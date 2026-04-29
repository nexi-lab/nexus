//! Mount-table primitives — router proxy methods that compose into
//! the `sys_setattr(DT_MOUNT)` syscall.
//!
//! Phase G of the Phase 3 restructure plan extracted these methods
//! from the monolithic `kernel.rs` into a dedicated submodule.  The
//! split is a file-organization change — every method stays a member
//! of [`Kernel`] via the submodule's `impl Kernel { ... }` block.
//!
//! Mount-related responsibilities split across three layers:
//!
//!   1. `sys_setattr(DT_MOUNT)` (in `kernel/mod.rs`) is the syscall
//!      Python and Rust callers invoke; it dispatches to the DLC.
//!   2. The `dlc::DriverLifecycleCoordinator` (kernel-internal)
//!      orchestrates the full mount lifecycle: routing-table insert,
//!      DT_MOUNT metastore write, dcache seed, lock-manager upgrade.
//!   3. This submodule's methods are the lower-level primitives DLC
//!      composes: `add_mount` / `remove_mount` modify the VFSRouter,
//!      `install_mount_metastore` wires a per-mount metastore for
//!      dcache-miss fallback, `has_mount` / `get_mount_points` /
//!      `canonical_mount_key` are introspection helpers.

use std::sync::Arc;

use crate::vfs_router::canonicalize_mount_path as canonicalize;

use super::{Kernel, KernelError, RustRouteResult};

impl Kernel {
    // ── Mount-table primitives (composed by sys_setattr DT_MOUNT) ──────

    /// Add a mount: inserts a routing table entry + optional per-mount
    /// metastore + optional Rust-native backend, and rebinds any
    /// federation mounts that replayed before the root mount landed.
    ///
    /// Visibility: ``pub(crate)`` — ``DLC::mount`` is the sole intended
    /// caller (R20.5). Python-driven mounts flow ``sys_setattr(DT_MOUNT)
    /// → DLC::mount → add_mount``; bypassing DLC skips the metastore
    /// DT_MOUNT write + dcache seed + mount-info bookkeeping.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn add_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        backend: Option<Arc<dyn crate::abc::object_store::ObjectStore>>,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<Box<dyn std::any::Any + Send + Sync>>,
        is_external: bool,
    ) -> Result<(), KernelError> {
        self.vfs_router
            .add_mount(mount_point, zone_id, backend.clone(), is_external);
        // Install per-mount metastore if provided. Must come AFTER the
        // entry is inserted so `install_metastore` finds it.
        if let Some(ms) = metastore {
            let canonical = canonicalize(mount_point, zone_id);
            self.vfs_router.install_metastore(&canonical, ms);
        }
        // Boot-order fix: on restart,
        // `RaftDistributedCoordinator::replay_existing_mounts` runs
        // before Python mounts root, so every federation mount it
        // replays gets `backend=None`. Once root lands with its CAS
        // backend, propagate it back into those stranded federation
        // mounts so sys_write stops silently missing.
        if mount_point == "/" && zone_id == contracts::ROOT_ZONE_ID {
            if let Some(ref root_backend) = backend {
                let rebound = self.vfs_router.rebind_missing_backends(root_backend);
                if rebound > 0 {
                    tracing::info!(
                        rebound_count = rebound,
                        "add_mount(/): rebound {} federation mounts that replayed before root",
                        rebound,
                    );
                }
            }
        }
        // Federation distributed-lock install lives on the trait
        // surface (`DistributedCoordinator::locks_for_zone` — §3.B.1).
        // `RaftDistributedCoordinator`, installed by the cdylib boot
        // path, wires the `DistributedLocks` backend the first time
        // a federated mount lands. Kernel sees only `Box<dyn Any>`
        // here so the raft edge stays inverted.
        let _ = raft_backend;
        Ok(())
    }

    /// Remove a mount point (and its per-mount metastore if any).
    /// Called by DLC.unmount() — not directly exposed to Python.
    #[allow(dead_code)]
    pub fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.vfs_router.remove(mount_point, zone_id)
    }

    /// Wire a per-mount `MetaStore` impl into the kernel's mount table.
    ///
    /// Used by code that constructs a `MetaStore` *outside* the kernel and
    /// wants the kernel's syscall fallback path to delegate to it for
    /// dcache misses on this mount. The canonical example is `rust/raft`'s
    /// `ZoneMetaStore`, which wraps a `ZoneConsensus` state machine and is
    /// constructed by the raft crate, then handed to the kernel via this
    /// method (see `PyZoneHandle::attach_to_kernel_mount`).
    ///
    /// `canonical_key` must match what `Kernel::add_mount(mount_point,
    /// zone_id, …)` produces internally — i.e. `/{zone_id}{mount_point}`
    /// after normalization. Use the `canonicalize` helper on the kernel
    /// side to compute it consistently.
    #[allow(dead_code)]
    pub fn install_mount_metastore(
        &self,
        canonical_key: String,
        ms: Arc<dyn crate::meta_store::MetaStore>,
    ) {
        self.vfs_router.install_metastore(&canonical_key, ms);
    }

    /// Compute the zone-canonical key for a (mount_point, zone_id) pair.
    ///
    /// Exposed publicly so external crates (e.g. `rust/raft`) can compute
    /// the same key the kernel uses internally without duplicating the
    /// normalization rules.
    pub fn canonical_mount_key(mount_point: &str, zone_id: &str) -> String {
        canonicalize(mount_point, zone_id)
    }

    /// Zone-canonical LPM routing.
    pub fn route(&self, path: &str, zone_id: &str) -> Result<RustRouteResult, KernelError> {
        self.vfs_router
            .route(path, zone_id)
            .map_err(KernelError::from)
    }

    /// Check if a mount exists.
    pub fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.vfs_router.has(mount_point, zone_id)
    }

    /// List all mount points (zone-canonical keys, sorted).
    pub fn get_mount_points(&self) -> Vec<String> {
        self.vfs_router.canonical_keys()
    }
}
