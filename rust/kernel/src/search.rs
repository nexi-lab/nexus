//! Grep search — PyO3 wrappers with mmap and SIMD-accelerated literal search.

use library::search::extract_original_match;
use library::search::grep::GrepMatch;
use library::search::literal::is_literal_pattern;
use memchr::memmem;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use simdutf8::basic::from_utf8 as simd_from_utf8;
use std::cell::RefCell;
use std::fs::File;
use std::num::NonZeroUsize;

use ahash::AHasher;
use lru::LruCache;
use std::hash::{Hash, Hasher};

/// Search mode for PyO3 layer — stores pre-built Finder for SIMD acceleration.
/// Distinct from `library::search::SearchMode` which stores owned strings
/// (suitable for `search_lines()`), while this version holds `memmem::Finder`
/// references for zero-copy mmap search.
enum SearchMode<'a> {
    Literal {
        finder: memmem::Finder<'a>,
        pattern: &'a str,
    },
    LiteralIgnoreCase {
        finder: memmem::Finder<'a>,
        pattern_lower: String,
    },
    Regex(regex::bytes::Regex),
}

/// Threshold for parallel processing in grep_files_mmap.
const GREP_MMAP_PARALLEL_THRESHOLD: usize = 10;

/// Maximum file size to mmap.
const GREP_MMAP_MAX_FILE_SIZE: u64 = 1024 * 1024 * 1024; // 1GB

/// Issue #3711: Below this size, std::fs::read() outperforms mmap.
/// mmap incurs page-table setup overhead (~20-35 µs) that dominates for
/// small files.  Regular read() is ~5-10 µs for files under 32 KB.
const GREP_MMAP_SMALL_FILE_THRESHOLD: u64 = 32 * 1024; // 32KB

// ---------------------------------------------------------------------------
// Regex compilation cache (thread-local LRU, 16 entries)
// ---------------------------------------------------------------------------

const REGEX_CACHE_CAPACITY: usize = 16;

/// Cached regex entry: stores pattern metadata for collision detection.
struct CachedRegex {
    pattern: String,
    case_insensitive: bool,
    regex: regex::bytes::Regex,
}

thread_local! {
    static REGEX_CACHE: RefCell<LruCache<u64, CachedRegex>> =
        RefCell::new(LruCache::new(NonZeroUsize::new(REGEX_CACHE_CAPACITY).unwrap()));
}

/// Compute cache key from pattern + case flag using ahash (zero allocation).
fn regex_cache_key(pattern: &str, case_insensitive: bool) -> u64 {
    let mut hasher = AHasher::default();
    pattern.hash(&mut hasher);
    case_insensitive.hash(&mut hasher);
    hasher.finish()
}

/// Get a compiled regex from cache or compile and cache it.
///
/// Uses u64 hash key for zero-alloc cache lookup. On hit, verifies the stored
/// pattern matches (collision detection). On miss, compiles and stores.
fn get_or_compile_regex(
    pattern: &str,
    case_insensitive: bool,
) -> Result<regex::bytes::Regex, String> {
    let key = regex_cache_key(pattern, case_insensitive);

    REGEX_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();

        if let Some(cached) = cache.get(&key) {
            if cached.pattern == pattern && cached.case_insensitive == case_insensitive {
                return Ok(cached.regex.clone());
            }
            // Hash collision: fall through to recompile.
        }

        let regex = RegexBuilder::new(pattern)
            .case_insensitive(case_insensitive)
            .build()
            .map_err(|e| format!("Invalid regex pattern: {}", e))?;

        cache.put(
            key,
            CachedRegex {
                pattern: pattern.to_string(),
                case_insensitive,
                regex: regex.clone(),
            },
        );

        Ok(regex)
    })
}

// ---------------------------------------------------------------------------
// Case-insensitive matching with thread-local buffer
// ---------------------------------------------------------------------------

thread_local! {
    static LOWER_BUF: RefCell<String> = RefCell::new(String::with_capacity(4096));
}

/// Find a case-insensitive literal match in `line` using a thread-local buffer.
///
/// Lowercases `line` into a reusable buffer (avoiding per-line allocation),
/// searches for `pattern_lower` using the SIMD-accelerated `finder`, and
/// maps the match back to the original-case text.
///
/// Returns `(match_start, match_end, original_case_match_text)` or None.
fn find_literal_ignore_case(
    line: &str,
    finder: &memmem::Finder,
    pattern_lower_len: usize,
) -> Option<(usize, usize, String)> {
    LOWER_BUF.with(|buf| {
        let mut buf = buf.borrow_mut();
        buf.clear();
        for c in line.chars() {
            for lc in c.to_lowercase() {
                buf.push(lc);
            }
        }
        finder.find(buf.as_bytes()).map(|start| {
            let end = start + pattern_lower_len;
            let match_text = extract_original_match(line, &buf, start, end);
            (start, end, match_text)
        })
    })
}

// ---------------------------------------------------------------------------
// PyO3 exports
// ---------------------------------------------------------------------------

/// Fast content search using Rust regex or SIMD-accelerated memchr for literals.
#[pyfunction]
#[pyo3(signature = (pattern, file_contents, ignore_case=false, max_results=1000))]
pub fn grep_bulk<'py>(
    py: Python<'py>,
    pattern: &str,
    file_contents: &Bound<PyDict>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    let is_literal = is_literal_pattern(pattern);

    let pattern_lower: String;
    let search_mode = if is_literal {
        if ignore_case {
            pattern_lower = pattern.to_lowercase();
            SearchMode::LiteralIgnoreCase {
                finder: memmem::Finder::new(pattern_lower.as_bytes()),
                pattern_lower: pattern_lower.clone(),
            }
        } else {
            SearchMode::Literal {
                finder: memmem::Finder::new(pattern.as_bytes()),
                pattern,
            }
        }
    } else {
        let regex = get_or_compile_regex(pattern, ignore_case)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        SearchMode::Regex(regex)
    };

    let mut files_data: Vec<(String, Vec<u8>)> = Vec::new();
    for (file_path_py, content_py) in file_contents.iter() {
        let file_path = match file_path_py.extract::<String>() {
            Ok(p) => p,
            Err(_) => continue,
        };
        let content_bytes = match content_py.extract::<Vec<u8>>() {
            Ok(b) => b,
            Err(_) => continue,
        };
        files_data.push((file_path, content_bytes));
    }

    let matches = py.detach(|| {
        let mut results = Vec::new();

        for (file_path, content_bytes) in files_data {
            if results.len() >= max_results {
                break;
            }

            let content_str = match simd_from_utf8(&content_bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();

                let match_result: Option<(usize, usize, String)> = match &search_mode {
                    SearchMode::Literal { finder, pattern } => {
                        finder.find(line_bytes).map(|start| {
                            let end = start + pattern.len();
                            let match_text = simd_from_utf8(&line_bytes[start..end])
                                .unwrap_or("")
                                .to_string();
                            (start, end, match_text)
                        })
                    }
                    SearchMode::LiteralIgnoreCase {
                        finder,
                        pattern_lower,
                    } => find_literal_ignore_case(line, finder, pattern_lower.len()),
                    SearchMode::Regex(regex) => regex.find(line_bytes).map(|m| {
                        let match_text = simd_from_utf8(&line_bytes[m.start()..m.end()])
                            .unwrap_or("")
                            .to_string();
                        (m.start(), m.end(), match_text)
                    }),
                };

                if let Some((_start, _end, match_text)) = match_result {
                    results.push(GrepMatch {
                        file: file_path.clone(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }

        results
    });

    let py_list = PyList::empty(py);
    for m in matches {
        let dict = PyDict::new(py);
        dict.set_item("file", m.file)?;
        dict.set_item("line", m.line)?;
        dict.set_item("content", m.content)?;
        dict.set_item("match", m.match_text)?;
        py_list.append(dict)?;
    }

    Ok(py_list)
}

/// Fast content search using memory-mapped I/O for zero-copy file access.
#[pyfunction]
#[pyo3(signature = (pattern, file_paths, ignore_case=false, max_results=1000))]
pub fn grep_files_mmap<'py>(
    py: Python<'py>,
    pattern: &str,
    file_paths: Vec<String>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    let is_literal = is_literal_pattern(pattern);
    let pattern_owned = pattern.to_string();

    let regex_opt: Option<regex::bytes::Regex> = if !is_literal {
        Some(
            get_or_compile_regex(pattern, ignore_case)
                .map_err(pyo3::exceptions::PyValueError::new_err)?,
        )
    } else {
        None
    };

    let pattern_lower: String = if is_literal && ignore_case {
        pattern.to_lowercase()
    } else {
        String::new()
    };

    let matches: Vec<GrepMatch> = py.detach(|| {
        if file_paths.len() < GREP_MMAP_PARALLEL_THRESHOLD {
            grep_files_mmap_sequential(
                &file_paths,
                &pattern_owned,
                &pattern_lower,
                is_literal,
                ignore_case,
                regex_opt.as_ref(),
                max_results,
            )
        } else {
            grep_files_mmap_parallel(
                file_paths,
                &pattern_owned,
                &pattern_lower,
                is_literal,
                ignore_case,
                regex_opt.as_ref(),
                max_results,
            )
        }
    });

    let py_list = PyList::empty(py);
    for m in matches {
        let dict = PyDict::new(py);
        dict.set_item("file", m.file)?;
        dict.set_item("line", m.line)?;
        dict.set_item("content", m.content)?;
        dict.set_item("match", m.match_text)?;
        py_list.append(dict)?;
    }

    Ok(py_list)
}

/// Sequential grep with mmap for small file batches.
fn grep_files_mmap_sequential(
    file_paths: &[String],
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Vec<GrepMatch> {
    let mut results = Vec::new();

    for file_path in file_paths {
        if results.len() >= max_results {
            break;
        }

        if let Some(mut file_matches) = grep_single_file_mmap(
            file_path,
            pattern,
            pattern_lower,
            is_literal,
            ignore_case,
            regex_opt,
            max_results - results.len(),
        ) {
            results.append(&mut file_matches);
        }
    }

    results
}

/// Parallel grep with mmap for large file batches.
fn grep_files_mmap_parallel(
    file_paths: Vec<String>,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Vec<GrepMatch> {
    use std::sync::atomic::{AtomicUsize, Ordering};

    let result_count = AtomicUsize::new(0);

    let all_matches: Vec<Vec<GrepMatch>> = file_paths
        .into_par_iter()
        .filter_map(|file_path| {
            if result_count.load(Ordering::Relaxed) >= max_results {
                return None;
            }

            let remaining = max_results.saturating_sub(result_count.load(Ordering::Relaxed));
            if remaining == 0 {
                return None;
            }

            let matches = grep_single_file_mmap(
                &file_path,
                pattern,
                pattern_lower,
                is_literal,
                ignore_case,
                regex_opt,
                remaining,
            )?;

            if !matches.is_empty() {
                result_count.fetch_add(matches.len(), Ordering::Relaxed);
                Some(matches)
            } else {
                None
            }
        })
        .collect();

    let mut results: Vec<GrepMatch> = all_matches.into_iter().flatten().collect();
    results.truncate(max_results);
    results
}

/// Search pre-loaded content string for pattern matches.
///
/// Extracted from `grep_single_file_mmap` so both the mmap and read()
/// paths share identical search logic (Issue #3711).
#[allow(clippy::too_many_arguments)]
fn search_content(
    content_str: &str,
    file_path: &str,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Vec<GrepMatch> {
    let mut results = Vec::new();

    if is_literal {
        if ignore_case {
            let finder = memmem::Finder::new(pattern_lower.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                if let Some((_start, _end, match_text)) =
                    find_literal_ignore_case(line, &finder, pattern_lower.len())
                {
                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        } else {
            let finder = memmem::Finder::new(pattern.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();
                if let Some(start) = finder.find(line_bytes) {
                    let end = start + pattern.len();
                    let match_text = simd_from_utf8(&line_bytes[start..end])
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
    } else if let Some(regex) = regex_opt {
        for (line_num, line) in content_str.lines().enumerate() {
            if results.len() >= max_results {
                break;
            }

            let line_bytes = line.as_bytes();
            if let Some(m) = regex.find(line_bytes) {
                let match_text = simd_from_utf8(&line_bytes[m.start()..m.end()])
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

    results
}

/// Grep a single file using memory-mapped I/O (large files) or regular
/// read (small files).
///
/// Issue #3711: Files below `GREP_MMAP_SMALL_FILE_THRESHOLD` are read
/// with `std::fs::read()` to avoid mmap page-table overhead that dominates
/// on small-file corpora.
fn grep_single_file_mmap(
    file_path: &str,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Option<Vec<GrepMatch>> {
    let file = File::open(file_path).ok()?;
    let metadata = file.metadata().ok()?;
    let file_size = metadata.len();

    if file_size == 0 {
        return Some(Vec::new());
    }

    if file_size > GREP_MMAP_MAX_FILE_SIZE {
        return None;
    }

    // Issue #3711: For small files, read() avoids mmap syscall overhead.
    if file_size < GREP_MMAP_SMALL_FILE_THRESHOLD {
        let bytes = std::fs::read(file_path).ok()?;
        let content_str = simd_from_utf8(&bytes).ok()?;
        return Some(search_content(
            content_str,
            file_path,
            pattern,
            pattern_lower,
            is_literal,
            ignore_case,
            regex_opt,
            max_results,
        ));
    }

    // SAFETY: Read-only mmap, same approach as ripgrep.
    let mmap = unsafe { Mmap::map(&file).ok()? };
    let content_str = simd_from_utf8(&mmap).ok()?;

    Some(search_content(
        content_str,
        file_path,
        pattern,
        pattern_lower,
        is_literal,
        ignore_case,
        regex_opt,
        max_results,
    ))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    // -- Regex cache --------------------------------------------------------

    #[test]
    fn test_regex_cache_key_deterministic() {
        let k1 = regex_cache_key("foo", true);
        let k2 = regex_cache_key("foo", true);
        assert_eq!(k1, k2);
    }

    #[test]
    fn test_regex_cache_key_varies_by_pattern() {
        let k1 = regex_cache_key("foo", false);
        let k2 = regex_cache_key("bar", false);
        assert_ne!(k1, k2);
    }

    #[test]
    fn test_regex_cache_key_varies_by_case_flag() {
        let k1 = regex_cache_key("foo", true);
        let k2 = regex_cache_key("foo", false);
        assert_ne!(k1, k2);
    }

    #[test]
    fn test_get_or_compile_regex_basic() {
        let re = get_or_compile_regex("hello", false).unwrap();
        assert!(re.is_match(b"say hello world"));
        assert!(!re.is_match(b"Say HELLO World"));
    }

    #[test]
    fn test_get_or_compile_regex_case_insensitive() {
        let re = get_or_compile_regex("hello", true).unwrap();
        assert!(re.is_match(b"Say HELLO World"));
    }

    #[test]
    fn test_get_or_compile_regex_cache_hit() {
        // Compile once.
        let re1 = get_or_compile_regex("test_pattern_123", false).unwrap();
        // Should hit cache.
        let re2 = get_or_compile_regex("test_pattern_123", false).unwrap();
        // Both should produce the same match results.
        assert_eq!(
            re1.is_match(b"test_pattern_123"),
            re2.is_match(b"test_pattern_123")
        );
    }

    #[test]
    fn test_get_or_compile_regex_invalid_pattern() {
        let result = get_or_compile_regex("[invalid", false);
        assert!(result.is_err());
    }

    // -- find_literal_ignore_case -------------------------------------------

    #[test]
    fn test_find_ignore_case_basic() {
        let pattern = "hello";
        let finder = memmem::Finder::new(pattern.as_bytes());
        let result = find_literal_ignore_case("Say Hello World", &finder, pattern.len());
        assert!(result.is_some());
        let (_start, _end, match_text) = result.unwrap();
        assert_eq!(match_text, "Hello");
    }

    #[test]
    fn test_find_ignore_case_no_match() {
        let pattern = "xyz";
        let finder = memmem::Finder::new(pattern.as_bytes());
        let result = find_literal_ignore_case("Say Hello World", &finder, pattern.len());
        assert!(result.is_none());
    }

    #[test]
    fn test_find_ignore_case_unicode() {
        // German: "straße" should match "STRASSE" when lowercased.
        // Note: 'ß'.to_lowercase() = "ß" (stays same), not "ss".
        // So searching for "straße" in "Straße" should work.
        let pattern = "straße";
        let finder = memmem::Finder::new(pattern.as_bytes());
        let result = find_literal_ignore_case("Die Straße ist lang", &finder, pattern.len());
        assert!(result.is_some());
        let (_, _, match_text) = result.unwrap();
        assert_eq!(match_text, "Straße");
    }

    #[test]
    fn test_find_ignore_case_all_caps() {
        let pattern = "hello";
        let finder = memmem::Finder::new(pattern.as_bytes());
        let result = find_literal_ignore_case("HELLO WORLD", &finder, pattern.len());
        assert!(result.is_some());
        let (_, _, match_text) = result.unwrap();
        assert_eq!(match_text, "HELLO");
    }

    #[test]
    fn test_find_ignore_case_empty_line() {
        let pattern = "test";
        let finder = memmem::Finder::new(pattern.as_bytes());
        let result = find_literal_ignore_case("", &finder, pattern.len());
        assert!(result.is_none());
    }

    // -- grep_single_file_mmap ----------------------------------------------

    fn write_temp_file(content: &str) -> (tempfile::NamedTempFile, String) {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f.flush().unwrap();
        let path = f.path().to_string_lossy().to_string();
        (f, path)
    }

    #[test]
    fn test_grep_mmap_literal_case_sensitive() {
        let (_f, path) = write_temp_file("Hello World\nhello world\nHELLO WORLD\n");
        let results = grep_single_file_mmap(&path, "hello", "", true, false, None, 100).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].line, 2);
        assert_eq!(results[0].match_text, "hello");
    }

    #[test]
    fn test_grep_mmap_literal_case_insensitive() {
        let (_f, path) = write_temp_file("Hello World\nhello world\nHELLO WORLD\n");
        let results =
            grep_single_file_mmap(&path, "hello", "hello", true, true, None, 100).unwrap();
        assert_eq!(results.len(), 3);
        assert_eq!(results[0].match_text, "Hello");
        assert_eq!(results[1].match_text, "hello");
        assert_eq!(results[2].match_text, "HELLO");
    }

    #[test]
    fn test_grep_mmap_regex() {
        let (_f, path) = write_temp_file("fn main() {}\nlet x = 42;\nfn helper() {}\n");
        let regex = RegexBuilder::new(r"fn \w+").build().unwrap();
        let results =
            grep_single_file_mmap(&path, r"fn \w+", "", false, false, Some(&regex), 100).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0].match_text, "fn main");
        assert_eq!(results[1].match_text, "fn helper");
    }

    #[test]
    fn test_grep_mmap_max_results() {
        let (_f, path) = write_temp_file("aaa\naaa\naaa\naaa\naaa\n");
        let results = grep_single_file_mmap(&path, "aaa", "", true, false, None, 2).unwrap();
        assert_eq!(results.len(), 2);
    }

    #[test]
    fn test_grep_mmap_empty_file() {
        let (_f, path) = write_temp_file("");
        let results = grep_single_file_mmap(&path, "test", "", true, false, None, 100).unwrap();
        assert!(results.is_empty());
    }

    #[test]
    fn test_grep_mmap_no_match() {
        let (_f, path) = write_temp_file("Hello World\n");
        let results = grep_single_file_mmap(&path, "xyz", "", true, false, None, 100).unwrap();
        assert!(results.is_empty());
    }

    #[test]
    fn test_grep_mmap_nonexistent_file() {
        let result = grep_single_file_mmap(
            "/nonexistent/path/file.txt",
            "test",
            "",
            true,
            false,
            None,
            100,
        );
        assert!(result.is_none());
    }
}
