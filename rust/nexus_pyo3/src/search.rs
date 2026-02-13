//! Grep search — PyO3 wrappers with mmap and SIMD-accelerated literal search.

use memchr::memmem;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use simdutf8::basic::from_utf8 as simd_from_utf8;
use std::fs::File;

/// Grep search result.
#[derive(Debug)]
struct GrepMatch {
    file: String,
    line: usize,
    content: String,
    match_text: String,
}

/// Check if a pattern is a literal string (no regex metacharacters).
fn is_literal_pattern(pattern: &str) -> bool {
    !pattern.chars().any(|c| {
        matches!(
            c,
            '.' | '*' | '+' | '?' | '(' | ')' | '[' | ']' | '{' | '}' | '|' | '^' | '$' | '\\'
        )
    })
}

/// Search mode — either SIMD-accelerated literal or regex.
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
        let regex = RegexBuilder::new(pattern)
            .case_insensitive(ignore_case)
            .build()
            .map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
            })?;
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

                let match_result: Option<(usize, usize)> = match &search_mode {
                    SearchMode::Literal { finder, pattern } => finder
                        .find(line_bytes)
                        .map(|start| (start, start + pattern.len())),
                    SearchMode::LiteralIgnoreCase {
                        finder,
                        pattern_lower,
                    } => {
                        let line_lower = line.to_lowercase();
                        finder
                            .find(line_lower.as_bytes())
                            .map(|start| (start, start + pattern_lower.len()))
                    }
                    SearchMode::Regex(regex) => {
                        regex.find(line_bytes).map(|m| (m.start(), m.end()))
                    }
                };

                if let Some((start, end)) = match_result {
                    let match_text = if matches!(&search_mode, SearchMode::LiteralIgnoreCase { .. })
                    {
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

/// Grep a single file using memory-mapped I/O.
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

    // SAFETY: Read-only mmap, same approach as ripgrep.
    let mmap = unsafe { Mmap::map(&file).ok()? };
    let content_str = simd_from_utf8(&mmap).ok()?;

    let mut results = Vec::new();

    if is_literal {
        if ignore_case {
            let finder = memmem::Finder::new(pattern_lower.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
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

    Some(results)
}
