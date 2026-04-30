//! `DistributedCoordinator` HAL trait — Control-Plane HAL §3.B.1.
//!
//! The kernel reaches distributed namespace state — zones, mounts, share
//! registry, per-zone metastore + locks — through this trait so
//! federation-aware syscalls dispatch via `kernel.distributed_coordinator()`
//! rather than naming raft types directly. Distributed state
//! (`ZoneManager`, `ZoneRaftRegistry`, tokio runtime, cross-zone mounts
//! reverse index) lives on the concrete impl, which the cdylib boot
//! installs through `nexus_raft::distributed_coordinator::install`.
//!
//! Linux analogue: kernel's `struct super_operations` — the filesystem
//! abstraction surface that lets the VFS layer talk to any concrete
//! filesystem driver without knowing the driver type.
//!
//! ## Method shape
//!
//! Every method takes `kernel: &Kernel` so the trait impl can reach
//! kernel-side state (zone_manager, peer_client, dcache, vfs_router)
//! without holding its own back-references. Implementations are
//! therefore unit / lightweight structs that delegate into the
//! kernel's federation primitives.
//!
//! ## Trait surface (11 methods, four families)
//!
//! - **Introspection (2):** `list_zones`, `cluster_info`.
//! - **Zone lifecycle (3):** `create_zone`, `remove_zone`, `join_zone`.
//! - **Mount wiring (2):** `wire_mount` / `unwire_mount`.
//! - **Share registry (2):** `share_zone`, `lookup_share`.
//! - **Per-zone dispatch (2):** `metastore_for_zone`, `locks_for_zone`.
//!
//! Boot-time setup is a module-level `install()` function — a once-per-process
//! hook that wires the slot and folds in DI plumbing (blob-fetcher slot
//! stash) outside the runtime trait surface.

use std::sync::Arc;

use crate::abc::meta_store::MetaStore;
use contracts::lock_state::Locks;

/// Result type used across the Control-Plane HAL. String errors carry
/// the raft / gRPC status messages verbatim from the underlying impl.
pub type CoordinatorResult<T> = Result<T, String>;

/// Opaque handle stashed by the coordinator install hook so the
/// raft-tier blob-fetcher handler can drain it. Kernel stores and
/// returns the handle so `nexus_raft::blob_fetcher_handler::install`
/// downcasts to the concrete type at cdylib boot.
pub type BlobFetcherSlot = Box<dyn std::any::Any + Send + Sync>;

/// Bundled cluster status for one zone — typed return from
/// [`DistributedCoordinator::cluster_info`].
///
/// Fields cover the full set of introspection probes a caller might
/// want in one round-trip: leader identity, raft term, replication
/// counts, mount link count.
#[derive(Debug, Clone)]
pub struct ClusterInfo {
    pub zone_id: String,
    pub node_id: u64,
    pub has_store: bool,
    pub is_leader: bool,
    pub leader_id: u64,
    pub term: u64,
    pub commit_index: u64,
    pub applied_index: u64,
    pub voter_count: usize,
    pub witness_count: usize,
    /// Count of cross-zone mounts pointing at this zone across the cluster.
    pub links_count: i64,
}

/// Share-registry entry — typed return from
/// [`DistributedCoordinator::share_zone`] and
/// [`DistributedCoordinator::lookup_share`].
///
/// `share_zone` populates `copied_entries` with the count of metadata
/// entries copied during the atomic share operation. `lookup_share`
/// leaves it at `0` since lookups read the registry without copying.
#[derive(Debug, Clone)]
pub struct ShareInfo {
    pub zone_id: String,
    pub copied_entries: u64,
}

/// Control-Plane HAL §3.B.1 trait — distributed namespace coordination.
///
/// Implementor: `nexus_raft::distributed_coordinator::RaftDistributedCoordinator`.
///
/// `Send + Sync + 'static` so the `Arc<dyn DistributedCoordinator>` can
/// be shared across syscall threads and the cdylib's tokio runtime
/// without per-call cloning of trait objects.
pub trait DistributedCoordinator: Send + Sync + 'static {
    // ── Introspection ──────────────────────────────────────────────────

    /// List zone IDs the coordinator knows about. Returns an empty
    /// `Vec` when federation is inactive, so callers (e.g.
    /// `sys_listdir("/__zones__")`) get a stable shape regardless of
    /// federation state.
    fn list_zones(&self, kernel: &crate::kernel::Kernel) -> Vec<String>;

    /// Bundled cluster status for `zone_id` — leader identity, raft
    /// term, replication counts, mount link count. Single round-trip
    /// for all introspection fields.
    fn cluster_info(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<ClusterInfo>;

    // ── Zone lifecycle ────────────────────────────────────────────────

    /// Create (or look up an existing) raft zone with `zone_id`.
    /// Idempotent — repeat calls return the same zone. Wires the
    /// kernel-side apply-cb so DT_MOUNT events on the new zone
    /// propagate to the VFSRouter + Python DLC.
    fn create_zone(&self, kernel: &crate::kernel::Kernel, zone_id: &str) -> CoordinatorResult<()>;

    /// Remove a raft zone, cascade-unmounting every cross-zone mount
    /// pointing to it first. `force=true` honors the POSIX-style
    /// `unlink while i_links > 0` bypass for the case where the
    /// cascade can't fully drain references (raft replication race
    /// on a follower, partial unmount, …).
    fn remove_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        force: bool,
    ) -> CoordinatorResult<()>;

    /// Join an existing raft zone as a voter (`as_learner=false`) or
    /// learner (`as_learner=true`). Used by `federation_join` to
    /// pull a zone advertised by a peer into the local node.
    fn join_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        as_learner: bool,
    ) -> CoordinatorResult<()>;

    // ── Mount wiring ──────────────────────────────────────────────────

    /// Wire a federation mount: register the dcache coherence callback,
    /// install the per-mount metastore, and seed the DCache entry.
    /// Leader-side fast-path; the apply-cb on the state machine is
    /// the correctness guarantee, this is the optimization.
    fn wire_mount(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> CoordinatorResult<()>;

    /// Reverse the bookkeeping done by `wire_mount` — drop the
    /// VFSRouter slot, evict the DCache seed, remove the reverse-index
    /// entry. Paired with `wire_mount` for symmetric leader-side
    /// fast-path; the apply-cb is the correctness guarantee.
    fn unwire_mount(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        mount_path: &str,
    ) -> CoordinatorResult<()>;

    // ── Share registry ────────────────────────────────────────────────

    /// Atomic share: copy a subtree from `local_path` into a freshly
    /// created `new_zone_id`, register the `local_path → new_zone_id`
    /// mapping in the raft-replicated share registry. Returns a
    /// [`ShareInfo`] with the new zone_id and copied-entry count.
    ///
    /// Single-call replacement for the older two-step
    /// `zone_share + register_share` orchestration.
    fn share_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        local_path: &str,
        new_zone_id: &str,
    ) -> CoordinatorResult<ShareInfo>;

    /// Look up a previously-registered share by remote path. Returns
    /// `None` when the path was never shared on any cluster member;
    /// `Some(ShareInfo)` carries the resolved `zone_id` (with
    /// `copied_entries=0` since lookups do not copy).
    fn lookup_share(
        &self,
        kernel: &crate::kernel::Kernel,
        remote_path: &str,
    ) -> CoordinatorResult<Option<ShareInfo>>;

    // ── Per-zone dispatch ─────────────────────────────────────────────

    /// Construct a per-zone `MetaStore` impl backed by the federation's
    /// Raft state machine. Used by `Kernel::add_mount` when wiring a
    /// federation-mounted zone — the returned `Arc<dyn MetaStore>`
    /// goes onto the mount entry so all path lookups under that mount
    /// route through Raft.
    fn metastore_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn MetaStore>>;

    /// Construct a per-zone distributed-lock backend. Replaces the
    /// kernel's default `LocalLocks` for the given zone so lock
    /// acquisitions replicate via `Command::AcquireLock` on every
    /// peer.
    fn locks_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn Locks>>;
}

/// No-op fallback used at `Kernel::new` so the coordinator slot is
/// always populated — non-cdylib Rust tests + WASM builds keep the
/// same call shape. Each method returns an empty/`None` value or
/// errors out with a clear "DistributedCoordinator not installed"
/// message; the cdylib's `install_distributed_coordinator` boot path
/// replaces this with the real `RaftDistributedCoordinator` impl
/// before any federation syscall fires.
pub struct NoopDistributedCoordinator;

impl DistributedCoordinator for NoopDistributedCoordinator {
    fn list_zones(&self, _kernel: &crate::kernel::Kernel) -> Vec<String> {
        Vec::new()
    }

    fn cluster_info(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<ClusterInfo> {
        Err("DistributedCoordinator not installed".into())
    }

    fn create_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn remove_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _force: bool,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn join_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _as_learner: bool,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn wire_mount(
        &self,
        _kernel: &crate::kernel::Kernel,
        _parent_zone: &str,
        _mount_path: &str,
        _target_zone: &str,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn unwire_mount(
        &self,
        _kernel: &crate::kernel::Kernel,
        _parent_zone: &str,
        _mount_path: &str,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn share_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _local_path: &str,
        _new_zone_id: &str,
    ) -> CoordinatorResult<ShareInfo> {
        Err("DistributedCoordinator not installed".into())
    }

    fn lookup_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _remote_path: &str,
    ) -> CoordinatorResult<Option<ShareInfo>> {
        Ok(None)
    }

    fn metastore_for_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn MetaStore>> {
        Err("DistributedCoordinator not installed".into())
    }

    fn locks_for_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn Locks>> {
        Err("DistributedCoordinator not installed".into())
    }
}

impl NoopDistributedCoordinator {
    pub fn arc() -> Arc<dyn DistributedCoordinator> {
        Arc::new(NoopDistributedCoordinator)
    }
}
