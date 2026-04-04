//! Kernel — Rust kernel owning all core state.
//!
//! Owns DCache, PathRouter, Trie, VFS Lock, HookRegistry, ObserverRegistry, Metastore.
//! Exposes proxy #[pymethods] for Python-side mutation and syscall execution.
//!
//! Architecture:
//!   - Created empty via Kernel(), then components are wired by factory.
//!   - DCache/Router/Trie/Hooks/Observers use interior mutability (&self methods).
//!   - VFS Lock is optionally Arc-shared with Python VFSLockManager (blocking acquire).
//!   - Metastore (PyMetastoreAdapter) wraps Python MetastoreABC via GIL.
//!
//! Issue #1868: PR 7b — Metastore wired, dcache-miss → metastore fallback.

use crate::dcache::{CachedEntry, DCache, DT_DIR, DT_PIPE, DT_REG, DT_STREAM};
use crate::dispatch::{HookRegistry, ObserverRegistry, Trie};
use crate::generated_adapters::PyMetastoreAdapter;
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
    /// True if post-hooks should be fired by the async wrapper.
    pub post_hook_needed: bool,
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
    // Metastore (PyMetastoreAdapter wrapping Python MetastoreABC)
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

    // ── Metastore wiring ──────────────────────────────────────────────

    /// Wire Python MetastoreABC → Rust Metastore trait via PyMetastoreAdapter.
    ///
    /// Called once from NexusFS.__init__. After this, sys_read dcache-miss
    /// falls back to metastore.get() instead of raising FileNotFoundError.
    fn set_metastore(&mut self, metastore: Py<PyAny>) {
        self.metastore = Some(Box::new(PyMetastoreAdapter::new(metastore)));
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
    ///
    /// `py_backend`: Optional Python ObjectStoreABC instance. When provided
    /// and `local_root` is None, wraps it via PyObjectStoreAdapter so Rust
    /// sys_read/sys_write can call the Python backend directly.
    #[pyo3(signature = (mount_point, zone_id, readonly, admin_only, io_profile, backend_name="", local_root=None, fsync=false, py_backend=None))]
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
        py_backend: Option<Py<PyAny>>,
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
                py_backend,
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

    /// Rust syscall: read file content.
    ///
    /// validate → route → dcache → [metastore fallback] → VFS lock → CAS read → return.
    ///
    /// DCache hit = hot path (zero GIL). DCache miss = cold path: queries
    /// Python MetastoreABC via PyMetastoreAdapter (GIL), populates dcache,
    /// then continues with CAS read.
    ///
    /// Returns `hit=false` for DT_PIPE/DT_STREAM (wrapper handles async IPC)
    /// or when no Rust backend is available (e.g. remote backends).
    /// Raises `InvalidPathError`, `NexusFileNotFoundError` on errors.
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

        // 3. DCache lookup — on miss, fallback to metastore (cold path, GIL)
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                // Metastore fallback: query Python MetastoreABC via PyMetastoreAdapter
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
                        // Metastore miss — may be overlay base layer; let Python handle
                        Ok(None) => return miss(),
                        Err(_) => return miss(),
                    },
                    None => return Err(Self::raise_file_not_found(py, path)),
                }
            }
        };

        // DT_PIPE/DT_STREAM → wrapper handles async IPC
        match entry.entry_type {
            DT_PIPE | DT_STREAM => return miss(),
            _ => {}
        }

        // Content identifier: CAS backends use etag (hash), path backends
        // use physical_path.  Either must be non-empty to attempt a read.
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

        // 5. Backend read (CasLocal or PyObjectStoreAdapter)
        let content = self
            .router
            .read_content(&route.mount_point, content_id, &route.backend_path);

        // 6. Release VFS lock (always, even on miss)
        if lock_handle > 0 {
            if let Some(ref lm) = self.vfs_lock {
                lm.do_release(lock_handle);
            }
        }

        // 7. Return result
        // CDC manifests are reassembled by CASEngine.read_content() — no special
        // handling needed here. Content is always the final assembled bytes.
        match content {
            Some(data) => Ok(SysReadResult {
                hit: true,
                data: Some(PyBytes::new(py, &data).into()),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_hash: entry.etag,
            }),
            None => miss(),
        }
    }

    // ── sys_write ──────────────────────────────────────────────────────

    /// Rust syscall: write file content — pure Rust path (zero GIL).
    ///
    /// validate → route → VFS lock (blocking, GIL released) → CAS write → return.
    ///
    /// Returns `hit=false` for DT_PIPE/DT_STREAM (wrapper handles async IPC)
    /// or when no Rust backend is available (e.g. remote backends).
    /// Raises `InvalidPathError` on invalid paths.
    ///
    /// Metastore.put (metadata update) is handled by the Python wrapper
    /// (intermediate state — migrates to Rust metastore in PR 7).
    #[pyo3(signature = (path, zone_id, content, is_admin))]
    fn sys_write<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        content: &[u8],
        is_admin: bool,
    ) -> PyResult<SysWriteResult> {
        let miss = || {
            Ok(SysWriteResult {
                hit: false,
                content_id: None,
                post_hook_needed: false,
            })
        };

        // 1. Validate
        if let Err(msg) = validate_path_fast(path) {
            return Err(Self::raise_invalid_path(py, &msg));
        }

        // 2. Route (check write access)
        let route = match self.router.route_impl(path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache check — DT_PIPE/DT_STREAM → wrapper handles
        if let Some(entry) = self.dcache.get_entry(path) {
            match entry.entry_type {
                DT_PIPE | DT_STREAM => return miss(),
                _ => {}
            }
        }

        // 4. VFS lock (blocking write lock, GIL released during wait)
        let lock_handle = if let Some(ref lm) = self.vfs_lock {
            let timeout = self.vfs_lock_timeout_ms;
            let lm = Arc::clone(lm);
            let p = path.to_string();
            py.detach(move || lm.blocking_acquire(&p, LockMode::Write, timeout))
        } else {
            0
        };

        // Lock timeout → miss (unsafe to write without lock)
        if self.vfs_lock.is_some() && lock_handle == 0 {
            return miss();
        }

        // 5. Backend write (CasLocal — pure Rust, zero GIL)
        let result = match self.router.write_content(&route.mount_point, content) {
            Some(hash) => Ok(SysWriteResult {
                hit: true,
                content_id: Some(hash),
                post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0,
            }),
            // No Rust backend available (e.g. remote) — wrapper handles
            None => miss(),
        };

        // 6. Release VFS lock (always, even on miss)
        if lock_handle > 0 {
            if let Some(ref lm) = self.vfs_lock {
                lm.do_release(lock_handle);
            }
        }

        result
    }

    // ── sys_stat ───────────────────────────────────────────────────────

    /// Rust syscall: get file metadata (FUSE getattr hot path, zero GIL).
    ///
    /// validate → route → dcache lookup → return dict.
    /// Returns None on dcache miss or trie-resolved paths (wrapper handles).
    fn sys_stat<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        // 1. Validate
        if validate_path_fast(path).is_err() {
            return Ok(None);
        }

        // 2. Trie-resolved paths → wrapper handles
        if self.trie.lookup(path).is_some() {
            return Ok(None);
        }

        // 3. Route
        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return Ok(None);
        }

        // 4. DCache lookup (miss → wrapper handles via metastore)
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

    // ── sys_unlink ────────────────────────────────────────────────────────

    /// Rust syscall: validate + route + dcache evict for unlink.
    ///
    /// Returns entry_type of the evicted entry (0 if not in dcache).
    /// Metastore.delete stays in Python wrapper [INTERMEDIATE].
    /// DT_PIPE/DT_STREAM → returns entry_type for wrapper dispatch.
    #[pyo3(signature = (path, zone_id, is_admin))]
    fn sys_unlink(
        &self,
        py: Python<'_>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<u8> {
        // 1. Validate
        if let Err(msg) = validate_path_fast(path) {
            return Err(Self::raise_invalid_path(py, &msg));
        }

        // 2. Route (check write access)
        if self
            .router
            .route_impl(path, zone_id, is_admin, true)
            .is_err()
        {
            return Ok(0);
        }

        // 3. DCache: get entry_type then evict
        let entry_type = self
            .dcache
            .get_entry(path)
            .map(|e| e.entry_type)
            .unwrap_or(DT_REG);
        self.dcache.evict(path);

        Ok(entry_type)
    }

    // ── sys_rename ────────────────────────────────────────────────────────

    /// Rust syscall: validate + route both + dcache move for rename.
    ///
    /// Returns true if both paths validated and routed successfully.
    /// Metastore.rename stays in Python wrapper [INTERMEDIATE].
    #[pyo3(signature = (old_path, new_path, zone_id, is_admin))]
    fn sys_rename(
        &self,
        py: Python<'_>,
        old_path: &str,
        new_path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<bool> {
        // 1. Validate both
        if let Err(msg) = validate_path_fast(old_path) {
            return Err(Self::raise_invalid_path(py, &msg));
        }
        if let Err(msg) = validate_path_fast(new_path) {
            return Err(Self::raise_invalid_path(py, &msg));
        }

        // 2. Route both (check write access)
        if self
            .router
            .route_impl(old_path, zone_id, is_admin, true)
            .is_err()
        {
            return Ok(false);
        }
        if self
            .router
            .route_impl(new_path, zone_id, is_admin, true)
            .is_err()
        {
            return Ok(false);
        }

        // 3. DCache: move entry from old to new
        if let Some(entry) = self.dcache.get_entry(old_path) {
            self.dcache.evict(old_path);
            self.dcache.put(
                new_path,
                CachedEntry {
                    backend_name: entry.backend_name,
                    physical_path: entry.physical_path,
                    size: entry.size,
                    etag: entry.etag,
                    version: entry.version,
                    entry_type: entry.entry_type,
                    zone_id: entry.zone_id,
                    mime_type: entry.mime_type,
                },
            );
        }

        Ok(true)
    }

    // ── Tier 2 convenience methods ────────────────────────────────────

    /// Fast access check: validate + route + dcache existence (~100ns).
    ///
    /// Returns true if file exists in dcache and path is routable.
    /// Does NOT check metastore (dcache authoritative for hot-path).
    #[pyo3(signature = (path, zone_id, is_admin))]
    fn access(&self, path: &str, zone_id: &str, is_admin: bool) -> bool {
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

    /// List immediate children of a directory path from dcache.
    ///
    /// Returns Vec of (child_name, entry_type) tuples.
    /// Only returns entries with `parent_path/child_name` pattern.
    /// Does NOT recurse into subdirectories.
    #[pyo3(signature = (parent_path, zone_id, is_admin))]
    fn readdir(&self, parent_path: &str, zone_id: &str, is_admin: bool) -> Vec<(String, u8)> {
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
}
