//! BLAKE3 content hashing — PyO3 wrappers for lib hash functions.

use lib::hash::{hash_content, hash_content_smart};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Below this size, GIL-release overhead outweighs the hashing cost —
/// stay on the caller thread. Tuned loosely: BLAKE3 is ~1 GB/s on modern
/// x86, so 16 KiB hashes in under 20 µs while `py.detach` adds ~1 µs.
const DETACH_THRESHOLD: usize = 16 * 1024;

/// Compute BLAKE3 hash of content (full hash). Returns 64-char hex string.
#[pyfunction]
pub fn hash_content_py(py: Python<'_>, content: &[u8]) -> String {
    if content.len() < DETACH_THRESHOLD {
        hash_content(content)
    } else {
        // §review fix #18: release the GIL for multi-MB payloads instead
        // of stalling every other Python thread during the hash.
        py.detach(|| hash_content(content))
    }
}

/// Compute BLAKE3 hash with strategic sampling for large files.
/// For files < 256KB: full hash. For >= 256KB: samples first+middle+last 64KB.
#[pyfunction]
pub fn hash_content_smart_py(py: Python<'_>, content: &[u8]) -> String {
    if content.len() < DETACH_THRESHOLD {
        hash_content_smart(content)
    } else {
        py.detach(|| hash_content_smart(content))
    }
}

/// Compute BLAKE3 hash from a Python bytes object, releasing the GIL.
#[pyfunction]
pub fn hash_bytes(py: Python<'_>, data: &Bound<PyBytes>) -> String {
    let bytes = data.as_bytes();
    py.detach(|| hash_content(bytes))
}
