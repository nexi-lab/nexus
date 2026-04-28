//! `FederationProvider` HAL trait — abstract Raft federation surface.
//!
//! Phase 5 cycle break.  Pre-Phase-5 the kernel's `Kernel` struct held
//! `Arc<nexus_raft::ZoneManager>` directly and 30+ call sites inside
//! `kernel.rs` / `core/dlc.rs` / `core/stream/wal.rs` named raft
//! types (`ZoneConsensus<FullStateMachine>`, `Command`, `BlobFetcherSlot`,
//! `ZoneRaftRegistry`, …).  The kernel-side calls now go through this
//! trait so federation-aware syscalls dispatch via `kernel.federation_arc()`
//! instead of naming raft types directly.
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

/// Result type used across the federation HAL.  String errors carry
/// the raft / gRPC status messages verbatim from the underlying
/// implementation.
pub type FederationResult<T> = Result<T, String>;

/// Opaque handle stashed by raft federation init for transport-tier
/// blob fetch wiring.  Kernel never inspects the contents — it only
/// stores and returns the handle so `transport::blob::fetcher::install`
/// can drain it at cdylib boot.
///
/// Pre-Phase-5 this slot held a concrete `nexus_raft::blob_fetcher::
/// BlobFetcherSlot`; the trait object form lets the kernel struct
/// stay raft-free at the call-site.
pub type BlobFetcherSlot = Box<dyn std::any::Any + Send + Sync>;

/// Abstract Raft federation surface.
///
/// Implementor: `kernel::raft_federation_provider::RaftFederationProvider`
/// (Phase 5 anchor — provider lives in kernel for now; Phase H follow-up
/// moves it to `nexus_raft::federation_provider`).
///
/// `Send + Sync + 'static` so the `Arc<dyn FederationProvider>` can be
/// shared across syscall threads and the cdylib's tokio runtime
/// without per-call cloning of trait objects.
pub trait FederationProvider: Send + Sync + 'static {
    /// Initialise the federation cluster from environment variables
    /// (`NEXUS_HOSTNAME`, `NEXUS_PEERS`, `NEXUS_BIND_ADDR`,
    /// `NEXUS_DATA_DIR`, `NEXUS_NO_TLS`).  Idempotent — returns
    /// `Ok(false)` if federation was already initialised.  Returns
    /// `Ok(true)` on first successful init.
    fn init_from_env(&self, kernel: &crate::kernel::Kernel) -> FederationResult<bool>;

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
    ) -> FederationResult<Arc<dyn MetaStore>>;

    /// Construct a per-zone distributed-lock backend.  Replaces the
    /// kernel's default `LocalLocks` for the given zone so lock
    /// acquisitions replicate via `Command::AcquireLock` on every
    /// peer.
    fn locks_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> FederationResult<Arc<dyn Locks>>;

    /// Construct a per-zone WAL `StreamBackend` impl backed by Raft
    /// `Command::AppendStreamEntry` proposals.
    fn wal_stream_for_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        stream_id: &str,
        prefix: &str,
    ) -> FederationResult<Arc<dyn crate::stream::StreamBackend>>;

    /// Read a blob (path or content-id) from a remote zone.  Used by
    /// the cross-zone read fast-path when local content is missing
    /// but the federation knows another peer has it.
    fn remote_read_blob(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> FederationResult<Vec<u8>>;

    /// Wire a federation mount: register the dcache coherence callback,
    /// install the per-mount metastore, and seed the DCache entry.
    fn wire_mount(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> FederationResult<()>;

    /// Start the EC replication scanner for the given zone.
    fn start_replication_scanner(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        policies_json: &str,
        interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>>;

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
    fn create_zone(&self, kernel: &crate::kernel::Kernel, zone_id: &str) -> FederationResult<()>;

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
    ) -> FederationResult<()>;

    /// Join an existing raft zone as a voter (`as_learner=false`) or
    /// learner (`as_learner=true`).  Used by `federation_join` to
    /// pull a zone advertised by a peer into the local node.
    fn join_zone(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        as_learner: bool,
    ) -> FederationResult<()>;

    /// Copy a subtree from `parent_zone` (rooted at `prefix`) into
    /// `new_zone` as the new zone's content.  Used by federation_share.
    /// Returns the number of entries copied.
    fn zone_share(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        prefix: &str,
        new_zone: &str,
    ) -> FederationResult<u64>;

    /// Register a `local_path → zone_id` mapping in the
    /// raft-replicated share registry so peers can resolve the share
    /// without a separate RPC.
    fn register_share(
        &self,
        kernel: &crate::kernel::Kernel,
        local_path: &str,
        zone_id: &str,
    ) -> FederationResult<()>;

    /// Look up a previously-registered share by remote path.  `None`
    /// if the path was never shared on any cluster member.
    fn lookup_share(
        &self,
        kernel: &crate::kernel::Kernel,
        remote_path: &str,
    ) -> FederationResult<Option<String>>;

    /// Count of mounts pointing at `zone_id` across the cluster.
    fn zone_links_count(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> FederationResult<i64>;

    /// Rich cluster status (node_id, leader_id, term, commit_index,
    /// applied_index, voter_count, witness_count) for `zone_id`.
    /// Returned as `(field_name, json_value)` pairs so the kernel
    /// HAL stays JSON-typed.
    fn zone_cluster_info(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
    ) -> FederationResult<Vec<(String, serde_json::Value)>>;

    /// Append `entry` at `(zone_id, stream_id, seq)` to the raft-replicated
    /// WAL stream and return the committed sequence number.  Used by both
    /// DT_STREAM (`io_profile="wal"`) and DT_PIPE (`io_profile="wal"`)
    /// backends — `stream_id` carries the kernel-side namespace prefix
    /// (`__wal_stream__/<id>` or `__wal_pipe__/<id>`) so pipe and stream
    /// entries share `TREE_STREAM_ENTRIES` without colliding.
    ///
    /// Read-your-writes semantics: a successful append is observable to a
    /// subsequent `get_stream_entry` from the same node before the entry
    /// flushes to disk.  Implementations achieve this via an inflight
    /// cache; the trait surface does not surface the cache directly.
    fn append_stream_entry(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        stream_id: &str,
        seq: u64,
        entry: Vec<u8>,
    ) -> FederationResult<u64>;

    /// Read the entry at `(zone_id, stream_id, seq)`.  Returns
    /// `Ok(None)` when the entry has not been written yet (cursor ahead
    /// of writer).  Returns `Err` when the stream is closed AND the
    /// offset is out of range — callers can distinguish "not yet"
    /// (retry / wait) from "permanently absent" (replay finished).
    fn get_stream_entry(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        stream_id: &str,
        seq: u64,
    ) -> FederationResult<Option<Vec<u8>>>;
}

/// No-op fallback used at `Kernel::new` so the federation slot is
/// never `None` — non-cdylib Rust tests + WASM builds keep the same
/// call shape.  Every method either returns an empty/`None` value or
/// errors out with a clear "federation not installed" message; the
/// cdylib's `install_federation_wiring` boot path replaces this with
/// the real `RaftFederationProvider` impl before any federation
/// syscall fires.
pub struct NoopFederationProvider;

impl FederationProvider for NoopFederationProvider {
    fn init_from_env(&self, _kernel: &crate::kernel::Kernel) -> FederationResult<bool> {
        Err("FederationProvider not installed (non-cdylib build)".into())
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
    ) -> FederationResult<Arc<dyn MetaStore>> {
        Err("FederationProvider not installed".into())
    }

    fn locks_for_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> FederationResult<Arc<dyn Locks>> {
        Err("FederationProvider not installed".into())
    }

    fn wal_stream_for_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _prefix: &str,
    ) -> FederationResult<Arc<dyn crate::stream::StreamBackend>> {
        Err("FederationProvider not installed".into())
    }

    fn remote_read_blob(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _path: &str,
        _content_id: &str,
    ) -> FederationResult<Vec<u8>> {
        Err("FederationProvider not installed".into())
    }

    fn wire_mount(
        &self,
        _kernel: &crate::kernel::Kernel,
        _parent_zone: &str,
        _mount_path: &str,
        _target_zone: &str,
    ) -> FederationResult<()> {
        Err("FederationProvider not installed".into())
    }

    fn start_replication_scanner(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _policies_json: &str,
        _interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>> {
        Err("FederationProvider not installed".into())
    }

    fn stash_blob_fetcher_slot(&self, _kernel: &crate::kernel::Kernel, _slot: BlobFetcherSlot) {}

    fn take_blob_fetcher_slot(&self, _kernel: &crate::kernel::Kernel) -> Option<BlobFetcherSlot> {
        None
    }

    fn create_zone(&self, _kernel: &crate::kernel::Kernel, _zone_id: &str) -> FederationResult<()> {
        Err("FederationProvider not installed".into())
    }

    fn remove_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _force: bool,
    ) -> FederationResult<()> {
        Err("FederationProvider not installed".into())
    }

    fn join_zone(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _as_learner: bool,
    ) -> FederationResult<()> {
        Err("FederationProvider not installed".into())
    }

    fn zone_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _parent_zone: &str,
        _prefix: &str,
        _new_zone: &str,
    ) -> FederationResult<u64> {
        Err("FederationProvider not installed".into())
    }

    fn register_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _local_path: &str,
        _zone_id: &str,
    ) -> FederationResult<()> {
        Err("FederationProvider not installed".into())
    }

    fn lookup_share(
        &self,
        _kernel: &crate::kernel::Kernel,
        _remote_path: &str,
    ) -> FederationResult<Option<String>> {
        Ok(None)
    }

    fn zone_links_count(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> FederationResult<i64> {
        Ok(0)
    }

    fn zone_cluster_info(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
    ) -> FederationResult<Vec<(String, serde_json::Value)>> {
        Err("FederationProvider not installed".into())
    }

    fn append_stream_entry(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _seq: u64,
        _entry: Vec<u8>,
    ) -> FederationResult<u64> {
        Err("FederationProvider not installed".into())
    }

    fn get_stream_entry(
        &self,
        _kernel: &crate::kernel::Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _seq: u64,
    ) -> FederationResult<Option<Vec<u8>>> {
        Ok(None)
    }
}

impl NoopFederationProvider {
    pub fn arc() -> Arc<dyn FederationProvider> {
        Arc::new(NoopFederationProvider)
    }
}
