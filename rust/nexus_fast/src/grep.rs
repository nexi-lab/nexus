// =============================================================================
// Fast Content Search (Grep) with SIMD Acceleration
// =============================================================================

use memchr::memmem;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use simdutf8::basic::from_utf8 as simd_from_utf8;
use std::fs::File;

/// Threshold for using parallel processing in grep_files_mmap
const GREP_MMAP_PARALLEL_THRESHOLD: usize = 10;

/// Maximum file size to mmap (avoid excessive memory usage for huge files)
const GREP_MMAP_MAX_FILE_SIZE: u64 = 1024 * 1024 * 1024; // 1GB

/// Grep search result
#[derive(Debug)]
struct GrepMatch {
    file: String,
    line: usize,
    content: String,
    match_text: String,
}

/// Check if a pattern is a literal string (no regex metacharacters).
/// Literal patterns can use SIMD-accelerated memchr search (Issue #863).
fn is_literal_pattern(pattern: &str) -> bool {
    !pattern.chars().any(|c| {
        matches!(
            c,
            '.' | '*' | '+' | '?' | '(' | ')' | '[' | ']' | '{' | '}' | '|' | '^' | '$' | '\\'
        )
    })
}

/// Search mode for grep_bulk - either SIMD-accelerated literal or regex
enum SearchMode<'a> {
    /// SIMD-accelerated literal search using memchr (4-10x faster)
    Literal {
        finder: memmem::Finder<'a>,
        pattern: &'a str,
    },
    /// Case-insensitive literal search (converts line to lowercase)
    LiteralIgnoreCase {
        finder: memmem::Finder<'a>,
        pattern_lower: String,
    },
    /// Full regex search for complex patterns
    Regex(regex::bytes::Regex),
}

/// Fast content search using Rust regex or SIMD-accelerated memchr for literals
#[pyfunction]
#[pyo3(signature = (pattern, file_contents, ignore_case=false, max_results=1000))]
pub fn grep_bulk<'py>(
    py: Python<'py>,
    pattern: &str,
    file_contents: &Bound<PyDict>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    // Determine search mode: use SIMD-accelerated memchr for literal patterns (Issue #863)
    let is_literal = is_literal_pattern(pattern);

    // For case-insensitive literal search, we need to own the lowercase pattern
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
        // Fall back to regex for complex patterns
        let regex = RegexBuilder::new(pattern)
            .case_insensitive(ignore_case)
            .build()
            .map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
            })?;
        SearchMode::Regex(regex)
    };

    // Extract all file contents from Python objects first
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

    // Release GIL for computation
    let matches = py.detach(|| {
        let mut results = Vec::new();

        // Iterate over extracted file contents
        for (file_path, content_bytes) in files_data {
            if results.len() >= max_results {
                break;
            }

            // Try to decode as UTF-8 using SIMD-accelerated validation (Issue #864)
            // simdutf8 is ~8x faster than std::str::from_utf8
            let content_str = match simd_from_utf8(&content_bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };

            // Search line by line
            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();

                // Use appropriate search mode
                let match_result: Option<(usize, usize)> = match &search_mode {
                    SearchMode::Literal { finder, pattern } => {
                        // SIMD-accelerated literal search
                        finder
                            .find(line_bytes)
                            .map(|start| (start, start + pattern.len()))
                    }
                    SearchMode::LiteralIgnoreCase {
                        finder,
                        pattern_lower,
                    } => {
                        // Case-insensitive: convert line to lowercase and search
                        let line_lower = line.to_lowercase();
                        finder
                            .find(line_lower.as_bytes())
                            .map(|start| (start, start + pattern_lower.len()))
                    }
                    SearchMode::Regex(regex) => {
                        // Full regex search
                        regex.find(line_bytes).map(|m| (m.start(), m.end()))
                    }
                };

                if let Some((start, end)) = match_result {
                    // For case-insensitive literal, extract match from original line
                    let match_text = if matches!(&search_mode, SearchMode::LiteralIgnoreCase { .. })
                    {
                        // Get character boundaries for the match
                        line.chars()
                            .skip(line[..start].chars().count())
                            .take(end - start)
                            .collect::<String>()
                    } else {
                        simd_from_utf8(&line_bytes[start..end])
                            .unwrap_or("")
                            .to_string()
                    };

                    results.push(GrepMatch {
                        file: file_path.clone(),
                        line: line_num + 1, // 1-indexed
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }

        results
    });

    // Convert results to Python list of dicts
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

/// Fast content search using memory-mapped I/O for zero-copy file access (Issue #893)
///
/// This function reads files directly from disk using mmap, avoiding the overhead
/// of passing file contents through Python. Best for searching large local files.
///
/// Performance characteristics:
/// - Small files (<4KB): Similar to grep_bulk (mmap overhead vs copy overhead)
/// - Medium files (4KB-10MB): 20-40% faster than grep_bulk
/// - Large files (>10MB): 50-70% faster than grep_bulk
/// - Parallel processing for batches of 10+ files
///
/// Args:
///     pattern: Regex pattern or literal string to search for
///     file_paths: List of absolute paths to search
///     ignore_case: Whether to ignore case in pattern matching
///     max_results: Maximum number of results to return
///
/// Returns:
///     List of match dicts with keys: file, line, content, match
///     Files that don't exist or can't be read are silently skipped.
#[pyfunction]
#[pyo3(signature = (pattern, file_paths, ignore_case=false, max_results=1000))]
pub fn grep_files_mmap<'py>(
    py: Python<'py>,
    pattern: &str,
    file_paths: Vec<String>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    // Determine search mode: use SIMD-accelerated memchr for literal patterns
    let is_literal = is_literal_pattern(pattern);

    // Build the search pattern/regex
    let pattern_owned = pattern.to_string();

    // For parallel processing, we need to create thread-safe search components
    let regex_opt: Option<regex::bytes::Regex> = if !is_literal {
        Some(
            RegexBuilder::new(pattern)
                .case_insensitive(ignore_case)
                .build()
                .map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
                })?,
        )
    } else {
        None
    };

    let pattern_lower: String = if is_literal && ignore_case {
        pattern.to_lowercase()
    } else {
        String::new()
    };

    // Process files - parallel for large batches, sequential for small
    let matches: Vec<GrepMatch> = py.detach(|| {
        if file_paths.len() < GREP_MMAP_PARALLEL_THRESHOLD {
            // Sequential processing for small batches
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
            // Parallel processing for large batches
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

    // Convert results to Python list of dicts
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

/// Sequential grep with mmap for small file batches
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

/// Parallel grep with mmap for large file batches
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
            // Early exit if we've hit max results
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

    // Flatten and truncate to max_results
    let mut results: Vec<GrepMatch> = all_matches.into_iter().flatten().collect();
    results.truncate(max_results);
    results
}

/// Grep a single file using memory-mapped I/O
fn grep_single_file_mmap(
    file_path: &str,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Option<Vec<GrepMatch>> {
    // Open the file
    let file = File::open(file_path).ok()?;
    let metadata = file.metadata().ok()?;
    let file_size = metadata.len();

    // Skip empty files
    if file_size == 0 {
        return Some(Vec::new());
    }

    // For very large files, skip mmap to avoid memory pressure
    if file_size > GREP_MMAP_MAX_FILE_SIZE {
        return None; // Let caller fall back to chunked reading
    }

    // Memory-map the file
    // SAFETY: The file is opened read-only and we only read from the mmap.
    // External modifications could cause undefined behavior, but this is
    // acceptable for grep operations (same approach as ripgrep).
    let mmap = unsafe { Mmap::map(&file).ok()? };

    // Try to decode as UTF-8 using SIMD-accelerated validation
    let content_str = simd_from_utf8(&mmap).ok()?;

    let mut results = Vec::new();

    // Create search mode based on pattern type
    if is_literal {
        if ignore_case {
            // Case-insensitive literal search
            let finder = memmem::Finder::new(pattern_lower.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_lower = line.to_lowercase();
                if let Some(start) = finder.find(line_lower.as_bytes()) {
                    let end = start + pattern_lower.len();
                    // Extract match from original line (preserving case)
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
        } else {
            // Case-sensitive literal search (SIMD-accelerated)
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
        // Regex search
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

    Some(results)
}
