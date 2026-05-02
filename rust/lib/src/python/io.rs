//! Memory-mapped file I/O (PyO3 wrappers).

use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use rayon::prelude::*;
use std::fs::File;

/// Read a file using memory-mapped I/O for zero-copy performance.
///
/// § review fix #28: open + mmap run off the GIL. The final `PyBytes::new`
/// copy necessarily holds the GIL, but at that point all syscall work is
/// done.
#[pyfunction]
pub fn read_file(py: Python<'_>, path: &str) -> PyResult<Option<Py<PyBytes>>> {
    enum Outcome {
        NotFound,
        Empty,
        Mapped(Mmap),
    }
    let path_str = path.to_string();
    let outcome: Result<Outcome, pyo3::PyErr> = py.detach(|| {
        let file = match File::open(&path_str) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Outcome::NotFound),
            Err(e) => {
                return Err(pyo3::exceptions::PyIOError::new_err(format!(
                    "Failed to open file '{}': {}",
                    path_str, e
                )))
            }
        };
        let metadata = file.metadata().map_err(|e| {
            pyo3::exceptions::PyIOError::new_err(format!("Failed to get file metadata: {}", e))
        })?;
        if metadata.len() == 0 {
            return Ok(Outcome::Empty);
        }
        // SAFETY: The file is opened read-only and we don't modify it.
        let mmap = unsafe {
            Mmap::map(&file).map_err(|e| {
                pyo3::exceptions::PyIOError::new_err(format!(
                    "Failed to mmap file '{}': {}",
                    path_str, e
                ))
            })?
        };
        Ok(Outcome::Mapped(mmap))
    });

    match outcome? {
        Outcome::NotFound => Ok(None),
        Outcome::Empty => Ok(Some(PyBytes::new(py, &[]).into())),
        Outcome::Mapped(mmap) => Ok(Some(PyBytes::new(py, &mmap).into())),
    }
}

/// Read multiple files using memory-mapped I/O in parallel.
#[pyfunction]
pub fn read_files_bulk(py: Python<'_>, paths: Vec<String>) -> PyResult<Bound<'_, PyDict>> {
    let mmaps: Vec<(String, Option<Mmap>)> = py.detach(|| {
        paths
            .into_par_iter()
            .filter_map(|path| {
                let file = File::open(&path).ok()?;
                let metadata = file.metadata().ok()?;
                if metadata.len() == 0 {
                    return Some((path, None));
                }
                let mmap = unsafe { Mmap::map(&file).ok()? };
                Some((path, Some(mmap)))
            })
            .collect()
    });

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
