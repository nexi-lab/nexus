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

use ahash::AHashMap;
use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};

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
    /// Uses ahash for faster hashing of 32-byte keys (~2-3x vs SipHash).
    map: AHashMap<[u8; 32], MemIndexEntry>,
    /// Volume file descriptors kept open for pread.
    volumes: HashMap<u32, std::fs::File>,
}

impl VolumeIndex {
    pub fn new() -> Self {
        Self {
            map: AHashMap::new(),
            volumes: HashMap::new(),
        }
    }

    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            map: AHashMap::with_capacity(capacity),
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
        // AHashMap (hashbrown) layout: each bucket = key(32) + value(16) = 48 bytes + 1 control byte
        // Load factor ~87.5%, so capacity ≈ len * 8/7
        let entry_size = std::mem::size_of::<[u8; 32]>() + std::mem::size_of::<MemIndexEntry>();
        let capacity = self.map.capacity().max(self.map.len());
        let map_bytes = capacity * (entry_size + 1); // +1 for control byte per bucket

        // Volume FD overhead
        let fd_bytes = self.volumes.capacity()
            * (std::mem::size_of::<u32>() + std::mem::size_of::<std::fs::File>());

        map_bytes + fd_bytes + std::mem::size_of::<Self>()
    }

    /// Number of open volume file descriptors.
    pub fn volume_count(&self) -> usize {
        self.volumes.len()
    }

    /// Sum of all entry sizes (for total_bytes tracking).
    pub fn total_content_bytes(&self) -> u64 {
        self.map.values().map(|e| e.size as u64).sum()
    }

    /// Check that every volume_id referenced by entries exists in the given paths.
    pub fn all_volumes_exist(&self, volume_paths: &HashMap<u32, PathBuf>) -> bool {
        self.map
            .values()
            .all(|e| volume_paths.contains_key(&e.volume_id))
    }

    // ─── Snapshot persistence (flat binary sidecar for fast startup) ──────

    /// Snapshot magic bytes.
    const SNAPSHOT_MAGIC: &'static [u8; 4] = b"NIDX";
    /// Snapshot version.
    const SNAPSHOT_VERSION: u32 = 1;
    /// Header: magic(4) + version(4) + entry_count(8) = 16 bytes.
    const SNAPSHOT_HEADER_SIZE: usize = 16;
    /// Per-entry: hash(32) + volume_id(4) + offset(8) + size(4) = 48 bytes.
    const SNAPSHOT_ENTRY_SIZE: usize = 48;

    /// Save the index to a flat binary file for fast startup.
    ///
    /// Format: `[magic:4][version:4][count:8] || [hash:32][vol_id:4][offset:8][size:4] × count`
    pub fn save_snapshot(&self, path: &Path) -> io::Result<()> {
        use std::io::Write;
        let tmp = path.with_extension("tmp");
        let mut f = std::fs::File::create(&tmp)?;

        // Header
        f.write_all(Self::SNAPSHOT_MAGIC)?;
        f.write_all(&Self::SNAPSHOT_VERSION.to_le_bytes())?;
        f.write_all(&(self.map.len() as u64).to_le_bytes())?;

        // Entries
        for (hash, entry) in &self.map {
            f.write_all(hash)?;
            f.write_all(&entry.volume_id.to_le_bytes())?;
            f.write_all(&entry.offset.to_le_bytes())?;
            f.write_all(&entry.size.to_le_bytes())?;
        }

        f.sync_data()?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    /// Load the index from a snapshot file. Returns None if the file doesn't
    /// exist, is corrupt, or has a version mismatch.
    pub fn load_snapshot(path: &Path) -> Option<Self> {
        use std::io::Read;
        let mut f = std::fs::File::open(path).ok()?;
        let file_len = f.metadata().ok()?.len() as usize;

        // Read header
        let mut header = [0u8; Self::SNAPSHOT_HEADER_SIZE];
        f.read_exact(&mut header).ok()?;

        if &header[0..4] != Self::SNAPSHOT_MAGIC {
            return None;
        }
        let version = u32::from_le_bytes(header[4..8].try_into().ok()?);
        if version != Self::SNAPSHOT_VERSION {
            return None;
        }
        let count = u64::from_le_bytes(header[8..16].try_into().ok()?) as usize;

        // Validate file size
        let expected = Self::SNAPSHOT_HEADER_SIZE + count * Self::SNAPSHOT_ENTRY_SIZE;
        if file_len != expected {
            return None;
        }

        // Read all entries in one syscall
        let data_len = count * Self::SNAPSHOT_ENTRY_SIZE;
        let mut buf = vec![0u8; data_len];
        f.read_exact(&mut buf).ok()?;

        // Parse entries — collect into AHashMap in one shot (avoids per-insert overhead)
        let map: AHashMap<[u8; 32], MemIndexEntry> = buf
            .chunks_exact(Self::SNAPSHOT_ENTRY_SIZE)
            .map(|chunk| {
                let mut hash = [0u8; 32];
                hash.copy_from_slice(&chunk[..32]);
                let volume_id = u32::from_le_bytes(chunk[32..36].try_into().unwrap());
                let offset = u64::from_le_bytes(chunk[36..44].try_into().unwrap());
                let size = u32::from_le_bytes(chunk[44..48].try_into().unwrap());
                (
                    hash,
                    MemIndexEntry {
                        volume_id,
                        offset,
                        size,
                    },
                )
            })
            .collect();

        Some(Self {
            map,
            volumes: HashMap::new(),
        })
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
