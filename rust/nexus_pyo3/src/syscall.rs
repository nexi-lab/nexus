//! SyscallEngine — single-FFI syscall planner + executor.
//!
//! Holds Arc references to DCache, PathRouter, PathTrie, and VFSLockManager.
//! A single `plan_read()` or `plan_write()` FFI call replaces 4 separate
//! Python→Rust roundtrips (validate → trie → route → dcache).
//!
//! Phase E adds `sys_read()` / `sys_write()` which combine planning
//! with CAS I/O for a complete Rust data path (dcache hit + local CAS).
//!
//! Phase H adds `sys_stat()` (full Rust on dcache hit), `plan_stat()`,
//! `plan_unlink()`, `plan_rename()`, and hook counts for all Tier 1 ops.
//!
//! Performance target: sys_stat() < 500ns (dcache hit, no hooks).

use crate::dcache::{RustDCache, RustDCacheInner, DT_DIR, DT_EXTERNAL, DT_PIPE, DT_STREAM};
use crate::dispatch::{PathTrie, PathTrieInner};
use crate::lock::{LockMode, VFSLockManager, VFSLockManagerInner};
use crate::router::{RustPathRouter, RustPathRouterInner};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

// ── Action constants ────────────────────────────────────────────────────

/// Normal read/write: dcache hit, routing resolved. Use plan fields directly.
pub const ACTION_DCACHE_HIT: u8 = 0;
/// PathTrie resolver matched — Python must run the resolver.
pub const ACTION_RESOLVED: u8 = 1;
/// DT_PIPE entry — delegate to PipeManager.
pub const ACTION_PIPE: u8 = 2;
/// DT_STREAM entry — delegate to StreamManager.
pub const ACTION_STREAM: u8 = 3;
/// DT_EXTERNAL entry — delegate to external backend.
pub const ACTION_EXTERNAL: u8 = 4;
/// DCache miss — fall back to full Python path.
pub const ACTION_CACHE_MISS: u8 = 5;
/// Validation or routing error.
pub const ACTION_ERROR: u8 = 6;

// ── ReadPlan ────────────────────────────────────────────────────────────

/// Result of plan_read(): tells Python what to do next without additional FFI.
#[pyclass(get_all)]
#[derive(Debug, Clone)]
pub struct ReadPlan {
    /// Action type (ACTION_* constants).
    pub action: u8,
    /// Zone-canonical mount point (from router).
    pub mount_point: String,
    /// Backend-relative path (from router).
    pub backend_path: String,
    /// Content hash for CAS lookup (from dcache).
    pub etag: Option<String>,
    /// Backend name for driver coordinator lookup.
    pub backend_name: String,
    /// Whether mount is read-only.
    pub readonly: bool,
    /// I/O profile hint ("fast", "balanced", etc.).
    pub io_profile: String,
    /// Entry type from dcache (DT_REG, DT_PIPE, etc.).
    pub entry_type: u8,
    /// Validated/normalized path.
    pub validated_path: String,
    /// Resolver index (only for ACTION_RESOLVED).
    pub resolver_idx: i64,
    /// Error message (only for ACTION_ERROR).
    pub error_msg: Option<String>,
}

// ── WritePlan ───────────────────────────────────────────────────────────

/// Result of plan_write(): tells Python what to do next.
#[pyclass(get_all)]
#[derive(Debug, Clone)]
pub struct WritePlan {
    /// Action type (ACTION_* constants).
    pub action: u8,
    /// Zone-canonical mount point.
    pub mount_point: String,
    /// Backend-relative path.
    pub backend_path: String,
    /// Existing content hash (for CAS update).
    pub etag: Option<String>,
    /// Backend name.
    pub backend_name: String,
    /// Whether mount is read-only (should always be false for write plans).
    pub readonly: bool,
    /// I/O profile hint.
    pub io_profile: String,
    /// Entry type from dcache.
    pub entry_type: u8,
    /// Validated/normalized path.
    pub validated_path: String,
    /// Resolver index (only for ACTION_RESOLVED).
    pub resolver_idx: i64,
    /// Error message (only for ACTION_ERROR).
    pub error_msg: Option<String>,
    /// Metadata version from dcache (for optimistic concurrency).
    pub version: u32,
}

// ── StatPlan (Phase H) ─────────────────────────────────────────────────

/// Result of plan_stat(): single-FFI stat planning.
///
/// When action == ACTION_DCACHE_HIT, all metadata fields are populated
/// from the Rust dcache — Python can construct the response dict without
/// any additional lookups.
#[pyclass(get_all)]
#[derive(Debug, Clone)]
pub struct StatPlan {
    /// Action type (ACTION_* constants).
    pub action: u8,
    /// Validated/normalized path.
    pub validated_path: String,
    /// Backend name (from dcache).
    pub backend_name: String,
    /// Physical path (from dcache).
    pub physical_path: String,
    /// File size (from dcache).
    pub size: u64,
    /// Content hash (from dcache).
    pub etag: Option<String>,
    /// MIME type (from dcache).
    pub mime_type: Option<String>,
    /// Entry type (DT_REG, DT_DIR, etc.).
    pub entry_type: u8,
    /// Metadata version.
    pub version: u32,
    /// Zone ID.
    pub zone_id: Option<String>,
    /// Whether path is a directory (entry_type == DT_DIR).
    pub is_directory: bool,
    /// Resolver index (only for ACTION_RESOLVED).
    pub resolver_idx: i64,
    /// Error message (only for ACTION_ERROR).
    pub error_msg: Option<String>,
}

// ── RenamePlan (Phase H) ───────────────────────────────────────────────

/// Result of plan_rename(): validates + routes both paths in a single FFI call.
///
/// Provides routing info for both old and new paths. Python uses these
/// to perform the actual metastore rename under VFS lock.
#[pyclass(get_all)]
#[derive(Debug, Clone)]
pub struct RenamePlan {
    /// Action type (ACTION_* constants).
    pub action: u8,
    /// Validated old path.
    pub old_path: String,
    /// Validated new path.
    pub new_path: String,
    /// Route result for old path: zone-canonical mount point.
    pub old_mount_point: String,
    /// Route result for old path: backend-relative path.
    pub old_backend_path: String,
    /// Route result for new path: zone-canonical mount point.
    pub new_mount_point: String,
    /// Route result for new path: backend-relative path.
    pub new_backend_path: String,
    /// Whether old path mount is read-only.
    pub old_readonly: bool,
    /// Whether new path mount is read-only.
    pub new_readonly: bool,
    /// Entry type of source (from dcache, 0 if miss).
    pub entry_type: u8,
    /// Error message (only for ACTION_ERROR).
    pub error_msg: Option<String>,
}

// ── SyscallEngine ───────────────────────────────────────────────────────

/// Single-FFI syscall facade holding shared refs to DCache, Router, Trie, and VFS Lock.
///
/// Constructed once during NexusFS initialization, reused for every syscall.
/// All inner Arcs point to the same live data as the Python-facing objects.
///
/// Phase G: VFS lock integration + hook counters for read/write.
/// Phase H: sys_stat + plan_stat/plan_unlink/plan_rename + hook counters
///          for stat/delete/rename.
#[pyclass]
pub struct SyscallEngine {
    dcache: Arc<RustDCacheInner>,
    router: Arc<RustPathRouterInner>,
    trie: Arc<PathTrieInner>,
    vfs_lock: Option<Arc<VFSLockManagerInner>>,
    read_hook_count: AtomicU64,
    write_hook_count: AtomicU64,
    stat_hook_count: AtomicU64,
    delete_hook_count: AtomicU64,
    rename_hook_count: AtomicU64,
}

#[pymethods]
impl SyscallEngine {
    /// Construct from existing Python objects.  Extracts Arc refs from each.
    ///
    /// `vfs_lock` is optional for backward compatibility (tests without lock manager).
    #[new]
    #[pyo3(signature = (dcache, router, trie, vfs_lock=None))]
    fn new(
        dcache: &RustDCache,
        router: &RustPathRouter,
        trie: &PathTrie,
        vfs_lock: Option<&VFSLockManager>,
    ) -> Self {
        Self {
            dcache: Arc::clone(&dcache.inner),
            router: Arc::clone(&router.inner),
            trie: Arc::clone(&trie.inner),
            vfs_lock: vfs_lock.map(|lm| Arc::clone(&lm.inner)),
            read_hook_count: AtomicU64::new(0),
            write_hook_count: AtomicU64::new(0),
            stat_hook_count: AtomicU64::new(0),
            delete_hook_count: AtomicU64::new(0),
            rename_hook_count: AtomicU64::new(0),
        }
    }

    /// Update hook count for an operation.
    /// Called by Python KernelDispatch when hooks are registered/unregistered.
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

    // ── sys_read (Phase G) ──────────────────────────────────────────────

    /// Rust syscall: read file content.
    ///
    /// Checks hook count → plans → acquires VFS read lock → CAS / backend I/O → releases lock.
    /// Returns None if hooks are present, dcache miss, or I/O fails → Python fallback.
    fn sys_read<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<Option<Bound<'py, PyBytes>>> {
        // 0. Hook check — if hooks registered, Python must run them
        if self.read_hook_count.load(Ordering::Relaxed) > 0 {
            return Ok(None);
        }

        let plan = self.plan_read(path, zone_id, is_admin);
        if plan.action != ACTION_DCACHE_HIT {
            return Ok(None);
        }
        let etag = match &plan.etag {
            Some(e) if !e.is_empty() => e.as_str(),
            _ => return Ok(None),
        };

        // 1. VFS read lock (non-blocking try-acquire)
        let lock_handle = self
            .vfs_lock
            .as_ref()
            .map(|lm| lm.try_acquire(path, LockMode::Read));
        if let Some(0) = lock_handle {
            // Lock contention — fall back to Python (which has blocking/timeout)
            return Ok(None);
        }

        // 2. CAS fast path (pure Rust, ~2μs)
        let result = if let Some(data) = self.router.read_cas(&plan.mount_point, etag) {
            Ok(Some(PyBytes::new(py, &data)))
        } else if let Some(py_result) = self.router.read_backend(py, &plan.mount_point, etag) {
            // 3. Python backend callback (Phase F, ~12-15μs)
            if let Ok(data) = py_result.extract::<Vec<u8>>(py) {
                Ok(Some(PyBytes::new(py, &data)))
            } else {
                Ok(None)
            }
        } else {
            // 4. Both failed → None → Python full path fallback
            Ok(None)
        };

        // 5. Release VFS lock
        if let Some(handle) = lock_handle {
            if handle > 0 {
                if let Some(lm) = &self.vfs_lock {
                    lm.do_release(handle);
                }
            }
        }

        result
    }

    // ── sys_write (Phase G) ─────────────────────────────────────────────

    /// Rust syscall: write file content.
    ///
    /// Checks hook count → plans → acquires VFS write lock → CAS / backend I/O → releases lock.
    /// Returns content_id on success, None for Python fallback.
    fn sys_write(
        &self,
        py: Python<'_>,
        path: &str,
        zone_id: &str,
        content: &[u8],
        is_admin: bool,
    ) -> PyResult<Option<String>> {
        // 0. Hook check
        if self.write_hook_count.load(Ordering::Relaxed) > 0 {
            return Ok(None);
        }

        let plan = self.plan_write(path, zone_id, is_admin);
        if plan.action == ACTION_ERROR {
            return Err(pyo3::exceptions::PyValueError::new_err(
                plan.error_msg.unwrap_or_default(),
            ));
        }
        if plan.action != ACTION_DCACHE_HIT {
            return Ok(None);
        }

        // 1. VFS write lock (non-blocking try-acquire)
        let lock_handle = self
            .vfs_lock
            .as_ref()
            .map(|lm| lm.try_acquire(path, LockMode::Write));
        if let Some(0) = lock_handle {
            return Ok(None);
        }

        // 2. CAS fast path (pure Rust)
        let result = if let Some(hash) = self.router.write_cas(&plan.mount_point, content) {
            Ok(Some(hash))
        } else if let Some(write_result) = self.router.write_backend(py, &plan.mount_point, content)
        {
            // 3. Python backend callback (Phase F)
            if let Ok(content_id) = write_result.getattr(py, "content_id") {
                if let Ok(s) = content_id.extract::<String>(py) {
                    Ok(Some(s))
                } else {
                    Ok(None)
                }
            } else {
                Ok(None)
            }
        } else {
            Ok(None)
        };

        // 4. Release VFS lock
        if let Some(handle) = lock_handle {
            if handle > 0 {
                if let Some(lm) = &self.vfs_lock {
                    lm.do_release(handle);
                }
            }
        }

        result
    }

    // ── sys_stat (Phase H) ──────────────────────────────────────────────

    /// Rust syscall: get file metadata (FUSE getattr hot path).
    ///
    /// On dcache hit + no hooks: returns a Python dict matching sys_stat() format.
    /// Returns None on dcache miss, hooks present, or implicit directories.
    ///
    /// Performance: ~200ns (dcache hit → dict construction), vs ~30μs Python path.
    fn sys_stat<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        // 0. Hook check
        if self.stat_hook_count.load(Ordering::Relaxed) > 0 {
            return Ok(None);
        }

        // 1. Validate path (allow root for stat)
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

        // 2. PathTrie resolver check — virtual paths need Python
        if self.trie.lookup(path).is_some() {
            return Ok(None);
        }

        // 3. Router LPM (read-only check, no write access needed)
        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return Ok(None);
        }

        // 4. DCache lookup
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => return Ok(None),
        };

        // 5. Build stat dict from dcache entry
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

        // mime_type: default based on entry_type
        let mime = entry.mime_type.as_deref().unwrap_or(if is_dir {
            "inode/directory"
        } else {
            "application/octet-stream"
        });
        dict.set_item("mime_type", mime)?;

        // Timestamps: None from dcache (Python path fills these from FileMetadata)
        dict.set_item("created_at", py.None())?;
        dict.set_item("modified_at", py.None())?;

        dict.set_item("is_directory", is_dir)?;
        dict.set_item("entry_type", entry.entry_type)?;

        // Mode: dirs=0o755, files=0o644
        dict.set_item("mode", if is_dir { 0o755u32 } else { 0o644u32 })?;

        dict.set_item("version", entry.version)?;
        dict.set_item("zone_id", entry.zone_id.as_deref())?;

        Ok(Some(dict))
    }

    // ── plan_read ───────────────────────────────────────────────────────

    /// Plan a read operation in a single FFI call.
    ///
    /// Performs: validate → trie lookup → router LPM → dcache lookup.
    /// Returns a ReadPlan telling Python what to do next.
    fn plan_read(&self, path: &str, zone_id: &str, is_admin: bool) -> ReadPlan {
        // 1. Basic validation
        if let Err(msg) = validate_path_fast(path) {
            return ReadPlan::error(msg);
        }

        // 2. PathTrie resolver check
        if let Some(idx) = self.trie.lookup(path) {
            return ReadPlan::resolved(path, idx);
        }

        // 3. Router LPM
        let route = match self.router.route_impl(path, zone_id, is_admin, false) {
            Ok(r) => r,
            Err(_) => {
                // Not mounted — Python slow path will handle this
                return ReadPlan::cache_miss(path);
            }
        };

        // 4. DCache lookup
        match self.dcache.get_entry(path) {
            Some(entry) => {
                let action = match entry.entry_type {
                    DT_PIPE => ACTION_PIPE,
                    DT_STREAM => ACTION_STREAM,
                    DT_EXTERNAL => ACTION_EXTERNAL,
                    _ => ACTION_DCACHE_HIT,
                };
                ReadPlan {
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
                }
            }
            None => ReadPlan::cache_miss(path),
        }
    }

    // ── plan_write ──────────────────────────────────────────────────────

    /// Plan a write operation in a single FFI call.
    ///
    /// Performs: validate → trie lookup → router LPM (check_write=true) → dcache lookup.
    /// Returns a WritePlan telling Python what to do next.
    fn plan_write(&self, path: &str, zone_id: &str, is_admin: bool) -> WritePlan {
        // 1. Basic validation
        if let Err(msg) = validate_path_fast(path) {
            return WritePlan::error(msg);
        }

        // 2. PathTrie resolver check
        if let Some(idx) = self.trie.lookup(path) {
            return WritePlan::resolved(path, idx);
        }

        // 3. Router LPM (check_write = true)
        let route = match self.router.route_impl(path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(_) => {
                return WritePlan::cache_miss(path);
            }
        };

        // 4. DCache lookup
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

    // ── plan_stat (Phase H) ─────────────────────────────────────────────

    /// Plan a stat operation in a single FFI call.
    ///
    /// Returns metadata from dcache if available. Python uses this to
    /// skip metadata.get() on dcache hit.
    fn plan_stat(&self, path: &str, zone_id: &str, is_admin: bool) -> StatPlan {
        // 1. Validate (allow root — stat("/") is valid)
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

        // 2. PathTrie resolver check
        if let Some(idx) = self.trie.lookup(path) {
            return StatPlan::resolved(path, idx);
        }

        // 3. Router LPM (read-only)
        if self
            .router
            .route_impl(path, zone_id, is_admin, false)
            .is_err()
        {
            return StatPlan::cache_miss(path);
        }

        // 4. DCache lookup — populate all metadata fields
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

    // ── plan_unlink (Phase H) ───────────────────────────────────────────

    /// Plan an unlink (delete) operation in a single FFI call.
    ///
    /// Validates path → router LPM (check_write=true) → dcache entry_type.
    /// Reuses WritePlan since the fields are identical.
    fn plan_unlink(&self, path: &str, zone_id: &str, is_admin: bool) -> WritePlan {
        // Delegate to plan_write — same validation + routing + dcache needs
        self.plan_write(path, zone_id, is_admin)
    }

    // ── plan_rename (Phase H) ───────────────────────────────────────────

    /// Plan a rename operation in a single FFI call.
    ///
    /// Validates and routes BOTH paths. Returns RenamePlan with dual routing info.
    /// Python uses this to avoid 2 separate route() calls + 2 validations.
    fn plan_rename(
        &self,
        old_path: &str,
        new_path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> RenamePlan {
        // 1. Validate both paths
        if let Err(msg) = validate_path_fast(old_path) {
            return RenamePlan::error(msg);
        }
        if let Err(msg) = validate_path_fast(new_path) {
            return RenamePlan::error(msg);
        }

        // 2. Route old path (check_write = true)
        let old_route = match self.router.route_impl(old_path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(e) => {
                return RenamePlan::error(format!("Old path routing failed: {e:?}"));
            }
        };

        // 3. Route new path (check_write = true)
        let new_route = match self.router.route_impl(new_path, zone_id, is_admin, true) {
            Ok(r) => r,
            Err(e) => {
                return RenamePlan::error(format!("New path routing failed: {e:?}"));
            }
        };

        // 4. DCache lookup for source entry_type (optional, informational)
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

// ── ReadPlan constructors ───────────────────────────────────────────────

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

// ── WritePlan constructors ──────────────────────────────────────────────

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

// ── StatPlan constructors (Phase H) ─────────────────────────────────────

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

// ── RenamePlan constructors (Phase H) ───────────────────────────────────

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

/// Minimal path validation (mirrors Python validate_path hot path).
/// Returns Ok(()) or Err(error_message).
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
    // Check for parent directory traversal
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
