//! Crash recovery for WAL segments.
//!
//! Scans segments in filename order, validates every record's CRC32,
//! and truncates at the first corruption point.

use std::fs::{self, OpenOptions};
use std::path::Path;

use crate::segment::{parse_segment_name, SegmentError, SegmentReader, HEADER_SIZE};

/// Result of a recovery pass.
#[derive(Debug)]
pub struct RecoveryResult {
    /// Total valid records found across all segments.
    pub valid_records: u64,
    /// Bytes truncated from the last corrupt segment (0 if clean).
    pub truncated_bytes: u64,
    /// Last valid sequence number (0 if no records).
    pub last_sequence: u64,
}

/// Run crash recovery over all segments in `dir`.
///
/// Segments are processed in filename order (ascending first_seq).
/// On the first bad CRC or truncated record, the segment is truncated
/// at that byte offset. Only the *last* segment can be partially valid;
/// any segment with a bad header is skipped entirely.
pub fn recover(dir: &Path) -> Result<RecoveryResult, SegmentError> {
    let mut segments = list_segment_files(dir)?;
    segments.sort(); // alphabetical = ascending first_seq

    let mut valid_records: u64 = 0;
    let mut truncated_bytes: u64 = 0;
    let mut last_sequence: u64 = 0;

    for seg_path in &segments {
        let reader = match SegmentReader::open(seg_path) {
            Ok(r) => r,
            Err(SegmentError::BadMagic | SegmentError::BadVersion(_)) => {
                // Skip entirely corrupt segments
                continue;
            }
            Err(e) => return Err(e),
        };

        let mut last_valid_offset = HEADER_SIZE;

        for record_result in reader.iter()? {
            match record_result {
                Ok(record) => {
                    valid_records += 1;
                    last_sequence = record.seq;
                    last_valid_offset += record.wire_size();
                }
                Err(SegmentError::CrcMismatch { .. } | SegmentError::TruncatedRecord(_)) => {
                    // Truncate segment at the last valid record boundary
                    let file_len = fs::metadata(seg_path)?.len();
                    if last_valid_offset < file_len {
                        truncated_bytes = file_len - last_valid_offset;
                        truncate_file(seg_path, last_valid_offset)?;
                    }
                    // Stop processing this segment — it was the tail
                    break;
                }
                Err(e) => return Err(e),
            }
        }

        // Check for trailing bytes too short to form a complete record header.
        // The iterator returns None when < 10 bytes remain, but those bytes
        // are still garbage that should be truncated.
        let file_len = fs::metadata(seg_path)?.len();
        if last_valid_offset < file_len {
            truncated_bytes = file_len - last_valid_offset;
            truncate_file(seg_path, last_valid_offset)?;
        }
    }

    Ok(RecoveryResult {
        valid_records,
        truncated_bytes,
        last_sequence,
    })
}

/// List all `*.seg` files in a directory.
fn list_segment_files(dir: &Path) -> Result<Vec<std::path::PathBuf>, SegmentError> {
    let mut files = Vec::new();
    if !dir.exists() {
        return Ok(files);
    }
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        if name_str.ends_with(".seg") && parse_segment_name(&name_str).is_some() {
            files.push(entry.path());
        }
    }
    Ok(files)
}

/// Truncate a file to `len` bytes.
fn truncate_file(path: &Path, len: u64) -> Result<(), SegmentError> {
    let file = OpenOptions::new().write(true).open(path)?;
    file.set_len(len)?;
    file.sync_all()?;
    Ok(())
}

// ===========================================================================
// Tests
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::segment::SegmentWriter;
    use std::io::Write;
    use tempfile::TempDir;

    #[test]
    fn recover_clean_segments() {
        let dir = TempDir::new().unwrap();
        let mut w = SegmentWriter::new(dir.path(), 1, 1000).unwrap();
        for i in 1..=5 {
            w.append(i, b"z", format!("p{i}").as_bytes()).unwrap();
        }
        w.sync().unwrap();

        let result = recover(dir.path()).unwrap();
        assert_eq!(result.valid_records, 5);
        assert_eq!(result.truncated_bytes, 0);
        assert_eq!(result.last_sequence, 5);
    }

    #[test]
    fn recover_truncated_mid_record() {
        let dir = TempDir::new().unwrap();
        let mut w = SegmentWriter::new(dir.path(), 1, 2000).unwrap();
        w.append(1, b"z", b"good").unwrap();
        w.append(2, b"z", b"also-good").unwrap();
        w.sync().unwrap();

        let seg_path = w.path().to_path_buf();
        let valid_len = w.bytes_written();

        // Append partial garbage (truncated record)
        {
            let mut f = OpenOptions::new().append(true).open(&seg_path).unwrap();
            f.write_all(&[0xDE, 0xAD, 0xBE, 0xEF]).unwrap();
            f.sync_all().unwrap();
        }

        let result = recover(dir.path()).unwrap();
        assert_eq!(result.valid_records, 2);
        assert_eq!(result.truncated_bytes, 4);
        assert_eq!(result.last_sequence, 2);

        // File should be truncated back to valid_len
        let meta = fs::metadata(&seg_path).unwrap();
        assert_eq!(meta.len(), valid_len);
    }

    #[test]
    fn recover_corrupted_crc() {
        let dir = TempDir::new().unwrap();
        let mut w = SegmentWriter::new(dir.path(), 1, 3000).unwrap();
        w.append(1, b"z", b"good").unwrap();
        let good_end = w.bytes_written();
        w.append(2, b"z", b"will-corrupt").unwrap();
        w.sync().unwrap();

        let seg_path = w.path().to_path_buf();

        // Corrupt the CRC of the second record (last 4 bytes of file)
        let data = fs::read(&seg_path).unwrap();
        let mut corrupted = data.clone();
        let crc_pos = corrupted.len() - 4;
        corrupted[crc_pos] ^= 0xFF;
        fs::write(&seg_path, &corrupted).unwrap();

        let result = recover(dir.path()).unwrap();
        assert_eq!(result.valid_records, 1);
        assert!(result.truncated_bytes > 0);
        assert_eq!(result.last_sequence, 1);

        // Segment truncated to just the first record
        let meta = fs::metadata(&seg_path).unwrap();
        assert_eq!(meta.len(), good_end);
    }

    #[test]
    fn recover_empty_segment() {
        let dir = TempDir::new().unwrap();
        // Create segment with only header (no records)
        let _ = SegmentWriter::new(dir.path(), 1, 4000).unwrap();

        let result = recover(dir.path()).unwrap();
        assert_eq!(result.valid_records, 0);
        assert_eq!(result.truncated_bytes, 0);
        assert_eq!(result.last_sequence, 0);
    }

    #[test]
    fn recover_empty_directory() {
        let dir = TempDir::new().unwrap();
        let result = recover(dir.path()).unwrap();
        assert_eq!(result.valid_records, 0);
        assert_eq!(result.last_sequence, 0);
    }
}
