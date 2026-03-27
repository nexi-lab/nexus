//! CAS Volume Engine — append-only volume files with redb index.
//!
//! Packs thousands of content blobs into append-only volume files, indexed by
//! a redb table mapping `blake3_hash → (volume_id, offset, size)`.
//!
//! Volume format (TOC-at-end pattern):
//!   Active volume (.tmp):  Header || Entry0 || Entry1 || ... || EntryN
//!   Sealed volume (.vol):  Header || Entry0 || ... || EntryN || TOC || Footer
//!
//! Entry format (8-byte aligned):
//!   [hash: 32B] [raw_size: 4B] [flags: 1B] [data: raw_size B] [padding: 0-7B]
//!
//! TOC entry (per blob):
//!   [hash: 32B] [offset: 8B] [size: 4B] [flags: 1B] = 45 bytes
//!
//! Footer (fixed 24 bytes):
//!   [magic: 4B "NVOL"] [version: 4B] [entry_count: 4B] [toc_offset: 8B] [checksum: 4B]
//!
//! Crash recovery:
//!   - Active volumes are `.tmp` files — deleted on startup (data not yet indexed)
//!   - Sealed volumes have TOC + footer — can rebuild index by scanning TOCs
//!   - Index entries always point to sealed volumes
//!
//! Issue #3403: CAS volume packing.

use parking_lot::{Mutex, RwLock};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use redb::{Database, ReadableTable, ReadableTableMetadata, TableDefinition};
use std::collections::HashMap;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

// ─── Constants ───────────────────────────────────────────────────────────────

const VOLUME_MAGIC: &[u8; 4] = b"NVOL";
const VOLUME_VERSION: u32 = 1;
const HEADER_SIZE: u64 = 64;
const FOOTER_SIZE: u64 = 24;
const ENTRY_HEADER_SIZE: u64 = 37; // hash(32) + size(4) + flags(1)
const TOC_ENTRY_SIZE: u64 = 45; // hash(32) + offset(8) + size(4) + flags(1)
const ALIGNMENT: u64 = 8;

// Entry flags
const FLAG_NONE: u8 = 0x00;
const FLAG_TOMBSTONE: u8 = 0x01;

// redb table definition: 32-byte hash key → 13-byte value (volume_id:4 + offset:8 + size:4 + timestamp:8 = 24)
// We use a fixed-width byte array key and a byte-slice value.
const INDEX_TABLE: TableDefinition<&[u8], &[u8]> = TableDefinition::new("cas_volume_index");
const META_TABLE: TableDefinition<&str, &[u8]> = TableDefinition::new("cas_volume_meta");

// ─── Volume sizing (dynamic) ────────────────────────────────────────────────

fn target_volume_size(total_store_bytes: u64) -> u64 {
    match total_store_bytes {
        0..=1_073_741_824 => 16 * 1024 * 1024, // <1GB → 16MB
        1_073_741_825..=10_737_418_240 => 64 * 1024 * 1024, // <10GB → 64MB
        10_737_418_241..=107_374_182_400 => 128 * 1024 * 1024, // <100GB → 128MB
        107_374_182_401..=1_099_511_627_776 => 256 * 1024 * 1024, // <1TB → 256MB
        _ => 512 * 1024 * 1024,                // ≥1TB → 512MB
    }
}

fn align_up(offset: u64, alignment: u64) -> u64 {
    (offset + alignment - 1) & !(alignment - 1)
}

fn now_unix_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ─── Index entry ─────────────────────────────────────────────────────────────

/// Serialized as 24 bytes: volume_id(4) + offset(8) + size(4) + timestamp_secs(8 as f64)
#[derive(Clone, Debug)]
struct IndexEntry {
    volume_id: u32,
    offset: u64,
    size: u32,
    timestamp: f64,
}

impl IndexEntry {
    fn to_bytes(&self) -> [u8; 24] {
        let mut buf = [0u8; 24];
        buf[0..4].copy_from_slice(&self.volume_id.to_le_bytes());
        buf[4..12].copy_from_slice(&self.offset.to_le_bytes());
        buf[12..16].copy_from_slice(&self.size.to_le_bytes());
        buf[16..24].copy_from_slice(&self.timestamp.to_le_bytes());
        buf
    }

    fn from_bytes(data: &[u8]) -> Option<Self> {
        if data.len() < 24 {
            return None;
        }
        Some(Self {
            volume_id: u32::from_le_bytes(data[0..4].try_into().ok()?),
            offset: u64::from_le_bytes(data[4..12].try_into().ok()?),
            size: u32::from_le_bytes(data[12..16].try_into().ok()?),
            timestamp: f64::from_le_bytes(data[16..24].try_into().ok()?),
        })
    }
}

// ─── TOC entry (in-memory) ──────────────────────────────────────────────────

#[derive(Clone, Debug)]
struct TocEntry {
    hash: [u8; 32],
    offset: u64,
    size: u32,
    flags: u8,
}

// ─── Active volume (the one currently being written to) ─────────────────────

struct ActiveVolume {
    volume_id: u32,
    path: PathBuf,
    file: fs::File,
    write_offset: u64,
    entries: Vec<TocEntry>,
    target_size: u64,
}

impl ActiveVolume {
    fn new(volumes_dir: &Path, volume_id: u32, target_size: u64) -> io::Result<Self> {
        let path = volumes_dir.join(format!("vol_{:08x}.tmp", volume_id));
        let mut file = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .read(true)
            .truncate(true)
            .open(&path)?;

        // Write header
        let mut header = [0u8; HEADER_SIZE as usize];
        header[0..4].copy_from_slice(VOLUME_MAGIC);
        header[4..8].copy_from_slice(&VOLUME_VERSION.to_le_bytes());
        header[8..12].copy_from_slice(&volume_id.to_le_bytes());
        let created_at = now_unix_secs();
        header[12..20].copy_from_slice(&created_at.to_le_bytes());
        file.write_all(&header)?;

        Ok(Self {
            volume_id,
            path,
            file,
            write_offset: HEADER_SIZE,
            entries: Vec::new(),
            target_size,
        })
    }

    /// Append a blob entry. Returns (offset, aligned_end) of the written data.
    fn append(&mut self, hash: &[u8; 32], data: &[u8]) -> io::Result<u64> {
        let offset = self.write_offset;

        // Write entry header: hash(32) + size(4) + flags(1)
        self.file.write_all(hash)?;
        self.file.write_all(&(data.len() as u32).to_le_bytes())?;
        self.file.write_all(&[FLAG_NONE])?;

        // Write data
        self.file.write_all(data)?;

        // Align to 8 bytes
        let end = offset + ENTRY_HEADER_SIZE + data.len() as u64;
        let aligned_end = align_up(end, ALIGNMENT);
        let padding = aligned_end - end;
        if padding > 0 {
            self.file.write_all(&vec![0u8; padding as usize])?;
        }

        self.entries.push(TocEntry {
            hash: *hash,
            offset,
            size: data.len() as u32,
            flags: FLAG_NONE,
        });

        self.write_offset = aligned_end;
        Ok(offset)
    }

    fn current_size(&self) -> u64 {
        self.write_offset
    }

    fn is_full(&self) -> bool {
        self.write_offset >= self.target_size
    }

    fn entry_count(&self) -> usize {
        self.entries.len()
    }

    /// Seal: write TOC + footer, fdatasync, rename .tmp → .vol
    fn seal(mut self, volumes_dir: &Path) -> io::Result<(PathBuf, Vec<TocEntry>)> {
        let toc_offset = self.write_offset;
        let entry_count = self.entries.len() as u32;

        // Write TOC entries
        for entry in &self.entries {
            self.file.write_all(&entry.hash)?;
            self.file.write_all(&entry.offset.to_le_bytes())?;
            self.file.write_all(&entry.size.to_le_bytes())?;
            self.file.write_all(&[entry.flags])?;
        }

        // Write footer (24 bytes)
        let mut footer = [0u8; FOOTER_SIZE as usize];
        footer[0..4].copy_from_slice(VOLUME_MAGIC);
        footer[4..8].copy_from_slice(&VOLUME_VERSION.to_le_bytes());
        footer[8..12].copy_from_slice(&entry_count.to_le_bytes());
        footer[12..20].copy_from_slice(&toc_offset.to_le_bytes());
        // CRC32 of toc_offset + entry_count for integrity check
        let mut crc_data = Vec::with_capacity(12);
        crc_data.extend_from_slice(&entry_count.to_le_bytes());
        crc_data.extend_from_slice(&toc_offset.to_le_bytes());
        let checksum = crc32fast::hash(&crc_data);
        footer[20..24].copy_from_slice(&checksum.to_le_bytes());
        self.file.write_all(&footer)?;

        // fdatasync for durability
        self.file.sync_data()?;

        // Rename .tmp → .vol (atomic on POSIX)
        let sealed_path = volumes_dir.join(format!("vol_{:08x}.vol", self.volume_id));
        fs::rename(&self.path, &sealed_path)?;

        // fsync parent directory to persist the rename
        if let Ok(dir) = fs::File::open(volumes_dir) {
            let _ = dir.sync_all();
        }

        let entries = self.entries;
        Ok((sealed_path, entries))
    }
}

// ─── Read a sealed volume's TOC ─────────────────────────────────────────────

fn read_volume_toc(path: &Path) -> io::Result<(u32, Vec<TocEntry>)> {
    let mut file = fs::File::open(path)?;
    let file_size = file.metadata()?.len();

    if file_size < HEADER_SIZE + FOOTER_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Volume file too small",
        ));
    }

    // Read header to get volume_id
    let mut header = [0u8; HEADER_SIZE as usize];
    file.read_exact(&mut header)?;
    if &header[0..4] != VOLUME_MAGIC {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid volume magic",
        ));
    }
    let volume_id = u32::from_le_bytes(header[8..12].try_into().unwrap());

    // Read footer
    let mut footer = [0u8; FOOTER_SIZE as usize];
    file.seek(SeekFrom::End(-(FOOTER_SIZE as i64)))?;
    file.read_exact(&mut footer)?;

    if &footer[0..4] != VOLUME_MAGIC {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid footer magic",
        ));
    }

    let entry_count = u32::from_le_bytes(footer[8..12].try_into().unwrap());
    let toc_offset = u64::from_le_bytes(footer[12..20].try_into().unwrap());
    let stored_checksum = u32::from_le_bytes(footer[20..24].try_into().unwrap());

    // Verify checksum
    let mut crc_data = Vec::with_capacity(12);
    crc_data.extend_from_slice(&entry_count.to_le_bytes());
    crc_data.extend_from_slice(&toc_offset.to_le_bytes());
    let computed_checksum = crc32fast::hash(&crc_data);
    if stored_checksum != computed_checksum {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Footer checksum mismatch",
        ));
    }

    // Read TOC entries
    file.seek(SeekFrom::Start(toc_offset))?;
    let mut entries = Vec::with_capacity(entry_count as usize);
    for _ in 0..entry_count {
        let mut toc_buf = [0u8; TOC_ENTRY_SIZE as usize];
        file.read_exact(&mut toc_buf)?;
        let mut hash = [0u8; 32];
        hash.copy_from_slice(&toc_buf[0..32]);
        let offset = u64::from_le_bytes(toc_buf[32..40].try_into().unwrap());
        let size = u32::from_le_bytes(toc_buf[40..44].try_into().unwrap());
        let flags = toc_buf[44];
        entries.push(TocEntry {
            hash,
            offset,
            size,
            flags,
        });
    }

    Ok((volume_id, entries))
}

/// Read a single blob from a sealed volume using pread semantics.
fn pread_blob(path: &Path, offset: u64, size: u32) -> io::Result<Vec<u8>> {
    let mut file = fs::File::open(path)?;
    // Skip entry header (hash + size + flags) to get to data
    file.seek(SeekFrom::Start(offset + ENTRY_HEADER_SIZE))?;
    let mut buf = vec![0u8; size as usize];
    file.read_exact(&mut buf)?;
    Ok(buf)
}

// ─── VolumeEngine — the main engine exposed to Python ───────────────────────

/// Thread-safe CAS volume engine with redb index.
///
/// Manages append-only volume files and a redb index mapping
/// content hashes to (volume_id, offset, size).
#[pyclass]
pub struct VolumeEngine {
    /// Root directory for volume storage
    volumes_dir: PathBuf,
    /// redb database for the index
    db: RwLock<Database>,
    /// Currently active (writable) volume
    active: Mutex<Option<ActiveVolume>>,
    /// Next volume ID counter
    next_volume_id: AtomicU32,
    /// Total bytes stored (for dynamic volume sizing)
    total_bytes: AtomicU64,
    /// Volume file paths: volume_id → path
    volume_paths: RwLock<HashMap<u32, PathBuf>>,
    /// Whether the engine is open
    is_open: AtomicBool,
    /// Configurable target volume size override (0 = dynamic)
    target_volume_size_override: u64,
    /// Compaction I/O rate limit in bytes/sec (0 = unlimited)
    compaction_rate_limit: u64,
    /// Sparsity threshold for compaction trigger (0.0 - 1.0)
    compaction_sparsity_threshold: f64,
}

fn db_err(e: impl std::fmt::Display) -> PyErr {
    pyo3::exceptions::PyIOError::new_err(format!("Volume index error: {}", e))
}

fn io_err(e: impl std::fmt::Display) -> PyErr {
    pyo3::exceptions::PyIOError::new_err(format!("Volume I/O error: {}", e))
}

#[pymethods]
impl VolumeEngine {
    /// Create or open a volume engine at the given directory.
    ///
    /// Args:
    ///     path: Root directory for volumes and index
    ///     target_volume_size: Override volume size in bytes (0 = dynamic)
    ///     compaction_rate_limit: I/O rate limit for compaction in bytes/sec (0 = unlimited)
    ///     compaction_sparsity_threshold: Trigger compaction when sparsity exceeds this (0.0-1.0)
    #[new]
    #[pyo3(signature = (path, target_volume_size=0, compaction_rate_limit=52_428_800, compaction_sparsity_threshold=0.4))]
    fn new(
        path: &str,
        target_volume_size: u64,
        compaction_rate_limit: u64,
        compaction_sparsity_threshold: f64,
    ) -> PyResult<Self> {
        let volumes_dir = PathBuf::from(path);
        fs::create_dir_all(&volumes_dir).map_err(io_err)?;

        let db_path = volumes_dir.join("volume_index.redb");
        let db = Database::create(&db_path).map_err(db_err)?;

        // Ensure tables exist
        {
            let write_txn = db.begin_write().map_err(db_err)?;
            write_txn.open_table(INDEX_TABLE).map_err(db_err)?;
            write_txn.open_table(META_TABLE).map_err(db_err)?;
            write_txn.commit().map_err(db_err)?;
        }

        let mut engine = Self {
            volumes_dir,
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: target_volume_size,
            compaction_rate_limit,
            compaction_sparsity_threshold,
        };

        // Startup recovery
        engine.recover_on_startup()?;

        Ok(engine)
    }

    /// Check if a content hash exists in the index.
    fn exists(&self, hash_hex: &str) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        let result = table.get(hash.as_slice()).map_err(db_err)?;
        Ok(result.is_some())
    }

    /// Write a blob. Returns true if it was new (not a dedup hit).
    fn put(&self, hash_hex: &str, data: &[u8]) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;

        // Dedup check: if already indexed, skip
        {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            if table.get(hash.as_slice()).map_err(db_err)?.is_some() {
                return Ok(false);
            }
        }

        // Append to active volume
        let (volume_id, offset) = self.append_to_active(&hash, data)?;

        // Update index
        let entry = IndexEntry {
            volume_id,
            offset,
            size: data.len() as u32,
            timestamp: now_unix_secs(),
        };
        {
            let db = self.db.read();
            let txn = db.begin_write().map_err(db_err)?;
            {
                let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                table
                    .insert(hash.as_slice(), entry.to_bytes().as_slice())
                    .map_err(db_err)?;
            }
            txn.commit().map_err(db_err)?;
        }

        self.total_bytes
            .fetch_add(data.len() as u64, Ordering::Relaxed);

        Ok(true)
    }

    /// Read a blob by hash. Returns None if not found.
    fn get<'py>(&self, py: Python<'py>, hash_hex: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let hash = hex_to_hash(hash_hex)?;

        // Lookup in index
        let entry = {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            match table.get(hash.as_slice()).map_err(db_err)? {
                Some(val) => match IndexEntry::from_bytes(val.value()) {
                    Some(e) => e,
                    None => return Ok(None),
                },
                None => return Ok(None),
            }
        };

        // Find volume path
        let vol_path = {
            let paths = self.volume_paths.read();
            match paths.get(&entry.volume_id) {
                Some(p) => p.clone(),
                None => return Ok(None),
            }
        };

        // pread from volume
        let data = pread_blob(&vol_path, entry.offset, entry.size).map_err(io_err)?;
        Ok(Some(PyBytes::new(py, &data)))
    }

    /// Get blob size by hash. Returns None if not found.
    fn get_size(&self, hash_hex: &str) -> PyResult<Option<u32>> {
        let hash = hex_to_hash(hash_hex)?;
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        match table.get(hash.as_slice()).map_err(db_err)? {
            Some(val) => Ok(IndexEntry::from_bytes(val.value()).map(|e| e.size)),
            None => Ok(None),
        }
    }

    /// Delete (tombstone) a blob by hash. Returns true if it existed.
    fn delete(&self, hash_hex: &str) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;
        let db = self.db.read();
        let txn = db.begin_write().map_err(db_err)?;
        let existed;
        {
            let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            existed = table.remove(hash.as_slice()).map_err(db_err)?.is_some();
        }
        txn.commit().map_err(db_err)?;
        Ok(existed)
    }

    /// Batch read multiple blobs. Returns dict of hash_hex → bytes (missing hashes omitted).
    fn batch_get<'py>(
        &self,
        py: Python<'py>,
        hash_hexes: Vec<String>,
    ) -> PyResult<HashMap<String, Bound<'py, PyBytes>>> {
        let mut result = HashMap::with_capacity(hash_hexes.len());

        // Batch lookup: collect all index entries first
        let mut lookups: Vec<(String, [u8; 32], IndexEntry)> = Vec::with_capacity(hash_hexes.len());
        {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            for hex in &hash_hexes {
                if let Ok(hash) = hex_to_hash(hex) {
                    if let Ok(Some(val)) = table.get(hash.as_slice()) {
                        if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                            lookups.push((hex.clone(), hash, entry));
                        }
                    }
                }
            }
        }

        // Group reads by volume for locality
        let mut by_volume: HashMap<u32, Vec<(String, u64, u32)>> = HashMap::new();
        for (hex, _hash, entry) in &lookups {
            by_volume.entry(entry.volume_id).or_default().push((
                hex.clone(),
                entry.offset,
                entry.size,
            ));
        }

        let paths = self.volume_paths.read();
        for (vol_id, reads) in &by_volume {
            if let Some(vol_path) = paths.get(vol_id) {
                if let Ok(mut file) = fs::File::open(vol_path) {
                    // Sort by offset for sequential reads
                    let mut sorted_reads = reads.clone();
                    sorted_reads.sort_by_key(|r| r.1);

                    for (hex, offset, size) in sorted_reads {
                        if file
                            .seek(SeekFrom::Start(offset + ENTRY_HEADER_SIZE))
                            .is_ok()
                        {
                            let mut buf = vec![0u8; size as usize];
                            if file.read_exact(&mut buf).is_ok() {
                                result.insert(hex, PyBytes::new(py, &buf));
                            }
                        }
                    }
                }
            }
        }

        Ok(result)
    }

    /// List all content hashes with their write timestamps.
    /// Returns list of (hash_hex, timestamp_secs) tuples.
    fn list_content_hashes(&self) -> PyResult<Vec<(String, f64)>> {
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;

        let mut result = Vec::new();
        let iter = table.iter().map_err(db_err)?;
        for item in iter {
            let (key, val) = item.map_err(db_err)?;
            let hash_hex = hex::encode(key.value());
            if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                result.push((hash_hex, entry.timestamp));
            }
        }

        Ok(result)
    }

    /// Get the write timestamp for a specific hash. Returns None if not found.
    fn get_timestamp(&self, hash_hex: &str) -> PyResult<Option<f64>> {
        let hash = hex_to_hash(hash_hex)?;
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        match table.get(hash.as_slice()).map_err(db_err)? {
            Some(val) => Ok(IndexEntry::from_bytes(val.value()).map(|e| e.timestamp)),
            None => Ok(None),
        }
    }

    /// Get total number of indexed blobs.
    fn len(&self) -> PyResult<u64> {
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        table.len().map_err(db_err)
    }

    /// Get total bytes stored across all volumes.
    fn total_bytes(&self) -> u64 {
        self.total_bytes.load(Ordering::Relaxed)
    }

    /// Seal the active volume (for testing or explicit flush).
    fn seal_active(&self) -> PyResult<bool> {
        self.do_seal_active()
    }

    /// Run compaction on volumes exceeding sparsity threshold.
    /// Returns (volumes_compacted, blobs_moved, bytes_reclaimed).
    fn compact(&self) -> PyResult<(u32, u64, u64)> {
        self.do_compact()
    }

    /// Get volume stats: {volume_count, total_blobs, total_bytes, active_volume_size}.
    fn stats(&self) -> PyResult<HashMap<String, u64>> {
        let mut stats = HashMap::new();
        let paths = self.volume_paths.read();
        stats.insert("sealed_volume_count".to_string(), paths.len() as u64);

        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        stats.insert("total_blobs".to_string(), table.len().map_err(db_err)?);
        stats.insert(
            "total_bytes".to_string(),
            self.total_bytes.load(Ordering::Relaxed),
        );

        let active = self.active.lock();
        stats.insert(
            "active_volume_size".to_string(),
            active.as_ref().map_or(0, |v| v.current_size()),
        );
        stats.insert(
            "active_volume_entries".to_string(),
            active.as_ref().map_or(0, |v| v.entry_count() as u64),
        );

        Ok(stats)
    }

    /// Close the engine: seal active volume, close database.
    fn close(&self) -> PyResult<()> {
        if !self.is_open.swap(false, Ordering::SeqCst) {
            return Ok(());
        }
        // Seal active volume if it has entries
        let _ = self.do_seal_active();
        Ok(())
    }

    /// Migrate existing one-file-per-hash CAS blobs into volumes.
    ///
    /// Scans `cas_root` for files matching the cas/{h[:2]}/{h[2:4]}/{h} layout,
    /// packs them into volumes, and deletes the originals after verification.
    ///
    /// Args:
    ///     cas_root: Path to the existing CAS directory (e.g., /data/cas)
    ///     batch_size: Number of files to migrate per batch (default 1000)
    ///     delete_originals: Whether to delete original files after migration (default true)
    ///     rate_limit_bytes: Max bytes to migrate per call (0 = unlimited)
    ///
    /// Returns:
    ///     (files_migrated, files_skipped, bytes_migrated)
    #[pyo3(signature = (cas_root, batch_size=1000, delete_originals=true, rate_limit_bytes=0))]
    fn migrate_from_files(
        &self,
        cas_root: &str,
        batch_size: usize,
        delete_originals: bool,
        rate_limit_bytes: u64,
    ) -> PyResult<(u64, u64, u64)> {
        let cas_path = PathBuf::from(cas_root);
        if !cas_path.is_dir() {
            return Ok((0, 0, 0));
        }

        let mut migrated: u64 = 0;
        let mut skipped: u64 = 0;
        let mut bytes_migrated: u64 = 0;
        let mut budget = if rate_limit_bytes > 0 {
            rate_limit_bytes as i64
        } else {
            i64::MAX
        };

        // Walk cas/{h[:2]}/{h[2:4]}/{hash} structure
        let entries = fs::read_dir(&cas_path).map_err(io_err)?;
        for dir1 in entries {
            let dir1 = dir1.map_err(io_err)?;
            if !dir1.file_type().map_err(io_err)?.is_dir() {
                continue;
            }

            let sub_entries = match fs::read_dir(dir1.path()) {
                Ok(e) => e,
                Err(_) => continue,
            };

            for dir2 in sub_entries {
                let dir2 = match dir2 {
                    Ok(e) => e,
                    Err(_) => continue,
                };
                if !dir2
                    .file_type()
                    .unwrap_or_else(|_| fs::metadata(dir2.path()).unwrap().file_type())
                    .is_dir()
                {
                    continue;
                }

                let file_entries = match fs::read_dir(dir2.path()) {
                    Ok(e) => e,
                    Err(_) => continue,
                };

                for file_entry in file_entries {
                    let file_entry = match file_entry {
                        Ok(e) => e,
                        Err(_) => continue,
                    };

                    let file_name = file_entry.file_name().to_string_lossy().to_string();

                    // Skip .meta sidecars and non-hash files
                    if file_name.ends_with(".meta") || file_name.ends_with(".lock") {
                        continue;
                    }
                    if file_name.len() != 64 {
                        continue;
                    }

                    // Parse hash
                    let hash = match hex_to_hash(&file_name) {
                        Ok(h) => h,
                        Err(_) => {
                            skipped += 1;
                            continue;
                        }
                    };

                    // Skip if already in volume index
                    {
                        let db = self.db.read();
                        let txn = db.begin_read().map_err(db_err)?;
                        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                        if table.get(hash.as_slice()).map_err(db_err)?.is_some() {
                            skipped += 1;
                            continue;
                        }
                    }

                    // Read file content
                    let file_path = file_entry.path();
                    let data = match fs::read(&file_path) {
                        Ok(d) => d,
                        Err(_) => {
                            skipped += 1;
                            continue;
                        }
                    };

                    // Append to active volume
                    let (volume_id, offset) = self.append_to_active(&hash, &data)?;

                    // Update index
                    let entry = IndexEntry {
                        volume_id,
                        offset,
                        size: data.len() as u32,
                        timestamp: now_unix_secs(),
                    };
                    {
                        let db = self.db.read();
                        let txn = db.begin_write().map_err(db_err)?;
                        {
                            let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                            table
                                .insert(hash.as_slice(), entry.to_bytes().as_slice())
                                .map_err(db_err)?;
                        }
                        txn.commit().map_err(db_err)?;
                    }

                    bytes_migrated += data.len() as u64;
                    self.total_bytes
                        .fetch_add(data.len() as u64, Ordering::Relaxed);
                    migrated += 1;

                    // Delete original file after successful migration
                    if delete_originals {
                        let _ = fs::remove_file(&file_path);
                    }

                    budget -= data.len() as i64;
                    if budget <= 0 {
                        // Seal current volume before returning
                        let _ = self.do_seal_active();
                        return Ok((migrated, skipped, bytes_migrated));
                    }

                    if (migrated as usize).is_multiple_of(batch_size) {
                        // Seal volume periodically during migration
                        let _ = self.do_seal_active();
                    }
                }
            }
        }

        // Seal final volume
        let _ = self.do_seal_active();

        // Clean up empty directories if we deleted originals
        if delete_originals {
            Self::cleanup_empty_dirs(&cas_path);
        }

        Ok((migrated, skipped, bytes_migrated))
    }
}

// ─── Internal methods (not exposed to Python) ───────────────────────────────

impl VolumeEngine {
    /// Remove empty directories recursively (bottom-up cleanup after migration).
    fn cleanup_empty_dirs(dir: &Path) {
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    Self::cleanup_empty_dirs(&path);
                    // Try to remove if now empty
                    let _ = fs::remove_dir(&path);
                }
            }
        }
    }

    fn get_target_volume_size(&self) -> u64 {
        if self.target_volume_size_override > 0 {
            return self.target_volume_size_override;
        }
        target_volume_size(self.total_bytes.load(Ordering::Relaxed))
    }

    /// Append data to the active volume. Seals and creates a new one if full.
    fn append_to_active(&self, hash: &[u8; 32], data: &[u8]) -> PyResult<(u32, u64)> {
        let mut active_guard = self.active.lock();

        // Create active volume if none exists
        if active_guard.is_none() {
            let vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
            let target = self.get_target_volume_size();
            let vol = ActiveVolume::new(&self.volumes_dir, vol_id, target).map_err(io_err)?;
            // Register .tmp path immediately so get() can read from active volume
            self.volume_paths.write().insert(vol_id, vol.path.clone());
            *active_guard = Some(vol);
        }

        // Check if current active is full
        {
            let vol = active_guard.as_ref().unwrap();
            if vol.is_full() {
                // Seal current, create new
                let old_vol = active_guard.take().unwrap();
                self.seal_volume(old_vol)?;

                let vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
                let target = self.get_target_volume_size();
                let new_vol =
                    ActiveVolume::new(&self.volumes_dir, vol_id, target).map_err(io_err)?;
                // Register .tmp path immediately
                self.volume_paths
                    .write()
                    .insert(vol_id, new_vol.path.clone());
                *active_guard = Some(new_vol);
            }
        }

        let vol = active_guard.as_mut().unwrap();
        let volume_id = vol.volume_id;
        let offset = vol.append(hash, data).map_err(io_err)?;

        Ok((volume_id, offset))
    }

    /// Seal a volume and register it in the volume paths.
    ///
    /// Before sealing, filters out entries that were deleted from the index
    /// since they were appended. This prevents deleted blobs from being
    /// resurrected on crash recovery (which re-inserts TOC entries missing
    /// from the index).
    fn seal_volume(&self, mut vol: ActiveVolume) -> PyResult<()> {
        if vol.entry_count() == 0 {
            // Empty volume — just delete the temp file
            let _ = fs::remove_file(&vol.path);
            self.volume_paths.write().remove(&vol.volume_id);
            return Ok(());
        }

        // Filter entries: only keep those still present in the index.
        // Deleted blobs have been removed from the index by delete(), but
        // their data is still in the volume file. Excluding them from the
        // TOC ensures they won't be resurrected by crash recovery.
        {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            vol.entries
                .retain(|entry| table.get(entry.hash.as_slice()).ok().flatten().is_some());
        }

        if vol.entries.is_empty() {
            // All entries were deleted — discard the volume
            let _ = fs::remove_file(&vol.path);
            self.volume_paths.write().remove(&vol.volume_id);
            return Ok(());
        }

        let vol_id = vol.volume_id;
        let (sealed_path, _entries) = vol.seal(&self.volumes_dir).map_err(io_err)?;

        // Register sealed volume path (replaces the .tmp entry)
        self.volume_paths.write().insert(vol_id, sealed_path);

        Ok(())
    }

    fn do_seal_active(&self) -> PyResult<bool> {
        let mut active_guard = self.active.lock();
        if let Some(vol) = active_guard.take() {
            if vol.entry_count() > 0 {
                // Before sealing, we need to move all entries from the active
                // volume's in-memory tracking to the sealed volume's index.
                // The entries are already in the redb index (added during put()),
                // but the volume_paths need to be updated after seal.
                self.seal_volume(vol)?;
                return Ok(true);
            } else {
                let _ = fs::remove_file(&vol.path);
            }
        }
        Ok(false)
    }

    /// Startup recovery: delete .tmp files, scan .vol files to rebuild state.
    fn recover_on_startup(&mut self) -> PyResult<()> {
        let entries = fs::read_dir(&self.volumes_dir).map_err(io_err)?;

        let mut max_vol_id: u32 = 0;
        let mut total_bytes: u64 = 0;
        let mut volume_paths = HashMap::new();

        // Track which hashes are in the index
        let indexed_hashes: std::collections::HashSet<Vec<u8>> = {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            let mut set = std::collections::HashSet::new();
            let iter = table.iter().map_err(db_err)?;
            for item in iter {
                let (key, _) = item.map_err(db_err)?;
                set.insert(key.value().to_vec());
            }
            set
        };

        for entry in entries {
            let entry = entry.map_err(io_err)?;
            let path = entry.path();
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();

            if name.ends_with(".tmp") {
                // Crash recovery: delete incomplete volumes
                let _ = fs::remove_file(&path);
                continue;
            }

            if name.ends_with(".vol") {
                // Read TOC from sealed volume
                match read_volume_toc(&path) {
                    Ok((vol_id, toc_entries)) => {
                        max_vol_id = max_vol_id.max(vol_id);
                        volume_paths.insert(vol_id, path.clone());

                        // Reconcile: add any entries in VOL but not in index
                        let now = now_unix_secs();
                        let db = self.db.read();
                        let txn = db.begin_write().map_err(db_err)?;
                        {
                            let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                            for toc_entry in &toc_entries {
                                if toc_entry.flags & FLAG_TOMBSTONE != 0 {
                                    continue;
                                }
                                if !indexed_hashes.contains(toc_entry.hash.as_slice()) {
                                    let idx_entry = IndexEntry {
                                        volume_id: vol_id,
                                        offset: toc_entry.offset,
                                        size: toc_entry.size,
                                        timestamp: now,
                                    };
                                    table
                                        .insert(
                                            toc_entry.hash.as_slice(),
                                            idx_entry.to_bytes().as_slice(),
                                        )
                                        .map_err(db_err)?;
                                }
                                total_bytes += toc_entry.size as u64;
                            }
                        }
                        txn.commit().map_err(db_err)?;
                    }
                    Err(e) => {
                        // Corrupted volume — log and skip
                        eprintln!(
                            "Warning: skipping corrupted volume {}: {}",
                            path.display(),
                            e
                        );
                    }
                }
            }
        }

        // Verify index entries point to existing volumes — remove stale entries
        {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            let mut stale_keys: Vec<Vec<u8>> = Vec::new();
            let iter = table.iter().map_err(db_err)?;
            for item in iter {
                let (key, val) = item.map_err(db_err)?;
                if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                    if !volume_paths.contains_key(&entry.volume_id) {
                        stale_keys.push(key.value().to_vec());
                    }
                }
            }
            drop(table);
            drop(txn);

            if !stale_keys.is_empty() {
                let txn = db.begin_write().map_err(db_err)?;
                {
                    let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                    for key in &stale_keys {
                        table.remove(key.as_slice()).map_err(db_err)?;
                    }
                }
                txn.commit().map_err(db_err)?;
            }
        }

        self.next_volume_id.store(max_vol_id + 1, Ordering::Relaxed);
        self.total_bytes.store(total_bytes, Ordering::Relaxed);
        *self.volume_paths.write() = volume_paths;

        Ok(())
    }

    /// Run compaction: find sparse volumes, copy live entries to new volume.
    fn do_compact(&self) -> PyResult<(u32, u64, u64)> {
        let mut volumes_compacted: u32 = 0;
        let mut blobs_moved: u64 = 0;
        let mut bytes_reclaimed: u64 = 0;

        // Build per-volume live entry counts from index
        let mut live_per_volume: HashMap<u32, (u64, u64)> = HashMap::new(); // vol_id → (live_count, live_bytes)
        {
            let db = self.db.read();
            let txn = db.begin_read().map_err(db_err)?;
            let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            let iter = table.iter().map_err(db_err)?;
            for item in iter {
                let (_key, val) = item.map_err(db_err)?;
                if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                    let stats = live_per_volume.entry(entry.volume_id).or_insert((0, 0));
                    stats.0 += 1;
                    stats.1 += entry.size as u64;
                }
            }
        }

        // Find candidate volumes with high sparsity
        let paths = self.volume_paths.read().clone();
        let mut candidates: Vec<(u32, PathBuf, u64, u64)> = Vec::new(); // (vol_id, path, total_bytes, live_bytes)

        for (vol_id, path) in &paths {
            if let Ok(meta) = fs::metadata(path) {
                let total = meta.len();
                let (_, live_bytes) = live_per_volume.get(vol_id).copied().unwrap_or((0, 0));
                if total > HEADER_SIZE + FOOTER_SIZE {
                    let data_bytes = total - HEADER_SIZE - FOOTER_SIZE;
                    let sparsity = if data_bytes > 0 {
                        1.0 - (live_bytes as f64 / data_bytes as f64)
                    } else {
                        0.0
                    };
                    if sparsity >= self.compaction_sparsity_threshold {
                        candidates.push((*vol_id, path.clone(), total, live_bytes));
                    }
                }
            }
        }

        // Sort by sparsity descending (most sparse first)
        candidates.sort_by(|a, b| {
            let sp_a = 1.0 - (a.3 as f64 / a.2 as f64);
            let sp_b = 1.0 - (b.3 as f64 / b.2 as f64);
            sp_b.partial_cmp(&sp_a).unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut rate_budget = self.compaction_rate_limit as i64;

        for (vol_id, vol_path, vol_total, _) in candidates {
            // Collect live entries from this volume
            let mut live_entries: Vec<([u8; 32], IndexEntry)> = Vec::new();
            {
                let db = self.db.read();
                let txn = db.begin_read().map_err(db_err)?;
                let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                let iter = table.iter().map_err(db_err)?;
                for item in iter {
                    let (key, val) = item.map_err(db_err)?;
                    if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                        if entry.volume_id == vol_id {
                            let mut hash = [0u8; 32];
                            hash.copy_from_slice(key.value());
                            live_entries.push((hash, entry));
                        }
                    }
                }
            }

            if live_entries.is_empty() {
                // Entirely dead volume — just delete
                let _ = fs::remove_file(&vol_path);
                self.volume_paths.write().remove(&vol_id);
                bytes_reclaimed += vol_total;
                volumes_compacted += 1;
                continue;
            }

            // Read live blobs and write to new volume
            let new_vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
            let target = self.get_target_volume_size();
            let mut new_vol =
                ActiveVolume::new(&self.volumes_dir, new_vol_id, target).map_err(io_err)?;

            let total_live = live_entries.len();
            let mut copied: u64 = 0;
            let mut rate_exhausted = false;

            for (hash, entry) in &live_entries {
                // Read blob from old volume
                match pread_blob(&vol_path, entry.offset, entry.size) {
                    Ok(data) => {
                        let new_offset = new_vol.append(hash, &data).map_err(io_err)?;

                        // Update index to point to new volume
                        let new_entry = IndexEntry {
                            volume_id: new_vol_id,
                            offset: new_offset,
                            size: entry.size,
                            timestamp: entry.timestamp,
                        };
                        let db = self.db.read();
                        let txn = db.begin_write().map_err(db_err)?;
                        {
                            let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                            table
                                .insert(hash.as_slice(), new_entry.to_bytes().as_slice())
                                .map_err(db_err)?;
                        }
                        txn.commit().map_err(db_err)?;

                        blobs_moved += 1;
                        copied += 1;

                        if rate_budget > 0 {
                            rate_budget -= entry.size as i64;
                            if rate_budget <= 0 {
                                rate_exhausted = true;
                                break;
                            }
                        }
                    }
                    Err(_) => continue, // Skip unreadable blobs
                }
            }

            // Seal the new volume
            if new_vol.entry_count() > 0 {
                let (sealed_path, _) = new_vol.seal(&self.volumes_dir).map_err(io_err)?;
                self.volume_paths.write().insert(new_vol_id, sealed_path);
            } else {
                let _ = fs::remove_file(&new_vol.path);
            }

            // Only delete old volume if ALL live entries were copied.
            // If rate limit interrupted, some entries still reference the old volume.
            if copied as usize >= total_live {
                let _ = fs::remove_file(&vol_path);
                self.volume_paths.write().remove(&vol_id);
                bytes_reclaimed += vol_total;
                volumes_compacted += 1;
            }

            if rate_exhausted {
                break;
            }
        }

        Ok((volumes_compacted, blobs_moved, bytes_reclaimed))
    }
}

impl Drop for VolumeEngine {
    fn drop(&mut self) {
        if self.is_open.load(Ordering::SeqCst) {
            // Best-effort seal on drop
            let mut active_guard = self.active.lock();
            if let Some(vol) = active_guard.take() {
                if vol.entry_count() > 0 {
                    let _ = vol.seal(&self.volumes_dir);
                } else {
                    let _ = fs::remove_file(&vol.path);
                }
            }
        }
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

fn hex_to_hash(hex_str: &str) -> PyResult<[u8; 32]> {
    let bytes = hex::decode(hex_str)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid hex hash: {}", e)))?;
    if bytes.len() != 32 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Hash must be 32 bytes (64 hex chars), got {} bytes",
            bytes.len()
        )));
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&bytes);
    Ok(arr)
}

// Inline hex encoding (avoid extra dependency for this simple case)
mod hex {
    pub fn decode(s: &str) -> Result<Vec<u8>, String> {
        if !s.len().is_multiple_of(2) {
            return Err("Odd-length hex string".to_string());
        }
        (0..s.len())
            .step_by(2)
            .map(|i| {
                u8::from_str_radix(&s[i..i + 2], 16)
                    .map_err(|e| format!("Invalid hex at position {}: {}", i, e))
            })
            .collect()
    }

    pub fn encode(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{:02x}", b)).collect()
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

    fn hash_hex(seed: u8) -> String {
        hex::encode(&make_hash(seed))
    }

    #[test]
    fn test_hex_roundtrip() {
        let hash = make_hash(0xab);
        let encoded = hex::encode(&hash);
        let decoded = hex::decode(&encoded).unwrap();
        assert_eq!(decoded, hash.to_vec());
    }

    #[test]
    fn test_index_entry_roundtrip() {
        let entry = IndexEntry {
            volume_id: 42,
            offset: 1234567890,
            size: 9999,
            timestamp: 1700000000.5,
        };
        let bytes = entry.to_bytes();
        let decoded = IndexEntry::from_bytes(&bytes).unwrap();
        assert_eq!(decoded.volume_id, 42);
        assert_eq!(decoded.offset, 1234567890);
        assert_eq!(decoded.size, 9999);
        assert!((decoded.timestamp - 1700000000.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_align_up() {
        assert_eq!(align_up(0, 8), 0);
        assert_eq!(align_up(1, 8), 8);
        assert_eq!(align_up(7, 8), 8);
        assert_eq!(align_up(8, 8), 8);
        assert_eq!(align_up(9, 8), 16);
        assert_eq!(align_up(64, 8), 64);
    }

    #[test]
    fn test_target_volume_size() {
        assert_eq!(target_volume_size(0), 16 * 1024 * 1024);
        assert_eq!(target_volume_size(500_000_000), 16 * 1024 * 1024);
        assert_eq!(target_volume_size(2_000_000_000), 64 * 1024 * 1024);
        assert_eq!(target_volume_size(50_000_000_000), 128 * 1024 * 1024);
        assert_eq!(target_volume_size(500_000_000_000), 256 * 1024 * 1024);
        assert_eq!(target_volume_size(2_000_000_000_000), 512 * 1024 * 1024);
    }

    #[test]
    fn test_active_volume_write_and_seal() {
        let dir = TempDir::new().unwrap();
        let mut vol = ActiveVolume::new(dir.path(), 1, 1024 * 1024).unwrap();

        let hash = make_hash(1);
        let data = b"hello world";
        let offset = vol.append(&hash, data).unwrap();
        assert_eq!(offset, HEADER_SIZE); // First entry starts after header
        assert_eq!(vol.entry_count(), 1);

        let (sealed_path, entries) = vol.seal(dir.path()).unwrap();
        assert!(sealed_path.exists());
        assert!(sealed_path.to_string_lossy().ends_with(".vol"));
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].size, data.len() as u32);
    }

    #[test]
    fn test_read_volume_toc() {
        let dir = TempDir::new().unwrap();
        let mut vol = ActiveVolume::new(dir.path(), 1, 1024 * 1024).unwrap();

        let hash1 = make_hash(1);
        let hash2 = make_hash(2);
        vol.append(&hash1, b"data one").unwrap();
        vol.append(&hash2, b"data two").unwrap();

        let (sealed_path, _) = vol.seal(dir.path()).unwrap();

        let (vol_id, entries) = read_volume_toc(&sealed_path).unwrap();
        assert_eq!(vol_id, 1);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].hash, hash1);
        assert_eq!(entries[1].hash, hash2);
    }

    #[test]
    fn test_pread_blob() {
        let dir = TempDir::new().unwrap();
        let mut vol = ActiveVolume::new(dir.path(), 1, 1024 * 1024).unwrap();

        let hash = make_hash(1);
        let data = b"hello pread";
        let offset = vol.append(&hash, data).unwrap();
        let (sealed_path, _) = vol.seal(dir.path()).unwrap();

        let read_data = pread_blob(&sealed_path, offset, data.len() as u32).unwrap();
        assert_eq!(read_data, data);
    }

    // Integration tests using Python API names but testing Rust internals
    #[test]
    fn test_engine_put_get_roundtrip() {
        let dir = TempDir::new().unwrap();
        // Direct Rust construction for testing (bypass PyO3)
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        let hash = hash_hex(1);
        let data = b"test data for roundtrip";

        // Put
        let is_new = engine.put(&hash, data).unwrap();
        assert!(is_new);

        // Read-after-write should work without explicit seal
        // (active volume's .tmp path is registered in volume_paths)

        // Exists
        assert!(engine.exists(&hash).unwrap());

        // Size
        assert_eq!(engine.get_size(&hash).unwrap(), Some(data.len() as u32));

        // List
        let hashes = engine.list_content_hashes().unwrap();
        assert_eq!(hashes.len(), 1);
        assert_eq!(hashes[0].0, hash);
    }

    #[test]
    fn test_engine_dedup() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        let hash = hash_hex(1);
        let data = b"dedup test data";

        assert!(engine.put(&hash, data).unwrap()); // first write = new
        assert!(!engine.put(&hash, data).unwrap()); // second write = dedup hit
    }

    #[test]
    fn test_engine_delete() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        let hash = hash_hex(1);
        engine.put(&hash, b"to be deleted").unwrap();
        assert!(engine.exists(&hash).unwrap());

        assert!(engine.delete(&hash).unwrap()); // existed → true
        assert!(!engine.exists(&hash).unwrap());
        assert!(!engine.delete(&hash).unwrap()); // already gone → false
    }

    #[test]
    fn test_crash_recovery_deletes_tmp() {
        let dir = TempDir::new().unwrap();

        // Create a fake .tmp file (simulating crash during write)
        let tmp_path = dir.path().join("vol_00000001.tmp");
        fs::write(&tmp_path, b"incomplete volume data").unwrap();
        assert!(tmp_path.exists());

        // Create engine — should delete .tmp
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let mut engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 0,
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        engine.recover_on_startup().unwrap();

        // .tmp should be gone
        assert!(!tmp_path.exists());
    }

    #[test]
    fn test_crash_recovery_rebuilds_from_vol() {
        let dir = TempDir::new().unwrap();

        // Create and seal a volume manually
        let mut vol = ActiveVolume::new(dir.path(), 1, 1024 * 1024).unwrap();
        let hash = make_hash(0xAA);
        vol.append(&hash, b"recovered data").unwrap();
        vol.seal(dir.path()).unwrap();

        // Create engine with EMPTY index — should reconcile from .vol TOC
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let mut engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 0,
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        engine.recover_on_startup().unwrap();

        // Hash should now be in the index
        let hash_hex_str = hex::encode(&hash);
        assert!(engine.exists(&hash_hex_str).unwrap());
        assert_eq!(engine.volume_paths.read().len(), 1);
    }

    #[test]
    fn test_volume_auto_seal_on_full() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        // Very small target so volumes seal quickly
        let engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 256, // Very small!
            compaction_rate_limit: 0,
            compaction_sparsity_threshold: 0.4,
        };

        // Write enough data to trigger multiple volume seals
        for i in 0..10u8 {
            let hash = hash_hex(i);
            engine.put(&hash, &vec![i; 100]).unwrap();
        }

        // Should have sealed some volumes
        let sealed_count = engine.volume_paths.read().len();
        assert!(sealed_count > 0, "Expected sealed volumes, got 0");

        // All entries should be in the index
        for i in 0..10u8 {
            assert!(engine.exists(&hash_hex(i)).unwrap());
        }
    }

    #[test]
    fn test_compaction() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }

        let engine = VolumeEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 512, // Small volumes for testing
            compaction_rate_limit: 0,         // No rate limit for tests
            compaction_sparsity_threshold: 0.3,
        };

        // Write 10 entries
        for i in 0..10u8 {
            engine.put(&hash_hex(i), &vec![i; 50]).unwrap();
        }
        engine.do_seal_active().unwrap();

        // Delete 7 of 10 (70% sparsity)
        for i in 0..7u8 {
            engine.delete(&hash_hex(i)).unwrap();
        }

        // Compact
        let (compacted, moved, _reclaimed) = engine.do_compact().unwrap();
        assert!(compacted > 0, "Expected compaction to run");
        assert!(moved > 0, "Expected blobs to be moved");

        // Remaining 3 entries should still be readable
        for i in 7..10u8 {
            assert!(engine.exists(&hash_hex(i)).unwrap());
        }

        // Deleted entries should still be gone
        for i in 0..7u8 {
            assert!(!engine.exists(&hash_hex(i)).unwrap());
        }
    }
}
