//! Zone-aware PathRouter — Rust-accelerated mount table with LPM routing.
//!
//! Performs zone canonicalization + longest-prefix-match in a single call.
//! ~30ns total vs ~300ns Python (canonicalize + dict walks).
//!
//! Design: HashMap<String, MountEntry> keyed by zone-canonical mount points.
//! `route(path, zone_id)` canonicalizes, then walks from deepest to shallowest.

use pyo3::prelude::*;
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct MountEntry {
    readonly: bool,
    admin_only: bool,
    io_profile: String,
}

#[derive(Debug)]
enum RouteError {
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
// Route result
// ---------------------------------------------------------------------------

/// Route result returned to Python. The actual backend ObjectStoreABC lives
/// in Python; Rust returns the mount_point key so Python looks up the backend.
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
// PathRouter
// ---------------------------------------------------------------------------

#[pyclass]
pub struct RustPathRouter {
    mounts: HashMap<String, MountEntry>,
}

#[pymethods]
impl RustPathRouter {
    #[new]
    fn new() -> Self {
        Self {
            mounts: HashMap::new(),
        }
    }

    /// Register a mount at a zone-canonical key.
    fn add_mount(
        &mut self,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
    ) {
        let canonical = canonicalize(mount_point, zone_id);
        self.mounts.insert(
            canonical,
            MountEntry {
                readonly,
                admin_only,
                io_profile: io_profile.to_string(),
            },
        );
    }

    /// Remove a mount.
    fn remove_mount(&mut self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);
        self.mounts.remove(&canonical).is_some()
    }

    /// Zone-canonical LPM routing. Raises ValueError/PermissionError.
    fn route(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> PyResult<RustRouteResult> {
        self.route_impl(path, zone_id, is_admin, check_write)
            .map_err(Into::into)
    }

    /// Canonicalize a path with zone prefix.
    #[staticmethod]
    fn canonicalize(path: &str, zone_id: &str) -> String {
        canonicalize(path, zone_id)
    }

    /// Strip zone prefix to get metastore-relative path.
    #[staticmethod]
    fn strip_zone(canonical_path: &str, zone_id: &str) -> String {
        strip_zone(canonical_path, zone_id)
    }

    /// Extract (zone_id, relative_path) from canonical path.
    #[staticmethod]
    fn extract_zone(canonical_path: &str) -> (String, String) {
        extract_zone(canonical_path)
    }

    /// Check if a mount exists.
    fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {
        let canonical = canonicalize(mount_point, zone_id);
        self.mounts.contains_key(&canonical)
    }

    /// List all mount points (zone-canonical).
    fn get_mount_points(&self) -> Vec<String> {
        let mut points: Vec<String> = self.mounts.keys().cloned().collect();
        points.sort();
        points
    }
}

// Core routing logic — testable without Python runtime.
// Compiler inlines route_impl into route() — zero overhead.
impl RustPathRouter {
    fn route_impl(
        &self,
        path: &str,
        zone_id: &str,
        is_admin: bool,
        check_write: bool,
    ) -> Result<RustRouteResult, RouteError> {
        let canonical = canonicalize(path, zone_id);
        let mut current = canonical.as_str();

        loop {
            if let Some(entry) = self.mounts.get(current) {
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
}

// ---------------------------------------------------------------------------
// Pure functions
// ---------------------------------------------------------------------------

fn canonicalize(path: &str, zone_id: &str) -> String {
    let stripped = path.trim_start_matches('/');
    if stripped.is_empty() {
        format!("/{}", zone_id)
    } else {
        format!("/{}/{}", zone_id, stripped)
    }
}

fn strip_zone(canonical_path: &str, zone_id: &str) -> String {
    let prefix = format!("/{}", zone_id);
    if canonical_path == prefix {
        "/".to_string()
    } else if let Some(rest) = canonical_path.strip_prefix(&format!("{}/", prefix)) {
        format!("/{}", rest)
    } else {
        canonical_path.to_string()
    }
}

fn extract_zone(canonical_path: &str) -> (String, String) {
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
        path[mount_point.len()..].trim_start_matches('/').to_string()
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
        assert_eq!(canonicalize("/workspace/file.txt", "root"), "/root/workspace/file.txt");
        assert_eq!(canonicalize("/", "root"), "/root");
        assert_eq!(canonicalize("/a/b/c", "zone-1"), "/zone-1/a/b/c");
    }

    #[test]
    fn test_strip_zone() {
        assert_eq!(strip_zone("/root/workspace/file.txt", "root"), "/workspace/file.txt");
        assert_eq!(strip_zone("/root", "root"), "/");
        assert_eq!(strip_zone("/zone-1/a/b", "zone-1"), "/a/b");
    }

    #[test]
    fn test_extract_zone() {
        assert_eq!(extract_zone("/root/workspace/file.txt"), ("root".into(), "/workspace/file.txt".into()));
        assert_eq!(extract_zone("/root"), ("root".into(), "/".into()));
    }

    #[test]
    fn test_strip_mount_prefix() {
        assert_eq!(strip_mount_prefix("/root/workspace/data/file.txt", "/root/workspace"), "data/file.txt");
        assert_eq!(strip_mount_prefix("/root/workspace", "/root/workspace"), "");
        assert_eq!(strip_mount_prefix("/root/a/b", "/root"), "a/b");
    }

    #[test]
    fn test_route_basic() {
        let mut router = RustPathRouter::new();
        router.add_mount("/", "root", false, false, "balanced");
        router.add_mount("/workspace", "root", false, false, "fast");

        let result = router.route_impl("/workspace/file.txt", "root", false, false).unwrap();
        assert_eq!(result.mount_point, "/root/workspace");
        assert_eq!(result.backend_path, "file.txt");
        assert_eq!(result.io_profile, "fast");
    }

    #[test]
    fn test_route_root_fallback() {
        let mut router = RustPathRouter::new();
        router.add_mount("/", "root", false, false, "balanced");

        let result = router.route_impl("/unknown/path", "root", false, false).unwrap();
        assert_eq!(result.mount_point, "/root");
        assert_eq!(result.backend_path, "unknown/path");
    }

    #[test]
    fn test_route_readonly() {
        let mut router = RustPathRouter::new();
        router.add_mount("/system", "root", true, false, "balanced");

        let err = router.route_impl("/system/config", "root", false, true).unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));
    }

    #[test]
    fn test_route_admin_only() {
        let mut router = RustPathRouter::new();
        router.add_mount("/admin", "root", false, true, "balanced");

        let err = router.route_impl("/admin/secrets", "root", false, false).unwrap_err();
        assert!(matches!(err, RouteError::AccessDenied(_)));

        let result = router.route_impl("/admin/secrets", "root", true, false).unwrap();
        assert_eq!(result.mount_point, "/root/admin");
    }

    #[test]
    fn test_cross_zone() {
        let mut router = RustPathRouter::new();
        router.add_mount("/", "root", false, false, "balanced");
        router.add_mount("/shared", "zone-beta", false, false, "balanced");

        let result = router.route_impl("/workspace/file.txt", "root", false, false).unwrap();
        assert_eq!(result.mount_point, "/root");

        let result = router.route_impl("/shared/doc.txt", "zone-beta", false, false).unwrap();
        assert_eq!(result.mount_point, "/zone-beta/shared");
    }
}
