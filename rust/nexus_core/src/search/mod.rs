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
                    let match_text = line
                        .chars()
                        .skip(line[..start].chars().count())
                        .take(end - start)
                        .collect::<String>();
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
}
