//! Trigram extraction from byte content.

use ahash::AHashSet;

/// Ratio of null bytes above which a file is considered binary.
const BINARY_NULL_RATIO: f64 = 0.10;

/// Maximum content size for trigram extraction (1 GB).
const MAX_CONTENT_SIZE: usize = 1024 * 1024 * 1024;

/// Extract unique trigrams from byte content.
///
/// Returns an empty Vec for:
/// - Content shorter than 3 bytes
/// - Binary content (high null-byte ratio)
/// - Content exceeding 1 GB
pub fn extract_trigrams(content: &[u8]) -> Vec<[u8; 3]> {
    if content.len() < 3 {
        return Vec::new();
    }
    if content.len() > MAX_CONTENT_SIZE {
        return Vec::new();
    }
    if is_binary(content) {
        return Vec::new();
    }

    let mut seen = AHashSet::new();
    for window in content.windows(3) {
        let trigram = [window[0], window[1], window[2]];
        seen.insert(trigram);
    }

    seen.into_iter().collect()
}

/// Extract trigrams from a literal search pattern (for query).
///
/// Returns an empty Vec if the pattern is shorter than 3 bytes.
pub fn extract_trigrams_for_query(pattern: &str) -> Vec<[u8; 3]> {
    let bytes = pattern.as_bytes();
    if bytes.len() < 3 {
        return Vec::new();
    }

    let mut seen = AHashSet::new();
    for window in bytes.windows(3) {
        let trigram = [window[0], window[1], window[2]];
        seen.insert(trigram);
    }

    seen.into_iter().collect()
}

/// Check if content appears to be binary (high null-byte ratio).
pub fn is_binary(content: &[u8]) -> bool {
    if content.is_empty() {
        return false;
    }
    // Sample first 8KB for efficiency on large files.
    let sample = if content.len() > 8192 {
        &content[..8192]
    } else {
        content
    };
    let null_count = sample.iter().filter(|&&b| b == 0).count();
    (null_count as f64 / sample.len() as f64) > BINARY_NULL_RATIO
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_trigrams_basic() {
        let trigrams = extract_trigrams(b"hello");
        // "hello" → ["hel", "ell", "llo"]
        assert_eq!(trigrams.len(), 3);
        assert!(trigrams.contains(&[b'h', b'e', b'l']));
        assert!(trigrams.contains(&[b'e', b'l', b'l']));
        assert!(trigrams.contains(&[b'l', b'l', b'o']));
    }

    #[test]
    fn test_extract_trigrams_short() {
        assert!(extract_trigrams(b"ab").is_empty());
        assert!(extract_trigrams(b"a").is_empty());
    }

    #[test]
    fn test_extract_trigrams_empty() {
        assert!(extract_trigrams(b"").is_empty());
    }

    #[test]
    fn test_extract_trigrams_unicode() {
        // Multi-byte UTF-8: "日本語" = 9 bytes, 7 trigrams
        let content = "日本語".as_bytes();
        let trigrams = extract_trigrams(content);
        assert!(!trigrams.is_empty());
        // 9 bytes → 7 windows of 3
        assert!(trigrams.len() <= 7);
    }

    #[test]
    fn test_extract_trigrams_binary_skipped() {
        // Create content with >10% null bytes
        let mut content = vec![0u8; 20];
        content.extend_from_slice(b"hello world");
        let trigrams = extract_trigrams(&content);
        assert!(trigrams.is_empty());
    }

    #[test]
    fn test_extract_trigrams_deduplicates() {
        // "aaa" has only one unique trigram: [a, a, a]
        let trigrams = extract_trigrams(b"aaaa");
        assert_eq!(trigrams.len(), 1);
        assert!(trigrams.contains(&[b'a', b'a', b'a']));
    }

    #[test]
    fn test_extract_trigrams_for_query() {
        let trigrams = extract_trigrams_for_query("hello");
        assert_eq!(trigrams.len(), 3);
        assert!(trigrams.contains(&[b'h', b'e', b'l']));
    }

    #[test]
    fn test_extract_trigrams_for_query_short() {
        assert!(extract_trigrams_for_query("ab").is_empty());
    }

    #[test]
    fn test_is_binary_normal_text() {
        assert!(!is_binary(b"hello world\nthis is text"));
    }

    #[test]
    fn test_is_binary_with_nulls() {
        let mut data = vec![0u8; 50];
        data.extend_from_slice(b"short text");
        assert!(is_binary(&data));
    }

    #[test]
    fn test_is_binary_empty() {
        assert!(!is_binary(b""));
    }
}
