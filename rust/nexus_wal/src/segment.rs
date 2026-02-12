//! Segment file I/O for the WAL.
//!
//! Each segment is a contiguous file of records with a fixed header.
//! File format:
//!   [MAGIC:4][VERSION:u32]  (header, 8 bytes)
//!   [Record]*               (variable-length records)
//!
//! Record format:
//!   [seq:u64][zone_id_len:u16][zone_id:bytes][payload_len:u32][payload:bytes][crc32:u32]
//!
//! CRC32 covers: seq + zone_id + payload bytes.
//! Segment naming: `wal-{first_seq}-{epoch_secs}.seg`

use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crc32fast::Hasher;
use thiserror::Error;

/// Magic bytes identifying a WAL segment file.
pub const MAGIC: [u8; 4] = *b"NXWL";

/// Current segment format version.
pub const VERSION: u32 = 1;

/// Header size in bytes (magic + version).
pub const HEADER_SIZE: u64 = 8;

#[derive(Debug, Error)]
pub enum SegmentError {
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("invalid magic bytes")]
    BadMagic,
    #[error("unsupported version: {0}")]
    BadVersion(u32),
    #[error("CRC mismatch at seq {seq}: expected {expected:#010x}, got {actual:#010x}")]
    CrcMismatch {
        seq: u64,
        expected: u32,
        actual: u32,
    },
    #[error("truncated record at offset {0}")]
    TruncatedRecord(u64),
}

/// A single WAL record.
#[derive(Debug, Clone)]
pub struct Record {
    pub seq: u64,
    pub zone_id: Vec<u8>,
    pub payload: Vec<u8>,
}

impl Record {
    /// Byte size of this record on disk (excluding header).
    pub fn wire_size(&self) -> u64 {
        // seq(8) + zone_id_len(2) + zone_id + payload_len(4) + payload + crc(4)
        8 + 2 + self.zone_id.len() as u64 + 4 + self.payload.len() as u64 + 4
    }
}

// ---------------------------------------------------------------------------
// Writer
// ---------------------------------------------------------------------------

pub struct SegmentWriter {
    writer: BufWriter<File>,
    path: PathBuf,
    bytes_written: u64,
}

impl SegmentWriter {
    /// Create a new segment file with header.
    pub fn new(dir: &Path, first_seq: u64, epoch_secs: u64) -> Result<Self, SegmentError> {
        let filename = format!("wal-{first_seq}-{epoch_secs}.seg");
        let path = dir.join(filename);

        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)?;
        let mut writer = BufWriter::new(file);

        // Write header
        writer.write_all(&MAGIC)?;
        writer.write_all(&VERSION.to_le_bytes())?;
        writer.flush()?;

        Ok(Self {
            writer,
            path,
            bytes_written: HEADER_SIZE,
        })
    }

    /// Re-open an existing segment for appending.
    pub fn open_append(path: &Path) -> Result<Self, SegmentError> {
        // Validate header first
        {
            let mut f = File::open(path)?;
            let mut magic = [0u8; 4];
            f.read_exact(&mut magic)?;
            if magic != MAGIC {
                return Err(SegmentError::BadMagic);
            }
            let mut ver = [0u8; 4];
            f.read_exact(&mut ver)?;
            let v = u32::from_le_bytes(ver);
            if v != VERSION {
                return Err(SegmentError::BadVersion(v));
            }
        }

        let file = OpenOptions::new().append(true).open(path)?;
        let bytes_written = file.metadata()?.len();
        let writer = BufWriter::new(file);

        Ok(Self {
            writer,
            path: path.to_path_buf(),
            bytes_written,
        })
    }

    /// Append a record to the segment. Does NOT fsync.
    pub fn append(&mut self, seq: u64, zone_id: &[u8], payload: &[u8]) -> Result<(), SegmentError> {
        // Compute CRC over seq + zone_id + payload
        let crc = compute_crc(seq, zone_id, payload);

        let zone_id_len = zone_id.len() as u16;
        let payload_len = payload.len() as u32;

        self.writer.write_all(&seq.to_le_bytes())?;
        self.writer.write_all(&zone_id_len.to_le_bytes())?;
        self.writer.write_all(zone_id)?;
        self.writer.write_all(&payload_len.to_le_bytes())?;
        self.writer.write_all(payload)?;
        self.writer.write_all(&crc.to_le_bytes())?;

        self.bytes_written += 8 + 2 + zone_id.len() as u64 + 4 + payload.len() as u64 + 4;
        Ok(())
    }

    /// Flush internal buffers and fsync to disk.
    pub fn sync(&mut self) -> Result<(), SegmentError> {
        self.writer.flush()?;
        self.writer.get_ref().sync_all()?;
        Ok(())
    }

    /// Current byte count written to this segment (including header).
    pub fn bytes_written(&self) -> u64 {
        self.bytes_written
    }

    /// Path of this segment file.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

// ---------------------------------------------------------------------------
// Reader
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct SegmentReader {
    path: PathBuf,
}

impl SegmentReader {
    /// Open and validate a segment file header.
    pub fn open(path: &Path) -> Result<Self, SegmentError> {
        let mut f = File::open(path)?;
        let mut magic = [0u8; 4];
        f.read_exact(&mut magic)?;
        if magic != MAGIC {
            return Err(SegmentError::BadMagic);
        }
        let mut ver_buf = [0u8; 4];
        f.read_exact(&mut ver_buf)?;
        let ver = u32::from_le_bytes(ver_buf);
        if ver != VERSION {
            return Err(SegmentError::BadVersion(ver));
        }
        Ok(Self {
            path: path.to_path_buf(),
        })
    }

    /// Iterate all records in the segment, validating CRC for each.
    pub fn iter(&self) -> Result<SegmentIter, SegmentError> {
        let file = File::open(&self.path)?;
        let file_len = file.metadata()?.len();
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(HEADER_SIZE))?;
        Ok(SegmentIter {
            reader,
            offset: HEADER_SIZE,
            file_len,
        })
    }

    /// Read records starting from `seq`, up to `limit`, with optional zone filter.
    pub fn read_from(
        &self,
        seq: u64,
        limit: usize,
        zone_id_filter: Option<&[u8]>,
    ) -> Result<Vec<Record>, SegmentError> {
        let mut results = Vec::new();
        for record_result in self.iter()? {
            let record = record_result?;
            if record.seq < seq {
                continue;
            }
            if let Some(filter) = zone_id_filter {
                if record.zone_id != filter {
                    continue;
                }
            }
            results.push(record);
            if results.len() >= limit {
                break;
            }
        }
        Ok(results)
    }
}

// ---------------------------------------------------------------------------
// Iterator
// ---------------------------------------------------------------------------

pub struct SegmentIter {
    reader: BufReader<File>,
    offset: u64,
    file_len: u64,
}

impl Iterator for SegmentIter {
    type Item = Result<Record, SegmentError>;

    fn next(&mut self) -> Option<Self::Item> {
        // Need at least seq(8) + zone_id_len(2) to read
        if self.offset + 10 > self.file_len {
            return None;
        }

        let record_start = self.offset;

        // Read seq
        let mut seq_buf = [0u8; 8];
        if let Err(e) = self.reader.read_exact(&mut seq_buf) {
            return if e.kind() == io::ErrorKind::UnexpectedEof {
                Some(Err(SegmentError::TruncatedRecord(record_start)))
            } else {
                Some(Err(e.into()))
            };
        }
        let seq = u64::from_le_bytes(seq_buf);
        self.offset += 8;

        // Read zone_id_len
        let mut zlen_buf = [0u8; 2];
        if let Err(e) = self.reader.read_exact(&mut zlen_buf) {
            return Some(if e.kind() == io::ErrorKind::UnexpectedEof {
                Err(SegmentError::TruncatedRecord(record_start))
            } else {
                Err(e.into())
            });
        }
        let zone_id_len = u16::from_le_bytes(zlen_buf) as usize;
        self.offset += 2;

        // Read zone_id
        let mut zone_id = vec![0u8; zone_id_len];
        if let Err(e) = self.reader.read_exact(&mut zone_id) {
            return Some(if e.kind() == io::ErrorKind::UnexpectedEof {
                Err(SegmentError::TruncatedRecord(record_start))
            } else {
                Err(e.into())
            });
        }
        self.offset += zone_id_len as u64;

        // Read payload_len
        let mut plen_buf = [0u8; 4];
        if let Err(e) = self.reader.read_exact(&mut plen_buf) {
            return Some(if e.kind() == io::ErrorKind::UnexpectedEof {
                Err(SegmentError::TruncatedRecord(record_start))
            } else {
                Err(e.into())
            });
        }
        let payload_len = u32::from_le_bytes(plen_buf) as usize;
        self.offset += 4;

        // Read payload
        let mut payload = vec![0u8; payload_len];
        if let Err(e) = self.reader.read_exact(&mut payload) {
            return Some(if e.kind() == io::ErrorKind::UnexpectedEof {
                Err(SegmentError::TruncatedRecord(record_start))
            } else {
                Err(e.into())
            });
        }
        self.offset += payload_len as u64;

        // Read CRC
        let mut crc_buf = [0u8; 4];
        if let Err(e) = self.reader.read_exact(&mut crc_buf) {
            return Some(if e.kind() == io::ErrorKind::UnexpectedEof {
                Err(SegmentError::TruncatedRecord(record_start))
            } else {
                Err(e.into())
            });
        }
        let stored_crc = u32::from_le_bytes(crc_buf);
        self.offset += 4;

        // Validate CRC
        let expected_crc = compute_crc(seq, &zone_id, &payload);
        if stored_crc != expected_crc {
            return Some(Err(SegmentError::CrcMismatch {
                seq,
                expected: expected_crc,
                actual: stored_crc,
            }));
        }

        Some(Ok(Record {
            seq,
            zone_id,
            payload,
        }))
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Compute CRC32 over seq + zone_id + payload.
fn compute_crc(seq: u64, zone_id: &[u8], payload: &[u8]) -> u32 {
    let mut h = Hasher::new();
    h.update(&seq.to_le_bytes());
    h.update(zone_id);
    h.update(payload);
    h.finalize()
}

/// Parse segment filename into (first_seq, epoch_secs).
/// Expected format: `wal-{seq}-{epoch}.seg`
pub fn parse_segment_name(filename: &str) -> Option<(u64, u64)> {
    let stem = filename.strip_suffix(".seg")?;
    let parts: Vec<&str> = stem.splitn(3, '-').collect();
    if parts.len() != 3 || parts[0] != "wal" {
        return None;
    }
    let seq = parts[1].parse().ok()?;
    let epoch = parts[2].parse().ok()?;
    Some((seq, epoch))
}

// ===========================================================================
// Tests
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn write_read_single_record() {
        let dir = TempDir::new().unwrap();
        let mut writer = SegmentWriter::new(dir.path(), 1, 1000).unwrap();
        writer.append(1, b"zone-a", b"hello world").unwrap();
        writer.sync().unwrap();

        let reader = SegmentReader::open(writer.path()).unwrap();
        let records: Vec<Record> = reader.iter().unwrap().map(|r| r.unwrap()).collect();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].seq, 1);
        assert_eq!(records[0].zone_id, b"zone-a");
        assert_eq!(records[0].payload, b"hello world");
    }

    #[test]
    fn write_multiple_records() {
        let dir = TempDir::new().unwrap();
        let mut writer = SegmentWriter::new(dir.path(), 1, 2000).unwrap();
        for i in 1..=10 {
            writer
                .append(i, b"z1", format!("payload-{i}").as_bytes())
                .unwrap();
        }
        writer.sync().unwrap();

        let reader = SegmentReader::open(writer.path()).unwrap();
        let records: Vec<Record> = reader.iter().unwrap().map(|r| r.unwrap()).collect();
        assert_eq!(records.len(), 10);
        for (i, r) in records.iter().enumerate() {
            assert_eq!(r.seq, (i + 1) as u64);
        }
    }

    #[test]
    fn read_from_with_zone_filter() {
        let dir = TempDir::new().unwrap();
        let mut writer = SegmentWriter::new(dir.path(), 1, 3000).unwrap();
        writer.append(1, b"zone-a", b"a1").unwrap();
        writer.append(2, b"zone-b", b"b1").unwrap();
        writer.append(3, b"zone-a", b"a2").unwrap();
        writer.append(4, b"zone-b", b"b2").unwrap();
        writer.sync().unwrap();

        let reader = SegmentReader::open(writer.path()).unwrap();
        let filtered = reader.read_from(1, 100, Some(b"zone-a")).unwrap();
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].payload, b"a1");
        assert_eq!(filtered[1].payload, b"a2");
    }

    #[test]
    fn bad_magic_rejected() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bad.seg");
        std::fs::write(&path, b"BAAD\x01\x00\x00\x00").unwrap();
        let err = SegmentReader::open(&path).unwrap_err();
        assert!(matches!(err, SegmentError::BadMagic));
    }

    #[test]
    fn bad_version_rejected() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("badver.seg");
        let mut data = Vec::new();
        data.extend_from_slice(&MAGIC);
        data.extend_from_slice(&99u32.to_le_bytes());
        std::fs::write(&path, &data).unwrap();
        let err = SegmentReader::open(&path).unwrap_err();
        assert!(matches!(err, SegmentError::BadVersion(99)));
    }

    #[test]
    fn parse_segment_name_valid() {
        assert_eq!(parse_segment_name("wal-1-1000.seg"), Some((1, 1000)));
        assert_eq!(
            parse_segment_name("wal-42-1700000000.seg"),
            Some((42, 1700000000))
        );
    }

    #[test]
    fn parse_segment_name_invalid() {
        assert_eq!(parse_segment_name("bad.seg"), None);
        assert_eq!(parse_segment_name("wal-abc-123.seg"), None);
        assert_eq!(parse_segment_name("wal-1.seg"), None);
    }

    #[test]
    fn bytes_written_tracks_correctly() {
        let dir = TempDir::new().unwrap();
        let mut writer = SegmentWriter::new(dir.path(), 1, 4000).unwrap();
        assert_eq!(writer.bytes_written(), HEADER_SIZE);

        // Record: seq(8) + zid_len(2) + zid(3) + plen(4) + payload(5) + crc(4) = 26
        writer.append(1, b"abc", b"hello").unwrap();
        assert_eq!(writer.bytes_written(), HEADER_SIZE + 26);
    }
}
