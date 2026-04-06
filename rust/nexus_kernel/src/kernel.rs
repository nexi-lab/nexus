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

use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_PIPE, DT_REG, DT_STREAM};
use crate::dispatch::{MutationObserver, Trie};
use crate::file_watch::FileWatchRegistry;
use crate::lock::{LockMode, VFSLockManagerInner};
use crate::metastore::RedbMetastore;
use crate::router::{canonicalize, PathRouter, RouteError, RustRouteResult};
use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

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
pub struct SysReadResult {
    /// True if Rust kernel handled the read (no Python fallback needed).
    pub hit: bool,
    /// Content bytes (only when hit=true). Vec<u8> — wrapper converts to PyBytes.
    pub data: Option<Vec<u8>>,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Content hash (etag) for post-hook context.
    pub content_hash: Option<String>,
    /// DT_PIPE(3)/DT_STREAM(4) when hit=false — tells wrapper to dispatch IPC.
    /// 0 = normal miss (not found or no backend).
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
}

/// Result of sys_unlink(): hit + metadata for event payload.
pub struct SysUnlinkResult {
    /// True if Rust completed the full operation (metastore + backend + dcache).
    /// False for DT_MOUNT/DT_PIPE/DT_STREAM or when Python fallback needed.
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
}

// ── KernelObserverRegistry — pure Rust observer dispatch ────────────────

/// Observer entry — pure Rust, no PyO3 dependency.
///
/// Stores `Box<dyn MutationObserver>` (either Rust-native or PyMutationObserverAdapter).
/// The event_mask bitmask matching happens without GIL.
struct KernelObserverEntry {
    observer: Box<dyn MutationObserver>,
    name: String,
    event_mask: u32,
    is_inline: bool,
}

/// Pure Rust observer registry — event-type bitmask filtering without GIL.
///
/// Moved from PyKernel (Phase 10). PyMutationObserverAdapter implements
/// MutationObserver, so Python observers work transparently.
/// Future: Rust-native observers (audit logger, search indexer) get zero-crossing dispatch.
struct KernelObserverRegistry {
    observers: Vec<KernelObserverEntry>,
}

impl KernelObserverRegistry {
    fn new() -> Self {
        Self {
            observers: Vec::new(),
        }
    }

    /// Register observer with event_mask + is_inline flag.
    fn register(
        &mut self,
        observer: Box<dyn MutationObserver>,
        name: String,
        event_mask: u32,
        is_inline: bool,
    ) {
        self.observers.push(KernelObserverEntry {
            observer,
            name,
            event_mask,
            is_inline,
        });
    }

    /// Unregister by name (identity not available for trait objects).
    fn unregister(&mut self, name: &str) -> bool {
        if let Some(pos) = self.observers.iter().position(|e| e.name == name) {
            self.observers.remove(pos);
            return true;
        }
        false
    }

    /// Dispatch matching observers inline (bitmask filter + on_mutation call).
    /// No GIL needed for the filter loop. PyMutationObserverAdapter acquires GIL
    /// only inside on_mutation().
    fn dispatch_inline(&self, event_type: u32, path: &str) {
        for entry in &self.observers {
            if entry.is_inline && entry.event_mask & event_type != 0 {
                entry.observer.on_mutation(event_type, path);
            }
        }
    }

    /// Return indices of deferred (non-inline) observers matching event_type.
    /// Caller (PyKernel) uses indices to get Py<PyAny> refs for asyncio.create_task().
    fn get_deferred_indices(&self, event_type: u32) -> Vec<usize> {
        self.observers
            .iter()
            .enumerate()
            .filter(|(_, e)| !e.is_inline && e.event_mask & event_type != 0)
            .map(|(i, _)| i)
            .collect()
    }

    fn count(&self) -> usize {
        self.observers.len()
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
///   - `set_vfs_lock(lock)` — share VFS lock.
///   - `add_mount(...)` — register mount points.
///   - `dcache_put(...)` — populate dentry cache.
///   - `trie_register(...)` — register path resolvers.
pub struct Kernel {
    // DCache (owned)
    dcache: DCache,
    // Router (owned)
    router: PathRouter,
    // PathTrie (owned)
    trie: Trie,
    // VFS Lock (Arc-shared with VFSLockManager for blocking acquire)
    vfs_lock: Option<Arc<VFSLockManagerInner>>,
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
    // Observer registry (owned by kernel — bitmask matching without GIL)
    observers: Mutex<KernelObserverRegistry>,
    // Zone revision counter — AtomicU64 per zone + Condvar for waiters (§10 A2)
    zone_revisions: DashMap<String, Arc<ZoneRevisionEntry>>,
    // File watch registry — Rust-native pattern matching (§10 A3)
    file_watches: FileWatchRegistry,
    // Agent registry — DashMap backing store (§10 B1)
    pub(crate) agent_registry: crate::agent_registry::AgentRegistry,
    // Per-mount metastores — federation zones have independent redb instances.
    // Keyed by zone-canonical mount point (e.g. "/zone-beta/shared").
    // Syscalls check here first, then fall back to self.metastore (global).
    mount_metastores: DashMap<String, Box<dyn crate::metastore::Metastore>>,
    // IPC registry — DT_PIPE buffers (lock-free SPSC ring)
    pipe_buffers: DashMap<String, Arc<crate::pipe::RingBufferCore>>,
    // IPC registry — DT_STREAM buffers (append-only linear)
    stream_buffers: DashMap<String, Arc<crate::stream::StreamBufferCore>>,
}

impl Kernel {
    // ── Constructor ────────────────────────────────────────────────────

    /// Create an empty kernel. Components wired by wrapper after construction.
    pub fn new() -> Self {
        Self {
            dcache: DCache::new(),
            router: PathRouter::new(),
            trie: Trie::new(),
            vfs_lock: None,
            metastore: None,
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
            file_watches: FileWatchRegistry::new(),
            agent_registry: crate::agent_registry::AgentRegistry::new(),
            mount_metastores: DashMap::new(),
            pipe_buffers: DashMap::new(),
            stream_buffers: DashMap::new(),
        }
    }

    // ── VFS Lock wiring ────────────────────────────────────────────────

    /// Wire VFS lock manager (shares Arc with VFSLockManager).
    pub fn set_vfs_lock(&mut self, inner: Arc<VFSLockManagerInner>) {
        self.vfs_lock = Some(inner);
    }

    /// Set VFS lock timeout in milliseconds (default 5000).
    pub fn set_vfs_lock_timeout(&mut self, timeout_ms: u64) {
        self.vfs_lock_timeout_ms = timeout_ms;
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

    /// Resolve metastore for a syscall: per-mount first, then global fallback.
    ///
    /// In federation mode each mount has its own redb instance (Raft-backed zone store).
    /// Standalone mode uses a single global metastore.
    /// `mount_point` must be the zone-canonical key from route_impl().
    fn with_metastore<F, R>(&self, mount_point: &str, f: F) -> Option<R>
    where
        F: FnOnce(&dyn crate::metastore::Metastore) -> R,
    {
        if let Some(ms) = self.mount_metastores.get(mount_point) {
            return Some(f(ms.as_ref()));
        }
        self.metastore.as_ref().map(|ms| f(ms.as_ref()))
    }

    // ── Metastore proxy methods (for Python RustMetastoreProxy) ────────

    pub fn metastore_get(
        &self,
        path: &str,
    ) -> Result<Option<crate::metastore::FileMetadata>, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .get(path)
                .map_err(|e| KernelError::IOError(format!("metastore_get({path}): {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_put(
        &self,
        path: &str,
        metadata: crate::metastore::FileMetadata,
    ) -> Result<(), KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .put(path, metadata)
                .map_err(|e| KernelError::IOError(format!("metastore_put({path}): {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_delete(&self, path: &str) -> Result<bool, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .delete(path)
                .map_err(|e| KernelError::IOError(format!("metastore_delete({path}): {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_list(
        &self,
        prefix: &str,
    ) -> Result<Vec<crate::metastore::FileMetadata>, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .list(prefix)
                .map_err(|e| KernelError::IOError(format!("metastore_list({prefix}): {e:?}"))),
            None => Err(KernelError::IOError("no metastore wired".into())),
        }
    }

    pub fn metastore_exists(&self, path: &str) -> Result<bool, KernelError> {
        match &self.metastore {
            Some(ms) => ms
                .exists(path)
                .map_err(|e| KernelError::IOError(format!("metastore_exists({path}): {e:?}"))),
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
            },
        );
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
    /// When `metastore_path` is provided, opens a Rust-native RedbMetastore
    /// for this mount point (federation: each zone has its own redb instance).
    /// Syscalls will use this per-mount metastore instead of the global one.
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
        metastore_path: Option<&str>,
    ) -> Result<(), KernelError> {
        // Open per-mount metastore if path provided (federation mode)
        if let Some(ms_path) = metastore_path {
            let ms = RedbMetastore::open(std::path::Path::new(ms_path))
                .map_err(|e| KernelError::IOError(format!("RedbMetastore: {e:?}")))?;
            let canonical = canonicalize(mount_point, zone_id);
            self.mount_metastores.insert(canonical, Box::new(ms));
        }
        self.router
            .add_mount(
                mount_point,
                zone_id,
                readonly,
                admin_only,
                io_profile,
                backend_name,
                backend,
            )
            .map_err(KernelError::from)
    }

    /// Remove a mount point (and its per-mount metastore if any).
    pub fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);
        self.mount_metastores.remove(&canonical);
        self.router.remove_mount(mount_point, zone_id)
    }

    /// Zone-canonical LPM routing.
    pub fn route(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> Result<RustRouteResult, KernelError> {
        self.router
            .route_impl(path, zone_id, is_admin, check_write)
            .map_err(KernelError::from)
    }

    /// Check if a mount exists.
    pub fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.router.has_mount(mount_point, zone_id)
    }

    /// List all mount points.
    pub fn get_mount_points(&self) -> Vec<String> {
        self.router.get_mount_points()
    }

    /// High-level mount: add_mount + create DT_MOUNT metastore entry.
    ///
    /// Encapsulates routing table update + metadata persistence in one call.
    /// DLC calls this, then does Python-side hook registration.
    #[allow(clippy::too_many_arguments, dead_code)]
    pub fn kernel_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
        backend_name: &str,
        backend: Option<Box<dyn crate::backend::ObjectStore>>,
        metastore_path: Option<&str>,
    ) -> Result<(), KernelError> {
        // 1. Router + per-mount metastore
        self.add_mount(
            mount_point,
            zone_id,
            readonly,
            admin_only,
            io_profile,
            backend_name,
            backend,
            metastore_path,
        )?;

        // 2. Create DT_MOUNT metadata entry (best-effort)
        let canonical = canonicalize(mount_point, zone_id);
        self.with_metastore(&canonical, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: mount_point.to_string(),
                backend_name: backend_name.to_string(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
            };
            let _ = ms.put(mount_point, meta);
        });

        // 3. DCache entry for mount point
        self.dcache.put(
            mount_point,
            CachedEntry {
                backend_name: backend_name.to_string(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some(zone_id.to_string()),
                mime_type: None,
            },
        );

        Ok(())
    }

    /// High-level unmount: remove_mount + cleanup metastore entry.
    pub fn kernel_unmount(&self, mount_point: &str, zone_id: &str) -> Result<bool, KernelError> {
        // 1. Cleanup metastore entry (best-effort)
        let canonical = canonicalize(mount_point, zone_id);
        self.with_metastore(&canonical, |ms| {
            let _ = ms.delete(mount_point);
        });

        // 2. DCache evict
        self.dcache.evict(mount_point);

        // 3. Remove from router + per-mount metastore
        Ok(self.remove_mount(mount_point, zone_id))
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

    // ── Observer registry (Phase 10) ────────────────────────────────────

    /// Register a mutation observer (pure Rust or PyMutationObserverAdapter).
    pub fn register_observer(
        &self,
        observer: Box<dyn MutationObserver>,
        name: String,
        event_mask: u32,
        is_inline: bool,
    ) {
        self.observers
            .lock()
            .register(observer, name, event_mask, is_inline);
    }

    /// Unregister observer by name.
    pub fn unregister_observer(&self, name: &str) -> bool {
        self.observers.lock().unregister(name)
    }

    /// Dispatch inline observers matching event_type (no GIL for filter loop).
    pub fn dispatch_observers_inline(&self, event_type: u32, path: &str) {
        self.observers.lock().dispatch_inline(event_type, path);
    }

    /// Get indices of deferred observers matching event_type.
    pub fn get_deferred_observer_indices(&self, event_type: u32) -> Vec<usize> {
        self.observers.lock().get_deferred_indices(event_type)
    }

    /// Number of registered observers.
    pub fn observer_count(&self) -> usize {
        self.observers.lock().count()
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

    // ── IPC Registry — Pipe methods ─────────────────────────────────────

    /// Create a pipe buffer in the IPC registry.
    pub fn create_pipe(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        if self.pipe_buffers.contains_key(path) {
            return Err(KernelError::PipeExists(path.to_string()));
        }
        let buf = crate::pipe::RingBufferCore::new_inner(capacity);
        self.pipe_buffers.insert(path.to_string(), Arc::new(buf));
        Ok(())
    }

    /// Destroy a pipe buffer.
    pub fn destroy_pipe(&self, path: &str) -> Result<(), KernelError> {
        match self.pipe_buffers.remove(path) {
            Some((_, buf)) => {
                buf.close_inner();
                Ok(())
            }
            None => Err(KernelError::PipeNotFound(path.to_string())),
        }
    }

    /// Close a pipe (signal close, keep in registry for drain).
    pub fn close_pipe(&self, path: &str) -> Result<(), KernelError> {
        match self.pipe_buffers.get(path) {
            Some(buf) => {
                buf.close_inner();
                Ok(())
            }
            None => Err(KernelError::PipeNotFound(path.to_string())),
        }
    }

    /// Check if a pipe exists.
    pub fn has_pipe(&self, path: &str) -> bool {
        self.pipe_buffers.contains_key(path)
    }

    /// Non-blocking write to a pipe. Returns bytes written.
    pub fn pipe_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        let buf = self
            .pipe_buffers
            .get(path)
            .ok_or_else(|| KernelError::PipeNotFound(path.to_string()))?;
        buf.push_inner(data).map_err(|e| match e {
            crate::pipe::RingError::Full(u, c) => {
                KernelError::PipeFull(format!("{u}/{c} bytes used"))
            }
            crate::pipe::RingError::Closed(msg) => KernelError::PipeClosed(msg.to_string()),
            crate::pipe::RingError::Oversized(s, c) => {
                KernelError::PipeFull(format!("msg {s} > capacity {c}"))
            }
            _ => KernelError::IOError(format!("pipe write: {e:?}")),
        })
    }

    /// Non-blocking read from a pipe. Returns data or WouldBlock if empty.
    pub fn pipe_read_nowait(&self, path: &str) -> Result<Option<Vec<u8>>, KernelError> {
        let buf = self
            .pipe_buffers
            .get(path)
            .ok_or_else(|| KernelError::PipeNotFound(path.to_string()))?;
        match buf.pop_inner() {
            Ok(data) => Ok(Some(data)),
            Err(crate::pipe::RingError::Empty) => Ok(None),
            Err(crate::pipe::RingError::ClosedEmpty) => {
                Err(KernelError::PipeClosed("closed and empty".to_string()))
            }
            Err(e) => Err(KernelError::IOError(format!("pipe read: {e:?}"))),
        }
    }

    /// List all pipes with their paths.
    pub fn list_pipes(&self) -> Vec<String> {
        self.pipe_buffers.iter().map(|r| r.key().clone()).collect()
    }

    /// Close all pipes (shutdown).
    pub fn close_all_pipes(&self) {
        for entry in self.pipe_buffers.iter() {
            entry.value().close_inner();
        }
    }

    // ── IPC Registry — Stream methods ─────────────────────────────────

    /// Create a stream buffer in the IPC registry.
    pub fn create_stream(&self, path: &str, capacity: usize) -> Result<(), KernelError> {
        if self.stream_buffers.contains_key(path) {
            return Err(KernelError::StreamExists(path.to_string()));
        }
        let buf = crate::stream::StreamBufferCore::new_inner(capacity);
        self.stream_buffers.insert(path.to_string(), Arc::new(buf));
        Ok(())
    }

    /// Destroy a stream buffer.
    pub fn destroy_stream(&self, path: &str) -> Result<(), KernelError> {
        match self.stream_buffers.remove(path) {
            Some((_, buf)) => {
                buf.close_inner();
                Ok(())
            }
            None => Err(KernelError::StreamNotFound(path.to_string())),
        }
    }

    /// Close a stream (signal close, keep in registry for drain).
    pub fn close_stream(&self, path: &str) -> Result<(), KernelError> {
        match self.stream_buffers.get(path) {
            Some(buf) => {
                buf.close_inner();
                Ok(())
            }
            None => Err(KernelError::StreamNotFound(path.to_string())),
        }
    }

    /// Check if a stream exists.
    pub fn has_stream(&self, path: &str) -> bool {
        self.stream_buffers.contains_key(path)
    }

    /// Non-blocking write to a stream. Returns byte offset.
    pub fn stream_write_nowait(&self, path: &str, data: &[u8]) -> Result<usize, KernelError> {
        let buf = self
            .stream_buffers
            .get(path)
            .ok_or_else(|| KernelError::StreamNotFound(path.to_string()))?;
        buf.push_inner(data).map_err(|e| match e {
            crate::stream::StreamError::Full(u, c) => {
                KernelError::StreamFull(format!("{u}/{c} bytes used"))
            }
            crate::stream::StreamError::Closed(msg) => KernelError::StreamClosed(msg.to_string()),
            crate::stream::StreamError::Oversized(s, c) => {
                KernelError::StreamFull(format!("msg {s} > capacity {c}"))
            }
            _ => KernelError::IOError(format!("stream write: {e:?}")),
        })
    }

    /// Read one message at byte offset. Returns (data, next_offset) or None if empty.
    pub fn stream_read_at(
        &self,
        path: &str,
        offset: usize,
    ) -> Result<Option<(Vec<u8>, usize)>, KernelError> {
        let buf = self
            .stream_buffers
            .get(path)
            .ok_or_else(|| KernelError::StreamNotFound(path.to_string()))?;
        match buf.read_at_raw(offset) {
            Ok((data, next)) => Ok(Some((data, next))),
            Err(crate::stream::StreamError::Empty) => Ok(None),
            Err(crate::stream::StreamError::ClosedEmpty) => {
                Err(KernelError::StreamClosed("closed and empty".to_string()))
            }
            Err(e) => Err(KernelError::IOError(format!("stream read: {e:?}"))),
        }
    }

    /// Read up to `count` messages starting from byte offset.
    pub fn stream_read_batch(
        &self,
        path: &str,
        offset: usize,
        count: usize,
    ) -> Result<(Vec<Vec<u8>>, usize), KernelError> {
        let buf = self
            .stream_buffers
            .get(path)
            .ok_or_else(|| KernelError::StreamNotFound(path.to_string()))?;
        buf.read_batch_raw(offset, count)
            .map_err(|e| KernelError::IOError(format!("stream batch: {e:?}")))
    }

    /// List all streams with their paths.
    pub fn list_streams(&self) -> Vec<String> {
        self.stream_buffers
            .iter()
            .map(|r| r.key().clone())
            .collect()
    }

    /// Close all streams (shutdown).
    pub fn close_all_streams(&self) {
        for entry in self.stream_buffers.iter() {
            entry.value().close_inner();
        }
    }

    // ── sys_read ───────────────────────────────────────────────────────

    /// Rust syscall: read file content (pure Rust, no GIL).
    ///
    /// validate -> route -> dcache -> [metastore fallback] -> VFS lock -> CAS read -> return.
    ///
    /// DCache hit = hot path. DCache miss = cold path: queries metastore, populates dcache,
    /// then continues with CAS read.
    ///
    /// Returns `hit=false` for DT_PIPE/DT_STREAM (wrapper handles async IPC)
    /// or when no Rust backend is available (e.g. remote backends).
    ///
    /// Hooks are NOT dispatched here — wrapper handles PRE-INTERCEPT.
    pub fn sys_read(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let miss = || {
            Ok(SysReadResult {
                hit: false,
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 2. Route (pure Rust LPM)
        let route = match self
            .router
            .route_impl(path, &ctx.zone_id, ctx.is_admin, false)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache lookup — on miss, fallback to metastore (cold path)
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                // Metastore fallback (per-mount first, then global)
                match self.with_metastore(&route.mount_point, |ms| ms.get(path)) {
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
                        };
                        self.dcache.put(path, cached);
                        // Re-fetch from dcache (now populated)
                        self.dcache.get_entry(path).unwrap()
                    }
                    // Metastore miss — may be overlay base layer; let wrapper handle
                    Some(Ok(None)) => return miss(),
                    Some(Err(_)) => return miss(),
                    None => return Err(KernelError::FileNotFound(path.to_string())),
                }
            }
        };

        // DT_PIPE — try Rust IPC registry (nowait pop)
        if entry.entry_type == DT_PIPE {
            if let Some(buf) = self.pipe_buffers.get(path) {
                match buf.pop_inner() {
                    Ok(data) => {
                        return Ok(SysReadResult {
                            hit: true,
                            data: Some(data),
                            post_hook_needed: false,
                            content_hash: None,
                            entry_type: DT_PIPE,
                        });
                    }
                    Err(crate::pipe::RingError::Empty) => {
                        // Empty — return miss with DT_PIPE so Python async shell retries
                        return Ok(SysReadResult {
                            hit: false,
                            data: None,
                            post_hook_needed: false,
                            content_hash: None,
                            entry_type: DT_PIPE,
                        });
                    }
                    Err(crate::pipe::RingError::ClosedEmpty) => {
                        return Err(KernelError::PipeClosed(path.to_string()));
                    }
                    Err(_) => {}
                }
            }
            // Not in Rust registry — fall through to Python fallback
            return Ok(SysReadResult {
                hit: false,
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: DT_PIPE,
            });
        }

        // DT_STREAM — try Rust IPC registry (nowait read_at)
        // Note: stream reads need byte offset, which comes from OperationContext or
        // the wrapper. For now, return miss so Python handles offset tracking.
        if entry.entry_type == DT_STREAM {
            return Ok(SysReadResult {
                hit: false,
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: DT_STREAM,
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
            None => return miss(),
        };

        // 4. VFS lock (blocking acquire — wrapper releases GIL before calling this)
        let lock_handle = if let Some(ref lm) = self.vfs_lock {
            let timeout = self.vfs_lock_timeout_ms;
            lm.blocking_acquire(path, LockMode::Read, timeout)
        } else {
            0
        };

        // Lock timeout -> miss (unsafe to read without lock)
        if self.vfs_lock.is_some() && lock_handle == 0 {
            return miss();
        }

        // 5. Backend read (CasLocal or PyObjectStoreAdapter)
        let content =
            self.router
                .read_content(&route.mount_point, content_id, &route.backend_path, ctx);

        // 6. Release VFS lock (always, even on miss)
        if lock_handle > 0 {
            if let Some(ref lm) = self.vfs_lock {
                lm.do_release(lock_handle);
            }
        }

        // 7. Return result
        match content {
            Some(data) => Ok(SysReadResult {
                hit: true,
                data: Some(data),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_hash: entry.etag,
                entry_type: DT_REG,
            }),
            None => miss(),
        }
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

        // 2. Route (check write access)
        let route = match self
            .router
            .route_impl(path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache check — DT_PIPE/DT_STREAM: try Rust IPC registry
        if let Some(entry) = self.dcache.get_entry(path) {
            if entry.entry_type == DT_PIPE {
                if let Some(buf) = self.pipe_buffers.get(path) {
                    match buf.push_inner(content) {
                        Ok(n) => {
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: false,
                                version: 0,
                                size: n as u64,
                            });
                        }
                        Err(crate::pipe::RingError::Full(_, _)) => {
                            // Full — return miss so Python async shell retries
                            return miss();
                        }
                        Err(crate::pipe::RingError::Closed(msg)) => {
                            return Err(KernelError::PipeClosed(msg.to_string()));
                        }
                        Err(_) => {}
                    }
                }
                return miss();
            }
            if entry.entry_type == DT_STREAM {
                if let Some(buf) = self.stream_buffers.get(path) {
                    match buf.push_inner(content) {
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
        let lock_handle = if let Some(ref lm) = self.vfs_lock {
            let timeout = self.vfs_lock_timeout_ms;
            lm.blocking_acquire(path, LockMode::Write, timeout)
        } else {
            0
        };

        // Lock timeout -> miss (unsafe to write without lock)
        if self.vfs_lock.is_some() && lock_handle == 0 {
            return miss();
        }

        // 5. Backend write (CasLocal or PyObjectStoreAdapter)
        //    Pass backend_path as content_id (CAS ignores it, PAS uses it as blob path).
        let write_result =
            self.router
                .write_content(&route.mount_point, content, &route.backend_path, ctx);

        // 6. After write -> build metadata + metastore.put + dcache update
        let result = match write_result {
            Some(wr) => {
                // Get existing version for increment
                let old_version = self.dcache.get_entry(path).map(|e| e.version).unwrap_or(0);
                let new_version = old_version + 1;

                // Build FileMetadata and persist via metastore (per-mount or global)
                self.with_metastore(&route.mount_point, |ms| {
                    let meta = crate::metastore::FileMetadata {
                        path: path.to_string(),
                        backend_name: route.io_profile.clone(),
                        physical_path: wr.content_id.clone(),
                        size: wr.size,
                        etag: Some(wr.content_id.clone()),
                        version: new_version,
                        entry_type: DT_REG,
                        zone_id: Some(ctx.zone_id.clone()),
                        mime_type: None,
                    };
                    // Best-effort metastore.put -- error logged but doesn't fail write
                    let _ = ms.put(path, meta);
                });

                // Update dcache with new metadata
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
                    },
                );

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
        if lock_handle > 0 {
            if let Some(ref lm) = self.vfs_lock {
                lm.do_release(lock_handle);
            }
        }

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
        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return None;
        }

        // 4. DCache lookup (miss -> wrapper handles via metastore)
        let entry = self.dcache.get_entry(path)?;

        let is_dir = entry.entry_type == DT_DIR;
        let mime = entry
            .mime_type
            .as_deref()
            .unwrap_or(if is_dir {
                "inode/directory"
            } else {
                "application/octet-stream"
            })
            .to_string();

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

        // 2. Route (check write access)
        let route = match self
            .router
            .route_impl(path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(0),
        };

        // 3. Get metadata (dcache or metastore — per-mount first, then global)
        let meta = match self.dcache.get_entry(path) {
            Some(e) => Some(e),
            None => self
                .with_metastore(&route.mount_point, |ms| {
                    ms.get(path).ok().flatten().map(|m| CachedEntry {
                        backend_name: m.backend_name,
                        physical_path: m.physical_path,
                        size: m.size,
                        etag: m.etag,
                        version: m.version,
                        entry_type: m.entry_type,
                        zone_id: m.zone_id,
                        mime_type: m.mime_type,
                    })
                })
                .flatten(),
        };

        let entry = match meta {
            Some(e) => e,
            None => return miss(0),
        };

        // 4. DT_DIR/DT_MOUNT/DT_PIPE/DT_STREAM → Python fallback
        match entry.entry_type {
            DT_DIR | DT_PIPE | DT_STREAM => return miss(entry.entry_type),
            // DT_MOUNT (2) and DT_EXTERNAL_STORAGE (5) → Python handles unmount
            2 | 5 => return miss(entry.entry_type),
            _ => {}
        }

        // 5. VFS write lock
        let lock_handle = if let Some(ref lm) = self.vfs_lock {
            lm.blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms)
        } else {
            0
        };
        if self.vfs_lock.is_some() && lock_handle == 0 {
            return miss(entry.entry_type);
        }

        // 6. Metastore delete (per-mount or global)
        self.with_metastore(&route.mount_point, |ms| {
            let _ = ms.delete(path);
        });

        // 7. Backend delete (best-effort, PAS only)
        let _ = self
            .router
            .delete_file(&route.mount_point, &route.backend_path);

        // 8. DCache evict
        self.dcache.evict(path);

        // 9. Release VFS lock
        if lock_handle > 0 {
            if let Some(ref lm) = self.vfs_lock {
                lm.do_release(lock_handle);
            }
        }

        // 10. Return hit=true with metadata for event payload
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

        // 2. Route both (check write access)
        let old_route = match self
            .router
            .route_impl(old_path, &ctx.zone_id, ctx.is_admin, true)
        {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let new_route = match self
            .router
            .route_impl(new_path, &ctx.zone_id, ctx.is_admin, true)
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

        let lock1 = if let Some(ref lm) = self.vfs_lock {
            lm.blocking_acquire(first, LockMode::Write, self.vfs_lock_timeout_ms)
        } else {
            0
        };
        let lock2 = if first != second {
            if let Some(ref lm) = self.vfs_lock {
                lm.blocking_acquire(second, LockMode::Write, self.vfs_lock_timeout_ms)
            } else {
                0
            }
        } else {
            0
        };

        let release_locks = |lm: &Option<Arc<VFSLockManagerInner>>, h1: u64, h2: u64| {
            if let Some(ref l) = lm {
                if h2 > 0 {
                    l.do_release(h2);
                }
                if h1 > 0 {
                    l.do_release(h1);
                }
            }
        };

        // Lock timeout check
        if self.vfs_lock.is_some() && lock1 == 0 {
            release_locks(&self.vfs_lock, lock1, lock2);
            return miss();
        }

        // 4. Existence check: get old metadata (per-mount or global)
        let old_meta = self
            .with_metastore(&old_route.mount_point, |ms| ms.get(old_path).ok().flatten())
            .flatten();

        // Also check dcache
        let old_entry = self.dcache.get_entry(old_path);

        let (is_directory, entry_type) = match (&old_meta, &old_entry) {
            (Some(m), _) => (m.entry_type == DT_DIR, m.entry_type),
            (None, Some(e)) => (e.entry_type == DT_DIR, e.entry_type),
            (None, None) => {
                // Not found in Rust metastore/dcache — let Python handle under VFS lock
                release_locks(&self.vfs_lock, lock1, lock2);
                return miss();
            }
        };

        // DT_MOUNT/DT_PIPE/DT_STREAM → Python fallback
        match entry_type {
            DT_PIPE | DT_STREAM | 2 | 5 => {
                release_locks(&self.vfs_lock, lock1, lock2);
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
                ms.exists(new_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if new_exists {
            release_locks(&self.vfs_lock, lock1, lock2);
            return miss();
        }

        // 6. Put-then-delete (crash-safe): metastore.put(new) → metastore.delete(old)
        if let Some(ref meta) = old_meta {
            let new_meta = crate::metastore::FileMetadata {
                path: new_path.to_string(),
                backend_name: meta.backend_name.clone(),
                physical_path: meta.physical_path.clone(),
                size: meta.size,
                etag: meta.etag.clone(),
                version: meta.version,
                entry_type: meta.entry_type,
                zone_id: meta.zone_id.clone(),
                mime_type: meta.mime_type.clone(),
            };
            self.with_metastore(&old_route.mount_point, |ms| {
                let _ = ms.put(new_path, new_meta);
                let _ = ms.delete(old_path);
            });
        }

        // 7. Recursive child rename (directories)
        if is_directory {
            self.with_metastore(&old_route.mount_point, |ms| {
                let prefix = format!("{}/", old_path.trim_end_matches('/'));
                if let Ok(children) = ms.list(&prefix) {
                    for child in &children {
                        let child_new_path =
                            format!("{}{}", new_path, &child.path[old_path.len()..]);
                        let child_new_meta = crate::metastore::FileMetadata {
                            path: child_new_path.clone(),
                            backend_name: child.backend_name.clone(),
                            physical_path: child.physical_path.clone(),
                            size: child.size,
                            etag: child.etag.clone(),
                            version: child.version,
                            entry_type: child.entry_type,
                            zone_id: child.zone_id.clone(),
                            mime_type: child.mime_type.clone(),
                        };
                        let _ = ms.put(&child_new_path, child_new_meta);
                        let _ = ms.delete(&child.path);
                    }
                }
            });
        }

        // 8. Backend rename (best-effort, PAS only)
        let _ = self.router.rename_file(
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
        release_locks(&self.vfs_lock, lock1, lock2);

        Ok(SysRenameResult {
            hit: true,
            success: true,
            post_hook_needed: self.rename_hook_count.load(Ordering::Relaxed) > 0,
            is_directory,
        })
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
            .router
            .route_impl(path, &ctx.zone_id, ctx.is_admin, true)?;

        // 3. Existence check via metastore (per-mount or global)
        let exists = self
            .with_metastore(&route.mount_point, |ms| ms.exists(path).unwrap_or(false))
            .unwrap_or(false);
        if exists {
            if !exist_ok && !parents {
                return Err(KernelError::IOError(format!(
                    "Directory already exists: {path}"
                )));
            }
            // Already exists — ensure parents and return
            if parents {
                self.ensure_parent_directories(path, ctx, &route.mount_point)?;
            }
            return Ok(SysMkdirResult {
                hit: true,
                post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
            });
        }

        // 4. Backend mkdir (best-effort, PAS backends create physical dirs)
        let _ = self
            .router
            .mkdir(&route.mount_point, &route.backend_path, parents, true);

        // 5. Ensure parent directories
        if parents {
            self.ensure_parent_directories(path, ctx, &route.mount_point)?;
        }

        // 6. Create directory metadata in metastore (per-mount or global)
        self.with_metastore(&route.mount_point, |ms| {
            let meta = crate::metastore::FileMetadata {
                path: path.to_string(),
                backend_name: route.io_profile.clone(),
                physical_path: String::new(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: DT_DIR,
                zone_id: Some(ctx.zone_id.clone()),
                mime_type: Some("inode/directory".to_string()),
            };
            let _ = ms.put(path, meta);
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
            },
        );

        Ok(SysMkdirResult {
            hit: true,
            post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
        })
    }

    /// Walk up path creating missing parent directory metadata.
    fn ensure_parent_directories(
        &self,
        path: &str,
        ctx: &OperationContext,
        mount_point: &str,
    ) -> Result<(), KernelError> {
        // Walk up from parent to root, collecting missing dirs
        let mut current = path;
        let mut to_create = Vec::new();
        loop {
            match current.rfind('/') {
                Some(0) | None => break,
                Some(pos) => {
                    current = &path[..pos];
                    if current.is_empty() || current == "/" {
                        break;
                    }
                    let exists = self
                        .with_metastore(mount_point, |ms| ms.exists(current).unwrap_or(true))
                        .unwrap_or(true);
                    if !exists {
                        to_create.push(current.to_string());
                    } else {
                        break; // Existing parent found, stop
                    }
                }
            }
        }

        // Create from shallowest to deepest
        for dir_path in to_create.into_iter().rev() {
            self.with_metastore(mount_point, |ms| {
                let meta = crate::metastore::FileMetadata {
                    path: dir_path.clone(),
                    backend_name: String::new(),
                    physical_path: String::new(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_DIR,
                    zone_id: Some(ctx.zone_id.clone()),
                    mime_type: Some("inode/directory".to_string()),
                };
                let _ = ms.put(&dir_path, meta);
            });
            self.dcache.put(
                &dir_path,
                CachedEntry {
                    backend_name: String::new(),
                    physical_path: String::new(),
                    size: 0,
                    etag: None,
                    version: 1,
                    entry_type: DT_DIR,
                    zone_id: Some(ctx.zone_id.clone()),
                    mime_type: Some("inode/directory".to_string()),
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
            .router
            .route_impl(path, &ctx.zone_id, ctx.is_admin, true)?;

        // 3. Get metadata (per-mount or global)
        let entry_type = self
            .with_metastore(&route.mount_point, |ms| {
                ms.get(path)
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

        // 4. Check children (per-mount or global)
        let mut children_deleted = 0;
        if let Some(result) = self.with_metastore(&route.mount_point, |ms| {
            let prefix = format!("{}/", path.trim_end_matches('/'));
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
            .router
            .rmdir(&route.mount_point, &route.backend_path, recursive);

        // 7. Delete directory metadata (per-mount or global)
        self.with_metastore(&route.mount_point, |ms| {
            let _ = ms.delete(path);
        });

        // 8. DCache evict + prefix evict
        self.dcache.evict(path);
        let prefix = format!("{}/", path.trim_end_matches('/'));
        self.dcache.evict_prefix(&prefix);

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
            .router
            .route_impl(path, zone_id, is_admin, false)
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
                .router
                .route_impl(path, &ctx.zone_id, ctx.is_admin, true)
                .ok();
            routes.push(route);
        }

        // 3. Sorted VFS lock acquisition for all paths
        let mut lock_handles: Vec<u64> = vec![0; items.len()];
        if self.vfs_lock.is_some() {
            // Sort indices by path to avoid deadlock
            let mut indices: Vec<usize> = (0..items.len()).collect();
            indices.sort_by(|a, b| items[*a].0.cmp(&items[*b].0));

            for idx in indices {
                if routes[idx].is_some() {
                    if let Some(ref lm) = self.vfs_lock {
                        lock_handles[idx] = lm.blocking_acquire(
                            &items[idx].0,
                            LockMode::Write,
                            self.vfs_lock_timeout_ms,
                        );
                    }
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
            if self.vfs_lock.is_some() && lock_handles[i] == 0 {
                results.push(SysWriteResult {
                    hit: false,
                    content_id: None,
                    post_hook_needed: false,
                    version: 0,
                    size: 0,
                });
                continue;
            }

            // Backend write
            let write_result =
                self.router
                    .write_content(&route.mount_point, content, &route.backend_path, ctx);

            match write_result {
                Some(wr) => {
                    let old_version = self.dcache.get_entry(path).map(|e| e.version).unwrap_or(0);
                    let new_version = old_version + 1;

                    // Collect metadata for batch put (instead of N individual puts)
                    let meta = crate::metastore::FileMetadata {
                        path: path.clone(),
                        backend_name: route.io_profile.clone(),
                        physical_path: wr.content_id.clone(),
                        size: wr.size,
                        etag: Some(wr.content_id.clone()),
                        version: new_version,
                        entry_type: DT_REG,
                        zone_id: Some(ctx.zone_id.clone()),
                        mime_type: None,
                    };
                    batch_meta.push((route.mount_point.clone(), path.clone(), meta));

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
                if self.mount_metastores.get(&mp).is_some() {
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
        if let Some(ref lm) = self.vfs_lock {
            for handle in &lock_handles {
                if *handle > 0 {
                    lm.do_release(*handle);
                }
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
                    hit: false,
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
        if self
            .router
            .route_impl(parent_path, zone_id, is_admin, false)
            .is_err()
        {
            return Vec::new();
        }

        let prefix = if parent_path == "/" {
            "/".to_string()
        } else {
            format!("{}/", parent_path)
        };

        self.dcache.list_children(&prefix)
    }
}

// ── Fast path validation ────────────────────────────────────────────────

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
}
