//! Kernel — pure Rust kernel owning all core state.
//!
//! Zero PyO3 dependency. All Python bridging lives in generated_pyo3.rs.
//!
//! Owns VFSRouter, Trie, VFS Lock, MetaStore.
//! Hook/Observer registries live in generated_pyo3::PyKernel (wrapper-only).
//!
//! Architecture:
//!   - Created empty via Kernel::new(), then components are wired by wrapper.
//!   - VFSRouter / Trie use interior mutability (&self methods).
//!   - VFS Lock is optionally Arc-shared with VFSLockManager (blocking acquire).
//!   - MetaStore (Box<dyn MetaStore>) wraps any impl (Python adapter, redb, gRPC).
//!     Each impl owns its own internal cache; there is no kernel-global
//!     metadata cache.
//!
//! Kernel struct + syscalls — pure Rust kernel boundary.

use crate::cache::index_cache::IndexCache;
use crate::core::permission_cache::PermissionLeaseCache;
use crate::dispatch::ops_registry::{
    BackendKind, CatHandlerKind, FileType, FingerprintHandlerKind, GrepHandlerKind, OpHandler,
    OpKey, OpName, OpsRegistry,
};
use crate::dispatch::{MutationObserver, PermissionProvider, Trie};
use crate::file_watch::FileWatchRegistry;
use crate::lock_manager::LockManager;
use crate::meta_store::LocalMetaStore;
#[cfg(test)]
use crate::meta_store::DT_REG;
use crate::meta_store::{DT_DIR, DT_LINK, DT_MOUNT, DT_PIPE, DT_STREAM};
use crate::vfs_router::{
    canonicalize_mount_path as canonicalize, RouteError, RouteResult, VFSRouter,
};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex, RwLock, RwLockReadGuard};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
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
/// `pub` so peer crates (`transport::grpc`, `transport::federation`)
/// can use the same generated client / server stubs without
/// re-generating them — proto definitions stay kernel-owned (the
/// build.rs that compiles `vfs.proto` lives in kernel) but the
/// generated module surface is shared.
pub mod vfs_proto {
    tonic::include_proto!("nexus.grpc.vfs");
}

// ── Per-syscall-family submodules ──────────────────────────────────
//
// Each submodule carries an `impl Kernel` block over a method subset.
// Every method remains a member of `Kernel` and is invoked the same
// way.
pub mod convenience;
mod dispatch;
mod federation;
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
    /// Federation bootstrap (env parsing, ZoneManager construction,
    /// create_zone/join_zone, reconcile) failed.
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
    /// Content generation after this read.
    pub gen: u64,
    /// DT_REG(1), DT_PIPE(3), DT_STREAM(4).
    pub entry_type: u8,
    /// DT_STREAM: next read offset (message index) for cursor advancement.
    /// None for non-stream entry types.
    pub stream_next_offset: Option<usize>,
}

/// Per-request entry for `Kernel::sys_read` (batch variant).
///
/// `offset` = byte offset into the file; `len = None` means "to EOF".
/// `timeout_ms` = blocking timeout for IPC types (DT_PIPE/DT_STREAM).
pub struct ReadRequest {
    pub path: String,
    pub offset: u64,
    pub len: Option<u64>,
    pub timeout_ms: u64,
}

/// Per-request entry for `Kernel::sys_write` (batch variant).
pub struct WriteRequest {
    pub path: String,
    pub content: Vec<u8>,
    pub offset: u64,
}

/// Per-request entry for `Kernel::sys_unlink` (batch variant).
pub struct UnlinkRequest {
    pub path: String,
    pub recursive: bool,
}

pub struct SysCatResult {
    pub data: Vec<u8>,
    pub handler: String,
    pub filetype: FileType,
    pub backend: BackendKind,
}

pub struct OpMetadataResult {
    pub filetype: FileType,
    pub backend: BackendKind,
    pub mime_type: Option<String>,
    pub backend_name: String,
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
    /// Content generation after write.
    pub gen: u64,
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
#[derive(Debug)]
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
    /// Destination content generation.
    pub gen: u64,
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
    pub gen: u64,
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
    /// User/agent identity that owns this file (from FileMetadata).
    pub owner_id: Option<String>,
}

impl From<crate::meta_store::FileMetadata> for StatResult {
    #[inline]
    fn from(entry: crate::meta_store::FileMetadata) -> Self {
        let is_dir = entry.entry_type == crate::meta_store::DT_DIR
            || entry.entry_type == crate::meta_store::DT_MOUNT;
        let mime = entry
            .mime_type
            .as_deref()
            .unwrap_or(if is_dir {
                "inode/directory"
            } else {
                "application/octet-stream"
            })
            .to_string();
        Self {
            path: entry.path,
            size: if is_dir && entry.size == 0 {
                4096
            } else {
                entry.size
            },
            content_id: entry.content_id,
            mime_type: mime,
            is_directory: is_dir,
            entry_type: entry.entry_type,
            mode: if is_dir { 0o755 } else { 0o644 },
            version: entry.version,
            gen: entry.gen,
            zone_id: entry.zone_id,
            created_at_ms: entry.created_at_ms,
            modified_at_ms: entry.modified_at_ms,
            last_writer_address: entry.last_writer_address,
            lock: None,
            link_target: entry.link_target,
            owner_id: entry.owner_id,
        }
    }
}

/// Result of paginated readdir: children + cursor for next page.
pub struct ReadDirResult {
    /// (child_path, entry_type) tuples for this page.
    pub items: Vec<(String, u8)>,
    /// Opaque cursor for the next page. `None` when no more entries.
    pub next_cursor: Option<String>,
    /// True when more entries exist beyond this page.
    pub has_more: bool,
}

// ── ZonesProcfsEntry — procfs virtual namespace ──────────────────────

/// Synthesized entry for `/__sys__/zones/*` virtual paths.
///
/// All fields are read live from `raft::ZoneManager` each call — this
/// struct carries no persisted state of its own (SSOT: raft state
/// machine). Returned by `Kernel::resolve_zones_procfs` and consumed
/// by `sys_stat` so Python callers see zone runtime state as if it
/// were a filesystem entry.
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
    /// Ready-signal passthrough — saves consumers a second Kernel call.
    pub mount_reconciliation_done: bool,
}

// ── KernelObserverRegistry — pure Rust observer dispatch ────────────────

/// Observer entry — pure Rust, no PyO3 dependency.
///
/// Stores `Arc<dyn MutationObserver>` so the OBSERVE ThreadPool worker
/// can clone the trait object across threads. `event_mask` bitmask
/// matching happens without external dependency.
struct KernelObserverEntry {
    observer: Arc<dyn MutationObserver>,
    name: String,
    event_mask: u32,
}

/// Pure Rust observer registry — event-type bitmask filtering lock-free.
///
/// Single dispatch path for all OBSERVE-phase observers. The trait
/// `MutationObserver` takes `&FileEvent`.
///
/// OBSERVE is fire-and-forget by definition — observers needing causal
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
    /// The dispatch loop (`Kernel::dispatch_observers`) submits each
    /// clone to the OBSERVE ThreadPool. Returning Arc clones lets the
    /// pool borrow the registry lock for the minimum possible time —
    /// the caller releases the lock before doing any per-observer work.
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

// ── Native Hook Registry ───────────────────────────────────────────────
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
///   - `trie_register(...)` — register path resolvers.
pub struct Kernel {
    // DriverLifecycleCoordinator — owns mount lifecycle (routing + metastore).
    pub(crate) dlc: crate::dlc::DriverLifecycleCoordinator,
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
    // for the apply-side federation-mount callback.
    metastore: parking_lot::RwLock<Option<Box<dyn crate::meta_store::MetaStore>>>,
    // Tempdir backing the boot-default ``LocalMetaStore``. ``Kernel::new``
    // creates a tempdir and opens a redb against it so bare kernels
    // (tests, quickstarts, minimal-mode boots) have a working SSOT
    // without explicit ``set_metastore_path``. The slot is dropped (set
    // to ``None``) when ``set_metastore_path`` swaps in a real path so
    // the ephemeral redb file is released along with the old metastore
    // ``Box<dyn MetaStore>``. This replaces the pre-U
    // ``MemoryMetaStore`` boot default — see the U commit body for the
    // ownership argument.
    boot_metastore_tempdir: parking_lot::RwLock<Option<tempfile::TempDir>>,
    // VFS lock timeout for blocking acquire (ms) — ``AtomicU64`` so
    // ``set_vfs_lock_timeout`` stays ``&self``; reads are lock-free.
    vfs_lock_timeout_ms: AtomicU64,
    // Max in-flight backend fetches inside `sys_read` batch path. Default 16.
    read_batch_max_concurrency: AtomicUsize,
    // Max aggregate bytes for batch reads (DoS guard). Default uncapped.
    read_batch_max_aggregate_bytes: AtomicUsize,
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
    #[allow(dead_code)]
    observers: Mutex<KernelObserverRegistry>,
    // OBSERVE is fire-and-forget by contract: the syscall returns as soon
    // as the event is queued; observer callbacks run on this pool, off
    // the hot path.
    //
    // 4 worker threads is enough for the typical workload (a handful of
    // long-lived observers: FileWatchRegistry, EventBus, etc.). Many
    // parallel Python observers will serialize on the GIL, but
    // Rust-native observers run truly parallel.
    #[allow(dead_code)]
    // observer_pool removed — inline dispatch, no background threads.
    // Zone revision counter — AtomicU64 per zone + Condvar for waiters (§10 A2)
    zone_revisions: DashMap<String, Arc<ZoneRevisionEntry>>,
    // FileWatchRegistry — inotify equivalent. Arc-shared with observer registry.
    file_watches: Arc<FileWatchRegistry>,
    // Agent registry — kernel SSOT for agent lifecycle state.  Visibility
    // is `pub(crate)`; peer crates reach it through
    // [`Self::agent_registry`] (parallel to `vfs_router_arc()` /
    // `dcache_arc()`) so any future kernel-side invariant — audit,
    // distributed replication, scheduling — has a single chokepoint.
    pub(crate) agent_registry: Arc<crate::core::agents::registry::AgentRegistry>,
    // Service registry — DashMap backing store for service lifecycle.
    pub(crate) service_registry: Arc<crate::service_registry::ServiceRegistry>,
    index_cache: IndexCache,
    pub(crate) ops_registry: OpsRegistry,
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
    // Native hook registry — pure Rust hooks dispatched lock-free.
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
    /// remote reads, LLM connector streaming). Kernel owns the runtime
    /// directly so kernel-internal callers keep the same shared runtime
    /// regardless of whether the cdylib has installed the real peer
    /// client yet.
    pub(crate) runtime: Arc<tokio::runtime::Runtime>,
    // Shared tokio runtime — constructed once at Kernel::new and used by
    // every peer RPC (scatter-gather chunk fetch + federation remote
    // reads). Replaces the one-shot `Builder::new_current_thread()` inside
    // `try_remote_fetch` so tokio's workers shut down cleanly on
    // `release_metastores`/Drop so docker stop does not hang on stuck
    // async tasks.
    //
    // Type is `Arc<dyn hal::peer::PeerBlobClient>`; the concrete impl
    // lives in `transport::blob::peer_client::PeerBlobClient`. Default
    // at boot is `NoopPeerBlobClient`; nexus-cdylib boot installs the
    // real transport impl via `Kernel::set_peer_client`.
    pub(crate) peer_client: parking_lot::RwLock<Arc<dyn crate::hal::peer::PeerBlobClient>>,
    // Control-Plane HAL §3.B.1 slot. `Arc<dyn DistributedCoordinator>` so
    // the kernel's distributed-namespace surface (zone listing, distributed-
    // lock / WAL-stream / Raft-MetaStore construction, mount wiring,
    // share registry, cluster introspection) is reachable through a trait
    // boundary rather than direct `nexus_raft::*` types. Default at boot
    // is `NoopDistributedCoordinator`; nexus-cdylib boot installs the real
    // raft-side impl via `Kernel::set_distributed_coordinator`. Same DI
    // shape as the PeerBlobClient slot above.
    pub(crate) distributed_coordinator:
        parking_lot::RwLock<Arc<dyn crate::hal::distributed_coordinator::DistributedCoordinator>>,
    // No `chunk_fetcher` field: `Kernel::peer_client` is the SSOT for
    // the cross-node blob client.  `PyKernel::sys_setattr` constructs a
    // fresh `GrpcChunkFetcher` per `DT_MOUNT` against the just-cloned
    // peer_client + current `self_address`, so a peer_client swap (or
    // a `set_self_address` after federation init) is reflected on the
    // next mount with no rebuild dance.
    /// Blob-fetcher slot stashed by federation init for the cdylib's
    /// transport-tier install hook to drain. Typed as
    /// `Box<dyn Any + Send + Sync>` so kernel does not name the
    /// raft-side `BlobFetcherSlot` type — `transport::blob::fetcher::
    /// install` downcasts to the concrete type at drain time.
    pub(crate) pending_blob_fetcher_slot:
        parking_lot::Mutex<Option<Box<dyn std::any::Any + Send + Sync>>>,
    // Distributed state (zone_manager / zone_registry / zone_runtime /
    // cross_zone_mounts / mount_reconciliation_done) lives on
    // `RaftDistributedCoordinator` in the raft crate. Kernel reaches it
    // through the `kernel::hal::distributed_coordinator::DistributedCoordinator`
    // trait.

    // ── §13 Permission gate ───────────────────────────────────────────
    //
    // Pluggable permission provider (set once at boot, never mutated).
    // When `has_permission_provider` is false, the entire permission
    // gate is skipped (~1ns AtomicBool load). When true, the gate
    // runs: lease cache → admin bypass → zone perms → provider.
    permission_provider: parking_lot::RwLock<Option<Arc<dyn PermissionProvider>>>,
    permission_lease_cache: PermissionLeaseCache,
    /// Admin bypass enabled — Docker default true.
    permission_admin_bypass: AtomicBool,
    /// Fast-path flag: skip entire permission gate when no provider is
    /// registered. AtomicBool so the hot path is a single relaxed load
    /// (~1ns) — not even a pointer dereference.
    has_permission_provider: AtomicBool,
}

impl Kernel {
    // ── Constructor ────────────────────────────────────────────────────

    /// Create an empty kernel. Components wired by wrapper after construction.
    ///
    /// `pub mod kernel` lets peer crates reach
    /// `Kernel::register_native_hook` etc. `clippy::new_without_default`
    /// is suppressed rather than auto-impl'd because `new()` does heavy
    /// wiring (runtime, peer client, dispatch hook registry, mount
    /// tables); callers should opt in explicitly via `Kernel::new()`
    /// rather than the implicit `Default::default()` shortcut.
    #[allow(clippy::new_without_default)]
    #[allow(clippy::let_and_return)]
    pub fn new() -> Self {
        // Kernel owns its tokio runtime — multi-thread, two workers
        // sized for IO-bound peer RPCs.
        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .thread_name("nexus-kernel-peer")
                .enable_all()
                .build()
                .expect("failed to build kernel tokio runtime"),
        );
        // The real peer_blob_client lives in
        // `transport::blob::peer_client`. Kernel boots with the no-op
        // fallback; the cdylib wires the real impl via
        // `Kernel::set_peer_client` before any federation read fires.
        // No `chunk_fetcher` snapshot is built here — `PyKernel::sys_setattr`
        // derives a fresh `GrpcChunkFetcher` per `DT_MOUNT` against the
        // current peer_client + self_address (see `Kernel.peer_client`
        // doc).
        let peer_client_dyn: Arc<dyn crate::hal::peer::PeerBlobClient> =
            crate::hal::peer::NoopPeerBlobClient::arc();
        // Bare kernels boot with a tempfile-backed ``LocalMetaStore`` so
        // tests, quickstarts, and minimal-mode boots have a working
        // redb-backed SSOT without explicit ``set_metastore_path``. The
        // tempdir is held by the kernel; it drops when the kernel drops
        // (or when ``set_metastore_path`` swaps in a real path). No
        // separate in-memory impl: every code path now exercises the
        // production redb implementation.
        let boot_tempdir = tempfile::tempdir().expect("failed to create kernel boot tempdir");
        let boot_redb = boot_tempdir.path().join("meta.redb");
        let boot_metastore = crate::core::meta_store::LocalMetaStore::open(&boot_redb)
            .expect("failed to open kernel boot LocalMetaStore");
        let k = Self {
            dlc: crate::dlc::DriverLifecycleCoordinator::new(),
            vfs_router: Arc::new(VFSRouter::new()),
            trie: Trie::new(),
            lock_manager: Arc::new(LockManager::new()),
            metastore: parking_lot::RwLock::new(Some(Box::new(boot_metastore))),
            boot_metastore_tempdir: parking_lot::RwLock::new(Some(boot_tempdir)),
            vfs_lock_timeout_ms: AtomicU64::new(5000),
            read_batch_max_concurrency: AtomicUsize::new(16),
            read_batch_max_aggregate_bytes: AtomicUsize::new(usize::MAX),
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
            agent_registry: Arc::new(crate::core::agents::registry::AgentRegistry::new()),
            service_registry: Arc::new(crate::service_registry::ServiceRegistry::new()),
            index_cache: IndexCache::default(),
            ops_registry: Self::default_ops_registry(),
            pipe_manager: crate::pipe_manager::PipeManager::new(),
            stream_manager: Arc::new(crate::stream_manager::StreamManager::new()),
            native_hooks: RwLock::new(NativeHookRegistry::new()),
            self_address: parking_lot::RwLock::new(None),
            runtime,
            peer_client: parking_lot::RwLock::new(peer_client_dyn),
            distributed_coordinator: parking_lot::RwLock::new(
                crate::hal::distributed_coordinator::NoopDistributedCoordinator::arc(),
            ),
            pending_blob_fetcher_slot: parking_lot::Mutex::new(None),
            permission_provider: parking_lot::RwLock::new(None),
            permission_lease_cache: PermissionLeaseCache::new(
                std::time::Duration::from_secs(30),
                100_000,
            ),
            permission_admin_bypass: AtomicBool::new(true),
            has_permission_provider: AtomicBool::new(false),
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

    fn default_ops_registry() -> OpsRegistry {
        let mut registry = OpsRegistry::new();
        registry
            .register(
                OpKey::new(OpName::new("cat"), None, None),
                OpHandler::Cat(CatHandlerKind::Default),
            )
            .expect("default cat handler registration must be unique");
        registry
            .register(
                OpKey::new(OpName::new("cat"), Some(FileType::Json), None),
                OpHandler::Cat(CatHandlerKind::JsonPretty),
            )
            .expect("json cat handler registration must be unique");
        registry
            .register(
                OpKey::new(OpName::new("grep"), None, None),
                OpHandler::Grep(GrepHandlerKind::Default),
            )
            .expect("default grep handler registration must be unique");
        registry
            .register(
                OpKey::new(OpName::new("fingerprint"), None, Some(BackendKind::S3)),
                OpHandler::Fingerprint(FingerprintHandlerKind::S3),
            )
            .expect("s3 fingerprint handler registration must be unique");
        registry
    }

    pub fn resolve_op_handler(
        &self,
        op: &str,
        filetype: &FileType,
        backend: &BackendKind,
    ) -> Option<OpHandler> {
        self.ops_registry.resolve(op, filetype, backend)
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

    /// Max in-flight backend fetches inside `_read_batch` (clamped to ≥1).
    #[inline]
    pub fn read_batch_max_concurrency(&self) -> usize {
        self.read_batch_max_concurrency
            .load(Ordering::Relaxed)
            .max(1)
    }

    /// Override the read-batch concurrency cap; clamped to ≥1.
    pub fn set_read_batch_max_concurrency(&self, n: usize) {
        self.read_batch_max_concurrency
            .store(n.max(1), Ordering::Relaxed);
    }

    /// Max aggregate bytes for batch reads (DoS guard). Default uncapped.
    #[inline]
    pub fn read_batch_max_aggregate_bytes(&self) -> usize {
        self.read_batch_max_aggregate_bytes.load(Ordering::Relaxed)
    }

    /// Override the read-batch aggregate-bytes cap.
    pub fn set_read_batch_max_aggregate_bytes(&self, n: usize) {
        self.read_batch_max_aggregate_bytes
            .store(n, Ordering::Relaxed);
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
    /// Only metastore wiring method.
    pub fn set_metastore_path(&self, path: &str) -> Result<(), KernelError> {
        let ms = LocalMetaStore::open(std::path::Path::new(path))
            .map_err(|e| KernelError::IOError(format!("LocalMetaStore: {e:?}")))?;
        *self.metastore.write() = Some(Box::new(ms));
        // Drop the boot tempdir so the ephemeral redb file is released.
        // The old metastore Box drops with the assignment above; the
        // tempdir's RAII clean-up runs here.
        *self.boot_metastore_tempdir.write() = None;
        Ok(())
    }

    /// Drop the global metastore + every per-mount metastore so the
    /// underlying redb file handles are released. Python ``NexusFS.close``
    /// calls this so a subsequent kernel can reopen the same redb path
    /// without the ``"Database already open"`` error (Issue #3765 Cat-5/6
    /// SQLite-lifecycle regression).
    pub fn release_metastores(&self) {
        *self.metastore.write() = None;
        *self.boot_metastore_tempdir.write() = None;
        // Drop per-mount metastores by clearing their slot on each
        // MountEntry. We iterate via `iter_mut` to avoid a full rebuild.
        for mut entry in self.vfs_router.entries_iter_mut() {
            entry.metastore = None;
        }
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

    /// Same as [`Self::with_metastore`], but consumes the per-mount
    /// metastore Arc already populated on [`crate::vfs_router::RouteResult`]
    /// — saves the second `get_canonical` lookup `with_metastore`
    /// otherwise performs on top of `route()`. Hot-path callers
    /// (sys_read, sys_stat, sys_unlink) prefer this entry.
    pub(crate) fn with_metastore_route<F, R>(
        &self,
        route: &crate::vfs_router::RouteResult,
        f: F,
    ) -> Option<R>
    where
        F: FnOnce(&dyn crate::meta_store::MetaStore) -> R,
    {
        if let Some(ms) = route.metastore.as_ref() {
            return Some(f(ms.as_ref()));
        }
        self.metastore.read().as_ref().map(|ms| f(ms.as_ref()))
    }

    // ── MetaStore routing ────────────────────────────────────────────
    //
    // The metastore abstraction owns key translation. Callers
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
    /// DRY helper for the ~10 write paths that persist inode
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
        gen: u64,
        version: u32,
        mime_type: Option<String>,
        created_at_ms: Option<i64>,
        modified_at_ms: Option<i64>,
    ) -> crate::meta_store::FileMetadata {
        crate::meta_store::FileMetadata {
            path: path.to_string(),
            size,
            content_id,
            gen,
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
            owner_id: None,
        }
    }

    // ── MetaStore proxy methods (for Python RustMetastoreProxy) ────────
    //
    // These route via ``vfs_router.route(path, ROOT_ZONE_ID, ...)`` so a
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

    /// Persist a metadata row.
    ///
    /// Routing zone is derived from ``metadata.zone_id`` — the row IS the
    /// SSOT for which zone owns it, so callers don't pass a separate zone
    /// parameter. ``None``/``"root"`` falls back to the root namespace.
    pub fn metastore_put(
        &self,
        path: &str,
        mut metadata: crate::meta_store::FileMetadata,
    ) -> Result<(), KernelError> {
        let zone = metadata
            .zone_id
            .as_deref()
            .unwrap_or(contracts::ROOT_ZONE_ID);
        let mount_point = self.resolve_mount_point(path, zone);
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
            // Ask the child metastore to list its own full-path
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
        match self.with_metastore(&old_mp, |ms| ms.rename_path(old_path, new_path, false)) {
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
    // (Moved to `kernel::locks` submodule.)

    /// DT_LINK transparent follow for `sys_read` / `sys_write` /
    /// `sys_copy`. Returns the absolute target path for a DT_LINK
    /// `entry`, `None` for non-link entries (caller continues with the
    /// original path). Self-loop and missing-target are surfaced here;
    /// chained-link rejection is the caller's responsibility — the
    /// recursive sys_* invocation re-loads the target's entry, sees
    /// `entry_type == DT_LINK`, and rejects via its own
    /// `max_link_hops == 0` branch.
    ///
    /// Resolution must happen AFTER the entry is loaded from
    /// authoritative storage. Cold-cache and cross-mount follows would
    /// otherwise silently fall through as if the path were a regular
    /// file.
    ///
    /// `sys_stat` deliberately bypasses link follow — `lstat` semantics
    /// require the raw DT_LINK metadata, not the resolved target.
    pub(crate) fn dt_link_target<'e>(
        path: &str,
        entry: &'e crate::meta_store::FileMetadata,
    ) -> Result<Option<&'e str>, KernelError> {
        if entry.entry_type != DT_LINK {
            return Ok(None);
        }
        let target = entry.link_target.as_deref().ok_or_else(|| {
            KernelError::PermissionDenied(format!("DT_LINK at {path} has no link_target"))
        })?;
        if target == path {
            return Err(KernelError::PermissionDenied(format!(
                "DT_LINK self-loop at {path}"
            )));
        }
        Ok(Some(target))
    }

    /// Clone the shared VFSRouter ``Arc`` for federation apply-event
    /// callbacks that need to look up mount-points-for-zone at
    /// invalidation time. The cache itself lives as long as *any*
    /// holder.
    #[allow(dead_code)]
    pub(crate) fn vfs_router_handle(&self) -> Arc<VFSRouter> {
        Arc::clone(&self.vfs_router)
    }

    // ── Router proxy methods ───────────────────────────────────────────
    // Mount-table primitives live in the `kernel::mount` submodule.
    // Federation-mount apply wiring lives on
    // `nexus_raft::distributed_coordinator::RaftDistributedCoordinator`.

    /// Install the apply-side dcache invalidation callback for a
    /// federation mount (coherence-key fanout).
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
    /// Why coherence_key and not Arc identity: every
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
        content_id: Option<&str>,
        size: Option<u64>,
        version: Option<u32>,
        created_at_ms: Option<i64>,
        // -- DT_LINK params (entry_type == 6) --
        link_target: Option<&str>,
        // -- DT_MOUNT explicit source (entry_type == 2) --
        //
        // When `source` is `Some(addr)` and `addr` is non-empty, the mount
        // semantics flip from "create the target zone locally" to "join the
        // target zone at this leader address".  Mirrors the explicit
        // `mount remote-addr:/remote /local` direction (joiner picks up
        // remote metadata) versus `mount /local remote-addr:/remote`
        // (sharer publishes local metadata).
        source: Option<&str>,
        // -- DT_MOUNT remote metastore (entry_type == 2) --
        //
        // Optional remote metastore produced by ObjectStoreProvider::build()
        // for remote backends. Installed on the VFS route entry after mount
        // registration so remote reads resolve through the correct metastore.
        remote_metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
    ) -> Result<SysSetAttrResult, KernelError> {
        match entry_type {
            2 => {
                // DT_MOUNT — full mount lifecycle via DLC.
                //
                // Zone-create-on-mount: when the caller did not supply
                // a `metastore` AND federation is active, ask the
                // DistributedCoordinator to materialise (auto-create) the
                // target zone's raft group and hand back an
                // `Arc<dyn MetaStore>` backed by the per-zone state
                // machine. Service-tier callers therefore reach
                // federation through the standard `sys_setattr DT_MOUNT`
                // syscall — no separate `kernel.zone_create` surface.
                //
                // The apply-side dcache coherence callback is installed
                // after routing is wired (handled by the provider's
                // `wire_mount` follow-up below). Install is keyed on
                // the state machine's ``coherence_id``, not on the
                // per-mount MetaStore Arc, so crosslinks of the same
                // zone share one callback.
                let coordinator = self.distributed_coordinator();
                // Federation readiness via the trait's is_initialized — true
                // once init_from_env completes regardless of whether any
                // zones are loaded.  This matters for dynamic-bootstrap
                // mode (NEXUS_PEERS empty), where zones are zero at boot
                // but the coordinator is fully ready to accept create_zone
                // / join_cluster calls.  Using list_zones as a readiness
                // shadow misclassified that state.
                let federation_active = coordinator.is_initialized(self);
                let source_addr = source.map(str::trim).filter(|s| !s.is_empty());
                let metastore = match (metastore, source_addr) {
                    (Some(m), _) => Some(m),
                    (None, Some(addr)) if federation_active && !zone_id.is_empty() => {
                        // Explicit source given — interpret as `mount
                        // remote-addr:/zone-id /local-path`: this node
                        // joins an existing cluster at `addr` (joiner
                        // semantics) rather than self-bootstrapping a
                        // 1-voter group.  Leader ConfChangeV2 AddNode
                        // commits + snapshot install populates ConfState.
                        coordinator
                            .join_cluster(self, zone_id, addr, false)
                            .map_err(KernelError::Federation)?;
                        coordinator.metastore_for_zone(self, zone_id).ok()
                    }
                    (None, None) if federation_active && !zone_id.is_empty() => {
                        // No source given — interpret as `mount
                        // /local-path remote-addr:/zone-id`: this node
                        // contributes a fresh 1-voter zone (creator
                        // semantics).  Subsequent peers join via the
                        // explicit-source path above.
                        let _ = coordinator.create_zone(self, zone_id);
                        coordinator.metastore_for_zone(self, zone_id).ok()
                    }
                    (None, _) => None,
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
                // commit.  Only fires for cross-zone federation mounts
                // (target_zone != parent_zone) — same-zone mounts (e.g.
                // typed connector backends like openai/anthropic where
                // zone_id defaults to "root" and the parent is also
                // "root") are local, non-replicated mounts and must
                // keep the backend the provider just constructed.
                //
                // Parent zone derived via the SAME longest-prefix route
                // ``DLC::mount`` uses to write the DT_MOUNT entry into
                // the parent zone's metastore (rust/kernel/src/core/dlc.rs
                // line 80).  For ``/family/work`` (target=corp), the
                // parent path ``/family`` routes to the family zone —
                // the entry replicates through family's raft log, so
                // the apply_cb that observes the new mount must be the
                // one installed on family (NOT root, which never sees
                // the entry).  Caught by TestCrossZoneDailyWorkflow's
                // crosslink read on the follower peer.
                //
                // We route the PARENT directory, not the path itself,
                // because by this point ``dlc.mount`` has already
                // registered ``path`` — routing it would resolve to
                // the new mount's own ``target_zone`` (corp), giving
                // the wrong answer for the same-zone-guard below.
                let parent_dir = path.rsplit_once('/').map(|(p, _)| p).unwrap_or("/");
                let parent_dir = if parent_dir.is_empty() {
                    "/"
                } else {
                    parent_dir
                };
                let parent_zone = self
                    .vfs_router
                    .route(parent_dir, contracts::ROOT_ZONE_ID)
                    .ok()
                    .map(|r| r.zone_id)
                    .filter(|z| !z.is_empty())
                    .unwrap_or_else(|| contracts::ROOT_ZONE_ID.to_string());
                if federation_active && !zone_id.is_empty() && zone_id != parent_zone {
                    let _ = coordinator.wire_mount(self, &parent_zone, path, zone_id);
                }
                // Install remote metastore on the VFS route entry if the
                // backend provider produced one (remote backends only).
                if let Some(rms) = remote_metastore {
                    let canonical_key = format!("/{zone_id}{path}");
                    self.vfs_router.install_metastore(&canonical_key, rms);
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
                // UPDATE existing DT_REG, or CREATE if path does not exist (upsert).
                self.setattr_update(
                    path,
                    zone_id,
                    mime_type,
                    modified_at_ms,
                    content_id,
                    size,
                    version,
                    created_at_ms,
                )
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
    /// resolves at sys_read / sys_write / sys_copy time (one hop, with
    /// cycle detection — see `Kernel::dt_link_target` and the
    /// `max_link_hops` parameter on `sys_read_single` etc.).
    /// Self-loops (`link_target == path`) are rejected here so the
    /// resolver never has to handle them at lookup time. Idempotent for
    /// an existing DT_LINK at the same path with the same target.
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
            gen: 0,
            version: 1,
            entry_type: DT_LINK,
            zone_id: Some(zone_id.to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: self.self_address.read().clone(),
            target_zone_id: None,
            link_target: Some(link_target.to_string()),
            owner_id: None,
        };
        self.metastore_put(path, meta)?;
        // The metastore impl populates its own internal cache during
        // ``put`` — no separate kernel-side cache to seed.
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
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_PIPE,
            capacity as u64,
            None,
            0,
            1,
            None,
            None,
            None,
        );
        self.metastore_put(path, meta)
    }

    /// Write DT_STREAM inode to metastore (shared by create_stream and SHM path).
    #[allow(dead_code)]
    fn write_stream_inode(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        let meta = self.build_metadata(
            path,
            contracts::ROOT_ZONE_ID,
            DT_STREAM,
            capacity as u64,
            None,
            0,
            1,
            None,
            None,
            None,
        );
        self.metastore_put(path, meta)
    }

    /// DT_DIR: create directory inode via metastore.
    fn setattr_create_dir(
        &self,
        path: &str,
        zone_id: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Idempotent: if DT_DIR (or DT_MOUNT, which is directory-like since
        // a mount point IS a directory) already exists, no-op. This matches
        // ``mkdir(exist_ok=True)`` semantics — a mount creates the directory
        // slot, so a follow-up mkdir on the same path shouldn't fail.
        let mount_point = self.resolve_mount_point(path, zone_id);
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
            0,
            1,
            Some("inode/directory".to_string()),
            Some(now_ms),
            Some(now_ms),
        );
        // metastore_put derives routing zone from meta.zone_id — set it
        // above (build_metadata writes zone_id), so this is zone-aware.
        self.metastore_put(path, meta)?;

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

    /// UPDATE existing DT_REG, or CREATE if path does not exist (upsert).
    ///
    /// When the metastore has no entry for `path`, a new DT_REG is created
    /// with the supplied fields (mirrors `setattr_create_dir` semantics).
    /// This eliminates the need for Python callers to use `metastore_put`
    /// to create file metadata entries.
    #[allow(clippy::too_many_arguments)]
    fn setattr_update(
        &self,
        path: &str,
        zone_id: &str,
        mime_type: Option<&str>,
        modified_at_ms: Option<i64>,
        content_id: Option<&str>,
        size: Option<u64>,
        version: Option<u32>,
        created_at_ms: Option<i64>,
    ) -> Result<SysSetAttrResult, KernelError> {
        // Route-scoped metastore resolution — same path sys_write/sys_read
        // use, ensuring SSOT. Falls back to global metastore_get/metastore_put
        // when no VFS route covers the path (e.g. boot-time, tests).
        let route = self.vfs_router.route(path, zone_id).ok();

        let existing: Option<crate::meta_store::FileMetadata> = if let Some(ref r) = route {
            self.with_metastore_route(r, |ms| ms.get(path).ok().flatten())
                .flatten()
        } else {
            self.metastore_get(path)?
        };

        // ── CREATE: path does not exist — build new DT_REG ──────────
        let Some(meta) = existing else {
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);

            let effective_zone = route
                .as_ref()
                .map(|r| r.zone_id.as_str())
                .unwrap_or(zone_id);
            let new_meta = self.build_metadata(
                path,
                effective_zone,
                crate::meta_store::DT_REG,
                size.unwrap_or(0),
                content_id.map(|s| s.to_string()),
                0, // gen — setattr create, gen will be set on first write
                version.unwrap_or(1),
                mime_type.map(|s| s.to_string()),
                Some(created_at_ms.unwrap_or(now_ms)),
                Some(modified_at_ms.unwrap_or(now_ms)),
            );
            if let Some(ref r) = route {
                self.with_metastore_route(r, |ms| ms.put(path, new_meta))
                    .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
                    .and_then(|r| {
                        r.map_err(|e| {
                            KernelError::IOError(format!("setattr_update put({path}): {e:?}"))
                        })
                    })?;
            } else {
                self.metastore_put(path, new_meta)?;
            }

            return Ok(SysSetAttrResult {
                path: path.to_string(),
                created: true,
                entry_type: crate::meta_store::DT_REG as i32,
                backend_name: None,
                capacity: None,
                updated: Vec::new(),
                shm_path: None,
                data_rd_fd: None,
                space_rd_fd: None,
            });
        };

        // No fields to update → idempotent open (no-op)
        if mime_type.is_none()
            && modified_at_ms.is_none()
            && content_id.is_none()
            && size.is_none()
            && version.is_none()
            && created_at_ms.is_none()
        {
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
        if let Some(cid) = content_id {
            new_meta.content_id = Some(cid.to_string());
            updated_fields.push("content_id".to_string());
        }
        if let Some(s) = size {
            new_meta.size = s;
            updated_fields.push("size".to_string());
        }
        if let Some(v) = version {
            new_meta.version = v;
            updated_fields.push("version".to_string());
        }
        if let Some(ca) = created_at_ms {
            new_meta.created_at_ms = Some(ca);
            updated_fields.push("created_at_ms".to_string());
        }

        if let Some(ref r) = route {
            self.with_metastore_route(r, |ms| ms.put(path, new_meta))
                .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
                .and_then(|r| {
                    r.map_err(|e| {
                        KernelError::IOError(format!("setattr_update put({path}): {e:?}"))
                    })
                })?;
        } else {
            self.metastore_put(path, new_meta)?;
        }

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

    // ── Observer registry ─────────────────────────────────────────────
    // (Moved to `kernel::observability` submodule.)

    // ── Native INTERCEPT hook dispatch ────────────────────────────────
    // (Moved to `kernel::dispatch` submodule.)

    /// Borrow the kernel's shared tokio runtime — kernel owns this Arc
    /// directly; peer crates (backends LLM connectors, transport gRPC
    /// server) clone it for their async work.
    pub fn runtime(&self) -> &Arc<tokio::runtime::Runtime> {
        &self.runtime
    }

    /// Replace the kernel's `peer_client` slot with a concrete
    /// implementation. Kernel boots with `NoopPeerBlobClient`; the
    /// cdylib boot path calls this with the real
    /// `transport::blob::peer_client::PeerBlobClient` once per kernel.
    pub fn set_peer_client(&self, client: Arc<dyn crate::hal::peer::PeerBlobClient>) {
        *self.peer_client.write() = client;
    }

    /// Borrow the current peer-client trait object — read-locked
    /// snapshot.  Internal callers use this to issue federation
    /// reads without holding the lock across `.await`.
    pub fn peer_client_arc(&self) -> Arc<dyn crate::hal::peer::PeerBlobClient> {
        Arc::clone(&self.peer_client.read())
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

    /// Resolve a VFS path to its locally-stored ``content_id``.
    ///
    /// Runs the same chain as ``sys_stat``'s metadata fetch (validate,
    /// route, per-mount metastore lookup, ``content_id`` non-empty
    /// filter) and returns the value the local backend expects: CAS
    /// hash for content-addressed mounts, backend-relative path for
    /// path-addressed mounts. Returns ``None`` on routing failure,
    /// missing metadata, or empty content_id.
    ///
    /// Public surface (kernel's syscall layer, not MetaStore-shaped) so
    /// cross-crate callers (federation's ``KernelBlobFetcher``) reach it
    /// through the same boundary the syscall API uses — no
    /// ``Arc<dyn MetaStore>`` leak across crates.
    pub fn lookup_content_id(&self, path: &str, zone_id: &str) -> Option<String> {
        let route = self.vfs_router.route(path, zone_id).ok()?;
        self.with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
            .flatten()
            .and_then(|m| m.content_id)
            .filter(|s| !s.is_empty())
    }

    /// Hand out a long-lived closure that calls
    /// [`Self::lookup_content_id`] under a fixed ``zone_id``. The
    /// closure clones the kernel's ``Arc`` so it survives the call
    /// frame that produced it — federation's ``KernelBlobFetcher``
    /// holds it for the lifetime of the gRPC server.
    #[allow(clippy::type_complexity)]
    pub fn content_id_lookup_fn(
        self: &Arc<Self>,
        zone_id: &str,
    ) -> Arc<dyn Fn(&str) -> Option<String> + Send + Sync> {
        let kernel = Arc::clone(self);
        let zone = zone_id.to_string();
        Arc::new(move |path: &str| kernel.lookup_content_id(path, &zone))
    }

    /// Borrow the kernel's `AgentRegistry` (the per-PID SSOT).  Used by
    /// service-tier callers (`services::managed_agent`, ACP install
    /// hooks, AgentStatusResolver) that need to register / observe /
    /// query agent state without going through a syscall.
    pub fn agent_registry(&self) -> &Arc<crate::core::agents::registry::AgentRegistry> {
        &self.agent_registry
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
    /// Hook construction + registration belong to `services::audit`;
    /// the kernel half owns only the stream-lifecycle work (kernel
    /// concern).
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
    // (Moved to `kernel::observability` submodule.)

    // ── IPC Registry — Pipe + Stream methods ────────────────────────────
    // (Moved to `kernel::ipc` submodule.)

    // ── File I/O syscalls (sys_read / sys_write / sys_stat / sys_unlink /
    //    sys_rename / sys_copy / sys_mkdir / sys_rmdir) ──────────────────
    // (Moved to `kernel::io` submodule.)

    // ── sys_grep + sys_glob ───────────────────────────────────────────
    //
    // Two read-only "search" syscalls that wrap `lib::search` /
    // `lib::glob` algorithms inside the standard syscall pipeline
    // (validate path → walk recursive prefix scan → INTERCEPT-free
    // since reads are routed through `sys_read`).

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
            // Probe entry_type via the routed metastore; skip
            // non-regular entries. A miss is conservatively treated as
            // regular (the metastore stamped it; sys_read will fail
            // gracefully if the underlying backend disagrees).
            if let Ok(route) = self.vfs_router.route(&fpath, &ctx.zone_id) {
                if let Some(Some(meta)) =
                    self.with_metastore_route(&route, |ms| ms.get(&fpath).ok().flatten())
                {
                    if meta.entry_type != crate::meta_store::DT_REG {
                        continue;
                    }
                }
            }
            let bytes = match self.sys_read_one(&fpath, ctx, 5000, 0) {
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
// Free-function helpers — take only ``Arc``-shared kernel state so the
// apply-side ``mount_apply_cb`` closure can call them without a
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

// ── Drop ────────────────────────────────────────────────────────────────

impl Drop for Kernel {
    fn drop(&mut self) {
        // Shut down the kernel-owned tokio runtime so its worker threads
        // exit promptly. Without this, the two `nexus-kernel-peer` threads
        // survive past Python process exit and keep xdist worker processes
        // alive indefinitely (~39 min hang on macOS CI).
        //
        // We replace the Arc with a dummy single-threaded runtime, then
        // drop the original. When the last Arc ref drops, tokio's own
        // Drop impl shuts down the worker threads. The swap ensures this
        // Kernel's drop triggers the shutdown even if other Arcs exist
        // (they'd hold the dummy, which is cheap to drop).
        let dummy = std::sync::Arc::new(
            tokio::runtime::Builder::new_current_thread()
                .build()
                .expect("dummy runtime for Kernel::drop"),
        );
        let old = std::mem::replace(&mut self.runtime, dummy);
        // Explicitly drop — if this is the last Arc, tokio shuts down workers.
        drop(old);
    }
}

// ── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use crate::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use parking_lot::Mutex;
    use std::collections::HashMap;

    use super::*;

    #[derive(Default)]
    struct TestObjectStore {
        blobs: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl ObjectStore for TestObjectStore {
        fn name(&self) -> &str {
            "test"
        }

        fn write_content(
            &self,
            content: &[u8],
            content_id: &str,
            _ctx: &OperationContext,
            offset: u64,
        ) -> Result<WriteResult, StorageError> {
            let key = content_id.to_string();
            let mut blobs = self.blobs.lock();
            let mut data = if offset > 0 {
                blobs.get(&key).cloned().unwrap_or_default()
            } else {
                Vec::new()
            };
            let start = offset as usize;
            if start > data.len() {
                data.resize(start, 0);
            }
            let end = start + content.len();
            if end > data.len() {
                data.resize(end, 0);
            }
            data[start..end].copy_from_slice(content);
            let size = data.len() as u64;
            blobs.insert(key.clone(), data);
            Ok(WriteResult {
                content_id: key.clone(),
                version: key,
                size,
            })
        }

        fn read_content(
            &self,
            content_id: &str,
            _ctx: &OperationContext,
        ) -> Result<Vec<u8>, StorageError> {
            self.blobs
                .lock()
                .get(content_id)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(content_id.into()))
        }

        fn delete_file(&self, path: &str) -> Result<(), StorageError> {
            self.blobs.lock().remove(path);
            Ok(())
        }

        fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
            self.blobs
                .lock()
                .get(content_id)
                .map(|data| data.len() as u64)
                .ok_or_else(|| StorageError::NotFound(content_id.into()))
        }

        fn copy_file(&self, src_path: &str, dst_path: &str) -> Result<WriteResult, StorageError> {
            let mut blobs = self.blobs.lock();
            let data = blobs
                .get(src_path)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(src_path.into()))?;
            let size = data.len() as u64;
            blobs.insert(dst_path.to_string(), data);
            Ok(WriteResult {
                content_id: dst_path.to_string(),
                version: dst_path.to_string(),
                size,
            })
        }
    }

    fn kernel_with_root_backend() -> Kernel {
        let k = Kernel::new();
        let backend: std::sync::Arc<dyn ObjectStore> =
            std::sync::Arc::new(TestObjectStore::default());
        k.add_mount(
            "/",
            contracts::ROOT_ZONE_ID,
            Some(backend),
            None,
            None,
            false,
        )
        .unwrap();
        k
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

    #[test]
    fn sys_write_increments_content_generation() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/gen.txt", DT_REG as i32).unwrap();

        let first = k.sys_write_one("/gen.txt", &ctx, b"one", 0).unwrap();
        let second = k.sys_write_one("/gen.txt", &ctx, b"two", 0).unwrap();
        let stat = k.sys_stat("/gen.txt", "root").unwrap();

        assert_eq!(first.gen, 1);
        assert_eq!(second.gen, 2);
        assert_eq!(stat.gen, 2);
    }

    #[test]
    fn sys_setattr_metadata_update_preserves_generation() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/mime.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/mime.txt", &ctx, b"body", 0).unwrap();

        k.sys_setattr(
            "/mime.txt",
            0,
            "",
            None,
            None,
            None,
            "memory",
            "root",
            false,
            0,
            None,
            None,
            Some("text/plain"),
            Some(1234),
            None,
            None,
            None,
            None, // created_at_ms
            None, // link_target
            None, // source
            None, // remote_metastore
        )
        .unwrap();

        let stat = k.sys_stat("/mime.txt", "root").unwrap();
        assert_eq!(stat.gen, 1);
    }

    #[test]
    fn copy_uses_destination_generation() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/src.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/src.txt", &ctx, b"body", 0).unwrap();

        let copied = k.sys_copy("/src.txt", "/dst.txt", &ctx).unwrap();
        let dst = k.sys_stat("/dst.txt", "root").unwrap();

        assert_eq!(copied.gen, 1);
        assert_eq!(dst.gen, 1);

        let copied_again = k.sys_copy("/src.txt", "/dst.txt", &ctx).unwrap();
        let dst_again = k.sys_stat("/dst.txt", "root").unwrap();

        assert_eq!(copied_again.gen, 2);
        assert_eq!(dst_again.gen, 2);
    }

    #[test]
    fn copy_rejects_non_regular_destination() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/src.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/src.txt", &ctx, b"body", 0).unwrap();
        k.sys_mkdir("/dst", &ctx, true, true).unwrap();

        match k.sys_copy("/src.txt", "/dst", &ctx) {
            Err(KernelError::InvalidPath(msg)) => {
                assert!(msg.contains("destination is not a regular file"));
            }
            Ok(_) => panic!("expected non-regular destination copy to fail"),
            Err(other) => {
                panic!("expected InvalidPath for non-regular destination, got {other:?}");
            }
        }
        assert_eq!(
            k.sys_stat("/dst", "root").unwrap().entry_type,
            crate::meta_store::DT_DIR
        );
    }

    #[test]
    fn copy_overwrite_preserves_destination_created_at() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/src.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/src.txt", &ctx, b"new", 0).unwrap();
        setattr(&k, "/dst.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/dst.txt", &ctx, b"old", 0).unwrap();

        let mut dst_meta = k.metastore_get("/dst.txt").unwrap().unwrap();
        dst_meta.created_at_ms = Some(123);
        k.metastore_put("/dst.txt", dst_meta).unwrap();

        k.sys_copy("/src.txt", "/dst.txt", &ctx).unwrap();
        let dst = k.sys_stat("/dst.txt", "root").unwrap();

        assert_eq!(dst.created_at_ms, Some(123));
        assert_eq!(dst.gen, 2);
    }

    #[test]
    fn copy_snapshot_failure_releases_vfs_locks() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/src.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/src.txt", &ctx, b"new", 0).unwrap();
        setattr(&k, "/dst.txt", DT_REG as i32).unwrap();
        k.sys_write_one("/dst.txt", &ctx, b"old", 0).unwrap();

        let mut dst_meta = k.metastore_get("/dst.txt").unwrap().unwrap();
        dst_meta.content_id = Some("/missing-destination-content.txt".to_string());
        k.metastore_put("/dst.txt", dst_meta).unwrap();

        assert!(matches!(
            k.sys_copy("/src.txt", "/dst.txt", &ctx),
            Err(KernelError::BackendError(msg)) if msg.contains("failed to snapshot destination")
        ));

        let lm = k.lock_manager_arc();
        let dst_handle = lm.blocking_acquire("/dst.txt", crate::lock_manager::LockMode::Write, 0);
        assert_ne!(dst_handle, 0, "destination VFS lock leaked");
        lm.do_release(dst_handle);

        let src_handle = lm.blocking_acquire("/src.txt", crate::lock_manager::LockMode::Write, 0);
        assert_ne!(src_handle, 0, "source VFS lock leaked");
        lm.do_release(src_handle);
    }

    #[test]
    fn batch_write_increments_each_path_generation() {
        let k = kernel_with_root_backend();
        let ctx = OperationContext::new("test", "root", true, None, true);
        setattr(&k, "/a.txt", DT_REG as i32).unwrap();
        setattr(&k, "/b.txt", DT_REG as i32).unwrap();

        let first = k.sys_write(
            &[
                WriteRequest {
                    path: "/a.txt".to_string(),
                    content: b"a1".to_vec(),
                    offset: 0,
                },
                WriteRequest {
                    path: "/b.txt".to_string(),
                    content: b"b1".to_vec(),
                    offset: 0,
                },
            ],
            &ctx,
        );
        let second = k.sys_write(
            &[WriteRequest {
                path: "/a.txt".to_string(),
                content: b"a2".to_vec(),
                offset: 0,
            }],
            &ctx,
        );

        assert_eq!(first[0].as_ref().unwrap().gen, 1);
        assert_eq!(first[1].as_ref().unwrap().gen, 1);
        assert_eq!(second[0].as_ref().unwrap().gen, 2);
        assert_eq!(k.sys_stat("/a.txt", "root").unwrap().gen, 2);
        assert_eq!(k.sys_stat("/b.txt", "root").unwrap().gen, 1);
    }

    // ── §11 OBSERVE ThreadPool tests ───────────────────────────────

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

    // ── §11 dispatch_mutation context propagation tests ────────────

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
            zone_perms: vec![],
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
            zone_perms: vec![],
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
            None,  // content_id
            None,  // size
            None,  // version
            None,  // created_at_ms
            None,  // link_target
            None,  // source
            None,  // remote_metastore
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
                gen: 0,
                version: 1,
                entry_type: 0,
                zone_id: None,
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: None,
                link_target: None,
                owner_id: None,
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
                None,
                None,
                None,
                None,
                None, // source
                None, // remote_metastore
            )
            .unwrap();
        assert!(!r.created);
        assert_eq!(r.updated, vec!["mime_type"]);
    }

    #[test]
    fn sys_setattr_update_creates_on_miss() {
        let k = Kernel::new();
        let result = setattr(&k, "/newfile", 0);
        assert!(result.is_ok());
        let r = result.unwrap();
        assert!(r.created);
        assert_eq!(r.entry_type, 0); // DT_REG

        // Idempotent: second call is an update (created=false)
        let r2 = setattr(&k, "/newfile", 0).unwrap();
        assert!(!r2.created);
    }

    // ── Metastore-key tests ────────────────────────────────────────────
    //
    // The kernel passes full global paths to the metastore trait.
    // ZoneMetaStore (the federation impl) internalizes the translation
    // to zone-relative — see rust/kernel/src/raft_metastore.rs for that
    // coverage. These tests use LocalMetaStore (full-path store) so
    // they exercise the kernel call path without any translation.

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
        // DT_DIR at "/data/sub" stores metastore key "/data/sub" (full
        // global path). ZoneMetaStore internalizes zone-relative
        // translation, so generic full-path stores see full keys.
        let k = Kernel::new();
        let ms = temp_metastore();
        k.add_mount("/data", "root", None, Some(ms.clone()), None, false)
            .unwrap();

        // Create DT_DIR via sys_setattr — writes to per-mount metastore.
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
                None,
                None,
                None,
                None,
                None, // source
                None, // remote_metastore
            )
            .unwrap();
        assert!(r.created);

        // Key is the full global path.
        assert!(
            ms.get("/data/sub").unwrap().is_some(),
            "full path /data/sub must exist"
        );
        assert!(
            ms.get("/sub").unwrap().is_none(),
            "zone-relative key /sub must NOT exist"
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
            None,
            None,
            None,
            None,
            None, // source
            None, // remote_metastore
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
    fn test_sys_rename_cross_mount_rejected() {
        // Cross-mount rename is always rejected — both PAS and CAS. Callers
        // must use copy + delete. Verify both metastores remain unchanged.
        use crate::meta_store::{FileMetadata, LocalMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        let _td = tempfile::tempdir().unwrap();
        let ms_a = Arc::new(LocalMetaStore::open(&_td.path().join("a.redb")).unwrap());
        let ms_b = Arc::new(LocalMetaStore::open(&_td.path().join("b.redb")).unwrap());

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

        // Seed a file in mount A's metastore using full VFS paths.
        let meta = FileMetadata {
            path: "/mnt_a/file.txt".to_string(),
            size: 42,
            gen: 0,
            entry_type: DT_REG,
            ..Default::default()
        };
        ms_a.put("/mnt_a/file.txt", meta).unwrap();

        // Cross-mount rename must be rejected with an IOError.
        let ctx = OperationContext::new("test", zone, true, None, true);
        let err = k
            .sys_rename("/mnt_a/file.txt", "/mnt_b/file.txt", &ctx)
            .expect_err("cross-mount rename must return Err");
        match err {
            KernelError::IOError(msg) => {
                assert!(
                    msg.contains("cross-mount"),
                    "error should mention cross-mount: {msg}"
                );
            }
            other => panic!("expected IOError, got {other:?}"),
        }

        // Both metastores must be unchanged.
        assert!(
            ms_a.exists("/mnt_a/file.txt").unwrap(),
            "source metastore must not be modified after rejected rename"
        );
        assert!(
            !ms_b.exists("/mnt_b/file.txt").unwrap(),
            "destination metastore must not be populated after rejected rename"
        );
    }

    #[test]
    fn test_sys_rename_cross_mount_directory_rejected() {
        // Cross-mount directory rename is rejected; all source children unchanged.
        use crate::meta_store::{FileMetadata, LocalMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        let _td = tempfile::tempdir().unwrap();
        let ms_a = Arc::new(LocalMetaStore::open(&_td.path().join("a.redb")).unwrap());
        let ms_b = Arc::new(LocalMetaStore::open(&_td.path().join("b.redb")).unwrap());

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

        // Seed a directory with children using full VFS paths.
        ms_a.put(
            "/mnt_a/docs",
            FileMetadata {
                path: "/mnt_a/docs".into(),
                gen: 0,
                entry_type: DT_DIR,
                ..Default::default()
            },
        )
        .unwrap();
        ms_a.put(
            "/mnt_a/docs/a.md",
            FileMetadata {
                path: "/mnt_a/docs/a.md".into(),
                size: 10,
                gen: 0,
                entry_type: DT_REG,
                ..Default::default()
            },
        )
        .unwrap();

        let ctx = OperationContext::new("test", zone, true, None, true);
        let err = k
            .sys_rename("/mnt_a/docs", "/mnt_b/docs", &ctx)
            .expect_err("cross-mount rename must return Err");
        assert!(matches!(err, KernelError::IOError(_)));

        // Source children must be unchanged.
        assert!(ms_a.exists("/mnt_a/docs").unwrap());
        assert!(ms_a.exists("/mnt_a/docs/a.md").unwrap());
        assert!(!ms_b.exists("/mnt_b/docs").unwrap());
        assert!(!ms_b.exists("/mnt_b/docs/a.md").unwrap());
    }

    /// sys_unlink on a DT_MOUNT path runs the full unmount lifecycle:
    /// metastore delete + dcache evict + routing remove. Replaces today's
    /// silent miss; callers no longer need a separate Python-side shim.
    #[test]
    fn test_sys_unlink_mount_root_delegates_to_dlc_unmount() {
        use crate::meta_store::{FileMetadata, LocalMetaStore};
        use std::sync::Arc;

        let k = Kernel::new();
        let zone = contracts::ROOT_ZONE_ID;

        let _td = tempfile::tempdir().unwrap();
        let ms = Arc::new(LocalMetaStore::open(&_td.path().join("meta.redb")).unwrap());
        k.vfs_router.add_mount("/mnt", zone, None, false);
        let canon = crate::vfs_router::canonicalize_mount_path("/mnt", zone);
        k.vfs_router
            .install_metastore(&canon, ms.clone() as Arc<dyn crate::meta_store::MetaStore>);

        // Seed a DT_MOUNT entry at the mount root and a child file.
        let mount_meta = FileMetadata {
            path: "/mnt".to_string(),
            gen: 0,
            entry_type: DT_MOUNT,
            zone_id: Some(zone.to_string()),
            ..Default::default()
        };
        ms.put("/mnt", mount_meta).unwrap();

        let ctx = OperationContext::new("test", zone, true, None, true);
        let result = k.sys_unlink_one("/mnt", &ctx, false).unwrap();

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

    // ── Logical cache split ───────────────────────────────────────────
    mod logical_cache_split {
        use super::*;
        use crate::cache::index_cache::{IndexCacheKey, IndexKind};
        use crate::meta_store::{LocalMetaStore, MetaStore};
        use std::time::Duration;

        #[test]
        fn sys_mkdir_invalidates_parent_listing_index_cache() {
            let k = Kernel::new();
            let _td = tempfile::tempdir().unwrap();
            let ms: Arc<dyn MetaStore> =
                Arc::new(LocalMetaStore::open(&_td.path().join("meta.redb")).unwrap());
            k.add_mount("/data", "root", None, Some(ms), None, false)
                .unwrap();

            let key = IndexCacheKey::new("root", "/data", IndexKind::Listing);
            k.index_cache.put_listing(
                key,
                vec![("/data/stale.txt".to_string(), DT_REG)],
                Duration::from_secs(60),
            );
            assert_eq!(
                k.readdir("/data", "root", false),
                vec![("/data/stale.txt".to_string(), DT_REG)]
            );

            let ctx = OperationContext::new("test", "root", true, None, true);
            k.sys_mkdir("/data/fresh", &ctx, false, false).unwrap();

            let entries = k.readdir("/data", "root", false);
            assert!(entries.contains(&("/data/fresh".to_string(), DT_DIR)));
            assert!(!entries.contains(&("/data/stale.txt".to_string(), DT_REG)));
        }
    }

    // ── DT_LINK transparent follow ───────────────────────────────────
    //
    // Resolution lives in `Kernel::dt_link_target` and runs AFTER
    // dcache + metastore populate inside sys_read / sys_write /
    // sys_copy. The pre-fix implementation called `dcache.resolve_link`
    // BEFORE routing, which silently fell through (returned the input
    // path unchanged) on a cold dcache, masking both link follow and
    // chain rejection. Tests below force the cold-cache path by
    // creating links via sys_setattr (writes through to metastore),
    // then evicting dcache before the syscall under test.

    mod dt_link {
        use super::*;
        use crate::meta_store::DT_LINK as DT_LINK_TYPE;
        use crate::meta_store::{FileMetadata, LocalMetaStore};
        use std::sync::Arc;

        fn link_entry(path: &str, target: &str) -> FileMetadata {
            FileMetadata {
                path: path.to_string(),
                size: 0,
                content_id: None,
                gen: 0,
                version: 1,
                entry_type: DT_LINK_TYPE,
                zone_id: Some("root".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: None,
                link_target: Some(target.to_string()),
                owner_id: None,
            }
        }

        fn reg_entry(path: &str) -> FileMetadata {
            FileMetadata {
                path: path.to_string(),
                size: 0,
                content_id: None,
                gen: 0,
                version: 1,
                entry_type: 0, // DT_REG
                zone_id: Some("root".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: None,
                link_target: None,
                owner_id: None,
            }
        }

        #[test]
        fn dt_link_target_passthrough_for_non_link() {
            let e = reg_entry("/x");
            assert_eq!(Kernel::dt_link_target("/x", &e).unwrap(), None);
        }

        #[test]
        fn dt_link_target_returns_target_for_link() {
            let e = link_entry("/proc/p1/agent", "/agents/scode-standard");
            assert_eq!(
                Kernel::dt_link_target("/proc/p1/agent", &e).unwrap(),
                Some("/agents/scode-standard"),
            );
        }

        #[test]
        fn dt_link_target_self_loop_rejected() {
            let e = link_entry("/loop", "/loop");
            let err = Kernel::dt_link_target("/loop", &e).unwrap_err();
            match err {
                KernelError::PermissionDenied(msg) => assert!(msg.contains("self-loop")),
                other => panic!("expected PermissionDenied, got {other:?}"),
            }
        }

        #[test]
        fn dt_link_target_missing_target_rejected() {
            let mut e = link_entry("/broken", "/x");
            e.link_target = None;
            let err = Kernel::dt_link_target("/broken", &e).unwrap_err();
            match err {
                KernelError::PermissionDenied(msg) => assert!(msg.contains("no link_target")),
                other => panic!("expected PermissionDenied, got {other:?}"),
            }
        }

        /// Chained-link rejection: even when resolution must consult
        /// the metastore directly (no kernel-side cache hot path),
        /// chained DT_LINK entries reject at the second hop. The
        /// per-mount metastore here is a fresh ``LocalMetaStore``
        /// against a tempfile redb — every lookup hits the underlying
        /// store, exercising the same path a cold cache hit would.
        #[test]
        fn sys_read_rejects_chained_link_through_metastore_only() {
            let k = Kernel::new();
            let _td = tempfile::tempdir().unwrap();
            let ms: Arc<dyn crate::meta_store::MetaStore> =
                Arc::new(LocalMetaStore::open(&_td.path().join("meta.redb")).unwrap());
            k.add_mount("/data", "root", None, Some(ms), None, false)
                .unwrap();

            // /data/a -> /data/b -> /data/c (chain).
            for (path, target) in &[("/data/a", "/data/b"), ("/data/b", "/data/c")] {
                k.sys_setattr(
                    path,
                    6, // DT_LINK
                    "",
                    None,
                    None,
                    None,
                    "memory",
                    "root",
                    false,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    Some(target),
                    None,
                    None, // remote_metastore
                )
                .unwrap();
            }

            let ctx = OperationContext::new("test", "root", true, None, true);
            match k.sys_read_one("/data/a", &ctx, 5000, 0) {
                Err(KernelError::PermissionDenied(msg)) => {
                    assert!(msg.contains("chain rejected"), "unexpected msg: {msg}");
                }
                Err(other) => panic!("expected PermissionDenied(chain rejected), got {other:?}"),
                Ok(_) => panic!("expected PermissionDenied, got Ok (chain silently followed)"),
            }
        }

        /// sys_write follows DT_LINK the same way as sys_read (so
        /// writes hit the target, not a phantom file at the link path).
        /// Chain rejection at the second hop reuses the same code path.
        #[test]
        fn sys_write_rejects_chained_link_through_metastore_only() {
            let k = Kernel::new();
            let _td = tempfile::tempdir().unwrap();
            let ms: Arc<dyn crate::meta_store::MetaStore> =
                Arc::new(LocalMetaStore::open(&_td.path().join("meta.redb")).unwrap());
            k.add_mount("/data", "root", None, Some(ms), None, false)
                .unwrap();

            for (path, target) in &[("/data/a", "/data/b"), ("/data/b", "/data/c")] {
                k.sys_setattr(
                    path,
                    6,
                    "",
                    None,
                    None,
                    None,
                    "memory",
                    "root",
                    false,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    Some(target),
                    None,
                    None, // remote_metastore
                )
                .unwrap();
            }

            let ctx = OperationContext::new("test", "root", true, None, true);
            match k.sys_write_one("/data/a", &ctx, b"payload", 0) {
                Err(KernelError::PermissionDenied(msg)) => {
                    assert!(msg.contains("chain rejected"), "unexpected msg: {msg}");
                }
                Err(other) => panic!("expected PermissionDenied(chain rejected), got {other:?}"),
                Ok(_) => panic!("expected PermissionDenied, got Ok (chain silently followed)"),
            }
        }
    }

    // ── Federation `io_profile=wal` selection (PR-A follow-up) ───────────
    //
    // The wal-backed DT_STREAM path in `setattr_stream` calls
    // `distributed_coordinator().metastore_for_zone(self, "root")` to
    // get the federation-state-machine-backed `Arc<dyn MetaStore>`,
    // then composes a `WalStreamCore` over it. Until this commit,
    // no test exercised that path — every existing
    // `register_proc_entry` test runs against a `Kernel::new()` whose
    // default `NoopDistributedCoordinator` reports
    // `is_initialized() = false`, so `chat_stream_profile` always
    // picks `"memory"`.
    //
    // These tests install a minimal `TestFederationCoordinator` whose
    // `is_initialized` returns `true` and whose `metastore_for_zone`
    // returns an in-memory `MemoryMetaStore`. With that wired:
    //
    //   1. `Kernel::is_federation_initialized()` returns `true` (so
    //      service-tier `chat_stream_profile()` picks `"wal"`).
    //   2. `kernel.sys_setattr(path, DT_STREAM, …, "wal", "root", …)`
    //      composes a `WalStreamCore` over the test metastore + writes
    //      the inode + registers the stream — same code path
    //      production runs through.
    //   3. `kernel.sys_write_one(path, …)` and `kernel.sys_read_one(path, …)`
    //      round-trip bytes through the wal stream, validating the
    //      stream is actually wal-backed (memory streams use a
    //      different backend type, so a memory-vs-wal mistake would
    //      surface here).
    mod federation_wal_e2e {
        use super::*;
        use crate::abc::meta_store::MetaStore;
        use crate::hal::distributed_coordinator::{
            ClusterInfo, CoordinatorResult, DistributedCoordinator, ShareInfo,
        };
        use crate::meta_store::LocalMetaStore;
        use tempfile::TempDir;

        /// Minimal `DistributedCoordinator` impl that reports
        /// `is_initialized=true` and hands back a tempdir-backed
        /// `LocalMetaStore` from `metastore_for_zone`. Every other
        /// trait method is a stub — the wal-stream path under test
        /// never calls them. `TempDir` is held on the coordinator
        /// so the redb file lives as long as the metastore.
        struct TestFederationCoordinator {
            metastore: Arc<dyn MetaStore>,
            _tempdir: TempDir,
        }

        impl TestFederationCoordinator {
            fn new() -> Self {
                let tempdir = TempDir::new().expect("tempdir for fed-wal test");
                let path = tempdir.path().join("fed-wal-metastore.redb");
                let metastore: Arc<dyn MetaStore> =
                    Arc::new(LocalMetaStore::open(&path).expect("open LocalMetaStore"));
                Self {
                    metastore,
                    _tempdir: tempdir,
                }
            }
        }

        impl DistributedCoordinator for TestFederationCoordinator {
            fn list_zones(&self, _kernel: &Kernel) -> Vec<String> {
                vec!["root".to_string()]
            }

            fn is_initialized(&self, _kernel: &Kernel) -> bool {
                true
            }

            fn cluster_info(&self, _: &Kernel, _: &str) -> CoordinatorResult<ClusterInfo> {
                Err("test coordinator: cluster_info unused".into())
            }

            fn create_zone(&self, _: &Kernel, _: &str) -> CoordinatorResult<()> {
                Err("test coordinator: create_zone unused".into())
            }

            fn remove_zone(&self, _: &Kernel, _: &str, _: bool) -> CoordinatorResult<()> {
                Err("test coordinator: remove_zone unused".into())
            }

            fn join_zone(&self, _: &Kernel, _: &str, _: bool) -> CoordinatorResult<()> {
                Err("test coordinator: join_zone unused".into())
            }

            fn wire_mount(&self, _: &Kernel, _: &str, _: &str, _: &str) -> CoordinatorResult<()> {
                Err("test coordinator: wire_mount unused".into())
            }

            fn unwire_mount(&self, _: &Kernel, _: &str, _: &str) -> CoordinatorResult<()> {
                Err("test coordinator: unwire_mount unused".into())
            }

            fn share_zone(&self, _: &Kernel, _: &str, _: &str) -> CoordinatorResult<ShareInfo> {
                Err("test coordinator: share_zone unused".into())
            }

            fn lookup_share(&self, _: &Kernel, _: &str) -> CoordinatorResult<Option<ShareInfo>> {
                Ok(None)
            }

            fn metastore_for_zone(
                &self,
                _: &Kernel,
                _: &str,
            ) -> CoordinatorResult<Arc<dyn MetaStore>> {
                Ok(Arc::clone(&self.metastore))
            }

            fn locks_for_zone(
                &self,
                _: &Kernel,
                _: &str,
            ) -> CoordinatorResult<Arc<dyn contracts::lock_state::Locks>> {
                Err("test coordinator: locks_for_zone unused".into())
            }
        }

        fn fresh_federated_kernel() -> Arc<Kernel> {
            let kernel = Arc::new(Kernel::new());
            kernel.set_distributed_coordinator(
                Arc::new(TestFederationCoordinator::new()) as Arc<dyn DistributedCoordinator>
            );
            // Mount /proc so sys_stat / sys_read / sys_write can
            // route to /proc/{pid}/chat-with-me. Production
            // services::managed_agent::install_returning does the
            // same; the e2e test mirrors that fixture so the wal
            // stream the test writes to is reachable by readers.
            kernel
                .vfs_router
                .add_mount("/proc", contracts::ROOT_ZONE_ID, None, false);
            kernel
        }

        #[test]
        fn is_federation_initialized_reports_true_with_test_coordinator() {
            // Probe the readiness signal `chat_stream_profile()` keys
            // off. `Kernel::new()` alone reports `false` (Noop
            // coordinator); installing the test coordinator flips it
            // to `true`, which is what services::managed_agent::
            // proc_entry::register_proc_entry checks before passing
            // io_profile="wal" to sys_setattr.
            use crate::abi::KernelAbi;
            let bare = Kernel::new();
            assert!(
                !KernelAbi::is_federation_initialized(&bare),
                "bare Kernel::new() must not advertise federation",
            );
            let kernel = fresh_federated_kernel();
            assert!(
                KernelAbi::is_federation_initialized(kernel.as_ref()),
                "kernel with test coordinator installed must advertise federation",
            );
        }

        #[test]
        fn sys_setattr_wal_stream_creates_inode_and_round_trips() {
            // End-to-end: sys_setattr DT_STREAM io_profile="wal" goes
            // through the wal branch of `setattr_stream`, composes a
            // `WalStreamCore` over the test coordinator's metastore,
            // and registers the stream so subsequent sys_write +
            // sys_read round-trip bytes through it.
            //
            // This is the path service-tier callers (
            // managed_agent::proc_entry::register_proc_entry,
            // matrix_adapter::rooms::create_chat_stream) take when
            // `is_federation_initialized()` returns true. A
            // memory-vs-wal mistake (e.g. service code accidentally
            // hardcoding "memory" or kernel taking the wrong branch
            // of setattr_stream) would surface here as a missing
            // metastore wire-up or wrong stream-backend type.
            let kernel = fresh_federated_kernel();
            let path = "/proc/p-fed/chat-with-me";

            kernel
                .sys_setattr(
                    path,
                    DT_STREAM as i32,
                    /* backend_name */ "",
                    /* backend */ None,
                    /* metastore */ None,
                    /* raft_backend */ None,
                    /* io_profile */ "wal",
                    /* zone_id */ "root",
                    /* is_external */ false,
                    /* capacity */ 65_536,
                    /* read_fd */ None,
                    /* write_fd */ None,
                    /* mime_type */ None,
                    /* modified_at_ms */ None,
                    /* content_id */ None,
                    /* size */ None,
                    /* version */ None,
                    /* created_at_ms */ None,
                    /* link_target */ None,
                    /* source */ None,
                    /* remote_metastore */ None,
                )
                .expect("sys_setattr DT_STREAM io_profile=wal");

            // Stream entry was written to the test coordinator's
            // metastore via `write_stream_inode` — sys_stat sees it.
            let stat = kernel
                .sys_stat(path, "root")
                .expect("sys_stat after sys_setattr DT_STREAM");
            assert_eq!(stat.entry_type, DT_STREAM, "entry must be DT_STREAM");

            // Round-trip a write + read — the stream_manager has the
            // wal backend registered and the bytes flow through it.
            let ctx = OperationContext::new("test", "root", true, None, true);
            kernel
                .sys_write_one(path, &ctx, b"federation hello", 0)
                .expect("sys_write to wal stream");
            let read = kernel
                .sys_read_one(path, &ctx, /* timeout_ms */ 0, 0)
                .expect("sys_read from wal stream");
            let bytes = read
                .data
                .expect("wal stream returns the just-written bytes");
            assert_eq!(bytes.as_slice(), b"federation hello");
        }

        #[test]
        fn sys_setattr_wal_stream_idempotent_reopen() {
            // Repeat sys_setattr on the same path is a no-op reopen
            // — the wal stream stays registered + bytes from earlier
            // writes survive the second setattr call. Mirrors the
            // production restart flow where register_proc_entry runs
            // again against an existing pid (our spawn_task tests
            // exercise this on the memory branch; this test covers
            // the wal branch).
            let kernel = fresh_federated_kernel();
            let path = "/proc/p-fed-2/chat-with-me";
            let ctx = OperationContext::new("test", "root", true, None, true);

            for _ in 0..2 {
                kernel
                    .sys_setattr(
                        path,
                        DT_STREAM as i32,
                        "",
                        None,
                        None,
                        None,
                        "wal",
                        "root",
                        false,
                        65_536,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                    .expect("idempotent wal sys_setattr");
            }
            kernel
                .sys_write_one(path, &ctx, b"survives reopen", 0)
                .expect("write to reopened wal stream");
            let read = kernel
                .sys_read_one(path, &ctx, 0, 0)
                .expect("read after idempotent reopen");
            assert_eq!(
                read.data.unwrap().as_slice(),
                b"survives reopen",
                "wal stream contents must survive a no-op reopen",
            );
        }
    }
}
