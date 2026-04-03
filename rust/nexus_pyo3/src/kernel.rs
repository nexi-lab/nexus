//! Kernel — Rust kernel owning all core state.
//!
//! Owns DCache, PathRouter, Trie, VFS Lock, HookRegistry, ObserverRegistry.
//! Exposes proxy #[pymethods] for Python-side mutation and syscall execution.
//!
//! Architecture:
//!   - Created empty via Kernel(), then components are wired by factory.
//!   - DCache/Router/Trie/Hooks/Observers use interior mutability (&self methods).
//!   - VFS Lock is optionally Arc-shared with Python VFSLockManager (blocking acquire).
//!   - Metastore trait (Option<Box<dyn Metastore>>) reserved for PR 4+.
//!
//! Issue #1868: PR 3 — Kernel owns state, 4-pillar traits, dispatch registration.

use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_EXTERNAL, DT_PIPE, DT_STREAM};
use crate::dispatch::{HookRegistry, ObserverRegistry, Trie};
use crate::lock::{LockMode, VFSLockManager, VFSLockManagerInner};
use crate::metastore::Metastore;
use crate::router::PathRouter;
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

// ── Strong-typed result types ──────────────────────────────────────────

/// Result of sys_read(): concrete type instead of Option<bytes>.
#[pyclass(get_all)]
pub struct SysReadResult {
    /// True if Rust kernel handled the read (no Python fallback needed).
    pub hit: bool,
    /// Content bytes (only when hit=true).
    pub data: Option<Py<PyBytes>>,
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
    /// Content hash (etag) for post-hook context.
    pub content_hash: Option<String>,
}

/// Result of sys_write(): concrete type instead of Option<str>.
#[pyclass(get_all)]
pub struct SysWriteResult {
    /// True if Rust backend completed the write.
    pub hit: bool,
    /// BLAKE3 content hash (only when hit=true).
    pub content_id: Option<String>,
}

// ── Action constants ────────────────────────────────────────────────────

pub const ACTION_DCACHE_HIT: u8 = 0;
pub const ACTION_RESOLVED: u8 = 1;
pub const ACTION_PIPE: u8 = 2;
pub const ACTION_STREAM: u8 = 3;
pub const ACTION_EXTERNAL: u8 = 4;
pub const ACTION_CACHE_MISS: u8 = 5;
pub const ACTION_ERROR: u8 = 6;

// ── Plan types (kernel-internal) ──────────────────────────────────────

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct ReadPlan {
    pub(crate) action: u8,
    pub(crate) mount_point: String,
    pub(crate) backend_path: String,
    pub(crate) etag: Option<String>,
    pub(crate) backend_name: String,
    pub(crate) readonly: bool,
    pub(crate) io_profile: String,
    pub(crate) entry_type: u8,
    pub(crate) validated_path: String,
    pub(crate) resolver_idx: i64,
    pub(crate) error_msg: Option<String>,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct WritePlan {
    pub(crate) action: u8,
    pub(crate) mount_point: String,
    pub(crate) backend_path: String,
    pub(crate) etag: Option<String>,
    pub(crate) backend_name: String,
    pub(crate) readonly: bool,
    pub(crate) io_profile: String,
    pub(crate) entry_type: u8,
    pub(crate) validated_path: String,
    pub(crate) resolver_idx: i64,
    pub(crate) error_msg: Option<String>,
    pub(crate) version: u32,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct StatPlan {
    pub(crate) action: u8,
    pub(crate) validated_path: String,
    pub(crate) backend_name: String,
    pub(crate) physical_path: String,
    pub(crate) size: u64,
    pub(crate) etag: Option<String>,
    pub(crate) mime_type: Option<String>,
    pub(crate) entry_type: u8,
    pub(crate) version: u32,
    pub(crate) zone_id: Option<String>,
    pub(crate) is_directory: bool,
    pub(crate) resolver_idx: i64,
    pub(crate) error_msg: Option<String>,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub(crate) struct RenamePlan {
    pub(crate) action: u8,
    pub(crate) old_path: String,
    pub(crate) new_path: String,
    pub(crate) old_mount_point: String,
    pub(crate) old_backend_path: String,
    pub(crate) new_mount_point: String,
    pub(crate) new_backend_path: String,
    pub(crate) old_readonly: bool,
    pub(crate) new_readonly: bool,
    pub(crate) entry_type: u8,
    pub(crate) error_msg: Option<String>,
}

// ── Kernel ──────────────────────────────────────────────────────────────

/// Rust kernel — owns all core state directly.
///
/// Created empty via `Kernel()`, then wired by factory:
///   - `set_vfs_lock(lock)` — share VFS lock with Python VFSLockManager.
///   - `add_mount(...)` — register mount points.
///   - `dcache_put(...)` — populate dentry cache.
///   - `trie_register(...)` — register path resolvers.
///   - `register_hook(...)` — register INTERCEPT hooks.
///   - `register_observer(...)` — register OBSERVE observers.
#[pyclass]
pub struct Kernel {
    // DCache (owned)
    dcache: DCache,
    // Router (owned)
    router: PathRouter,
    // PathTrie (owned)
    trie: Trie,
    // VFS Lock (Arc-shared with Python VFSLockManager for blocking acquire)
    vfs_lock: Option<Arc<VFSLockManagerInner>>,
    // Hook/Observer registries (Mutex for interior mutability)
    hooks: Mutex<HookRegistry>,
    observers: Mutex<ObserverRegistry>,
    // Metastore trait (Rust-native, used when available)
    #[allow(dead_code)]
    metastore: Option<Box<dyn Metastore>>,
    // VFS lock timeout for blocking acquire (ms)
    vfs_lock_timeout_ms: u64,
    // Hook counts (atomics for lock-free hot-path check)
    read_hook_count: AtomicU64,
    write_hook_count: AtomicU64,
    stat_hook_count: AtomicU64,
    delete_hook_count: AtomicU64,
    rename_hook_count: AtomicU64,
}

#[pymethods]
impl Kernel {
    // ── Constructor ────────────────────────────────────────────────────

    /// Create an empty kernel. Components wired by factory after construction.
    #[new]
    fn new() -> Self {
        Self {
            dcache: DCache::new(),
            router: PathRouter::new(),
            trie: Trie::new(),
            vfs_lock: None,
            hooks: Mutex::new(HookRegistry::new()),
            observers: Mutex::new(ObserverRegistry::new()),
            metastore: None,
            vfs_lock_timeout_ms: 5000,
            read_hook_count: AtomicU64::new(0),
            write_hook_count: AtomicU64::new(0),
            stat_hook_count: AtomicU64::new(0),
            delete_hook_count: AtomicU64::new(0),
            rename_hook_count: AtomicU64::new(0),
        }
    }

    // ── VFS Lock wiring ────────────────────────────────────────────────

    /// Wire VFS lock manager (shares Arc with Python VFSLockManager).
    fn set_vfs_lock(&mut self, vfs_lock: &VFSLockManager) {
        self.vfs_lock = Some(Arc::clone(&vfs_lock.inner));
    }

    /// Set VFS lock timeout in milliseconds (default 5000).
    fn set_vfs_lock_timeout(&mut self, timeout_ms: u64) {
        self.vfs_lock_timeout_ms = timeout_ms;
    }

    // ── DCache proxy methods ───────────────────────────────────────────

    /// Insert or update a cache entry.
    #[pyo3(signature = (path, backend_name, physical_path, size, entry_type, version=1, etag=None, zone_id=None, mime_type=None))]
    #[allow(clippy::too_many_arguments)]
    fn dcache_put(
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
    fn dcache_get(&self, path: &str) -> Option<(String, String, u8)> {
        self.dcache.get_hot(path)
    }

    /// Get full entry as dict.
    fn dcache_get_full(&self, py: Python<'_>, path: &str) -> PyResult<Option<Py<PyAny>>> {
        match self.dcache.get_entry(path) {
            Some(e) => {
                let dict = PyDict::new(py);
                dict.set_item("backend_name", &e.backend_name)?;
                dict.set_item("physical_path", &e.physical_path)?;
                dict.set_item("size", e.size)?;
                dict.set_item("etag", e.etag.as_deref())?;
                dict.set_item("version", e.version)?;
                dict.set_item("entry_type", e.entry_type)?;
                dict.set_item("zone_id", e.zone_id.as_deref())?;
                dict.set_item("mime_type", e.mime_type.as_deref())?;
                Ok(Some(dict.into()))
            }
            None => Ok(None),
        }
    }

    /// Evict a single path.
    fn dcache_evict(&self, path: &str) -> bool {
        self.dcache.evict(path)
    }

    /// Evict all entries with given prefix.
    fn dcache_evict_prefix(&self, prefix: &str) -> usize {
        self.dcache.evict_prefix(prefix)
    }

    /// Check if path exists in cache.
    fn dcache_contains(&self, path: &str) -> bool {
        self.dcache.contains(path)
    }

    /// Return cache statistics as a dict.
    fn dcache_stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (hits, misses, size) = self.dcache.stats();
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };
        let dict = PyDict::new(py);
        dict.set_item("hits", hits)?;
        dict.set_item("misses", misses)?;
        dict.set_item("size", size)?;
        dict.set_item("hit_rate", hit_rate)?;
        Ok(dict.into())
    }

    /// Clear all entries and reset counters.
    fn dcache_clear(&self) {
        self.dcache.clear();
    }

    /// Number of entries in dcache.
    fn dcache_len(&self) -> usize {
        self.dcache.len()
    }

    // ── Router proxy methods ───────────────────────────────────────────

    /// Register a mount point.
    #[pyo3(signature = (mount_point, zone_id, readonly, admin_only, io_profile, backend_name="", local_root=None, fsync=false))]
    #[allow(clippy::too_many_arguments)]
    fn add_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
        backend_name: &str,
        local_root: Option<&str>,
        fsync: bool,
    ) -> PyResult<()> {
        self.router
            .add_mount(
                mount_point,
                zone_id,
                readonly,
                admin_only,
                io_profile,
                backend_name,
                local_root,
                fsync,
            )
            .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))
    }

    /// Remove a mount point.
    fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.router.remove_mount(mount_point, zone_id)
    }

    /// Zone-canonical LPM routing.
    fn route(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> PyResult<crate::router::RustRouteResult> {
        self.router
            .route_impl(path, zone_id, is_admin, check_write)
            .map_err(Into::into)
    }

    /// Check if a mount exists.
    fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        self.router.has_mount(mount_point, zone_id)
    }

    /// List all mount points.
    fn get_mount_points(&self) -> Vec<String> {
        self.router.get_mount_points()
    }

    // ── Trie proxy methods ─────────────────────────────────────────────

    /// Register a path pattern with a resolver index.
    fn trie_register(&self, pattern: &str, resolver_idx: usize) -> PyResult<()> {
        self.trie
            .register(pattern, resolver_idx)
            .map_err(pyo3::exceptions::PyValueError::new_err)
    }

    /// Remove a resolver by index.
    fn trie_unregister(&self, resolver_idx: usize) -> bool {
        self.trie.unregister(resolver_idx)
    }

    /// Lookup a concrete path.
    fn trie_lookup(&self, path: &str) -> Option<usize> {
        self.trie.lookup(path)
    }

    /// Number of registered trie patterns.
    fn trie_len(&self) -> usize {
        self.trie.len()
    }

    // ── Hook proxy methods ─────────────────────────────────────────────

    /// Register a hook for an operation.
    fn register_hook(&self, py: Python<'_>, op: &str, hook: Py<PyAny>) -> PyResult<()> {
        self.hooks.lock().register(py, op, hook)
    }

    /// Unregister a hook by identity.
    fn unregister_hook(&self, py: Python<'_>, op: &str, hook: &Bound<'_, PyAny>) -> bool {
        self.hooks.lock().unregister(py, op, hook)
    }

    /// Return hooks with on_pre_{op}.
    fn get_pre_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.hooks.lock().get_pre_hooks(py, op)
    }

    /// Return (sync_post, async_post) hooks.
    fn get_post_hooks(&self, py: Python<'_>, op: &str) -> (Vec<Py<PyAny>>, Vec<Py<PyAny>>) {
        self.hooks.lock().get_post_hooks(py, op)
    }

    /// Return all hooks for operation.
    fn get_all_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.hooks.lock().get_all_hooks(py, op)
    }

    /// Number of hooks for operation.
    fn hook_count(&self, op: &str) -> usize {
        self.hooks.lock().count(op)
    }

    // ── Observer proxy methods ─────────────────────────────────────────

    /// Register an observer with event_mask bitmask.
    fn register_observer(&self, py: Python<'_>, obs: Py<PyAny>, event_mask: u32) -> PyResult<()> {
        self.observers.lock().register(py, obs, event_mask)
    }

    /// Unregister an observer by identity.
    fn unregister_observer(&self, py: Python<'_>, obs: &Bound<'_, PyAny>) -> bool {
        self.observers.lock().unregister(py, obs)
    }

    /// Return matching observers for event_type_bit.
    fn get_matching_observers(
        &self,
        py: Python<'_>,
        event_type_bit: u32,
    ) -> Vec<(Py<PyAny>, String)> {
        self.observers.lock().get_matching(py, event_type_bit)
    }

    /// Number of registered observers.
    fn observer_count(&self) -> usize {
        self.observers.lock().count()
    }

    // ── Hook counts ────────────────────────────────────────────────────

    /// Update hook count for an operation.
    fn set_hook_count(&self, op: &str, count: u64) {
        match op {
            "read" => self.read_hook_count.store(count, Ordering::Relaxed),
            "write" => self.write_hook_count.store(count, Ordering::Relaxed),
            "stat" => self.stat_hook_count.store(count, Ordering::Relaxed),
            "delete" => self.delete_hook_count.store(count, Ordering::Relaxed),
            "rename" => self.rename_hook_count.store(count, Ordering::Relaxed),
            _ => {}
        }
    }

    // ── sys_read ───────────────────────────────────────────────────────

    /// Rust syscall: read file content — pure Rust path (zero GIL).
    ///
    /// validate → route → dcache (authoritative) → VFS lock → CAS read → return.
    ///
    /// DCache is authoritative: miss = FileNotFoundError (no metastore fallback).
    /// Returns `hit=false` for DT_PIPE/DT_STREAM (wrapper handles async IPC)
    /// or when no Rust backend is available (e.g. remote backends).
    /// Raises `InvalidPathError`, `NexusFileNotFoundError` on errors.
    ///
    /// Resolve, pre-hooks, and post-hooks are handled by the Python wrapper
    /// (intermediate state — migrates to Rust dispatch middleware in PR 7).
    #[pyo3(signature = (path, zone_id, is_admin))]
    fn sys_read<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<SysReadResult> {
        let miss = || {
            Ok(SysReadResult {
                hit: false,
                data: None,
                post_hook_needed: false,
                content_hash: None,
            })
        };

        // 1. Validate
        if let Err(msg) = validate_path_fast(path) {
            return Err(Self::raise_invalid_path(py, &msg));
        }

        // 2. Route (pure Rust LPM)
        let route = match self.router.route_impl(path, zone_id, is_admin, false) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache lookup (authoritative — miss = FileNotFoundError)
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => return Err(Self::raise_file_not_found(py, path)),
        };

        // DT_PIPE/DT_STREAM → wrapper handles async IPC
        match entry.entry_type {
            DT_PIPE | DT_STREAM => return miss(),
            _ => {}
        }

        // Path-based backend: no etag → use backend_path
        let etag = entry.etag.as_deref().unwrap_or("");
        if etag.is_empty() && route.backend_path.is_empty() {
            return Err(Self::raise_file_not_found(py, path));
        }

        // 4. VFS lock (blocking, GIL released during wait)
        let lock_handle = if let Some(ref lm) = self.vfs_lock {
            let timeout = self.vfs_lock_timeout_ms;
            let lm = Arc::clone(lm);
            let p = path.to_string();
            py.detach(move || lm.blocking_acquire(&p, LockMode::Read, timeout))
        } else {
            0
        };

        // Lock timeout → miss (unsafe to read without lock)
        if self.vfs_lock.is_some() && lock_handle == 0 {
            return miss();
        }

        // 5. Backend read (CasLocal — pure Rust, zero GIL)
        let content = if !etag.is_empty() {
            self.router.read_content(&route.mount_point, etag)
        } else {
            None
        };

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
                data: Some(PyBytes::new(py, &data).into()),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_hash: entry.etag,
            }),
            // No Rust backend available (e.g. remote) — wrapper handles
            None => miss(),
        }
    }

    // ── sys_write ──────────────────────────────────────────────────────

    /// Rust syscall: write file content.
    fn sys_write(
        &self,
        path: &str,
        zone_id: &str,
        content: &[u8],
        is_admin: bool,
    ) -> PyResult<SysWriteResult> {
        let miss = || SysWriteResult {
            hit: false,
            content_id: None,
        };

        if self.write_hook_count.load(Ordering::Relaxed) > 0 {
            return Ok(miss());
        }

        let plan = self.plan_write(path, zone_id, is_admin);
        if plan.action == ACTION_ERROR {
            return Err(pyo3::exceptions::PyValueError::new_err(
                plan.error_msg.unwrap_or_default(),
            ));
        }
        if plan.action != ACTION_DCACHE_HIT {
            return Ok(miss());
        }

        let lock_handle = self
            .vfs_lock
            .as_ref()
            .map(|lm| lm.try_acquire(path, LockMode::Write));
        if let Some(0) = lock_handle {
            return Ok(miss());
        }

        let result = match self.router.write_content(&plan.mount_point, content) {
            Some(hash) => SysWriteResult {
                hit: true,
                content_id: Some(hash),
            },
            None => miss(),
        };

        if let Some(handle) = lock_handle {
            if handle > 0 {
                if let Some(lm) = &self.vfs_lock {
                    lm.do_release(handle);
                }
            }
        }

        Ok(result)
    }

    // ── sys_stat ───────────────────────────────────────────────────────

    /// Rust syscall: get file metadata (FUSE getattr hot path).
    fn sys_stat<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        if self.stat_hook_count.load(Ordering::Relaxed) > 0 {
            return Ok(None);
        }

        if path.is_empty() || path.contains('\0') {
            return Ok(None);
        }
        if !path.starts_with('/') {
            return Ok(None);
        }
        for segment in path.split('/') {
            if segment == ".." {
                return Ok(None);
            }
        }

        if self.trie.lookup(path).is_some() {
            return Ok(None);
        }

        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return Ok(None);
        }

        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => return Ok(None),
        };

        let is_dir = entry.entry_type == DT_DIR;
        let dict = PyDict::new(py);

        dict.set_item("path", path)?;
        dict.set_item("backend_name", &entry.backend_name)?;
        dict.set_item("physical_path", &entry.physical_path)?;
        dict.set_item(
            "size",
            if is_dir && entry.size == 0 {
                4096u64
            } else {
                entry.size
            },
        )?;
        dict.set_item("etag", entry.etag.as_deref())?;

        let mime = entry.mime_type.as_deref().unwrap_or(if is_dir {
            "inode/directory"
        } else {
            "application/octet-stream"
        });
        dict.set_item("mime_type", mime)?;

        dict.set_item("created_at", py.None())?;
        dict.set_item("modified_at", py.None())?;

        dict.set_item("is_directory", is_dir)?;
        dict.set_item("entry_type", entry.entry_type)?;

        dict.set_item("mode", if is_dir { 0o755u32 } else { 0o644u32 })?;

        dict.set_item("version", entry.version)?;
        dict.set_item("zone_id", entry.zone_id.as_deref())?;

        Ok(Some(dict))
    }
}

// ── Private helpers (kernel-internal) ────────────────────────────────────

impl Kernel {
    // ── Error constructors ──

    fn raise_invalid_path(py: Python<'_>, msg: &str) -> PyErr {
        py.import("nexus.contracts.exceptions")
            .and_then(|m| m.getattr("InvalidPathError"))
            .and_then(|cls| cls.call1((msg,)))
            .map(PyErr::from_value)
            .unwrap_or_else(|e| e)
    }

    fn raise_file_not_found(py: Python<'_>, path: &str) -> PyErr {
        py.import("nexus.contracts.exceptions")
            .and_then(|m| m.getattr("NexusFileNotFoundError"))
            .and_then(|cls| cls.call1((path,)))
            .map(PyErr::from_value)
            .unwrap_or_else(|e| e)
    }

    // ── Planning methods ──

    fn plan_write(&self, path: &str, zone_id: &str, is_admin: bool) -> WritePlan {
        if let Err(msg) = validate_path_fast(path) {
            return WritePlan::error(msg);
        }

        if let Some(idx) = self.trie.lookup(path) {
            return WritePlan::resolved(path, idx);
        }

        let route = match self.router.route_impl(path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(_) => {
                return WritePlan::cache_miss(path);
            }
        };

        match self.dcache.get_entry(path) {
            Some(entry) => {
                let action = match entry.entry_type {
                    DT_PIPE => ACTION_PIPE,
                    DT_STREAM => ACTION_STREAM,
                    DT_EXTERNAL => ACTION_EXTERNAL,
                    _ => ACTION_DCACHE_HIT,
                };
                WritePlan {
                    action,
                    mount_point: route.mount_point,
                    backend_path: route.backend_path,
                    etag: entry.etag,
                    backend_name: entry.backend_name,
                    readonly: route.readonly,
                    io_profile: route.io_profile,
                    entry_type: entry.entry_type,
                    validated_path: path.to_string(),
                    resolver_idx: -1,
                    error_msg: None,
                    version: entry.version,
                }
            }
            None => WritePlan::cache_miss(path),
        }
    }

    #[allow(dead_code)]
    fn plan_stat(&self, path: &str, zone_id: &str, is_admin: bool) -> StatPlan {
        if path.is_empty() || path.contains('\0') {
            return StatPlan::error("Invalid path".to_string());
        }
        if !path.starts_with('/') {
            return StatPlan::error("Path must start with /".to_string());
        }
        for segment in path.split('/') {
            if segment == ".." {
                return StatPlan::error(
                    "Path contains parent directory reference (..)".to_string(),
                );
            }
        }

        if let Some(idx) = self.trie.lookup(path) {
            return StatPlan::resolved(path, idx);
        }

        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return StatPlan::cache_miss(path);
        }

        match self.dcache.get_entry(path) {
            Some(entry) => StatPlan {
                action: ACTION_DCACHE_HIT,
                validated_path: path.to_string(),
                backend_name: entry.backend_name,
                physical_path: entry.physical_path,
                size: entry.size,
                etag: entry.etag,
                mime_type: entry.mime_type,
                entry_type: entry.entry_type,
                version: entry.version,
                zone_id: entry.zone_id,
                is_directory: entry.entry_type == DT_DIR,
                resolver_idx: -1,
                error_msg: None,
            },
            None => StatPlan::cache_miss(path),
        }
    }

    #[allow(dead_code)]
    fn plan_unlink(&self, path: &str, zone_id: &str, is_admin: bool) -> WritePlan {
        self.plan_write(path, zone_id, is_admin)
    }

    #[allow(dead_code)]
    fn plan_rename(
        &self,
        old_path: &str,
        new_path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> RenamePlan {
        if let Err(msg) = validate_path_fast(old_path) {
            return RenamePlan::error(msg);
        }
        if let Err(msg) = validate_path_fast(new_path) {
            return RenamePlan::error(msg);
        }

        let old_route = match self.router.route_impl(old_path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(e) => {
                return RenamePlan::error(format!("Old path routing failed: {e:?}"));
            }
        };

        let new_route = match self.router.route_impl(new_path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(e) => {
                return RenamePlan::error(format!("New path routing failed: {e:?}"));
            }
        };

        let entry_type = self.dcache.get_entry_type(old_path).unwrap_or(0);

        RenamePlan {
            action: ACTION_DCACHE_HIT,
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
            old_mount_point: old_route.mount_point,
            old_backend_path: old_route.backend_path,
            new_mount_point: new_route.mount_point,
            new_backend_path: new_route.backend_path,
            old_readonly: old_route.readonly,
            new_readonly: new_route.readonly,
            entry_type,
            error_msg: None,
        }
    }
}

// ── Plan constructors ──────────────────────────────────────────────────

#[allow(dead_code)]
impl ReadPlan {
    fn error(msg: String) -> Self {
        Self {
            action: ACTION_ERROR,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: String::new(),
            resolver_idx: -1,
            error_msg: Some(msg),
        }
    }

    fn resolved(path: &str, idx: usize) -> Self {
        Self {
            action: ACTION_RESOLVED,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: path.to_string(),
            resolver_idx: idx as i64,
            error_msg: None,
        }
    }

    fn cache_miss(path: &str) -> Self {
        Self {
            action: ACTION_CACHE_MISS,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: path.to_string(),
            resolver_idx: -1,
            error_msg: None,
        }
    }
}

impl WritePlan {
    fn error(msg: String) -> Self {
        Self {
            action: ACTION_ERROR,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: String::new(),
            resolver_idx: -1,
            error_msg: Some(msg),
            version: 0,
        }
    }

    fn resolved(path: &str, idx: usize) -> Self {
        Self {
            action: ACTION_RESOLVED,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: path.to_string(),
            resolver_idx: idx as i64,
            error_msg: None,
            version: 0,
        }
    }

    fn cache_miss(path: &str) -> Self {
        Self {
            action: ACTION_CACHE_MISS,
            mount_point: String::new(),
            backend_path: String::new(),
            etag: None,
            backend_name: String::new(),
            readonly: false,
            io_profile: String::new(),
            entry_type: 0,
            validated_path: path.to_string(),
            resolver_idx: -1,
            error_msg: None,
            version: 0,
        }
    }
}

impl StatPlan {
    fn error(msg: String) -> Self {
        Self {
            action: ACTION_ERROR,
            validated_path: String::new(),
            backend_name: String::new(),
            physical_path: String::new(),
            size: 0,
            etag: None,
            mime_type: None,
            entry_type: 0,
            version: 0,
            zone_id: None,
            is_directory: false,
            resolver_idx: -1,
            error_msg: Some(msg),
        }
    }

    fn resolved(path: &str, idx: usize) -> Self {
        Self {
            action: ACTION_RESOLVED,
            validated_path: path.to_string(),
            backend_name: String::new(),
            physical_path: String::new(),
            size: 0,
            etag: None,
            mime_type: None,
            entry_type: 0,
            version: 0,
            zone_id: None,
            is_directory: false,
            resolver_idx: idx as i64,
            error_msg: None,
        }
    }

    fn cache_miss(path: &str) -> Self {
        Self {
            action: ACTION_CACHE_MISS,
            validated_path: path.to_string(),
            backend_name: String::new(),
            physical_path: String::new(),
            size: 0,
            etag: None,
            mime_type: None,
            entry_type: 0,
            version: 0,
            zone_id: None,
            is_directory: false,
            resolver_idx: -1,
            error_msg: None,
        }
    }
}

impl RenamePlan {
    fn error(msg: String) -> Self {
        Self {
            action: ACTION_ERROR,
            old_path: String::new(),
            new_path: String::new(),
            old_mount_point: String::new(),
            old_backend_path: String::new(),
            new_mount_point: String::new(),
            new_backend_path: String::new(),
            old_readonly: false,
            new_readonly: false,
            entry_type: 0,
            error_msg: Some(msg),
        }
    }
}

// ── Fast path validation ────────────────────────────────────────────────

fn validate_path_fast(path: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err("Path cannot be empty".to_string());
    }
    if !path.starts_with('/') {
        return Err("Path must start with /".to_string());
    }
    if path.contains('\0') {
        return Err("Path contains null byte".to_string());
    }
    for segment in path.split('/') {
        if segment == ".." {
            return Err("Path contains parent directory reference (..)".to_string());
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

    #[test]
    fn test_action_constants() {
        assert_eq!(ACTION_DCACHE_HIT, 0);
        assert_eq!(ACTION_RESOLVED, 1);
        assert_eq!(ACTION_PIPE, 2);
        assert_eq!(ACTION_STREAM, 3);
        assert_eq!(ACTION_EXTERNAL, 4);
        assert_eq!(ACTION_CACHE_MISS, 5);
        assert_eq!(ACTION_ERROR, 6);
    }

    #[test]
    fn test_stat_plan_constructors() {
        let err = StatPlan::error("bad path".to_string());
        assert_eq!(err.action, ACTION_ERROR);
        assert_eq!(err.error_msg.as_deref(), Some("bad path"));

        let miss = StatPlan::cache_miss("/foo");
        assert_eq!(miss.action, ACTION_CACHE_MISS);
        assert_eq!(miss.validated_path, "/foo");

        let resolved = StatPlan::resolved("/bar", 7);
        assert_eq!(resolved.action, ACTION_RESOLVED);
        assert_eq!(resolved.resolver_idx, 7);
    }

    #[test]
    fn test_rename_plan_error() {
        let err = RenamePlan::error("readonly".to_string());
        assert_eq!(err.action, ACTION_ERROR);
        assert_eq!(err.error_msg.as_deref(), Some("readonly"));
    }
}
