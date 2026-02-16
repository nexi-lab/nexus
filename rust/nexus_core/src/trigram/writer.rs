//! Trigram index writer — serializes a built index to bytes.
//!
//! This module is WASM-safe: no file I/O, just byte serialization.

use super::builder::TrigramIndexBuilder;
use super::error::TrigramError;
use super::format::{IndexHeader, FILE_ENTRY_SIZE, HEADER_SIZE, TRIGRAM_ENTRY_SIZE, VERSION};

/// Serialize a built trigram index to bytes.
///
/// Returns the complete index file content as a `Vec<u8>`.
/// The caller is responsible for writing this to disk.
pub fn write_index(builder: &TrigramIndexBuilder) -> Result<Vec<u8>, TrigramError> {
    let files = builder.files();
    let sorted_postings = builder.sorted_posting_lists();

    // Phase 1: Compute sizes for all sections.

    // File table: entries + concatenated path bytes.
    let mut path_bytes_total: usize = 0;
    for f in files {
        path_bytes_total += f.path.len();
    }
    let file_table_size = files.len() * FILE_ENTRY_SIZE + path_bytes_total + 4; // +4 for section CRC32

    // Trigram table: sorted entries.
    let trigram_table_size = sorted_postings.len() * TRIGRAM_ENTRY_SIZE + 4; // +4 for section CRC32

    // Posting lists: serialize each Roaring bitmap.
    let mut serialized_postings: Vec<Vec<u8>> = Vec::with_capacity(sorted_postings.len());
    let mut posting_data_size: usize = 0;
    for (_, bitmap) in &sorted_postings {
        let mut buf = Vec::new();
        bitmap
            .serialize_into(&mut buf)
            .map_err(|e| TrigramError::CorruptIndex {
                reason: format!("Failed to serialize posting list: {}", e),
            })?;
        posting_data_size += buf.len();
        serialized_postings.push(buf);
    }
    let posting_section_size = posting_data_size + 4; // +4 for section CRC32

    // Phase 2: Compute offsets.
    let file_table_offset = HEADER_SIZE as u64;
    let trigram_table_offset = file_table_offset + file_table_size as u64;
    let posting_offset = trigram_table_offset + trigram_table_size as u64;
    let total_size = HEADER_SIZE + file_table_size + trigram_table_size + posting_section_size;

    let mut output = Vec::with_capacity(total_size);

    // Phase 3: Write header.
    let header = IndexHeader {
        version: VERSION,
        flags: 0,
        file_count: builder.file_count(),
        trigram_count: builder.trigram_count(),
        file_table_offset,
        trigram_table_offset,
        posting_offset,
    };
    output.extend_from_slice(&header.to_bytes());

    // Phase 4: Write file table.
    let file_table_start = output.len();
    let mut path_offset: u32 = (files.len() * FILE_ENTRY_SIZE) as u32;
    let mut all_paths = Vec::new();
    for f in files {
        let path_len: u16 = f
            .path
            .len()
            .try_into()
            .map_err(|_| TrigramError::CorruptIndex {
                reason: format!(
                    "File path too long ({} bytes, max {}): {}",
                    f.path.len(),
                    u16::MAX,
                    &f.path[..f.path.len().min(80)]
                ),
            })?;
        output.extend_from_slice(&f.file_id.to_le_bytes());
        output.extend_from_slice(&path_offset.to_le_bytes());
        output.extend_from_slice(&path_len.to_le_bytes());
        all_paths.extend_from_slice(f.path.as_bytes());
        path_offset += path_len as u32;
    }
    output.extend_from_slice(&all_paths);
    // File table CRC32.
    let file_table_crc = crc32fast::hash(&output[file_table_start..]);
    output.extend_from_slice(&file_table_crc.to_le_bytes());

    // Phase 5: Write trigram table.
    let trigram_table_start = output.len();
    let mut current_posting_offset: u32 = 0;
    for (i, (trigram, _)) in sorted_postings.iter().enumerate() {
        output.extend_from_slice(trigram);
        output.extend_from_slice(&current_posting_offset.to_le_bytes());
        let posting_len = serialized_postings[i].len() as u32;
        output.extend_from_slice(&posting_len.to_le_bytes());
        current_posting_offset += posting_len;
    }
    // Trigram table CRC32.
    let trigram_table_crc = crc32fast::hash(&output[trigram_table_start..]);
    output.extend_from_slice(&trigram_table_crc.to_le_bytes());

    // Phase 6: Write posting lists.
    let posting_start = output.len();
    for serialized in &serialized_postings {
        output.extend_from_slice(serialized);
    }
    // Posting section CRC32.
    let posting_crc = crc32fast::hash(&output[posting_start..]);
    output.extend_from_slice(&posting_crc.to_le_bytes());

    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_write_empty_index() {
        let builder = TrigramIndexBuilder::new();
        let bytes = write_index(&builder).expect("Should serialize empty index");
        // At minimum: header (48) + file table CRC (4) + trigram table CRC (4) + posting CRC (4)
        assert!(bytes.len() >= HEADER_SIZE + 12);
        assert_eq!(&bytes[0..4], b"TRGM");
    }

    #[test]
    fn test_write_single_file() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("hello.txt", b"hello world");
        let bytes = write_index(&builder).expect("Should serialize");
        assert_eq!(&bytes[0..4], b"TRGM");
        assert!(bytes.len() > HEADER_SIZE);
    }

    #[test]
    fn test_write_preserves_header() {
        let mut builder = TrigramIndexBuilder::new();
        for i in 0..10 {
            builder.add_file(
                &format!("file_{}.txt", i),
                format!("content of file {}", i).as_bytes(),
            );
        }
        let bytes = write_index(&builder).expect("Should serialize");

        let header = IndexHeader::from_bytes(&bytes).expect("Should parse header");
        assert_eq!(header.version, VERSION);
        assert_eq!(header.file_count, 10);
        assert!(header.trigram_count > 0);
    }

    #[test]
    fn test_corrupt_header_detected() {
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("test.txt", b"hello world");
        let mut bytes = write_index(&builder).expect("Should serialize");
        // Corrupt magic bytes.
        bytes[0] = b'X';
        assert!(IndexHeader::from_bytes(&bytes).is_none());
    }

    #[test]
    fn test_truncated_file_detected() {
        // A truncated file shorter than the header should fail to parse.
        let short_bytes = vec![0u8; 10];
        assert!(IndexHeader::from_bytes(&short_bytes).is_none());
    }

    #[test]
    fn test_roundtrip_build_and_search() {
        // Build → serialize → deserialize header → verify structure.
        let mut builder = TrigramIndexBuilder::new();
        builder.add_file("a.txt", b"hello world");
        builder.add_file("b.txt", b"foo bar baz");
        builder.add_file("c.txt", b"hello foo");

        let bytes = write_index(&builder).expect("Should serialize");
        let header = IndexHeader::from_bytes(&bytes).expect("Should parse header");

        assert_eq!(header.file_count, 3);
        assert!(header.trigram_count > 0);
        assert_eq!(header.version, VERSION);
        // Verify sections are at expected offsets.
        assert_eq!(header.file_table_offset, HEADER_SIZE as u64);
        assert!(header.trigram_table_offset > header.file_table_offset);
        assert!(header.posting_offset > header.trigram_table_offset);
    }
}

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #[test]
        fn prop_roundtrip_serialization(
            file_count in 0..20u32,
            content_seed in prop::collection::vec(any::<u8>(), 10..100)
        ) {
            let mut builder = TrigramIndexBuilder::new();
            for i in 0..file_count {
                // Generate distinct content per file using seed + index.
                let mut content = content_seed.clone();
                content.extend_from_slice(&i.to_le_bytes());
                // Ensure content is not detected as binary (remove null bytes).
                content.retain(|&b| b != 0);
                if content.len() >= 3 {
                    builder.add_file(&format!("file_{}.txt", i), &content);
                }
            }

            let bytes = write_index(&builder).expect("Should serialize");
            let header = IndexHeader::from_bytes(&bytes).expect("Should parse header");

            // Roundtrip preserves file count.
            prop_assert_eq!(header.file_count, builder.file_count());
            prop_assert_eq!(header.trigram_count, builder.trigram_count());
        }
    }
}
