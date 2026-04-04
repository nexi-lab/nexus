//! Zone-aware PathRouter — Rust-accelerated mount table with LPM routing.
//!
//! Performs zone canonicalization + longest-prefix-match in a single call.
//! ~30ns total vs ~300ns Python (canonicalize + dict walks).
//!
//! Design: HashMap<String, MountEntry> keyed by zone-canonical mount points.
//! `route(path, zone_id)` canonicalizes, then walks from deepest to shallowest.
//!
//! Issue #1868: Kernel owns PathRouter directly. RustPathRouter wrapper removed.

use parking_lot::RwLock;
use pyo3::prelude::*;
use std::collections::HashMap;
use std::path::Path;

use crate::backend::{CasLocalBackend, ObjectStore};
use crate::generated_adapters::PyObjectStoreAdapter;

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

pub(crate) struct MountEntry {
    pub(crate) readonly: bool,
    pub(crate) admin_only: bool,
    pub(crate) io_profile: String,
    #[allow(dead_code)]
    pub(crate) backend_name: String,
    pub(crate) backend: Option<Box<dyn ObjectStore>>,
}

#[derive(Debug)]
pub(crate) enum RouteError {
    NotMounted(String),
    AccessDenied(String),
}

impl From<RouteError> for PyErr {
    fn from(e: RouteError) -> PyErr {
        match e {
            RouteError::NotMounted(msg) => pyo3::exceptions::PyValueError::new_err(msg),
            RouteError::AccessDenied(msg) => pyo3::exceptions::PyPermissionError::new_err(msg),
        }
    }
}

// ---------------------------------------------------------------------------
// Route result (still a #[pyclass] — returned from Kernel.route())
// ---------------------------------------------------------------------------

/// Route result returned to Python.
#[pyclass]
#[derive(Debug, Clone)]
pub struct RustRouteResult {
    #[pyo3(get)]
    pub mount_point: String,
    #[pyo3(get)]
    pub backend_path: String,
    #[pyo3(get)]
    pub readonly: bool,
    #[pyo3(get)]
    pub io_profile: String,
}

// ---------------------------------------------------------------------------
// PathRouter — owned directly by Kernel
// ---------------------------------------------------------------------------

pub(crate) struct PathRouter {
    mounts: RwLock<HashMap<String, MountEntry>>,
}

impl PathRouter {
    pub(crate) fn new() -> Self {
        Self {
            mounts: RwLock::new(HashMap::new()),
        }
    }

    /// Core routing logic — used by Kernel syscalls.
    pub(crate) fn route_impl(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> Result<RustRouteResult, RouteError> {
        let canonical = canonicalize(path, zone_id);
        let mounts = self.mounts.read();
        let mut current = canonical.as_str();

        loop {
            if let Some(entry) = mounts.get(current) {
                if entry.admin_only && !is_admin {
                    return Err(RouteError::AccessDenied(format!(
                        "Mount '{}' requires admin privileges",
                        current
                    )));
                }
                if entry.readonly && check_write {
                    return Err(RouteError::AccessDenied(format!(
                        "Mount '{}' is read-only",
                        current
                    )));
                }

                return Ok(RustRouteResult {
                    mount_point: current.to_string(),
                    backend_path: strip_mount_prefix(&canonical, current),
                    readonly: entry.readonly,
                    io_profile: entry.io_profile.clone(),
                });
            }

            if current == "/" {
                break;
            }
            match current.rfind('/') {
                Some(0) => current = "/",
                Some(pos) => current = &canonical[..pos],
                None => break,
            }
        }

        Err(RouteError::NotMounted(format!(
            "No mount found for path: {}",
            path
        )))
    }

    /// Read content from the storage backend attached to a mount.
    pub(crate) fn read_content(&self, mount_point: &str, etag: &str) -> Option<Vec<u8>> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry.backend.as_ref()?.read_content(etag).ok()
    }

    /// Write content to the storage backend attached to a mount.
    pub(crate) fn write_content(&self, mount_point: &str, content: &[u8]) -> Option<String> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry.backend.as_ref()?.write_content(content).ok()
    }

    // ── Mount management (called via Kernel proxy methods) ──────────────

    /// Register a mount at a zone-canonical key.
    ///
    /// Backend resolution order:
    ///   1. `local_root` provided → CasLocalBackend (pure Rust, zero GIL)
    ///   2. `py_backend` provided → PyObjectStoreAdapter (GIL on cold path)
    ///   3. Neither → no backend (sys_read returns miss)
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn add_mount(
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
    ) -> Result<(), std::io::Error> {
        let canonical = canonicalize(mount_point, zone_id);
        let backend: Option<Box<dyn ObjectStore>> = if let Some(root) = local_root {
            let b = CasLocalBackend::new(Path::new(root), fsync)?;
            Some(Box::new(b))
        } else {
            py_backend.map(|obj| -> Box<dyn ObjectStore> {
                Python::attach(|py| Box::new(PyObjectStoreAdapter::new(py, obj)))
            })
        };
        self.mounts.write().insert(
            canonical,
            MountEntry {
                readonly,
                admin_only,
                io_profile: io_profile.to_string(),
                backend_name: backend_name.to_string(),
                backend,
            },
        );
        Ok(())
    }

    /// Remove a mount.
    pub(crate) fn remove_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);
        self.mounts.write().remove(&canonical).is_some()
    }

    /// Check if a mount exists.
    pub(crate) fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);
        self.mounts.read().contains_key(&canonical)
    }

    /// List all mount points (zone-canonical).
    pub(crate) fn get_mount_points(&self) -> Vec<String> {
        let mut points: Vec<String> = self.mounts.read().keys().cloned().collect();
        points.sort();
        points
    }
}

// ---------------------------------------------------------------------------
// Pure functions (pub(crate) — used by Kernel and tests)
// ---------------------------------------------------------------------------

pub(crate) fn canonicalize(path: &str, zone_id: &str) -> String {
    let stripped = path.trim_start_matches('/');
    if stripped.is_empty() {
        format!("/{}", zone_id)
    } else {
        format!("/{}/{}", zone_id, stripped)
    }
}

#[allow(dead_code)]
pub(crate) fn strip_zone(canonical_path: &str, zone_id: &str) -> String {
    let prefix = format!("/{}", zone_id);
    if canonical_path == prefix {
        "/".to_string()
    } else if let Some(rest) = canonical_path.strip_prefix(&format!("{}/", prefix)) {
        format!("/{}", rest)
    } else {
        canonical_path.to_string()
    }
}

#[allow(dead_code)]
pub(crate) fn extract_zone(canonical_path: &str) -> (String, String) {
    let trimmed = canonical_path.trim_start_matches('/');
    match trimmed.split_once('/') {
        Some((zone, rest)) => (zone.to_string(), format!("/{}", rest)),
        None => (trimmed.to_string(), "/".to_string()),
    }
}

fn strip_mount_prefix(path: &str, mount_point: &str) -> String {
    if path == mount_point {
        String::new()
    } else if mount_point == "/" {
        path.trim_start_matches('/').to_string()
    } else {
        path[mount_point.len()..]
            .trim_start_matches('/')
            .to_string()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_canonicalize() {
        assert_eq!(
            canonicalize("/workspace/file.txt", "root"),
            "/root/workspace/file.txt"
        );
        assert_eq!(canonicalize("/", "root"), "/root");
        assert_eq!(canonicalize("/a/b/c", "zone-1"), "/zone-1/a/b/c");
    }

    #[test]
    fn test_strip_zone() {
        assert_eq!(
            strip_zone("/root/workspace/file.txt", "root"),
            "/workspace/file.txt"
        );
        assert_eq!(strip_zone("/root", "root"), "/");
        assert_eq!(strip_zone("/zone-1/a/b", "zone-1"), "/a/b");
    }

    #[test]
    fn test_extract_zone() {
        assert_eq!(
            extract_zone("/root/workspace/file.txt"),
            ("root".into(), "/workspace/file.txt".into())
        );
        assert_eq!(extract_zone("/root"), ("root".into(), "/".into()));
    }

    #[test]
    fn test_strip_mount_prefix() {
        assert_eq!(
            strip_mount_prefix("/root/workspace/data/file.txt", "/root/workspace"),
            "data/file.txt"
        );
        assert_eq!(strip_mount_prefix("/root/workspace", "/root/workspace"), "");
        assert_eq!(strip_mount_prefix("/root/a/b", "/root"), "a/b");
    }

    #[test]
    fn test_route_basic() {
        let router = PathRouter::new();
        router
            .add_mount("/", "root", false, false, "balanced", "", None, false, None)
            .unwrap();
        router
            .add_mount(
                "/workspace",
                "root",
                false,
                false,
                "fast",
                "",
                None,
                false,
                None,
            )
            .unwrap();

        let result = router
            .route_impl("/workspace/file.txt", "root", false, false)
            .unwrap();
        assert_eq!(result.mount_point, "/root/workspace");
        assert_eq!(result.backend_path, "file.txt");
        assert_eq!(result.io_profile, "fast");
    }

    #[test]
    fn test_route_root_fallback() {
        let router = PathRouter::new();
        router
            .add_mount("/", "root", false, false, "balanced", "", None, false, None)
            .unwrap();

        let result = router
            .route_impl("/unknown/path", "root", false, false)
            .unwrap();
        assert_eq!(result.mount_point, "/root");
        assert_eq!(result.backend_path, "unknown/path");
    }

    #[test]
    fn test_route_readonly() {
        let router = PathRouter::new();
        router
            .add_mount(
                "/system", "root", true, false, "balanced", "", None, false, None,
            )
            .unwrap();

        let err = router
            .route_impl("/system/config", "root", false, true)
            .unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));
    }

    #[test]
    fn test_route_admin_only() {
        let router = PathRouter::new();
        router
            .add_mount(
                "/admin", "root", false, true, "balanced", "", None, false, None,
            )
            .unwrap();

        let err = router
            .route_impl("/admin/secrets", "root", false, false)
            .unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));

        let result = router
            .route_impl("/admin/secrets", "root", true, false)
            .unwrap();
        assert_eq!(result.mount_point, "/root/admin");
    }

    #[test]
    fn test_cross_zone() {
        let router = PathRouter::new();
        router
            .add_mount("/", "root", false, false, "balanced", "", None, false, None)
            .unwrap();
        router
            .add_mount(
                "/shared",
                "zone-beta",
                false,
                false,
                "balanced",
                "",
                None,
                false,
                None,
            )
            .unwrap();

        let result = router
            .route_impl("/workspace/file.txt", "root", false, false)
            .unwrap();
        assert_eq!(result.mount_point, "/root");

        let result = router
            .route_impl("/shared/doc.txt", "zone-beta", false, false)
            .unwrap();
        assert_eq!(result.mount_point, "/zone-beta/shared");
    }

    #[test]
    fn test_mount_management() {
        let router = PathRouter::new();
        router
            .add_mount(
                "/data", "root", false, false, "balanced", "local", None, false, None,
            )
            .unwrap();
        assert!(router.has_mount("/data", "root"));
        assert!(!router.has_mount("/data", "other"));

        let points = router.get_mount_points();
        assert_eq!(points, vec!["/root/data"]);

        assert!(router.remove_mount("/data", "root"));
        assert!(!router.has_mount("/data", "root"));
    }
}
