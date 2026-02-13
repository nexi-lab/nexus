// =============================================================================
// BLAKE3 Hashing for Content-Addressable Storage (Issue #1395)
// =============================================================================

use pyo3::prelude::*;

/// Compute BLAKE3 hash of content (full hash).
///
/// BLAKE3 is ~3x faster than SHA-256 and uses SIMD acceleration.
/// Returns 64-character hex string (256-bit hash).
#[pyfunction]
pub fn hash_content(content: &[u8]) -> String {
    blake3::hash(content).to_hex().to_string()
}

/// Compute BLAKE3 hash with strategic sampling for large files.
///
/// For files < 256KB: full hash (same as hash_content)
/// For files >= 256KB: samples first 64KB + middle 64KB + last 64KB
///
/// This provides ~10x speedup for large files while maintaining
/// good collision resistance for deduplication purposes.
///
/// NOTE: This is NOT suitable for cryptographic integrity verification,
/// only for content-addressable storage fingerprinting.
#[pyfunction]
pub fn hash_content_smart(content: &[u8]) -> String {
    const THRESHOLD: usize = 256 * 1024; // 256KB
    const SAMPLE_SIZE: usize = 64 * 1024; // 64KB per sample

    if content.len() < THRESHOLD {
        blake3::hash(content).to_hex().to_string()
    } else {
        let mut hasher = blake3::Hasher::new();

        // First 64KB
        hasher.update(&content[..SAMPLE_SIZE]);

        // Middle 64KB
        let mid_start = content.len() / 2 - SAMPLE_SIZE / 2;
        hasher.update(&content[mid_start..mid_start + SAMPLE_SIZE]);

        // Last 64KB
        hasher.update(&content[content.len() - SAMPLE_SIZE..]);

        // Include file size to differentiate files with same samples.
        // Cast to u64 for cross-platform consistency with Python's
        // len(content).to_bytes(8, byteorder="little").
        hasher.update(&(content.len() as u64).to_le_bytes());

        hasher.finalize().to_hex().to_string()
    }
}
