//! Path prefix matching using sorted binary search (PyO3 wrappers).
//!
//! Provides O(log N) prefix matching for Tiger Cache permission filtering.
//! Used by `PermissionEnforcer.has_accessible_descendants_batch()` to replace
//! O(N×M) Python `startswith()` loops with Rust binary search.
//!
//! Related: Issue #1565

use pyo3::prelude::*;

/// Normalize a prefix: strip trailing slashes.
#[inline]
fn normalize_prefix(prefix: &str) -> &str {
    let trimmed = prefix.trim_end_matches('/');
    if trimmed.is_empty() {
        // Root prefix "/" normalizes to ""
        return "";
    }
    trimmed
}

/// Check if `path` matches the given normalized prefix.
///
/// A path matches if:
/// - `prefix_norm` is empty (root prefix `/` matches everything)
/// - `path` starts with `prefix_norm` followed by `/`
/// - `path` equals `prefix_norm` exactly
#[inline]
fn path_matches_prefix(path: &str, prefix_norm: &str) -> bool {
    if prefix_norm.is_empty() {
        return true;
    }
    if let Some(rest) = path.strip_prefix(prefix_norm) {
        rest.is_empty() || rest.starts_with('/')
    } else {
        false
    }
}

/// Check if ANY path in the list starts with the given prefix.
///
/// Uses sorted binary search for O(log N) lookup instead of O(N) scan.
/// The input paths are sorted internally; callers need not pre-sort.
///
/// Args:
///     paths: List of file paths (e.g., ["/a/b/c.txt", "/d/e.txt"])
///     prefix: Directory prefix to check (e.g., "/a/b")
///
/// Returns:
///     True if at least one path starts with the prefix
#[pyfunction]
pub fn any_path_starts_with(mut paths: Vec<String>, prefix: &str) -> bool {
    if paths.is_empty() {
        return false;
    }

    let prefix_norm = normalize_prefix(prefix);

    // Root prefix matches everything
    if prefix_norm.is_empty() {
        return true;
    }

    paths.sort_unstable();

    // Binary search for the first path >= prefix_norm
    let idx = paths.partition_point(|p| p.as_str() < prefix_norm);

    // Check from idx onwards — paths are sorted, so matching paths are contiguous
    for path in paths[idx..].iter() {
        if path_matches_prefix(path, prefix_norm) {
            return true;
        }
        // Once we pass paths that could match the prefix, stop
        if !path.starts_with(prefix_norm) {
            break;
        }
    }

    false
}

/// Check multiple prefixes against a single sorted path list.
///
/// For each prefix, returns whether any path starts with that prefix.
/// Sorts paths once, then does O(log N) binary search per prefix.
///
/// Args:
///     paths: List of file paths
///     prefixes: List of directory prefixes to check
///
/// Returns:
///     List of booleans, one per prefix (same order as input)
#[pyfunction]
pub fn batch_prefix_check(mut paths: Vec<String>, prefixes: Vec<String>) -> Vec<bool> {
    if paths.is_empty() {
        return vec![false; prefixes.len()];
    }

    paths.sort_unstable();

    prefixes
        .iter()
        .map(|prefix| {
            let prefix_norm = normalize_prefix(prefix);

            // Root prefix matches everything
            if prefix_norm.is_empty() {
                return true;
            }

            let idx = paths.partition_point(|p| p.as_str() < prefix_norm);

            for path in paths[idx..].iter() {
                if path_matches_prefix(path, prefix_norm) {
                    return true;
                }
                if !path.starts_with(prefix_norm) {
                    break;
                }
            }

            false
        })
        .collect()
}

/// Return all paths that start with the given prefix.
///
/// Uses sorted binary search to find the matching range efficiently.
///
/// Args:
///     paths: List of file paths
///     prefix: Directory prefix to filter by
///
/// Returns:
///     List of paths that match the prefix
#[pyfunction]
pub fn filter_paths_by_prefix(mut paths: Vec<String>, prefix: &str) -> Vec<String> {
    if paths.is_empty() {
        return Vec::new();
    }

    let prefix_norm = normalize_prefix(prefix);

    // Root prefix matches everything
    if prefix_norm.is_empty() {
        return paths;
    }

    paths.sort_unstable();

    let idx = paths.partition_point(|p| p.as_str() < prefix_norm);

    let mut result = Vec::new();
    for path in paths[idx..].iter() {
        if path_matches_prefix(path, prefix_norm) {
            result.push(path.clone());
        } else if !path.starts_with(prefix_norm) {
            break;
        }
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- any_path_starts_with tests ---

    #[test]
    fn test_any_starts_with_found() {
        let paths = vec![
            "/a/b/c.txt".into(),
            "/d/e/f.txt".into(),
            "/x/y/z.txt".into(),
        ];
        assert!(any_path_starts_with(paths, "/a/b"));
    }

    #[test]
    fn test_any_starts_with_not_found() {
        let paths = vec![
            "/a/b/c.txt".into(),
            "/d/e/f.txt".into(),
            "/x/y/z.txt".into(),
        ];
        assert!(!any_path_starts_with(paths, "/g/h"));
    }

    #[test]
    fn test_any_starts_with_empty_paths() {
        let paths: Vec<String> = vec![];
        assert!(!any_path_starts_with(paths, "/a/b"));
    }

    #[test]
    fn test_any_starts_with_empty_prefix() {
        let paths = vec!["/a/b/c.txt".into()];
        // Empty string prefix => root, matches everything
        assert!(any_path_starts_with(paths, ""));
    }

    // --- batch_prefix_check tests ---

    #[test]
    fn test_batch_all_found() {
        let paths = vec![
            "/docs/readme.md".into(),
            "/skills/python.md".into(),
            "/archive/old.txt".into(),
        ];
        let prefixes = vec!["/docs".into(), "/skills".into(), "/archive".into()];
        assert_eq!(batch_prefix_check(paths, prefixes), vec![true, true, true]);
    }

    #[test]
    fn test_batch_some_found() {
        let paths = vec!["/docs/readme.md".into(), "/docs/guide.md".into()];
        let prefixes = vec!["/docs".into(), "/skills".into(), "/archive".into()];
        assert_eq!(
            batch_prefix_check(paths, prefixes),
            vec![true, false, false]
        );
    }

    #[test]
    fn test_batch_none_found() {
        let paths = vec!["/other/file.txt".into()];
        let prefixes = vec!["/docs".into(), "/skills".into()];
        assert_eq!(batch_prefix_check(paths, prefixes), vec![false, false]);
    }

    // --- filter_paths_by_prefix tests ---

    #[test]
    fn test_filter_returns_matching_paths() {
        let paths = vec![
            "/a/b/c.txt".into(),
            "/a/b/d.txt".into(),
            "/x/y/z.txt".into(),
        ];
        let result = filter_paths_by_prefix(paths, "/a/b");
        assert_eq!(result, vec!["/a/b/c.txt", "/a/b/d.txt"]);
    }

    // --- Edge cases ---

    #[test]
    fn test_trailing_slash_normalization() {
        // "/a/b/" should behave same as "/a/b"
        let paths = vec!["/a/b/c.txt".into()];
        assert!(any_path_starts_with(paths.clone(), "/a/b/"));
        assert!(any_path_starts_with(paths, "/a/b"));
    }

    #[test]
    fn test_prefix_collision() {
        // "/workspace" must NOT match "/workspace-old/file.txt"
        let paths = vec!["/workspace-old/file.txt".into()];
        assert!(!any_path_starts_with(paths, "/workspace"));
    }

    #[test]
    fn test_exact_match() {
        // path "/a/b" should match prefix "/a/b"
        let paths = vec!["/a/b".into()];
        assert!(any_path_starts_with(paths, "/a/b"));
    }

    #[test]
    fn test_root_prefix() {
        // prefix "/" should match everything
        let paths = vec!["/a/b/c.txt".into(), "/d/e.txt".into()];
        assert!(any_path_starts_with(paths.clone(), "/"));

        let results = batch_prefix_check(paths.clone(), vec!["/".into()]);
        assert_eq!(results, vec![true]);

        let filtered = filter_paths_by_prefix(paths.clone(), "/");
        assert_eq!(filtered.len(), 2);
    }

    #[test]
    fn test_unicode_paths() {
        let paths = vec![
            "/workspace/日本語/file.txt".into(),
            "/workspace/中文/doc.md".into(),
        ];
        assert!(any_path_starts_with(paths.clone(), "/workspace/日本語"));
        assert!(!any_path_starts_with(paths, "/workspace/한국어"));
    }

    #[test]
    fn test_sorted_binary_search_boundary() {
        // Test boundary conditions: prefix at very start and very end of sorted order
        let paths = vec![
            "/aaa/file.txt".into(),
            "/mmm/file.txt".into(),
            "/zzz/file.txt".into(),
        ];
        assert!(any_path_starts_with(paths.clone(), "/aaa")); // first
        assert!(any_path_starts_with(paths.clone(), "/zzz")); // last
        assert!(!any_path_starts_with(paths, "/000")); // before all
    }

    #[test]
    fn test_batch_with_empty_paths() {
        let paths: Vec<String> = vec![];
        let prefixes = vec!["/a".into(), "/b".into()];
        assert_eq!(batch_prefix_check(paths, prefixes), vec![false, false]);
    }

    #[test]
    fn test_filter_empty_paths() {
        let paths: Vec<String> = vec![];
        let result = filter_paths_by_prefix(paths, "/a");
        assert!(result.is_empty());
    }

    #[test]
    fn test_filter_exact_match_included() {
        let paths = vec!["/a/b".into(), "/a/b/c.txt".into()];
        let result = filter_paths_by_prefix(paths, "/a/b");
        assert_eq!(result, vec!["/a/b", "/a/b/c.txt"]);
    }

    #[test]
    fn test_prefix_collision_filter() {
        // filter should NOT include "/workspace-old/file.txt" for prefix "/workspace"
        let paths = vec![
            "/workspace/file.txt".into(),
            "/workspace-old/file.txt".into(),
        ];
        let result = filter_paths_by_prefix(paths, "/workspace");
        assert_eq!(result, vec!["/workspace/file.txt"]);
    }

    #[test]
    fn test_many_paths_performance_sanity() {
        // Sanity check that 10K paths doesn't panic or hang
        let paths: Vec<String> = (0..10_000)
            .map(|i| format!("/dir{}/subdir/file{}.txt", i % 100, i))
            .collect();
        let prefixes: Vec<String> = (0..50).map(|i| format!("/dir{}", i)).collect();
        let results = batch_prefix_check(paths, prefixes);
        assert_eq!(results.len(), 50);
        // dir0..dir49 should all be found (files exist for dir0..dir99)
        assert!(results.iter().all(|&r| r));
    }
}
