//! Zone-aware PathRouter — Rust-accelerated mount table with LPM routing.
//!
//! Performs zone canonicalization + longest-prefix-match in a single call.
//! ~30ns total vs ~300ns Python (canonicalize + dict walks).
//!
//! Design: HashMap<String, MountEntry> keyed by zone-canonical mount points.
//! `route(path, zone_id)` canonicalizes, then walks from deepest to shallowest.
//!
//! Issue #1868: Kernel owns PathRouter directly. Zero PyO3 dependency.

use parking_lot::RwLock;
use std::collections::HashMap;

use crate::backend::ObjectStore;

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
pub enum RouteError {
    NotMounted(String),
    AccessDenied(String),
}

// ---------------------------------------------------------------------------
// Route result — pure Rust struct (wrapper converts to Python)
// ---------------------------------------------------------------------------

/// Route result — pure Rust. PyO3 wrapper in generated_pyo3.rs.
#[derive(Debug, Clone)]
pub struct RustRouteResult {
    pub mount_point: String,
    pub backend_path: String,
    pub readonly: bool,
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
    pub(crate) fn read_content(
        &self,
        mount_point: &str,
        content_id: &str,
        backend_path: &str,
        ctx: &crate::kernel::OperationContext,
    ) -> Option<Vec<u8>> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry
            .backend
            .as_ref()?
            .read_content(content_id, backend_path, ctx)
            .ok()
    }

    /// Write content to the storage backend attached to a mount.
    ///
    /// `content_id`: CAS=ignored, PAS=blob path (backend_path from route).
    pub(crate) fn write_content(
        &self,
        mount_point: &str,
        content: &[u8],
        content_id: &str,
        ctx: &crate::kernel::OperationContext,
    ) -> Option<crate::backend::WriteResult> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry
            .backend
            .as_ref()?
            .write_content(content, content_id, ctx)
            .ok()
    }

    /// Delete a file via the storage backend attached to a mount.
    ///
    /// PAS backends delete the physical file. CAS backends return None (no-op).
    pub(crate) fn delete_file(&self, mount_point: &str, backend_path: &str) -> Option<()> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry.backend.as_ref()?.delete_file(backend_path).ok()
    }

    /// Rename a file via the storage backend attached to a mount.
    ///
    /// PAS backends rename the physical file. CAS backends return None (no-op).
    pub(crate) fn rename_file(
        &self,
        mount_point: &str,
        old_backend_path: &str,
        new_backend_path: &str,
    ) -> Option<()> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry
            .backend
            .as_ref()?
            .rename(old_backend_path, new_backend_path)
            .ok()
    }

    /// Create a directory via the storage backend attached to a mount.
    pub(crate) fn mkdir(
        &self,
        mount_point: &str,
        backend_path: &str,
        parents: bool,
        exist_ok: bool,
    ) -> Option<()> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry
            .backend
            .as_ref()?
            .mkdir(backend_path, parents, exist_ok)
            .ok()
    }

    /// Remove a directory via the storage backend attached to a mount.
    pub(crate) fn rmdir(
        &self,
        mount_point: &str,
        backend_path: &str,
        recursive: bool,
    ) -> Option<()> {
        let mounts = self.mounts.read();
        let entry = mounts.get(mount_point)?;
        entry.backend.as_ref()?.rmdir(backend_path, recursive).ok()
    }

    // ── Mount management (called via Kernel proxy methods) ──────────────

    /// Register a mount at a zone-canonical key.
    ///
    /// Backend resolution:
    ///   - `backend` provided -> uses it directly.
    ///   - `backend` is None -> no backend (sys_read returns miss).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn add_mount(
        &self,
        mount_point: &str,
        zone_id: &str,
        readonly: bool,
        admin_only: bool,
        io_profile: &str,
        backend_name: &str,
        backend: Option<Box<dyn ObjectStore>>,
    ) -> Result<(), std::io::Error> {
        let canonical = canonicalize(mount_point, zone_id);
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
            .add_mount("/", "root", false, false, "balanced", "", None)
            .unwrap();
        router
            .add_mount("/workspace", "root", false, false, "fast", "", None)
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
            .add_mount("/", "root", false, false, "balanced", "", None)
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
            .add_mount("/system", "root", true, false, "balanced", "", None)
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
            .add_mount("/admin", "root", false, true, "balanced", "", None)
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
            .add_mount("/", "root", false, false, "balanced", "", None)
            .unwrap();
        router
            .add_mount("/shared", "zone-beta", false, false, "balanced", "", None)
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
            .add_mount("/data", "root", false, false, "balanced", "local", None)
            .unwrap();
        assert!(router.has_mount("/data", "root"));
        assert!(!router.has_mount("/data", "other"));

        let points = router.get_mount_points();
        assert_eq!(points, vec!["/root/data"]);

        assert!(router.remove_mount("/data", "root"));
        assert!(!router.has_mount("/data", "root"));
    }
}
