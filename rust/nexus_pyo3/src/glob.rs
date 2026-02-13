//! Glob pattern matching (PyO3 wrappers).

use globset::{Glob, GlobSetBuilder};
use pyo3::prelude::*;
use pyo3::types::PyList;
use rayon::prelude::*;

/// Threshold for parallelization
const GLOB_PARALLEL_THRESHOLD: usize = 500;

/// Fast glob pattern matching using Rust globset.
#[pyfunction]
#[pyo3(signature = (patterns, paths))]
pub fn glob_match_bulk(
    py: Python<'_>,
    patterns: Vec<String>,
    paths: Vec<String>,
) -> PyResult<Bound<'_, PyList>> {
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

    let matches: Vec<String> = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
            paths
                .into_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        } else {
            paths
                .into_par_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        }
    });

    let py_list = PyList::empty(py);
    for path in matches {
        py_list.append(path)?;
    }

    Ok(py_list)
}

/// Fast path filtering using Rust glob patterns.
#[pyfunction]
pub fn filter_paths(
    py: Python<'_>,
    paths: Vec<String>,
    exclude_patterns: Vec<String>,
) -> PyResult<Vec<String>> {
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

    let filtered = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
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
