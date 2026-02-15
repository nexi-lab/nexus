//! Trigram index reader + PyO3 wrappers.
//!
//! This module provides:
//! - `TrigramIndexReader` — mmap-based index reader with binary search
//! - PyO3 functions: `build_trigram_index`, `trigram_grep`, `trigram_index_stats`
//! - Thread-safe index cache for per-zone lazy loading

use memmap2::Mmap;
use nexus_core::search::grep::GrepMatch;
use nexus_core::search::{build_search_mode, search_lines};
use nexus_core::trigram::builder::TrigramIndexBuilder;
use nexus_core::trigram::error::TrigramError;
use nexus_core::trigram::format::{
    IndexHeader, FILE_ENTRY_SIZE, HEADER_SIZE, TRIGRAM_ENTRY_SIZE, VERSION,
};
use nexus_core::trigram::posting::{intersect, union, PostingList};
use nexus_core::trigram::query::{build_trigram_query, TrigramQuery};
use nexus_core::trigram::write_index;
use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use roaring::RoaringBitmap;
use simdutf8::basic::from_utf8 as simd_from_utf8;
use std::collections::HashMap;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Threshold for parallel candidate verification.
const PARALLEL_VERIFY_THRESHOLD: usize = 10;

/// Maximum file size for candidate verification (1 GB).
const MAX_VERIFY_FILE_SIZE: u64 = 1024 * 1024 * 1024;

// ---------------------------------------------------------------------------
// Index Reader
// ---------------------------------------------------------------------------

/// Memory-mapped trigram index reader.
///
/// Holds an mmap reference to the index file and provides search operations.
/// Thread-safe: `Send + Sync` (read-only mmap).
pub struct TrigramIndexReader {
    mmap: Mmap,
    header: IndexHeader,
}

/// Verify a section's CRC32 checksum.
///
/// Each section is laid out as: `[data bytes][crc32 (4 bytes)]`.
/// The CRC is computed over the data bytes only.
fn verify_section_crc(
    data: &[u8],
    section_start: usize,
    section_end: usize,
    name: &str,
) -> Result<(), TrigramError> {
    if section_end < section_start + 4 || section_end > data.len() {
        return Err(TrigramError::CorruptIndex {
            reason: format!("{} section too small for CRC", name),
        });
    }
    let crc_start = section_end - 4;
    let stored_crc = u32::from_le_bytes([
        data[crc_start],
        data[crc_start + 1],
        data[crc_start + 2],
        data[crc_start + 3],
    ]);
    let computed_crc = crc32fast::hash(&data[section_start..crc_start]);
    if stored_crc != computed_crc {
        return Err(TrigramError::CorruptIndex {
            reason: format!("{} CRC mismatch (stored={:#010x}, computed={:#010x})", name, stored_crc, computed_crc),
        });
    }
    Ok(())
}

impl TrigramIndexReader {
    /// Open and validate a trigram index file.
    pub fn open(path: &Path) -> Result<Self, TrigramError> {
        if !path.exists() {
            return Err(TrigramError::IndexNotFound(path.to_path_buf()));
        }

        let file = File::open(path)?;
        let metadata = file.metadata()?;

        if metadata.len() < HEADER_SIZE as u64 {
            return Err(TrigramError::CorruptIndex {
                reason: "File too small for header".to_string(),
            });
        }

        // SAFETY: Read-only mmap.
        let mmap = unsafe { Mmap::map(&file)? };

        let header = IndexHeader::from_bytes(&mmap).ok_or(TrigramError::InvalidMagic)?;

        if header.version != VERSION {
            return Err(TrigramError::VersionMismatch {
                expected: VERSION,
                found: header.version,
            });
        }

        // Validate that offsets are within bounds and ordered.
        let file_len = mmap.len() as u64;
        if header.file_table_offset > file_len
            || header.trigram_table_offset > file_len
            || header.posting_offset > file_len
            || header.file_table_offset > header.trigram_table_offset
            || header.trigram_table_offset > header.posting_offset
        {
            return Err(TrigramError::CorruptIndex {
                reason: "Section offset exceeds file size or offsets not ordered".to_string(),
            });
        }

        // Verify section CRC32 checksums.
        let ft_start = header.file_table_offset as usize;
        let tt_start = header.trigram_table_offset as usize;
        let ps_start = header.posting_offset as usize;
        let end = mmap.len();

        verify_section_crc(&mmap, ft_start, tt_start, "File table")?;
        verify_section_crc(&mmap, tt_start, ps_start, "Trigram table")?;
        verify_section_crc(&mmap, ps_start, end, "Posting section")?;

        Ok(TrigramIndexReader { mmap, header })
    }

    /// Get file path for a given file ID.
    pub fn get_file_path(&self, file_id: u32) -> Option<&str> {
        let ft_offset = self.header.file_table_offset as usize;
        let file_count = self.header.file_count as usize;

        if file_id as usize >= file_count {
            return None;
        }

        let entry_offset = ft_offset + (file_id as usize) * FILE_ENTRY_SIZE;
        if entry_offset + FILE_ENTRY_SIZE > self.mmap.len() {
            return None;
        }

        let data = &self.mmap[entry_offset..];
        // Skip file_id (4 bytes), read path_offset (4 bytes) and path_len (2 bytes).
        let path_offset =
            u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
        let path_len = u16::from_le_bytes([data[8], data[9]]) as usize;

        // Path bytes are relative to the start of the file table.
        let abs_path_offset = ft_offset + path_offset;
        if abs_path_offset + path_len > self.mmap.len() {
            return None;
        }

        std::str::from_utf8(&self.mmap[abs_path_offset..abs_path_offset + path_len]).ok()
    }

    /// Look up the posting list for a trigram using binary search.
    fn lookup_posting_list(&self, trigram: &[u8; 3]) -> Option<PostingList> {
        let tt_offset = self.header.trigram_table_offset as usize;
        let trigram_count = self.header.trigram_count as usize;
        let posting_base = self.header.posting_offset as usize;

        if trigram_count == 0 {
            return None;
        }

        // Binary search in the sorted trigram table.
        let mut lo = 0usize;
        let mut hi = trigram_count;

        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let entry_offset = tt_offset + mid * TRIGRAM_ENTRY_SIZE;
            if entry_offset + TRIGRAM_ENTRY_SIZE > self.mmap.len() {
                return None;
            }

            let entry = &self.mmap[entry_offset..];
            let entry_trigram = [entry[0], entry[1], entry[2]];

            match entry_trigram.cmp(trigram) {
                std::cmp::Ordering::Equal => {
                    // Found — deserialize the posting list.
                    let p_offset =
                        u32::from_le_bytes([entry[3], entry[4], entry[5], entry[6]]) as usize;
                    let p_len =
                        u32::from_le_bytes([entry[7], entry[8], entry[9], entry[10]]) as usize;

                    let abs_offset = posting_base + p_offset;
                    if abs_offset + p_len > self.mmap.len() {
                        return None;
                    }

                    let bitmap_bytes = &self.mmap[abs_offset..abs_offset + p_len];
                    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).ok()?;
                    return Some(PostingList::from_bitmap(bitmap));
                }
                std::cmp::Ordering::Less => lo = mid + 1,
                std::cmp::Ordering::Greater => hi = mid,
            }
        }

        None
    }

    /// Get candidate file IDs matching a pattern using the trigram index.
    ///
    /// When `ignore_case` is true, the pattern is lowercased before trigram
    /// extraction, matching against the lowercased trigrams stored in the index.
    pub fn search_candidates(
        &self,
        pattern: &str,
        ignore_case: bool,
    ) -> Result<Vec<u32>, TrigramError> {
        let query_pattern = if ignore_case {
            pattern.to_lowercase()
        } else {
            pattern.to_string()
        };
        let query = build_trigram_query(&query_pattern);
        self.execute_query(&query)
    }

    /// Execute a trigram query against the index.
    fn execute_query(&self, query: &TrigramQuery) -> Result<Vec<u32>, TrigramError> {
        match query {
            TrigramQuery::All => {
                // Return all file IDs.
                Ok((0..self.header.file_count).collect())
            }
            TrigramQuery::And(trigrams) => {
                if trigrams.is_empty() {
                    return Ok((0..self.header.file_count).collect());
                }

                let mut lists = Vec::with_capacity(trigrams.len());
                for trigram in trigrams {
                    match self.lookup_posting_list(trigram) {
                        Some(pl) => lists.push(pl),
                        None => return Ok(Vec::new()), // Trigram not in index → no matches.
                    }
                }

                Ok(intersect(&lists).to_vec())
            }
            TrigramQuery::Or(sub_queries) => {
                if sub_queries.is_empty() {
                    return Ok(Vec::new());
                }

                let mut lists = Vec::new();
                for sub in sub_queries {
                    let ids = self.execute_query(sub)?;
                    let mut bitmap = RoaringBitmap::new();
                    for id in ids {
                        bitmap.insert(id);
                    }
                    lists.push(PostingList::from_bitmap(bitmap));
                }

                Ok(union(&lists).to_vec())
            }
        }
    }

    /// Full search: get candidates, verify each, return matches.
    pub fn search(
        &self,
        pattern: &str,
        ignore_case: bool,
        max_results: usize,
    ) -> Result<Vec<GrepMatch>, TrigramError> {
        let candidate_ids = self.search_candidates(pattern, ignore_case)?;

        if candidate_ids.is_empty() {
            return Ok(Vec::new());
        }

        let search_mode = build_search_mode(pattern, ignore_case)
            .map_err(|e| TrigramError::InvalidPattern(e.to_string()))?;

        // Resolve candidate file IDs to paths.
        let candidates: Vec<(u32, &str)> = candidate_ids
            .iter()
            .filter_map(|&id| self.get_file_path(id).map(|p| (id, p)))
            .collect();

        if candidates.len() < PARALLEL_VERIFY_THRESHOLD {
            // Sequential verification.
            let mut results = Vec::new();
            for (_id, path) in &candidates {
                if results.len() >= max_results {
                    break;
                }
                if let Some(mut matches) = verify_file(path, &search_mode, max_results - results.len()) {
                    results.append(&mut matches);
                }
            }
            Ok(results)
        } else {
            // Parallel verification with rayon.
            use std::sync::atomic::{AtomicUsize, Ordering};
            let count = AtomicUsize::new(0);

            let all_matches: Vec<Vec<GrepMatch>> = candidates
                .par_iter()
                .filter_map(|(_id, path)| {
                    if count.load(Ordering::Relaxed) >= max_results {
                        return None;
                    }
                    let remaining = max_results.saturating_sub(count.load(Ordering::Relaxed));
                    if remaining == 0 {
                        return None;
                    }
                    let matches = verify_file(path, &search_mode, remaining)?;
                    if !matches.is_empty() {
                        count.fetch_add(matches.len(), Ordering::Relaxed);
                        Some(matches)
                    } else {
                        None
                    }
                })
                .collect();

            let mut results: Vec<GrepMatch> = all_matches.into_iter().flatten().collect();
            results.truncate(max_results);
            Ok(results)
        }
    }

    /// Number of files in the index.
    pub fn file_count(&self) -> u32 {
        self.header.file_count
    }

    /// Number of unique trigrams in the index.
    pub fn trigram_count(&self) -> u32 {
        self.header.trigram_count
    }

    /// Index file size in bytes.
    pub fn index_size(&self) -> usize {
        self.mmap.len()
    }
}

/// Verify a candidate file against the search pattern.
fn verify_file(
    path: &str,
    search_mode: &nexus_core::search::SearchMode,
    max_results: usize,
) -> Option<Vec<GrepMatch>> {
    let file = File::open(path).ok()?;
    let metadata = file.metadata().ok()?;

    if metadata.len() == 0 || metadata.len() > MAX_VERIFY_FILE_SIZE {
        return None;
    }

    // SAFETY: Read-only mmap.
    let mmap = unsafe { Mmap::map(&file).ok()? };
    let content = simd_from_utf8(&mmap).ok()?;

    let matches = search_lines(path, content, search_mode, max_results);
    if matches.is_empty() {
        None
    } else {
        Some(matches)
    }
}

// ---------------------------------------------------------------------------
// Index Cache
// ---------------------------------------------------------------------------

/// Global index cache: zone_path → reader.
static INDEX_CACHE: std::sync::LazyLock<RwLock<HashMap<PathBuf, Arc<TrigramIndexReader>>>> =
    std::sync::LazyLock::new(|| RwLock::new(HashMap::new()));

/// Get or open a cached index reader.
fn get_cached_reader(path: &Path) -> Result<Arc<TrigramIndexReader>, TrigramError> {
    // Fast path: read lock.
    {
        let cache = INDEX_CACHE.read();
        if let Some(reader) = cache.get(path) {
            return Ok(Arc::clone(reader));
        }
    }

    // Slow path: write lock, re-check to avoid TOCTOU race.
    let mut cache = INDEX_CACHE.write();
    if let Some(reader) = cache.get(path) {
        return Ok(Arc::clone(reader));
    }
    let reader = Arc::new(TrigramIndexReader::open(path)?);
    cache.insert(path.to_path_buf(), Arc::clone(&reader));
    Ok(reader)
}

// ---------------------------------------------------------------------------
// PyO3 Functions
// ---------------------------------------------------------------------------

/// Build a trigram index from a list of file paths and write to output_path.
#[pyfunction]
#[pyo3(signature = (file_paths, output_path))]
pub fn build_trigram_index(file_paths: Vec<String>, output_path: &str) -> PyResult<()> {
    let mut builder = TrigramIndexBuilder::new();

    for path in &file_paths {
        let content = match std::fs::read(path) {
            Ok(c) => c,
            Err(_) => continue, // Skip unreadable files.
        };
        builder.add_file(path, &content);
    }

    let bytes = write_index(&builder).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to build index: {}", e))
    })?;

    let mut file = File::create(output_path).map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to create index file: {}", e))
    })?;
    file.write_all(&bytes).map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to write index: {}", e))
    })?;

    // Invalidate cache for this path.
    let path = PathBuf::from(output_path);
    let mut cache = INDEX_CACHE.write();
    cache.remove(&path);

    Ok(())
}

/// Build a trigram index from (path, content) pairs without disk I/O.
///
/// This is used when the caller already has file content in memory
/// (e.g., from NexusFS CAS backend where files are content-addressed).
#[pyfunction]
#[pyo3(signature = (entries, output_path))]
pub fn build_trigram_index_from_entries(
    entries: Vec<(String, Vec<u8>)>,
    output_path: &str,
) -> PyResult<()> {
    let mut builder = TrigramIndexBuilder::new();

    for (path, content) in &entries {
        builder.add_file(path, content);
    }

    let bytes = write_index(&builder).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to build index: {}", e))
    })?;

    let mut file = File::create(output_path).map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to create index file: {}", e))
    })?;
    file.write_all(&bytes).map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to write index: {}", e))
    })?;

    // Invalidate cache for this path.
    let path = PathBuf::from(output_path);
    let mut cache = INDEX_CACHE.write();
    cache.remove(&path);

    Ok(())
}

/// Return candidate file paths from the trigram index without verification.
///
/// This is used when the caller will verify candidates itself (e.g., by
/// reading content through NexusFS rather than direct file I/O).
#[pyfunction]
#[pyo3(signature = (index_path, pattern, ignore_case=false))]
pub fn trigram_search_candidates(
    index_path: &str,
    pattern: &str,
    ignore_case: bool,
) -> PyResult<Vec<String>> {
    let path = PathBuf::from(index_path);
    let reader = get_cached_reader(&path).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to open index: {}", e))
    })?;

    let candidate_ids = reader.search_candidates(pattern, ignore_case).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to search index: {}", e))
    })?;

    let paths: Vec<String> = candidate_ids
        .iter()
        .filter_map(|&id| reader.get_file_path(id).map(|p| p.to_string()))
        .collect();

    Ok(paths)
}

/// Search using trigram index. Returns list of match dicts.
#[pyfunction]
#[pyo3(signature = (index_path, pattern, ignore_case=false, max_results=1000))]
pub fn trigram_grep<'py>(
    py: Python<'py>,
    index_path: &str,
    pattern: &str,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<pyo3::Bound<'py, pyo3::types::PyList>> {
    let path = PathBuf::from(index_path);
    let reader = get_cached_reader(&path).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to open index: {}", e))
    })?;

    let matches = py.detach(|| {
        reader
            .search(pattern, ignore_case, max_results)
            .unwrap_or_default()
    });

    let py_list = pyo3::types::PyList::empty(py);
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

/// Get statistics about a trigram index.
#[pyfunction]
#[pyo3(signature = (index_path,))]
pub fn trigram_index_stats<'py>(
    py: Python<'py>,
    index_path: &str,
) -> PyResult<pyo3::Bound<'py, PyDict>> {
    let path = PathBuf::from(index_path);
    let reader = get_cached_reader(&path).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to open index: {}", e))
    })?;

    let dict = PyDict::new(py);
    dict.set_item("file_count", reader.file_count())?;
    dict.set_item("trigram_count", reader.trigram_count())?;
    dict.set_item("index_size_bytes", reader.index_size())?;

    Ok(dict)
}

/// Invalidate a cached trigram index reader.
#[pyfunction]
#[pyo3(signature = (index_path,))]
pub fn invalidate_trigram_cache(index_path: &str) -> PyResult<()> {
    let path = PathBuf::from(index_path);
    let mut cache = INDEX_CACHE.write();
    cache.remove(&path);
    Ok(())
}
