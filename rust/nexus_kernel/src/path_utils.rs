//! Pure path utility functions — Rust-accelerated replacements for
//! `src/nexus/core/path_utils.py` and mount_table zone helpers.
//!
//! All functions are stateless and side-effect-free.  On the syscall hot
//! path, Rust shaves ~1μs Python string ops down to ~50ns per call.
//!
//! Python wrappers use `# RUST_FALLBACK` markers so that `grep RUST_FALLBACK`
//! finds every fallback site when we eventually remove them.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// ---------------------------------------------------------------------------
// Path splitting / parent / ancestors
// ---------------------------------------------------------------------------

/// Split a virtual path into component parts.
///
/// ``split_path("/a/b/c.txt")`` → ``["a", "b", "c.txt"]``
#[pyfunction]
pub fn split_path(path: &str) -> Vec<String> {
    if path.is_empty() || path == "/" {
        return Vec::new();
    }
    path.trim_matches('/')
        .split('/')
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

/// Get the parent directory of *path*, or None for root.
///
/// ``get_parent("/a/b/c.txt")`` → ``"/a/b"``
/// ``get_parent("/a")`` → ``"/"``
/// ``get_parent("/")`` → ``None``
#[pyfunction]
pub fn get_parent(path: &str) -> Option<String> {
    let parts = split_path(path);
    if parts.is_empty() {
        return None;
    }
    if parts.len() < 2 {
        return Some("/".to_string());
    }
    Some(format!("/{}", parts[..parts.len() - 1].join("/")))
}

/// Get all ancestor paths from most specific to least specific.
///
/// ``get_ancestors("/a/b/c.txt")`` → ``["/a/b/c.txt", "/a/b", "/a"]``
#[pyfunction]
pub fn get_ancestors(path: &str) -> Vec<String> {
    let parts = split_path(path);
    if parts.is_empty() {
        return Vec::new();
    }
    (1..=parts.len())
        .rev()
        .map(|i| format!("/{}", parts[..i].join("/")))
        .collect()
}

/// Get (child_path, parent_path) tuples for the full hierarchy.
///
/// ``get_parent_chain("/a/b/c.txt")`` → ``[("/a/b/c.txt", "/a/b"), ("/a/b", "/a")]``
#[pyfunction]
pub fn get_parent_chain(path: &str) -> Vec<(String, String)> {
    let parts = split_path(path);
    if parts.len() < 2 {
        return Vec::new();
    }
    (2..=parts.len())
        .rev()
        .map(|i| {
            let child = format!("/{}", parts[..i].join("/"));
            let parent = format!("/{}", parts[..i - 1].join("/"));
            (child, parent)
        })
        .collect()
}

/// Return the parent directory of *path*, or None for root.
///
/// Simpler than ``get_parent`` — uses rfind('/') for speed.
/// ``parent_path("/a/b/c")`` → ``"/a/b"``
/// ``parent_path("/a")`` → ``"/"``
/// ``parent_path("/")`` → ``None``
#[pyfunction]
pub fn parent_path(path: &str) -> Option<String> {
    if path == "/" {
        return None;
    }
    let trimmed = path.trim_end_matches('/');
    match trimmed.rfind('/') {
        Some(0) => Some("/".to_string()),
        Some(pos) => Some(trimmed[..pos].to_string()),
        None => None,
    }
}

// ---------------------------------------------------------------------------
// Validation + normalization
// ---------------------------------------------------------------------------

/// Validate and normalize a virtual path with security checks.
///
/// Raises ``ValueError`` for invalid paths (maps to Python ``InvalidPathError``
/// which the caller catches and re-raises).
#[pyfunction]
#[pyo3(signature = (path, allow_root=false))]
pub fn validate_path(path: &str, allow_root: bool) -> PyResult<String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err(PyValueError::new_err(
            "Path cannot be empty or whitespace-only",
        ));
    }

    // Root "/" check
    if trimmed == "/" && !allow_root {
        return Err(PyValueError::new_err(
            "Root path '/' not allowed for file operations. Use list('/') for directory listings.",
        ));
    }

    // Ensure starts with /
    let mut result = if trimmed.starts_with('/') {
        trimmed.to_string()
    } else {
        format!("/{}", trimmed)
    };

    // Collapse consecutive slashes
    result = collapse_slashes(&result);

    // Remove trailing slash (except for root)
    if result.len() > 1 {
        result = result.trim_end_matches('/').to_string();
    }

    // Invalid character check
    for ch in result.chars() {
        if ch == '\0' || ch == '\n' || ch == '\r' || ch == '\t' {
            return Err(PyValueError::new_err(format!(
                "Path contains invalid character: {:?}",
                ch
            )));
        }
    }

    // Component whitespace check
    for part in result.split('/') {
        if !part.is_empty() && part != part.trim() {
            return Err(PyValueError::new_err(format!(
                "Path component '{}' has leading/trailing whitespace. \
                 Path components must not contain spaces at start/end.",
                part
            )));
        }
    }

    // Parent directory traversal
    if result.contains("..") {
        return Err(PyValueError::new_err("Path contains '..' segments"));
    }

    Ok(result)
}

/// Normalize virtual path: absolute, collapse ``//``, resolve ``.`` / ``..``.
///
/// Raises ``ValueError`` if path is not absolute or traversal detected.
#[pyfunction]
pub fn normalize_path(path: &str) -> PyResult<String> {
    if !path.starts_with('/') {
        return Err(PyValueError::new_err(format!(
            "Path must be absolute: {}",
            path
        )));
    }
    let normalized = normpath(path);
    if !normalized.starts_with('/') {
        return Err(PyValueError::new_err(format!(
            "Path traversal detected: {}",
            path
        )));
    }
    Ok(normalized)
}

// ---------------------------------------------------------------------------
// Glob matching
// ---------------------------------------------------------------------------

/// Check if *path* matches a glob pattern (``*``, ``**``, ``?``).
#[pyfunction]
pub fn path_matches_pattern(path: &str, pattern: &str) -> bool {
    match compile_glob(pattern) {
        Some(re) => re.is_match(path),
        // Regex compilation failed (e.g., non-UTF8 chars) — fall back to
        // exact string equality (matches Python re behavior for literals).
        None => path == pattern,
    }
}

// ---------------------------------------------------------------------------
// Internal path scoping
// ---------------------------------------------------------------------------

/// Strip internal zone/tenant/user prefix from a storage path.
#[pyfunction]
pub fn unscope_internal_path(path: &str) -> String {
    let trimmed = path.trim_start_matches('/');
    let parts: Vec<&str> = trimmed.split('/').collect();
    let skip;
    if parts.is_empty() {
        return path.to_string();
    }
    if parts[0].starts_with("tenant:") {
        if parts.len() > 1 && parts[1].starts_with("user:") {
            skip = 2;
        } else {
            skip = 1;
        }
    } else if parts[0] == "zone" && parts.len() >= 2 {
        if parts.len() > 2 && parts[2].starts_with("user:") {
            skip = 3;
        } else {
            skip = 2;
        }
    } else {
        // No prefix to strip
        return if path.is_empty() {
            "/".to_string()
        } else {
            path.to_string()
        };
    }
    let remaining: Vec<&str> = parts[skip..].to_vec();
    if remaining.is_empty() {
        "/".to_string()
    } else {
        format!("/{}", remaining.join("/"))
    }
}

// ---------------------------------------------------------------------------
// Zone-canonical helpers (also in router.rs, duplicated here as standalone
// #[pyfunction]s for direct use by mount_table.py)
// ---------------------------------------------------------------------------

/// Canonicalize a virtual path with zone prefix.
///
/// ``canonicalize_path("/workspace/file.txt", "root")``
/// → ``"/root/workspace/file.txt"``
#[pyfunction]
#[pyo3(signature = (path, zone_id="root"))]
pub fn canonicalize_path(path: &str, zone_id: &str) -> String {
    let stripped = path.trim_start_matches('/');
    if stripped.is_empty() {
        format!("/{}", zone_id)
    } else {
        format!("/{}/{}", zone_id, stripped)
    }
}

/// Extract (zone_id, relative_path) from a canonical path.
///
/// ``extract_zone_id("/root/workspace/file.txt")``
/// → ``("root", "/workspace/file.txt")``
#[pyfunction]
pub fn extract_zone_id(canonical_path: &str) -> (String, String) {
    let trimmed = canonical_path.trim_start_matches('/');
    match trimmed.split_once('/') {
        Some((zone, rest)) => (zone.to_string(), format!("/{}", rest)),
        None => (trimmed.to_string(), "/".to_string()),
    }
}

// ---------------------------------------------------------------------------
// Internal helpers (not exported to Python)
// ---------------------------------------------------------------------------

/// Collapse consecutive slashes into a single slash.
fn collapse_slashes(path: &str) -> String {
    let mut result = String::with_capacity(path.len());
    let mut prev_slash = false;
    for ch in path.chars() {
        if ch == '/' {
            if !prev_slash {
                result.push('/');
            }
            prev_slash = true;
        } else {
            result.push(ch);
            prev_slash = false;
        }
    }
    result
}

/// POSIX-style normpath: collapse ``//``, resolve ``.`` and ``..``.
/// Does not touch the filesystem — pure string operation.
fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_string();
    }
    let is_abs = path.starts_with('/');
    let mut components: Vec<&str> = Vec::new();
    for part in path.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            if is_abs {
                // For absolute paths, just pop (can't go above root)
                components.pop();
            } else if components.last().is_none_or(|last| *last == "..") {
                components.push("..");
            } else {
                components.pop();
            }
        } else {
            components.push(part);
        }
    }
    if is_abs {
        if components.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", components.join("/"))
        }
    } else if components.is_empty() {
        ".".to_string()
    } else {
        components.join("/")
    }
}

/// Compile a glob pattern into a regex.
fn compile_glob(pattern: &str) -> Option<regex::Regex> {
    let mut regex_str = String::from("^");
    let bytes = pattern.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if i + 1 < bytes.len() && bytes[i] == b'*' && bytes[i + 1] == b'*' {
            regex_str.push_str(".*");
            i += 2;
            // Optional trailing slash after **
            if i < bytes.len() && bytes[i] == b'/' {
                regex_str.push_str("/?");
                i += 1;
            }
        } else if bytes[i] == b'*' {
            regex_str.push_str("[^/]*");
            i += 1;
        } else if bytes[i] == b'?' {
            regex_str.push('.');
            i += 1;
        } else {
            let ch = bytes[i] as char;
            if r"\.[]{}()+^$|".contains(ch) {
                regex_str.push('\\');
            }
            regex_str.push(ch);
            i += 1;
        }
    }
    regex_str.push('$');
    regex::Regex::new(&regex_str).ok()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- split_path --
    #[test]
    fn test_split_empty() {
        assert!(split_path("").is_empty());
        assert!(split_path("/").is_empty());
    }

    #[test]
    fn test_split_basic() {
        assert_eq!(split_path("/a/b/c.txt"), vec!["a", "b", "c.txt"]);
    }

    // -- get_parent --
    #[test]
    fn test_get_parent() {
        assert_eq!(get_parent("/a/b/c.txt"), Some("/a/b".into()));
        assert_eq!(get_parent("/a"), Some("/".into()));
        assert_eq!(get_parent("/"), None);
    }

    // -- get_ancestors --
    #[test]
    fn test_get_ancestors() {
        assert_eq!(
            get_ancestors("/a/b/c.txt"),
            vec!["/a/b/c.txt", "/a/b", "/a"]
        );
        assert_eq!(get_ancestors("/a"), vec!["/a"]);
        assert!(get_ancestors("/").is_empty());
    }

    // -- get_parent_chain --
    #[test]
    fn test_get_parent_chain() {
        assert_eq!(
            get_parent_chain("/a/b/c.txt"),
            vec![
                ("/a/b/c.txt".into(), "/a/b".into()),
                ("/a/b".into(), "/a".into()),
            ]
        );
        assert!(get_parent_chain("/a").is_empty());
    }

    // -- parent_path --
    #[test]
    fn test_parent_path() {
        assert_eq!(parent_path("/a/b/c"), Some("/a/b".into()));
        assert_eq!(parent_path("/a"), Some("/".into()));
        assert_eq!(parent_path("/"), None);
    }

    // -- validate_path --
    #[test]
    fn test_validate_basic() {
        assert_eq!(validate_path("  /foo/bar  ", false).unwrap(), "/foo/bar");
        assert_eq!(validate_path("foo///bar", false).unwrap(), "/foo/bar");
    }

    #[test]
    fn test_validate_rejects_empty() {
        assert!(validate_path("", false).is_err());
        assert!(validate_path("  ", false).is_err());
    }

    #[test]
    fn test_validate_rejects_root() {
        assert!(validate_path("/", false).is_err());
        assert!(validate_path("/", true).is_ok());
    }

    #[test]
    fn test_validate_rejects_dotdot() {
        assert!(validate_path("/a/../b", false).is_err());
    }

    #[test]
    fn test_validate_rejects_null() {
        assert!(validate_path("/a\0b", false).is_err());
    }

    // -- normalize_path --
    #[test]
    fn test_normalize_basic() {
        assert_eq!(normalize_path("/a//b/./c").unwrap(), "/a/b/c");
        assert_eq!(normalize_path("/a/b/../c").unwrap(), "/a/c");
    }

    #[test]
    fn test_normalize_rejects_relative() {
        assert!(normalize_path("a/b").is_err());
    }

    // -- path_matches_pattern --
    #[test]
    fn test_glob_star() {
        assert!(path_matches_pattern("/a/b.txt", "/a/*.txt"));
        assert!(!path_matches_pattern("/a/b/c.txt", "/a/*.txt"));
    }

    #[test]
    fn test_glob_double_star() {
        assert!(path_matches_pattern("/a/b/c.txt", "/a/**/*.txt"));
    }

    #[test]
    fn test_glob_question() {
        assert!(path_matches_pattern("/a/b", "/a/?"));
        assert!(!path_matches_pattern("/a/bc", "/a/?"));
    }

    // -- unscope_internal_path --
    #[test]
    fn test_unscope_tenant() {
        assert_eq!(
            unscope_internal_path("/tenant:x/workspace/file.txt"),
            "/workspace/file.txt"
        );
    }

    #[test]
    fn test_unscope_tenant_user() {
        assert_eq!(
            unscope_internal_path("/tenant:x/user:y/data/file.txt"),
            "/data/file.txt"
        );
    }

    #[test]
    fn test_unscope_zone() {
        assert_eq!(
            unscope_internal_path("/zone/alpha/workspace/file.txt"),
            "/workspace/file.txt"
        );
    }

    #[test]
    fn test_unscope_no_prefix() {
        assert_eq!(
            unscope_internal_path("/workspace/file.txt"),
            "/workspace/file.txt"
        );
    }

    // -- canonicalize_path --
    #[test]
    fn test_canonicalize() {
        assert_eq!(
            canonicalize_path("/workspace/file.txt", "root"),
            "/root/workspace/file.txt"
        );
        assert_eq!(canonicalize_path("/", "root"), "/root");
    }

    // -- extract_zone_id --
    #[test]
    fn test_extract_zone_id() {
        assert_eq!(
            extract_zone_id("/root/workspace/file.txt"),
            ("root".into(), "/workspace/file.txt".into())
        );
        assert_eq!(extract_zone_id("/root"), ("root".into(), "/".into()));
    }

    // -- normpath internal --
    #[test]
    fn test_normpath() {
        assert_eq!(normpath("/a//b/./c"), "/a/b/c");
        assert_eq!(normpath("/a/b/../c"), "/a/c");
        assert_eq!(normpath("/"), "/");
        assert_eq!(normpath("/a/b/../../"), "/");
    }
}
