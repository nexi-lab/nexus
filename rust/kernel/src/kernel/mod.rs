//! Kernel — pure Rust kernel owning all core state.
//!
//! Zero PyO3 dependency. All Python bridging lives in generated_pyo3.rs.
//!
//! Owns DCache, PathRouter, Trie, VFS Lock, MetaStore.
//! Hook/Observer registries live in generated_pyo3::PyKernel (wrapper-only).
//!
//! Architecture:
//!   - Created empty via Kernel::new(), then components are wired by wrapper.
//!   - DCache/Router/Trie use interior mutability (&self methods).
//!   - VFS Lock is optionally Arc-shared with VFSLockManager (blocking acquire).
//!   - MetaStore (Box<dyn MetaStore>) wraps any impl (Python adapter, redb, gRPC).
//!
//! Issue #1868: Phase H — kernel boundary collapse.

use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_MOUNT, DT_PIPE, DT_REG, DT_STREAM};
use crate::dispatch::{MutationObserver, Trie};
use crate::file_watch::FileWatchRegistry;
use crate::lock_manager::{LockManager, LockMode};
use crate::meta_store::LocalMetaStore;
use crate::vfs_router::{
    canonicalize_mount_path as canonicalize, RouteError, RustRouteResult, VFSRouter,
};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex, RwLock, RwLockReadGuard};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// Extension trait giving parking_lot's two read-lock methods names that
/// describe what they DO rather than what they're called for, so a reader
/// (human or AI) doesn't have to consult the docs to know which is safe.
///
/// parking_lot exposes:
/// * ``read()`` — yields to a queued writer (writer-fair). Same-thread
///   recursion can deadlock.
/// * ``read_recursive()`` — does NOT yield (reader priority). Same-thread
///   recursion always succeeds.
///
/// The standard names hide the policy and the deadlock risk. We rename:
/// * ``read_unconditional`` — unconditionally takes a shared read; safe
///   under recursion.
/// * ``read_yielding_to_writer`` — explicitly opts in to writer fairness;
///   **not** safe under recursion.
///
/// Pick ``read_unconditional`` whenever there's any chance a callback
/// triggered while the lock is held could re-enter; pick the other only
/// when writer starvation is a real concern *and* recursion is impossible.
pub(crate) trait RwLockExt<T: ?Sized> {
    fn read_unconditional(&self) -> RwLockReadGuard<'_, T>;
    #[allow(dead_code)]
    fn read_yielding_to_writer(&self) -> RwLockReadGuard<'_, T>;
}

impl<T: ?Sized> RwLockExt<T> for RwLock<T> {
    #[inline]
    fn read_unconditional(&self) -> RwLockReadGuard<'_, T> {
        self.read_recursive()
    }
    #[inline]
    fn read_yielding_to_writer(&self) -> RwLockReadGuard<'_, T> {
        self.read()
    }
}

/// VFS gRPC client stubs — used by `try_remote_fetch` to pull blobs from
/// the origin node when metadata has been Raft-replicated but the CAS
/// blob lives on a remote peer. Generated from `proto/nexus/grpc/vfs/vfs.proto`
/// (see `build.rs`).
///
/// Phase 4 bumped to `pub` so peer crates (`transport::grpc`,
/// `transport::federation`) can use the same generated client / server
/// stubs without re-generating them — proto definitions stay
/// kernel-owned (the build.rs that compiles `vfs.proto` lives in
/// kernel) but the generated module surface is shared.
pub mod vfs_proto {
    tonic::include_proto!("nexus.grpc.vfs");
}

// ── Phase G: per-syscall-family submodules ─────────────────────────
//
// These submodules each carry an `impl Kernel` block over a method
// subset. The split is a file-organization change — every method
// remains a member of `Kernel` and is invoked the same way.
mod dispatch;
mod io;
mod ipc;
mod locks;
mod observability;

// ── KernelError ────────────────────────────────────────────────────────────

/// Kernel-level error type — pure Rust, no PyO3 dependency.
///
/// Error conversion to PyErr lives in generated_pyo3.rs.
#[derive(Debug)]
pub enum KernelError {
    InvalidPath(String),
    FileNotFound(String),
    FileExists(String),
    Route(RouteError),
    IOError(String),
    TrieError(String),
    // IPC error variants
    PipeFull(String),
    PipeEmpty(String),
    PipeClosed(String),
    PipeExists(String),
    PipeNotFound(String),
    StreamFull(String),
    StreamEmpty(String),
    StreamClosed(String),
    StreamExists(String),
    StreamNotFound(String),
    WouldBlock(String),
    PermissionDenied(String),
    /// Backend operation failed (``Backend.write_content`` / ``read_content``
    /// / ``delete_content`` / ``rename_file``). Propagated as
    /// ``nexus.contracts.exceptions.BackendError`` on the Python side so
    /// callers can distinguish storage failures from pure kernel issues.
    BackendError(String),
    /// R20.18.2: federation bootstrap (env parsing, ZoneManager
    /// construction, create_zone/join_zone, reconcile) failed.
    Federation(String),
}

impl From<RouteError> for KernelError {
    fn from(e: RouteError) -> Self {
        KernelError::Route(e)
    }
}

impl From<std::io::Error> for KernelError {
    fn from(e: std::io::Error) -> Self {
        KernelError::IOError(e.to_string())
    }
}

// ── OperationContext — kernel-internal credential ─────────────────────────

/// Syscall credential — carried through every kernel operation.
///
/// Constructed by thin wrapper (Python, gRPC, etc.) with identity fields.
/// Rust kernel uses `zone_id` for routing; hooks use the full context.
///
/// Analogous to Linux `struct cred` — immutable after construction.
#[derive(Clone, Debug)]
pub struct OperationContext {
    /// Subject identity (human user or service account).
    pub user_id: String,
    /// Routing zone — NexusFS instance zone for mount lookup (always set).
    pub zone_id: String,
    /// Admin privilege flag.
    pub is_admin: bool,
    /// Agent identity (optional, for agent-initiated operations).
    pub agent_id: Option<String>,
    /// System operation flag (bypasses all checks).
    pub is_system: bool,
    /// Group memberships for ReBAC.
    pub groups: Vec<String>,
    /// Granted admin capabilities (e.g. "MANAGE_ZONES", "READ_ALL").
    pub admin_capabilities: Vec<String>,
    /// Subject type for ReBAC (default: "user").
    pub subject_type: String,
    /// Subject ID for ReBAC (defaults to user_id).
    pub subject_id: Option<String>,
    /// Audit trail correlation ID.
    pub request_id: String,
    /// Caller's zone_id (None = no zone restriction). Distinct from routing zone_id.
    pub context_zone_id: Option<String>,
}

impl OperationContext {
    #[allow(dead_code)]
    pub fn new(
        user_id: &str,
        zone_id: &str,
        is_admin: bool,
        agent_id: Option<&str>,
        is_system: bool,
    ) -> Self {
        Self {
            user_id: user_id.to_string(),
            zone_id: zone_id.to_string(),
            is_admin,
            agent_id: agent_id.map(|s| s.to_string()),
            is_system,
            groups: Vec::new(),
            admin_capabilities: Vec::new(),
            subject_type: "user".to_string(),
            subject_id: None,
            request_id: String::new(),
            context_zone_id: None,
        }
    }
}

// ── Strong-typed result types ──────────────────────────────────────────

/// Result of sys_read(): concrete type instead of Option<bytes>.
///
/// DT_REG: `data` is always `Some(bytes)` on success. Failures return
/// `Err(KernelError::FileNotFound)` — no `hit` flag, no Python-side miss
/// handling. Federation remote fetch is handled internally (see
/// `Kernel::try_remote_fetch`).
///
/// DT_PIPE / DT_STREAM: `entry_type` tells the wrapper to dispatch IPC.
/// `data` may be `None` when the Rust IPC registry has no buffer and
/// Python must fall through to blocking backends (still transitional).
pub struct SysReadResult {
    /// Content bytes. Vec<u8> — wrapper converts to PyBytes.
    pub data: Option<Vec<u8>>,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Content hash (etag) for post-hook context.
    pub content_hash: Option<String>,
    /// DT_REG(1), DT_PIPE(3), DT_STREAM(4).
    pub entry_type: u8,
}

/// Result of sys_write(): concrete type instead of Option<str>.
pub struct SysWriteResult {
    /// True if Rust backend completed the write.
    pub hit: bool,
    /// BLAKE3 content hash (only when hit=true).
    pub content_id: Option<String>,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Metadata version after write (for event dispatch).
    pub version: u32,
    /// Content size in bytes.
    pub size: u64,
    /// True if the file did not exist before this write.
    pub is_new: bool,
    /// Etag (content hash) of the file before this write (None if new file).
    pub old_etag: Option<String>,
    /// Size of the file before this write (None if new file).
    pub old_size: Option<u64>,
    /// Metadata version before this write (None if new file).
    pub old_version: Option<u32>,
    /// Modified-at timestamp (epoch ms) before this write (None if new file).
    pub old_modified_at_ms: Option<i64>,
}

/// Result of sys_unlink(): hit + metadata for event payload.
pub struct SysUnlinkResult {
    /// True if Rust completed the full operation (metastore + backend + dcache).
    /// False for DT_MOUNT/DT_PIPE/DT_STREAM or when Rust fallback not available.
    pub hit: bool,
    /// Entry type of the deleted entry (DT_REG, DT_DIR, etc.).
    pub entry_type: u8,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Path that was deleted (for event payload).
    pub path: String,
    /// Etag of deleted file (for event payload).
    pub etag: Option<String>,
    /// Size of deleted file (for event payload).
    pub size: u64,
}

/// Result of sys_rename(): hit + metadata for event payload.
pub struct SysRenameResult {
    /// True if Rust completed the full operation (metastore + backend + dcache).
    pub hit: bool,
    /// True if both paths validated and routed successfully.
    pub success: bool,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// True if the renamed entry is a directory.
    pub is_directory: bool,
    /// Old metadata fields for Python post-hook dispatch (audit trail).
    pub old_etag: Option<String>,
    pub old_size: Option<u64>,
    pub old_version: Option<u32>,
    pub old_modified_at_ms: Option<i64>,
}

/// Result of sys_mkdir(): hit flag.
pub struct SysMkdirResult {
    /// True if Rust completed the full operation (backend + metastore + dcache).
    pub hit: bool,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
}

/// Result of sys_rmdir(): hit + children info.
pub struct SysRmdirResult {
    /// True if Rust completed the full operation.
    pub hit: bool,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Number of children deleted (when recursive).
    pub children_deleted: usize,
}

/// Result of sys_copy(): concrete type for copy operation.
pub struct SysCopyResult {
    /// True if Rust completed the full operation.
    pub hit: bool,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Destination path.
    pub dst_path: String,
    /// Content hash (etag) of the destination file.
    pub etag: Option<String>,
    /// Destination file size.
    pub size: u64,
    /// Metadata version of the destination file.
    pub version: u32,
}

/// Result of sys_setattr(): Rust handles ALL filesystem entry types.
#[derive(Debug)]
pub struct SysSetAttrResult {
    /// Path that was operated on.
    pub path: String,
    /// True if a new inode was created.
    pub created: bool,
    /// Entry type that was set.
    pub entry_type: i32,
    /// Backend name (when DT_MOUNT).
    pub backend_name: Option<String>,
    /// Buffer capacity (DT_PIPE/DT_STREAM).
    pub capacity: Option<usize>,
    /// Field names changed (UPDATE path).
    pub updated: Vec<String>,
    /// SHM path (when io_profile="shared_memory", unix only).
    pub shm_path: Option<String>,
    /// SHM data read fd — reader listens for data availability.
    pub data_rd_fd: Option<i32>,
    /// SHM space read fd — writer listens for space freed (pipe only).
    pub space_rd_fd: Option<i32>,
}

// ── DcacheStats ──────────────────────────────────────────────────────

/// DCache statistics — pure Rust struct returned by dcache_stats().
pub struct DcacheStats {
    pub hits: u64,
    pub misses: u64,
    pub size: usize,
    pub hit_rate: f64,
}

// ── StatResult ───────────────────────────────────────────────────────

/// Result of sys_stat(): pure Rust struct returned by sys_stat().
/// Wrapper converts to PyDict for Python callers.
pub struct StatResult {
    pub path: String,
    pub size: u64,
    pub etag: Option<String>,
    pub mime_type: String,
    pub is_directory: bool,
    pub entry_type: u8,
    pub mode: u32,
    pub version: u32,
    pub zone_id: Option<String>,
    pub created_at_ms: Option<i64>,
    pub modified_at_ms: Option<i64>,
    pub last_writer_address: Option<String>,
    pub lock: Option<crate::lock_manager::KernelLockInfo>,
}

// ── ZonesProcfsEntry — R20.18.4 procfs virtual namespace ──────────────

/// Synthesized entry for `/__sys__/zones/*` virtual paths.
///
/// All fields are read live from `raft::ZoneManager` each call — this
/// struct carries no persisted state of its own (SSOT: raft state
/// machine). Returned by `Kernel::resolve_zones_procfs`; R20.18.5
/// wires it into `sys_stat` so Python callers see zone runtime state
/// as if it were a filesystem entry.
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct ZonesProcfsEntry {
    /// True when the path is the `/__sys__/zones/` directory itself.
    pub is_directory: bool,
    /// Zone id when `is_directory == false`; `None` for the dir.
    pub zone_id: Option<String>,
    pub node_id: u64,
    pub has_store: bool,
    pub is_leader: bool,
    pub leader_id: u64,
    pub term: u64,
    pub commit_index: u64,
    pub applied_index: u64,
    pub voter_count: usize,
    pub witness_count: usize,
    /// R20.16.6 ready-signal passthrough — saves consumers a
    /// second Kernel call.
    pub mount_reconciliation_done: bool,
}

// ── KernelObserverRegistry — pure Rust observer dispatch ────────────────

/// Observer entry — pure Rust, no PyO3 dependency.
///
/// Stores `Arc<dyn MutationObserver>` so the OBSERVE ThreadPool worker
/// (§11 Phase 3) can clone the trait object across threads. `event_mask`
/// bitmask matching happens without external dependency.
struct KernelObserverEntry {
    observer: Arc<dyn MutationObserver>,
    name: String,
    event_mask: u32,
}

/// Pure Rust observer registry — event-type bitmask filtering lock-free.
///
/// Single dispatch path for all OBSERVE-phase observers. The trait
/// `MutationObserver` takes `&FileEvent` (post §11 Phase 2); the
/// to a Python `FileEvent` once per call.
///
/// `OBSERVE_INLINE` (the legacy inline-on-caller-thread mode) was deleted
/// in §11 Phase 2: it overlapped with INTERCEPT POST hooks and violated
/// dispatch-contract orthogonality. OBSERVE is fire-and-forget by
/// definition — there is no other mode. Observers needing causal
/// ordering or sync blocking belong in INTERCEPT POST, not OBSERVE.
struct KernelObserverRegistry {
    observers: Vec<KernelObserverEntry>,
}

#[allow(dead_code)]
impl KernelObserverRegistry {
    fn new() -> Self {
        Self {
            observers: Vec::new(),
        }
    }

    /// Register an observer with its event-type bitmask.
    fn register(&mut self, observer: Arc<dyn MutationObserver>, name: String, event_mask: u32) {
        self.observers.push(KernelObserverEntry {
            observer,
            name,
            event_mask,
        });
    }

    /// Unregister by name (identity is not available for trait objects).
    /// Returns true if a registration with that name was removed.
    fn unregister(&mut self, name: &str) -> bool {
        if let Some(pos) = self.observers.iter().position(|e| e.name == name) {
            self.observers.remove(pos);
            return true;
        }
        false
    }

    /// Return clones of all observers whose event_mask matches `event.event_type`.
    ///
    /// The dispatch loop (`Kernel::dispatch_observers`, §11 Phase 3) submits
    /// each clone to the OBSERVE ThreadPool. Returning Arc clones lets the
    /// pool borrow the registry lock for the minimum possible time — the
    /// caller releases the lock before doing any per-observer work.
    fn matching(&self, event_type_bit: u32) -> Vec<Arc<dyn MutationObserver>> {
        self.observers
            .iter()
            .filter(|e| e.event_mask & event_type_bit != 0)
            .map(|e| Arc::clone(&e.observer))
            .collect()
    }

    fn count(&self) -> usize {
        self.observers.len()
    }
}

// ── Native Hook Registry (§11 Phase 10) ────────────────────────────────
//
// Pure Rust hook dispatch — no GIL crossing for Rust-native hooks.
// Parallel to the PyO3-dependent HookRegistry in hook_registry.rs.
// NativeInterceptHook trait defined in dispatch.rs.

use crate::dispatch::{HookContext, NativeInterceptHook};

#[allow(dead_code)]
struct NativeHookEntry {
    hook: Box<dyn NativeInterceptHook>,
}

#[allow(dead_code)]
pub(crate) struct NativeHookRegistry {
    hooks: Vec<NativeHookEntry>,
}

#[allow(dead_code)]
impl NativeHookRegistry {
    pub(crate) fn new() -> Self {
        Self { hooks: Vec::new() }
    }

    pub(crate) fn register(&mut self, hook: Box<dyn NativeInterceptHook>) {
        self.hooks.push(NativeHookEntry { hook });
    }

    /// Dispatch pre-hooks. Returns Err on first abort.
    pub(crate) fn dispatch_pre(&self, ctx: &HookContext) -> Result<(), String> {
        for entry in &self.hooks {
            entry.hook.on_pre(ctx)?;
        }
        Ok(())
    }

    /// Dispatch post-hooks (fire-and-forget).
    pub(crate) fn dispatch_post(&self, ctx: &HookContext) {
        for entry in &self.hooks {
            entry.hook.on_post(ctx);
        }
    }

    pub(crate) fn count(&self) -> usize {
        self.hooks.len()
    }
}

// ── Zone Revision Entry ─────────────────────────────────────────────────

/// Per-zone monotonic revision counter + condvar for waiters.
/// AtomicU64 increment = ~1ns (Relaxed ordering).
/// Condvar notify_all only fires when waiters exist (check has_waiters flag).
pub(crate) struct ZoneRevisionEntry {
    revision: AtomicU64,
    has_waiters: AtomicU64,
    mutex: parking_lot::Mutex<()>,
    condvar: Condvar,
}

impl ZoneRevisionEntry {
    fn new() -> Self {
        Self {
            revision: AtomicU64::new(0),
            has_waiters: AtomicU64::new(0),
            mutex: parking_lot::Mutex::new(()),
            condvar: Condvar::new(),
        }
    }
}

// ── Kernel ──────────────────────────────────────────────────────────────

/// Rust kernel — owns all core state directly.
///
/// Created empty via `Kernel::new()`, then wired by wrapper:
///   - `set_lock_manager(lm)` — share unified lock manager.
///   - `add_mount(...)` — register mount points.
///   - `dcache_put(...)` — populate dentry cache.
///   - `trie_register(...)` — register path resolvers.
pub struct Kernel {
    // DriverLifecycleCoordinator — owns mount lifecycle (routing + metastore + dcache).
    pub(crate) dlc: crate::dlc::DriverLifecycleCoordinator,
    // DCache — ``Arc`` so federation apply-event callbacks can hold a
    // shared reference that outlives the kernel's call frames (the
    // state machine's invalidate_cb closure runs on the raft driver
    // thread, not the kernel's Python-binding thread).
    dcache: Arc<DCache>,
    // Mount table — owns backend + per-mount metastore + access flags.
    // Replaces the old `router: PathRouter` + `mount_metastores: DashMap`
    // split; both lookups now go through `VFSRouter` (F2 C2). Wrapped
    // in ``Arc`` so federation apply-event callbacks can look up the
    // current set of mounts-for-zone at invalidation time (a zone can
    // be mounted under multiple paths — direct + crosslink).
    pub(crate) vfs_router: Arc<VFSRouter>,
    // PathTrie (owned)
    trie: Trie,
    // Unified lock manager: I/O lock + advisory lock + optional Raft.
    lock_manager: Arc<LockManager>,
    // MetaStore (Box<dyn MetaStore>), behind parking_lot::RwLock so
    // the setter paths (``set_metastore_path`` / ``release_metastores``)
    // don't need ``&mut self`` — lets ``PyKernel`` hold an ``Arc<Kernel>``
    // for the apply-side federation-mount callback (R20.16.3).
    metastore: parking_lot::RwLock<Option<Box<dyn crate::meta_store::MetaStore>>>,
    // VFS lock timeout for blocking acquire (ms) — ``AtomicU64`` so
    // ``set_vfs_lock_timeout`` stays ``&self``; reads are lock-free.
    vfs_lock_timeout_ms: AtomicU64,
    // Hook counts (atomics for lock-free hot-path check)
    read_hook_count: AtomicU64,
    write_hook_count: AtomicU64,
    stat_hook_count: AtomicU64,
    delete_hook_count: AtomicU64,
    rename_hook_count: AtomicU64,
    mkdir_hook_count: AtomicU64,
    rmdir_hook_count: AtomicU64,
    copy_hook_count: AtomicU64,
    access_hook_count: AtomicU64,
    write_batch_hook_count: AtomicU64,
    // Observer registry (owned by kernel — bitmask matching lock-free).
    //
    // Field is accessed only via the `register_observer` / `dispatch_observers`
    // methods, which have no production caller yet — Phase 5 wires them
    // into the sys_* methods, Phase 6 wires PyKernel.register_observer to
    // delegate here. Until then this is intentional pre-built infrastructure.
    #[allow(dead_code)]
    observers: Mutex<KernelObserverRegistry>,
    //
    // OBSERVE is fire-and-forget by contract: the syscall returns as soon
    // as the event is queued; observer callbacks run on this pool, off
    // the hot path. There is no other mode — the legacy `OBSERVE_INLINE`
    // flag was deleted in §11 Phase 2 because inline-on-caller-thread
    // observers were functionally identical to INTERCEPT POST hooks and
    // violated dispatch-contract orthogonality.
    //
    // 4 worker threads is enough for the typical workload (a handful of
    // long-lived observers: FileWatchRegistry, EventBus, etc.). Each worker
    // when calling Python observers — many parallel Python observers
    // will serialize on the GIL, but Rust-native observers run truly
    // parallel.
    //
    // No production caller yet — `dispatch_observers` becomes the sole
    // submitter once Phase 5 wires sys_* call sites. The pool is created
    // up-front so the cost (4 OS threads, ~8MB stack each) is paid once
    // at kernel construction.
    #[allow(dead_code)]
    // observer_pool removed — inline dispatch, no background threads.
    // Zone revision counter — AtomicU64 per zone + Condvar for waiters (§10 A2)
    zone_revisions: DashMap<String, Arc<ZoneRevisionEntry>>,
    // FileWatchRegistry — inotify equivalent. Arc-shared with observer registry.
    file_watches: Arc<FileWatchRegistry>,
    // Agent table — Rust SSOT for agent lifecycle state. Source lives in
    // the services rlib (rust/services/src/agent_table.rs); the kernel
    // owns an Arc handle so AgentStatusResolver and other kernel-internal
    // consumers can share read access without depending on field layout.
    pub(crate) agent_table: Arc<crate::core::agents::table::AgentTable>,
    // Service registry — DashMap backing store for service lifecycle.
    pub(crate) service_registry: Arc<crate::service_registry::ServiceRegistry>,
    // Per-mount metastores now live inside `VFSRouter::entries` as
    // `MountEntry::metastore: Option<Arc<dyn MetaStore>>` (our v20
    // SSOT cleanup — kept against develop's legacy split map).
    // Federation installs them via `VFSRouter::install_metastore`
    // after the mount is registered; standalone mode sets them during
    // `add_mount` when `metastore_path` is provided.
    // IPC registry — PipeManager owns DashMap<String, Arc<dyn PipeBackend>>
    pub(crate) pipe_manager: crate::pipe_manager::PipeManager,
    // IPC registry — StreamManager owns DashMap<String, Arc<dyn StreamBackend>>
    pub(crate) stream_manager: Arc<crate::stream_manager::StreamManager>,
    // Native hook registry — pure Rust hooks dispatched lock-free (§11 Phase 10)
    #[allow(dead_code)]
    // RwLock (not Mutex) so concurrent + recursive read-locks are allowed.
    // Recursion arises when a hook callback (e.g. ReBAC permission_hook)
    // calls back into ``sys_read`` for ``/__sys__/...`` configuration:
    // dispatch_pre → Python hook → sys_read → dispatch_native_pre. The
    // outer dispatch holds the lock for the duration of the Python call,
    // so a Mutex (non-reentrant) would deadlock; parking_lot::RwLock
    // allows the inner reader to proceed (registration is write-only and
    // happens once at startup, so writer starvation is not a concern).
    pub(crate) native_hooks: RwLock<NativeHookRegistry>,
    // Node advertise address — set in federation mode so sys_write encodes
    // origin in backend_name (e.g. "cas-local@nexus-1:2126"). Enables
    // on-demand remote content fetch on other nodes.
    self_address: parking_lot::RwLock<Option<String>>,
    /// Kernel-owned tokio runtime — built once at `Kernel::new` and
    /// shared across every async caller (peer RPC fan-out, federation
    /// remote reads, LLM connector streaming).  Phase 4 (full) lifted
    /// this off `peer_blob_client::PeerBlobClient` (which moved to
    /// the transport crate) so kernel-internal callers keep the same
    /// shared runtime regardless of whether the cdylib has installed
    /// the real peer client yet.
    pub(crate) runtime: Arc<tokio::runtime::Runtime>,
    // Shared tokio runtime — constructed once at Kernel::new and used by
    // every peer RPC (scatter-gather chunk fetch + federation remote
    // reads). Replaces the one-shot `Builder::new_current_thread()` inside
    // `try_remote_fetch` so tokio's workers shut down cleanly on
    // `release_metastores`/Drop (addresses R11 hypothesis #2 — stuck async
    // task blocking `docker stop`).
    // Phase 4 (full): widened from concrete `Arc<PeerBlobClient>`
    // (kernel::peer_blob_client) to `Arc<dyn hal::peer::PeerBlobClient>`
    // because the concrete impl moved to
    // `transport::blob::peer_client::PeerBlobClient` (Phase 4 ship).
    // Default at boot is `NoopPeerBlobClient`; nexus-cdylib boot
    // installs the real transport impl via `Kernel::set_peer_client`.
    pub(crate) peer_client: parking_lot::RwLock<Arc<dyn crate::hal::peer::PeerBlobClient>>,
    // Federation HAL slot (Phase 5).  `Arc<dyn FederationProvider>` so
    // the kernel's federation surface (init from env, zone listing,
    // distributed-lock / WAL-stream / Raft-MetaStore construction, mount
    // wiring, replication scanner) is reachable through a trait boundary
    // rather than direct `nexus_raft::*` types.  Default at boot is
    // `NoopFederationProvider`; nexus-cdylib boot installs the real
    // raft-side impl via `Kernel::set_federation`.  Mirrors the Phase-4
    // PeerBlobClient DI pattern.
    pub(crate) federation: parking_lot::RwLock<Arc<dyn crate::hal::federation::FederationProvider>>,
    // Scatter-gather fetcher: drives bounded fan-out against
    // `backend_name.origins` whenever a local chunk miss occurs.
    // Installed on every `CASEngine` via `VFSRouter` on mount
    // registration.
    //
    // Phase 2: type widened from concrete `Arc<GrpcChunkFetcher>` to
    // `Arc<dyn RemoteChunkFetcher>` so `BackendFactory` impls in the
    // backends crate can `Arc::clone(&self.inner.chunk_fetcher)` and
    // pass it through to `CasLocalBackend::new_with_fetcher` without
    // an explicit cast.
    #[allow(dead_code)]
    pub(crate) chunk_fetcher: Arc<dyn crate::cas_remote::RemoteChunkFetcher>,
    /// Pending remote metastore — set by ``sys_setattr(backend_type="remote")``
    /// and consumed immediately after mount registration to install the
    /// ``RemoteMetaStore`` on the mount entry. This avoids threading the
    /// metastore through ``sys_setattr``'s return value.
    pub(crate) pending_remote_meta_store:
        parking_lot::Mutex<Option<Arc<dyn crate::meta_store::MetaStore>>>,

    /// Phase 4 (full): `init_federation_from_env` used to call
    /// `wire_blob_fetcher` directly, which constructed a
    /// `transport::blob::fetcher::KernelBlobFetcher`.  Kernel can no
    /// longer reference `transport::*` after the Phase-4 crate split,
    /// so the slot is *stashed* here at federation bootstrap and
    /// drained by `transport::blob::fetcher::install(&kernel)` —
    /// invoked from the cdylib boot path once both crates are linked.
    /// Phase 4 (full): blob-fetcher slot stashed by federation init for
    /// the cdylib's transport-tier install hook to drain.
    /// Phase 5: typed as `Box<dyn Any + Send + Sync>` so kernel does not
    /// name the raft-side `BlobFetcherSlot` type — `transport::blob::
    /// fetcher::install` downcasts to the concrete type at drain time.
    pub(crate) pending_blob_fetcher_slot:
        parking_lot::Mutex<Option<Box<dyn std::any::Any + Send + Sync>>>,

    // ── Federation mount wiring (R20.16.3) ─────────────────────────
    //
    // Installed once at federation bootstrap via ``attach_zone_registry``.
    // Replaces the old Python ``_on_mount_event`` / ``_mount_via_kernel``
    // / ``_mounts_by_target`` chain with a pure-Rust apply-cb path:
    //
    //   FullStateMachine::apply(DT_MOUNT) — mount_apply_cb
    //     → Kernel::wire_federation_mount(parent, path, target, backend)
    //       → VFSRouter.add_mount + install_metastore(ZoneMetaStore)
    //       → DCache.put (seed DT_MOUNT entry so sys_stat sees it)
    //       → install_federation_dcache_coherence on target consensus
    //       → cross_zone_mounts.entry(target).push((parent, path, global))
    //
    // All three are set once; ``OnceLock`` is idempotent + lock-free read.
    #[allow(dead_code)]
    zone_registry: std::sync::OnceLock<Arc<nexus_raft::raft::ZoneRaftRegistry>>,
    #[allow(dead_code)]
    zone_runtime: std::sync::OnceLock<tokio::runtime::Handle>,
    /// R20.18.2: the owning `raft::ZoneManager` — populated by
    /// `init_federation_from_env()` when federation env vars are set.
    /// Kernel-internal; never exposed to Python per v20.10 boundary rule.
    #[allow(dead_code)]
    zone_manager: std::sync::OnceLock<Arc<nexus_raft::ZoneManager>>,
    /// Reverse index target_zone_id → [(parent_zone, mount_path,
    /// global_path)] for ``global_mount_of`` + cascade-unmount.
    /// Maintained by the apply-side callback: Set inserts, Delete drains.
    /// SSOT: derived from DT_MOUNT entries in every parent zone's state
    /// machine. Re-populated at startup by ``reconcile_mounts_from_zones``.
    #[allow(dead_code)]
    #[allow(clippy::type_complexity)]
    cross_zone_mounts: Arc<DashMap<String, Vec<(String, String, String)>>>,
    /// R20.18.2: set true by `init_federation_from_env` after
    /// `reconcile_mounts_from_zones` finishes. R20.16.6 /healthz/ready
    /// and R20.18.4 `/__sys__/zones/root` PathResolver will read this
    /// as the "federation bootstrap complete, safe to serve traffic"
    /// signal.
    #[allow(dead_code)]
    mount_reconciliation_done: AtomicBool,
}

impl Kernel {
    // ── Constructor ────────────────────────────────────────────────────

    /// Create an empty kernel. Components wired by wrapper after construction.
    ///
    /// Phase 3 bumped \`mod kernel\` to \`pub mod kernel\` so peer crates
    /// can reach \`Kernel::register_native_hook\` etc. — that surfaced
    /// `clippy::new_without_default` on this constructor.  Suppressed
    /// rather than auto-impl'd because `new()` does heavy wiring
    /// (runtime, peer client, dispatch hook registry, mount tables);
    /// callers should opt in explicitly via `Kernel::new()` rather
    /// than the implicit `Default::default()` shortcut.
    #[allow(clippy::new_without_default)]
    pub fn new() -> Self {
        // Phase 4 (full): kernel owns its tokio runtime now (was on
        // `PeerBlobClient` pre-Phase-4).  Multi-thread, two workers
        // sized for IO-bound peer RPCs.
        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .thread_name("nexus-kernel-peer")
                .enable_all()
                .build()
                .expect("failed to build kernel tokio runtime"),
        );
        // Phase 4 (full): peer_blob_client moved to `transport::blob::
        // peer_client`.  Kernel boots with the no-op fallback; the
        // cdylib wires the real impl via `Kernel::set_peer_client`
        // before any federation read fires.
        let peer_client_dyn: Arc<dyn crate::hal::peer::PeerBlobClient> =
            crate::hal::peer::NoopPeerBlobClient::arc();
        // GrpcChunkFetcher takes the trait object directly.
        let chunk_fetcher: Arc<dyn crate::cas_remote::RemoteChunkFetcher> = Arc::new(
            crate::cas_remote::GrpcChunkFetcher::new(Arc::clone(&peer_client_dyn), None),
        );
        let k = Self {
            dlc: crate::dlc::DriverLifecycleCoordinator::new(),
            dcache: Arc::new(DCache::new()),
            vfs_router: Arc::new(VFSRouter::new()),
            trie: Trie::new(),
            lock_manager: Arc::new(LockManager::new()),
            // Bare kernels boot with an in-memory metastore so tests,
            // quickstarts and minimal-mode boots have a working SSOT
            // without explicit wiring. `set_metastore_path` swaps it
            // for a redb-backed one on demand; federation installs a
            // per-mount `ZoneMetaStore` via `install_mount_metastore`.
            metastore: parking_lot::RwLock::new(Some(Box::new(
                crate::meta_store::MemoryMetaStore::new(),
            ))),
            vfs_lock_timeout_ms: AtomicU64::new(5000),
            read_hook_count: AtomicU64::new(0),
            write_hook_count: AtomicU64::new(0),
            stat_hook_count: AtomicU64::new(0),
            delete_hook_count: AtomicU64::new(0),
            rename_hook_count: AtomicU64::new(0),
            mkdir_hook_count: AtomicU64::new(0),
            rmdir_hook_count: AtomicU64::new(0),
            copy_hook_count: AtomicU64::new(0),
            access_hook_count: AtomicU64::new(0),
            write_batch_hook_count: AtomicU64::new(0),
            observers: Mutex::new(KernelObserverRegistry::new()),
            zone_revisions: DashMap::new(),
            file_watches: Arc::new(FileWatchRegistry::new()),
            agent_table: Arc::new(crate::core::agents::table::AgentTable::new()),
            service_registry: Arc::new(crate::service_registry::ServiceRegistry::new()),
            pipe_manager: crate::pipe_manager::PipeManager::new(),
            stream_manager: Arc::new(crate::stream_manager::StreamManager::new()),
            native_hooks: RwLock::new(NativeHookRegistry::new()),
            self_address: parking_lot::RwLock::new(None),
            runtime,
            peer_client: parking_lot::RwLock::new(peer_client_dyn),
            federation: parking_lot::RwLock::new(
                crate::hal::federation::NoopFederationProvider::arc(),
            ),
            chunk_fetcher,
            pending_remote_meta_store: parking_lot::Mutex::new(None),
            pending_blob_fetcher_slot: parking_lot::Mutex::new(None),
            zone_registry: std::sync::OnceLock::new(),
            zone_runtime: std::sync::OnceLock::new(),
            zone_manager: std::sync::OnceLock::new(),
            cross_zone_mounts: Arc::new(DashMap::new()),
            mount_reconciliation_done: AtomicBool::new(false),
        };
        // R20.18.5 activation: every Kernel instance attempts federation
        // bootstrap from env. `init_federation_from_env` is a no-op
        // when NEXUS_HOSTNAME is unset (tests / slim profile) so
        // unit tests aren't affected. Bootstrap failures are logged
        // and the kernel stays up in "federation disabled" mode so a
        // misconfigured NEXUS_PEERS doesn't take the whole process
        // down — gives operators a path to diagnose via sys_stat on
        // `/__sys__/zones/`.
        if let Err(e) = k.init_federation_from_env() {
            tracing::warn!("federation bootstrap from env failed: {:?}", e);
        }
        // Observers registered on-demand (not at Kernel::new()).
        // FileWatchRegistry + StreamEventObservers are registered by orchestrator
        // at boot time to avoid issues in lightweight test contexts.
        k
    }

    // ── Lock Manager wiring ──────────────────────────────────────────

    /// Set VFS lock timeout in milliseconds (default 5000).
    pub fn set_vfs_lock_timeout(&self, timeout_ms: u64) {
        self.vfs_lock_timeout_ms
            .store(timeout_ms, Ordering::Relaxed);
    }

    /// Read current VFS lock timeout (ms).
    #[inline]
    fn vfs_lock_timeout_ms(&self) -> u64 {
        self.vfs_lock_timeout_ms.load(Ordering::Relaxed)
    }

    // ── Node identity (federation content origin) ─────────────────────

    /// Set this node's advertise address for origin-aware metadata.
    ///
    /// When set, `sys_write` encodes `backend_name` as `{name}@{addr}`
    /// so replicated metadata on other nodes knows where to fetch content.
    pub fn set_self_address(&self, addr: &str) {
        *self.self_address.write() = Some(addr.to_string());
    }

    // ── MetaStore wiring ──────────────────────────────────────────────

    /// Wire LocalMetaStore by path — Rust kernel opens redb directly.
    /// Only metastore wiring method (PyMetaStoreAdapter removed in Phase 9).
    pub fn set_metastore_path(&self, path: &str) -> Result<(), KernelError> {
        let ms = LocalMetaStore::open(std::path::Path::new(path))
            .map_err(|e| KernelError::IOError(format!("LocalMetaStore: {e:?}")))?;
        *self.metastore.write() = Some(Box::new(ms));
        Ok(())
    }

    /// Drop the global metastore + every per-mount metastore so the
    /// underlying redb file handles are released. Python ``NexusFS.close``
    /// calls this so a subsequent kernel can reopen the same redb path
    /// without the ``"Database already open"`` error (Issue #3765 Cat-5/6
    /// SQLite-lifecycle regression).
    pub fn release_metastores(&self) {
        *self.metastore.write() = None;
        // Drop per-mount metastores by clearing their slot on each
        // MountEntry. We iterate via `iter_mut` to avoid a full rebuild.
        for mut entry in self.vfs_router.entries_iter_mut() {
            entry.metastore = None;
        }
    }

    /// Atomic metadata commit — propose to metastore first, update
    /// dcache only on success.
    ///
    /// Replaces the legacy "best-effort metastore put + eager dcache
    /// update" pattern that scattered across 10+ sys_* paths. That
    /// pattern silently lost data on raft propose failure: leader's
    /// dcache held the entry but it was never committed to the
    /// state machine, so followers caught up to leader's
    /// applied_index without ever seeing the file
    /// (TestPartialReplicationFailure::test_partition_then_heal CI
    /// regression — see PR #3890 for the full diagnostic).
    ///
    /// Architecture:
    ///   - MetaStore is the SSOT. dcache is a downstream cache.
    ///   - For federation mounts (`ZoneMetaStore`), `put` blocks
    ///     until raft commits the entry on quorum. If the propose
    ///     times out (e.g., quorum unreachable), this returns Err
    ///     and the dcache stays consistent with the state machine
    ///     (i.e., file does NOT appear in subsequent reads).
    ///   - For standalone mounts (`LocalMetaStore`), `put` is a
    ///     synchronous redb write — same atomicity story, smaller
    ///     latency budget.
    ///
    /// Perf:
    ///   - Federation: caller waits one raft RTT per write. Same as
    ///     the implicit cost of "successful raft commit"; the old
    ///     pattern only made it look free by lying.
    ///   - Standalone: redb fsync, microseconds.
    ///   - All other state mutations (dcache update, observer
    ///     dispatch) wait for commit. No double-bookkeeping.
    pub(crate) fn commit_metadata(
        &self,
        path: &str,
        mount_point: &str,
        meta: crate::meta_store::FileMetadata,
    ) -> Result<(), KernelError> {
        let cache_entry: CachedEntry = (&meta).into();
        let put_result = self
            .with_metastore(mount_point, move |ms| ms.put(path, meta))
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "commit_metadata({path}): no metastore wired for mount {mount_point}"
                ))
            })?;
        put_result.map_err(|e| {
            KernelError::IOError(format!("commit_metadata({path}): metastore.put: {e:?}"))
        })?;
        self.dcache.put(path, cache_entry);
        Ok(())
    }

    /// Atomic metadata delete — same pattern as `commit_metadata`
    /// but for the unlink path. Removes from metastore first; on
    /// success evicts dcache. Failure leaves dcache untouched so a
    /// retry sees the still-present entry instead of a phantom miss.
    pub(crate) fn commit_delete(&self, path: &str, mount_point: &str) -> Result<bool, KernelError> {
        let del_result = self
            .with_metastore(mount_point, move |ms| ms.delete(path))
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "commit_delete({path}): no metastore wired for mount {mount_point}"
                ))
            })?;
        let removed = del_result.map_err(|e| {
            KernelError::IOError(format!("commit_delete({path}): metastore.delete: {e:?}"))
        })?;
        self.dcache.evict(path);
        Ok(removed)
    }

    /// Resolve metastore for a syscall: per-mount first, then global fallback.
    ///
    /// In federation mode each mount has its own state machine (Raft-backed
    /// zone store). Standalone mode uses a single global metastore.
    /// `mount_point` must be the zone-canonical key from `vfs_router.route()`.
    pub(crate) fn with_metastore<F, R>(&self, mount_point: &str, f: F) -> Option<R>
    where
        F: FnOnce(&dyn crate::meta_store::MetaStore) -> R,
    {
        // Hold the DashMap read guard only long enough to snapshot the
        // `Arc<dyn MetaStore>`, then release it before running the closure
        // — avoids pinning the shard for the duration of a Raft propose.
        if let Some(entry) = self.vfs_router.get_canonical(mount_point) {
            if let Some(ms) = entry.metastore.as_ref() {
                let ms_arc = Arc::clone(ms);
                drop(entry);
                return Some(f(ms_arc.as_ref()));
            }
        }
        self.metastore.read().as_ref().map(|ms| f(ms.as_ref()))
    }

    // ── MetaStore routing ────────────────────────────────────────────
    //
    // R20.3: the metastore abstraction owns key translation. Callers
    // pass full global paths; per-mount ``ZoneMetaStore`` impls translate
    // to their zone-relative storage on the way in and back on the way
    // out. The global fallback ``LocalMetaStore`` stores full paths
    // directly. There is no longer a kernel-side "is per-mount"
    // branch — we just resolve the right metastore and forward.

    /// Resolve the canonical mount point for a global path.
    ///
    /// Returns ``""`` when no mount covers the path (caller decides
    /// whether to fall back to the global metastore).
    fn resolve_mount_point(&self, path: &str, zone_id: &str) -> String {
        self.vfs_router
            .route(path, zone_id)
            .map(|r| r.mount_point)
            .unwrap_or_default()
    }

    /// Build a `FileMetadata` record for `path` under the given zone, with
    /// every other field supplied by the caller.
    ///
    /// R20.16.7: DRY helper for the ~10 write paths that persist inode
    /// records (sys_write, sys_mkdir, rename destination, pipe/stream
    /// registration, batch write, …). `zone_id` is the destination zone —
    /// callers pass `&route.zone_id` or an explicit zone (e.g.
    /// `contracts::ROOT_ZONE_ID` for kernel-internal IPC inodes). The
    /// matching `CachedEntry` derives via `(&meta).into()`.
    ///
    /// `last_writer_address` is auto-filled from `self.self_address`
    /// (the kernel's own RPC address); reads on remote nodes use it to
    /// route to the originating node when the local mount table misses.
    #[allow(clippy::too_many_arguments)]
    fn build_metadata(
        &self,
        path: &str,
        zone_id: &str,
        entry_type: u8,
        size: u64,
        etag: Option<String>,
        version: u32,
        mime_type: Option<String>,
        created_at_ms: Option<i64>,
        modified_at_ms: Option<i64>,
    ) -> crate::meta_store::FileMetadata {
        crate::meta_store::FileMetadata {
            path: path.to_string(),
            size,
            etag,
            version,
            entry_type,
            zone_id: Some(zone_id.to_string()),
            mime_type,
            created_at_ms,
            modified_at_ms,
            last_writer_address: self.self_address.read().clone(),
        }
    }

    /// Compute zone-relative metastore key from a route's `backend_path`.
    ///
    /// Still used by backend / dcache code paths that need the
    /// zone-namespace key even though the metastore no longer does.
    #[inline]
    fn zone_key(backend_path: &str) -> String {
        if backend_path.is_empty() {
            contracts::VFS_ROOT.to_string()
        } else {
            format!("/{}", backend_path)
        }
    }

    // ── MetaStore proxy methods (for Python RustMetastoreProxy) ────────
    //
    // F2 C8: these route via ``vfs_router.route(path, ROOT_ZONE_ID, ...)`` so a
    // lookup under a federation mount (e.g. ``/corp/eng/foo.txt``) lands on
    // the corresponding per-mount ``ZoneMetaStore`` installed by
    // ``attach_raft_zone_to_kernel``. Without this, every Python-side
    // RustMetastoreProxy call went to the global kernel metastore and
    // federation data was invisible on follower nodes.
    //
    // R7: keys are now zone-relative (backend_path from route, prefixed
    // with `/`). Callers pass global paths; these methods translate.

    pub fn metastore_get(
        &self,
        path: &str,
    ) -> Result<Option<crate::meta_store::FileMetadata>, KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.get(path)) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_get({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_put(
        &self,
        path: &str,
        mut metadata: crate::meta_store::FileMetadata,
    ) -> Result<(), KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        metadata.path = path.to_string();
        match self.with_metastore(&mount_point, move |ms| ms.put(path, metadata)) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_put({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_delete(&self, path: &str) -> Result<bool, KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.delete(path)) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_delete({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_list(
        &self,
        prefix: &str,
    ) -> Result<Vec<crate::meta_store::FileMetadata>, KernelError> {
        let route_path = if prefix.is_empty() {
            contracts::VFS_ROOT
        } else {
            prefix
        };
        let global_prefix = if prefix.is_empty() {
            contracts::VFS_ROOT.to_string()
        } else {
            prefix.to_string()
        };
        let routed_mount = self.resolve_mount_point(route_path, contracts::ROOT_ZONE_ID);

        let mut results: Vec<crate::meta_store::FileMetadata> = match self
            .with_metastore(&routed_mount, |ms| ms.list(&global_prefix))
        {
            Some(result) => result
                .map_err(|e| KernelError::IOError(format!("metastore_list({prefix}): {e:?}")))?,
            None => return Err(KernelError::IOError("no metastore wired".into())),
        };

        // F2 C5 follow-up: when the user-facing prefix spans MULTIPLE mounts
        // (e.g. prefix=`/personal/` with a mount at `/personal/alice`), the
        // routed metastore above only returns entries rooted on the parent
        // mount. Merge in each child mount's own per-mount metastore so the
        // caller sees the full subtree — including the mount roots themselves,
        // which each metastore stores under its own mount-point key.
        let user_prefix = if prefix.is_empty() {
            contracts::VFS_ROOT.to_string()
        } else if prefix.ends_with('/') {
            prefix.to_string()
        } else {
            format!("{}/", prefix)
        };
        let user_prefix_trim = if user_prefix == contracts::VFS_ROOT {
            ""
        } else {
            user_prefix.trim_end_matches('/')
        };
        for canonical in self.vfs_router.canonical_keys() {
            if canonical == routed_mount {
                continue;
            }
            let (_zone, user_mp) = crate::vfs_router::extract_zone_from_canonical(&canonical);
            // Child mount must sit strictly under the list prefix. Root list
            // (`/`) sees every mount. Non-root prefix `/a` matches `/a/b` but
            // not `/a` itself (caller already has the DT_MOUNT entry from the
            // parent metastore, or gets it via a separate sys_stat).
            let under_prefix = if user_prefix == contracts::VFS_ROOT {
                user_mp != contracts::VFS_ROOT
            } else {
                user_mp.starts_with(&user_prefix)
                    || user_mp == user_prefix_trim.to_string().as_str()
            };
            if !under_prefix {
                continue;
            }
            // R20.3: ask the child metastore to list its own full-path
            // root; it translates internally. Returned entries already
            // carry full global paths, so no post-hoc translation needed.
            if let Some(Ok(child_entries)) = self.with_metastore(&canonical, |ms| ms.list(&user_mp))
            {
                for meta in child_entries {
                    // Deduplicate — parent metastore may also carry a stub
                    // DT_DIR entry for the mount point path.
                    if !results.iter().any(|m| m.path == meta.path) {
                        results.push(meta);
                    }
                }
            }
        }
        Ok(results)
    }

    pub fn metastore_exists(&self, path: &str) -> Result<bool, KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.exists(path)) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_exists({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_get_batch(
        &self,
        paths: &[String],
    ) -> Result<Vec<Option<crate::meta_store::FileMetadata>>, KernelError> {
        match self.metastore.read().as_ref() {
            Some(ms) => ms
                .get_batch(paths)
                .map_err(|e| KernelError::IOError(format!("metastore_get_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    // Called by PyKernel.metastore_delete_batch() via PyO3 — no direct Rust caller.
    #[allow(dead_code)]
    pub fn metastore_delete_batch(&self, paths: &[String]) -> Result<usize, KernelError> {
        match self.metastore.read().as_ref() {
            Some(ms) => ms
                .delete_batch(paths)
                .map_err(|e| KernelError::IOError(format!("metastore_delete_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_put_batch(
        &self,
        items: &[(String, crate::meta_store::FileMetadata)],
    ) -> Result<(), KernelError> {
        match self.metastore.read().as_ref() {
            Some(ms) => ms
                .put_batch(items)
                .map_err(|e| KernelError::IOError(format!("metastore_put_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    /// OCC put. See `MetaStore::put_if_version`.
    pub fn metastore_put_if_version(
        &self,
        mut metadata: crate::meta_store::FileMetadata,
        expected_version: u32,
    ) -> Result<crate::meta_store::PutIfVersionResult, KernelError> {
        let path = metadata.path.clone();
        let mount_point = self.resolve_mount_point(&path, contracts::ROOT_ZONE_ID);
        // Metadata.path stays at the full global path — ZoneMetaStore
        // translates internally now.
        metadata.path = path.clone();
        match self.with_metastore(&mount_point, move |ms| {
            ms.put_if_version(metadata, expected_version)
        }) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!("metastore_put_if_version({path}): {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    /// Rename `old_path` → `new_path` (and prefix children). See
    /// `MetaStore::rename_path`.
    pub fn metastore_rename_path(&self, old_path: &str, new_path: &str) -> Result<(), KernelError> {
        let old_mp = self.resolve_mount_point(old_path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&old_mp, |ms| ms.rename_path(old_path, new_path)) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!(
                    "metastore_rename_path({old_path} → {new_path}): {e:?}"
                ))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_set_file_metadata(
        &self,
        path: &str,
        key: &str,
        value: String,
    ) -> Result<(), KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, move |ms| {
            ms.set_file_metadata(path, key, value)
        }) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!("metastore_set_file_metadata({path}, {key}): {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_get_file_metadata(
        &self,
        path: &str,
        key: &str,
    ) -> Result<Option<String>, KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.get_file_metadata(path, key)) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!("metastore_get_file_metadata({path}, {key}): {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_get_file_metadata_bulk(
        &self,
        paths: &[String],
        key: &str,
    ) -> Result<Vec<crate::meta_store::PathValueStr>, KernelError> {
        // Bulk: fan out to the global metastore. Mixed-mount bulk reads
        // go through the Python wrapper.
        match self.metastore.read().as_ref() {
            Some(ms) => ms.get_file_metadata_bulk(paths, key).map_err(|e| {
                KernelError::IOError(format!("metastore_get_file_metadata_bulk: {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_is_implicit_directory(&self, path: &str) -> Result<bool, KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.is_implicit_directory(path)) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!("metastore_is_implicit_directory({path}): {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_list_paginated(
        &self,
        prefix: &str,
        recursive: bool,
        limit: usize,
        cursor: Option<&str>,
    ) -> Result<crate::meta_store::PaginatedList, KernelError> {
        let route_path = if prefix.is_empty() {
            contracts::VFS_ROOT
        } else {
            prefix
        };
        let list_prefix = if prefix.is_empty() {
            contracts::VFS_ROOT
        } else {
            prefix
        };
        let mount_point = self.resolve_mount_point(route_path, contracts::ROOT_ZONE_ID);
        // Cursor is a metastore-internal key, pass as-is.
        match self.with_metastore(&mount_point, |ms| {
            ms.list_paginated(list_prefix, recursive, limit, cursor)
        }) {
            Some(result) => result.map_err(|e| {
                KernelError::IOError(format!("metastore_list_paginated({prefix}): {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_batch_get_content_ids(
        &self,
        paths: &[String],
    ) -> Result<Vec<crate::meta_store::PathEtag>, KernelError> {
        match self.metastore.read().as_ref() {
            Some(ms) => ms.batch_get_content_ids(paths).map_err(|e| {
                KernelError::IOError(format!("metastore_batch_get_content_ids: {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    // ── Advisory lock primitive (§4.4) ──────────────────────────
    // (Moved to `kernel::locks` submodule — Phase G of Phase 3 restructure.)

    // ── DCache proxy methods ───────────────────────────────────────────

    /// Insert or update a cache entry.
    #[allow(clippy::too_many_arguments)]
    pub fn dcache_put(
        &self,
        path: &str,
        size: u64,
        entry_type: u8,
        version: u32,
        etag: Option<&str>,
        zone_id: Option<&str>,
        mime_type: Option<&str>,
        last_writer_address: Option<&str>,
    ) {
        self.dcache.put(
            path,
            CachedEntry {
                size,
                etag: etag.map(|s| s.to_string()),
                version,
                entry_type,
                zone_id: zone_id.map(|s| s.to_string()),
                mime_type: mime_type.map(|s| s.to_string()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: last_writer_address.map(|s| s.to_string()),
            },
        );
    }

    /// Put a pre-built CachedEntry into the dcache. Used by DLC.mount().
    pub(crate) fn dcache_put_entry(&self, path: &str, entry: CachedEntry) {
        self.dcache.put(path, entry);
    }

    /// Get hot-path tuple: (entry_type, last_writer_address).
    pub fn dcache_get(&self, path: &str) -> Option<(u8, Option<String>)> {
        self.dcache.get_hot(path)
    }

    /// Get full entry (returns CachedEntry for wrapper to convert).
    pub(crate) fn dcache_get_full(&self, path: &str) -> Option<CachedEntry> {
        self.dcache.get_entry(path)
    }

    /// Evict a single path.
    pub fn dcache_evict(&self, path: &str) -> bool {
        self.dcache.evict(path)
    }

    /// Clone the shared DCache ``Arc`` for federation apply-event
    /// callbacks. Consumer holds its own reference so the callback
    /// stays valid even if the kernel's invoking call frame has
    /// returned — the cache itself lives as long as *any* holder.
    #[allow(dead_code)]
    pub(crate) fn dcache_handle(&self) -> Arc<DCache> {
        Arc::clone(&self.dcache)
    }

    /// Clone the shared VFSRouter ``Arc`` for federation apply-event
    /// callbacks that need to look up mount-points-for-zone at
    /// invalidation time. See ``dcache_handle`` for the lifetime
    /// rationale — same contract.
    #[allow(dead_code)]
    pub(crate) fn vfs_router_handle(&self) -> Arc<VFSRouter> {
        Arc::clone(&self.vfs_router)
    }

    /// Evict all entries with given prefix.
    pub fn dcache_evict_prefix(&self, prefix: &str) -> usize {
        self.dcache.evict_prefix(prefix)
    }

    /// Check if path exists in cache.
    pub fn dcache_contains(&self, path: &str) -> bool {
        self.dcache.contains(path)
    }

    /// Return cache statistics.
    pub fn dcache_stats(&self) -> DcacheStats {
        let (hits, misses, size) = self.dcache.stats();
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };
        DcacheStats {
            hits,
            misses,
            size,
            hit_rate,
        }
    }

    /// Clear all entries and reset counters.
    pub fn dcache_clear(&self) {
        self.dcache.clear();
    }

    /// Number of entries in dcache.
    pub fn dcache_len(&self) -> usize {
        self.dcache.len()
    }

    // ── Router proxy methods ───────────────────────────────────────────

    /// Register a mount point.
    ///
    /// Backend resolution:
    ///   - `backend` provided → uses it directly.
    ///   - `backend` is None → no backend (sys_read returns miss).
    ///
    /// Caller provides an optional pre-built `MetaStore` impl (e.g.
    /// `LocalMetaStore` for standalone, `ZoneMetaStore` for federation).
    /// Kernel just installs it — it doesn't know or care which impl.
    ///
    /// When `raft_backend` is `Some` **and** `zone_id` is the root zone,
    /// the kernel automatically upgrades its `LockManager` to distributed
    /// mode (federation DI).
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
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
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
        // Boot-order fix: on restart, `reconcile_mounts_from_zones` runs
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
        // Federation DI: the presence of `raft_backend` on a mount means
        // the mount is cross-node replicated (a ZoneConsensus is attached).
        // Once ANY replicated mount lands in the kernel, locks must also
        // become cross-node — otherwise sys_lock on a federated path only
        // sees each node's local BTreeMap and every node accepts the same
        // acquire.
        //
        // LockManager's upgrade is first-wins (idempotent), so the first
        // replicated mount on each peer picks the backend zone. Because
        // federation topology is replicated + applied in a deterministic
        // order on every peer, all nodes converge on the same zone for
        // lock state. Subsequent mounts keep that backend so live lock
        // state never migrates between state machines.
        //
        // Previously this was gated on `zone_id == ROOT_ZONE_ID`, but
        // Python-driven federation bootstrap never calls add_mount for
        // the root zone itself — it only mounts non-root zones under `/`
        // — so that branch never fired in production and the whole
        // cluster stayed in local-lock mode.
        if let Some((node, runtime)) = raft_backend {
            self.install_federation_locks(node, runtime);
        }
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

    /// Install the apply-side dcache invalidation callback for a
    /// federation mount (R20.6 option B — coherence-key fanout).
    ///
    /// Fires on every committed metadata mutation on ``consensus``'s
    /// state machine — evicts the corresponding DCache entry on every
    /// current mount whose metastore reports the same ``coherence_key``
    /// (direct mount + every crosslink). Without this, nodes that
    /// didn't originate a write (leader-forwarded follower writes,
    /// catch-up replication) keep serving stale ``sys_stat`` /
    /// ``sys_read`` from their local dcache after raft applies the
    /// new state — a textbook distributed-cache-coherence hole.
    ///
    /// Why coherence_key and not Arc identity: R20.3 gave every
    /// crosslink its own ``ZoneMetaStore`` Arc (different
    /// ``mount_point``), so Arc::ptr_eq groups just one surface per
    /// zone. ``coherence_key`` is the state-machine Arc's pointer
    /// (same value across every crosslink), so a single invalidate
    /// on the raft side correctly fans out to every VFS surface.
    ///
    /// Install is idempotent: the slot's ``write().replace()`` is fine
    /// because every install for the same state machine captures the
    /// SAME ``coherence_key``, so overwriting is a no-op semantically —
    /// kernel gates further installs via
    /// ``LockManager::locks_installed``-style atomic to avoid the
    /// ``runtime.block_on`` cost, but correctness does not depend on
    /// it.
    fn install_federation_dcache_coherence(
        &self,
        consensus: nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
    ) {
        install_federation_dcache_coherence_impl(&self.vfs_router, &self.dcache, &consensus);
    }

    /// Syscall: set attributes on a path. Handles ALL filesystem entry types.
    ///
    /// - `entry_type == 2` (DT_MOUNT) → DLC mount lifecycle
    /// - `entry_type == 3` (DT_PIPE) → create pipe buffer
    /// - `entry_type == 4` (DT_STREAM) → create stream buffer
    /// - `entry_type == 1` (DT_DIR) → create directory inode
    /// - `entry_type == 0` (UPDATE/IDEMPOTENT) → update mutable fields or no-op
    ///
    /// `/__sys__/` paths are dispatched by Python BEFORE reaching Rust.
    #[allow(clippy::too_many_arguments)]
    pub fn sys_setattr(
        &self,
        path: &str,
        entry_type: i32,
        // -- DT_MOUNT params (entry_type == 2) --
        backend_name: &str,
        backend: Option<Arc<dyn crate::abc::object_store::ObjectStore>>,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
        io_profile: &str,
        zone_id: &str,
        // -- DT_MOUNT is_external flag (entry_type == 2) --
        is_external: bool,
        // -- DT_PIPE/DT_STREAM params (entry_type == 3, 4) --
        capacity: usize,
        // -- DT_PIPE stdio params (io_profile == "stdio") --
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        // -- UPDATE params (entry_type == 0) --
        mime_type: Option<&str>,
        modified_at_ms: Option<i64>,
    ) -> Result<SysSetAttrResult, KernelError> {
        match entry_type {
            2 => {
                // DT_MOUNT — full mount lifecycle via DLC.
                //
                // R20.6 option B: snapshot the raft handle BEFORE it's
                // consumed by ``dlc.mount`` so we can install the
                // apply-side dcache coherence callback after routing is
                // wired. Install is keyed on the state machine's
                // ``coherence_id``, not on the per-mount MetaStore Arc,
                // so crosslinks of the same zone share one callback
                // that fans out across every surface via VFSRouter's
                // reverse lookup.
                //
                // R20.18.3: zone-create-on-mount. If the caller didn't
                // supply metastore + raft_backend (neither `py_zone_handle`
                // nor `metastore_path` in the Python shim) AND federation
                // is active (zone_manager installed by
                // `init_federation_from_env`), auto-resolve: ensure the
                // zone's raft group exists on this node, then build a
                // ZoneMetaStore over it. This replaces the Python
                // `_mount_via_kernel` chain — every DT_MOUNT becomes a
                // federation-wired mount when federation is active,
                // without any Python-side ZoneManager orchestration.
                let (metastore, raft_backend) =
                    self.resolve_federation_mount_backing(zone_id, path, metastore, raft_backend)?;

                let consensus_for_cb = raft_backend.as_ref().map(|(c, _)| c.clone());
                self.dlc.mount(
                    self,
                    path,
                    zone_id,
                    backend_name,
                    backend,
                    metastore,
                    raft_backend,
                    is_external,
                )?;
                if let Some(consensus) = consensus_for_cb {
                    self.install_federation_dcache_coherence(consensus);
                }
                Ok(SysSetAttrResult {
                    path: path.to_string(),
                    created: true,
                    entry_type,
                    backend_name: Some(backend_name.to_string()),
                    capacity: None,
                    updated: Vec::new(),
                    shm_path: None,
                    data_rd_fd: None,
                    space_rd_fd: None,
                })
            }
            3 => {
                // DT_PIPE — create or idempotent-open
                self.setattr_pipe(path, capacity, io_profile, read_fd, write_fd)
            }
            4 => {
                // DT_STREAM — create or idempotent-open
                self.setattr_stream(path, capacity, io_profile)
            }
            1 => {
                // DT_DIR — create directory inode
                self.setattr_create_dir(path, zone_id)
            }
            0 => {
                // UPDATE or IDEMPOTENT OPEN
                self.setattr_update(path, mime_type, modified_at_ms)
            }
            _ => Err(KernelError::PermissionDenied(format!(
                "sys_setattr: unsupported entry_type={entry_type}"
            ))),
        }
    }

    /// DT_PIPE: create pipe buffer, or idempotent-open if it already exists.
    ///
    /// `io_profile`:
    /// - `"memory"` (default) → MemoryPipeBackend
    /// - `"shared_memory"` → SharedMemoryPipeBackend (mmap, cross-process)
    /// - `"stdio"` → StdioPipeBackend (subprocess fd, newline-framed)
    /// - `"wal"` → WalPipeCore (raft-replicated, cross-node, single-consumer)
    #[allow(unused_variables)]
    fn setattr_pipe(
        &self,
        path: &str,
        capacity: usize,
        io_profile: &str,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Idempotent open: if DT_PIPE already exists, re-create buffer if lost
        if let Some(meta) = self.metastore_get(path).ok().flatten() {
            if meta.entry_type == DT_PIPE {
                if !self.has_pipe(path) {
                    self.create_pipe(path, capacity)?;
                }
                return Ok(SysSetAttrResult {
                    path: path.to_string(),
                    created: false,
                    entry_type: DT_PIPE as i32,
                    backend_name: None,
                    capacity: Some(capacity),
                    updated: Vec::new(),
                    shm_path: None,
                    data_rd_fd: None,
                    space_rd_fd: None,
                });
            }
            return Err(KernelError::PermissionDenied(format!(
                "entry_type immutable (cannot change {} → DT_PIPE)",
                meta.entry_type
            )));
        }

        // Create based on io_profile
        let (shm_path, data_rd_fd, space_rd_fd) = if io_profile == "shared_memory" {
            #[cfg(unix)]
            {
                let (backend, shm, dfd, sfd) =
                    crate::shm_pipe::SharedMemoryPipeBackend::create_native(capacity)?;
                self.pipe_manager
                    .register(path, Arc::new(backend))
                    .map_err(pipe_mgr_err)?;
                self.write_pipe_inode(path, capacity)?;
                (Some(shm), Some(dfd), Some(sfd))
            }
            #[cfg(not(unix))]
            {
                return Err(KernelError::IOError(
                    "shared_memory pipes require unix".into(),
                ));
            }
        } else if io_profile == "stdio" {
            #[cfg(unix)]
            {
                let rfd = read_fd.unwrap_or(-1);
                let wfd = write_fd.unwrap_or(-1);
                let backend = crate::stdio_pipe::StdioPipeBackend::new(rfd, wfd);
                self.pipe_manager
                    .register(path, Arc::new(backend))
                    .map_err(pipe_mgr_err)?;
                self.write_pipe_inode(path, capacity)?;
                (None, None, None)
            }
            #[cfg(not(unix))]
            {
                return Err(KernelError::IOError("stdio pipes require unix".into()));
            }
        } else if io_profile == "wal" {
            // Raft-replicated DT_PIPE — mirrors the `setattr_stream`
            // wal branch. Single-consumer semantics (each replica owns
            // its head cursor); see `core/pipe/wal.rs` for the contract.
            let zm = self.zone_manager_arc().ok_or_else(|| {
                KernelError::IOError("io_profile=wal requires federation (set NEXUS_PEERS)".into())
            })?;
            let root_zone = "root";
            let consensus = zm.registry().get_node(root_zone).ok_or_else(|| {
                KernelError::IOError(format!("io_profile=wal: zone {root_zone} not loaded"))
            })?;
            let runtime = zm.runtime_handle();
            let wal_consensus: Arc<dyn crate::wal_stream::WalConsensus> =
                Arc::new(crate::wal_stream::RaftWalConsensus::new(consensus, runtime));
            let backend = crate::core::pipe::wal::WalPipeCore::new(wal_consensus, path.to_string());
            self.pipe_manager
                .register(path, Arc::new(backend))
                .map_err(pipe_mgr_err)?;
            self.write_pipe_inode(path, capacity)?;
            (None, None, None)
        } else {
            self.create_pipe(path, capacity)?;
            (None, None, None)
        };

        Ok(SysSetAttrResult {
            path: path.to_string(),
            created: true,
            entry_type: DT_PIPE as i32,
            backend_name: None,
            capacity: Some(capacity),
            updated: Vec::new(),
            shm_path,
            data_rd_fd,
            space_rd_fd,
        })
    }

    /// DT_STREAM: create stream buffer, or idempotent-open if it already exists.
    fn setattr_stream(
        &self,
        path: &str,
        capacity: usize,
        io_profile: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        if let Some(meta) = self.metastore_get(path).ok().flatten() {
            if meta.entry_type == DT_STREAM {
                if !self.has_stream(path) {
                    self.create_stream(path, capacity)?;
                }
                return Ok(SysSetAttrResult {
                    path: path.to_string(),
                    created: false,
                    entry_type: DT_STREAM as i32,
                    backend_name: None,
                    capacity: Some(capacity),
                    updated: Vec::new(),
                    shm_path: None,
                    data_rd_fd: None,
                    space_rd_fd: None,
                });
            }
            return Err(KernelError::PermissionDenied(format!(
                "entry_type immutable (cannot change {} → DT_STREAM)",
                meta.entry_type
            )));
        }

        let (shm_path, data_rd_fd) = if io_profile == "shared_memory" {
            #[cfg(unix)]
            {
                let (backend, shm, dfd) =
                    crate::shm_stream::SharedMemoryStreamBackend::create_native(capacity)?;
                self.stream_manager
                    .register(path, Arc::new(backend))
                    .map_err(stream_mgr_err)?;
                self.write_stream_inode(path, capacity)?;
                (Some(shm), Some(dfd))
            }
            #[cfg(not(unix))]
            {
                return Err(KernelError::IOError(
                    "shared_memory streams require unix".into(),
                ));
            }
        } else if io_profile == "wal" {
            // R20.18.6: raft-backed durable stream. Previously constructed
            // via the deleted `WalStreamBackend` pyclass; now the kernel
            // picks the zone's consensus off `zone_manager_arc()` directly
            // so no ZoneHandle crosses the PyO3 boundary.
            let zm = self.zone_manager_arc().ok_or_else(|| {
                KernelError::IOError("io_profile=wal requires federation (set NEXUS_PEERS)".into())
            })?;
            // Stream inode lives inside the zone whose raft group we use —
            // for now we wire against the root zone (the only zone Python
            // currently writes DT_STREAM into). When streams need to
            // follow the mount tree, swap this for VFSRouter::route(path).
            let root_zone = "root";
            let consensus = zm.registry().get_node(root_zone).ok_or_else(|| {
                KernelError::IOError(format!("io_profile=wal: zone {root_zone} not loaded"))
            })?;
            let runtime = zm.runtime_handle();
            let wal_consensus: Arc<dyn crate::wal_stream::WalConsensus> =
                Arc::new(crate::wal_stream::RaftWalConsensus::new(consensus, runtime));
            let backend = crate::wal_stream::WalStreamCore::new(wal_consensus, path.to_string());
            self.stream_manager
                .register(path, Arc::new(backend))
                .map_err(stream_mgr_err)?;
            self.write_stream_inode(path, capacity)?;
            (None, None)
        } else {
            self.create_stream(path, capacity)?;
            (None, None)
        };

        Ok(SysSetAttrResult {
            path: path.to_string(),
            created: true,
            entry_type: DT_STREAM as i32,
            backend_name: None,
            capacity: Some(capacity),
            updated: Vec::new(),
            shm_path,
            data_rd_fd,
            space_rd_fd: None,
        })
    }

    /// Write DT_PIPE inode to metastore + dcache (shared by create_pipe and SHM path).
    #[allow(dead_code)]
    fn write_pipe_inode(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_PIPE,
            capacity as u64,
            None,
            1,
            None,
            None,
            None,
        );
        self.commit_metadata(path, &mount_point, meta)
    }

    /// Write DT_STREAM inode to metastore + dcache (shared by create_stream and SHM path).
    #[allow(dead_code)]
    fn write_stream_inode(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        let mount_point = self.resolve_mount_point(path, contracts::ROOT_ZONE_ID);
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_STREAM,
            capacity as u64,
            None,
            1,
            None,
            None,
            None,
        );
        self.commit_metadata(path, &mount_point, meta)
    }

    /// DT_DIR: create directory inode via metastore + dcache.
    fn setattr_create_dir(
        &self,
        path: &str,
        zone_id: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Route first to locate the right per-mount metastore.
        let mount_point = self.resolve_mount_point(path, zone_id);

        // Idempotent: if DT_DIR (or DT_MOUNT, which is directory-like since
        // a mount point IS a directory) already exists, no-op. This matches
        // ``mkdir(exist_ok=True)`` semantics — a mount creates the directory
        // slot, so a follow-up mkdir on the same path shouldn't fail.
        let existing = self
            .with_metastore(&mount_point, |ms| ms.get(path).ok().flatten())
            .flatten();
        if let Some(meta) = existing {
            if meta.entry_type == DT_DIR || meta.entry_type == DT_MOUNT {
                return Ok(SysSetAttrResult {
                    path: path.to_string(),
                    created: false,
                    entry_type: meta.entry_type as i32,
                    backend_name: None,
                    capacity: None,
                    updated: Vec::new(),
                    shm_path: None,
                    data_rd_fd: None,
                    space_rd_fd: None,
                });
            }
            return Err(KernelError::PermissionDenied(format!(
                "entry_type immutable (cannot change {} → DT_DIR)",
                meta.entry_type
            )));
        }

        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);

        let meta = self.build_metadata(
            path,
            zone_id,
            DT_DIR,
            0,
            Some(contracts::BLAKE3_EMPTY.to_string()),
            1,
            Some("inode/directory".to_string()),
            Some(now_ms),
            Some(now_ms),
        );
        // Atomic commit — metastore (raft) first, dcache on success.
        self.commit_metadata(path, &mount_point, meta)?;

        Ok(SysSetAttrResult {
            path: path.to_string(),
            created: true,
            entry_type: DT_DIR as i32,
            backend_name: None,
            capacity: None,
            updated: Vec::new(),
            shm_path: None,
            data_rd_fd: None,
            space_rd_fd: None,
        })
    }

    /// UPDATE or IDEMPOTENT OPEN: modify mutable fields on existing inode.
    fn setattr_update(
        &self,
        path: &str,
        mime_type: Option<&str>,
        modified_at_ms: Option<i64>,
    ) -> Result<SysSetAttrResult, KernelError> {
        let existing = self.metastore_get(path)?;
        let meta = existing.ok_or_else(|| KernelError::FileNotFound(path.to_string()))?;

        // No fields to update → idempotent open (no-op)
        if mime_type.is_none() && modified_at_ms.is_none() {
            return Ok(SysSetAttrResult {
                path: path.to_string(),
                created: false,
                entry_type: meta.entry_type as i32,
                backend_name: None,
                capacity: None,
                updated: Vec::new(),
                shm_path: None,
                data_rd_fd: None,
                space_rd_fd: None,
            });
        }

        // Update mutable fields
        let mut updated_fields = Vec::new();
        let mut new_meta = meta;
        if let Some(mt) = mime_type {
            new_meta.mime_type = Some(mt.to_string());
            updated_fields.push("mime_type".to_string());
        }
        if let Some(ms) = modified_at_ms {
            new_meta.modified_at_ms = Some(ms);
            updated_fields.push("modified_at_ms".to_string());
        }

        self.metastore_put(path, new_meta)?;

        Ok(SysSetAttrResult {
            path: path.to_string(),
            created: false,
            entry_type: 0,
            backend_name: None,
            capacity: None,
            updated: updated_fields,
            shm_path: None,
            data_rd_fd: None,
            space_rd_fd: None,
        })
    }

    // ── Trie proxy methods ─────────────────────────────────────────────

    /// Register a path pattern with a resolver index.
    pub fn trie_register(&self, pattern: &str, resolver_idx: usize) -> Result<(), KernelError> {
        self.trie
            .register(pattern, resolver_idx)
            .map_err(KernelError::TrieError)
    }

    /// Remove a resolver by index.
    pub fn trie_unregister(&self, resolver_idx: usize) -> bool {
        self.trie.unregister(resolver_idx)
    }

    /// Lookup a concrete path.
    pub fn trie_lookup(&self, path: &str) -> Option<usize> {
        self.trie.lookup(path)
    }

    /// Number of registered trie patterns.
    pub fn trie_len(&self) -> usize {
        self.trie.len()
    }

    // ── Hook counts ────────────────────────────────────────────────────

    /// Update hook count for an operation.
    pub fn set_hook_count(&self, op: &str, count: u64) {
        match op {
            "read" => self.read_hook_count.store(count, Ordering::Relaxed),
            "write" => self.write_hook_count.store(count, Ordering::Relaxed),
            "stat" => self.stat_hook_count.store(count, Ordering::Relaxed),
            "delete" => self.delete_hook_count.store(count, Ordering::Relaxed),
            "rename" => self.rename_hook_count.store(count, Ordering::Relaxed),
            "mkdir" => self.mkdir_hook_count.store(count, Ordering::Relaxed),
            "rmdir" => self.rmdir_hook_count.store(count, Ordering::Relaxed),
            "copy" => self.copy_hook_count.store(count, Ordering::Relaxed),
            "access" => self.access_hook_count.store(count, Ordering::Relaxed),
            "write_batch" => self.write_batch_hook_count.store(count, Ordering::Relaxed),
            _ => {}
        }
    }

    /// Check if hooks are registered for an operation (lock-free).
    pub fn has_hooks(&self, op: &str) -> bool {
        match op {
            "read" => self.read_hook_count.load(Ordering::Relaxed) > 0,
            "write" => self.write_hook_count.load(Ordering::Relaxed) > 0,
            "stat" => self.stat_hook_count.load(Ordering::Relaxed) > 0,
            "delete" => self.delete_hook_count.load(Ordering::Relaxed) > 0,
            "rename" => self.rename_hook_count.load(Ordering::Relaxed) > 0,
            "mkdir" => self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
            "rmdir" => self.rmdir_hook_count.load(Ordering::Relaxed) > 0,
            "copy" => self.copy_hook_count.load(Ordering::Relaxed) > 0,
            "access" => self.access_hook_count.load(Ordering::Relaxed) > 0,
            "write_batch" => self.write_batch_hook_count.load(Ordering::Relaxed) > 0,
            _ => false,
        }
    }

    // ── Observer registry (§10 Phase 10 / §11 Phase 2) ────────────────
    // (Moved to `kernel::observability` submodule — Phase G of Phase 3 restructure.)

    // ── Native INTERCEPT hook dispatch (§11 Phase 14) ─────────────────
    // (Moved to `kernel::dispatch` submodule — Phase G of Phase 3 restructure.)

    /// Borrow the kernel's shared tokio runtime.  Phase 4 (full):
    /// kernel owns this Arc directly; peer crates (backends LLM
    /// connectors, transport gRPC server) clone it for their async
    /// work.
    pub fn runtime(&self) -> &Arc<tokio::runtime::Runtime> {
        &self.runtime
    }

    /// Replace the kernel's `peer_client` slot with a concrete
    /// implementation.  Phase 4 (full) DI: kernel boots with
    /// `NoopPeerBlobClient`; the cdylib boot path calls this with
    /// the real `transport::blob::peer_client::PeerBlobClient` once
    /// per kernel.
    pub fn set_peer_client(&self, client: Arc<dyn crate::hal::peer::PeerBlobClient>) {
        *self.peer_client.write() = client;
    }

    /// Borrow the current peer-client trait object — read-locked
    /// snapshot.  Internal callers use this to issue federation
    /// reads without holding the lock across `.await`.
    pub fn peer_client_arc(&self) -> Arc<dyn crate::hal::peer::PeerBlobClient> {
        Arc::clone(&self.peer_client.read())
    }

    /// Replace the kernel's `federation` slot with a concrete
    /// `FederationProvider` impl.  Phase 5 DI: kernel boots with
    /// `NoopFederationProvider`; the cdylib boot path calls this with
    /// the real `nexus_raft::federation_provider` impl once per
    /// kernel.  Mirrors `set_peer_client`.
    pub fn set_federation(&self, fed: Arc<dyn crate::hal::federation::FederationProvider>) {
        *self.federation.write() = fed;
    }

    /// Borrow the current federation provider — read-locked snapshot.
    /// Internal callers use this to issue federation calls without
    /// holding the lock across `.await`.  After `set_federation` runs
    /// (cdylib boot), this returns the real raft-backed impl; before
    /// then, a `NoopFederationProvider` that errors on every call.
    pub fn federation_arc(&self) -> Arc<dyn crate::hal::federation::FederationProvider> {
        Arc::clone(&self.federation.read())
    }

    /// Prepare a WAL-replicated DT_STREAM for audit / observer use.
    ///
    /// Creates a `WalStreamCore` for `stream_path` using the Raft
    /// consensus of `zone_id`, registers the stream with
    /// `StreamManager` (so Python can read audit records via
    /// `sys_read`), and seeds the DT_STREAM inode in DCache + metastore.
    /// Returns the concrete `Arc<WalStreamCore>` so the caller
    /// (typically `services::audit::install`) can build its own hook
    /// impl from the WAL non-blocking write API (`write_nowait`).
    ///
    /// Phase 3 split this out of the old `Kernel::start_audit_hook`
    /// (now lives in `services::audit`).  The kernel half owns only
    /// the stream-lifecycle work (kernel concern); the hook
    /// construction + registration belong to the service.
    ///
    /// Safe to call after `init_federation_from_env` has loaded the
    /// zone.  The `stream_manager.register` step is idempotent — a
    /// second call with the same path is silently ignored.
    pub fn prepare_audit_stream(
        &self,
        zone_id: &str,
        stream_path: &str,
    ) -> Result<Arc<crate::wal_stream::WalStreamCore>, KernelError> {
        let zm = self.zone_manager_arc().ok_or_else(|| {
            KernelError::IOError(
                "prepare_audit_stream: federation not active (set NEXUS_PEERS)".into(),
            )
        })?;
        let consensus = zm.registry().get_node(zone_id).ok_or_else(|| {
            KernelError::IOError(format!("prepare_audit_stream: zone {zone_id} not loaded"))
        })?;
        let runtime = zm.runtime_handle();
        let wal_consensus: Arc<dyn crate::wal_stream::WalConsensus> =
            Arc::new(crate::wal_stream::RaftWalConsensus::new(consensus, runtime));
        let wal_stream = Arc::new(crate::wal_stream::WalStreamCore::new(
            wal_consensus,
            stream_path.to_string(),
        ));
        // Register with StreamManager — ignore Exists (idempotent re-call).
        let _ = self.stream_manager.register(
            stream_path,
            Arc::clone(&wal_stream) as Arc<dyn crate::stream::StreamBackend>,
        );
        // Seed DCache + metastore inode so sys_read can locate the stream.
        let _ = self.write_stream_inode(stream_path, 0);
        Ok(wal_stream)
    }

    // ── Zone revision counter (§10 A2) ────────────────────────────────

    /// Get or create zone revision entry.
    fn zone_entry(&self, zone_id: &str) -> Arc<ZoneRevisionEntry> {
        self.zone_revisions
            .entry(zone_id.to_string())
            .or_insert_with(|| Arc::new(ZoneRevisionEntry::new()))
            .clone()
    }

    /// Increment zone revision (called after successful metastore write).
    /// Returns the new revision value.
    pub fn increment_zone_revision(&self, zone_id: &str) -> u64 {
        let entry = self.zone_entry(zone_id);
        let new_rev = entry.revision.fetch_add(1, Ordering::Relaxed) + 1;
        // Only notify if waiters exist (zero cost on non-waited paths)
        if entry.has_waiters.load(Ordering::Relaxed) > 0 {
            let _guard = entry.mutex.lock();
            entry.condvar.notify_all();
        }
        new_rev
    }

    /// Notify a specific zone revision (monotonic: only updates if greater).
    pub fn notify_zone_revision(&self, zone_id: &str, revision: u64) {
        let entry = self.zone_entry(zone_id);
        // CAS loop for monotonic update
        loop {
            let current = entry.revision.load(Ordering::Relaxed);
            if revision <= current {
                break;
            }
            if entry
                .revision
                .compare_exchange_weak(current, revision, Ordering::Relaxed, Ordering::Relaxed)
                .is_ok()
            {
                break;
            }
        }
        if entry.has_waiters.load(Ordering::Relaxed) > 0 {
            let _guard = entry.mutex.lock();
            entry.condvar.notify_all();
        }
    }

    /// Get current zone revision (0 if unknown).
    pub fn get_zone_revision(&self, zone_id: &str) -> u64 {
        self.zone_revisions
            .get(zone_id)
            .map(|e| e.revision.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    /// Wait until zone revision >= min_revision, or timeout.
    /// Pure Rust condvar wait — zero GIL (caller must release GIL before calling).
    /// Returns true if revision reached, false on timeout.
    pub fn wait_zone_revision(&self, zone_id: &str, min_revision: u64, timeout_ms: u64) -> bool {
        let entry = self.zone_entry(zone_id);
        // Fast check before blocking
        if entry.revision.load(Ordering::Relaxed) >= min_revision {
            return true;
        }
        // Register waiter
        entry.has_waiters.fetch_add(1, Ordering::Relaxed);
        let timeout = std::time::Duration::from_millis(timeout_ms);
        let mut guard = entry.mutex.lock();
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if entry.revision.load(Ordering::Relaxed) >= min_revision {
                entry.has_waiters.fetch_sub(1, Ordering::Relaxed);
                return true;
            }
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                entry.has_waiters.fetch_sub(1, Ordering::Relaxed);
                return false;
            }
            let result = entry.condvar.wait_for(&mut guard, remaining);
            if result.timed_out() && entry.revision.load(Ordering::Relaxed) < min_revision {
                entry.has_waiters.fetch_sub(1, Ordering::Relaxed);
                return false;
            }
        }
    }

    // ── File watch registry (§10 A3) ──────────────────────────────────
    // (Moved to `kernel::observability` submodule — Phase G of Phase 3 restructure.)

    // ── IPC Registry — Pipe + Stream methods ────────────────────────────
    // (Moved to `kernel::ipc` submodule — Phase G of Phase 3 restructure.)

    // ── File I/O syscalls (sys_read / sys_write / sys_stat / sys_unlink /
    //    sys_rename / sys_copy / sys_mkdir / sys_rmdir) ──────────────────
    // (Moved to `kernel::io` submodule — Phase G of Phase 3 restructure.)

    // ── Tier 2 convenience methods ────────────────────────────────────

    /// Fast access check: validate + route + dcache existence (~100ns).
    ///
    /// Returns true if file exists in dcache and path is routable.
    /// Does NOT check metastore (dcache authoritative for hot-path).
    pub fn access(&self, path: &str, zone_id: &str) -> bool {
        if validate_path_fast(path).is_err() {
            return false;
        }
        if self.vfs_router.route(path, zone_id).is_err() {
            return false;
        }
        self.dcache.contains(path)
    }

    // ── Internal batch functions (not Tier 1 syscalls) ────────────────

    /// Internal: batch write — loops sys_write logic for each item.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python `write_batch` method.
    /// Each item is (path, content). Returns Vec<SysWriteResult> with per-item results.
    /// Sorted VFS lock acquisition to avoid deadlocks.
    /// PRE-hooks are NOT dispatched here (caller handles batch pre-hooks).
    pub fn _write_batch(
        &self,
        items: &[(String, Vec<u8>)],
        ctx: &OperationContext,
    ) -> Result<Vec<SysWriteResult>, KernelError> {
        let mut results = Vec::with_capacity(items.len());

        // 1. Validate all paths (fail-fast)
        for (path, _) in items {
            validate_path_fast(path)?;
        }

        // 2. Route all paths (single lock acquisition on mount table via read lock)
        let mut routes = Vec::with_capacity(items.len());
        for (path, _) in items {
            let route = self.vfs_router.route(path, &ctx.zone_id).ok();
            routes.push(route);
        }

        // 3. Sorted VFS lock acquisition for all paths
        let mut lock_handles: Vec<u64> = vec![0; items.len()];
        {
            // Sort indices by path to avoid deadlock
            let mut indices: Vec<usize> = (0..items.len()).collect();
            indices.sort_by(|a, b| items[*a].0.cmp(&items[*b].0));

            for idx in indices {
                if routes[idx].is_some() {
                    lock_handles[idx] = self.lock_manager.blocking_acquire(
                        &items[idx].0,
                        LockMode::Write,
                        self.vfs_lock_timeout_ms(),
                    );
                }
            }
        }

        // 4. Write each item — collect metadata for batch put
        // Tuple: (mount_point, path, FileMetadata) for per-mount metastore support
        let mut batch_meta: Vec<(String, String, crate::meta_store::FileMetadata)> = Vec::new();

        for (i, ((path, content), route_opt)) in items.iter().zip(routes.iter()).enumerate() {
            let route = match route_opt {
                Some(r) => r,
                None => {
                    results.push(SysWriteResult {
                        hit: false,
                        content_id: None,
                        post_hook_needed: false,
                        version: 0,
                        size: 0,
                        is_new: false,
                        old_etag: None,
                        old_size: None,
                        old_version: None,
                        old_modified_at_ms: None,
                    });
                    continue;
                }
            };

            // Lock timeout check
            if lock_handles[i] == 0 {
                results.push(SysWriteResult {
                    hit: false,
                    content_id: None,
                    post_hook_needed: false,
                    version: 0,
                    size: 0,
                    is_new: false,
                    old_etag: None,
                    old_size: None,
                    old_version: None,
                    old_modified_at_ms: None,
                });
                continue;
            }

            // Backend write. ``sys_write_batch`` keeps per-item error
            // semantics: a failure only taints that item's result, not the
            // whole batch. We still surface the full error to the caller by
            // synthesising a backend-error result via ``hit=false`` so the
            // observer/post-hook path doesn't fire. The per-item error is
            // logged for observability but not hoisted to ``Result<..>``.
            // Backend write error (batch variant): collapse to None so the
            // per-item result surfaces as hit=false (observer + post-hook
            // path skipped). Caller inspects ``SysWriteResult.hit`` + retries.
            let write_result = self
                .vfs_router
                .write_content(&route.mount_point, content, &route.backend_path, ctx, 0)
                .unwrap_or_default();

            match write_result {
                Some(wr) => {
                    let batch_old_entry = self.dcache.get_entry(path);
                    let old_version = batch_old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                    let new_version = old_version + 1;

                    // Collect metadata for batch put (instead of N individual puts)
                    let meta = self.build_metadata(
                        path,
                        &route.zone_id,
                        DT_REG,
                        wr.size,
                        Some(wr.content_id.clone()),
                        new_version,
                        None,
                        None,
                        None,
                    );
                    // Defer dcache + metastore commit to step 4b so
                    // we can group raft proposes per mount and mark
                    // each result hit/miss based on the actual
                    // commit outcome rather than eagerly lying.
                    batch_meta.push((route.mount_point.clone(), path.to_string(), meta));

                    results.push(SysWriteResult {
                        hit: true,
                        content_id: Some(wr.content_id),
                        post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0
                            || self.write_batch_hook_count.load(Ordering::Relaxed) > 0,
                        version: new_version,
                        size: wr.size,
                        is_new: batch_old_entry.is_none(),
                        old_etag: batch_old_entry.as_ref().and_then(|e| e.etag.clone()),
                        old_size: batch_old_entry.as_ref().map(|e| e.size),
                        old_version: batch_old_entry.as_ref().map(|e| e.version),
                        old_modified_at_ms: batch_old_entry.as_ref().and_then(|e| e.modified_at_ms),
                    });
                }
                None => {
                    results.push(SysWriteResult {
                        hit: false,
                        content_id: None,
                        post_hook_needed: false,
                        version: 0,
                        size: 0,
                        is_new: false,
                        old_etag: None,
                        old_size: None,
                        old_version: None,
                        old_modified_at_ms: None,
                    });
                }
            }
        }

        // 4b. Atomic per-item commit. Per-mount items go through
        // commit_metadata (raft propose, ms.put then dcache). Global
        // items (no per-mount metastore) collect into a batch put
        // since the global LocalMetaStore can do that as one redb
        // txn — but we still update dcache only after the txn lands.
        // Failures flip the corresponding result entry from
        // hit=true → hit=false so the caller learns which items
        // actually committed.
        if !batch_meta.is_empty() {
            let mut global_items: Vec<(String, crate::meta_store::FileMetadata)> = Vec::new();
            let mut global_idx: Vec<usize> = Vec::new();
            for (idx, (mp, path, meta)) in batch_meta.into_iter().enumerate() {
                let has_per_mount = self
                    .vfs_router
                    .get_canonical(&mp)
                    .map(|e| e.metastore.is_some())
                    .unwrap_or(false);
                if has_per_mount {
                    if let Err(_e) = self.commit_metadata(&path, &mp, meta) {
                        // Mark this batch entry as not-hit so the
                        // caller knows the propose didn't commit.
                        if let Some(r) = results.get_mut(idx) {
                            r.hit = false;
                        }
                    }
                } else {
                    global_items.push((path, meta));
                    global_idx.push(idx);
                }
            }
            if !global_items.is_empty() {
                let dcache_updates: Vec<(String, CachedEntry)> = global_items
                    .iter()
                    .map(|(p, m)| (p.clone(), m.into()))
                    .collect();
                let put_ok = self
                    .metastore
                    .read()
                    .as_ref()
                    .map(|ms| ms.put_batch(&global_items).is_ok())
                    .unwrap_or(false);
                if put_ok {
                    for (p, e) in dcache_updates {
                        self.dcache.put(&p, e);
                    }
                } else {
                    for idx in global_idx {
                        if let Some(r) = results.get_mut(idx) {
                            r.hit = false;
                        }
                    }
                }
            }
        }

        // 5. Release all VFS locks
        for handle in &lock_handles {
            if *handle > 0 {
                self.lock_manager.do_release(*handle);
            }
        }

        Ok(results)
    }

    /// Internal: batch read — parallel reads using rayon.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python `read_bulk` method.
    /// Returns Vec<SysReadResult> with per-path results.
    /// Safe because Kernel is Sync (DashMap + parking_lot).
    pub fn _read_batch(
        &self,
        paths: &[String],
        ctx: &OperationContext,
    ) -> Result<Vec<SysReadResult>, KernelError> {
        use rayon::prelude::*;

        let results: Vec<SysReadResult> = paths
            .par_iter()
            .map(|path| {
                self.sys_read(path, ctx).unwrap_or(SysReadResult {
                    data: None,
                    post_hook_needed: false,
                    content_hash: None,
                    entry_type: 0,
                })
            })
            .collect();

        Ok(results)
    }

    /// Internal: batch delete — full Rust + batch metastore.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python batch delete.
    /// Returns Vec<SysUnlinkResult> with per-path results.
    /// Collects hit=true paths for a single metastore.delete_batch() call.
    pub fn _delete_batch(
        &self,
        paths: &[String],
        ctx: &OperationContext,
    ) -> Result<Vec<SysUnlinkResult>, KernelError> {
        let mut results = Vec::with_capacity(paths.len());

        for path in paths {
            match self.sys_unlink(path, ctx, false) {
                Ok(r) => results.push(r),
                Err(_) => results.push(SysUnlinkResult {
                    hit: false,
                    entry_type: 0,
                    post_hook_needed: false,
                    path: path.clone(),
                    etag: None,
                    size: 0,
                }),
            }
        }

        Ok(results)
    }

    /// List immediate children of a directory path from dcache + metastore.
    ///
    /// When `is_admin` is false and `zone_id` is not ROOT_ZONE_ID, entries
    /// are filtered to only include those belonging to the caller's zone or
    /// the root zone (global namespace).
    ///
    /// Returns Vec of (child_path, entry_type) tuples.
    pub fn readdir(&self, parent_path: &str, zone_id: &str, is_admin: bool) -> Vec<(String, u8)> {
        if validate_path_fast(parent_path).is_err() {
            return Vec::new();
        }
        // Callers pass either "/local" or "/local/" — normalize the trailing
        // slash off before routing so prefix comparisons below don't produce
        // double slashes (which silently return no children).
        let normalized = if parent_path != "/" && parent_path.ends_with('/') {
            parent_path.trim_end_matches('/')
        } else {
            parent_path
        };
        let route = match self.vfs_router.route(normalized, zone_id) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };

        let global_prefix = if normalized == contracts::VFS_ROOT {
            contracts::VFS_ROOT.to_string()
        } else {
            format!("{}/", normalized)
        };

        let needs_zone_filter = !is_admin && zone_id != contracts::ROOT_ZONE_ID;

        // Merge dcache children with per-mount metastore list.
        // Track (entry_type, zone_id) so we can zone-filter at the end.
        let mut seen: std::collections::BTreeMap<String, (u8, Option<String>)> =
            std::collections::BTreeMap::new();
        let parent_for_join = if parent_path == contracts::VFS_ROOT {
            ""
        } else {
            parent_path.trim_end_matches('/')
        };
        for (child, etype, entry_zone) in self.dcache.list_children(&global_prefix) {
            let global = format!("{}/{}", parent_for_join, child);
            seen.insert(global, (etype, entry_zone));
        }

        if let Some(ms_children) =
            self.with_metastore(&route.mount_point, |ms| ms.list(&global_prefix).ok())
        {
            let parent_depth = global_prefix.matches('/').count();
            for meta in ms_children.into_iter().flatten() {
                // Direct children only: same depth as prefix + 1 segment.
                if meta.path.matches('/').count() != parent_depth {
                    continue;
                }
                if !meta.path.starts_with(&global_prefix) {
                    continue;
                }
                seen.entry(meta.path)
                    .or_insert((meta.entry_type, meta.zone_id));
            }
        }

        // Phase 3: Backend list_dir merge (all backend types uniformly).
        // CAS/S3/GCS return Err(NotSupported) → ignored.  Path-local
        // returns disk entries, external connectors return API results.
        // No ABC leak: kernel treats every backend the same.
        if let Ok(backend_entries) = self
            .vfs_router
            .list_dir(&route.mount_point, &route.backend_path)
        {
            for name in backend_entries {
                let is_dir = name.ends_with('/');
                let clean = name.trim_end_matches('/');
                if clean.is_empty() {
                    continue;
                }
                let etype = if is_dir { DT_DIR } else { DT_REG };
                let child_path = format!("{}/{}", parent_for_join, clean);
                seen.entry(child_path)
                    .or_insert((etype, Some(route.zone_id.clone())));
            }
        }

        if needs_zone_filter {
            seen.into_iter()
                .filter(|(_, (_, entry_zone))| {
                    let ez = entry_zone.as_deref().unwrap_or(contracts::ROOT_ZONE_ID);
                    ez == contracts::ROOT_ZONE_ID || ez == zone_id
                })
                .map(|(path, (etype, _))| (path, etype))
                .collect()
        } else {
            seen.into_iter()
                .map(|(path, (etype, _))| (path, etype))
                .collect()
        }
    }

    /// Backend-native directory listing for external mounts.
    ///
    /// Unlike `readdir` (which merges dcache + metastore), this calls the
    /// backend's `list_dir` directly — needed for external connectors
    /// (HN, CLI, X, GDrive, etc.) whose entries are only known to the
    /// live API, not persisted in dcache/metastore.
    ///
    /// Returns entry names (files plain, directories with trailing `/`).
    /// Returns empty Vec on backend NotSupported or mount not found.
    pub fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String> {
        if validate_path_fast(path).is_err() {
            return Vec::new();
        }
        let normalized = if path != "/" && path.ends_with('/') {
            path.trim_end_matches('/')
        } else {
            path
        };
        let route = match self.vfs_router.route(normalized, zone_id) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        self.vfs_router
            .list_dir(&route.mount_point, &route.backend_path)
            .unwrap_or_default()
    }

    // ── Phase 6: sys_grep + sys_glob ───────────────────────────────────────
    //
    // Two read-only "search" syscalls that wrap `lib::search` /
    // `lib::glob` algorithms inside the standard syscall pipeline
    // (validate path → walk recursive prefix scan → INTERCEPT-free
    // since reads are routed through `sys_read`).  Replace the
    // pre-Phase-6 Python helpers in `nexus.fs._helpers.{grep, glob}`,
    // which Phase 7 deletes.

    /// Glob-match: walk every path under `prefix` recursively and
    /// return the ones matching `pattern` (one of `?`, `*`, `**`,
    /// `[abc]`, `{a,b}` per the `globset` crate's syntax).
    ///
    /// Pure metadata scan — never reads file content, only consults
    /// the metastore for the path list.  `Send + Sync` callers can
    /// use the result list directly without holding kernel locks.
    pub fn sys_glob(
        &self,
        pattern: &str,
        prefix: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<String>, KernelError> {
        validate_path_fast(prefix)?;
        let all_paths = self.collect_paths_recursive(prefix)?;
        let patterns = vec![pattern.to_string()];
        lib::glob::glob_match(&patterns, &all_paths)
            .map_err(|e| KernelError::IOError(format!("sys_glob: {e}")))
    }

    /// Grep: walk every regular file under `prefix` recursively, read
    /// content via `sys_read`, scan lines with `lib::search::search_lines`,
    /// return up to `max_results` matches.
    ///
    /// When `disk_paths` is non-empty the walk is skipped: the kernel
    /// reads each absolute path from disk directly (bypassing the
    /// metastore) and scans the same way.  Used by the search-tier
    /// cache fast path where the cached blob's on-disk location is
    /// already known.
    ///
    /// Skips:
    ///   * non-regular entries (directories, pipes, streams, mounts)
    ///   * unreadable files (permission errors, missing content)
    ///   * non-UTF-8 content (binary files)
    ///
    /// `ignore_case = true` switches `lib::search::build_search_mode`
    /// to a case-insensitive regex; literal patterns auto-detect via
    /// `lib::search::is_literal_pattern`.
    pub fn sys_grep(
        &self,
        pattern: &str,
        prefix: &str,
        ignore_case: bool,
        max_results: usize,
        disk_paths: &[String],
        ctx: &OperationContext,
    ) -> Result<Vec<lib::search::grep::GrepMatch>, KernelError> {
        let search_mode = lib::search::build_search_mode(pattern, ignore_case)
            .map_err(|e| KernelError::IOError(format!("sys_grep regex: {e}")))?;

        let mut all_matches: Vec<lib::search::grep::GrepMatch> = Vec::new();

        if !disk_paths.is_empty() {
            // Disk-path mode: read each path directly, no metastore walk.
            for fpath in disk_paths {
                if all_matches.len() >= max_results {
                    break;
                }
                let bytes = match std::fs::read(fpath) {
                    Ok(b) => b,
                    Err(_) => continue,
                };
                let content = match std::str::from_utf8(&bytes) {
                    Ok(s) => s,
                    Err(_) => continue,
                };
                let remaining = max_results.saturating_sub(all_matches.len());
                let matches = lib::search::search_lines(fpath, content, &search_mode, remaining);
                all_matches.extend(matches);
            }
            return Ok(all_matches);
        }

        validate_path_fast(prefix)?;
        let all_paths = self.collect_paths_recursive(prefix)?;
        for fpath in all_paths {
            if all_matches.len() >= max_results {
                break;
            }
            // Probe entry_type via dcache; skip non-regular entries.
            // A None dcache entry is conservatively treated as
            // regular (the metastore stamped it; sys_read will fail
            // gracefully if the underlying backend disagrees).
            if let Some(entry) = self.dcache.get_entry(&fpath) {
                if entry.entry_type != crate::dcache::DT_REG {
                    continue;
                }
            }
            let bytes = match self.sys_read(&fpath, ctx) {
                Ok(r) => r.data.unwrap_or_default(),
                Err(_) => continue,
            };
            let content = match std::str::from_utf8(&bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };
            let remaining = max_results.saturating_sub(all_matches.len());
            let matches = lib::search::search_lines(&fpath, content, &search_mode, remaining);
            all_matches.extend(matches);
        }
        Ok(all_matches)
    }

    /// Helper: walk every metastore entry under `prefix` recursively
    /// and return the full list of paths.  Pages through the metastore
    /// in chunks of 1024 to bound peak memory on a deep tree.
    fn collect_paths_recursive(&self, prefix: &str) -> Result<Vec<String>, KernelError> {
        let mut out: Vec<String> = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let page = self.metastore_list_paginated(prefix, true, 1024, cursor.as_deref())?;
            for meta in &page.items {
                out.push(meta.path.clone());
            }
            if !page.has_more {
                break;
            }
            cursor = page.next_cursor.clone();
        }
        Ok(out)
    }

    // ── R10c: direct CAS surface ─────────────────────────────────────────
    //
    // These methods replace Python `CASAddressingEngine`'s hot-path bodies
    // (`write_content`, `read_content`, `read_range`, `delete_content`,
    // `content_exists`, `get_content_size`, `is_chunked`, `_write_at_offset`).
    // Each resolves (mount_point, zone_id) → MountEntry → &CASEngine via
    // `ObjectStore::as_cas`; non-CAS backends surface as `InvalidPath`.
    // Error context enrichment: the backend_name + content_hash are baked
    // into the returned `KernelError` so Python callers see
    // `BackendError("CAS I/O error [mount=cas-local hash=abcd…]: …")`
    // instead of a bare I/O message.
    //
    // `ttl_seconds` is accepted on `cas_write` but not routed — the flat
    // `LocalCASTransport` has no TTL bucketing; when a TTL-aware transport
    // (e.g. the VolumeEngine in cluster mode) is wired, the kwarg gets
    // plumbed through without changing the PyKernel surface.

    fn cas_engine_do<F, R>(
        &self,
        mount_point: &str,
        zone_id: &str,
        op: &str,
        f: F,
    ) -> Result<R, KernelError>
    where
        F: FnOnce(&crate::cas_engine::CASEngine) -> Result<R, crate::cas_engine::CASError>,
    {
        let canonical = canonicalize(mount_point, zone_id);
        let entry = self.vfs_router.get_canonical(&canonical).ok_or_else(|| {
            KernelError::InvalidPath(format!(
                "{}: mount not found: {}@{}",
                op, mount_point, zone_id
            ))
        })?;
        let cas = entry
            .backend
            .as_ref()
            .and_then(|b| b.as_cas())
            .ok_or_else(|| {
                KernelError::InvalidPath(format!(
                    "{}: mount '{}@{}' backend is not CAS",
                    op, mount_point, zone_id
                ))
            })?;
        f(cas).map_err(|e| cas_err_to_kernel(e, mount_point, op))
    }

    /// Write content → (hash, is_new). Fires `is_new=true` only when the
    /// top-level manifest/blob hash was freshly written (CAS dedup miss).
    pub fn cas_write(
        &self,
        mount_point: &str,
        zone_id: &str,
        content: &[u8],
        _ttl_seconds: Option<u64>,
    ) -> Result<(String, bool), KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_write", |cas| {
            cas.write_content_tracked(content)
        })
    }

    /// Read content by hash. Transparently reassembles chunked manifests;
    /// falls through to scatter-gather on local chunk miss when origins
    /// are provided.
    pub fn cas_read(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
        origins: &[String],
    ) -> Result<Vec<u8>, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_read", |cas| {
            cas.read_content_with_origins(content_hash, origins)
        })
    }

    /// Read byte range `[start, end)` from content. Uses the chunked
    /// range path when content is chunked, slice of full blob otherwise.
    pub fn cas_read_range(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
        start: u64,
        end: u64,
        origins: &[String],
    ) -> Result<Vec<u8>, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_read_range", |cas| {
            if cas.is_chunked(content_hash) {
                cas.read_chunked_range_with_origins(content_hash, start, end, origins)
            } else {
                let full = cas.read_content_with_origins(content_hash, origins)?;
                let s = start as usize;
                let e = (end as usize).min(full.len());
                if s >= e {
                    return Ok(Vec::new());
                }
                Ok(full[s..e].to_vec())
            }
        })
    }

    /// Delete content. Dispatches to chunked-manifest delete (which sweeps
    /// chunks + sidecars) when appropriate.
    pub fn cas_delete(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
    ) -> Result<(), KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_delete", |cas| {
            if cas.is_chunked(content_hash) {
                cas.delete_chunked(content_hash)
            } else {
                cas.delete_content(content_hash)
            }
        })
    }

    /// Fast existence check — just `path.exists` against the CAS
    /// filesystem layout (hash-as-filename).
    pub fn cas_exists(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
    ) -> Result<bool, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_exists", |cas| {
            Ok(cas.content_exists(content_hash))
        })
    }

    /// Content size. For chunked content, reads the manifest's `.meta`
    /// sidecar (no chunk I/O). For plain blobs, stats the CAS file.
    pub fn cas_size(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
    ) -> Result<u64, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_size", |cas| {
            cas.get_size(content_hash)
        })
    }

    /// True iff this content_hash was stored as a chunked manifest.
    /// Uses the `.meta` sidecar presence as a fast-reject.
    pub fn cas_is_chunked(
        &self,
        mount_point: &str,
        zone_id: &str,
        content_hash: &str,
    ) -> Result<bool, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_is_chunked", |cas| {
            Ok(cas.is_chunked(content_hash))
        })
    }

    /// Partial write — dispatches to `write_chunked_partial` when the old
    /// blob is chunked, otherwise does a full read-modify-write in Rust.
    /// Returns the new content_hash.
    pub fn cas_write_partial(
        &self,
        mount_point: &str,
        zone_id: &str,
        old_hash: &str,
        buf: &[u8],
        offset: u64,
        origins: &[String],
    ) -> Result<String, KernelError> {
        self.cas_engine_do(mount_point, zone_id, "cas_write_partial", |cas| {
            cas.write_partial(old_hash, buf, offset, origins)
        })
    }

    // ═════════════════════════════════════════════════════════════════
    // R20.18.2+.3: Federation mount wiring — kernel-internal Rust calls
    //   (not exposed to Python per v20.10 boundary rule). Invoked by
    //   Kernel::init_federation_from_env (R20.18.2) + apply-cb installed
    //   on every loaded zone. All methods below carry #[allow(dead_code)]
    //   until R20.18.2 wires the env-driven bootstrap that invokes them.
    // ═════════════════════════════════════════════════════════════════

    /// Install the ``ZoneRaftRegistry`` + tokio runtime the apply-side
    /// federation mount wiring needs. Idempotent: subsequent calls are
    /// no-ops (``OnceLock::set`` returns ``Err`` on second call).
    #[allow(dead_code)]
    pub fn attach_zone_registry(
        &self,
        registry: Arc<nexus_raft::raft::ZoneRaftRegistry>,
        runtime: tokio::runtime::Handle,
    ) {
        let _ = self.zone_registry.set(registry);
        let _ = self.zone_runtime.set(runtime);
    }

    /// Phase 4 (full): stash the blob-fetcher slot for later install
    /// by `transport::blob::fetcher::install`.  Pre-Phase-4 this
    /// constructed `transport::blob::fetcher::KernelBlobFetcher`
    /// directly, but kernel no longer depends on the high-level
    /// transport crate (cycle break); the cdylib boot drains the
    /// slot and installs the fetcher.
    fn stash_blob_fetcher_slot(&self, slot: Box<dyn std::any::Any + Send + Sync>) {
        *self.pending_blob_fetcher_slot.lock() = Some(slot);
    }

    /// Drain the stashed blob-fetcher slot (called by
    /// `transport::blob::fetcher::install`).  Returns `None` after
    /// the first drain so the cdylib boot can be safely re-invoked.
    pub fn take_pending_blob_fetcher_slot(&self) -> Option<Box<dyn std::any::Any + Send + Sync>> {
        self.pending_blob_fetcher_slot.lock().take()
    }

    /// Borrow the kernel's `Arc<VFSRouter>` — exposed so peer crates
    /// (transport blob fetcher) can construct their own fetchers
    /// pointed at the kernel's mount table.
    pub fn vfs_router_arc(&self) -> Arc<VFSRouter> {
        Arc::clone(&self.vfs_router)
    }

    /// Borrow the kernel's `Arc<DCache>` — exposed for the same
    /// reason as `vfs_router_arc`.
    pub fn dcache_arc(&self) -> Arc<DCache> {
        Arc::clone(&self.dcache)
    }

    /// R20.18.2: driven by `Kernel::new()` at startup (post-R20.18.5)
    /// or from tests. Reads federation env vars, constructs
    /// `raft::ZoneManager` internally, bootstraps the root raft group,
    /// creates listed zones, installs per-zone apply-cb, and replays
    /// persisted DT_MOUNT entries into the VFSRouter.
    ///
    /// Env vars read (all optional — absence of `NEXUS_HOSTNAME` is
    /// the "no federation" signal and the method returns `Ok(())`
    /// as a no-op):
    /// - `NEXUS_HOSTNAME`: this node's hostname (required to enable
    ///   federation; absence disables).
    /// - `NEXUS_PEERS`: comma-separated `host:port` list.
    /// - `NEXUS_BIND_ADDR`: defaults to `0.0.0.0:2126`.
    /// - `NEXUS_DATA_DIR`: base dir for zone redb files; defaults to
    ///   `<NEXUS_STATE_DIR>/zones` where `NEXUS_STATE_DIR` itself
    ///   falls back to `~/.nexus`.
    /// - `NEXUS_FEDERATION_ZONES`: comma-separated zone ids to create
    ///   at Phase-1 bootstrap (raft group ConfState init only; no
    ///   data writes).
    /// - TLS state at `<zones_dir>/tls/`: probed to decide leader vs
    ///   joiner path. A `join-token` file triggers TLS pre-provision
    ///   from the cluster leader before ZoneManager construction.
    ///
    /// Aligned with Python `federation.py:from_env()` behavior —
    /// R20.18.5 deletes that Python path and activates this one.
    #[allow(dead_code)]
    pub(crate) fn init_federation_from_env(&self) -> Result<(), KernelError> {
        use std::path::Path;
        use std::sync::atomic::AtomicBool;

        // Activation signal: NEXUS_PEERS non-empty means federation is
        // explicitly configured. NEXUS_HOSTNAME defaults to the OS
        // hostname (docker compose sets `hostname: nexus-1` but not
        // the env var — Python `federation.py:from_env()` used
        // `socket.gethostname()` as the same fallback).
        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        if peers_csv.trim().is_empty() {
            return Ok(());
        }
        let hostname = std::env::var("NEXUS_HOSTNAME").ok().unwrap_or_else(|| {
            // Best-effort OS hostname — docker sets this to the
            // container `hostname:` field.
            #[cfg(unix)]
            {
                std::process::Command::new("hostname")
                    .output()
                    .ok()
                    .and_then(|o| String::from_utf8(o.stdout).ok())
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "localhost".to_string())
            }
            #[cfg(not(unix))]
            {
                std::env::var("COMPUTERNAME").unwrap_or_else(|_| "localhost".to_string())
            }
        });

        // R20.18.7: publish self's peer-reachable address so
        // `origin_backend_name` can encode "{backend}@{host:port}" into
        // every FileMetadata.backend_name it writes. Follower
        // `try_remote_fetch` parses that suffix to know where to pull
        // the blob from; without it, every cross-node read after
        // metadata-only replication fails with FileNotFound.
        //
        // SSOT: `NEXUS_ADVERTISE_ADDR` — the same env var raft uses for
        // cluster peering. Since R20.18.7 co-locates `ReadBlob` with
        // `ZoneApiService` on the raft port, one advertised address
        // covers both planes (etcd / CockroachDB `--advertise-addr`
        // pattern). Fallback: hostname + raft port parsed from
        // `NEXUS_BIND_ADDR` (defaults to 2126) so simple smoke-test
        // setups still publish something reachable.
        let self_addr = std::env::var(contracts::env::ADVERTISE_ADDR)
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| {
                let raft_port = std::env::var(contracts::env::BIND_ADDR)
                    .ok()
                    .as_deref()
                    .and_then(|s| s.rsplit_once(':'))
                    .and_then(|(_, p)| p.parse::<u16>().ok())
                    .unwrap_or(2126);
                format!("{}:{}", hostname, raft_port)
            });
        self.set_self_address(&self_addr);
        tracing::info!(
            self_address = %self_addr,
            "R20.18.7 init_federation_from_env: self-address published"
        );

        // Process-wide one-shot guard. Multiple `Kernel::new()` calls in
        // one process (e.g. `nexus.connect()` + a side embedded store
        // for OAuth crypto settings) would otherwise each try to bind
        // the same gRPC port. First kernel wins; later kernels in the
        // same process run in "federation disabled" mode.
        static FEDERATION_CLAIMED: AtomicBool = AtomicBool::new(false);
        if FEDERATION_CLAIMED.swap(true, Ordering::AcqRel) {
            tracing::debug!(
                "init_federation_from_env: already claimed by another Kernel in this process, skipping"
            );
            return Ok(());
        }

        let bind_addr =
            std::env::var("NEXUS_BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:2126".to_string());

        // zones_dir: honor NEXUS_DATA_DIR, else <NEXUS_STATE_DIR>/zones,
        // else ./nexus-zones (last-resort for smoke tests — prod must
        // set one of the env vars explicitly).
        let zones_dir = std::env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| {
            std::env::var("NEXUS_STATE_DIR")
                .map(|s| format!("{}/zones", s))
                .unwrap_or_else(|_| "./nexus-zones".to_string())
        });

        // Parse peers "host:port,host:port" → "id@host:port". `peers_csv`
        // already read above as part of the activation gate.
        let peers = parse_peer_list_to_raft_format(&peers_csv)
            .map_err(|e| KernelError::Federation(format!("NEXUS_PEERS parse: {}", e)))?;

        // TLS dir probe: detect joiner vs leader.
        let tls_dir = Path::new(&zones_dir).join("tls");
        let join_token_path = tls_dir.join("join-token");
        let ca_path = tls_dir.join("ca.pem");
        let node_cert_path = tls_dir.join("node.pem");
        let node_key_path = tls_dir.join("node-key.pem");

        // Join-token present + no node.pem → pre-provision TLS by
        // calling the leader's JoinCluster RPC.
        if join_token_path.exists() && !node_cert_path.exists() {
            let join_token = std::fs::read_to_string(&join_token_path)
                .map_err(|e| KernelError::Federation(format!("read join-token: {}", e)))?;
            let join_token = join_token.trim();
            // Pick any peer != self as the join target.
            let my_id = nexus_raft::transport::hostname_to_node_id(&hostname);
            let join_peer = peers.iter().find_map(|p| {
                // "id@host:port" → extract id + "host:port"
                let (id_str, hostport) = p.split_once('@')?;
                let id: u64 = id_str.parse().ok()?;
                (id != my_id).then(|| hostport.to_string())
            });
            if let Some(peer_addr) = join_peer {
                nexus_raft::zone_manager::join_cluster_and_provision_tls(
                    &peer_addr,
                    join_token,
                    &hostname,
                    &tls_dir.to_string_lossy(),
                )
                .map_err(|e| KernelError::Federation(format!("TLS pre-provision: {}", e)))?;
            } else {
                return Err(KernelError::Federation(
                    "Join token found but no peer in NEXUS_PEERS to join".to_string(),
                ));
            }
        }

        // R20.18.7: hand the same on-disk TLS material to the peer blob
        // client so its `ReadBlob` calls reach the co-located handler on
        // :2126 over the cluster's mTLS. ZoneManager re-reads these
        // files internally; reading them twice keeps the kernel's
        // TLS wiring independent of raft's `TlsFiles` struct and stops
        // a future raft refactor from silently breaking the client.
        if ca_path.exists() && node_cert_path.exists() && node_key_path.exists() {
            let ca_pem = std::fs::read(&ca_path)
                .map_err(|e| KernelError::Federation(format!("read ca.pem: {}", e)))?;
            let cert_pem = std::fs::read(&node_cert_path)
                .map_err(|e| KernelError::Federation(format!("read node.pem: {}", e)))?;
            let key_pem = std::fs::read(&node_key_path)
                .map_err(|e| KernelError::Federation(format!("read node-key.pem: {}", e)))?;
            // Phase 4 (full): trait method takes raw PEM bytes — the
            // concrete impl in transport reconstitutes the
            // `transport_primitives::TlsConfig` itself.
            self.peer_client_arc()
                .install_tls(&ca_pem, Some(&cert_pem), Some(&key_pem));
        }

        // TLS config for ZoneManager: present if ca.pem + node.pem +
        // node-key.pem all exist.
        let tls = if ca_path.exists() && node_cert_path.exists() && node_key_path.exists() {
            Some(nexus_raft::TlsFiles {
                cert_path: node_cert_path.clone(),
                key_path: node_key_path,
                ca_path: ca_path.clone(),
                ca_key_path: tls_dir
                    .join("ca-key.pem")
                    .exists()
                    .then(|| tls_dir.join("ca-key.pem")),
                join_token_hash: std::env::var("NEXUS_JOIN_TOKEN_HASH").ok(),
            })
        } else {
            None
        };

        // Construct ZoneManager. This spawns the gRPC server and opens
        // every previously-persisted zone from disk (R15.e).
        let zm =
            nexus_raft::ZoneManager::new(&hostname, &zones_dir, peers.clone(), &bind_addr, tls)
                .map_err(|e| KernelError::Federation(format!("ZoneManager::new: {}", e)))?;

        // Store Arc + derived handles. OnceLock::set is idempotent but
        // second call is Err — ignore per attach_zone_registry semantics.
        let runtime_handle = zm.runtime_handle();
        let registry = zm.registry();
        let blob_slot = zm.blob_fetcher_slot();
        let _ = self.zone_manager.set(zm.clone());
        let _ = self.zone_registry.set(registry);
        let _ = self.zone_runtime.set(runtime_handle);

        // R20.18.7: install the kernel-side `BlobFetcher` into the slot
        // the ZoneManager handed back. The gRPC server is already
        // running — once this write lands, every peer `ReadBlob`
        // resolves against the local VFSRouter's backends.
        self.stash_blob_fetcher_slot(Box::new(blob_slot));

        // Joiner detection — etcd `--initial-cluster-state=existing` equivalent.
        // Either signal alone is sufficient:
        //
        // (a) TLS-enrolled node: ca.pem + node.pem present, join-token
        //     consumed. Implies the node was enrolled by a CA and is
        //     restarting into an existing cluster.
        // (b) Explicit plaintext joiner: ``NEXUS_JOINER_HINT=1``. Used when
        //     there are no certs (plaintext mode) or when the user wants
        //     to override cert-based auto-detection. A fresh data dir plus
        //     this hint means "leader adds me via ConfChange + snapshot",
        //     avoiding the raft-rs `to_commit N out of range` panic on
        //     amnesia rejoin.
        //
        // Either true → skip local ConfState bootstrap; leader sends the
        // authoritative voter set via InstallSnapshot.
        let joiner_hint = std::env::var("NEXUS_JOINER_HINT")
            .map(|v| v == "1")
            .unwrap_or(false);
        let has_enrolled_certs =
            ca_path.exists() && node_cert_path.exists() && !join_token_path.exists();
        let is_joiner = joiner_hint || has_enrolled_certs;

        // Single bootstrap entry point — respects `is_joiner` uniformly
        // across the root zone and every `NEXUS_FEDERATION_ZONES` entry.
        // Previously the root zone honored `is_joiner` but the
        // `NEXUS_FEDERATION_ZONES` loop hard-coded `create_zone`, so a
        // joiner would skip bootstrap on root but still clobber other
        // zones with a locally-computed ConfState.
        let bootstrap_or_join = |zone_id: &str| -> Result<(), KernelError> {
            if zm.get_zone(zone_id).is_some() {
                return Ok(()); // Idempotent — already loaded from disk.
            }
            if is_joiner {
                zm.join_zone(zone_id, peers.clone(), false).map_err(|e| {
                    KernelError::Federation(format!("join_zone({}): {}", zone_id, e))
                })?;
            } else {
                zm.create_zone(zone_id, peers.clone()).map_err(|e| {
                    KernelError::Federation(format!("create_zone({}): {}", zone_id, e))
                })?;
            }
            Ok(())
        };

        // Phase-1 bootstrap: root zone, then any zones declared in
        // NEXUS_FEDERATION_ZONES. Mounts are handled separately via
        // reconcile_mounts_from_zones (for persisted DT_MOUNT) + apply-cb
        // (for new proposals).
        const ROOT_ZONE_ID: &str = "root";
        bootstrap_or_join(ROOT_ZONE_ID)?;

        if let Ok(zones_csv) = std::env::var("NEXUS_FEDERATION_ZONES") {
            for zone_id in zones_csv
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                bootstrap_or_join(zone_id)?;
            }
        }

        // Install apply-cb on every loaded zone so future DT_MOUNT
        // commits fire wire_federation_mount.
        for zone_id in zm.list_zones() {
            if let Some(consensus) = zm.registry().get_node(&zone_id) {
                self.install_federation_mount_coherence(&zone_id, consensus);
            }
        }

        // Replay persisted DT_MOUNT entries so VFSRouter is current
        // before first syscall arrives.
        self.reconcile_mounts_from_zones()?;

        // Signal: federation bootstrap complete.
        self.mount_reconciliation_done
            .store(true, Ordering::Release);
        tracing::info!("Federation bootstrap complete (hostname={})", hostname);
        Ok(())
    }

    /// R20.18.5 Phase B: clone the owning Arc<ZoneManager> so the
    /// codegen'd PyKernel `zone_*` methods (FederationRPCService
    /// backend) can reach it without a crate-internal back-reference.
    /// Returns None when federation isn't active.
    pub fn zone_manager_arc(&self) -> Option<Arc<nexus_raft::ZoneManager>> {
        self.zone_manager.get().cloned()
    }

    /// R20.16.6: snapshot the federation-bootstrap-complete flag.
    /// `/healthz/ready` and the `/__sys__/zones/<id>` PathResolver
    /// (R20.18.4) read this as the "safe to serve" signal.
    ///
    /// Returns `true` when federation was never bootstrapped (no
    /// `NEXUS_HOSTNAME`) — the "federation disabled = always ready"
    /// semantics the health probe relies on. When federation IS
    /// active, returns the atomic flag flipped by
    /// `init_federation_from_env` after `reconcile_mounts_from_zones`
    /// finishes.
    pub fn mount_reconciliation_done(&self) -> bool {
        if self.zone_manager.get().is_none() {
            return true;
        }
        self.mount_reconciliation_done.load(Ordering::Acquire)
    }

    /// R20.18.4: procfs-style virtual namespace for zone state. Read
    /// path only — any write/delete on `/__sys__/zones/*` must be
    /// rejected upstream (the underlying state is raft state-machine
    /// SSOT, not filesystem mutable state). Path format:
    ///
    /// - `/__sys__/zones/` → directory; `list_zones_procfs()`
    ///   enumerates zone ids.
    /// - `/__sys__/zones/<zone_id>` → synthesized entry with
    ///   `{is_leader, leader_id, term, commit_index, applied_index,
    ///   node_id, voter_count, witness_count}` fields read live from
    ///   `raft::ZoneManager`. Never persisted.
    ///
    /// Returns `None` when: federation isn't active; the path
    /// doesn't fall under `/__sys__/zones/`; or the zone id is
    /// unknown on this node. R20.18.5 wires this into `sys_stat` so
    /// Python `nx.sys_stat("/__sys__/zones/root")` reads through.
    #[allow(dead_code)]
    pub fn resolve_zones_procfs(&self, path: &str) -> Option<ZonesProcfsEntry> {
        const PREFIX: &str = "/__sys__/zones";
        let zm = self.zone_manager.get()?;

        if path == PREFIX || path == "/__sys__/zones/" {
            return Some(ZonesProcfsEntry {
                is_directory: true,
                zone_id: None,
                node_id: zm.node_id(),
                has_store: false,
                is_leader: false,
                leader_id: 0,
                term: 0,
                commit_index: 0,
                applied_index: 0,
                voter_count: 0,
                witness_count: 0,
                mount_reconciliation_done: self.mount_reconciliation_done(),
            });
        }

        // Extract zone id: must match exactly `/__sys__/zones/<id>`
        // with no trailing subpath.
        let suffix = path.strip_prefix(&format!("{}/", PREFIX))?;
        if suffix.is_empty() || suffix.contains('/') {
            return None;
        }
        let zone_id = suffix;
        let status = zm.cluster_status(zone_id);
        if !status.has_store {
            return None;
        }
        Some(ZonesProcfsEntry {
            is_directory: false,
            zone_id: Some(zone_id.to_string()),
            node_id: status.node_id,
            has_store: status.has_store,
            is_leader: status.is_leader,
            leader_id: status.leader_id,
            term: status.term,
            commit_index: status.commit_index,
            applied_index: status.applied_index,
            voter_count: status.voter_count,
            witness_count: status.witness_count,
            mount_reconciliation_done: self.mount_reconciliation_done(),
        })
    }

    /// R20.18.4: readdir companion for `/__sys__/zones/`. Returns the
    /// list of zone ids loaded on this node (derived from live
    /// `raft::ZoneManager::list_zones`), or an empty Vec when
    /// federation isn't active. Never errors.
    #[allow(dead_code)]
    pub fn list_zones_procfs(&self) -> Vec<String> {
        self.zone_manager
            .get()
            .map(|zm| zm.list_zones())
            .unwrap_or_default()
    }

    /// R20.18.3: when `sys_setattr(DT_MOUNT)` leader path runs without
    /// explicit metastore / raft_backend (Python didn't hand in
    /// `py_zone_handle` or `metastore_path`) AND federation is active,
    /// auto-resolve the zone raft group and build a `ZoneMetaStore`
    /// over it.
    ///
    /// Behavior matrix:
    /// - `metastore` OR `raft_backend` already supplied → passthrough.
    /// - No zone_manager attached (no federation) → passthrough (None, None).
    /// - Federation active, zone_id unknown locally →
    ///   `zone_manager.get_or_create_zone` creates the raft group
    ///   (Phase-1 ConfState bootstrap, idempotent).
    /// - Federation active, zone_id already loaded → reuse handle.
    ///
    /// In every federation-active branch, the returned tuple is
    /// `(Some(ZoneMetaStore), Some((consensus, runtime)))` so
    /// `dlc.mount` wires a raft-backed mount identically to the
    /// old Python `_mount_via_kernel` path.
    #[allow(clippy::type_complexity)]
    fn resolve_federation_mount_backing(
        &self,
        zone_id: &str,
        mount_path: &str,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
    ) -> Result<
        (
            Option<Arc<dyn crate::meta_store::MetaStore>>,
            Option<(
                nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
                tokio::runtime::Handle,
            )>,
        ),
        KernelError,
    > {
        // Explicit caller-supplied backing wins; never clobber it.
        if metastore.is_some() || raft_backend.is_some() {
            return Ok((metastore, raft_backend));
        }

        let Some(zm) = self.zone_manager.get() else {
            // No federation — local-only mount (metastore_path /
            // MemoryMetaStore fallback handled upstream).
            return Ok((None, None));
        };

        let handle = zm.get_or_create_zone(zone_id).map_err(|e| {
            KernelError::Federation(format!("get_or_create_zone({}): {}", zone_id, e))
        })?;
        let consensus = handle.consensus_node();
        let runtime = handle.runtime_handle();
        let ms: Arc<dyn crate::meta_store::MetaStore> =
            crate::raft_meta_store::ZoneMetaStore::new_arc(
                consensus.clone(),
                runtime.clone(),
                mount_path.to_string(),
            );
        // Ensure this zone has the mount-apply callback installed
        // (idempotent — OnceLock-backed). Matters when
        // init_federation_from_env ran before this zone was created,
        // so the bootstrap-time install loop didn't see it.
        self.install_federation_mount_coherence(zone_id, consensus.clone());
        Ok((Some(ms), Some((consensus, runtime))))
    }

    /// Look up a target zone's global VFS mount path on this node —
    /// R20.16.3 Rust port of Python ``_global_mount_of``.
    ///
    /// Returns the lexicographically smallest global path under which
    /// ``target_zone_id`` is currently mounted locally, or ``None`` if
    /// the zone has no local mount. Reads the apply-cb-maintained
    /// ``cross_zone_mounts`` reverse index (SSOT: DT_MOUNT entries in
    /// each parent zone's state machine).
    #[allow(dead_code)]
    pub fn global_mount_of(&self, target_zone_id: &str) -> Option<String> {
        let bucket = self.cross_zone_mounts.get(target_zone_id)?;
        bucket.iter().map(|(_, _, g)| g.clone()).min()
    }

    /// Snapshot the reverse-index entries for a target zone — used by
    /// Python ``remove_zone(force=True)`` to iterate cascade-unmount
    /// candidates (PyO3 surface returns this as a list of 3-tuples).
    #[allow(dead_code)]
    pub fn list_cross_zone_mounts(&self, target_zone_id: &str) -> Vec<(String, String, String)> {
        self.cross_zone_mounts
            .get(target_zone_id)
            .map(|v| v.clone())
            .unwrap_or_default()
    }

    /// Wire a federation child-zone mount into the local VFSRouter
    /// (R20.16.3). Invoked by the apply-side ``mount_apply_cb`` on
    /// every replica — leader and followers alike — after a DT_MOUNT
    /// Set commits in the parent zone's state machine. Safe to call
    /// before ``attach_zone_registry`` (returns Ok no-op).
    #[allow(dead_code)]
    pub fn wire_federation_mount(
        &self,
        parent_zone_id: &str,
        mount_path: &str,
        target_zone_id: &str,
    ) -> Result<(), KernelError> {
        let (Some(registry), Some(runtime)) = (self.zone_registry.get(), self.zone_runtime.get())
        else {
            // Not yet attached — startup replay will re-drive this.
            return Ok(());
        };
        wire_federation_mount_impl(
            &self.vfs_router,
            &self.dcache,
            &self.lock_manager,
            registry,
            runtime,
            &self.cross_zone_mounts,
            parent_zone_id,
            mount_path,
            target_zone_id,
        )
    }

    /// Install the apply-side DT_MOUNT callback that drives
    /// ``wire_federation_mount`` for every DT_MOUNT commit in
    /// ``consensus`` (R20.16.4). Mirrors the ``invalidate_cb``
    /// pattern — the closure captures cloned ``Arc``s of everything it
    /// needs so the state-machine callback stays a pure Fn with no
    /// ``&Kernel`` back-reference.
    #[allow(dead_code)]
    pub fn install_federation_mount_coherence(
        &self,
        parent_zone_id: &str,
        consensus: nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
    ) {
        tracing::info!(parent_zone_id = %parent_zone_id, "R20.18.5 install_federation_mount_coherence");
        let Some(slot) = consensus.mount_apply_cb_slot() else {
            tracing::warn!(parent_zone_id = %parent_zone_id, "install_federation_mount_coherence: mount_apply_cb_slot returned None");
            return;
        };
        let (Some(registry), Some(runtime)) = (self.zone_registry.get(), self.zone_runtime.get())
        else {
            tracing::warn!(parent_zone_id = %parent_zone_id, "install_federation_mount_coherence: zone_registry or zone_runtime not set");
            return;
        };
        let vfs_router = self.vfs_router_handle();
        let dcache = self.dcache_handle();
        let lock_manager = Arc::clone(&self.lock_manager);
        let registry = Arc::clone(registry);
        let runtime = runtime.clone();
        let cross_zone_mounts = Arc::clone(&self.cross_zone_mounts);
        let parent_zone_id_owned = parent_zone_id.to_string();
        let log_parent_zone_id = parent_zone_id_owned.clone();

        use nexus_raft::raft::MountApplyEvent;
        let cb: Arc<dyn Fn(&MountApplyEvent) + Send + Sync> =
            Arc::new(move |event: &MountApplyEvent| match event {
                MountApplyEvent::Set {
                    key,
                    target_zone_id,
                } => {
                    let _ = wire_federation_mount_impl(
                        &vfs_router,
                        &dcache,
                        &lock_manager,
                        &registry,
                        &runtime,
                        &cross_zone_mounts,
                        &parent_zone_id_owned,
                        key,
                        target_zone_id,
                    );
                }
                MountApplyEvent::Delete { key } => {
                    unwire_federation_mount_impl(
                        &vfs_router,
                        &dcache,
                        &cross_zone_mounts,
                        &parent_zone_id_owned,
                        key,
                    );
                }
            });
        *slot.write() = Some(cb);
        tracing::info!(parent_zone_id = %log_parent_zone_id, "R20.18.5 install_federation_mount_coherence: slot set");
    }

    /// Startup replay (R20.16.4): iterate every currently-loaded zone's
    /// DT_MOUNT entries, wire each one, and install the apply-cb so
    /// future DT_MOUNT commits fire ``wire_federation_mount``.
    /// Topological: repeats the pass until no progress (parent not
    /// wired yet → child mount deferred one round).
    #[allow(dead_code)]
    pub fn reconcile_mounts_from_zones(&self) -> Result<(), KernelError> {
        let Some(registry) = self.zone_registry.get() else {
            return Ok(());
        };

        let zone_ids = registry.list_zones();
        // Install callbacks first so any fresh commits arriving during
        // the scan are captured directly instead of being missed.
        for zone_id in &zone_ids {
            if let Some(node) = registry.get_node(zone_id) {
                self.install_federation_mount_coherence(zone_id, node);
            }
        }

        // Collect every DT_MOUNT entry across all zones.
        let mut pending: Vec<(String, String, String)> = Vec::new();
        for zone_id in &zone_ids {
            let Some(node) = registry.get_node(zone_id) else {
                continue;
            };
            let entries = node.iter_dt_mount_entries().unwrap_or_default();
            for (key, target_zone_id) in entries {
                pending.push((zone_id.clone(), key, target_zone_id));
            }
        }

        // Topological wire: loop until no progress. Cap iterations to
        // zone_count + 1 so a misconfigured cycle errors instead of
        // looping forever.
        let max_rounds = pending.len() + 1;
        for _ in 0..max_rounds {
            if pending.is_empty() {
                break;
            }
            let mut progressed = false;
            pending.retain(|(parent, key, target)| {
                match self.wire_federation_mount(parent, key, target) {
                    Ok(()) => {
                        // Check whether actually wired (cross_zone_mounts
                        // updated). If parent still unknown, the impl
                        // returns Ok but doesn't insert — retry.
                        if self.cross_zone_mounts.contains_key(target) {
                            progressed = true;
                            false
                        } else {
                            true
                        }
                    }
                    Err(_) => false, // give up on permanent failures
                }
            });
            if !progressed {
                break;
            }
        }
        Ok(())
    }
}

// ─────────────────────────────────────────────────────────────────────
// R20.16.3 free-function helpers — take only ``Arc``-shared kernel state
// so the apply-side ``mount_apply_cb`` closure can call them without a
// back-reference to ``Kernel`` itself.
// ─────────────────────────────────────────────────────────────────────

fn install_federation_dcache_coherence_impl(
    vfs_router: &Arc<VFSRouter>,
    dcache: &Arc<DCache>,
    consensus: &nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
) {
    let Some(slot) = consensus.invalidate_cb_slot() else {
        return;
    };
    let coherence_key = consensus.coherence_id();
    let dcache = Arc::clone(dcache);
    let vfs_router = Arc::clone(vfs_router);
    let cb: Arc<dyn Fn(&str) + Send + Sync> = Arc::new(move |zone_relative_key: &str| {
        let trimmed = zone_relative_key.trim_start_matches('/');
        for mp in vfs_router.mount_points_for_coherence_key(coherence_key) {
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

/// R20.18.2: parse a comma-separated `host:port` peer list (the
/// `NEXUS_PEERS` env-var format) into the `id@host:port` form
/// `raft::ZoneManager::new` expects. Node IDs are derived via the
/// raft crate's `hostname_to_node_id` SHA-256 helper — identical
/// to Python `PeerAddress.parse` so both sides agree on IDs during
/// the transition window.
#[allow(dead_code)]
fn parse_peer_list_to_raft_format(peers_csv: &str) -> Result<Vec<String>, String> {
    if peers_csv.trim().is_empty() {
        return Ok(Vec::new());
    }
    peers_csv
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            let (host, port_str) = s
                .rsplit_once(':')
                .ok_or_else(|| format!("expected 'host:port', got '{}'", s))?;
            let _port: u16 = port_str
                .parse()
                .map_err(|_| format!("invalid port in '{}'", s))?;
            let node_id = nexus_raft::transport::hostname_to_node_id(host);
            Ok(format!("{}@{}", node_id, s))
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
#[allow(dead_code)]
fn wire_federation_mount_impl(
    vfs_router: &Arc<VFSRouter>,
    dcache: &Arc<DCache>,
    lock_manager: &Arc<LockManager>,
    registry: &Arc<nexus_raft::raft::ZoneRaftRegistry>,
    runtime: &tokio::runtime::Handle,
    cross_zone_mounts: &DashMap<String, Vec<(String, String, String)>>,
    parent_zone_id: &str,
    mount_path: &str,
    target_zone_id: &str,
) -> Result<(), KernelError> {
    tracing::info!(
        parent_zone_id = %parent_zone_id,
        mount_path = %mount_path,
        target_zone_id = %target_zone_id,
        "R20.18.5 wire_federation_mount_impl entered"
    );
    // 1. Look up target zone. Not-yet-local is a no-op — reconcile
    //    loop and future apply events will re-drive.
    let Some(target_consensus) = registry.get_node(target_zone_id) else {
        tracing::warn!(target_zone_id = %target_zone_id, "wire_federation_mount: target zone not loaded locally");
        return Ok(());
    };

    // 2. Reconstruct the global VFS path for this mount (Python
    //    ``_on_mount_event`` did the same prefix logic on
    //    ``_mounts_by_target``).
    let global_path = match reconstruct_global_path(cross_zone_mounts, parent_zone_id, mount_path) {
        Some(g) => g,
        None => {
            tracing::warn!(parent_zone_id = %parent_zone_id, mount_path = %mount_path, "wire_federation_mount: reconstruct_global_path returned None");
            return Ok(());
        }
    };
    tracing::info!(global_path = %global_path, "wire_federation_mount: will add to VFSRouter");

    // 3. Build a ZoneMetaStore rooted at global_path targeting the
    //    target's state machine. Reuses the root mount's backend (Arc
    //    clone), so every federation mount shares the CAS backend on
    //    this node.
    let metastore: Arc<dyn crate::meta_store::MetaStore> =
        crate::raft_meta_store::ZoneMetaStore::new_arc(
            target_consensus.clone(),
            runtime.clone(),
            global_path.clone(),
        );
    let root_canonical = canonicalize("/", contracts::ROOT_ZONE_ID);
    let root_backend = vfs_router
        .get_canonical(&root_canonical)
        .and_then(|e| e.backend.clone());

    // 4. Install into VFSRouter (routing + backend + metastore) under
    //    the root zone — federation mounts live in the root zone's path
    //    space on every node. Tag the entry with `target_zone_id` so
    //    routing carries the destination zone (not the caller's ambient)
    //    — fixes `sys_write` tagging files with `zone_id=root` for
    //    paths under `/corp/eng` (owning zone is `corp-eng`) and lets
    //    `federation_share` derive zone-relative prefix from a global
    //    path via the existing `RouteResult`.
    vfs_router.add_federation_mount(
        &global_path,
        contracts::ROOT_ZONE_ID,
        root_backend,
        target_zone_id,
        false,
    );
    let canonical = canonicalize(&global_path, contracts::ROOT_ZONE_ID);
    vfs_router.install_metastore(&canonical, metastore);

    // 5. LockManager upgrade on first federated mount — idempotent.
    //    Bind distributed locks to the ROOT zone's consensus, not this
    //    mount's `target_consensus`. Root is the one zone every
    //    federation peer always has loaded, so every node agrees on
    //    which state machine holds lock state. Binding to the caller's
    //    target meant reconcile-order differences (DashMap iteration,
    //    restart replay) picked different zones on different nodes —
    //    locks then lived in disjoint state machines and cross-node
    //    `lock_acquire` couldn't see each other, letting two peers
    //    "acquire" the same path concurrently (test_contended_write_ordering).
    if !lock_manager.locks_installed() {
        match registry.get_node(contracts::ROOT_ZONE_ID) {
            Some(root_consensus) => {
                tracing::info!(
                    parent_zone = %parent_zone_id,
                    mount_path = %mount_path,
                    "wire_federation_mount: installing distributed locks bound to ROOT zone"
                );
                let kernel_state = lock_manager.advisory_state_arc();
                let (backend, shared_state) = nexus_raft::federation::DistributedLocks::new(
                    root_consensus,
                    runtime.clone(),
                    kernel_state,
                );
                lock_manager.install_locks(Arc::new(backend), shared_state);
            }
            None => {
                tracing::warn!(
                    "wire_federation_mount: root zone not loaded — distributed locks NOT installed; sys_lock will stay local-only until next mount"
                );
            }
        }
    }

    // 6. DCache seed so sys_stat on the mount point resolves locally
    //    without a metastore round-trip.
    dcache.put(
        &global_path,
        CachedEntry {
            size: 0,
            etag: None,
            version: 1,
            entry_type: 2, // DT_MOUNT
            zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
        },
    );

    // 7. Install apply-side dcache coherence on the target consensus
    //    (idempotent — replays overwrite with an equivalent closure).
    install_federation_dcache_coherence_impl(vfs_router, dcache, &target_consensus);

    // 8. Update reverse index (target → [(parent, mount_path, global)]).
    //    Dedup so replayed apply events don't double-register.
    let mut bucket = cross_zone_mounts
        .entry(target_zone_id.to_string())
        .or_default();
    let tuple = (
        parent_zone_id.to_string(),
        mount_path.to_string(),
        global_path,
    );
    if !bucket.contains(&tuple) {
        bucket.push(tuple);
    }
    Ok(())
}

#[allow(dead_code)]
fn unwire_federation_mount_impl(
    vfs_router: &Arc<VFSRouter>,
    dcache: &Arc<DCache>,
    cross_zone_mounts: &DashMap<String, Vec<(String, String, String)>>,
    parent_zone_id: &str,
    mount_path: &str,
) {
    // Find the matching entry via the reverse index, then drop the
    // VFSRouter slot + evict the DCache seed. Scans all targets
    // because the apply-cb only knows (parent, mount_path), not target.
    let mut remove_empty: Option<String> = None;
    let mut unwired_global: Option<String> = None;
    for mut entry in cross_zone_mounts.iter_mut() {
        let bucket = entry.value_mut();
        if let Some(pos) = bucket
            .iter()
            .position(|(p, m, _)| p == parent_zone_id && m == mount_path)
        {
            let (_, _, global) = bucket.remove(pos);
            unwired_global = Some(global);
            if bucket.is_empty() {
                remove_empty = Some(entry.key().clone());
            }
            break;
        }
    }
    if let Some(target) = remove_empty {
        cross_zone_mounts.remove(&target);
    }
    if let Some(global) = unwired_global {
        vfs_router.remove(&global, contracts::ROOT_ZONE_ID);
        dcache.evict(&global);
        dcache.evict_prefix(&format!("{}/", global.trim_end_matches('/')));
    }
}

/// Reconstruct the global VFS path for a DT_MOUNT apply event. Port of
/// Python ``_on_mount_event`` prefix logic — root-zone parents already
/// publish global paths; non-root parents need the parent's own global
/// prepended (looked up via ``cross_zone_mounts``).
#[allow(dead_code)]
fn reconstruct_global_path(
    cross_zone_mounts: &DashMap<String, Vec<(String, String, String)>>,
    parent_zone_id: &str,
    mount_path: &str,
) -> Option<String> {
    if parent_zone_id == contracts::ROOT_ZONE_ID || parent_zone_id.is_empty() {
        return Some(mount_path.to_string());
    }
    let parent_global = cross_zone_mounts
        .get(parent_zone_id)
        .and_then(|v| v.iter().map(|(_, _, g)| g.clone()).min())?;
    if mount_path == parent_global || mount_path.starts_with(&format!("{}/", parent_global)) {
        Some(mount_path.to_string())
    } else if mount_path == "/" {
        Some(parent_global)
    } else {
        Some(format!("{}{}", parent_global, mount_path))
    }
}

/// Convert `CASError` → `KernelError` with backend + op context baked
/// into the message. Python side receives either `NexusFileNotFoundError`
/// (for NotFound) or `BackendError` (for I/O), with enough breadcrumbs to
/// debug without re-decorating on every call site.
fn cas_err_to_kernel(e: crate::cas_engine::CASError, mount_point: &str, op: &str) -> KernelError {
    use crate::cas_engine::CASError;
    match e {
        CASError::NotFound(hash) => {
            KernelError::FileNotFound(format!("{} [mount={}]: {}", op, mount_point, hash))
        }
        CASError::IOError(io) => {
            KernelError::BackendError(format!("{} [mount={}]: {}", op, mount_point, io))
        }
    }
}

// ── Fast path validation ────────────────────────────────────────────────

// ── Manager error conversions ─────────────────────────────────────────

fn pipe_mgr_err(e: crate::pipe_manager::PipeManagerError) -> KernelError {
    use crate::pipe_manager::PipeManagerError;
    match e {
        PipeManagerError::Exists(p) => KernelError::PipeExists(p),
        PipeManagerError::NotFound(p) => KernelError::PipeNotFound(p),
        PipeManagerError::Closed(p) => KernelError::PipeClosed(p),
        PipeManagerError::WouldBlock(msg) => KernelError::WouldBlock(msg),
        PipeManagerError::Backend(be) => {
            use crate::pipe::PipeError;
            match be {
                PipeError::Full(u, c) => KernelError::PipeFull(format!("{u}/{c} bytes used")),
                PipeError::Closed(msg) => KernelError::PipeClosed(msg.to_string()),
                PipeError::Oversized(s, c) => {
                    KernelError::PipeFull(format!("msg {s} > capacity {c}"))
                }
                other => KernelError::IOError(format!("pipe: {other:?}")),
            }
        }
    }
}

fn stream_mgr_err(e: crate::stream_manager::StreamManagerError) -> KernelError {
    use crate::stream_manager::StreamManagerError;
    match e {
        StreamManagerError::Exists(p) => KernelError::StreamExists(p),
        StreamManagerError::NotFound(p) => KernelError::StreamNotFound(p),
        StreamManagerError::Closed(p) => KernelError::StreamClosed(p),
        StreamManagerError::WouldBlock(msg) => KernelError::WouldBlock(msg),
        StreamManagerError::Backend(be) => {
            use crate::stream::StreamError;
            match be {
                StreamError::Full(u, c) => KernelError::StreamFull(format!("{u}/{c} bytes used")),
                StreamError::Closed(msg) => KernelError::StreamClosed(msg.to_string()),
                StreamError::Oversized(s, c) => {
                    KernelError::StreamFull(format!("msg {s} > capacity {c}"))
                }
                other => KernelError::IOError(format!("stream: {other:?}")),
            }
        }
    }
}

pub(crate) fn validate_path_fast(path: &str) -> Result<(), KernelError> {
    if path.is_empty() {
        return Err(KernelError::InvalidPath("Path cannot be empty".to_string()));
    }
    if !path.starts_with('/') {
        return Err(KernelError::InvalidPath(
            "Path must start with /".to_string(),
        ));
    }
    if path.contains('\0') {
        return Err(KernelError::InvalidPath(
            "Path contains null byte".to_string(),
        ));
    }
    for segment in path.split('/') {
        if segment == ".." {
            return Err(KernelError::InvalidPath(
                "Path contains parent directory reference (..)".to_string(),
            ));
        }
    }
    Ok(())
}

// ── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_peer_list_to_raft_format_empty() {
        assert_eq!(
            parse_peer_list_to_raft_format("").unwrap(),
            Vec::<String>::new()
        );
        assert_eq!(
            parse_peer_list_to_raft_format("   ").unwrap(),
            Vec::<String>::new()
        );
    }

    #[test]
    fn test_parse_peer_list_to_raft_format_single() {
        let out = parse_peer_list_to_raft_format("nexus-1:2126").unwrap();
        assert_eq!(out.len(), 1);
        // "id@host:port" — id derived from SHA-256(hostname), can't hard-code
        // (it's raft crate's SSOT); just check shape.
        assert!(out[0].ends_with("@nexus-1:2126"));
        assert!(out[0].contains('@'));
    }

    #[test]
    fn test_parse_peer_list_to_raft_format_multiple() {
        let out =
            parse_peer_list_to_raft_format("nexus-1:2126, nexus-2:2126 , nexus-3:2126").unwrap();
        assert_eq!(out.len(), 3);
        assert!(out[0].ends_with("@nexus-1:2126"));
        assert!(out[1].ends_with("@nexus-2:2126"));
        assert!(out[2].ends_with("@nexus-3:2126"));
    }

    #[test]
    fn test_parse_peer_list_to_raft_format_invalid() {
        assert!(parse_peer_list_to_raft_format("no-colon").is_err());
        assert!(parse_peer_list_to_raft_format("nexus-1:notanumber").is_err());
    }

    #[test]
    fn test_parse_peer_list_to_raft_format_deterministic_ids() {
        // Same hostname → same ID across calls (SSOT: raft crate's
        // hostname_to_node_id SHA-256 derivation).
        let a = parse_peer_list_to_raft_format("nexus-1:2126").unwrap();
        let b = parse_peer_list_to_raft_format("nexus-1:2126").unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn test_init_federation_from_env_no_peers_is_noop() {
        // Activation gate: NEXUS_PEERS must be non-empty. Without it
        // the method returns Ok(()) without touching any fields —
        // the "federation disabled" path. Save + clear + restore so
        // parallel tests don't collide.
        let saved_peers = std::env::var("NEXUS_PEERS").ok();
        // SAFETY: tests run serial on the same process; we restore
        // before returning and only flip one key.
        unsafe {
            std::env::remove_var("NEXUS_PEERS");
        }
        let k = Kernel::new();
        assert!(k.init_federation_from_env().is_ok());
        assert!(k.zone_manager.get().is_none());
        // R20.18.5: "federation disabled = always ready" semantics —
        // mount_reconciliation_done() returns true when zone_manager
        // is None so /healthz/ready isn't pinned-unhealthy on slim
        // profile. Verify the "inactive" fast path.
        assert!(k.mount_reconciliation_done());
        unsafe {
            match saved_peers {
                Some(v) => std::env::set_var("NEXUS_PEERS", v),
                None => std::env::remove_var("NEXUS_PEERS"),
            }
        }
    }

    #[test]
    fn test_resolve_federation_mount_backing_passthrough_when_explicit() {
        // When the caller already supplied a metastore (e.g. Python
        // passed `metastore_path` → LocalMetaStore), the resolver
        // must not auto-resolve — preserves the "local-only mount"
        // path even when federation is active.
        let k = Kernel::new();
        let ms: Arc<dyn crate::meta_store::MetaStore> =
            Arc::new(crate::meta_store::MemoryMetaStore::new());
        let (out_ms, out_rb) = k
            .resolve_federation_mount_backing("test-zone", "/test", Some(ms.clone()), None)
            .expect("resolve");
        assert!(
            out_ms.is_some(),
            "passthrough must preserve caller's metastore"
        );
        assert!(out_rb.is_none());
    }

    #[test]
    fn test_resolve_federation_mount_backing_no_federation_returns_none_none() {
        // No zone_manager attached (slim / non-federation profile) →
        // resolver is a no-op, returns (None, None) so upstream
        // continues with local-only MemoryMetaStore fallback.
        let k = Kernel::new();
        assert!(k.zone_manager.get().is_none());
        let (out_ms, out_rb) = k
            .resolve_federation_mount_backing("test-zone", "/test", None, None)
            .expect("resolve");
        assert!(out_ms.is_none());
        assert!(out_rb.is_none());
    }

    #[test]
    fn test_resolve_zones_procfs_returns_none_without_federation() {
        // No zone_manager attached → every `/__sys__/zones/*` query
        // is None so the caller falls through to regular path routing.
        let k = Kernel::new();
        assert!(k.resolve_zones_procfs("/__sys__/zones").is_none());
        assert!(k.resolve_zones_procfs("/__sys__/zones/").is_none());
        assert!(k.resolve_zones_procfs("/__sys__/zones/root").is_none());
        assert_eq!(k.list_zones_procfs(), Vec::<String>::new());
    }

    #[test]
    fn test_resolve_zones_procfs_rejects_non_zones_paths() {
        // Even when federation is active the resolver must only
        // claim paths under `/__sys__/zones/` — otherwise sys_stat
        // on unrelated paths would silently short-circuit.
        let k = Kernel::new();
        // These should be None regardless of federation state.
        assert!(k.resolve_zones_procfs("/workspace/file.txt").is_none());
        assert!(k.resolve_zones_procfs("/__sys__/other/path").is_none());
        assert!(k
            .resolve_zones_procfs("/__sys__/zones/root/nested")
            .is_none());
        assert!(k.resolve_zones_procfs("/__sys__/zones/").is_none()); // no federation
    }

    #[test]
    fn test_validate_path_fast() {
        assert!(validate_path_fast("/valid/path").is_ok());
        assert!(validate_path_fast("/").is_ok());
        assert!(validate_path_fast("/a/b/c.txt").is_ok());

        assert!(validate_path_fast("").is_err());
        assert!(validate_path_fast("no-slash").is_err());
        assert!(validate_path_fast("/has\0null").is_err());
        assert!(validate_path_fast("/has/../traversal").is_err());
        assert!(validate_path_fast("/..").is_err());
    }

    // ── §11 Phase 3 OBSERVE ThreadPool tests ───────────────────────

    use crate::dispatch::{FileEvent, FileEventType, MutationObserver};
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;

    /// Counts every observed event and stashes the path so the test
    /// can assert delivery in arbitrary order. Pure-Rust observer —
    /// no GIL involved, so works fine in `cargo test --lib`.
    struct CountingObserver {
        seen: Arc<AtomicUsize>,
        last_path: Arc<parking_lot::Mutex<Option<String>>>,
    }

    impl MutationObserver for CountingObserver {
        fn on_mutation(&self, event: &FileEvent) {
            *self.last_path.lock() = Some(event.path.clone());
            self.seen.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn dispatch_observers_runs_on_threadpool_off_caller_thread() {
        let kernel = Kernel::new();
        let seen = Arc::new(AtomicUsize::new(0));
        let last_path = Arc::new(parking_lot::Mutex::new(None));
        let obs = Arc::new(CountingObserver {
            seen: Arc::clone(&seen),
            last_path: Arc::clone(&last_path),
        });

        kernel.register_observer(obs, "counting".to_string(), FileEventType::FileWrite.bit());

        let event = FileEvent::new(FileEventType::FileWrite, "/test/file.txt");
        kernel.dispatch_observers(&event);

        // dispatch_observers is fire-and-forget; the worker may not
        // have run yet. flush_observers blocks until the queue drains.
        kernel.flush_observers();

        assert_eq!(seen.load(Ordering::Relaxed), 1);
        assert_eq!(last_path.lock().as_deref(), Some("/test/file.txt"));
    }

    #[test]
    fn dispatch_observers_skips_non_matching_event_mask() {
        let kernel = Kernel::new();
        let seen = Arc::new(AtomicUsize::new(0));
        let obs = Arc::new(CountingObserver {
            seen: Arc::clone(&seen),
            last_path: Arc::new(parking_lot::Mutex::new(None)),
        });

        // Register for FileDelete only.
        kernel.register_observer(obs, "del-only".to_string(), FileEventType::FileDelete.bit());

        // Fire FileWrite — must NOT trigger the observer.
        kernel.dispatch_observers(&FileEvent::new(FileEventType::FileWrite, "/x"));
        kernel.flush_observers();
        assert_eq!(seen.load(Ordering::Relaxed), 0);

        // Fire FileDelete — must trigger.
        kernel.dispatch_observers(&FileEvent::new(FileEventType::FileDelete, "/y"));
        kernel.flush_observers();
        assert_eq!(seen.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn dispatch_observers_fans_out_to_multiple_observers() {
        let kernel = Kernel::new();
        let count_a = Arc::new(AtomicUsize::new(0));
        let count_b = Arc::new(AtomicUsize::new(0));

        kernel.register_observer(
            Arc::new(CountingObserver {
                seen: Arc::clone(&count_a),
                last_path: Arc::new(parking_lot::Mutex::new(None)),
            }),
            "a".to_string(),
            FileEventType::FileWrite.bit(),
        );
        kernel.register_observer(
            Arc::new(CountingObserver {
                seen: Arc::clone(&count_b),
                last_path: Arc::new(parking_lot::Mutex::new(None)),
            }),
            "b".to_string(),
            FileEventType::FileWrite.bit(),
        );

        for i in 0..10 {
            kernel.dispatch_observers(&FileEvent::new(FileEventType::FileWrite, format!("/p/{i}")));
        }
        kernel.flush_observers();

        assert_eq!(count_a.load(Ordering::Relaxed), 10);
        assert_eq!(count_b.load(Ordering::Relaxed), 10);
    }

    #[test]
    fn dispatch_observers_no_observers_is_zero_cost_no_op() {
        let kernel = Kernel::new();
        // No observers registered; dispatch must not panic and must
        // not even submit to the pool. flush_observers is a sanity
        // check that returns immediately.
        kernel.dispatch_observers(&FileEvent::new(FileEventType::FileWrite, "/empty"));
        kernel.flush_observers();
        assert_eq!(kernel.observer_count(), 0);
    }

    #[test]
    fn unregister_observer_stops_dispatch() {
        let kernel = Kernel::new();
        let seen = Arc::new(AtomicUsize::new(0));
        let obs = Arc::new(CountingObserver {
            seen: Arc::clone(&seen),
            last_path: Arc::new(parking_lot::Mutex::new(None)),
        });
        kernel.register_observer(obs, "to-remove".to_string(), FileEventType::FileWrite.bit());

        kernel.dispatch_observers(&FileEvent::new(FileEventType::FileWrite, "/before"));
        kernel.flush_observers();
        assert_eq!(seen.load(Ordering::Relaxed), 1);

        assert!(kernel.unregister_observer("to-remove"));
        kernel.dispatch_observers(&FileEvent::new(FileEventType::FileWrite, "/after"));
        kernel.flush_observers();
        // Count is unchanged — observer is gone.
        assert_eq!(seen.load(Ordering::Relaxed), 1);
        assert_eq!(kernel.observer_count(), 0);
    }

    // ── §11 Phase 5 dispatch_mutation context propagation tests ────

    /// Captures the FileEvent it receives so the test can assert on
    /// every field. Used by the dispatch_mutation context tests below.
    struct CapturingObserver {
        captured: Arc<parking_lot::Mutex<Option<FileEvent>>>,
    }

    impl MutationObserver for CapturingObserver {
        fn on_mutation(&self, event: &FileEvent) {
            *self.captured.lock() = Some(event.clone());
        }
    }

    #[test]
    fn dispatch_mutation_propagates_operation_context_identity() {
        let kernel = Kernel::new();
        let captured = Arc::new(parking_lot::Mutex::new(None));
        let obs = Arc::new(CapturingObserver {
            captured: Arc::clone(&captured),
        });
        kernel.register_observer(obs, "cap".to_string(), FileEventType::FileWrite.bit());

        let ctx = OperationContext {
            user_id: "alice".to_string(),
            zone_id: "root".to_string(),
            is_admin: false,
            agent_id: Some("agent-42".to_string()),
            is_system: false,
            groups: vec![],
            admin_capabilities: vec![],
            subject_type: "user".to_string(),
            subject_id: None,
            request_id: "req-1".to_string(),
            context_zone_id: None,
        };

        kernel.dispatch_mutation(FileEventType::FileWrite, "/foo.txt", &ctx, |ev| {
            ev.size = Some(42);
            ev.etag = Some("abc123".to_string());
            ev.version = Some(1);
            ev.is_new = true;
        });
        kernel.flush_observers();

        let event = captured.lock().clone().expect("observer received event");
        assert_eq!(event.event_type, FileEventType::FileWrite);
        assert_eq!(event.path, "/foo.txt");
        assert_eq!(event.zone_id.as_deref(), Some("root"));
        assert_eq!(event.user_id.as_deref(), Some("alice"));
        assert_eq!(event.agent_id.as_deref(), Some("agent-42"));
        assert_eq!(event.size, Some(42));
        assert_eq!(event.etag.as_deref(), Some("abc123"));
        assert_eq!(event.version, Some(1));
        assert!(event.is_new);
    }

    #[test]
    fn dispatch_mutation_handles_anonymous_context_without_user_id() {
        // Edge case: kernel-internal calls (e.g. background scanners)
        // pass an OperationContext with empty user_id. The helper must
        // not stamp Some("") into event.user_id — it should leave it None.
        let kernel = Kernel::new();
        let captured = Arc::new(parking_lot::Mutex::new(None));
        kernel.register_observer(
            Arc::new(CapturingObserver {
                captured: Arc::clone(&captured),
            }),
            "cap".to_string(),
            FileEventType::DirCreate.bit(),
        );

        let ctx = OperationContext {
            user_id: String::new(),
            zone_id: "root".to_string(),
            is_admin: true,
            agent_id: None,
            is_system: true,
            groups: vec![],
            admin_capabilities: vec![],
            subject_type: "user".to_string(),
            subject_id: None,
            request_id: String::new(),
            context_zone_id: None,
        };

        kernel.dispatch_mutation(FileEventType::DirCreate, "/d", &ctx, |_ev| {});
        kernel.flush_observers();

        let event = captured.lock().clone().expect("observer received event");
        assert!(event.user_id.is_none());
        assert!(event.agent_id.is_none());
        assert_eq!(event.zone_id.as_deref(), Some("root"));
    }

    // ── sys_setattr tests ─────────────────────────────────────────────

    /// Helper: call sys_setattr with only the fields needed, rest defaulted.
    fn setattr(
        kernel: &Kernel,
        path: &str,
        entry_type: i32,
    ) -> Result<SysSetAttrResult, KernelError> {
        kernel.sys_setattr(
            path, entry_type, "",   // backend_name
            None, // backend
            None, // metastore
            None, // raft_backend
            "memory", "root", false, // is_external
            65536, // capacity
            None,  // read_fd
            None,  // write_fd
            None,  // mime_type
            None,  // modified_at_ms
        )
    }

    #[test]
    fn sys_setattr_create_dir() {
        let k = Kernel::new();
        let r = setattr(&k, "/test-dir", 1).unwrap();
        assert!(r.created);
        assert_eq!(r.entry_type, 1);

        // Idempotent: second call returns created=false
        let r2 = setattr(&k, "/test-dir", 1).unwrap();
        assert!(!r2.created);
    }

    #[test]
    fn sys_setattr_create_pipe() {
        let k = Kernel::new();
        let r = setattr(&k, "/test-pipe", 3).unwrap();
        assert!(r.created);
        assert_eq!(r.entry_type, 3);
        assert_eq!(r.capacity, Some(65536));
        assert!(k.has_pipe("/test-pipe"));

        // Idempotent open
        let r2 = setattr(&k, "/test-pipe", 3).unwrap();
        assert!(!r2.created);
    }

    #[test]
    fn sys_setattr_create_stream() {
        let k = Kernel::new();
        let r = setattr(&k, "/test-stream", 4).unwrap();
        assert!(r.created);
        assert_eq!(r.entry_type, 4);
        assert!(k.has_stream("/test-stream"));

        // Idempotent open
        let r2 = setattr(&k, "/test-stream", 4).unwrap();
        assert!(!r2.created);
    }

    #[test]
    fn sys_setattr_entry_type_immutable() {
        let k = Kernel::new();
        // Create as DT_DIR
        setattr(&k, "/immut", 1).unwrap();
        // Try to change to DT_PIPE — should fail
        let err = setattr(&k, "/immut", 3);
        assert!(err.is_err());
        match err.unwrap_err() {
            KernelError::PermissionDenied(msg) => {
                assert!(msg.contains("immutable"), "unexpected msg: {msg}");
            }
            other => panic!("expected PermissionDenied, got: {other:?}"),
        }
    }

    #[test]
    fn sys_setattr_update_mime_type() {
        let k = Kernel::new();
        // Write a file via metastore so UPDATE has something to find
        k.metastore_put(
            "/update-test.txt",
            crate::meta_store::FileMetadata {
                path: "/update-test.txt".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 0,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
            },
        )
        .unwrap();

        // UPDATE with mime_type
        let r = k
            .sys_setattr(
                "/update-test.txt",
                0,
                "",
                None,
                None,
                None,
                "memory",
                "root",
                false,
                65536,
                None,
                None,
                Some("text/plain"),
                None,
            )
            .unwrap();
        assert!(!r.created);
        assert_eq!(r.updated, vec!["mime_type"]);
    }

    #[test]
    fn sys_setattr_update_file_not_found() {
        let k = Kernel::new();
        let err = setattr(&k, "/nonexistent", 0);
        assert!(err.is_err());
        match err.unwrap_err() {
            KernelError::FileNotFound(_) => {}
            other => panic!("expected FileNotFound, got: {other:?}"),
        }
    }

    // ── R20.3 metastore-key tests ──────────────────────────────────────
    //
    // Post-R20.3 the kernel passes full global paths to the metastore
    // trait. ZoneMetaStore (the federation impl) internalizes the
    // translation to zone-relative — see rust/kernel/src/raft_metastore.rs
    // for that coverage. These tests use LocalMetaStore (full-path store)
    // so they exercise the kernel call path without any translation.

    use crate::meta_store::MetaStore as MetastoreTrait;

    /// Create a temporary LocalMetaStore for testing.
    fn temp_metastore() -> Arc<crate::meta_store::LocalMetaStore> {
        let dir = std::env::temp_dir().join(format!("nexus-test-ms-{}", uuid::Uuid::new_v4()));
        let path = dir.join("meta.redb");
        Arc::new(crate::meta_store::LocalMetaStore::open(&path).unwrap())
    }

    #[test]
    fn sys_setattr_dir_stores_full_path_key() {
        // Mount "/data" in zone "root" with a shared metastore.
        // DT_DIR at "/data/sub" now stores metastore key "/data/sub"
        // (full global path) — R20.3 moved zone-relative translation
        // into ZoneMetaStore, so generic full-path stores see full keys.
        let k = Kernel::new();
        let ms = temp_metastore();
        k.add_mount("/data", "root", None, Some(ms.clone()), None, false)
            .unwrap();

        // Create DT_DIR via sys_setattr — writes to per-mount metastore
        let r = k
            .sys_setattr(
                "/data/sub",
                1,
                "",
                None,
                None,
                None,
                "balanced",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
            )
            .unwrap();
        assert!(r.created);

        // R20.3: key is the full global path.
        assert!(
            ms.get("/data/sub").unwrap().is_some(),
            "full path /data/sub must exist"
        );
        assert!(
            ms.get("/sub").unwrap().is_none(),
            "old zone-relative key /sub must NOT exist post-R20.3"
        );
    }

    #[test]
    fn metastore_proxy_returns_global_paths() {
        // metastore_get/list should return global paths even though storage is zone-relative.
        let k = Kernel::new();
        let ms = temp_metastore();
        k.add_mount("/data", "root", None, Some(ms.clone()), None, false)
            .unwrap();

        // Create a DT_DIR at /data/reports
        k.sys_setattr(
            "/data/reports",
            1,
            "",
            None,
            None,
            None,
            "balanced",
            "root",
            false,
            0,
            None,
            None,
            None,
            None,
        )
        .unwrap();

        // metastore_get should return global path "/data/reports"
        let meta = k.metastore_get("/data/reports").unwrap().unwrap();
        assert_eq!(
            meta.path, "/data/reports",
            "metastore_get must return global path"
        );

        // metastore_list should return global paths
        let entries = k.metastore_list("/data/").unwrap();
        assert!(!entries.is_empty());
        for e in &entries {
            assert!(
                e.path.starts_with("/data/"),
                "metastore_list entry path must be global: {}",
                e.path
            );
        }
    }

    #[test]
    fn test_sys_rename_cross_mount() {
        use crate::meta_store::{FileMetadata, MemoryMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        // Set up two separate mounts with independent MemoryMetaStores
        let ms_a = Arc::new(MemoryMetaStore::new());
        let ms_b = Arc::new(MemoryMetaStore::new());

        k.vfs_router.add_mount("/mnt_a", zone, None, false);
        k.vfs_router.add_mount("/mnt_b", zone, None, false);

        let canon_a = crate::vfs_router::canonicalize_mount_path("/mnt_a", zone);
        let canon_b = crate::vfs_router::canonicalize_mount_path("/mnt_b", zone);
        k.vfs_router.install_metastore(
            &canon_a,
            ms_a.clone() as Arc<dyn crate::meta_store::MetaStore>,
        );
        k.vfs_router.install_metastore(
            &canon_b,
            ms_b.clone() as Arc<dyn crate::meta_store::MetaStore>,
        );

        // Seed a file in mount A's metastore
        let meta = FileMetadata {
            path: "/file.txt".to_string(),
            size: 42,
            entry_type: DT_REG,
            ..Default::default()
        };
        ms_a.put("/file.txt", meta).unwrap();
        assert!(ms_a.exists("/file.txt").unwrap());
        assert!(!ms_b.exists("/file.txt").unwrap());

        // Cross-mount rename: /mnt_a/file.txt → /mnt_b/file.txt
        let ctx = OperationContext::new("test", zone, true, None, true);
        let result = k
            .sys_rename("/mnt_a/file.txt", "/mnt_b/file.txt", &ctx)
            .unwrap();
        assert!(result.hit, "cross-mount rename should return hit=true");
        assert!(result.success, "cross-mount rename should succeed");

        // Old metastore should be empty, new metastore should have the entry
        assert!(
            !ms_a.exists("/file.txt").unwrap(),
            "source metastore should no longer contain the file"
        );
        assert!(
            ms_b.exists("/file.txt").unwrap(),
            "destination metastore should contain the file"
        );
        let moved = ms_b.get("/file.txt").unwrap().unwrap();
        assert_eq!(moved.size, 42);
        assert_eq!(moved.path, "/file.txt");
    }

    #[test]
    fn test_sys_rename_cross_mount_directory_children() {
        use crate::meta_store::{FileMetadata, MemoryMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        let ms_a = Arc::new(MemoryMetaStore::new());
        let ms_b = Arc::new(MemoryMetaStore::new());

        k.vfs_router.add_mount("/mnt_a", zone, None, false);
        k.vfs_router.add_mount("/mnt_b", zone, None, false);

        let canon_a = crate::vfs_router::canonicalize_mount_path("/mnt_a", zone);
        let canon_b = crate::vfs_router::canonicalize_mount_path("/mnt_b", zone);
        k.vfs_router.install_metastore(
            &canon_a,
            ms_a.clone() as Arc<dyn crate::meta_store::MetaStore>,
        );
        k.vfs_router.install_metastore(
            &canon_b,
            ms_b.clone() as Arc<dyn crate::meta_store::MetaStore>,
        );

        // Seed a directory with children
        let dir_meta = FileMetadata {
            path: "/docs".to_string(),
            entry_type: DT_DIR,
            ..Default::default()
        };
        let child1 = FileMetadata {
            path: "/docs/a.md".to_string(),
            size: 10,
            entry_type: DT_REG,
            ..Default::default()
        };
        let child2 = FileMetadata {
            path: "/docs/b.md".to_string(),
            size: 20,
            entry_type: DT_REG,
            ..Default::default()
        };
        ms_a.put("/docs", dir_meta).unwrap();
        ms_a.put("/docs/a.md", child1).unwrap();
        ms_a.put("/docs/b.md", child2).unwrap();

        let ctx = OperationContext::new("test", zone, true, None, true);
        let result = k.sys_rename("/mnt_a/docs", "/mnt_b/docs", &ctx).unwrap();
        assert!(result.hit);
        assert!(result.success);
        assert!(result.is_directory);

        // All entries should have moved from ms_a to ms_b
        assert!(!ms_a.exists("/docs").unwrap());
        assert!(!ms_a.exists("/docs/a.md").unwrap());
        assert!(!ms_a.exists("/docs/b.md").unwrap());

        assert!(ms_b.exists("/docs").unwrap());
        assert!(ms_b.exists("/docs/a.md").unwrap());
        assert!(ms_b.exists("/docs/b.md").unwrap());

        assert_eq!(ms_b.get("/docs/a.md").unwrap().unwrap().size, 10);
        assert_eq!(ms_b.get("/docs/b.md").unwrap().unwrap().size, 20);
    }

    /// sys_unlink on a DT_MOUNT path runs the full unmount lifecycle:
    /// metastore delete + dcache evict + routing remove. Replaces today's
    /// silent miss; callers no longer need a separate Python-side shim.
    #[test]
    fn test_sys_unlink_mount_root_delegates_to_dlc_unmount() {
        use crate::meta_store::{FileMetadata, MemoryMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        let ms = Arc::new(MemoryMetaStore::new());
        k.vfs_router.add_mount("/mnt", zone, None, false);
        let canon = crate::vfs_router::canonicalize_mount_path("/mnt", zone);
        k.vfs_router
            .install_metastore(&canon, ms.clone() as Arc<dyn crate::meta_store::MetaStore>);

        // Seed a DT_MOUNT entry at the mount root and a child file.
        let mount_meta = FileMetadata {
            path: "/mnt".to_string(),
            entry_type: DT_MOUNT,
            zone_id: Some(zone.to_string()),
            ..Default::default()
        };
        ms.put("/mnt", mount_meta).unwrap();

        let ctx = OperationContext::new("test", zone, true, None, true);
        let result = k.sys_unlink("/mnt", &ctx, false).unwrap();

        assert!(result.hit, "DT_MOUNT unlink should return hit=true");
        assert_eq!(result.entry_type, DT_MOUNT);

        // Mount is gone from the routing table
        assert!(
            !k.vfs_router.mount_points().iter().any(|m| m == "/mnt"),
            "mount point should have been removed from the routing table"
        );
    }
}
