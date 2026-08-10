//! Per-zone skeleton title index — the Rust mirror of Python's
//! deleted `SearchDaemon.locate()` BM25-lite (#4628, mirrors #4552 /
//! #4545 / #3725).
//!
//! The skeleton is DERIVED data: path tokens come from the FTS
//! `path` field, titles from the first ATX heading of each doc's
//! chunk-0 text — both already indexed, so no schema change and no
//! reindex (issue #4628 non-goal).  [`ZoneSkeleton`] builds the
//! in-memory index from a tantivy stored-doc scan and
//! [`crate::index_manager::IndexManager`] caches it per zone keyed
//! by the FTS searcher generation.
//!
//! Scoring matches Python exactly: title-token overlap × 2 +
//! path-token overlap × 1, DF-capped candidate selection, aggregate
//! budgets, deterministic (-score, path) ordering.

/// Bytes of chunk-0 text scanned for a title — Python
/// `SKELETON_HEAD_BYTES` parity (titles live at the top of a doc;
/// scanning further finds section headings, not titles).
pub const SKELETON_HEAD_BYTES: usize = 2048;

/// Tokenize a path or title into lowercase word tokens — port of
/// Python `text_utils.tokenize_path`.  Splits on `/ _ - .` and
/// whitespace, then on camelCase boundaries within each segment
/// (`parseUserAuth` → `parse user auth`, `HTMLParser` → `html
/// parser`).  Index-time and query-time both use this, so the two
/// sides always agree.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    for part in text.split(|c: char| matches!(c, '/' | '_' | '-' | '.') || c.is_whitespace()) {
        if part.is_empty() {
            continue;
        }
        let chars: Vec<char> = part.chars().collect();
        let mut start = 0;
        for i in 1..chars.len() {
            let prev = chars[i - 1];
            let cur = chars[i];
            // Python _CAMEL_SPLIT_RE: (?<=[a-z0-9])(?=[A-Z]) |
            // (?<=[A-Z])(?=[A-Z][a-z])
            let boundary = ((prev.is_ascii_lowercase() || prev.is_ascii_digit())
                && cur.is_ascii_uppercase())
                || (prev.is_ascii_uppercase()
                    && cur.is_ascii_uppercase()
                    && matches!(chars.get(i + 1), Some(n) if n.is_ascii_lowercase()));
            if boundary {
                tokens.push(chars[start..i].iter().collect::<String>().to_lowercase());
                start = i;
            }
        }
        tokens.push(chars[start..].iter().collect::<String>().to_lowercase());
    }
    tokens
}

/// Extract a doc title from its chunk-0 text: the first ATX heading
/// (`# ...` – `###### ...`) within the first [`SKELETON_HEAD_BYTES`].
/// A leading YAML frontmatter block (`---` ... `---`) is skipped so
/// `#`-comments inside it can't masquerade as headings.  Returns
/// `None` for docs with no heading in the head window — they still
/// join the skeleton on path tokens alone (Python parity: extractor
/// returning None kept the doc, title-less).
pub fn extract_title(chunk0_text: &str) -> Option<String> {
    let mut end = SKELETON_HEAD_BYTES.min(chunk0_text.len());
    while end > 0 && !chunk0_text.is_char_boundary(end) {
        end -= 1;
    }
    let head = &chunk0_text[..end];
    let mut in_frontmatter = false;
    for (i, line) in head.lines().enumerate() {
        let trimmed = line.trim();
        if i == 0 && trimmed == "---" {
            in_frontmatter = true;
            continue;
        }
        if in_frontmatter {
            if trimmed == "---" {
                in_frontmatter = false;
            }
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix('#') {
            let title = rest.trim_start_matches('#').trim();
            if !title.is_empty() {
                return Some(title.to_string());
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenize_matches_python_docstring_examples() {
        assert_eq!(
            tokenize("/workspace/src/auth/parseUserLogin.py"),
            ["workspace", "src", "auth", "parse", "user", "login", "py"]
        );
        assert_eq!(tokenize("/docs/README_API.md"), ["docs", "readme", "api", "md"]);
    }

    #[test]
    fn tokenize_splits_acronym_camel_boundary() {
        // (?<=[A-Z])(?=[A-Z][a-z]): HTMLParser → html parser
        assert_eq!(tokenize("HTMLParser"), ["html", "parser"]);
        // digit→upper boundary: v2Beta → v2 beta
        assert_eq!(tokenize("v2Beta"), ["v2", "beta"]);
    }

    #[test]
    fn tokenize_title_text_with_spaces() {
        assert_eq!(tokenize("Atlas Design Doc"), ["atlas", "design", "doc"]);
    }

    #[test]
    fn tokenize_empty_and_separator_only() {
        assert!(tokenize("").is_empty());
        assert!(tokenize("///--..").is_empty());
    }

    #[test]
    fn extract_title_first_atx_heading() {
        assert_eq!(
            extract_title("# Atlas Design Doc\n\nbody text"),
            Some("Atlas Design Doc".to_string())
        );
        // Deeper heading levels count too; hashes stripped.
        assert_eq!(extract_title("### Deep Title\nbody"), Some("Deep Title".to_string()));
    }

    #[test]
    fn extract_title_skips_yaml_frontmatter() {
        let text = "---\ntitle: raw\n# not a heading, a YAML comment\n---\n# Real Title\nbody";
        assert_eq!(extract_title(text), Some("Real Title".to_string()));
    }

    #[test]
    fn extract_title_none_when_no_heading() {
        assert_eq!(extract_title("plain prose with no heading"), None);
        assert_eq!(extract_title(""), None);
    }

    #[test]
    fn extract_title_respects_head_cap() {
        // Heading past the 2 KiB window is not a title.
        let text = format!("{}\n# Late Heading\n", "x".repeat(SKELETON_HEAD_BYTES));
        assert_eq!(extract_title(&text), None);
    }
}
