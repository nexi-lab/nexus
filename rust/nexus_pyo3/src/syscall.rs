//! SyscallEngine — single-FFI sys_read/sys_write planner + executor.
//!
//! Holds Arc references to DCache, PathRouter, and PathTrie.
//! A single `plan_read()` or `plan_write()` FFI call replaces 4 separate
//! Python→Rust roundtrips (validate → trie → route → dcache).
//!
//! Phase E adds `execute_read()` / `execute_write()` which combine planning
//! with CAS I/O for a complete Rust data path (dcache hit + local CAS).
//!
//! Performance target: execute_read() < 2μs (dcache hit, page-cache-hot CAS).

use crate::cas_engine::CASEngine;
use crate::cas_transport::LocalCASTransport;
use crate::dcache::{RustDCache, RustDCacheInner, DT_EXTERNAL, DT_PIPE, DT_STREAM};
use crate::dispatch::{PathTrie, PathTrieInner};
use crate::router::{RustPathRouter, RustPathRouterInner};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::HashMap;
use std::path::Path;
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

// ── SyscallEngine ───────────────────────────────────────────────────────

/// Single-FFI syscall planner holding shared refs to DCache, Router, and Trie.
///
/// Constructed once during NexusFS initialization, reused for every syscall.
/// All three inner Arcs point to the same live data as the Python-facing
/// RustDCache, RustPathRouter, and PathTrie objects.
#[pyclass]
pub struct SyscallEngine {
    dcache: Arc<RustDCacheInner>,
    router: Arc<RustPathRouterInner>,
    trie: Arc<PathTrieInner>,
    /// backend_name → CASEngine for local CAS backends (Phase E).
    cas_engines: HashMap<String, CASEngine>,
}

#[pymethods]
impl SyscallEngine {
    /// Construct from existing Python objects.  Extracts Arc refs from each.
    #[new]
    fn new(dcache: &RustDCache, router: &RustPathRouter, trie: &PathTrie) -> Self {
        Self {
            dcache: Arc::clone(&dcache.inner),
            router: Arc::clone(&router.inner),
            trie: Arc::clone(&trie.inner),
            cas_engines: HashMap::new(),
        }
    }

    /// Register a local CAS backend for full Rust I/O (Phase E).
    fn register_local_cas(
        &mut self,
        backend_name: &str,
        root_path: &str,
        fsync: bool,
    ) -> PyResult<()> {
        let transport = LocalCASTransport::new(Path::new(root_path), fsync)
            .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))?;
        self.cas_engines
            .insert(backend_name.to_string(), CASEngine::new(transport));
        Ok(())
    }

    /// Unregister a CAS backend (for unmount).
    fn unregister_cas(&mut self, backend_name: &str) -> bool {
        self.cas_engines.remove(backend_name).is_some()
    }

    /// Execute read entirely in Rust if possible (Phase E).
    ///
    /// DCached hit + local CAS → returns bytes. Otherwise returns None
    /// and Python falls through to the full Python path.
    fn execute_read<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        is_admin: bool,
    ) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let plan = self.plan_read(path, zone_id, is_admin);
        if plan.action != ACTION_DCACHE_HIT {
            return Ok(None);
        }
        let etag = match &plan.etag {
            Some(e) if !e.is_empty() => e.as_str(),
            _ => return Ok(None),
        };
        let cas = match self.cas_engines.get(&plan.backend_name) {
            Some(cas) => cas,
            None => return Ok(None),
        };
        match cas.read_content(etag) {
            Ok(data) => Ok(Some(PyBytes::new(py, &data))),
            Err(_) => Ok(None),
        }
    }

    /// Execute write's CAS portion in Rust (Phase E).
    ///
    /// Returns BLAKE3 hash if local CAS write succeeded, else None.
    fn execute_write(
        &self,
        path: &str,
        zone_id: &str,
        content: &[u8],
        is_admin: bool,
    ) -> PyResult<Option<String>> {
        let plan = self.plan_write(path, zone_id, is_admin);
        if plan.action == ACTION_ERROR {
            return Err(pyo3::exceptions::PyValueError::new_err(
                plan.error_msg.unwrap_or_default(),
            ));
        }
        if plan.action != ACTION_DCACHE_HIT {
            return Ok(None);
        }
        let cas = match self.cas_engines.get(&plan.backend_name) {
            Some(cas) => cas,
            None => return Ok(None),
        };
        match cas.write_content(content) {
            Ok(hash) => Ok(Some(hash)),
            Err(_) => Ok(None),
        }
    }

    /// Number of registered CAS backends.
    fn cas_backend_count(&self) -> usize {
        self.cas_engines.len()
    }

    /// List registered CAS backend names.
    fn cas_backend_names(&self) -> Vec<String> {
        self.cas_engines.keys().cloned().collect()
    }

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
}
