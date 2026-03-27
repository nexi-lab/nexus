//! In-memory volume index — O(1) content lookup via Rust HashMap.
//!
//! Maintains a `HashMap<[u8; 32], MemIndexEntry>` for instant hash-to-location
//! lookups and keeps volume file descriptors open for zero-overhead pread.
//!
//! Uses full 32-byte blake3 hashes as keys to preserve CAS identity —
//! a content-addressed store must never alias distinct hashes.
//!
//! Thread safety: callers protect the index with `RwLock<VolumeIndex>`.
//! Volume FDs support concurrent pread via `read_at` (no seek required).
//!
//! Issue #3404: in-memory volume index.

#[cfg(unix)]
use std::os::unix::fs::FileExt;

use std::collections::HashMap;
use std::io;
use std::path::Path;

/// Entry header size in volume files: hash(32) + size(4) + flags(1) = 37 bytes.
/// Must match `ENTRY_HEADER_SIZE` in `volume_engine.rs`.
const ENTRY_HEADER_SIZE: u64 = 37;

/// Compact index entry for in-memory O(1) lookup.
///
/// 16 bytes total: volume_id(4) + offset(8) + size(4).
/// Omits timestamp (only needed for GC, served from redb).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MemIndexEntry {
    pub volume_id: u32,
    pub offset: u64,
    pub size: u32,
}

/// Result of a `read_content` attempt.
pub enum ReadContentResult {
    /// Content successfully read via pread.
    Ok(Vec<u8>),
    /// Hash not found in the index.
    NotFound,
    /// Hash found but no cached file descriptor for this volume.
    /// Caller should fall back to opening the file by path.
    NoFd(MemIndexEntry),
    /// I/O error during pread.
    IoError(io::Error),
}

/// In-memory volume index for O(1) content lookup.
///
/// Memory: ~56 bytes per entry (32B key + 16B value + hashmap overhead).
/// For 1M entries: ~56 MB — trivial for any deployment.
pub struct VolumeIndex {
    /// blake3_hash → (volume_id, offset, size)
    map: HashMap<[u8; 32], MemIndexEntry>,
    /// Volume file descriptors kept open for pread.
    volumes: HashMap<u32, std::fs::File>,
}

impl VolumeIndex {
    pub fn new() -> Self {
        Self {
            map: HashMap::new(),
            volumes: HashMap::new(),
        }
    }

    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            map: HashMap::with_capacity(capacity),
            volumes: HashMap::new(),
        }
    }

    /// O(1) lookup of content location by hash.
    #[inline]
    pub fn lookup(&self, hash: &[u8; 32]) -> Option<MemIndexEntry> {
        self.map.get(hash).copied()
    }

    /// Check if a hash exists in the index.
    #[inline]
    pub fn contains(&self, hash: &[u8; 32]) -> bool {
        self.map.contains_key(hash)
    }

    /// Insert or update an entry.
    #[inline]
    pub fn insert(&mut self, hash: [u8; 32], entry: MemIndexEntry) {
        self.map.insert(hash, entry);
    }

    /// Remove an entry. Returns true if it existed.
    #[inline]
    pub fn remove(&mut self, hash: &[u8; 32]) -> bool {
        self.map.remove(hash).is_some()
    }

    /// Lookup + pread in a single operation (no Python round-trip).
    ///
    /// Uses `read_at` (pread) for thread-safe concurrent reads from cached FDs.
    #[cfg(unix)]
    pub fn read_content(&self, hash: &[u8; 32]) -> ReadContentResult {
        let entry = match self.map.get(hash) {
            Some(e) => *e,
            None => return ReadContentResult::NotFound,
        };

        let file = match self.volumes.get(&entry.volume_id) {
            Some(f) => f,
            None => return ReadContentResult::NoFd(entry),
        };

        let data_offset = entry.offset + ENTRY_HEADER_SIZE;
        let mut buf = vec![0u8; entry.size as usize];
        match file.read_at(&mut buf, data_offset) {
            Ok(n) if n == entry.size as usize => ReadContentResult::Ok(buf),
            Ok(_) => ReadContentResult::IoError(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "Short read from volume",
            )),
            Err(e) => ReadContentResult::IoError(e),
        }
    }

    #[cfg(not(unix))]
    pub fn read_content(&self, hash: &[u8; 32]) -> ReadContentResult {
        match self.map.get(hash) {
            Some(e) => ReadContentResult::NoFd(*e),
            None => ReadContentResult::NotFound,
        }
    }

    /// Register a volume file descriptor for pread access.
    pub fn open_volume(&mut self, volume_id: u32, path: &Path) -> io::Result<()> {
        let file = std::fs::File::open(path)?;
        self.volumes.insert(volume_id, file);
        Ok(())
    }

    /// Close a volume file descriptor.
    pub fn close_volume(&mut self, volume_id: u32) {
        self.volumes.remove(&volume_id);
    }

    /// Get a reference to a cached volume file descriptor.
    #[allow(dead_code)]
    pub fn volume_fd(&self, volume_id: u32) -> Option<&std::fs::File> {
        self.volumes.get(&volume_id)
    }

    /// Number of entries in the index.
    #[inline]
    pub fn len(&self) -> usize {
        self.map.len()
    }

    /// Estimated memory usage in bytes.
    pub fn memory_bytes(&self) -> usize {
        // hashbrown layout: each bucket = key(32) + value(16) = 48 bytes + 1 control byte
        // Load factor ~87.5%, so capacity ≈ len * 8/7
        let entry_size = std::mem::size_of::<[u8; 32]>() + std::mem::size_of::<MemIndexEntry>();
        let capacity = self.map.capacity().max(self.map.len());
        let map_bytes = capacity * (entry_size + 1); // +1 for control byte per bucket

        // Volume FD overhead
        let fd_bytes = self.volumes.capacity()
            * (std::mem::size_of::<u32>() + std::mem::size_of::<std::fs::File>());

        map_bytes + fd_bytes + std::mem::size_of::<Self>()
    }

    /// Bulk load entries from an iterator (for startup).
    #[allow(dead_code)]
    pub fn load_entries(&mut self, entries: impl Iterator<Item = ([u8; 32], MemIndexEntry)>) {
        for (hash, entry) in entries {
            self.map.insert(hash, entry);
        }
    }

    /// Number of open volume file descriptors.
    pub fn volume_count(&self) -> usize {
        self.volumes.len()
    }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn make_hash(seed: u8) -> [u8; 32] {
        let mut h = [0u8; 32];
        h[0] = seed;
        h[31] = seed;
        h
    }

    fn make_entry(vol: u32, offset: u64, size: u32) -> MemIndexEntry {
        MemIndexEntry {
            volume_id: vol,
            offset,
            size,
        }
    }

    #[test]
    fn test_insert_lookup_remove() {
        let mut idx = VolumeIndex::new();
        let hash = make_hash(1);
        let entry = make_entry(1, 64, 100);

        assert!(!idx.contains(&hash));
        assert_eq!(idx.len(), 0);

        idx.insert(hash, entry);
        assert!(idx.contains(&hash));
        assert_eq!(idx.lookup(&hash), Some(entry));
        assert_eq!(idx.len(), 1);

        assert!(idx.remove(&hash));
        assert!(!idx.contains(&hash));
        assert_eq!(idx.len(), 0);

        assert!(!idx.remove(&hash)); // already removed
    }

    #[test]
    fn test_with_capacity() {
        let idx = VolumeIndex::with_capacity(1000);
        assert_eq!(idx.len(), 0);
        assert!(idx.memory_bytes() > 0);
    }

    #[test]
    fn test_load_entries() {
        let mut idx = VolumeIndex::new();
        let entries = (0..100u8).map(|i| (make_hash(i), make_entry(1, i as u64 * 100, 50)));
        idx.load_entries(entries);
        assert_eq!(idx.len(), 100);
        assert!(idx.contains(&make_hash(50)));
    }

    #[test]
    fn test_memory_bytes_grows() {
        let mut idx = VolumeIndex::new();
        let empty_bytes = idx.memory_bytes();

        for i in 0..100u8 {
            idx.insert(make_hash(i), make_entry(1, i as u64 * 100, 50));
        }

        let loaded_bytes = idx.memory_bytes();
        assert!(loaded_bytes > empty_bytes);

        let per_entry = (loaded_bytes - std::mem::size_of::<VolumeIndex>()) as f64 / 100.0;
        // 32 (key) + 16 (value) + 1 (control) = 49 bytes minimum
        assert!(per_entry >= 49.0, "per_entry={per_entry} too small");
        assert!(per_entry < 120.0, "per_entry={per_entry} too large");
    }

    #[test]
    fn test_read_content_not_found() {
        let idx = VolumeIndex::new();
        let hash = make_hash(1);
        matches!(idx.read_content(&hash), ReadContentResult::NotFound);
    }

    #[test]
    fn test_read_content_no_fd() {
        let mut idx = VolumeIndex::new();
        let hash = make_hash(1);
        let entry = make_entry(99, 64, 100);
        idx.insert(hash, entry);

        match idx.read_content(&hash) {
            ReadContentResult::NoFd(e) => assert_eq!(e, entry),
            other => panic!(
                "Expected NoFd, got {:?}",
                match other {
                    ReadContentResult::Ok(_) => "Ok",
                    ReadContentResult::NotFound => "NotFound",
                    ReadContentResult::IoError(_) => "IoError",
                    ReadContentResult::NoFd(_) => unreachable!(),
                }
            ),
        }
    }

    #[test]
    fn test_open_close_volume() {
        let dir = TempDir::new().unwrap();
        let vol_path = dir.path().join("test.vol");
        std::fs::write(&vol_path, b"test volume data").unwrap();

        let mut idx = VolumeIndex::new();
        assert_eq!(idx.volume_count(), 0);

        idx.open_volume(1, &vol_path).unwrap();
        assert_eq!(idx.volume_count(), 1);

        idx.close_volume(1);
        assert_eq!(idx.volume_count(), 0);
    }

    #[test]
    fn test_overwrite_entry() {
        let mut idx = VolumeIndex::new();
        let hash = make_hash(1);

        idx.insert(hash, make_entry(1, 64, 100));
        assert_eq!(idx.lookup(&hash).unwrap().volume_id, 1);

        // Overwrite with new volume
        idx.insert(hash, make_entry(2, 128, 200));
        assert_eq!(idx.lookup(&hash).unwrap().volume_id, 2);
        assert_eq!(idx.lookup(&hash).unwrap().size, 200);
        assert_eq!(idx.len(), 1); // still just one entry
    }
}
