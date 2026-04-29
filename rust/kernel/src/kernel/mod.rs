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

#[cfg(test)]
use crate::dcache::DT_REG;
use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_LINK, DT_MOUNT, DT_PIPE, DT_STREAM};
use crate::dispatch::{MutationObserver, Trie};
use crate::file_watch::FileWatchRegistry;
use crate::lock_manager::LockManager;
use crate::meta_store::LocalMetaStore;
use crate::vfs_router::{
    canonicalize_mount_path as canonicalize, RouteError, RustRouteResult, VFSRouter,
};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex, RwLock, RwLockReadGuard};
use std::sync::atomic::{AtomicU64, Ordering};
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
mod mount;
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
//
// Struct + impl live in the `contracts` crate so out-of-kernel
// services (`rust/services/src/{acp,managed_agent,…}/`) can build
// system-tier contexts without pulling kernel as a dep just for this
// type. Re-exported here under the historical `kernel::kernel::
// OperationContext` path so every existing call site keeps compiling.
pub use contracts::OperationContext;

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
    /// Content hash (content_id) for post-hook context.
    pub content_id: Option<String>,
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
    pub old_content_id: Option<String>,
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
    pub content_id: Option<String>,
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
    pub old_content_id: Option<String>,
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
    /// Content hash (content_id) of the destination file.
    pub content_id: Option<String>,
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
    pub content_id: Option<String>,
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
    /// DT_LINK target — `Some` only when `entry_type == DT_LINK`.
    /// `sys_stat` uses lstat semantics (returns the link's own
    /// metadata, not the target's), so callers that want to follow
    /// the link compose with the kernel's transparent-follow paths
    /// or call sys_stat on `link_target` directly.
    pub link_target: Option<String>,
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
    /// Suffixes declared by registered mutating hooks (via
    /// `NativeInterceptHook::mutating_path_suffix`). Populated on
    /// register; consulted by `has_mutating_match` so the kernel can
    /// decide whether to clone write content into `WriteHookCtx`. An
    /// empty Vec is the steady state today (no mutating hooks
    /// registered) — the call site short-circuits before any path
    /// comparison.
    mutating_suffixes: Vec<&'static str>,
}

#[allow(dead_code)]
impl NativeHookRegistry {
    pub(crate) fn new() -> Self {
        Self {
            hooks: Vec::new(),
            mutating_suffixes: Vec::new(),
        }
    }

    pub(crate) fn register(&mut self, hook: Box<dyn NativeInterceptHook>) {
        if let Some(suffix) = hook.mutating_path_suffix() {
            self.mutating_suffixes.push(suffix);
        }
        self.hooks.push(NativeHookEntry { hook });
    }

    /// Dispatch pre-hooks. Returns Err on first abort. The
    /// `HookOutcome::Replace` variant is propagated to the caller via
    /// the returned bytes; today only `sys_write` honours it, other
    /// syscalls drop the replacement.
    pub(crate) fn dispatch_pre(&self, ctx: &HookContext) -> Result<Option<Vec<u8>>, String> {
        let mut replacement: Option<Vec<u8>> = None;
        for entry in &self.hooks {
            match entry.hook.on_pre(ctx)? {
                crate::dispatch::HookOutcome::Pass => {}
                crate::dispatch::HookOutcome::Replace(bytes) => replacement = Some(bytes),
            }
        }
        Ok(replacement)
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

    /// Returns true when at least one registered hook declared a
    /// mutating path suffix that matches `path`. Cheap (linear scan
    /// over a Vec that today has at most a handful of entries); the
    /// steady state (no mutating hooks) returns false on the
    /// empty-Vec check before any string comparison.
    pub(crate) fn has_mutating_match(&self, path: &str) -> bool {
        self.mutating_suffixes
            .iter()
            .any(|suffix| path.ends_with(suffix))
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
    pub agent_table: Arc<crate::core::agents::table::AgentTable>,
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
    // Control-Plane HAL §3.B.1 slot. `Arc<dyn DistributedCoordinator>` so
    // the kernel's distributed-namespace surface (zone listing, distributed-
    // lock / WAL-stream / Raft-MetaStore construction, mount wiring,
    // share registry, cluster introspection) is reachable through a trait
    // boundary rather than direct `nexus_raft::*` types. Default at boot
    // is `NoopDistributedCoordinator`; nexus-cdylib boot installs the real
    // raft-side impl via `Kernel::set_distributed_coordinator`. Mirrors
    // the Phase-4 PeerBlobClient DI pattern.
    pub(crate) distributed_coordinator:
        parking_lot::RwLock<Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator>>,
    // Scatter-gather fetcher: drives bounded fan-out against
    // `backend_name.origins` whenever a local chunk miss occurs.
    // Installed on every `CASEngine` via `VFSRouter` on mount
    // registration.
    //
    // Type is `Arc<dyn RemoteChunkFetcher>` so `ObjectStoreProvider`
    // impls in the backends crate `Arc::clone(&self.inner.chunk_fetcher)`
    // and pass it through to `CasLocalBackend::new_with_fetcher`
    // without an explicit cast.
    #[allow(dead_code)]
    pub(crate) chunk_fetcher: Arc<dyn crate::cas_remote::RemoteChunkFetcher>,
    /// Pending remote metastore — set by ``sys_setattr(backend_type="remote")``
    /// and consumed immediately after mount registration to install the
    /// ``RemoteMetaStore`` on the mount entry. This avoids threading the
    /// metastore through ``sys_setattr``'s return value.
    pub(crate) pending_remote_meta_store:
        parking_lot::Mutex<Option<Arc<dyn crate::meta_store::MetaStore>>>,

    /// Phase 4 (full): blob-fetcher slot stashed by federation init for
    /// the cdylib's transport-tier install hook to drain.
    /// Phase 5: typed as `Box<dyn Any + Send + Sync>` so kernel does not
    /// name the raft-side `BlobFetcherSlot` type — `transport::blob::
    /// fetcher::install` downcasts to the concrete type at drain time.
    pub(crate) pending_blob_fetcher_slot:
        parking_lot::Mutex<Option<Box<dyn std::any::Any + Send + Sync>>>,
    // Distributed state (zone_manager / zone_registry / zone_runtime /
    // cross_zone_mounts / mount_reconciliation_done) lives on
    // `RaftDistributedCoordinator` in the raft crate. Kernel reaches it
    // through the `kernel::hal::distributed_coordinator::DistributedCoordinator`
    // trait.
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
    #[allow(clippy::let_and_return)]
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
            distributed_coordinator: parking_lot::RwLock::new(
                crate::hal::distributed_coordinator::NoopDistributedCoordinator::arc(),
            ),
            chunk_fetcher,
            pending_remote_meta_store: parking_lot::Mutex::new(None),
            pending_blob_fetcher_slot: parking_lot::Mutex::new(None),
        };
        // Distributed-coordinator bootstrap is driven by
        // `nexus_raft::distributed_coordinator::install`. The cdylib boot
        // path constructs `Kernel`, then calls `install(kernel)` which
        // wires the `RaftDistributedCoordinator` and dispatches
        // `init_from_env` through the trait. Kernel construction stays
        // raft-free at this seam so non-cdylib callers (Rust tests,
        // embedded) skip federation init unless they explicitly install
        // the coordinator.
        // ManagedAgentService is installed by the cdylib boot path
        // (services lives in a peer crate; kernel does NOT depend on
        // services). Python-side: `nexus_runtime.nx_managed_agent_install
        // (kernel)` runs in `_wired.py` after `Kernel::new` returns.
        // Pure-Rust embedders call `services::managed_agent::ManagedAgentService::install(&k)`
        // themselves; nothing happens automatically here.
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

    /// Get the federation self-address (peer-reachable `host:port`)
    /// previously set by `set_self_address`.  `None` until federation
    /// init populates it.
    pub fn self_address_string(&self) -> Option<String> {
        self.self_address.read().clone()
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
        content_id: Option<String>,
        version: u32,
        mime_type: Option<String>,
        created_at_ms: Option<i64>,
        modified_at_ms: Option<i64>,
    ) -> crate::meta_store::FileMetadata {
        crate::meta_store::FileMetadata {
            path: path.to_string(),
            size,
            content_id,
            version,
            entry_type,
            zone_id: Some(zone_id.to_string()),
            mime_type,
            created_at_ms,
            modified_at_ms,
            last_writer_address: self.self_address.read().clone(),
            // build_metadata is called for non-DT_MOUNT writes (sys_write,
            // mkdir, etc.); DT_MOUNT entries are constructed in dlc.rs
            // with the target zone explicitly set.
            target_zone_id: None,
            // DT_LINK target: sys_setattr's DT_LINK branch passes the
            // target through a different construction path; non-link
            // metadata never carries a value here.
            link_target: None,
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
        content_id: Option<&str>,
        zone_id: Option<&str>,
        mime_type: Option<&str>,
        last_writer_address: Option<&str>,
    ) {
        self.dcache.put(
            path,
            CachedEntry {
                size,
                content_id: content_id.map(|s| s.to_string()),
                version,
                entry_type,
                zone_id: zone_id.map(|s| s.to_string()),
                mime_type: mime_type.map(|s| s.to_string()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: last_writer_address.map(|s| s.to_string()),
                link_target: None,
            },
        );
    }

    /// Put a pre-built CachedEntry into the dcache. Used by DLC.mount().
    pub(crate) fn dcache_put_entry(&self, path: &str, entry: CachedEntry) {
        self.dcache.put(path, entry);
    }

    /// Resolve `path` through DT_LINK indirection for content-touching
    /// syscalls (`sys_read`, `sys_write`, etc.). Non-link paths and
    /// missing entries borrow the input — zero allocation on the hot
    /// path. Link entries follow one hop with cycle detection (see
    /// `DCache::resolve_link`); chain / self-loop / missing-target
    /// failures surface as `KernelError::PermissionDenied` so the
    /// existing kernel-error handling chain doesn't need a new variant.
    ///
    /// `sys_stat` deliberately bypasses this helper — `lstat` semantics
    /// require the raw DT_LINK metadata, not the resolved target.
    pub(crate) fn resolve_path_through_link<'a>(
        &self,
        path: &'a str,
    ) -> Result<std::borrow::Cow<'a, str>, KernelError> {
        match self.dcache.resolve_link(path) {
            Ok(resolved) => {
                if resolved == path {
                    Ok(std::borrow::Cow::Borrowed(path))
                } else {
                    Ok(std::borrow::Cow::Owned(resolved))
                }
            }
            Err(crate::dcache::LinkResolveError::Chained) => Err(KernelError::PermissionDenied(
                format!("DT_LINK chain rejected (ELOOP) at {path}"),
            )),
            Err(crate::dcache::LinkResolveError::SelfLoop) => Err(KernelError::PermissionDenied(
                format!("DT_LINK self-loop at {path}"),
            )),
            Err(crate::dcache::LinkResolveError::MissingTarget) => Err(
                KernelError::PermissionDenied(format!("DT_LINK at {path} has no link_target")),
            ),
        }
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
    // Mount-table primitives live in the `kernel::mount` submodule.
    // Federation-mount apply wiring lives on
    // `nexus_raft::distributed_coordinator::RaftDistributedCoordinator`.

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
        raft_backend: Option<Box<dyn std::any::Any + Send + Sync>>,
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
        // -- DT_LINK params (entry_type == 6) --
        link_target: Option<&str>,
    ) -> Result<SysSetAttrResult, KernelError> {
        match entry_type {
            2 => {
                // DT_MOUNT — full mount lifecycle via DLC.
                //
                // Phase H zone-create-on-mount: when the caller did not
                // supply a `metastore` AND federation is active, ask the
                // DistributedCoordinator to materialise (auto-create) the
                // target zone's raft group and hand back an
                // `Arc<dyn MetaStore>` backed by the per-zone state
                // machine.  Service-tier callers therefore reach
                // federation through the standard `sys_setattr DT_MOUNT`
                // syscall — no separate `kernel.zone_create` surface.
                //
                // R20.6 option B preserved: install the apply-side
                // dcache coherence callback after routing is wired
                // (handled by the provider's `wire_mount` follow-up
                // below).  Install is keyed on the state machine's
                // ``coherence_id``, not on the per-mount MetaStore Arc,
                // so crosslinks of the same zone share one callback.
                let coordinator = self.distributed_coordinator();
                // Federation activity derives from `list_zones` — empty
                // before `install()` populates the ZoneManager, populated
                // after with at least the root zone.
                let federation_active = !coordinator.list_zones(self).is_empty();
                let metastore = match metastore {
                    Some(m) => Some(m),
                    None if federation_active && !zone_id.is_empty() => {
                        // Auto-create + resolve.
                        let _ = coordinator.create_zone(self, zone_id);
                        coordinator.metastore_for_zone(self, zone_id).ok()
                    }
                    None => None,
                };
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
                // Federation wire-mount: register apply-cb + replicate
                // the DT_MOUNT entry so peers see the mount via raft
                // commit.  No-op when federation is inactive.
                if federation_active && !zone_id.is_empty() {
                    let _ = coordinator.wire_mount(self, "root", path, zone_id);
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
                self.setattr_pipe(path, capacity, io_profile, read_fd, write_fd, zone_id)
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
            6 => {
                // DT_LINK — VFS-internal symlink (KERNEL-ARCHITECTURE.md §4.5).
                let target = link_target.ok_or_else(|| {
                    KernelError::PermissionDenied(
                        "sys_setattr(DT_LINK): link_target is required".to_string(),
                    )
                })?;
                self.setattr_create_link(path, zone_id, target)
            }
            _ => Err(KernelError::PermissionDenied(format!(
                "sys_setattr: unsupported entry_type={entry_type}"
            ))),
        }
    }

    /// DT_LINK: create a VFS-internal symlink whose `link_target`
    /// resolves at `route()` time (one hop, with cycle detection — see
    /// `DCache::resolve_link`). Self-loops (`link_target == path`) are
    /// rejected here so the resolver never has to handle them at lookup
    /// time. Idempotent for an existing DT_LINK at the same path with
    /// the same target.
    fn setattr_create_link(
        &self,
        path: &str,
        zone_id: &str,
        link_target: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Reject self-loops at write time; resolver assumes none ever land.
        if link_target == path {
            return Err(KernelError::PermissionDenied(format!(
                "sys_setattr(DT_LINK): self-loop rejected ({path:?})"
            )));
        }
        // Reject relative targets — DT_LINK semantics require absolute
        // paths so the resolver can route() without a contextual base.
        if !link_target.starts_with('/') {
            return Err(KernelError::PermissionDenied(format!(
                "sys_setattr(DT_LINK): link_target must be absolute, got {link_target:?}"
            )));
        }
        // Idempotent open: existing DT_LINK with the same target is OK.
        if let Some(existing) = self.metastore_get(path).ok().flatten() {
            if existing.entry_type == DT_LINK
                && existing.link_target.as_deref() == Some(link_target)
            {
                return Ok(SysSetAttrResult {
                    path: path.to_string(),
                    created: false,
                    entry_type: DT_LINK as i32,
                    backend_name: None,
                    capacity: None,
                    updated: Vec::new(),
                    shm_path: None,
                    data_rd_fd: None,
                    space_rd_fd: None,
                });
            }
            // Existing DT_LINK with a different target — reject so writes
            // don't silently re-target. Caller must sys_unlink first.
            if existing.entry_type == DT_LINK {
                return Err(KernelError::PermissionDenied(format!(
                    "sys_setattr(DT_LINK): {path:?} already a DT_LINK with different target"
                )));
            }
        }
        let meta = crate::meta_store::FileMetadata {
            path: path.to_string(),
            size: 0,
            content_id: None,
            version: 1,
            entry_type: DT_LINK,
            zone_id: Some(zone_id.to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: self.self_address.read().clone(),
            target_zone_id: None,
            link_target: Some(link_target.to_string()),
        };
        self.metastore_put(path, meta)?;
        let entry = CachedEntry {
            size: 0,
            content_id: None,
            version: 1,
            entry_type: DT_LINK,
            zone_id: Some(zone_id.to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            link_target: Some(link_target.to_string()),
        };
        self.dcache_put_entry(path, entry);
        Ok(SysSetAttrResult {
            path: path.to_string(),
            created: true,
            entry_type: DT_LINK as i32,
            backend_name: None,
            capacity: None,
            updated: Vec::new(),
            shm_path: None,
            data_rd_fd: None,
            space_rd_fd: None,
        })
    }

    /// DT_PIPE: create pipe buffer, or idempotent-open if it already exists.
    ///
    /// `io_profile`:
    /// - `"memory"` (default) → MemoryPipeBackend
    /// - `"shared_memory"` → SharedMemoryPipeBackend (mmap, cross-process)
    /// - `"stdio"` → StdioPipeBackend (subprocess fd, newline-framed)
    /// - `"wal"` → WalPipeCore (raft-replicated, cross-node, single-consumer)
    #[allow(unused_variables)]
    pub fn setattr_pipe(
        &self,
        path: &str,
        capacity: usize,
        io_profile: &str,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        zone_id: &str,
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
            // Raft-replicated DT_PIPE — composes whatever distributed
            // `MetaStore` impl the coordinator has DI'd
            // (`DistributedCoordinator::metastore_for_zone`). Single-
            // consumer semantics (each replica owns its head cursor);
            // see `core/pipe/wal.rs` for the contract.  Resolves the
            // metastore from the path's mount entry so per-zone WAL
            // pipes pick up their own zone's raft group.
            let provider = self.distributed_coordinator();
            let resolve_zone = if zone_id.is_empty() { "root" } else { zone_id };
            let store = provider
                .metastore_for_zone(self, resolve_zone)
                .map_err(|e| {
                    KernelError::IOError(format!(
                        "io_profile=wal requires federation (set NEXUS_PEERS): {e}"
                    ))
                })?;
            let backend = crate::core::pipe::wal::WalPipeCore::new(store, path.to_string());
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
            // Raft-replicated durable DT_STREAM.  WalStreamCore is a
            // kernel primitive (`core/stream/wal.rs`); it composes
            // whatever distributed `MetaStore` impl the coordinator has
            // DI'd via `metastore_for_zone`. The coordinator installs
            // the storage capability and the kernel constructs the
            // backend itself — layering preserved without a
            // per-primitive DI method on the trait.
            let provider = self.distributed_coordinator();
            let store = provider.metastore_for_zone(self, "root").map_err(|e| {
                KernelError::IOError(format!(
                    "io_profile=wal requires federation (set NEXUS_PEERS): {e}"
                ))
            })?;
            let backend = crate::core::stream::wal::WalStreamCore::new(store, path.to_string());
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

    /// Replace the kernel's coordinator slot with a concrete
    /// `DistributedCoordinator` impl. Kernel boots with
    /// `NoopDistributedCoordinator`; the cdylib boot path calls this
    /// with the real `nexus_raft::distributed_coordinator` impl once
    /// per kernel. Mirrors `set_peer_client`.
    pub fn set_distributed_coordinator(
        &self,
        coordinator: Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator>,
    ) {
        *self.distributed_coordinator.write() = coordinator;
    }

    /// Borrow the current distributed coordinator — read-locked snapshot.
    /// Internal callers use this to issue federation calls without
    /// holding the lock across `.await`. After `set_distributed_coordinator`
    /// runs (cdylib boot), this returns the real raft-backed impl;
    /// before then, a `NoopDistributedCoordinator` that errors on every
    /// call.
    pub fn distributed_coordinator(
        &self,
    ) -> Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator> {
        Arc::clone(&self.distributed_coordinator.read())
    }

    /// Federation procfs: synthesise a `StatResult` for paths under the
    /// `/__sys__/zones/` virtual namespace.  Read-only — like Linux
    /// `/proc`, you cannot create / remove a zone by writing to this
    /// path.  Returns `Some` for `/__sys__/zones/` (directory marker)
    /// and `/__sys__/zones/<id>` (per-zone synthesised entry); `None`
    /// otherwise so the caller falls through to normal routing.
    pub(crate) fn zones_procfs_stat(&self, path: &str) -> Option<StatResult> {
        let suffix = path.strip_prefix("/__sys__/zones")?;
        let provider = self.distributed_coordinator();
        // Directory marker.
        if suffix.is_empty() || suffix == "/" {
            return Some(StatResult {
                path: path.to_string(),
                size: 4096,
                content_id: None,
                mime_type: "inode/directory".to_string(),
                is_directory: true,
                entry_type: crate::dcache::DT_DIR,
                mode: 0o555, // r-x — read-only namespace
                version: 0,
                zone_id: Some("root".to_string()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                lock: None,
                link_target: None,
            });
        }
        // /__sys__/zones/<id>: synthesise from federation list.
        let zone_id = suffix.trim_start_matches('/');
        if zone_id.is_empty() || zone_id.contains('/') {
            return None;
        }
        if !provider.list_zones(self).iter().any(|z| z == zone_id) {
            return None;
        }
        Some(StatResult {
            path: path.to_string(),
            size: 0,
            content_id: None,
            mime_type: "application/x-nexus-zone".to_string(),
            is_directory: false,
            entry_type: crate::dcache::DT_REG,
            mode: 0o444,
            version: 0,
            zone_id: Some(zone_id.to_string()),
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: self.self_address_string(),
            lock: None,
            link_target: None,
        })
    }

    /// Federation procfs: list zones for `/__sys__/zones/` directory
    /// reads.  Returns `None` for paths outside the namespace so the
    /// caller falls through to normal routing.
    pub(crate) fn zones_procfs_readdir(&self, path: &str) -> Option<Vec<String>> {
        let suffix = path.strip_prefix("/__sys__/zones")?;
        if !suffix.is_empty() && suffix != "/" {
            return None;
        }
        Some(self.distributed_coordinator().list_zones(self))
    }

    /// Stash the raft-tier blob-fetcher slot. Drained by
    /// `nexus_raft::blob_fetcher_handler::install` at cdylib boot.
    /// Typed as `Box<dyn Any>` so kernel does not name the raft-side
    /// `BlobFetcherSlot` concrete type.
    pub fn stash_blob_fetcher_slot(&self, slot: Box<dyn std::any::Any + Send + Sync>) {
        *self.pending_blob_fetcher_slot.lock() = Some(slot);
    }

    /// Drain the previously stashed blob-fetcher slot.  Returns
    /// `None` after the first drain so re-imports of the cdylib
    /// stay safe.
    pub fn take_pending_blob_fetcher_slot(&self) -> Option<Box<dyn std::any::Any + Send + Sync>> {
        self.pending_blob_fetcher_slot.lock().take()
    }

    /// Borrow the kernel's `peer_client` slot for federation reads.
    pub fn peer_client_slot(&self) -> Arc<dyn crate::hal::peer::PeerBlobClient> {
        self.peer_client_arc()
    }

    /// Clone the VFSRouter `Arc` — used by federation / transport
    /// install hooks to wire callbacks against the kernel's routing
    /// table without holding the lock across `.await`.
    pub fn vfs_router_arc(&self) -> Arc<VFSRouter> {
        Arc::clone(&self.vfs_router)
    }

    /// Clone the DCache `Arc` — used by federation / transport install
    /// hooks to wire invalidation callbacks against the kernel's
    /// dcache without holding the lock across `.await`.
    pub fn dcache_arc(&self) -> Arc<DCache> {
        Arc::clone(&self.dcache)
    }

    /// Clone the LockManager `Arc` — used by federation install hooks
    /// to swap the lock backend on first federated mount (distributed
    /// locks bound to the root zone's consensus).
    pub fn lock_manager_arc(&self) -> Arc<LockManager> {
        Arc::clone(&self.lock_manager)
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
    ) -> Result<Arc<crate::core::stream::wal::WalStreamCore>, KernelError> {
        // WAL streams are kernel primitives composing whatever
        // distributed `MetaStore` the coordinator has DI'd via
        // `DistributedCoordinator::metastore_for_zone`. The coordinator
        // installs the storage capability; the kernel constructs the
        // backend itself, with no per-primitive DI methods.
        let store = self
            .distributed_coordinator()
            .metastore_for_zone(self, zone_id)
            .map_err(KernelError::IOError)?;
        let core = Arc::new(crate::core::stream::wal::WalStreamCore::new(
            store,
            stream_path.to_string(),
        ));
        // Register with StreamManager — ignore Exists (idempotent re-call).
        let _ = self.stream_manager.register(
            stream_path,
            Arc::clone(&core) as Arc<dyn crate::stream::StreamBackend>,
        );
        // Seed DCache + metastore inode so sys_read can locate the stream.
        let _ = self.write_stream_inode(stream_path, 0);
        Ok(core)
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
        // Federation procfs: /__sys__/zones/ enumerates loaded zones
        // (read-only namespace, like Linux /proc).  Returns the zone-id
        // list verbatim — caller may format with trailing-slash etc.
        if let Some(zones) = self.zones_procfs_readdir(path) {
            return zones;
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
}

// ─────────────────────────────────────────────────────────────────────
// R20.16.3 free-function helpers — take only ``Arc``-shared kernel state
// so the apply-side ``mount_apply_cb`` closure can call them without a
// back-reference to ``Kernel`` itself.
// ─────────────────────────────────────────────────────────────────────

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
            ev.content_id = Some("abc123".to_string());
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
        assert_eq!(event.content_id.as_deref(), Some("abc123"));
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
            None,  // link_target
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
                content_id: None,
                version: 1,
                entry_type: 0,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: None,
                link_target: None,
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

    // ── dispatch_rust_call ─────────────────────────────────────────────

    mod dispatch_rust_call {
        use super::*;
        use crate::service_registry::{RustCallError, RustService};
        use std::sync::Arc;

        struct EchoService;

        impl RustService for EchoService {
            fn name(&self) -> &str {
                "echo"
            }
            fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
                match method {
                    "echo" => Ok(payload.to_vec()),
                    _ => Err(RustCallError::NotFound),
                }
            }
        }

        #[test]
        fn returns_none_for_unknown_service() {
            let k = Kernel::new();
            assert!(k.dispatch_rust_call("nope", "any", b"{}").is_none());
        }

        #[test]
        fn returns_none_for_python_flavoured_service() {
            // ServiceRegistry stores Python services through `enlist`;
            // dispatch_rust_call only routes Rust-flavoured ones, so
            // Python entries should fall through (None) — caller hands
            // off to the Python `dispatch_method` path.
            let k = Kernel::new();
            assert!(k.dispatch_rust_call("auth_service", "any", b"{}").is_none());
        }

        #[test]
        fn routes_through_to_registered_rust_service() {
            let k = Kernel::new();
            k.register_rust_service(
                "echo",
                Arc::new(EchoService) as Arc<dyn RustService>,
                vec![],
            )
            .unwrap();
            let out = k
                .dispatch_rust_call("echo", "echo", b"hello")
                .unwrap()
                .unwrap();
            assert_eq!(out, b"hello");
        }

        #[test]
        fn surfaces_method_not_found_from_service() {
            let k = Kernel::new();
            k.register_rust_service(
                "echo",
                Arc::new(EchoService) as Arc<dyn RustService>,
                vec![],
            )
            .unwrap();
            let err = k
                .dispatch_rust_call("echo", "nope", b"{}")
                .unwrap()
                .unwrap_err();
            assert!(matches!(err, RustCallError::NotFound));
        }
    }
}
