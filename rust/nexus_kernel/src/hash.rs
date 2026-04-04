//! BLAKE3 content hashing — PyO3 wrappers for nexus_core hash functions.

use nexus_core::hash::{hash_content, hash_content_smart};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Compute BLAKE3 hash of content (full hash). Returns 64-char hex string.
#[pyfunction]
pub fn hash_content_py(_py: Python<'_>, content: &[u8]) -> String {
    hash_content(content)
}

/// Compute BLAKE3 hash with strategic sampling for large files.
/// For files < 256KB: full hash. For >= 256KB: samples first+middle+last 64KB.
#[pyfunction]
pub fn hash_content_smart_py(_py: Python<'_>, content: &[u8]) -> String {
    hash_content_smart(content)
}

/// Compute BLAKE3 hash from a Python bytes object, releasing the GIL.
#[pyfunction]
pub fn hash_bytes(py: Python<'_>, data: &Bound<PyBytes>) -> String {
    let bytes = data.as_bytes();
    py.detach(|| hash_content(bytes))
}
