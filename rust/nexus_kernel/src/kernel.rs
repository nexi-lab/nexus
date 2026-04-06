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
use crate::dispatch::Trie;
use crate::lock::{LockMode, VFSLockManagerInner};
use crate::metastore::{Metastore, RedbMetastore};
use crate::router::{PathRouter, RouteError, RustRouteResult};
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
    metastore: Option<Box<dyn Metastore>>,
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

    /// Wire a Metastore impl (PyMetastoreAdapter, redb, gRPC, etc.).
    pub fn set_metastore(&mut self, metastore: Box<dyn Metastore>) {
        self.metastore = Some(metastore);
    }

    /// Wire RedbMetastore by path — Rust kernel opens redb directly.
    ///
    /// Preferred over `set_metastore(PyMetastoreAdapter)` — eliminates
    /// GIL crossing on every metastore.get/put in the hot path.
    pub fn set_metastore_path(&mut self, path: &str) -> Result<(), KernelError> {
        let ms = RedbMetastore::open(std::path::Path::new(path))
            .map_err(|e| KernelError::IOError(format!("RedbMetastore: {e:?}")))?;
        self.metastore = Some(Box::new(ms));
        Ok(())
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
    ) -> Result<(), KernelError> {
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

    /// Remove a mount point.
    pub fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
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
                // Metastore fallback
                match &self.metastore {
                    Some(ms) => match ms.get(path) {
                        Ok(Some(meta)) => {
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
                        Ok(None) => return miss(),
                        Err(_) => return miss(),
                    },
                    None => return Err(KernelError::FileNotFound(path.to_string())),
                }
            }
        };

        // DT_PIPE/DT_STREAM -> return entry_type so wrapper dispatches IPC
        if let dt @ (DT_PIPE | DT_STREAM) = entry.entry_type {
            return Ok(SysReadResult {
                hit: false,
                data: None,
                post_hook_needed: false,
                content_hash: None,
                entry_type: dt,
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

        // 3. DCache check — DT_PIPE/DT_STREAM -> wrapper handles
        if let Some(entry) = self.dcache.get_entry(path) {
            match entry.entry_type {
                DT_PIPE | DT_STREAM => return miss(),
                _ => {}
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

                // Build FileMetadata and persist via metastore
                if let Some(ref ms) = self.metastore {
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
                }

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

        // 3. Get metadata (dcache or metastore)
        let meta = match self.dcache.get_entry(path) {
            Some(e) => Some(e),
            None => {
                // Metastore fallback
                if let Some(ref ms) = self.metastore {
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
                } else {
                    None
                }
            }
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

        // 6. Metastore delete
        if let Some(ref ms) = self.metastore {
            let _ = ms.delete(path);
        }

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

        // 4. Existence check: get old metadata
        let old_meta = if let Some(ref ms) = self.metastore {
            ms.get(old_path).ok().flatten()
        } else {
            None
        };

        // Also check dcache
        let old_entry = self.dcache.get_entry(old_path);

        let (is_directory, entry_type) = match (&old_meta, &old_entry) {
            (Some(m), _) => (m.entry_type == DT_DIR, m.entry_type),
            (None, Some(e)) => (e.entry_type == DT_DIR, e.entry_type),
            (None, None) => {
                release_locks(&self.vfs_lock, lock1, lock2);
                return Err(KernelError::FileNotFound(old_path.to_string()));
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

        // 5. Destination conflict check
        if let Some(ref ms) = self.metastore {
            if ms.exists(new_path).unwrap_or(false) {
                release_locks(&self.vfs_lock, lock1, lock2);
                return Err(KernelError::IOError(format!(
                    "Destination path already exists: {new_path}"
                )));
            }
        }

        // 6. Put-then-delete (crash-safe): metastore.put(new) → metastore.delete(old)
        if let Some(ref ms) = self.metastore {
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
                let _ = ms.put(new_path, new_meta);
                let _ = ms.delete(old_path);
            }

            // 7. Recursive child rename (directories)
            if is_directory {
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
            }
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

        // 3. Existence check via metastore
        if let Some(ref ms) = self.metastore {
            if ms.exists(path).unwrap_or(false) {
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
        }

        // 4. Backend mkdir (best-effort, PAS backends create physical dirs)
        let _ = self
            .router
            .mkdir(&route.mount_point, &route.backend_path, parents, true);

        // 5. Ensure parent directories
        if parents {
            self.ensure_parent_directories(path, ctx, &route.mount_point)?;
        }

        // 6. Create directory metadata in metastore
        if let Some(ref ms) = self.metastore {
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
        }

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
        let ms = match &self.metastore {
            Some(ms) => ms,
            None => return Ok(()),
        };

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
                    if !ms.exists(current).unwrap_or(true) {
                        to_create.push(current.to_string());
                    } else {
                        break; // Existing parent found, stop
                    }
                }
            }
        }

        // Create from shallowest to deepest
        for dir_path in to_create.into_iter().rev() {
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
        let _ = mount_point; // used for routing context
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

        // 3. Get metadata
        let entry_type = if let Some(ref ms) = self.metastore {
            ms.get(path)
                .ok()
                .flatten()
                .map(|m| m.entry_type)
                .unwrap_or(DT_DIR)
        } else {
            DT_DIR
        };

        // DT_MOUNT(2) / DT_EXTERNAL_STORAGE(5) → Python handles unmount
        if entry_type == 2 || entry_type == 5 {
            return miss();
        }

        // 4. Check children
        let mut children_deleted = 0;
        if let Some(ref ms) = self.metastore {
            let prefix = format!("{}/", path.trim_end_matches('/'));
            let children = ms.list(&prefix).unwrap_or_default();

            if !children.is_empty() {
                if !recursive {
                    return Err(KernelError::IOError(format!("Directory not empty: {path}")));
                }

                // 5. Recursive: batch delete all children
                let child_paths: Vec<String> = children.iter().map(|c| c.path.clone()).collect();
                children_deleted = ms.delete_batch(&child_paths).unwrap_or(0);
            }
        }

        // 6. Backend rmdir (best-effort)
        let _ = self
            .router
            .rmdir(&route.mount_point, &route.backend_path, recursive);

        // 7. Delete directory metadata
        if let Some(ref ms) = self.metastore {
            let _ = ms.delete(path);
        }

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
        let mut batch_meta: Vec<(String, crate::metastore::FileMetadata)> = Vec::new();

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
                    batch_meta.push((path.clone(), meta));

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

        // 4b. Single metastore.put_batch for all successful writes
        if !batch_meta.is_empty() {
            if let Some(ref ms) = self.metastore {
                let _ = ms.put_batch(&batch_meta);
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
