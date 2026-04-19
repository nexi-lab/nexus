//! Kernel — pure Rust kernel owning all core state.
//!
//! Zero PyO3 dependency. All Python bridging lives in generated_pyo3.rs.
//!
//! Owns DCache, PathRouter, Trie, VFS Lock, Metastore.
//! Hook/Observer registries live in generated_pyo3::PyKernel (wrapper-only).
//!
//! Architecture:
//!   - Created empty via Kernel::new(), then components are wired by wrapper.
//!   - DCache/Router/Trie use interior mutability (&self methods).
//!   - VFS Lock is optionally Arc-shared with VFSLockManager (blocking acquire).
//!   - Metastore (Box<dyn Metastore>) wraps any impl (Python adapter, redb, gRPC).
//!
//! Issue #1868: Phase H — kernel boundary collapse.

use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_MOUNT, DT_PIPE, DT_REG, DT_STREAM};
use crate::dispatch::{FileEvent, FileEventType, MutationObserver, Trie};
use crate::file_watch::FileWatcher;
use crate::lock_manager::{LockManager, LockMode};
use crate::metastore::RedbMetastore;
use crate::mount_table::{
    canonicalize_mount_path as canonicalize, MountTable, RouteError, RustRouteResult,
};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// VFS gRPC client stubs — used by `try_remote_fetch` to pull blobs from
/// the origin node when metadata has been Raft-replicated but the CAS
/// blob lives on a remote peer. Generated from `proto/nexus/grpc/vfs/vfs.proto`
/// (see `build.rs`).
pub(crate) mod vfs_proto {
    tonic::include_proto!("nexus.grpc.vfs");
}

// ── KernelError ────────────────────────────────────────────────────────────

/// Kernel-level error type — pure Rust, no PyO3 dependency.
///
/// Error conversion to PyErr lives in generated_pyo3.rs.
#[derive(Debug)]
pub enum KernelError {
    InvalidPath(String),
    FileNotFound(String),
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
    /// True when the routed mount is an external connector — Python must handle.
    pub is_external: bool,
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
    pub backend_name: String,
    pub physical_path: String,
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
    pub lock: Option<crate::lock_manager::KernelLockInfo>,
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

use crate::dispatch::{
    DeleteHookCtx, HookContext, HookIdentity, NativeInterceptHook, ReadHookCtx, RenameHookCtx,
    WriteHookCtx,
};

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
    // split; both lookups now go through `MountTable` (F2 C2). Wrapped
    // in ``Arc`` so federation apply-event callbacks can look up the
    // current set of mounts-for-zone at invalidation time (a zone can
    // be mounted under multiple paths — direct + crosslink).
    pub(crate) mount_table: Arc<MountTable>,
    // PathTrie (owned)
    trie: Trie,
    // Unified lock manager: I/O lock + advisory lock + optional Raft.
    lock_manager: Arc<LockManager>,
    // Metastore (Box<dyn Metastore>)
    metastore: Option<Box<dyn crate::metastore::Metastore>>,
    // VFS lock timeout for blocking acquire (ms)
    vfs_lock_timeout_ms: u64,
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
    // long-lived observers: FileWatcher, EventBus, etc.). Each worker
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
    // FileWatcher — inotify equivalent. Arc-shared with observer registry.
    file_watches: Arc<FileWatcher>,
    // Agent registry — DashMap backing store (§10 B1)
    pub(crate) agent_registry: crate::agent_registry::AgentRegistry,
    // Per-mount metastores now live inside `MountTable::entries` as
    // `MountEntry::metastore: Option<Arc<dyn Metastore>>`. Federation
    // installs them via `MountTable::install_metastore` after the mount
    // is registered; standalone mode sets them during `add_mount` when
    // `metastore_path` is provided.
    // IPC registry — PipeManager owns DashMap<String, Arc<dyn PipeBackend>>
    pub(crate) pipe_manager: crate::pipe_manager::PipeManager,
    // IPC registry — StreamManager owns DashMap<String, Arc<dyn StreamBackend>>
    pub(crate) stream_manager: Arc<crate::stream_manager::StreamManager>,
    // Native hook registry — pure Rust hooks dispatched lock-free (§11 Phase 10)
    #[allow(dead_code)]
    pub(crate) native_hooks: Mutex<NativeHookRegistry>,
    // Node advertise address — set in federation mode so sys_write encodes
    // origin in backend_name (e.g. "cas-local@nexus-1:2126"). Enables
    // on-demand remote content fetch on other nodes.
    self_address: parking_lot::RwLock<Option<String>>,
    // Shared tokio runtime — constructed once at Kernel::new and used by
    // every peer RPC (scatter-gather chunk fetch + federation remote
    // reads). Replaces the one-shot `Builder::new_current_thread()` inside
    // `try_remote_fetch` so tokio's workers shut down cleanly on
    // `release_metastores`/Drop (addresses R11 hypothesis #2 — stuck async
    // task blocking `docker stop`).
    pub(crate) peer_client: Arc<crate::peer_blob_client::PeerBlobClient>,
    // Scatter-gather fetcher: drives bounded fan-out against
    // `backend_name.origins` whenever a local chunk miss occurs. Installed
    // on every `CASEngine` via `MountTable` on mount registration.
    #[allow(dead_code)]
    pub(crate) chunk_fetcher: Arc<crate::cas_remote::GrpcChunkFetcher>,
}

impl Kernel {
    // ── Constructor ────────────────────────────────────────────────────

    /// Create an empty kernel. Components wired by wrapper after construction.
    pub fn new() -> Self {
        let runtime = crate::peer_blob_client::build_kernel_runtime();
        let peer_client = Arc::new(crate::peer_blob_client::PeerBlobClient::new(Arc::clone(
            &runtime,
        )));
        let chunk_fetcher = Arc::new(crate::cas_remote::GrpcChunkFetcher::new(
            Arc::clone(&peer_client),
            None,
        ));
        Self {
            dlc: crate::dlc::DriverLifecycleCoordinator::new(),
            dcache: Arc::new(DCache::new()),
            mount_table: Arc::new(MountTable::new()),
            trie: Trie::new(),
            lock_manager: Arc::new(LockManager::new()),
            // Bare kernels boot with an in-memory metastore so tests,
            // quickstarts and minimal-mode boots have a working SSOT
            // without explicit wiring. `set_metastore_path` swaps it
            // for a redb-backed one on demand; federation installs a
            // per-mount `ZoneMetastore` via `install_mount_metastore`.
            metastore: Some(Box::new(crate::metastore::MemoryMetastore::new())),
            vfs_lock_timeout_ms: 5000,
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
            file_watches: Arc::new(FileWatcher::new()),
            agent_registry: crate::agent_registry::AgentRegistry::new(),
            pipe_manager: crate::pipe_manager::PipeManager::new(),
            stream_manager: Arc::new(crate::stream_manager::StreamManager::new()),
            native_hooks: Mutex::new(NativeHookRegistry::new()),
            self_address: parking_lot::RwLock::new(None),
            peer_client,
            chunk_fetcher,
        }
        // Observers registered on-demand (not at Kernel::new()).
        // FileWatcher + StreamEventObservers are registered by orchestrator
        // at boot time to avoid issues in lightweight test contexts.
    }

    // ── Lock Manager wiring ──────────────────────────────────────────

    /// Set VFS lock timeout in milliseconds (default 5000).
    pub fn set_vfs_lock_timeout(&mut self, timeout_ms: u64) {
        self.vfs_lock_timeout_ms = timeout_ms;
    }

    // ── Node identity (federation content origin) ─────────────────────

    /// Set this node's advertise address for origin-aware metadata.
    ///
    /// When set, `sys_write` encodes `backend_name` as `{name}@{addr}`
    /// so replicated metadata on other nodes knows where to fetch content.
    pub fn set_self_address(&self, addr: &str) {
        *self.self_address.write() = Some(addr.to_string());
    }

    /// Format backend name with origin address (if set).
    #[inline]
    fn origin_backend_name(&self, base_name: &str) -> String {
        match self.self_address.read().as_deref() {
            Some(addr) if !addr.is_empty() => format!("{}@{}", base_name, addr),
            _ => base_name.to_string(),
        }
    }

    // ── Metastore wiring ──────────────────────────────────────────────

    /// Wire RedbMetastore by path — Rust kernel opens redb directly.
    /// Only metastore wiring method (PyMetastoreAdapter removed in Phase 9).
    pub fn set_metastore_path(&mut self, path: &str) -> Result<(), KernelError> {
        let ms = RedbMetastore::open(std::path::Path::new(path))
            .map_err(|e| KernelError::IOError(format!("RedbMetastore: {e:?}")))?;
        self.metastore = Some(Box::new(ms));
        Ok(())
    }

    /// Drop the global metastore + every per-mount metastore so the
    /// underlying redb file handles are released. Python ``NexusFS.close``
    /// calls this so a subsequent kernel can reopen the same redb path
    /// without the ``"Database already open"`` error (Issue #3765 Cat-5/6
    /// SQLite-lifecycle regression).
    pub fn release_metastores(&mut self) {
        self.metastore = None;
        // Drop per-mount metastores by clearing their slot on each
        // MountEntry. We iterate via `iter_mut` to avoid a full rebuild.
        for mut entry in self.mount_table.entries_iter_mut() {
            entry.metastore = None;
        }
    }

    /// Resolve metastore for a syscall: per-mount first, then global fallback.
    ///
    /// In federation mode each mount has its own state machine (Raft-backed
    /// zone store). Standalone mode uses a single global metastore.
    /// `mount_point` must be the zone-canonical key from `mount_table.route()`.
    pub(crate) fn with_metastore<F, R>(&self, mount_point: &str, f: F) -> Option<R>
    where
        F: FnOnce(&dyn crate::metastore::Metastore) -> R,
    {
        // Hold the DashMap read guard only long enough to snapshot the
        // `Arc<dyn Metastore>`, then release it before running the closure
        // — avoids pinning the shard for the duration of a Raft propose.
        if let Some(entry) = self.mount_table.get_canonical(mount_point) {
            if let Some(ms) = entry.metastore.as_ref() {
                let ms_arc = Arc::clone(ms);
                drop(entry);
                return Some(f(ms_arc.as_ref()));
            }
        }
        self.metastore.as_ref().map(|ms| f(ms.as_ref()))
    }

    /// Variant of ``with_metastore`` that reports whether the returned
    /// metastore is per-mount or the global fallback. The caller uses this
    /// to decide whether to store a zone-relative key (per-mount) or a
    /// full global key (global fallback) — without the distinction, two
    /// mounts that share the global metastore collide on zone-relative
    /// paths like `/file.txt` (Issue #3765 Cat-8).
    pub(crate) fn with_metastore_scoped<F, R>(&self, mount_point: &str, f: F) -> Option<R>
    where
        F: FnOnce(&dyn crate::metastore::Metastore, bool) -> R,
    {
        if let Some(entry) = self.mount_table.get_canonical(mount_point) {
            if let Some(ms) = entry.metastore.as_ref() {
                let ms_arc = Arc::clone(ms);
                drop(entry);
                return Some(f(ms_arc.as_ref(), true));
            }
        }
        self.metastore.as_ref().map(|ms| f(ms.as_ref(), false))
    }

    // ── Zone-relative metastore key helpers ─────────────────────────────
    //
    // Per-zone metastores use zone-relative keys (like Linux per-superblock
    // inode paths: ext4 stores `/foo`, not `/mnt/disk1/foo`).
    //
    // Global path `/corp/eng/foo.txt` under mount `/corp` → zone key `/eng/foo.txt`.
    // DCache keeps global paths (correct — dcache spans all zones).

    /// Compute zone-relative metastore key from a route's `backend_path`.
    #[inline]
    fn zone_key(backend_path: &str) -> String {
        if backend_path.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", backend_path)
        }
    }

    /// Convert global path to zone-relative metastore key via routing.
    ///
    /// Returns `(mount_point, zone_relative_path)` where `mount_point` is
    /// the zone-canonical key for `with_metastore()`, and
    /// `zone_relative_path` is the key to use inside that metastore.
    fn resolve_metastore_key(&self, path: &str, zone_id: &str) -> (String, String) {
        match self.mount_table.route(path, zone_id, true, false) {
            Ok(route) => {
                let zone_path = Self::zone_key(&route.backend_path);
                (route.mount_point, zone_path)
            }
            Err(_) => (String::new(), path.to_string()),
        }
    }

    // ── Metastore proxy methods (for Python RustMetastoreProxy) ────────
    //
    // F2 C8: these route via ``mount_table.route(path, ROOT_ZONE_ID, ...)`` so a
    // lookup under a federation mount (e.g. ``/corp/eng/foo.txt``) lands on
    // the corresponding per-mount ``ZoneMetastore`` installed by
    // ``attach_raft_zone_to_kernel``. Without this, every Python-side
    // RustMetastoreProxy call went to the global kernel metastore and
    // federation data was invisible on follower nodes.
    //
    // R7: keys are now zone-relative (backend_path from route, prefixed
    // with `/`). Callers pass global paths; these methods translate.

    pub fn metastore_get(
        &self,
        path: &str,
    ) -> Result<Option<crate::metastore::FileMetadata>, KernelError> {
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        let global_path = path.to_string();
        let mount_point_owned = mount_point.clone();
        match self.with_metastore_scoped(&mount_point, |ms, is_per_mount| {
            let key = if is_per_mount {
                zone_path.as_str()
            } else {
                global_path.as_str()
            };
            (is_per_mount, ms.get(key))
        }) {
            Some((is_per_mount, result)) => result
                .map(|opt| {
                    opt.map(|mut m| {
                        // Convert zone-relative path back to global for Python callers.
                        if is_per_mount {
                            m.path =
                                crate::mount_table::zone_to_global(&mount_point_owned, &m.path);
                        }
                        m
                    })
                })
                .map_err(|e| KernelError::IOError(format!("metastore_get({path}): {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_put(
        &self,
        path: &str,
        mut metadata: crate::metastore::FileMetadata,
    ) -> Result<(), KernelError> {
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        let global_path = path.to_string();
        match self.with_metastore_scoped(&mount_point, move |ms, is_per_mount| {
            let key = if is_per_mount {
                zone_path.clone()
            } else {
                global_path.clone()
            };
            metadata.path = key.clone();
            ms.put(&key, metadata)
        }) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_put({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_delete(&self, path: &str) -> Result<bool, KernelError> {
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        let global_path = path.to_string();
        match self.with_metastore_scoped(&mount_point, |ms, is_per_mount| {
            let key = if is_per_mount {
                zone_path.as_str()
            } else {
                global_path.as_str()
            };
            ms.delete(key)
        }) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_delete({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_list(
        &self,
        prefix: &str,
    ) -> Result<Vec<crate::metastore::FileMetadata>, KernelError> {
        let route_path = if prefix.is_empty() { "/" } else { prefix };
        let (mount_point, zone_prefix) =
            self.resolve_metastore_key(route_path, contracts::ROOT_ZONE_ID);
        let global_prefix = if prefix.is_empty() {
            "/".to_string()
        } else {
            prefix.to_string()
        };
        // Per-mount metastore → zone-relative prefix; global fallback →
        // the caller's original (global) prefix so we don't spuriously match
        // entries that were stored with full global keys (Cat-8).
        let mount_point_for_conv = mount_point.clone();
        let routed_mount = mount_point.clone();
        let mut results: Vec<crate::metastore::FileMetadata> =
            match self.with_metastore_scoped(&mount_point, |ms, is_per_mount| {
                let list_prefix = if is_per_mount {
                    if prefix.is_empty() {
                        "/"
                    } else {
                        &zone_prefix
                    }
                } else {
                    global_prefix.as_str()
                };
                (is_per_mount, ms.list(list_prefix))
            }) {
                Some((is_per_mount, inner)) => inner
                    .map(|entries| {
                        entries
                            .into_iter()
                            .map(|mut m| {
                                if is_per_mount {
                                    m.path = crate::mount_table::zone_to_global(
                                        &mount_point_for_conv,
                                        &m.path,
                                    );
                                }
                                m
                            })
                            .collect::<Vec<_>>()
                    })
                    .map_err(|e| {
                        KernelError::IOError(format!("metastore_list({prefix}): {e:?}"))
                    })?,
                None => return Err(KernelError::IOError("no metastore wired".into())),
            };

        // F2 C5 follow-up: when the user-facing prefix spans MULTIPLE mounts
        // (e.g. prefix=`/personal/` with a mount at `/personal/alice`), the
        // routed metastore above only returns entries rooted on the parent
        // mount. Merge in each child mount's own per-mount metastore so the
        // caller sees the full subtree — including the mount roots themselves,
        // which are stored as zone-relative `/` inside each child metastore.
        let user_prefix = if prefix.is_empty() {
            "/".to_string()
        } else if prefix.ends_with('/') {
            prefix.to_string()
        } else {
            format!("{}/", prefix)
        };
        let user_prefix_trim = if user_prefix == "/" {
            ""
        } else {
            user_prefix.trim_end_matches('/')
        };
        for canonical in self.mount_table.canonical_keys() {
            if canonical == routed_mount {
                continue;
            }
            let (_zone, user_mp) = crate::mount_table::extract_zone_from_canonical(&canonical);
            // Child mount must sit strictly under the list prefix. Root list
            // (`/`) sees every mount. Non-root prefix `/a` matches `/a/b` but
            // not `/a` itself (caller already has the DT_MOUNT entry from the
            // parent metastore, or gets it via a separate sys_stat).
            let under_prefix = if user_prefix == "/" {
                user_mp != "/"
            } else {
                user_mp.starts_with(&user_prefix)
                    || user_mp == user_prefix_trim.to_string().as_str()
            };
            if !under_prefix {
                continue;
            }
            if let Some(Ok(child_entries)) = self.with_metastore(&canonical, |ms| ms.list("/")) {
                for mut meta in child_entries {
                    meta.path = crate::mount_table::zone_to_global(&canonical, &meta.path);
                    // `zone_to_global(mp, "/")` yields `"<mp>/"` which is the
                    // mount-root inode — the caller expects the non-slashed
                    // canonical mount point, so trim the trailing separator
                    // (but keep root `/` as-is).
                    if meta.path.len() > 1 && meta.path.ends_with('/') {
                        meta.path.pop();
                    }
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
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        let global_path = path.to_string();
        match self.with_metastore_scoped(&mount_point, |ms, is_per_mount| {
            let key = if is_per_mount {
                zone_path.as_str()
            } else {
                global_path.as_str()
            };
            ms.exists(key)
        }) {
            Some(result) => {
                result.map_err(|e| KernelError::IOError(format!("metastore_exists({path}): {e:?}")))
            }
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_get_batch(
        &self,
        paths: &[String],
    ) -> Result<Vec<Option<crate::metastore::FileMetadata>>, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .get_batch(paths)
                .map_err(|e| KernelError::IOError(format!("metastore_get_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    // Called by PyKernel.metastore_delete_batch() via PyO3 — no direct Rust caller.
    #[allow(dead_code)]
    pub fn metastore_delete_batch(&self, paths: &[String]) -> Result<usize, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .delete_batch(paths)
                .map_err(|e| KernelError::IOError(format!("metastore_delete_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_put_batch(
        &self,
        items: &[(String, crate::metastore::FileMetadata)],
    ) -> Result<(), KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .put_batch(items)
                .map_err(|e| KernelError::IOError(format!("metastore_put_batch: {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    /// OCC put. See `Metastore::put_if_version`.
    pub fn metastore_put_if_version(
        &self,
        mut metadata: crate::metastore::FileMetadata,
        expected_version: u32,
    ) -> Result<crate::metastore::PutIfVersionResult, KernelError> {
        let path = metadata.path.clone();
        let (mount_point, zone_path) = self.resolve_metastore_key(&path, contracts::ROOT_ZONE_ID);
        metadata.path = zone_path;
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
    /// `Metastore::rename_path`.
    pub fn metastore_rename_path(&self, old_path: &str, new_path: &str) -> Result<(), KernelError> {
        let (old_mp, old_zp) = self.resolve_metastore_key(old_path, contracts::ROOT_ZONE_ID);
        let (_new_mp, new_zp) = self.resolve_metastore_key(new_path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&old_mp, |ms| ms.rename_path(&old_zp, &new_zp)) {
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
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, move |ms| {
            ms.set_file_metadata(&zone_path, key, value)
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
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.get_file_metadata(&zone_path, key)) {
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
    ) -> Result<Vec<crate::metastore::PathValueStr>, KernelError> {
        // Bulk: fan out to the global metastore. Mixed-mount bulk reads
        // go through the Python wrapper.
        match &self.metastore {
            Some(ms) => ms.get_file_metadata_bulk(paths, key).map_err(|e| {
                KernelError::IOError(format!("metastore_get_file_metadata_bulk: {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_is_implicit_directory(&self, path: &str) -> Result<bool, KernelError> {
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        match self.with_metastore(&mount_point, |ms| ms.is_implicit_directory(&zone_path)) {
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
    ) -> Result<crate::metastore::PaginatedList, KernelError> {
        let route_path = if prefix.is_empty() { "/" } else { prefix };
        let (mount_point, zone_prefix) =
            self.resolve_metastore_key(route_path, contracts::ROOT_ZONE_ID);
        let list_prefix = if prefix.is_empty() { "/" } else { &zone_prefix };
        // Cursor is a metastore-internal key, pass as-is (already zone-relative
        // since cursors are produced by previous list calls on the same store).
        match self.with_metastore(&mount_point, |ms| {
            ms.list_paginated(list_prefix, recursive, limit, cursor)
        }) {
            Some(result) => result
                .map(|mut page| {
                    // Convert zone-relative paths back to global for Python callers.
                    for m in &mut page.items {
                        m.path = crate::mount_table::zone_to_global(&mount_point, &m.path);
                    }
                    page
                })
                .map_err(|e| {
                    KernelError::IOError(format!("metastore_list_paginated({prefix}): {e:?}"))
                }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_batch_get_content_ids(
        &self,
        paths: &[String],
    ) -> Result<Vec<crate::metastore::PathEtag>, KernelError> {
        match &self.metastore {
            Some(ms) => ms.batch_get_content_ids(paths).map_err(|e| {
                KernelError::IOError(format!("metastore_batch_get_content_ids: {e:?}"))
            }),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    // ── Advisory lock primitive (§4.4) ──────────────────────────

    /// Acquire or extend an advisory lock.
    ///
    /// `lock_id` empty → try-acquire (returns `Some(new_uuid)` or
    /// `None` on conflict). `lock_id` non-empty → extend TTL
    /// (returns `Some(lock_id)` or `None` if holder not found).
    #[allow(clippy::too_many_arguments)]
    pub fn sys_lock(
        &self,
        path: &str,
        lock_id: &str,
        mode: crate::lock_manager::KernelLockMode,
        max_holders: u32,
        ttl_secs: u64,
        holder_info: &str,
    ) -> Result<Option<String>, KernelError> {
        if lock_id.is_empty() {
            let generated_id = uuid::Uuid::new_v4().to_string();
            let acquired = self
                .lock_manager
                .acquire_lock(
                    path,
                    &generated_id,
                    mode,
                    max_holders,
                    ttl_secs,
                    holder_info,
                )
                .map_err(|e| KernelError::IOError(format!("sys_lock({path}): {e}")))?;
            Ok(if acquired { Some(generated_id) } else { None })
        } else {
            let extended = self
                .lock_manager
                .extend_lock(path, lock_id, ttl_secs)
                .map_err(|e| KernelError::IOError(format!("sys_lock({path}): {e}")))?;
            Ok(if extended {
                Some(lock_id.to_string())
            } else {
                None
            })
        }
    }

    /// Release a specific holder, or force-release all holders.
    pub fn sys_unlock(&self, path: &str, lock_id: &str, force: bool) -> Result<bool, KernelError> {
        if force {
            self.lock_manager
                .force_release_lock(path)
                .map_err(|e| KernelError::IOError(format!("sys_unlock({path}): {e}")))
        } else {
            self.lock_manager
                .release_lock(path, lock_id)
                .map_err(|e| KernelError::IOError(format!("sys_unlock({path}): {e}")))
        }
    }

    /// Enumerate locks under `prefix`, capped at `limit`.
    pub fn metastore_list_locks(
        &self,
        prefix: &str,
        limit: usize,
    ) -> Result<Vec<crate::lock_manager::KernelLockInfo>, KernelError> {
        self.lock_manager
            .list_locks(prefix, limit)
            .map_err(|e| KernelError::IOError(format!("metastore_list_locks({prefix}): {e}")))
    }

    /// Upgrade lock manager to distributed mode (federation DI).
    /// Sets Raft backend for advisory lock operations; I/O locks stay local.
    #[allow(dead_code)]
    pub fn upgrade_lock_manager(
        &self,
        node: nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) {
        self.lock_manager.upgrade_to_distributed(node, runtime);
    }

    // ── DCache proxy methods ───────────────────────────────────────────

    /// Insert or update a cache entry.
    #[allow(clippy::too_many_arguments)]
    pub fn dcache_put(
        &self,
        path: &str,
        backend_name: &str,
        physical_path: &str,
        size: u64,
        entry_type: u8,
        version: u32,
        etag: Option<&str>,
        zone_id: Option<&str>,
        mime_type: Option<&str>,
    ) {
        self.dcache.put(
            path,
            CachedEntry {
                backend_name: backend_name.to_string(),
                physical_path: physical_path.to_string(),
                size,
                etag: etag.map(|s| s.to_string()),
                version,
                entry_type,
                zone_id: zone_id.map(|s| s.to_string()),
                mime_type: mime_type.map(|s| s.to_string()),
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
    }

    /// Put a pre-built CachedEntry into the dcache. Used by DLC.mount().
    pub(crate) fn dcache_put_entry(&self, path: &str, entry: CachedEntry) {
        self.dcache.put(path, entry);
    }

    /// Get hot-path tuple: (backend_name, physical_path, entry_type).
    pub fn dcache_get(&self, path: &str) -> Option<(String, String, u8)> {
        self.dcache.get_hot(path)
    }

    /// Get full entry (returns CachedEntry for wrapper to convert).
    pub fn dcache_get_full(&self, path: &str) -> Option<CachedEntry> {
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
    pub(crate) fn dcache_handle(&self) -> Arc<DCache> {
        Arc::clone(&self.dcache)
    }

    /// Clone the shared MountTable ``Arc`` for federation apply-event
    /// callbacks that need to look up mount-points-for-zone at
    /// invalidation time. See ``dcache_handle`` for the lifetime
    /// rationale — same contract.
    pub(crate) fn mount_table_handle(&self) -> Arc<MountTable> {
        Arc::clone(&self.mount_table)
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
    /// Caller provides an optional pre-built `Metastore` impl (e.g.
    /// `RedbMetastore` for standalone, `ZoneMetastore` for federation).
    /// Kernel just installs it — it doesn't know or care which impl.
    ///
    /// When `raft_backend` is `Some` **and** `zone_id` is the root zone,
    /// the kernel automatically upgrades its `LockManager` to distributed
    /// mode (federation DI).
    #[allow(clippy::too_many_arguments)]
    pub fn add_mount(
        &self,
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
        self.mount_table.add_mount(
            mount_point,
            zone_id,
            readonly,
            admin_only,
            io_profile,
            backend_name,
            backend,
            is_external,
        );
        // Install per-mount metastore if provided. Must come AFTER the
        // entry is inserted so `install_metastore` finds it.
        if let Some(ms) = metastore {
            let canonical = canonicalize(mount_point, zone_id);
            self.mount_table.install_metastore(&canonical, ms);
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
            self.lock_manager.upgrade_to_distributed(node, runtime);
        }
        Ok(())
    }

    /// Remove a mount point (and its per-mount metastore if any).
    /// Called by DLC.unmount() — not directly exposed to Python.
    #[allow(dead_code)]
    pub fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.mount_table.remove(mount_point, zone_id)
    }

    /// Wire a per-mount `Metastore` impl into the kernel's mount table.
    ///
    /// Used by code that constructs a `Metastore` *outside* the kernel and
    /// wants the kernel's syscall fallback path to delegate to it for
    /// dcache misses on this mount. The canonical example is `rust/raft`'s
    /// `ZoneMetastore`, which wraps a `ZoneConsensus` state machine and is
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
        ms: Arc<dyn crate::metastore::Metastore>,
    ) {
        self.mount_table.install_metastore(&canonical_key, ms);
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
    pub fn route(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> Result<RustRouteResult, KernelError> {
        self.mount_table
            .route(path, zone_id, is_admin, check_write)
            .map_err(KernelError::from)
    }

    /// Check if a mount exists.
    pub fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.mount_table.has(mount_point, zone_id)
    }

    /// List all mount points (zone-canonical keys, sorted).
    pub fn get_mount_points(&self) -> Vec<String> {
        self.mount_table.canonical_keys()
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
        backend: Option<Box<dyn crate::backend::ObjectStore>>,
        metastore: Option<Arc<dyn crate::metastore::Metastore>>,
        raft_backend: Option<(
            nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
            tokio::runtime::Handle,
        )>,
        readonly: bool,
        admin_only: bool,
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
                // DT_MOUNT — full mount lifecycle via DLC
                self.dlc.mount(
                    self,
                    path,
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
                self.write_pipe_inode(path, capacity);
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
                self.write_pipe_inode(path, capacity);
                (None, None, None)
            }
            #[cfg(not(unix))]
            {
                return Err(KernelError::IOError("stdio pipes require unix".into()));
            }
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
                self.write_stream_inode(path, capacity);
                (Some(shm), Some(dfd))
            }
            #[cfg(not(unix))]
            {
                return Err(KernelError::IOError(
                    "shared_memory streams require unix".into(),
                ));
            }
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
    fn write_pipe_inode(&self, path: &str, capacity: usize) {
        let route = self
            .mount_table
            .route(path, contracts::ROOT_ZONE_ID, true, false);
        let (mount_point, zone_path) = match &route {
            Ok(r) => (r.mount_point.clone(), Self::zone_key(&r.backend_path)),
            Err(_) => (String::new(), path.to_string()),
        };
        self.with_metastore(&mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: zone_path.clone(),
                backend_name: "pipe".to_string(),
                physical_path: "shm://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_PIPE,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                target_zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(&zone_path, meta);
        });
        self.dcache.put(
            path,
            CachedEntry {
                backend_name: "pipe".to_string(),
                physical_path: "shm://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_PIPE,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
    }

    /// Write DT_STREAM inode to metastore + dcache (shared by create_stream and SHM path).
    #[allow(dead_code)]
    fn write_stream_inode(&self, path: &str, capacity: usize) {
        let route = self
            .mount_table
            .route(path, contracts::ROOT_ZONE_ID, true, false);
        let (mount_point, zone_path) = match &route {
            Ok(r) => (r.mount_point.clone(), Self::zone_key(&r.backend_path)),
            Err(_) => (String::new(), path.to_string()),
        };
        self.with_metastore(&mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: zone_path.clone(),
                backend_name: "stream".to_string(),
                physical_path: "shm://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_STREAM,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                target_zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(&zone_path, meta);
        });
        self.dcache.put(
            path,
            CachedEntry {
                backend_name: "stream".to_string(),
                physical_path: "shm://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_STREAM,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );
    }

    /// DT_DIR: create directory inode via metastore + dcache.
    fn setattr_create_dir(
        &self,
        path: &str,
        zone_id: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Route first to get zone-relative key
        let (mount_point, zone_path) = self.resolve_metastore_key(path, zone_id);

        // Idempotent: if DT_DIR (or DT_MOUNT, which is directory-like since
        // a mount point IS a directory) already exists, no-op. This matches
        // ``mkdir(exist_ok=True)`` semantics — a mount creates the directory
        // slot, so a follow-up mkdir on the same path shouldn't fail.
        let existing = self
            .with_metastore(&mount_point, |ms| ms.get(&zone_path).ok().flatten())
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

        let meta = crate::metastore::FileMetadata {
            path: zone_path.clone(),
            backend_name: String::new(),
            physical_path: contracts::BLAKE3_EMPTY.to_string(),
            size: 0,
            etag: Some(contracts::BLAKE3_EMPTY.to_string()),
            version: 1,
            entry_type: DT_DIR,
            zone_id: Some(zone_id.to_string()),
            target_zone_id: None,
            mime_type: Some("inode/directory".to_string()),
            created_at_ms: Some(now_ms),
            modified_at_ms: Some(now_ms),
        };

        // Write to metastore (routed via mount_point) — zone-relative key
        self.with_metastore(&mount_point, |ms| {
            let _ = ms.put(&zone_path, meta);
        });

        // DCache entry
        self.dcache.put(
            path,
            CachedEntry {
                backend_name: String::new(),
                physical_path: contracts::BLAKE3_EMPTY.to_string(),
                size: 0,
                etag: Some(contracts::BLAKE3_EMPTY.to_string()),
                version: 1,
                entry_type: DT_DIR,
                zone_id: Some(zone_id.to_string()),
                mime_type: Some("inode/directory".to_string()),
                created_at_ms: Some(now_ms),
                modified_at_ms: Some(now_ms),
            },
        );

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
    //
    // These methods are pre-built infrastructure for §11 Phases 3/5/6:
    //   - Phase 3: replaces the inline loop with a `ThreadPool::execute()`
    //     submission so observers run off the syscall hot path.
    //   - Phase 5: kernel sys_* methods call `dispatch_observers` after
    //     each successful mutation.
    //   - Phase 6: PyKernel.register_observer rewires from the legacy
    //     `Py<PyAny>`-based ObserverRegistry to this Rust-typed registry,
    //     and the legacy registry is deleted.
    //
    // No production caller exists yet, hence #[allow(dead_code)] — the
    // attribute is removed when Phase 5 wires the first call site.

    ///
    /// `OBSERVE_INLINE` was deleted in §11 Phase 2 — all OBSERVE callbacks
    /// run on `observer_pool` (the kernel's background ThreadPool). There
    /// is no other mode. Observers needing synchronous-blocking semantics
    /// must be moved to INTERCEPT POST.
    #[allow(dead_code)]
    pub fn register_observer(
        &self,
        observer: Arc<dyn MutationObserver>,
        name: String,
        event_mask: u32,
    ) {
        self.observers.lock().register(observer, name, event_mask);
    }

    /// Unregister observer by name. Returns true if removed.
    #[allow(dead_code)]
    pub fn unregister_observer(&self, name: &str) -> bool {
        self.observers.lock().unregister(name)
    }

    /// OBSERVE-phase dispatch — call all matching observers inline.
    ///
    /// Fire-and-forget by contract. Observers are pure Rust (~0.5μs each:
    /// FileWatcher Condvar notify + StreamEventObserver stream_write_nowait).
    /// Inline dispatch avoids ThreadPool + fork() incompatibility in xdist CI.
    ///
    /// Snapshot-then-drop-lock pattern: collect Arc clones under the registry
    /// lock, release lock, then call each observer. Prevents deadlocks if an
    /// observer re-enters the kernel.
    ///
    /// Called by every successful Tier 1 mutation syscall via dispatch_mutation.
    pub fn dispatch_observers(&self, event: &FileEvent) {
        let observers = self.observers.lock().matching(event.event_type as u32);
        for obs in observers {
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                obs.on_mutation(event);
            }));
        }
    }

    /// No-op — observers dispatch inline (no background pool).
    /// Kept for API compat with tests that call flush_observers().
    pub fn flush_observers(&self) {}

    /// Total registered Rust-native observers.
    pub fn observer_count(&self) -> usize {
        self.observers.lock().count()
    }

    /// Dispatch a manually constructed FileEvent (for DLC mount/unmount, Python fallback).
    pub fn dispatch_event(&self, event_type: FileEventType, path: &str) {
        let event = FileEvent::new(event_type, path);
        self.dispatch_observers(&event);
    }

    /// Helper: build a `FileEvent` pre-populated with the syscall's
    /// `OperationContext` identity fields (zone_id, user_id, agent_id),
    /// then apply caller-provided extras and dispatch.
    ///
    /// Used by sys_* methods to keep the per-syscall dispatch site to
    /// 3-4 lines instead of a 15-field struct literal. Fast path: when
    /// no observers are registered, `dispatch_observers` is an early
    /// return after a single Mutex acquire — the FileEvent construction
    /// is essentially free against any observer-bearing workload, so
    /// there's no point in gating it behind a count check.
    #[inline]
    fn dispatch_mutation(
        &self,
        event_type: FileEventType,
        path: &str,
        ctx: &OperationContext,
        extra: impl FnOnce(&mut FileEvent),
    ) {
        let mut event = FileEvent::new(event_type, path);
        event.zone_id = Some(ctx.zone_id.clone());
        if !ctx.user_id.is_empty() {
            event.user_id = Some(ctx.user_id.clone());
        }
        event.agent_id = ctx.agent_id.clone();
        extra(&mut event);
        self.dispatch_observers(&event);
    }

    // observer_count() is defined above (Rust-native + event buffers).

    // ── Native INTERCEPT hook dispatch (§11 Phase 14) ─────────────────

    /// Dispatch PRE-INTERCEPT hooks from NativeHookRegistry.
    /// Returns Err(KernelError) if any hook aborts.
    /// No-op when registry is empty (zero-cost lock check).
    pub fn dispatch_native_pre(&self, ctx: &HookContext) -> Result<(), KernelError> {
        let registry = self.native_hooks.lock();
        if registry.count() == 0 {
            return Ok(());
        }
        registry
            .dispatch_pre(ctx)
            .map_err(KernelError::PermissionDenied)
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

    /// Register a file watch pattern. Returns watch ID.
    pub fn register_watch(&self, pattern: &str) -> u64 {
        self.file_watches.register(pattern)
    }

    /// Unregister a file watch by ID.
    pub fn unregister_watch(&self, watch_id: u64) -> bool {
        self.file_watches.unregister(watch_id)
    }

    /// Match a path against all registered watch patterns.
    /// Returns list of matching watch IDs.
    pub fn match_watches(&self, path: &str) -> Vec<u64> {
        self.file_watches.match_path(path)
    }

    /// sys_watch — block until a file event matching the pattern arrives, or timeout.
    /// Tier 1 syscall (inotify equivalent). Returns matching FileEvent or None on timeout.
    pub fn sys_watch(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent> {
        self.file_watches.wait_for_event(pattern, timeout_ms)
    }

    // ── IPC Registry — Pipe methods (delegates to PipeManager) ──────────

    /// Create a pipe buffer in the IPC registry.
    ///
    /// PipeManager owns the buffer; Kernel persists DT_PIPE inode to
    /// metastore + dcache so sys_read/sys_write dispatch to IPC fast-path.
    pub fn create_pipe(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        self.pipe_manager
            .create(path, capacity)
            .map_err(pipe_mgr_err)?;

        // Persist DT_PIPE inode (best-effort — metastore may not be wired in tests).
        let route = self
            .mount_table
            .route(path, contracts::ROOT_ZONE_ID, true, false);
        let (mount_point, zone_path) = match &route {
            Ok(r) => (r.mount_point.clone(), Self::zone_key(&r.backend_path)),
            Err(_) => (String::new(), path.to_string()),
        };
        self.with_metastore(&mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: zone_path.clone(),
                backend_name: "pipe".to_string(),
                physical_path: "mem://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_PIPE,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                target_zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(&zone_path, meta);
        });

        self.dcache.put(
            path,
            CachedEntry {
                backend_name: "pipe".to_string(),
                physical_path: "mem://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_PIPE,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        Ok(())
    }

    /// Destroy a pipe buffer.
    pub fn destroy_pipe(&self, path: &str) -> Result<(), KernelError> {
        self.pipe_manager.destroy(path).map_err(pipe_mgr_err)?;

        // Remove DT_PIPE inode (best-effort) — zone-relative key
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        self.with_metastore(&mount_point, |ms| {
            let _ = ms.delete(&zone_path);
        });
        self.dcache.evict(path);

        Ok(())
    }

    /// Close a pipe (signal close, keep in registry for drain).
    pub fn close_pipe(&self, path: &str) -> Result<(), KernelError> {
        self.pipe_manager.close(path).map_err(pipe_mgr_err)
    }

    /// Check if a pipe exists.
    pub fn has_pipe(&self, path: &str) -> bool {
        self.pipe_manager.has(path)
    }

    /// Non-blocking write to a pipe. Returns bytes written.
    pub fn pipe_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        self.pipe_manager
            .write_nowait(path, data)
            .map_err(pipe_mgr_err)
    }

    /// Non-blocking read from a pipe. Returns data or None if empty.
    pub fn pipe_read_nowait(&self, path: &str) -> Result<Option<Vec<u8>>, KernelError> {
        self.pipe_manager.read_nowait(path).map_err(pipe_mgr_err)
    }

    /// List all pipes with their paths.
    pub fn list_pipes(&self) -> Vec<String> {
        self.pipe_manager.list()
    }

    /// Blocking read — Condvar wait (GIL-free via py.allow_threads).
    /// Called from generated_pyo3.rs PyKernel wrapper.
    #[allow(dead_code)]
    pub fn pipe_read_blocking(&self, path: &str, timeout_ms: u64) -> Result<Vec<u8>, KernelError> {
        self.pipe_manager
            .read_blocking(path, timeout_ms)
            .map_err(pipe_mgr_err)
    }

    /// Close all pipes (shutdown).
    pub fn close_all_pipes(&self) {
        self.pipe_manager.close_all();
    }

    // ── IPC Registry — Stream methods (delegates to StreamManager) ────

    /// Create a stream buffer in the IPC registry.
    pub fn create_stream(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        self.stream_manager
            .create(path, capacity)
            .map_err(stream_mgr_err)?;

        let route = self
            .mount_table
            .route(path, contracts::ROOT_ZONE_ID, true, false);
        let (mount_point, zone_path) = match &route {
            Ok(r) => (r.mount_point.clone(), Self::zone_key(&r.backend_path)),
            Err(_) => (String::new(), path.to_string()),
        };
        self.with_metastore(&mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: zone_path.clone(),
                backend_name: "stream".to_string(),
                physical_path: "mem://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_STREAM,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                target_zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(&zone_path, meta);
        });

        self.dcache.put(
            path,
            CachedEntry {
                backend_name: "stream".to_string(),
                physical_path: "mem://".to_string(),
                size: capacity as u64,
                etag: None,
                version: 1,
                entry_type: DT_STREAM,
                zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        Ok(())
    }

    /// Destroy a stream buffer.
    pub fn destroy_stream(&self, path: &str) -> Result<(), KernelError> {
        self.stream_manager.destroy(path).map_err(stream_mgr_err)?;

        // Remove DT_STREAM inode (best-effort) — zone-relative key
        let (mount_point, zone_path) = self.resolve_metastore_key(path, contracts::ROOT_ZONE_ID);
        self.with_metastore(&mount_point, |ms| {
            let _ = ms.delete(&zone_path);
        });
        self.dcache.evict(path);

        Ok(())
    }

    /// Close a stream (signal close, keep in registry for drain).
    pub fn close_stream(&self, path: &str) -> Result<(), KernelError> {
        self.stream_manager.close(path).map_err(stream_mgr_err)
    }

    /// Check if a stream exists.
    pub fn has_stream(&self, path: &str) -> bool {
        self.stream_manager.has(path)
    }

    /// Non-blocking write to a stream. Returns byte offset.
    pub fn stream_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        self.stream_manager
            .write_nowait(path, data)
            .map_err(stream_mgr_err)
    }

    /// Read one message at byte offset. Returns (data, next_offset) or None if empty.
    pub fn stream_read_at(
        &self,
        path: &str,
        offset: usize,
    ) -> Result<Option<(Vec<u8>, usize)>, KernelError> {
        self.stream_manager
            .read_at(path, offset)
            .map_err(stream_mgr_err)
    }

    /// Read up to `count` messages starting from byte offset.
    pub fn stream_read_batch(
        &self,
        path: &str,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), KernelError> {
        self.stream_manager
            .read_batch(path, offset, count)
            .map_err(stream_mgr_err)
    }

    /// Collect all stream payloads from offset 0, concatenated.
    ///
    /// Replaces the manual `read_at` loop in Python LLM backends.
    /// Single Rust call → no per-frame PyO3 round-trip.
    pub fn stream_collect_all(&self, path: &str) -> Result<Vec<u8>, KernelError> {
        self.stream_manager
            .collect_all_payloads(path)
            .map_err(stream_mgr_err)
    }

    /// List all streams with their paths.
    pub fn list_streams(&self) -> Vec<String> {
        self.stream_manager.list()
    }

    /// Blocking read at offset — Condvar wait (GIL-free via py.allow_threads).
    /// Called from generated_pyo3.rs PyKernel wrapper.
    #[allow(dead_code)]
    pub fn stream_read_at_blocking(
        &self,
        path: &str,
        offset: usize,
        timeout_ms: u64,
    ) -> Result<(Vec<u8>, usize), KernelError> {
        self.stream_manager
            .read_at_blocking(path, offset, timeout_ms)
            .map_err(stream_mgr_err)
    }

    /// Close all streams (shutdown).
    pub fn close_all_streams(&self) {
        self.stream_manager.close_all();
    }

    // ── sys_read ───────────────────────────────────────────────────────

    /// Rust syscall: read file content (pure Rust, no GIL).
    ///
    /// validate -> route -> dcache -> [metastore fallback] -> VFS lock -> CAS read -> return.
    ///
    /// DCache hit = hot path. DCache miss = cold path: queries metastore, populates dcache,
    /// then continues with CAS read.
    ///
    /// DT_REG success: returns `Ok(SysReadResult { data: Some(bytes), ... })`.
    /// DT_REG miss (including remote-fetch failure) returns
    /// `Err(KernelError::FileNotFound)` — no Python-side fallback.
    ///
    /// DT_PIPE / DT_STREAM still surface to the wrapper (entry_type set,
    /// data may be None) so Python IPC dispatch keeps working. Their
    /// migration is out of scope here.
    ///
    /// Hooks are NOT dispatched here — wrapper handles PRE-INTERCEPT.
    pub fn sys_read(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        // 1. Validate
        validate_path_fast(path)?;

        // 1b. Trie-resolved virtual paths (§11 Phase 21) — Python's resolve_read
        // should have handled these before reaching us; treat as missing.
        if self.trie.lookup(path).is_some() {
            return Err(not_found());
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14) — permission check etc.
        let hook_id = HookIdentity {
            user_id: ctx.user_id.clone(),
            zone_id: ctx.zone_id.clone(),
            agent_id: ctx.agent_id.clone().unwrap_or_default(),
            is_admin: ctx.is_admin,
        };
        self.dispatch_native_pre(&HookContext::Read(ReadHookCtx {
            path: path.to_string(),
            identity: hook_id,
            content: None,
            content_hash: None,
        }))?;

        // 2. Route (pure Rust LPM)
        let route = match self
            .mount_table
            .route(path, &ctx.zone_id, ctx.is_admin, false)
        {
            Ok(r) => r,
            Err(_) => return Err(not_found()),
        };

        // 2b. External mount — signal Python to handle via connector backend
        if route.is_external {
            return Ok(SysReadResult {
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: 0,
                is_external: true,
            });
        }

        // 3. DCache lookup — on miss, fallback to metastore (cold path)
        let zone_path = Self::zone_key(&route.backend_path);
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                // Metastore fallback (per-mount first, then global) — zone-relative key
                match self.with_metastore(&route.mount_point, |ms| ms.get(&zone_path)) {
                    Some(Ok(Some(meta))) => {
                        // Populate dcache from metastore result
                        let cached = CachedEntry {
                            backend_name: meta.backend_name.clone(),
                            physical_path: meta.physical_path.clone(),
                            size: meta.size,
                            etag: meta.etag.clone(),
                            version: meta.version,
                            entry_type: meta.entry_type,
                            zone_id: meta.zone_id.clone(),
                            mime_type: meta.mime_type.clone(),
                            created_at_ms: None,
                            modified_at_ms: None,
                        };
                        self.dcache.put(path, cached);
                        // Re-fetch from dcache (now populated)
                        self.dcache.get_entry(path).unwrap()
                    }
                    Some(Ok(None)) | Some(Err(_)) | None => return Err(not_found()),
                }
            }
        };

        // DT_PIPE — try Rust IPC registry (nowait pop)
        if entry.entry_type == DT_PIPE {
            if let Some(buf) = self.pipe_manager.get(path) {
                match buf.pop() {
                    Ok(data) => {
                        return Ok(SysReadResult {
                            data: Some(data),
                            post_hook_needed: false,
                            content_hash: None,
                            entry_type: DT_PIPE,
                            is_external: false,
                        });
                    }
                    Err(crate::pipe::PipeError::Empty) => {
                        // Empty — surface DT_PIPE so Python async shell retries.
                        return Ok(SysReadResult {
                            data: None,
                            post_hook_needed: false,
                            content_hash: None,
                            entry_type: DT_PIPE,
                            is_external: false,
                        });
                    }
                    Err(crate::pipe::PipeError::ClosedEmpty) => {
                        return Err(KernelError::PipeClosed(path.to_string()));
                    }
                    Err(_) => {}
                }
            }
            // Not in Rust registry — fall through to Python fallback.
            return Ok(SysReadResult {
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: DT_PIPE,
                is_external: false,
            });
        }

        // DT_STREAM — surface to wrapper so Python stream_read_at handles offset.
        if entry.entry_type == DT_STREAM {
            return Ok(SysReadResult {
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: DT_STREAM,
                is_external: false,
            });
        }

        // Content identifier: CAS backends use etag (hash), path backends
        // use physical_path. Either must be non-empty to attempt a read.
        let content_id = entry.etag.as_deref().filter(|s| !s.is_empty()).or_else(|| {
            let pp = entry.physical_path.as_str();
            if pp.is_empty() {
                None
            } else {
                Some(pp)
            }
        });
        let content_id = match content_id {
            Some(id) => id,
            None => return Err(not_found()),
        };

        // 4. VFS lock (blocking acquire — wrapper releases GIL before calling this)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Read, self.vfs_lock_timeout_ms);
        if lock_handle == 0 {
            return Err(KernelError::IOError(format!(
                "vfs read lock timeout: {path}"
            )));
        }

        // 5. Backend read (CasLocal or PyObjectStoreAdapter)
        let content =
            self.mount_table
                .read_content(&route.mount_point, content_id, &route.backend_path, ctx);

        // 6. Release VFS lock (always, even on miss)
        self.lock_manager.do_release(lock_handle);

        // 7. Return result
        match content {
            Some(data) => Ok(SysReadResult {
                data: Some(data),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_hash: entry.etag.clone(),
                entry_type: DT_REG,
                is_external: false,
            }),
            // Local backend miss + metadata exists → federation path:
            // try the origin encoded in backend_name. Otherwise it's a
            // genuine miss.
            None => self.try_remote_fetch(path, &entry, &route.mount_point, ctx),
        }
    }

    /// Federation on-demand content fetch.
    ///
    /// When local CAS has no blob but metadata does, `backend_name` may
    /// carry an origin (`cas-local@nexus-1:2028`). We call VFS `ReadBlob`
    /// on that origin to pull the blob. Used by follower nodes after
    /// metadata has been Raft-replicated ahead of content.
    ///
    /// Returns `Err(FileNotFound)` if:
    /// - backend_name has no `@origin` (local-only backend)
    /// - origin equals `self_address` (we ARE the origin — blob is gone)
    /// - entry has no content hash
    /// - the remote call fails
    fn try_remote_fetch(
        &self,
        path: &str,
        entry: &CachedEntry,
        mount_point: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        // Parse "{type}@{host:port[,host:port...]}" — take first origin.
        let origin = match entry.backend_name.split_once('@') {
            Some((_, origins)) => origins
                .split(',')
                .next()
                .map(str::trim)
                .filter(|s| !s.is_empty()),
            None => None,
        };
        let origin = match origin {
            Some(o) => o,
            None => return Err(not_found()),
        };

        // Don't loop back to self — we're the origin, blob is truly missing.
        if let Some(addr) = self.self_address.read().as_deref() {
            if origin == addr {
                return Err(not_found());
            }
        }

        // Need a content hash for ReadBlob.
        let content_hash = entry
            .etag
            .as_deref()
            .filter(|s| !s.is_empty())
            .ok_or_else(not_found)?
            .to_string();

        // Drive the RPC on the kernel-owned shared runtime — reusing the
        // pooled tonic Channel from `peer_client`. No more one-shot
        // `new_current_thread()` per call (that pattern left the runtime
        // lingering if the future hadn't finished draining; see R11
        // hypothesis #2).
        let data = self
            .peer_client
            .fetch_blob(origin, &content_hash)
            .map_err(KernelError::IOError)?;

        // Cache the remote-fetched blob into the local mount backend so
        // subsequent reads hit locally. Critical for failover: once the
        // origin goes down, re-fetch would fail but the blob must still
        // be readable. write_content is idempotent for CAS backends.
        let _ = self
            .mount_table
            .write_content(mount_point, &data, &content_hash, ctx);

        Ok(SysReadResult {
            data: Some(data),
            post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
            content_hash: entry.etag.clone(),
            entry_type: DT_REG,
            is_external: false,
        })
    }

    // ── sys_write ──────────────────────────────────────────────────────

    /// Rust syscall: write file content (pure Rust, no GIL).
    ///
    /// validate -> route -> VFS lock -> CAS write -> metadata build -> metastore.put
    /// -> dcache update -> return.
    ///
    /// Hooks are NOT dispatched here — wrapper handles PRE-INTERCEPT.
    pub fn sys_write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
    ) -> Result<SysWriteResult, KernelError> {
        let miss = || {
            Ok(SysWriteResult {
                hit: false,
                content_id: None,
                post_hook_needed: false,
                version: 0,
                size: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 1b. Trie-resolved virtual paths (§11 Phase 21)
        if self.trie.lookup(path).is_some() {
            return miss();
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Write(WriteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
            content: content.to_vec(),
            is_new_file: false,
            content_hash: None,
            new_version: 0,
        }))?;

        // 2. Route (check write access)
        let route = match self
            .mount_table
            .route(path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache check — DT_PIPE/DT_STREAM: try Rust IPC registry
        if let Some(entry) = self.dcache.get_entry(path) {
            if entry.entry_type == DT_PIPE {
                if let Some(buf) = self.pipe_manager.get(path) {
                    match buf.push(content) {
                        Ok(n) => {
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: false,
                                version: 0,
                                size: n as u64,
                            });
                        }
                        Err(crate::pipe::PipeError::Full(_, _)) => {
                            // Full — return miss so Python async shell retries
                            return miss();
                        }
                        Err(crate::pipe::PipeError::Closed(msg)) => {
                            return Err(KernelError::PipeClosed(msg.to_string()));
                        }
                        Err(_) => {}
                    }
                }
                return miss();
            }
            if entry.entry_type == DT_STREAM {
                if let Some(buf) = self.stream_manager.get(path) {
                    match buf.push(content) {
                        Ok(offset) => {
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: false,
                                version: 0,
                                size: offset as u64,
                            });
                        }
                        Err(crate::stream::StreamError::Full(_, _)) => return miss(),
                        Err(crate::stream::StreamError::Closed(msg)) => {
                            return Err(KernelError::StreamClosed(msg.to_string()));
                        }
                        Err(_) => {}
                    }
                }
                return miss();
            }
        }

        // 4. VFS lock (blocking write lock)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms);
        if lock_handle == 0 {
            return miss();
        }

        // 5. Backend write (CasLocal or PyObjectStoreAdapter)
        //    Pass backend_path as content_id (CAS ignores it, PAS uses it as blob path).
        let zone_path = Self::zone_key(&route.backend_path);
        let write_result = match self.mount_table.write_content(
            &route.mount_point,
            content,
            &route.backend_path,
            ctx,
        ) {
            Ok(opt) => opt,
            Err(storage_err) => {
                // Storage/backend-level failure (connector wrapper raised a
                // BackendError, disk full, permission denied, etc.). Release
                // the VFS lock and surface the error to Python so callers
                // can react (F2 C4 / Issue #3765 Cat-7 regression — previous
                // code silently swallowed this via ``.ok()``).
                self.lock_manager.do_release(lock_handle);
                return Err(KernelError::BackendError(format!("{storage_err:?}")));
            }
        };

        // 6. After write -> build metadata + metastore.put + dcache update
        let result = match write_result {
            Some(wr) => {
                // Snapshot old dcache state for the OBSERVE event payload
                // (is_new + old_etag fields). Done before metastore.put so
                // we capture the pre-write version, not the new one.
                let old_entry = self.dcache.get_entry(path);
                let old_version = old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                let old_etag = old_entry.as_ref().and_then(|e| e.etag.clone());
                let new_version = old_version + 1;

                // Build FileMetadata and persist via metastore (per-mount or global)
                let now_ms = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0);
                let raw_backend_name = self
                    .mount_table
                    .get_canonical(&route.mount_point)
                    .map(|e| e.backend_name.clone())
                    .unwrap_or_else(|| route.io_profile.clone());
                let backend_display_name = self.origin_backend_name(&raw_backend_name);
                let created_at_ms = old_entry
                    .as_ref()
                    .and_then(|e| e.created_at_ms)
                    .or(Some(now_ms));
                // When the mount has its own metastore, use the zone-relative
                // key. When we fall back to the global metastore (Cat-8 bug
                // — two un-federated mounts both hitting the single global
                // store), use the full global path so the keys don't collide.
                self.with_metastore_scoped(&route.mount_point, |ms, is_per_mount| {
                    let key = if is_per_mount {
                        zone_path.clone()
                    } else {
                        path.to_string()
                    };
                    let meta = crate::metastore::FileMetadata {
                        path: key.clone(),
                        backend_name: backend_display_name.clone(),
                        physical_path: wr.content_id.clone(),
                        size: wr.size,
                        etag: Some(wr.content_id.clone()),
                        version: new_version,
                        entry_type: DT_REG,
                        zone_id: Some(ctx.zone_id.clone()),
                        target_zone_id: None,
                        mime_type: None,
                        created_at_ms,
                        modified_at_ms: Some(now_ms),
                    };
                    // Best-effort metastore.put -- error logged but doesn't fail write
                    let _ = ms.put(&key, meta);
                });

                // Update dcache with new metadata
                self.dcache.put(
                    path,
                    CachedEntry {
                        backend_name: backend_display_name,
                        physical_path: wr.content_id.clone(),
                        size: wr.size,
                        etag: Some(wr.content_id.clone()),
                        version: new_version,
                        entry_type: DT_REG,
                        zone_id: Some(ctx.zone_id.clone()),
                        mime_type: None,
                        created_at_ms,
                        modified_at_ms: Some(now_ms),
                    },
                );

                // OBSERVE-phase dispatch (§11 Phase 5): queue FileWrite to
                // the kernel observer ThreadPool. Returns immediately —
                // observer callbacks run off the syscall hot path.
                let etag = wr.content_id.clone();
                let size = wr.size;
                self.dispatch_mutation(FileEventType::FileWrite, path, ctx, |ev| {
                    ev.size = Some(size);
                    ev.etag = Some(etag);
                    ev.version = Some(new_version);
                    ev.is_new = old_version == 0;
                    ev.old_etag = old_etag;
                });

                Ok(SysWriteResult {
                    hit: true,
                    content_id: Some(wr.content_id),
                    post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0,
                    version: new_version,
                    size: wr.size,
                })
            }
            None => miss(),
        };

        // 7. Release VFS lock (always, even on miss)
        self.lock_manager.do_release(lock_handle);

        result
    }

    // ── sys_stat ───────────────────────────────────────────────────────

    /// Rust syscall: get file metadata (pure Rust, no GIL).
    ///
    /// validate -> route -> dcache lookup -> return StatResult.
    /// Returns None on dcache miss or trie-resolved paths (wrapper handles).
    pub fn sys_stat(&self, path: &str, zone_id: &str, is_admin: bool) -> Option<StatResult> {
        // 1. Validate
        if validate_path_fast(path).is_err() {
            return None;
        }

        // 2. Trie-resolved paths -> wrapper handles
        if self.trie.lookup(path).is_some() {
            return None;
        }

        // 3. Route
        let route = self
            .mount_table
            .route(path, zone_id, is_admin, false)
            .ok()?;

        // 4. DCache lookup. On miss, fall back to the per-mount metastore
        //    so federation zones see inodes that haven't been cached yet
        //    (F2 C5 — matches sys_read's cold path). Zone-relative key.
        let zone_path = Self::zone_key(&route.backend_path);
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                let meta = self
                    .with_metastore(&route.mount_point, |ms| ms.get(&zone_path).ok().flatten())
                    .flatten()?;
                let cached = CachedEntry {
                    backend_name: meta.backend_name.clone(),
                    physical_path: meta.physical_path.clone(),
                    size: meta.size,
                    etag: meta.etag.clone(),
                    version: meta.version,
                    entry_type: meta.entry_type,
                    zone_id: meta.zone_id.clone(),
                    mime_type: meta.mime_type.clone(),
                    created_at_ms: meta.created_at_ms,
                    modified_at_ms: meta.modified_at_ms,
                };
                self.dcache.put(path, cached.clone());
                cached
            }
        };

        // Treat DT_MOUNT like a directory for VFS callers — a mount point is
        // the zone-root inode, analogous to a DT_DIR from the user's view.
        let is_dir = entry.entry_type == DT_DIR || entry.entry_type == DT_MOUNT;
        let mime = entry
            .mime_type
            .as_deref()
            .unwrap_or(if is_dir {
                "inode/directory"
            } else {
                "application/octet-stream"
            })
            .to_string();

        let lock = self.lock_manager.get_lock_info(path).ok().flatten();

        Some(StatResult {
            path: path.to_string(),
            backend_name: entry.backend_name,
            physical_path: entry.physical_path,
            size: if is_dir && entry.size == 0 {
                4096
            } else {
                entry.size
            },
            etag: entry.etag,
            mime_type: mime,
            is_directory: is_dir,
            entry_type: entry.entry_type,
            mode: if is_dir { 0o755 } else { 0o644 },
            version: entry.version,
            zone_id: entry.zone_id,
            created_at_ms: entry.created_at_ms,
            modified_at_ms: entry.modified_at_ms,
            lock,
        })
    }

    // ── sys_unlink ────────────────────────────────────────────────────

    /// Rust syscall: full unlink (validate → route → metastore → backend → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation. Python only
    /// dispatches event notify + POST hooks.
    /// Returns `hit=false` for DT_DIR/DT_MOUNT/DT_PIPE/DT_STREAM → Python fallback.
    pub fn sys_unlink(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<SysUnlinkResult, KernelError> {
        let miss = |et: u8| {
            Ok(SysUnlinkResult {
                hit: false,
                entry_type: et,
                post_hook_needed: false,
                path: path.to_string(),
                etag: None,
                size: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 1b. Trie-resolved virtual paths (§11 Phase 21)
        if self.trie.lookup(path).is_some() {
            return miss(0);
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Delete(DeleteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
        }))?;

        // 2. Route (check write access)
        let route = match self
            .mount_table
            .route(path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(0),
        };

        // 3. Get metadata (dcache or metastore — per-mount first, then global)
        let zone_path = Self::zone_key(&route.backend_path);
        let meta = match self.dcache.get_entry(path) {
            Some(e) => Some(e),
            None => self
                .with_metastore(&route.mount_point, |ms| {
                    ms.get(&zone_path).ok().flatten().map(|m| CachedEntry {
                        backend_name: m.backend_name,
                        physical_path: m.physical_path,
                        size: m.size,
                        etag: m.etag,
                        version: m.version,
                        entry_type: m.entry_type,
                        zone_id: m.zone_id,
                        mime_type: m.mime_type,
                        created_at_ms: None,
                        modified_at_ms: None,
                    })
                })
                .flatten(),
        };

        let entry = match meta {
            Some(e) => e,
            None => return miss(0),
        };

        // 4. Entry-type dispatch
        match entry.entry_type {
            DT_PIPE => {
                // Destroy pipe buffer + metastore/dcache cleanup (Rust-native)
                let _ = self.destroy_pipe(path);
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: DT_PIPE,
                    post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
                    path: path.to_string(),
                    etag: entry.etag,
                    size: entry.size,
                });
            }
            DT_STREAM => {
                // Destroy stream buffer + metastore/dcache cleanup (Rust-native)
                let _ = self.destroy_stream(path);
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: DT_STREAM,
                    post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
                    path: path.to_string(),
                    etag: entry.etag,
                    size: entry.size,
                });
            }
            DT_DIR => return miss(entry.entry_type),
            // DT_MOUNT (2) and DT_EXTERNAL_STORAGE (5) → Python handles unmount
            2 | 5 => return miss(entry.entry_type),
            _ => {}
        }

        // 5. VFS write lock (DT_REG path)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms);
        if lock_handle == 0 {
            return miss(entry.entry_type);
        }

        // 6. Metastore delete (per-mount or global) — zone-relative key
        self.with_metastore(&route.mount_point, |ms| {
            let _ = ms.delete(&zone_path);
        });

        // 7. Backend delete (best-effort, PAS only)
        let _ = self
            .mount_table
            .delete_file(&route.mount_point, &route.backend_path);

        // 8. DCache evict
        self.dcache.evict(path);

        // 9. Release VFS lock
        self.lock_manager.do_release(lock_handle);

        // 10. OBSERVE-phase dispatch (§11 Phase 5): queue FileDelete.
        // Cloned out of `entry` because the SysUnlinkResult below also
        // moves them.
        let etag_for_event = entry.etag.clone();
        let size_for_event = entry.size;
        self.dispatch_mutation(FileEventType::FileDelete, path, ctx, |ev| {
            ev.size = Some(size_for_event);
            ev.etag = etag_for_event;
        });

        // 11. Return hit=true with metadata for event payload
        Ok(SysUnlinkResult {
            hit: true,
            entry_type: entry.entry_type,
            post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
            path: path.to_string(),
            etag: entry.etag,
            size: entry.size,
        })
    }

    // ── sys_rename ────────────────────────────────────────────────────

    /// Rust syscall: full rename (validate → route → VFS lock → metastore → backend → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Returns `hit=false` for DT_MOUNT/DT_PIPE/DT_STREAM → Python fallback.
    pub fn sys_rename(
        &self,
        old_path: &str,
        new_path: &str,
        ctx: &OperationContext,
    ) -> Result<SysRenameResult, KernelError> {
        let miss = || {
            Ok(SysRenameResult {
                hit: false,
                success: false,
                post_hook_needed: false,
                is_directory: false,
            })
        };

        // 1. Validate both
        validate_path_fast(old_path)?;
        validate_path_fast(new_path)?;

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Rename(RenameHookCtx {
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
            is_directory: false,
        }))?;

        // 2. Route both (check write access)
        let old_route = match self
            .mount_table
            .route(old_path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let new_route = match self
            .mount_table
            .route(new_path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. Sorted VFS lock acquire (deadlock-free: min(old,new) first)
        let (first, second) = if old_path <= new_path {
            (old_path, new_path)
        } else {
            (new_path, old_path)
        };

        let lock1 =
            self.lock_manager
                .blocking_acquire(first, LockMode::Write, self.vfs_lock_timeout_ms);
        let lock2 = if first != second {
            self.lock_manager
                .blocking_acquire(second, LockMode::Write, self.vfs_lock_timeout_ms)
        } else {
            0
        };

        let release_locks = |lm: &LockManager, h1: u64, h2: u64| {
            if h2 > 0 {
                lm.do_release(h2);
            }
            if h1 > 0 {
                lm.do_release(h1);
            }
        };

        // Lock timeout check
        if lock1 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // 4. Existence check: get old metadata (per-mount or global) — zone-relative keys
        let old_zone_path = Self::zone_key(&old_route.backend_path);
        let new_zone_path = Self::zone_key(&new_route.backend_path);
        let old_meta = self
            .with_metastore(&old_route.mount_point, |ms| {
                ms.get(&old_zone_path).ok().flatten()
            })
            .flatten();

        // Also check dcache
        let old_entry = self.dcache.get_entry(old_path);

        let (is_directory, entry_type) = match (&old_meta, &old_entry) {
            (Some(m), _) => (m.entry_type == DT_DIR, m.entry_type),
            (None, Some(e)) => (e.entry_type == DT_DIR, e.entry_type),
            (None, None) => {
                // Not found in Rust metastore/dcache — let Python handle under VFS lock
                release_locks(&self.lock_manager, lock1, lock2);
                return miss();
            }
        };

        // DT_PIPE/DT_STREAM: rename not supported (IPC endpoints are identity-bound)
        // DT_MOUNT (2) / DT_EXTERNAL_STORAGE (5): Python handles unmount logic
        match entry_type {
            DT_PIPE | DT_STREAM => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(KernelError::IOError(format!(
                    "rename not supported for entry type {} at {}",
                    entry_type, old_path
                )));
            }
            2 | 5 => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Ok(SysRenameResult {
                    hit: false,
                    success: false,
                    post_hook_needed: false,
                    is_directory,
                });
            }
            _ => {}
        }

        // 5. Destination conflict check — let Python handle under VFS lock
        //    (Python has richer stale-metadata cleanup logic with backend.file_exists)
        let new_exists = self
            .with_metastore(&old_route.mount_point, |ms| {
                ms.exists(&new_zone_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if new_exists {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // 6. Atomic rename (and, for directories, recursive child rewrite)
        //    via the `Metastore::rename_path` helper added in F3 C1. Redb
        //    overrides this with a single write txn, so the entire rename
        //    is crash-safe on the standalone-redb hot path; the default
        //    trait impl is a put-then-delete that matches the previous
        //    hand-rolled loop below.
        if old_meta.is_some() {
            self.with_metastore(&old_route.mount_point, |ms| {
                let _ = ms.rename_path(&old_zone_path, &new_zone_path);
            });
        }

        // 8. Backend rename (best-effort, PAS only)
        let _ = self.mount_table.rename_file(
            &old_route.mount_point,
            &old_route.backend_path,
            &new_route.backend_path,
        );

        // 9. DCache: evict old + put new; evict children prefix for directories
        if let Some(entry) = self.dcache.get_entry(old_path) {
            self.dcache.evict(old_path);
            self.dcache.put(new_path, entry);
        }
        if is_directory {
            let prefix = format!("{}/", old_path.trim_end_matches('/'));
            self.dcache.evict_prefix(&prefix);
        }

        // 10. Release sorted locks
        release_locks(&self.lock_manager, lock1, lock2);

        // 11. OBSERVE-phase dispatch (§11 Phase 5): queue FileRename.
        // Convention (mirrors Python FileEvent for renames): primary
        // `path` is the source, `new_path` is the destination.
        let new_path_owned = new_path.to_string();
        self.dispatch_mutation(FileEventType::FileRename, old_path, ctx, |ev| {
            ev.new_path = Some(new_path_owned);
        });

        Ok(SysRenameResult {
            hit: true,
            success: true,
            post_hook_needed: self.rename_hook_count.load(Ordering::Relaxed) > 0,
            is_directory,
        })
    }

    // ── sys_copy ───────────────────────────────────────────────────────

    /// Rust syscall: copy file (validate → route → VFS lock → backend copy → metastore → dcache).
    ///
    /// Three strategies:
    ///   1. Same mount, CAS backend → metadata-only copy (content deduplicated by hash).
    ///   2. Same mount, PAS backend → `backend.copy_file()`, fallback to read+write.
    ///   3. Cross mount → `read_content()` from src + `write_content()` to dst.
    ///
    /// Returns `hit=false` for directories, DT_PIPE/DT_STREAM, or when src not found.
    pub fn sys_copy(
        &self,
        src_path: &str,
        dst_path: &str,
        ctx: &OperationContext,
    ) -> Result<SysCopyResult, KernelError> {
        let miss = || {
            Ok(SysCopyResult {
                hit: false,
                post_hook_needed: false,
                dst_path: dst_path.to_string(),
                etag: None,
                size: 0,
                version: 0,
            })
        };

        // 1. Validate both paths
        validate_path_fast(src_path)?;
        validate_path_fast(dst_path)?;

        // 2. Route both (read access for src, write access for dst)
        let src_route = match self
            .mount_table
            .route(src_path, &ctx.zone_id, ctx.is_admin, false)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let dst_route = match self
            .mount_table
            .route(dst_path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. Get source metadata (dcache or metastore) — zone-relative keys
        let src_zone_path = Self::zone_key(&src_route.backend_path);
        let dst_zone_path = Self::zone_key(&dst_route.backend_path);
        let src_meta = match self.dcache.get_entry(src_path) {
            Some(e) => e,
            None => {
                match self
                    .with_metastore(&src_route.mount_point, |ms| {
                        ms.get(&src_zone_path).ok().flatten().map(|m| CachedEntry {
                            backend_name: m.backend_name,
                            physical_path: m.physical_path,
                            size: m.size,
                            etag: m.etag,
                            version: m.version,
                            entry_type: m.entry_type,
                            zone_id: m.zone_id,
                            mime_type: m.mime_type,
                            created_at_ms: None,
                            modified_at_ms: None,
                        })
                    })
                    .flatten()
                {
                    Some(e) => e,
                    None => return miss(),
                }
            }
        };

        // 4. Reject non-regular files
        match src_meta.entry_type {
            DT_REG => {}
            _ => return miss(),
        }

        // 5. Check destination doesn't already exist (zone-relative key)
        let dst_exists = self
            .with_metastore(&dst_route.mount_point, |ms| {
                ms.exists(&dst_zone_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if dst_exists {
            return Err(KernelError::IOError(format!(
                "sys_copy: destination already exists: {dst_path}"
            )));
        }

        // 6. VFS lock both paths (sorted, deadlock-free)
        let (first, second) = if src_path <= dst_path {
            (src_path, dst_path)
        } else {
            (dst_path, src_path)
        };
        let lock1 =
            self.lock_manager
                .blocking_acquire(first, LockMode::Write, self.vfs_lock_timeout_ms);
        let lock2 = if first != second {
            self.lock_manager
                .blocking_acquire(second, LockMode::Write, self.vfs_lock_timeout_ms)
        } else {
            0
        };

        let release_locks = |lm: &LockManager, h1: u64, h2: u64| {
            if h2 > 0 {
                lm.do_release(h2);
            }
            if h1 > 0 {
                lm.do_release(h1);
            }
        };

        if lock1 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // 7. Copy content (strategy depends on same-mount vs cross-mount)
        let same_mount = src_route.mount_point == dst_route.mount_point;

        let copy_result: Result<(String, u64), KernelError> = if same_mount {
            // Try server-side copy first (PAS backends)
            match self.mount_table.copy_file(
                &src_route.mount_point,
                &src_route.backend_path,
                &dst_route.backend_path,
            ) {
                Some(wr) => Ok((wr.content_id, wr.size)),
                None => {
                    // CAS backend or copy_file not supported — metadata-only copy
                    // (content deduplicated by hash, just create new metastore entry)
                    let etag = src_meta.etag.clone().unwrap_or_default();
                    if !etag.is_empty() {
                        // CAS: same content_id, just new path
                        Ok((etag, src_meta.size))
                    } else {
                        // Fallback: read + write
                        self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx)
                    }
                }
            }
        } else {
            // Cross-mount: read from src backend, write to dst backend
            self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx)
        };

        let (content_id, size) = match copy_result {
            Ok(r) => r,
            Err(e) => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(e);
            }
        };

        // 8. Build destination metadata and persist
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        let raw_backend_name = self
            .mount_table
            .get_canonical(&dst_route.mount_point)
            .map(|e| e.backend_name.clone())
            .unwrap_or_else(|| dst_route.io_profile.clone());
        let backend_display_name = self.origin_backend_name(&raw_backend_name);

        let new_version = 1u32;
        self.with_metastore(&dst_route.mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: dst_zone_path.clone(),
                backend_name: backend_display_name.clone(),
                physical_path: content_id.clone(),
                size,
                etag: Some(content_id.clone()),
                version: new_version,
                entry_type: DT_REG,
                zone_id: Some(ctx.zone_id.clone()),
                target_zone_id: None,
                mime_type: src_meta.mime_type.clone(),
                created_at_ms: Some(now_ms),
                modified_at_ms: Some(now_ms),
            };
            let _ = ms.put(&dst_zone_path, meta);
        });

        // 9. Update dcache
        self.dcache.put(
            dst_path,
            CachedEntry {
                backend_name: backend_display_name,
                physical_path: content_id.clone(),
                size,
                etag: Some(content_id.clone()),
                version: new_version,
                entry_type: DT_REG,
                zone_id: Some(ctx.zone_id.clone()),
                mime_type: src_meta.mime_type.clone(),
                created_at_ms: Some(now_ms),
                modified_at_ms: Some(now_ms),
            },
        );

        // 10. Release VFS locks
        release_locks(&self.lock_manager, lock1, lock2);

        Ok(SysCopyResult {
            hit: true,
            post_hook_needed: self.copy_hook_count.load(Ordering::Relaxed) > 0,
            dst_path: dst_path.to_string(),
            etag: Some(content_id),
            size,
            version: new_version,
        })
    }

    /// Internal: copy content via read_content + write_content (cross-mount or fallback).
    fn copy_via_read_write(
        &self,
        src_route: &crate::mount_table::RustRouteResult,
        dst_route: &crate::mount_table::RustRouteResult,
        src_meta: &CachedEntry,
        ctx: &OperationContext,
    ) -> Result<(String, u64), KernelError> {
        let content_id = src_meta
            .etag
            .as_deref()
            .filter(|s| !s.is_empty())
            .or_else(|| {
                let pp = src_meta.physical_path.as_str();
                if pp.is_empty() {
                    None
                } else {
                    Some(pp)
                }
            });
        let content_id = match content_id {
            Some(id) => id,
            None => {
                return Err(KernelError::IOError(
                    "sys_copy: source has no content_id".into(),
                ))
            }
        };

        let content = self
            .mount_table
            .read_content(
                &src_route.mount_point,
                content_id,
                &src_route.backend_path,
                ctx,
            )
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to read source content at {}",
                    src_route.backend_path
                ))
            })?;

        let wr = self
            .mount_table
            .write_content(
                &dst_route.mount_point,
                &content,
                &dst_route.backend_path,
                ctx,
            )
            .map_err(|e| KernelError::BackendError(format!("sys_copy: {e:?}")))?
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to write destination at {}",
                    dst_route.backend_path
                ))
            })?;

        Ok((wr.content_id, wr.size))
    }

    // ── sys_mkdir ──────────────────────────────────────────────────────

    /// Rust syscall: full mkdir (validate → route → backend → metastore → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Python only dispatches event notify + POST hooks when hit=true.
    /// `parents=true` creates parent directories. `exist_ok=true` ignores existing.
    pub fn sys_mkdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        parents: bool,
        exist_ok: bool,
    ) -> Result<SysMkdirResult, KernelError> {
        // 1. Validate
        validate_path_fast(path)?;

        // 2. Route (check write access)
        let route = self
            .mount_table
            .route(path, &ctx.zone_id, ctx.is_admin, true)?;

        // 3. Existence check via metastore (per-mount or global) — zone-relative key
        let zone_path = Self::zone_key(&route.backend_path);
        let exists = self
            .with_metastore(&route.mount_point, |ms| {
                ms.exists(&zone_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if exists {
            if !exist_ok && !parents {
                return Err(KernelError::IOError(format!(
                    "Directory already exists: {path}"
                )));
            }
            // Already exists — ensure parents and return
            if parents {
                self.ensure_parent_directories(path, &zone_path, ctx, &route.mount_point)?;
            }
            return Ok(SysMkdirResult {
                hit: true,
                post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
            });
        }

        // 4. Backend mkdir (best-effort, PAS backends create physical dirs)
        let _ = self
            .mount_table
            .mkdir(&route.mount_point, &route.backend_path, parents, true);

        // 5. Ensure parent directories
        if parents {
            self.ensure_parent_directories(path, &zone_path, ctx, &route.mount_point)?;
        }

        // 6. Create directory metadata in metastore (per-mount or global) — zone-relative key
        self.with_metastore(&route.mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: zone_path.clone(),
                backend_name: route.io_profile.clone(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: Some(ctx.zone_id.clone()),
                target_zone_id: None,
                mime_type: Some("inode/directory".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
            };
            let _ = ms.put(&zone_path, meta);
        });

        // 7. DCache put
        self.dcache.put(
            path,
            CachedEntry {
                backend_name: route.io_profile.clone(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: Some(ctx.zone_id.clone()),
                mime_type: Some("inode/directory".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
            },
        );

        // 8. OBSERVE-phase dispatch (§11 Phase 5): queue DirCreate.
        // Only fires on the newly-created path — the early return at
        // step 3 (already-exists branch) does NOT dispatch because no
        // state actually changed. Parent directories created via
        // ensure_parent_directories don't get individual events; the
        // top-level mkdir event is enough for observers like
        // FileWatcher to invalidate their dcache for the subtree.
        self.dispatch_mutation(FileEventType::DirCreate, path, ctx, |_ev| {});

        Ok(SysMkdirResult {
            hit: true,
            post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
        })
    }

    /// Walk up path creating missing parent directory metadata.
    ///
    /// `zone_path` is the zone-relative path for metastore operations;
    /// `path` is the global path for dcache operations. They share the
    /// same suffix structure, so parent traversal stays in sync.
    fn ensure_parent_directories(
        &self,
        path: &str,
        zone_path: &str,
        ctx: &OperationContext,
        mount_point: &str,
    ) -> Result<(), KernelError> {
        // Walk up zone_path from parent to root, collecting missing dirs.
        // Collect (zone_dir, global_dir) pairs.
        let mut zp_cur = zone_path;
        let mut gp_cur = path;
        let mut to_create: Vec<(String, String)> = Vec::new();
        loop {
            match zp_cur.rfind('/') {
                Some(0) | None => break,
                Some(zpos) => {
                    // Suffix removed by this iter = current zp_cur tail length.
                    // Must be computed against current zp_cur, not original
                    // zone_path.len() — iter N already trimmed earlier suffixes
                    // from gp_cur, so using zone_path.len() double-counts.
                    let suffix_len = zp_cur.len() - zpos;
                    zp_cur = &zone_path[..zpos];
                    if zp_cur.is_empty() || zp_cur == "/" {
                        break;
                    }
                    let gpos = gp_cur.len() - suffix_len;
                    gp_cur = &path[..gpos];
                    let exists = self
                        .with_metastore(mount_point, |ms| ms.exists(zp_cur).unwrap_or(true))
                        .unwrap_or(true);
                    if !exists {
                        to_create.push((zp_cur.to_string(), gp_cur.to_string()));
                    } else {
                        break; // Existing parent found, stop
                    }
                }
            }
        }

        // Create from shallowest to deepest
        for (zone_dir, global_dir) in to_create.into_iter().rev() {
            self.with_metastore(mount_point, |ms| {
                let meta = crate::metastore::FileMetadata {
                    path: zone_dir.clone(),
                    backend_name: String::new(),
                    physical_path: String::new(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_DIR,
                    zone_id: Some(ctx.zone_id.clone()),
                    target_zone_id: None,
                    mime_type: Some("inode/directory".to_string()),
                    created_at_ms: None,
                    modified_at_ms: None,
                };
                let _ = ms.put(&zone_dir, meta);
            });
            self.dcache.put(
                &global_dir,
                CachedEntry {
                    backend_name: String::new(),
                    physical_path: String::new(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_DIR,
                    zone_id: Some(ctx.zone_id.clone()),
                    mime_type: Some("inode/directory".to_string()),
                    created_at_ms: None,
                    modified_at_ms: None,
                },
            );
        }
        Ok(())
    }

    // ── sys_rmdir ──────────────────────────────────────────────────────

    /// Rust syscall: full rmdir (validate → route → children check → delete → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Returns `hit=false` for DT_MOUNT/DT_EXTERNAL_STORAGE → Python handles unmount.
    pub fn sys_rmdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysRmdirResult, KernelError> {
        let miss = || {
            Ok(SysRmdirResult {
                hit: false,
                post_hook_needed: false,
                children_deleted: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 2. Route (check write access)
        let route = self
            .mount_table
            .route(path, &ctx.zone_id, ctx.is_admin, true)?;

        // 3. Get metadata (per-mount or global) — zone-relative key
        let zone_path = Self::zone_key(&route.backend_path);
        let entry_type = self
            .with_metastore(&route.mount_point, |ms| {
                ms.get(&zone_path)
                    .ok()
                    .flatten()
                    .map(|m| m.entry_type)
                    .unwrap_or(DT_DIR)
            })
            .unwrap_or(DT_DIR);

        // DT_MOUNT(2) / DT_EXTERNAL_STORAGE(5) → Python handles unmount
        if entry_type == 2 || entry_type == 5 {
            return miss();
        }

        // 4. Check children (per-mount or global) — zone-relative prefix
        let mut children_deleted = 0;
        if let Some(result) = self.with_metastore(&route.mount_point, |ms| {
            let prefix = format!("{}/", zone_path.trim_end_matches('/'));
            let children = ms.list(&prefix).unwrap_or_default();

            if !children.is_empty() {
                if !recursive {
                    return Err(KernelError::IOError(format!("Directory not empty: {path}")));
                }

                // 5. Recursive: batch delete all children
                let child_paths: Vec<String> = children.iter().map(|c| c.path.clone()).collect();
                Ok(ms.delete_batch(&child_paths).unwrap_or(0))
            } else {
                Ok(0)
            }
        }) {
            children_deleted = result?;
        }

        // 6. Backend rmdir (best-effort)
        let _ = self
            .mount_table
            .rmdir(&route.mount_point, &route.backend_path, recursive);

        // 7. Delete directory metadata (per-mount or global) — zone-relative key
        self.with_metastore(&route.mount_point, |ms| {
            let _ = ms.delete(&zone_path);
        });

        // 8. DCache evict + prefix evict
        self.dcache.evict(path);
        let prefix = format!("{}/", path.trim_end_matches('/'));
        self.dcache.evict_prefix(&prefix);

        // 9. OBSERVE-phase dispatch (§11 Phase 5): queue DirDelete.
        // Like sys_mkdir, only the top-level rmdir event fires —
        // recursively-deleted children don't generate individual events
        // (observers needing per-child notifications can list the
        // directory before unlink themselves; the top-level event is
        // the cache-invalidation signal).
        self.dispatch_mutation(FileEventType::DirDelete, path, ctx, |_ev| {});

        Ok(SysRmdirResult {
            hit: true,
            post_hook_needed: self.rmdir_hook_count.load(Ordering::Relaxed) > 0,
            children_deleted,
        })
    }

    // ── Tier 2 convenience methods ────────────────────────────────────

    /// Fast access check: validate + route + dcache existence (~100ns).
    ///
    /// Returns true if file exists in dcache and path is routable.
    /// Does NOT check metastore (dcache authoritative for hot-path).
    pub fn access(&self, path: &str, zone_id: &str, is_admin: bool) -> bool {
        if validate_path_fast(path).is_err() {
            return false;
        }
        if self
            .mount_table
            .route(path, zone_id, is_admin, false)
            .is_err()
        {
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
            let route = self
                .mount_table
                .route(path, &ctx.zone_id, ctx.is_admin, true)
                .ok();
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
                        self.vfs_lock_timeout_ms,
                    );
                }
            }
        }

        // 4. Write each item — collect metadata for batch put
        // Tuple: (mount_point, path, FileMetadata) for per-mount metastore support
        let mut batch_meta: Vec<(String, String, crate::metastore::FileMetadata)> = Vec::new();

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
                .mount_table
                .write_content(&route.mount_point, content, &route.backend_path, ctx)
                .unwrap_or_default();

            match write_result {
                Some(wr) => {
                    let old_version = self.dcache.get_entry(path).map(|e| e.version).unwrap_or(0);
                    let new_version = old_version + 1;
                    let zone_path = Self::zone_key(&route.backend_path);

                    // Collect metadata for batch put (instead of N individual puts)
                    let batch_backend_name = self.origin_backend_name(&route.io_profile);
                    let meta = crate::metastore::FileMetadata {
                        path: zone_path.clone(),
                        backend_name: batch_backend_name,
                        physical_path: wr.content_id.clone(),
                        size: wr.size,
                        etag: Some(wr.content_id.clone()),
                        version: new_version,
                        entry_type: DT_REG,
                        zone_id: Some(ctx.zone_id.clone()),
                        target_zone_id: None,
                        mime_type: None,
                        created_at_ms: None,
                        modified_at_ms: None,
                    };
                    batch_meta.push((route.mount_point.clone(), zone_path, meta));

                    // DCache update
                    self.dcache.put(
                        path,
                        CachedEntry {
                            backend_name: route.io_profile.clone(),
                            physical_path: wr.content_id.clone(),
                            size: wr.size,
                            etag: Some(wr.content_id.clone()),
                            version: new_version,
                            entry_type: DT_REG,
                            zone_id: Some(ctx.zone_id.clone()),
                            mime_type: None,
                            created_at_ms: None,
                            modified_at_ms: None,
                        },
                    );

                    results.push(SysWriteResult {
                        hit: true,
                        content_id: Some(wr.content_id),
                        post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0
                            || self.write_batch_hook_count.load(Ordering::Relaxed) > 0,
                        version: new_version,
                        size: wr.size,
                    });
                }
                None => {
                    results.push(SysWriteResult {
                        hit: false,
                        content_id: None,
                        post_hook_needed: false,
                        version: 0,
                        size: 0,
                    });
                }
            }
        }

        // 4b. Metastore put_batch — per-mount metastore aware.
        // Global items (no per-mount metastore) use batch put for efficiency.
        if !batch_meta.is_empty() {
            let mut global_items: Vec<(String, crate::metastore::FileMetadata)> = Vec::new();
            for (mp, path, meta) in batch_meta {
                if self
                    .mount_table
                    .get_canonical(&mp)
                    .map(|e| e.metastore.is_some())
                    .unwrap_or(false)
                {
                    self.with_metastore(&mp, |ms| {
                        let _ = ms.put(&path, meta);
                    });
                } else {
                    global_items.push((path, meta));
                }
            }
            if !global_items.is_empty() {
                if let Some(ref ms) = self.metastore {
                    let _ = ms.put_batch(&global_items);
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
                    is_external: false,
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
            match self.sys_unlink(path, ctx) {
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

    /// List immediate children of a directory path from dcache.
    ///
    /// Returns Vec of (child_name, entry_type) tuples.
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
        let route = match self.mount_table.route(normalized, zone_id, is_admin, false) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };

        let global_prefix = if normalized == "/" {
            "/".to_string()
        } else {
            format!("{}/", normalized)
        };

        // Zone-relative prefix for metastore list (R7: zone-relative keys).
        let zone_parent = Self::zone_key(&route.backend_path);
        let zone_prefix = if zone_parent == "/" {
            "/".to_string()
        } else {
            format!("{}/", zone_parent)
        };

        // Merge dcache children (zone-relative basenames; we re-prefix with
        // the global parent path below) with per-mount metastore list
        // (zone-relative full paths converted to global) so federation zones
        // see entries that haven't been warmed into the dcache (F2 C5).
        //
        // ``dcache.list_children(prefix)`` strips the common prefix and
        // returns the remaining *basename* for each immediate child (e.g.
        // for prefix ``/mnt/`` it returns ``gcs_demo``). To satisfy the
        // ``sys_readdir`` contract which returns global paths, splice the
        // parent back on here.
        let mut seen: std::collections::BTreeMap<String, u8> = std::collections::BTreeMap::new();
        let parent_for_join = if parent_path == "/" {
            ""
        } else {
            parent_path.trim_end_matches('/')
        };
        for (child, etype) in self.dcache.list_children(&global_prefix) {
            let global = format!("{}/{}", parent_for_join, child);
            seen.insert(global, etype);
        }

        if let Some(ms_children) =
            self.with_metastore(&route.mount_point, |ms| ms.list(&zone_prefix).ok())
        {
            let parent_depth = zone_prefix.matches('/').count();
            for meta in ms_children.into_iter().flatten() {
                // Direct children only: same depth as prefix + 1 segment.
                if meta.path.matches('/').count() != parent_depth {
                    continue;
                }
                if !meta.path.starts_with(&zone_prefix) {
                    continue;
                }
                // Convert zone-relative path back to global for the seen map.
                let global_path =
                    crate::mount_table::zone_to_global(&route.mount_point, &meta.path);
                seen.entry(global_path).or_insert(meta.entry_type);
            }
        }

        seen.into_iter().collect()
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
        let entry = self.mount_table.get_canonical(&canonical).ok_or_else(|| {
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
                    "{}: mount '{}' backend is not CAS",
                    op, entry.backend_name
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
            path, entry_type, "",    // backend_name
            None,  // backend
            None,  // metastore
            None,  // raft_backend
            false, // readonly
            false, // admin_only
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
            crate::metastore::FileMetadata {
                path: "/update-test.txt".to_string(),
                backend_name: "test".to_string(),
                physical_path: "".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 0,
                zone_id: None,
                target_zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
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
                false,
                false,
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

    // ── R7: zone-relative metastore key tests ───────────────────────

    use crate::metastore::Metastore as MetastoreTrait;

    /// Create a temporary RedbMetastore for testing.
    fn temp_metastore() -> Arc<crate::metastore::RedbMetastore> {
        let dir = std::env::temp_dir().join(format!("nexus-test-ms-{}", uuid::Uuid::new_v4()));
        let path = dir.join("meta.redb");
        Arc::new(crate::metastore::RedbMetastore::open(&path).unwrap())
    }

    #[test]
    fn sys_setattr_dir_stores_zone_relative_key() {
        // Mount "/data" in zone "root" with a shared metastore.
        // DT_DIR at "/data/sub" should store metastore key "/sub" (zone-relative),
        // not "/data/sub" (global).
        let k = Kernel::new();
        let ms = temp_metastore();
        k.add_mount(
            "/data",
            "root",
            false,
            false,
            "balanced",
            "test",
            None,
            Some(ms.clone()),
            None,
            false,
        )
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
                false,
                false,
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

        // Verify metastore stores zone-relative key "/sub", not global "/data/sub"
        assert!(
            ms.get("/sub").unwrap().is_some(),
            "zone-relative key /sub must exist"
        );
        assert!(
            ms.get("/data/sub").unwrap().is_none(),
            "global key /data/sub must NOT exist"
        );
    }

    #[test]
    fn crosslink_metastore_shares_zone_relative_keys() {
        // Crosslink scenario: two mounts share the same per-zone metastore.
        // "/corp" and "/family/work" both point to the corp zone store.
        // Write via /corp/file → read via /family/work/file must succeed.
        let k = Kernel::new();
        let corp_ms = temp_metastore();

        // Mount "/corp" with the corp metastore
        k.add_mount(
            "/corp",
            "root",
            false,
            false,
            "balanced",
            "corp-backend",
            None,
            Some(corp_ms.clone()),
            None,
            false,
        )
        .unwrap();
        // Mount "/family/work" with the SAME corp metastore (crosslink)
        k.add_mount(
            "/family/work",
            "root",
            false,
            false,
            "balanced",
            "corp-backend",
            None,
            Some(corp_ms.clone()),
            None,
            false,
        )
        .unwrap();

        // Need /family mount first for routing to work
        k.add_mount(
            "/family",
            "root",
            false,
            false,
            "balanced",
            "family-backend",
            None,
            None,
            None,
            false,
        )
        .unwrap();

        // Create DT_DIR at /corp/docs
        let r = k
            .sys_setattr(
                "/corp/docs",
                1,
                "",
                None,
                None,
                None,
                false,
                false,
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
        assert!(r.created, "DT_DIR at /corp/docs should be created");

        // Metastore should store zone-relative key "/docs"
        assert!(corp_ms.get("/docs").unwrap().is_some());

        // sys_stat via the crosslink path "/family/work/docs" should find it
        // because both mounts share the same metastore and zone-relative key is "/docs".
        let stat = k.sys_stat("/family/work/docs", "root", true);
        assert!(
            stat.is_some(),
            "crosslink stat /family/work/docs must find /docs in shared metastore"
        );
        let stat = stat.unwrap();
        assert!(stat.is_directory);
        // StatResult.path should be the global path (user-facing)
        assert_eq!(stat.path, "/family/work/docs");
    }

    #[test]
    fn metastore_proxy_returns_global_paths() {
        // metastore_get/list should return global paths even though storage is zone-relative.
        let k = Kernel::new();
        let ms = temp_metastore();
        k.add_mount(
            "/data",
            "root",
            false,
            false,
            "balanced",
            "test",
            None,
            Some(ms.clone()),
            None,
            false,
        )
        .unwrap();

        // Create a DT_DIR at /data/reports
        k.sys_setattr(
            "/data/reports",
            1,
            "",
            None,
            None,
            None,
            false,
            false,
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
}
