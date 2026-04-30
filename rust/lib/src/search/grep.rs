//! Grep match result type.

/// A single grep match with file, line, content, and match text.
#[derive(Debug, Clone)]
pub struct GrepMatch {
    pub file: String,
    pub line: usize,
    pub content: String,
    pub match_text: String,
}
