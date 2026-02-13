// =============================================================================
// Fast Glob Pattern Matching and Path Filtering
// =============================================================================

use pyo3::prelude::*;
use pyo3::types::PyList;
use rayon::prelude::*;

/// Threshold for parallelization: only use rayon for lists larger than this
const GLOB_PARALLEL_THRESHOLD: usize = 500;

/// Fast glob pattern matching using Rust globset
#[pyfunction]
#[pyo3(signature = (patterns, paths))]
pub fn glob_match_bulk(
    py: Python<'_>,
    patterns: Vec<String>,
    paths: Vec<String>,
) -> PyResult<Bound<'_, PyList>> {
    use globset::{Glob, GlobSetBuilder};

    // Build glob set from patterns
    let globset = py.detach(|| {
        let mut builder = GlobSetBuilder::new();
        for pattern in &patterns {
            match Glob::new(pattern) {
                Ok(glob) => {
                    builder.add(glob);
                }
                Err(e) => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "Invalid glob pattern '{}': {}",
                        pattern, e
                    )));
                }
            }
        }
        builder.build().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to build globset: {}", e))
        })
    })?;

    // Match paths against the glob set
    // Use parallel iteration for large lists, sequential for small lists
    let matches: Vec<String> = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
            // Sequential for small lists (avoid rayon overhead)
            paths
                .into_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        } else {
            // Parallel for large lists
            paths
                .into_par_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        }
    });

    // Convert results to Python list
    let py_list = PyList::empty(py);
    for path in matches {
        py_list.append(path)?;
    }

    Ok(py_list)
}

/// Fast path filtering using Rust glob patterns
/// Uses rayon parallelization for large path lists (>500 paths)
#[pyfunction]
pub fn filter_paths(
    py: Python<'_>,
    paths: Vec<String>,
    exclude_patterns: Vec<String>,
) -> PyResult<Vec<String>> {
    use globset::{Glob, GlobSetBuilder};

    // Build glob set from exclude patterns
    let globset = py.detach(|| {
        let mut builder = GlobSetBuilder::new();
        for pattern in &exclude_patterns {
            match Glob::new(pattern) {
                Ok(glob) => {
                    builder.add(glob);
                }
                Err(e) => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "Invalid glob pattern '{}': {}",
                        pattern, e
                    )));
                }
            }
        }
        builder.build().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to build globset: {}", e))
        })
    })?;

    // Filter paths against exclude patterns
    // Use parallel iteration for large lists, sequential for small lists
    let filtered = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
            // Sequential for small lists
            paths
                .into_iter()
                .filter(|path| {
                    let filename = if let Some(pos) = path.rfind('/') {
                        &path[pos + 1..]
                    } else {
                        path.as_str()
                    };
                    !globset.is_match(filename)
                })
                .collect()
        } else {
            // Parallel for large lists
            paths
                .into_par_iter()
                .filter(|path| {
                    let filename = if let Some(pos) = path.rfind('/') {
                        &path[pos + 1..]
                    } else {
                        path.as_str()
                    };
                    !globset.is_match(filename)
                })
                .collect()
        }
    });

    Ok(filtered)
}
