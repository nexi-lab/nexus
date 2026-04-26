//! Line-oriented text search (literal + regex).
//!
//! Provides `search_lines()` — a unified search function that automatically
//! selects SIMD-accelerated literal search or regex depending on the pattern.

pub mod grep;
pub mod literal;

use grep::GrepMatch;
use literal::is_literal_pattern;

/// Search mode — either SIMD-accelerated literal or full regex.
pub enum SearchMode {
    /// Case-sensitive literal search using memchr.
    Literal { pattern: String },
    /// Case-insensitive literal search.
    LiteralIgnoreCase { pattern_lower: String },
    /// Full regex search for complex patterns.
    Regex(regex::bytes::Regex),
}

/// Build a `SearchMode` from a pattern string.
pub fn build_search_mode(pattern: &str, ignore_case: bool) -> Result<SearchMode, regex::Error> {
    if is_literal_pattern(pattern) {
        if ignore_case {
            Ok(SearchMode::LiteralIgnoreCase {
                pattern_lower: pattern.to_lowercase(),
            })
        } else {
            Ok(SearchMode::Literal {
                pattern: pattern.to_string(),
            })
        }
    } else {
        let regex = regex::bytes::RegexBuilder::new(pattern)
            .case_insensitive(ignore_case)
            .build()?;
        Ok(SearchMode::Regex(regex))
    }
}

/// Map byte offsets in a lowercased string back to the corresponding substring
/// in the original string. Handles cases where `to_lowercase()` changes byte
/// lengths (e.g., Turkish İ → i̇, German ß → ss).
pub fn extract_original_match(
    original: &str,
    lowered: &str,
    byte_start: usize,
    byte_end: usize,
) -> String {
    if byte_start >= byte_end || byte_start >= lowered.len() {
        return String::new();
    }
    let clamped_end = byte_end.min(lowered.len());

    let mut lower_pos = 0usize;
    let mut orig_pos = 0usize;
    let mut orig_start: Option<usize> = None;
    let mut orig_end: Option<usize> = None;

    for orig_ch in original.chars() {
        let orig_next = orig_pos + orig_ch.len_utf8();
        let lowered_len = orig_ch
            .to_lowercase()
            .map(|ch| ch.len_utf8())
            .sum::<usize>();
        let lower_next = lower_pos + lowered_len;

        // If match starts anywhere inside this lowered-char span,
        // map to the start of the original char.
        if orig_start.is_none() && byte_start < lower_next {
            orig_start = Some(orig_pos);
        }

        // If match end falls on/inside this lowered-char span,
        // map to the end of the original char.
        if orig_end.is_none() && clamped_end <= lower_next {
            orig_end = Some(orig_next);
            break;
        }

        lower_pos = lower_next;
        orig_pos = orig_next;
    }

    let start = match orig_start {
        Some(s) => s,
        None => return String::new(),
    };
    let end = orig_end.unwrap_or(original.len());
    if start > end || end > original.len() {
        return String::new();
    }
    original[start..end].to_string()
}

/// Search lines of content for matches. Returns up to `max_results` matches.
///
/// This is the unified search function extracted from `grep_bulk` — it works on
/// already-decoded UTF-8 content (no file I/O, no mmap, no PyO3).
pub fn search_lines(
    file_path: &str,
    content: &str,
    search_mode: &SearchMode,
    max_results: usize,
) -> Vec<GrepMatch> {
    use memchr::memmem;

    let mut results = Vec::new();

    match search_mode {
        SearchMode::Literal { pattern } => {
            let finder = memmem::Finder::new(pattern.as_bytes());
            for (line_num, line) in content.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }
                let line_bytes = line.as_bytes();
                if let Some(start) = finder.find(line_bytes) {
                    let end = start + pattern.len();
                    let match_text = std::str::from_utf8(&line_bytes[start..end])
                        .unwrap_or("")
                        .to_string();
                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }
        SearchMode::LiteralIgnoreCase { pattern_lower } => {
            let finder = memmem::Finder::new(pattern_lower.as_bytes());
            for (line_num, line) in content.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }
                let line_lower = line.to_lowercase();
                if let Some(start) = finder.find(line_lower.as_bytes()) {
                    let end = start + pattern_lower.len();
                    let match_text = extract_original_match(line, &line_lower, start, end);
                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }
        SearchMode::Regex(regex) => {
            for (line_num, line) in content.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }
                let line_bytes = line.as_bytes();
                if let Some(m) = regex.find(line_bytes) {
                    let match_text = std::str::from_utf8(&line_bytes[m.start()..m.end()])
                        .unwrap_or("")
                        .to_string();
                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literal_case_sensitive() {
        let mode = build_search_mode("hello", false).unwrap();
        let results = search_lines(
            "test.txt",
            "say hello world\ngoodbye\nhello again",
            &mode,
            100,
        );
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].line, 1);
        assert_eq!(results[0].match_text, "hello");
        assert_eq!(results[1].line, 3);
    }

    #[test]
    fn literal_case_insensitive() {
        let mode = build_search_mode("HELLO", true).unwrap();
        let results = search_lines("test.txt", "Hello World\nGoodbye\nhELLo", &mode, 100);
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].line, 1);
        assert_eq!(results[1].line, 3);
    }

    #[test]
    fn regex_search() {
        let mode = build_search_mode(r"fn\s+\w+", false).unwrap();
        let results = search_lines(
            "test.rs",
            "fn main() {\n  let x = 1;\n}\nfn helper() {",
            &mode,
            100,
        );
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].match_text, "fn main");
        assert_eq!(results[1].match_text, "fn helper");
    }

    #[test]
    fn empty_content() {
        let mode = build_search_mode("hello", false).unwrap();
        let results = search_lines("empty.txt", "", &mode, 100);
        assert!(results.is_empty());
    }

    #[test]
    fn max_results_limit() {
        let mode = build_search_mode("a", false).unwrap();
        let content = "a\na\na\na\na";
        let results = search_lines("test.txt", content, &mode, 3);
        assert_eq!(results.len(), 3);
    }

    #[test]
    fn unicode_content() {
        let mode = build_search_mode("世界", false).unwrap();
        let results = search_lines("test.txt", "你好世界\nhello\n世界和平", &mode, 100);
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].line, 1);
        assert_eq!(results[1].line, 3);
    }

    #[test]
    fn unicode_ignore_case_match_text() {
        // Turkish İ (U+0130, 2 bytes) lowercases to i\u{0307} (3 bytes).
        // This verifies byte-offset mapping handles length changes correctly.
        // Search for "i\u{0307}b" (lowercase form) in "AİB" (original casing)
        let mode = build_search_mode("i\u{0307}b", true).unwrap();
        let results = search_lines("test.txt", "A\u{0130}B", &mode, 100);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].match_text, "\u{0130}B");
    }

    #[test]
    fn unicode_ignore_case_match_inside_expansion() {
        // Pattern starts inside İ's lowercase expansion (i + combining dot).
        // Match text must still map back to the full original character span.
        let mode = build_search_mode("\u{0307}b", true).unwrap();
        let results = search_lines("test.txt", "A\u{0130}B", &mode, 100);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].match_text, "\u{0130}B");
    }

    #[test]
    fn unicode_ignore_case_ascii() {
        // Basic ASCII case-insensitive should still work
        let mode = build_search_mode("hello", true).unwrap();
        let results = search_lines("test.txt", "Say HELLO World", &mode, 100);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].match_text, "HELLO");
    }
}
