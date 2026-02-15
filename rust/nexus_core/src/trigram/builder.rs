//! Trigram index builder — accumulates files and their trigrams.

use ahash::AHashMap;
use roaring::RoaringBitmap;

use super::extract::{extract_trigrams, is_binary};

/// Maximum content size for indexing (1 GB).
const MAX_INDEX_FILE_SIZE: usize = 1024 * 1024 * 1024;

/// Entry for a file in the index.
#[derive(Debug, Clone)]
pub struct FileEntry {
    pub file_id: u32,
    pub path: String,
}

/// Builder for constructing a trigram index in memory.
///
/// Accumulates files and their trigrams, then serializes to the binary format
/// via `writer::write_index()`.
#[derive(Debug)]
pub struct TrigramIndexBuilder {
    /// Registered files in insertion order.
    files: Vec<FileEntry>,
    /// Trigram → set of file IDs containing this trigram.
    posting_lists: AHashMap<[u8; 3], RoaringBitmap>,
    /// Next file ID to assign.
    next_file_id: u32,
}

impl TrigramIndexBuilder {
    /// Create a new empty builder.
    pub fn new() -> Self {
        TrigramIndexBuilder {
            files: Vec::new(),
            posting_lists: AHashMap::new(),
            next_file_id: 0,
        }
    }

    /// Add a file to the index.
    ///
    /// Extracts trigrams from the content and adds them to posting lists.
    /// Skips binary files and files exceeding the size limit.
    pub fn add_file(&mut self, path: &str, content: &[u8]) {
        // Skip oversized files.
        if content.len() > MAX_INDEX_FILE_SIZE {
            return;
        }

        // Skip binary files.
        if is_binary(content) {
            return;
        }

        let file_id = self.next_file_id;
        self.next_file_id = self
            .next_file_id
            .checked_add(1)
            .expect("TrigramIndexBuilder: file_id overflow (exceeded u32::MAX files)");

        self.files.push(FileEntry {
            file_id,
            path: path.to_string(),
        });

        // Empty files have no trigrams but are still registered.
        if content.len() < 3 {
            return;
        }

        // Extract trigrams from original content (for case-sensitive search).
        let trigrams = extract_trigrams(content);
        for trigram in &trigrams {
            self.posting_lists
                .entry(*trigram)
                .or_insert_with(RoaringBitmap::new)
                .insert(file_id);
        }

        // Also extract trigrams from lowercased content (for case-insensitive search).
        // This ensures that case-insensitive queries with lowered patterns find matches.
        if let Ok(text) = std::str::from_utf8(content) {
            let lower = text.to_lowercase();
            let lower_trigrams = extract_trigrams(lower.as_bytes());
            for trigram in &lower_trigrams {
                self.posting_lists
                    .entry(*trigram)
                    .or_insert_with(RoaringBitmap::new)
                    .insert(file_id);
            }
        }
    }

    /// Number of files in the index.
    pub fn file_count(&self) -> u32 {
        self.files.len() as u32
    }

    /// Number of unique trigrams in the index.
    pub fn trigram_count(&self) -> u32 {
        self.posting_lists.len() as u32
    }

    /// Get the file entries (for serialization).
    pub fn files(&self) -> &[FileEntry] {
        &self.files
    }

    /// Get the posting lists (for serialization).
    /// Returns entries sorted by trigram bytes for binary search.
    pub fn sorted_posting_lists(&self) -> Vec<([u8; 3], &RoaringBitmap)> {
        let mut entries: Vec<([u8; 3], &RoaringBitmap)> =
            self.posting_lists.iter().map(|(k, v)| (*k, v)).collect();
        entries.sort_by_key(|(trigram, _)| *trigram);
        entries
    }
}

impl Default for TrigramIndexBuilder {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_empty_index() {
        let builder = TrigramIndexBuilder::new();
        assert_eq!(builder.file_count(), 0);
        assert_eq!(builder.trigram_count(), 0);
    }

    #[test]
    fn test_build_single_file() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("test.rs", b"fn main() {}");
        assert_eq!(builder.file_count(), 1);
        assert!(builder.trigram_count() > 0);
    }

    #[test]
    fn test_build_100_files() {
        let mut builder = TrigramIndexBuilder::new();
        for i in 0..100 {
            let content = format!("file {} content with some text for trigrams", i);
            builder.add_file(&format!("file_{}.txt", i), content.as_bytes());
        }
        assert_eq!(builder.file_count(), 100);
        assert!(builder.trigram_count() > 0);
    }

    #[test]
    fn test_build_binary_file_skipped() {
        let mut builder = TrigramIndexBuilder::new();
        // Binary content with high null ratio.
        let mut content = vec![0u8; 100];
        content.extend_from_slice(b"some text");
        builder.add_file("binary.bin", &content);
        // Binary file is skipped entirely.
        assert_eq!(builder.file_count(), 0);
    }

    #[test]
    fn test_build_empty_file() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("empty.txt", b"");
        // File is registered but has no trigrams.
        assert_eq!(builder.file_count(), 1);
        assert_eq!(builder.trigram_count(), 0);
    }

    #[test]
    fn test_sorted_posting_lists() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("a.txt", b"hello world");
        let sorted = builder.sorted_posting_lists();
        // Verify sorted order.
        for i in 1..sorted.len() {
            assert!(sorted[i - 1].0 <= sorted[i].0);
        }
    }

    #[test]
    fn test_file_ids_sequential() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("a.txt", b"hello");
        builder.add_file("b.txt", b"world");
        assert_eq!(builder.files()[0].file_id, 0);
        assert_eq!(builder.files()[1].file_id, 1);
    }
}
