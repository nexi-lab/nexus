//! `FederationProvider` HAL trait — abstract Raft federation surface.
//!
//! Phase 5 cycle break.  Pre-Phase-5 the kernel's `Kernel` struct held
//! `Arc<nexus_raft::ZoneManager>` directly and 30+ call sites inside
//! `kernel.rs` / `core/dlc.rs` / `core/stream/wal.rs` named raft
//! types (`ZoneConsensus<FullStateMachine>`, `Command`, `BlobFetcherSlot`,
//! `ZoneRaftRegistry`, …).  Adding `raft → kernel` (so Raft-backed
//! `MetaStore` impls + replication scanners + WAL stream backends can
//! depend on `kernel::abc::MetaStore` etc.) closed a Cargo cycle
//! against the existing `kernel → raft` edge.
//!
//! This trait inverts the edge.  Kernel code holds an
//! `Arc<dyn FederationProvider>` and never names a raft type; the
//! concrete impl lives in `nexus_raft::federation_provider` and is
//! installed by `nexus-cdylib` at boot via
//! `Kernel::set_federation` (mirrors the Phase 4 PeerBlobClient DI
//! pattern).
//!
//! Linux analogue: kernel's `struct super_operations` — the filesystem
//! abstraction surface that lets the VFS layer talk to any concrete
//! filesystem driver without knowing the driver type.

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
/// stay raft-free.
pub type BlobFetcherSlot = Box<dyn std::any::Any + Send + Sync>;

/// Abstract Raft federation surface.
///
/// Implementor: `nexus_raft::federation_provider::RaftFederationProvider`.
/// Kernel code calls into this trait whenever it needs a Raft-side
/// service — zone listing, cross-zone routing, distributed lock
/// installation, replicated stream construction, etc.
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
    ///
    /// The provider may stash a `BlobFetcherSlot` via
    /// [`stash_blob_fetcher_slot`] during this call so the transport
    /// crate's blob fetcher can install itself later.
    fn init_from_env(&self, kernel: &crate::kernel::Kernel) -> FederationResult<bool>;

    /// True once federation has been initialised (zone manager exists).
    fn is_initialized(&self) -> bool;

    /// Federation gRPC server bind address (e.g. `"0.0.0.0:2126"`).
    /// `None` when federation is not initialised.
    fn bind_address(&self) -> Option<String>;

    /// Hostname this node advertises in federation.  `None` when
    /// federation is not initialised.
    fn hostname(&self) -> Option<String>;

    /// List zone IDs the federation knows about.  Returns an empty
    /// `Vec` when federation is not initialised, so callers (e.g.
    /// `sys_listdir("/__zones__")`) get a stable shape regardless of
    /// federation state.
    fn list_zones(&self) -> Vec<String>;

    /// Construct a per-zone `MetaStore` impl backed by the federation's
    /// Raft state machine.  Used by `Kernel::add_mount` when wiring a
    /// federation-mounted zone — the returned `Arc<dyn MetaStore>`
    /// goes onto the mount entry so all path lookups under that mount
    /// route through Raft.
    fn metastore_for_zone(&self, zone_id: &str) -> FederationResult<Arc<dyn MetaStore>>;

    /// Construct a per-zone distributed-lock backend.  Replaces the
    /// kernel's default `LocalLocks` for the given zone so lock
    /// acquisitions replicate via `Command::AcquireLock` on every
    /// peer.  Called from `Kernel::install_distributed_locks`.
    fn locks_for_zone(&self, zone_id: &str) -> FederationResult<Arc<dyn Locks>>;

    /// Construct a per-zone WAL `StreamBackend` impl backed by Raft
    /// `Command::AppendStreamEntry` proposals.  Used when
    /// `sys_setattr(DT_STREAM, io_profile = "wal")` registers a new
    /// stream.
    fn wal_stream_for_zone(
        &self,
        zone_id: &str,
        stream_id: &str,
        prefix: &str,
    ) -> FederationResult<Arc<dyn crate::stream::StreamBackend>>;

    /// Read a blob (path or content-id) from a remote zone.  Used by
    /// the cross-zone read fast-path when local content is missing
    /// but the federation knows another peer has it.
    fn remote_read_blob(
        &self,
        zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> FederationResult<Vec<u8>>;

    /// Wire a federation mount: register the dcache coherence callback,
    /// install the per-mount metastore, and seed the DCache entry.
    /// Called from the apply-side `mount_apply_cb` after a
    /// `Command::MountTarget` lands.
    fn wire_mount(
        &self,
        kernel: &crate::kernel::Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> FederationResult<()>;

    /// Start the EC replication scanner for the given zone.  Returns
    /// the scanner as an opaque `Send`able handle whose `Drop`
    /// stops the background thread; `Kernel::start_replication_scanner`
    /// stashes the handle so a later `stop_replication_scanner`
    /// drains it.
    fn start_replication_scanner(
        &self,
        kernel: &crate::kernel::Kernel,
        zone_id: &str,
        policies_json: &str,
        interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>>;

    /// Stash a transport-tier blob-fetcher slot.  Drained by
    /// `transport::blob::fetcher::install` at cdylib boot.
    fn stash_blob_fetcher_slot(&self, slot: BlobFetcherSlot);

    /// Take and return any previously stashed blob-fetcher slot.
    /// `None` after the first take or if no slot was stashed.
    fn take_blob_fetcher_slot(&self) -> Option<BlobFetcherSlot>;
}

/// No-op fallback used at `Kernel::new` so the federation slot is
/// never `None` — non-cdylib Rust tests + WASM builds keep the same
/// call shape.  Every method either returns an empty/`None` value or
/// errors out with a clear "federation not installed" message; the
/// cdylib's `nexus-cdylib::install_federation_wiring` boot path
/// replaces this with the real `nexus_raft::federation_provider`
/// impl before any federation syscall fires.
pub struct NoopFederationProvider;

impl FederationProvider for NoopFederationProvider {
    fn init_from_env(&self, _kernel: &crate::kernel::Kernel) -> FederationResult<bool> {
        Err("FederationProvider not installed (non-cdylib build)".into())
    }

    fn is_initialized(&self) -> bool {
        false
    }

    fn bind_address(&self) -> Option<String> {
        None
    }

    fn hostname(&self) -> Option<String> {
        None
    }

    fn list_zones(&self) -> Vec<String> {
        Vec::new()
    }

    fn metastore_for_zone(&self, _zone_id: &str) -> FederationResult<Arc<dyn MetaStore>> {
        Err("FederationProvider not installed".into())
    }

    fn locks_for_zone(&self, _zone_id: &str) -> FederationResult<Arc<dyn Locks>> {
        Err("FederationProvider not installed".into())
    }

    fn wal_stream_for_zone(
        &self,
        _zone_id: &str,
        _stream_id: &str,
        _prefix: &str,
    ) -> FederationResult<Arc<dyn crate::stream::StreamBackend>> {
        Err("FederationProvider not installed".into())
    }

    fn remote_read_blob(
        &self,
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

    fn stash_blob_fetcher_slot(&self, _slot: BlobFetcherSlot) {}

    fn take_blob_fetcher_slot(&self) -> Option<BlobFetcherSlot> {
        None
    }
}

impl NoopFederationProvider {
    pub fn arc() -> Arc<dyn FederationProvider> {
        Arc::new(NoopFederationProvider)
    }
}
