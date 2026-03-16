//! Memory-mapped file I/O (PyO3 wrappers).

use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use rayon::prelude::*;
use std::fs::File;

/// Read a file using memory-mapped I/O for zero-copy performance.
#[pyfunction]
pub fn read_file(py: Python<'_>, path: &str) -> PyResult<Option<Py<PyBytes>>> {
    let file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => {
            return Err(pyo3::exceptions::PyIOError::new_err(format!(
                "Failed to open file '{}': {}",
                path, e
            )))
        }
    };

    let metadata = file.metadata().map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to get file metadata: {}", e))
    })?;

    if metadata.len() == 0 {
        return Ok(Some(PyBytes::new(py, &[]).into()));
    }

    // SAFETY: The file is opened read-only and we don't modify it.
    let mmap = unsafe {
        Mmap::map(&file).map_err(|e| {
            pyo3::exceptions::PyIOError::new_err(format!("Failed to mmap file '{}': {}", path, e))
        })?
    };

    Ok(Some(PyBytes::new(py, &mmap).into()))
}

/// Read multiple files using memory-mapped I/O in parallel.
///
/// Returns a dict mapping path -> bytes. Files that fail to open are silently skipped.
/// The GIL is released during I/O; PyBytes are created from mmap'd regions without
/// intermediate Vec copies.
#[pyfunction]
pub fn read_files_bulk(py: Python<'_>, paths: Vec<String>) -> PyResult<Bound<'_, PyDict>> {
    // Phase 1: mmap all files in parallel (GIL released, no copies).
    // Mmap is Send, so we can collect across rayon threads.
    let mmaps: Vec<(String, Option<Mmap>)> = py.detach(|| {
        paths
            .into_par_iter()
            .filter_map(|path| {
                let file = File::open(&path).ok()?;
                let metadata = file.metadata().ok()?;

                if metadata.len() == 0 {
                    return Some((path, None)); // empty file — no mmap needed
                }

                let mmap = unsafe { Mmap::map(&file).ok()? };
                Some((path, Some(mmap)))
            })
            .collect()
    });

    // Phase 2: create PyBytes from mmap regions (GIL held, one copy into Python heap per file).
    let py_dict = PyDict::new(py);
    for (path, mmap) in &mmaps {
        let bytes = match mmap {
            Some(m) => PyBytes::new(py, m),
            None => PyBytes::new(py, &[]),
        };
        py_dict.set_item(path, bytes)?;
    }

    Ok(py_dict)
}
