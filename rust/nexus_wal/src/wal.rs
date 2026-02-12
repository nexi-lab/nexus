//! WAL engine — the top-level coordinator.
//!
//! Manages segment rotation, zone indexing, and read/truncate operations.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use thiserror::Error;

use crate::recovery;
use crate::segment::{parse_segment_name, Record, SegmentError, SegmentReader, SegmentWriter};

#[derive(Debug, Error)]
pub enum WalError {
    #[error("segment error: {0}")]
    Segment(#[from] SegmentError),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("WAL is closed")]
    Closed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncMode {
    /// fsync after every write (durable).
    Every,
    /// No explicit fsync (OS-buffered, faster but risk of data loss).
    None,
}

impl SyncMode {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "every" => Some(Self::Every),
            "none" => Some(Self::None),
            _ => Option::None,
        }
    }
}

/// Metadata about a segment file on disk.
#[derive(Debug, Clone)]
struct SegmentMeta {
    path: PathBuf,
    first_seq: u64,
    #[allow(dead_code)]
    epoch: u64,
}

/// Internal mutable state protected by a mutex.
struct WalInner {
    active_writer: Option<SegmentWriter>,
    current_seq: u64,
    segments: Vec<SegmentMeta>,
    zone_index: HashMap<Vec<u8>, Vec<u64>>,
    closed: bool,
}

/// Thread-safe WAL engine.
pub struct WalEngine {
    dir: PathBuf,
    segment_size: u64,
    sync_mode: SyncMode,
    inner: Mutex<WalInner>,
}

impl WalEngine {
    /// Open (or create) a WAL in the given directory.
    ///
    /// Runs crash recovery, rebuilds zone index, and prepares for appends.
    pub fn open(dir: &Path, segment_size: u64, sync_mode: SyncMode) -> Result<Self, WalError> {
        fs::create_dir_all(dir)?;

        // Run crash recovery
        let recovery_result = recovery::recover(dir)?;

        // Discover existing segments
        let segments = discover_segments(dir)?;

        // Rebuild zone index from all segments
        let mut zone_index: HashMap<Vec<u8>, Vec<u64>> = HashMap::new();
        for seg in &segments {
            if let Ok(reader) = SegmentReader::open(&seg.path) {
                if let Ok(iter) = reader.iter() {
                    for record in iter.flatten() {
                        zone_index
                            .entry(record.zone_id.clone())
                            .or_default()
                            .push(record.seq);
                    }
                }
            }
        }

        // Open last segment for appending, or create new one
        let active_writer = if let Some(last) = segments.last() {
            // Only re-open if the last segment hasn't exceeded size
            let meta = fs::metadata(&last.path)?;
            if meta.len() < segment_size {
                Some(SegmentWriter::open_append(&last.path)?)
            } else {
                Option::None
            }
        } else {
            Option::None
        };

        Ok(Self {
            dir: dir.to_path_buf(),
            segment_size,
            sync_mode,
            inner: Mutex::new(WalInner {
                active_writer,
                current_seq: recovery_result.last_sequence,
                segments,
                zone_index,
                closed: false,
            }),
        })
    }

    /// Append a single record. Returns the assigned sequence number.
    pub fn append(&self, zone_id: &[u8], payload: &[u8]) -> Result<u64, WalError> {
        let mut inner = self.inner.lock();
        if inner.closed {
            return Err(WalError::Closed);
        }

        inner.current_seq += 1;
        let seq = inner.current_seq;

        self.ensure_active_writer(&mut inner)?;

        // Write record and capture bytes_written before releasing the borrow
        {
            let writer = inner.active_writer.as_mut().unwrap();
            writer.append(seq, zone_id, payload)?;
            if self.sync_mode == SyncMode::Every {
                writer.sync()?;
            }
        }

        // Update zone index
        inner
            .zone_index
            .entry(zone_id.to_vec())
            .or_default()
            .push(seq);

        // Rotate if needed
        let should_rotate = inner
            .active_writer
            .as_ref()
            .is_some_and(|w| w.bytes_written() >= self.segment_size);
        if should_rotate {
            self.rotate_segment(&mut inner)?;
        }

        Ok(seq)
    }

    /// Append a batch of records atomically. Returns assigned sequence numbers.
    pub fn append_batch(&self, events: &[(Vec<u8>, Vec<u8>)]) -> Result<Vec<u64>, WalError> {
        let mut inner = self.inner.lock();
        if inner.closed {
            return Err(WalError::Closed);
        }

        let mut seqs = Vec::with_capacity(events.len());

        self.ensure_active_writer(&mut inner)?;

        for (zone_id, payload) in events {
            inner.current_seq += 1;
            let seq = inner.current_seq;
            seqs.push(seq);

            // Write record in a scoped borrow
            {
                let writer = inner.active_writer.as_mut().unwrap();
                writer.append(seq, zone_id, payload)?;
            }

            // Update zone index
            inner
                .zone_index
                .entry(zone_id.clone())
                .or_default()
                .push(seq);

            // Rotate mid-batch if needed
            let should_rotate = inner
                .active_writer
                .as_ref()
                .is_some_and(|w| w.bytes_written() >= self.segment_size);
            if should_rotate {
                self.rotate_segment(&mut inner)?;
                self.ensure_active_writer(&mut inner)?;
            }
        }

        // Single fsync for the batch
        if self.sync_mode == SyncMode::Every {
            if let Some(writer) = inner.active_writer.as_mut() {
                writer.sync()?;
            }
        }

        Ok(seqs)
    }

    /// Read records starting from `seq`, up to `limit`, with optional zone filter.
    pub fn read_from(
        &self,
        seq: u64,
        limit: usize,
        zone_id_filter: Option<&[u8]>,
    ) -> Result<Vec<Record>, WalError> {
        let inner = self.inner.lock();
        if inner.closed {
            return Err(WalError::Closed);
        }

        let mut results = Vec::new();
        let remaining = limit;

        // Find the first segment that may contain `seq`
        let start_idx = find_start_segment(&inner.segments, seq);

        for seg in &inner.segments[start_idx..] {
            if results.len() >= remaining {
                break;
            }
            let reader = SegmentReader::open(&seg.path)?;
            let records = reader.read_from(seq, remaining - results.len(), zone_id_filter)?;
            results.extend(records);
        }

        Ok(results)
    }

    /// Delete segments whose last sequence < before_seq.
    /// Returns the number of deleted records.
    pub fn truncate(&self, before_seq: u64) -> Result<u64, WalError> {
        let mut inner = self.inner.lock();
        if inner.closed {
            return Err(WalError::Closed);
        }

        let mut deleted_records: u64 = 0;
        let mut to_remove = Vec::new();

        for (i, seg) in inner.segments.iter().enumerate() {
            // Check if this segment's records are all < before_seq
            let reader = SegmentReader::open(&seg.path)?;
            let records: Vec<Record> = reader.iter()?.filter_map(|r| r.ok()).collect();

            let all_below = records.iter().all(|r| r.seq < before_seq);
            if all_below && !records.is_empty() {
                to_remove.push(i);
                deleted_records += records.len() as u64;
            }
        }

        // Remove in reverse order to preserve indices
        for &i in to_remove.iter().rev() {
            let seg = inner.segments.remove(i);
            let _ = fs::remove_file(&seg.path);
        }

        // Clean zone index entries for deleted sequences
        for seqs in inner.zone_index.values_mut() {
            seqs.retain(|&s| s >= before_seq);
        }
        inner.zone_index.retain(|_, v| !v.is_empty());

        Ok(deleted_records)
    }

    /// Force fsync on the active segment.
    pub fn sync(&self) -> Result<(), WalError> {
        let mut inner = self.inner.lock();
        if let Some(writer) = inner.active_writer.as_mut() {
            writer.sync()?;
        }
        Ok(())
    }

    /// Close the WAL: fsync and release the active segment writer.
    pub fn close(&self) -> Result<(), WalError> {
        let mut inner = self.inner.lock();
        if inner.closed {
            return Ok(());
        }
        if let Some(mut writer) = inner.active_writer.take() {
            writer.sync()?;
        }
        inner.closed = true;
        Ok(())
    }

    /// Current (most recent) sequence number.
    pub fn current_sequence(&self) -> u64 {
        self.inner.lock().current_seq
    }

    /// Check if the WAL is open and writable.
    pub fn health_check(&self) -> bool {
        !self.inner.lock().closed
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    /// Ensure there is an active writer, creating a new segment if needed.
    fn ensure_active_writer(&self, inner: &mut WalInner) -> Result<(), WalError> {
        if inner.active_writer.is_none() {
            let first_seq = inner.current_seq + 1;
            let epoch = epoch_secs();
            let writer = SegmentWriter::new(&self.dir, first_seq, epoch)?;
            let path = writer.path().to_path_buf();
            inner.segments.push(SegmentMeta {
                path,
                first_seq,
                epoch,
            });
            inner.active_writer = Some(writer);
        }
        Ok(())
    }

    /// Rotate: finalize current segment and clear the writer so the next
    /// append creates a fresh segment.
    fn rotate_segment(&self, inner: &mut WalInner) -> Result<(), WalError> {
        if let Some(mut writer) = inner.active_writer.take() {
            writer.sync()?;
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Free functions
// ---------------------------------------------------------------------------

/// Discover and sort existing segment files in a directory.
fn discover_segments(dir: &Path) -> Result<Vec<SegmentMeta>, WalError> {
    let mut metas = Vec::new();
    if !dir.exists() {
        return Ok(metas);
    }
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        if let Some((first_seq, epoch)) = parse_segment_name(&name_str) {
            metas.push(SegmentMeta {
                path: entry.path(),
                first_seq,
                epoch,
            });
        }
    }
    metas.sort_by_key(|m| m.first_seq);
    Ok(metas)
}

/// Find the index of the first segment that *might* contain `seq`.
fn find_start_segment(segments: &[SegmentMeta], seq: u64) -> usize {
    if segments.is_empty() {
        return 0;
    }
    // Binary search for the last segment with first_seq <= seq
    match segments.binary_search_by_key(&seq, |m| m.first_seq) {
        Ok(i) => i,
        Err(0) => 0,
        Err(i) => i - 1,
    }
}

/// Current time as seconds since Unix epoch.
fn epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

// ===========================================================================
// Tests
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn open_wal(dir: &Path) -> WalEngine {
        WalEngine::open(dir, 4 * 1024 * 1024, SyncMode::Every).unwrap()
    }

    #[test]
    fn append_single_event() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        let seq = wal.append(b"zone1", b"payload1").unwrap();
        assert_eq!(seq, 1);
        assert_eq!(wal.current_sequence(), 1);
    }

    #[test]
    fn append_sequential_numbering() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        for i in 1..=100 {
            let seq = wal.append(b"z", format!("p{i}").as_bytes()).unwrap();
            assert_eq!(seq, i);
        }
        assert_eq!(wal.current_sequence(), 100);
    }

    #[test]
    fn append_batch() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        let events: Vec<(Vec<u8>, Vec<u8>)> = (0..10)
            .map(|i| (b"z".to_vec(), format!("p{i}").into_bytes()))
            .collect();
        let seqs = wal.append_batch(&events).unwrap();
        assert_eq!(seqs, (1..=10).collect::<Vec<u64>>());
    }

    #[test]
    fn read_from_middle() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        for i in 1..=20 {
            wal.append(b"z", format!("p{i}").as_bytes()).unwrap();
        }
        let records = wal.read_from(11, 5, None).unwrap();
        assert_eq!(records.len(), 5);
        assert_eq!(records[0].seq, 11);
        assert_eq!(records[4].seq, 15);
    }

    #[test]
    fn read_with_zone_filter() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        wal.append(b"a", b"p1").unwrap();
        wal.append(b"b", b"p2").unwrap();
        wal.append(b"a", b"p3").unwrap();
        wal.append(b"c", b"p4").unwrap();
        wal.append(b"a", b"p5").unwrap();

        let records = wal.read_from(1, 100, Some(b"a")).unwrap();
        assert_eq!(records.len(), 3);
        assert_eq!(records[0].payload, b"p1");
        assert_eq!(records[1].payload, b"p3");
        assert_eq!(records[2].payload, b"p5");
    }

    #[test]
    fn truncate_removes_old_segments() {
        let dir = TempDir::new().unwrap();
        // Small segment size to force rotation
        let wal = WalEngine::open(dir.path(), 100, SyncMode::Every).unwrap();

        // Write enough to create multiple segments
        for i in 1..=50 {
            wal.append(b"z", format!("payload-{i:04}").as_bytes())
                .unwrap();
        }

        let before = wal.read_from(1, 1000, None).unwrap().len();
        assert_eq!(before, 50);

        // Truncate everything before seq 30
        let deleted = wal.truncate(30).unwrap();
        assert!(deleted > 0);

        // Records >= 30 should still be readable
        let after = wal.read_from(30, 1000, None).unwrap();
        assert!(!after.is_empty());
        assert!(after[0].seq >= 30);
    }

    #[test]
    fn segment_rotation() {
        let dir = TempDir::new().unwrap();
        // Very small segments to force multiple rotations
        // Very small segments (header=8 + 50 = 58 bytes) to force rotation
        let wal = WalEngine::open(dir.path(), 58, SyncMode::Every).unwrap();

        for i in 1..=10 {
            wal.append(b"z", format!("data-{i}").as_bytes()).unwrap();
        }

        // Should have multiple segment files
        let seg_count = fs::read_dir(dir.path())
            .unwrap()
            .filter(|e| {
                e.as_ref()
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".seg")
            })
            .count();
        assert!(seg_count > 1, "expected multiple segments, got {seg_count}");

        // All records should still be readable
        let records = wal.read_from(1, 100, None).unwrap();
        assert_eq!(records.len(), 10);
    }

    #[test]
    fn close_and_reopen() {
        let dir = TempDir::new().unwrap();
        {
            let wal = open_wal(dir.path());
            wal.append(b"z", b"hello").unwrap();
            wal.append(b"z", b"world").unwrap();
            wal.close().unwrap();
            assert!(!wal.health_check());
        }
        {
            let wal = open_wal(dir.path());
            assert!(wal.health_check());
            assert_eq!(wal.current_sequence(), 2);
            let records = wal.read_from(1, 100, None).unwrap();
            assert_eq!(records.len(), 2);
            assert_eq!(records[0].payload, b"hello");
            assert_eq!(records[1].payload, b"world");
        }
    }

    #[test]
    fn append_after_close_errors() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        wal.close().unwrap();
        assert!(wal.append(b"z", b"fail").is_err());
    }

    #[test]
    fn concurrent_appends() {
        let dir = TempDir::new().unwrap();
        let wal = std::sync::Arc::new(open_wal(dir.path()));
        let mut handles = Vec::new();

        for t in 0..8 {
            let wal = wal.clone();
            handles.push(std::thread::spawn(move || {
                let mut seqs = Vec::new();
                for i in 0..50 {
                    let seq = wal
                        .append(format!("t{t}").as_bytes(), format!("t{t}-{i}").as_bytes())
                        .unwrap();
                    seqs.push(seq);
                }
                seqs
            }));
        }

        let mut all_seqs: Vec<u64> = Vec::new();
        for h in handles {
            all_seqs.extend(h.join().unwrap());
        }

        // All sequence numbers must be unique
        all_seqs.sort();
        let unique_count = all_seqs.len();
        all_seqs.dedup();
        assert_eq!(all_seqs.len(), unique_count, "sequences must be unique");

        // Must be monotonic within each thread (guaranteed by mutex)
        assert_eq!(wal.current_sequence(), 400); // 8 threads * 50
    }

    #[test]
    fn empty_wal_read() {
        let dir = TempDir::new().unwrap();
        let wal = open_wal(dir.path());
        let records = wal.read_from(1, 100, None).unwrap();
        assert!(records.is_empty());
    }
}
