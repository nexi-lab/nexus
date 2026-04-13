//! BLAKE3 content hashing for content-addressable storage.

/// Compute BLAKE3 hash of content (full hash).
///
/// Returns 64-character hex string (256-bit hash).
pub fn hash_content(content: &[u8]) -> String {
    blake3::hash(content).to_hex().to_string()
}

/// Compute BLAKE3 hash with strategic sampling for large files.
///
/// For files < 256KB: full hash (same as `hash_content`)
/// For files >= 256KB: samples first 64KB + middle 64KB + last 64KB + file size
///
/// ~10x speedup for large files while maintaining good collision resistance.
///
/// NOTE: Not suitable for cryptographic integrity verification —
/// only for content-addressable storage fingerprinting.
pub fn hash_content_smart(content: &[u8]) -> String {
    const THRESHOLD: usize = 256 * 1024;
    const SAMPLE_SIZE: usize = 64 * 1024;

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

        // Include file size to differentiate files with same samples
        hasher.update(&content.len().to_le_bytes());

        hasher.finalize().to_hex().to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_hash() {
        let content = b"hello world";
        let h1 = hash_content(content);
        let h2 = hash_content(content);
        assert_eq!(h1, h2);
        assert_eq!(h1.len(), 64); // 256-bit = 64 hex chars
    }

    #[test]
    fn different_content_different_hash() {
        let h1 = hash_content(b"hello");
        let h2 = hash_content(b"world");
        assert_ne!(h1, h2);
    }

    #[test]
    fn smart_hash_small_file_equals_full() {
        let content = b"small content under threshold";
        assert_eq!(hash_content(content), hash_content_smart(content));
    }

    #[test]
    fn smart_hash_large_file_uses_sampling() {
        let large = vec![0u8; 512 * 1024]; // 512KB
        let full = hash_content(&large);
        let smart = hash_content_smart(&large);
        // Smart hash uses sampling so should differ from full hash
        assert_ne!(full, smart);
    }

    #[test]
    fn smart_hash_deterministic() {
        let large = vec![42u8; 512 * 1024];
        let h1 = hash_content_smart(&large);
        let h2 = hash_content_smart(&large);
        assert_eq!(h1, h2);
    }
}
