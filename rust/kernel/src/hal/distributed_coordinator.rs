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
//! without holding its own back-references.  Implementations are
//! therefore unit / lightweight structs that delegate into the
//! kernel's federation primitives.

use std::sync::Arc;

use crate::abc::meta_store::MetaStore;
use contracts::lock_state::Locks;

/// Result type used across the Control-Plane HAL. String errors carry
/// the raft / gRPC status messages verbatim from the underlying impl.
pub type CoordinatorResult<T> = Result<T, String>;

/// Opaque handle stashed by the coordinator install hook for the
/// transport-tier blob-fetch wiring to drain. Kernel stores and
/// returns the handle so `transport::blob::fetcher::install` can
/// downcast to the concrete type at cdylib boot.
pub type BlobFetcherSlot = Box<dyn std::any::Any + Send + Sync>;

/// Control-Plane HAL §3.B.1 trait — distributed namespace coordination.
///
/// Implementor: `nexus_raft::distributed_coordinator::RaftDistributedCoordinator`.
///
/// `Send + Sync + 'static` so the `Arc<dyn DistributedCoordinator>` can
/// be shared across syscall threads and the cdylib's tokio runtime
/// without per-call cloning of trait objects.
pub trait DistributedCoordinator: Send + Sync + 'static {
    /// Initialise the federation cluster from environment variables
    /// (`NEXUS_HOSTNAME`, `NEXUS_PEERS`, `NEXUS_BIND_ADDR`,
    /// `NEXUS_DATA_DIR`, `NEXUS_NO_TLS`).  Idempotent — returns
    /// `Ok(false)` if federation was already initialised.  Returns
    /// `Ok(true)` on first successful init.
    fn init_from_env(&self, kernel: &crate::kernel::Kernel) -> CoordinatorResult<bool>;

    /// True once federation has been initialised (zone manager exists).
    fn is_initialized(&self, kernel: &crate::kernel::Kernel) -> bool;

    /// Federation gRPC server bind address (e.g. `"0.0.0.0:2126"`).
    /// `None` when federation is not initialised.
    fn bind_address(&self, kernel: &crate::kernel::Kernel) -> Option<String>;

    /// Hostname this node advertises in federation.  `None` when
    /// federation is not initialised.
    fn hostname(&self, kernel: &crate::kernel::Kernel) -> Option<String>;

    /// List zone IDs the federation knows about.  Returns an empty
    /// `Vec` when federation is not initialised, so callers (e.g.
    /// `sys_listdir("/__zones__")`) get a stable shape regardless of
    /// federation state.
    fn list_zones(&self, kernel: &crate::kernel::Kernel) -> Vec<String>;

    /// Construct a per-zone `MetaStore` impl backed by the federation's
    /// Raft state machine.  Used by `Kernel::add_mount` when wiring a
    /// federation-mounted zone — the returned `Arc<dyn MetaStore>`
    /// goes onto the mount entry so all path lookups under that mount
    /// route through Raft.
    fn metastore_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn MetaStore>>;

    /// Construct a per-zone distributed-lock backend.  Replaces the
    /// kernel's default `LocalLocks` for the given zone so lock
    /// acquisitions replicate via `Command::AcquireLock` on every
    /// peer.
    fn locks_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn Locks>>;

    /// Read a blob (path or content-id) from a remote zone.  Used by
    /// the cross-zone read fast-path when local content is missing
    /// but the federation knows another peer has it.
    fn remote_read_blob(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> CoordinatorResult<Vec<u8>>;

    /// Wire a federation mount: register the dcache coherence callback,
    /// install the per-mount metastore, and seed the DCache entry.
    fn wire_mount(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> CoordinatorResult<()>;

    /// Stash a transport-tier blob-fetcher slot.  Drained by
    /// `transport::blob::fetcher::install` at cdylib boot.
    fn stash_blob_fetcher_slot(&self, kernel: &crate::kernel::Kernel, slot: BlobFetcherSlot);

    /// Take and return any previously stashed blob-fetcher slot.
    /// `None` after the first take or if no slot was stashed.
    fn take_blob_fetcher_slot(&self, kernel: &crate::kernel::Kernel) -> Option<BlobFetcherSlot>;

    /// Create (or look up an existing) raft zone with `zone_id`.
    /// Idempotent — repeat calls return the same zone.  Wires the
    /// kernel-side apply-cb so DT_MOUNT events on the new zone
    /// propagate to the VFSRouter + Python DLC.  Used by the
    /// `federation_create_zone` RPC entry point.
    fn create_zone(&self, kernel: &crate::kernel::Kernel, zone_id: &str) -> CoordinatorResult<()>;

    /// Remove a raft zone, cascade-unmounting every cross-zone mount
    /// pointing to it first.  `force=true` honors the POSIX-style
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
    /// learner (`as_learner=true`).  Used by `federation_join` to
    /// pull a zone advertised by a peer into the local node.
    fn join_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        as_learner: bool,
    ) -> CoordinatorResult<()>;

    /// Copy a subtree from `parent_zone` (rooted at `prefix`) into
    /// `new_zone` as the new zone's content.  Used by federation_share.
    /// Returns the number of entries copied.
    fn zone_share(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        prefix: &str,
        new_zone: &str,
    ) -> CoordinatorResult<u64>;

    /// Register a `local_path → zone_id` mapping in the
    /// raft-replicated share registry so peers can resolve the share
    /// without a separate RPC.
    fn register_share(
        &self,
        kernel: &crate::kernel::Kernel,
        local_path: &str,
        zone_id: &str,
    ) -> CoordinatorResult<()>;

    /// Look up a previously-registered share by remote path.  `None`
    /// if the path was never shared on any cluster member.
    fn lookup_share(
        &self,
        kernel: &crate::kernel::Kernel,
        remote_path: &str,
    ) -> CoordinatorResult<Option<String>>;

    /// Count of mounts pointing at `zone_id` across the cluster.
    fn zone_links_count(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<i64>;

    /// Rich cluster status (node_id, leader_id, term, commit_index,
    /// applied_index, voter_count, witness_count) for `zone_id`.
    /// Returned as `(field_name, json_value)` pairs so the kernel
    /// HAL stays JSON-typed.
    fn zone_cluster_info(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Vec<(String, serde_json::Value)>>;
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
    fn init_from_env(&self, _kernel: &crate::kernel::Kernel) -> CoordinatorResult<bool> {
        Err("DistributedCoordinator not installed (non-cdylib build)".into())
    }

    fn is_initialized(&self, _kernel: &crate::kernel::Kernel) -> bool {
        false
    }

    fn bind_address(&self, _kernel: &crate::kernel::Kernel) -> Option<String> {
        None
    }

    fn hostname(&self, _kernel: &crate::kernel::Kernel) -> Option<String> {
        None
    }

    fn list_zones(&self, _kernel: &crate::kernel::Kernel) -> Vec<String> {
        Vec::new()
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

    fn remote_read_blob(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _path: &str,
        _content_id: &str,
    ) -> CoordinatorResult<Vec<u8>> {
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

    fn stash_blob_fetcher_slot(&self, _kernel: &crate::kernel::Kernel, _slot: BlobFetcherSlot) {}

    fn take_blob_fetcher_slot(&self, _kernel: &crate::kernel::Kernel) -> Option<BlobFetcherSlot> {
        None
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

    fn zone_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _parent_zone: &str,
        _prefix: &str,
        _new_zone: &str,
    ) -> CoordinatorResult<u64> {
        Err("DistributedCoordinator not installed".into())
    }

    fn register_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _local_path: &str,
        _zone_id: &str,
    ) -> CoordinatorResult<()> {
        Err("DistributedCoordinator not installed".into())
    }

    fn lookup_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _remote_path: &str,
    ) -> CoordinatorResult<Option<String>> {
        Ok(None)
    }

    fn zone_links_count(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<i64> {
        Ok(0)
    }

    fn zone_cluster_info(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> CoordinatorResult<Vec<(String, serde_json::Value)>> {
        Err("DistributedCoordinator not installed".into())
    }
}

impl NoopDistributedCoordinator {
    pub fn arc() -> Arc<dyn DistributedCoordinator> {
        Arc::new(NoopDistributedCoordinator)
    }
}
