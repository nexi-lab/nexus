//! Literal pattern detection for SIMD-accelerated search path selection.

/// Check if a pattern is a literal string (no regex metacharacters).
/// Literal patterns can use SIMD-accelerated memchr search.
pub fn is_literal_pattern(pattern: &str) -> bool {
    !pattern.chars().any(|c| {
        matches!(
            c,
            '.' | '*' | '+' | '?' | '(' | ')' | '[' | ']' | '{' | '}' | '|' | '^' | '$' | '\\'
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals() {
        assert!(is_literal_pattern("hello"));
        assert!(is_literal_pattern("foo bar"));
        assert!(is_literal_pattern("hello world 123"));
        assert!(is_literal_pattern("path/to/file"));
    }

    #[test]
    fn regex_patterns() {
        assert!(!is_literal_pattern("foo.*bar"));
        assert!(!is_literal_pattern("^start"));
        assert!(!is_literal_pattern("end$"));
        assert!(!is_literal_pattern("a+b"));
        assert!(!is_literal_pattern("a?b"));
        assert!(!is_literal_pattern("[abc]"));
        assert!(!is_literal_pattern("a|b"));
        assert!(!is_literal_pattern("a\\b"));
        assert!(!is_literal_pattern("(group)"));
        assert!(!is_literal_pattern("x{3}"));
    }
}
