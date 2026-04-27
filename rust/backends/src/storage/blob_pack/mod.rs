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

pub mod index;

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

use crate::storage::blob_pack::index::{BlobPackIndex, MemIndexEntry, ReadContentResult};

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

/// Compute dead ratio: proportion of bytes that are dead.
/// `dead_ratio = 1 - (live_bytes / total_bytes)` per Issue #3408.
/// Returns 0.0 when total is 0 (empty volume is not sparse).
fn dead_ratio(live_bytes: u64, total_bytes: u64) -> f64 {
    if total_bytes > 0 {
        1.0 - (live_bytes as f64 / total_bytes as f64)
    } else {
        0.0
    }
}

fn now_unix_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

/// Compute the total aligned size of an entry with the given data size.
/// Includes entry header (hash + size + flags) and 8-byte alignment padding.
/// Shared by put_impl and batch pre-allocation (Decision #5A: DRY).
fn compute_entry_aligned_size(data_size: u32) -> u64 {
    align_up(ENTRY_HEADER_SIZE + data_size as u64, ALIGNMENT)
}

// ─── Index entry ─────────────────────────────────────────────────────────────

/// Serialized as 32 bytes: volume_id(4) + offset(8) + size(4) + timestamp(8) + expiry(8)
/// Issue #3405: added expiry field for TTL-bucketed volumes.
#[derive(Clone, Debug)]
struct IndexEntry {
    volume_id: u32,
    offset: u64,
    size: u32,
    timestamp: f64,
    /// Unix timestamp when this entry expires. 0.0 = permanent.
    expiry: f64,
}

impl IndexEntry {
    fn to_bytes(&self) -> [u8; 32] {
        let mut buf = [0u8; 32];
        buf[0..4].copy_from_slice(&self.volume_id.to_le_bytes());
        buf[4..12].copy_from_slice(&self.offset.to_le_bytes());
        buf[12..16].copy_from_slice(&self.size.to_le_bytes());
        buf[16..24].copy_from_slice(&self.timestamp.to_le_bytes());
        buf[24..32].copy_from_slice(&self.expiry.to_le_bytes());
        buf
    }

    fn from_bytes(data: &[u8]) -> Option<Self> {
        // Accept both 24-byte (v1, no expiry) and 32-byte (v2, with expiry) entries
        if data.len() < 24 {
            return None;
        }
        let expiry = if data.len() >= 32 {
            f64::from_le_bytes(data[24..32].try_into().ok()?)
        } else {
            0.0 // v1 entries are permanent
        };
        Some(Self {
            volume_id: u32::from_le_bytes(data[0..4].try_into().ok()?),
            offset: u64::from_le_bytes(data[4..12].try_into().ok()?),
            size: u32::from_le_bytes(data[12..16].try_into().ok()?),
            timestamp: f64::from_le_bytes(data[16..24].try_into().ok()?),
            expiry,
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

// ─── Batch pre-allocation types (Issue #3409) ──────────────────────────────

/// Pre-allocated slot in a volume.
#[derive(Clone, Debug)]
struct SlotInfo {
    volume_id: u32,
    offset: u64,
    data_size: u32,
}

/// Ephemeral batch reservation — in-memory only, not persisted (Decision #1A).
/// If the process crashes, all reservations are lost and space is reclaimed
/// by deleting .tmp files or by compaction of sealed volumes.
struct BatchReservation {
    slots: Vec<SlotInfo>,
    /// Hash for each slot (set by write_slot).
    hashes: Vec<Option<[u8; 32]>>,
    /// Whether each slot has been written.
    written: Vec<bool>,
    /// Unix timestamp when this reservation expires.
    expires_at: f64,
}

/// Default reservation timeout in seconds.
const RESERVATION_TIMEOUT_SECS: f64 = 60.0;

// ─── Active volume (the one currently being written to) ─────────────────────

struct ActiveVolume {
    volume_id: u32,
    path: PathBuf,
    file: fs::File,
    write_offset: u64,
    entries: Vec<TocEntry>,
    target_size: u64,
    /// Bytes reserved by batch preallocate (not in entries/TocEntries).
    /// Prevents seal_volume from deleting volumes with batch-reserved space.
    batch_reserved_bytes: u64,
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
            batch_reserved_bytes: 0,
        })
    }

    /// Append a blob entry. Returns the offset of the written data.
    /// Uses a single write_all call to reduce syscalls (1 vs 5 per entry).
    fn append(&mut self, hash: &[u8; 32], data: &[u8]) -> io::Result<u64> {
        let offset = self.write_offset;
        let aligned_total = compute_entry_aligned_size(data.len() as u32) as usize;

        // Build entry in a single buffer: hash(32) + size(4) + flags(1) + data + padding
        let mut buf = vec![0u8; aligned_total];
        buf[0..32].copy_from_slice(hash);
        buf[32..36].copy_from_slice(&(data.len() as u32).to_le_bytes());
        buf[36] = FLAG_NONE;
        buf[37..37 + data.len()].copy_from_slice(data);
        // Padding bytes are already 0

        self.file.write_all(&buf)?;

        self.entries.push(TocEntry {
            hash: *hash,
            offset,
            size: data.len() as u32,
            flags: FLAG_NONE,
        });

        self.write_offset = offset + aligned_total as u64;
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

        // Seek to write_offset to handle gaps from batch pre-allocation (Issue #3409).
        // For non-batch writes, this is a no-op (file position is already at write_offset).
        self.file.seek(SeekFrom::Start(toc_offset))?;

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

// ─── BlobPackEngine — the main engine exposed to Python ───────────────────────

/// Thread-safe CAS volume engine with redb index.
///
/// Manages append-only volume files and a redb index mapping
/// content hashes to (volume_id, offset, size).
#[pyclass]
pub struct BlobPackEngine {
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
    /// Max bytes to process per compaction cycle (0 = unlimited).
    /// Controls how much I/O a single compact() call can do.
    compaction_bytes_per_cycle: u64,
    /// Sparsity threshold for compaction trigger (0.0 - 1.0)
    compaction_sparsity_threshold: f64,
    /// Pending index writes — batched and flushed periodically to avoid
    /// one redb write transaction (with fsync) per blob.
    pending_index: Mutex<Vec<([u8; 32], IndexEntry)>>,
    /// Max pending entries before auto-flush (default 256)
    index_batch_size: usize,
    /// In-memory index for O(1) lookups — mirrors redb, avoids disk I/O on reads.
    /// Issue #3404.
    mem_index: RwLock<BlobPackIndex>,
    /// Compaction stats counters (Issue #3408).
    compaction_volumes_total: AtomicU64,
    compaction_blobs_moved_total: AtomicU64,
    compaction_bytes_reclaimed_total: AtomicU64,
    /// Per-volume max expiry timestamp (Issue #3405).
    /// When `now > max_expiry` for a sealed volume, the entire volume can be
    /// deleted with a single `unlink()` — no per-entry scanning needed.
    /// Only populated for volumes that contain TTL entries (expiry > 0).
    volume_max_expiry: RwLock<HashMap<u32, f64>>,
    /// Ephemeral batch reservations (Issue #3409).
    /// Maps reservation_id → BatchReservation. In-memory only.
    reservations: Mutex<HashMap<u64, BatchReservation>>,
    /// Next reservation ID counter.
    next_reservation_id: AtomicU64,
    /// Cached write file descriptors for batch pwrite.
    /// Opened in preallocate(), shared via RwLock read for concurrent write_slot(),
    /// closed in commit_batch(). Uses pwrite (write_all_at) which takes &self,
    /// so multiple threads can pwrite to the same fd concurrently.
    batch_write_fds: RwLock<HashMap<u32, fs::File>>,
}

fn db_err(e: impl std::fmt::Display) -> PyErr {
    pyo3::exceptions::PyIOError::new_err(format!("Volume index error: {}", e))
}

fn io_err(e: impl std::fmt::Display) -> PyErr {
    pyo3::exceptions::PyIOError::new_err(format!("Volume I/O error: {}", e))
}

#[pymethods]
impl BlobPackEngine {
    /// Create or open a volume engine at the given directory.
    ///
    /// Args:
    ///     path: Root directory for volumes and index
    ///     target_volume_size: Override volume size in bytes (0 = dynamic)
    ///     compaction_bytes_per_cycle: Max bytes to process per compact() call (0 = unlimited)
    ///     compaction_sparsity_threshold: Trigger compaction when sparsity exceeds this (0.0-1.0)
    #[new]
    #[pyo3(signature = (path, target_volume_size=0, compaction_bytes_per_cycle=52_428_800, compaction_sparsity_threshold=0.3))]
    fn new(
        path: &str,
        target_volume_size: u64,
        compaction_bytes_per_cycle: u64,
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
            compaction_bytes_per_cycle,
            compaction_sparsity_threshold,
            pending_index: Mutex::new(Vec::with_capacity(256)),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Startup recovery (also populates in-memory index)
        engine.recover_on_startup()?;

        Ok(engine)
    }

    /// Check if a content hash exists in the index.
    fn exists(&self, hash_hex: &str) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;
        // O(1) via in-memory index (Issue #3404)
        Ok(self.mem_index.read().contains(&hash))
    }

    /// Write a blob. Returns true if it was new (not a dedup hit).
    ///
    /// Index updates are batched — entries go into a pending buffer and are
    /// flushed to redb in a single transaction every `index_batch_size` writes
    /// or at seal time. This amortizes the redb fsync cost across many blobs.
    fn put(&self, hash_hex: &str, data: &[u8]) -> PyResult<bool> {
        self.put_impl(hash_hex, data, 0.0)
    }

    /// Write a blob with an expiry timestamp (Issue #3405).
    ///
    /// Args:
    ///     hash_hex: Content hash as hex string.
    ///     data: Blob content.
    ///     expiry: Unix timestamp when this entry expires (0.0 = permanent).
    #[pyo3(signature = (hash_hex, data, expiry=0.0))]
    fn put_with_expiry(&self, hash_hex: &str, data: &[u8], expiry: f64) -> PyResult<bool> {
        self.put_impl(hash_hex, data, expiry)
    }

    /// Flush pending index entries to redb in a single transaction.
    fn flush_index(&self) -> PyResult<()> {
        self.flush_pending_index()
    }

    /// Read a blob by hash. Returns None if not found.
    ///
    /// Fast path (Issue #3404): O(1) HashMap lookup + pread from cached FD.
    /// Fallback: volume_paths + open file (for active volumes without cached FDs).
    fn get<'py>(&self, py: Python<'py>, hash_hex: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let hash = hex_to_hash(hash_hex)?;

        // Fast path: in-memory index lookup + pread from cached FD
        let idx = self.mem_index.read();
        match idx.read_content(&hash) {
            ReadContentResult::Ok(data) => Ok(Some(PyBytes::new(py, &data))),
            ReadContentResult::IoError(e) => Err(io_err(e)),
            ReadContentResult::NoFd(entry) => {
                // Entry found but no cached FD (active volume) — use volume_paths
                drop(idx);
                let vol_path = {
                    let paths = self.volume_paths.read();
                    match paths.get(&entry.volume_id) {
                        Some(p) => p.clone(),
                        None => return Ok(None),
                    }
                };
                let data = pread_blob(&vol_path, entry.offset, entry.size).map_err(io_err)?;
                Ok(Some(PyBytes::new(py, &data)))
            }
            ReadContentResult::NotFound => Ok(None),
        }
    }

    /// Read content by hash — combines lookup + pread in a single Rust call.
    ///
    /// Same implementation as `get()` but named explicitly for the Issue #3404
    /// in-memory index fast path. No Python round-trip for the lookup.
    fn read_content<'py>(
        &self,
        py: Python<'py>,
        hash_hex: &str,
    ) -> PyResult<Option<Bound<'py, PyBytes>>> {
        self.get(py, hash_hex)
    }

    /// Get blob size by hash. Returns None if not found.
    fn get_size(&self, hash_hex: &str) -> PyResult<Option<u32>> {
        let hash = hex_to_hash(hash_hex)?;
        // O(1) via in-memory index (Issue #3404)
        Ok(self.mem_index.read().lookup(&hash).map(|e| e.size))
    }

    /// Delete (tombstone) a blob by hash. Returns true if it existed.
    fn delete(&self, hash_hex: &str) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;

        // Remove from in-memory index (Issue #3404)
        let was_in_mem = self.mem_index.write().remove(&hash);

        // Remove from pending buffer if present
        let was_pending = {
            let mut pending = self.pending_index.lock();
            let before = pending.len();
            pending.retain(|(h, _)| h != &hash);
            pending.len() < before
        };

        // Remove from committed index
        let was_committed = {
            let db = self.db.read();
            let txn = db.begin_write().map_err(db_err)?;
            let existed;
            {
                let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                existed = table.remove(hash.as_slice()).map_err(db_err)?.is_some();
            }
            txn.commit().map_err(db_err)?;
            existed
        };

        Ok(was_in_mem || was_pending || was_committed)
    }

    /// Batch read multiple blobs. Returns dict of hash_hex → bytes (missing hashes omitted).
    fn batch_get<'py>(
        &self,
        py: Python<'py>,
        hash_hexes: Vec<String>,
    ) -> PyResult<HashMap<String, Bound<'py, PyBytes>>> {
        let mut result = HashMap::with_capacity(hash_hexes.len());

        // Batch lookup from in-memory index — O(1) per hash (Issue #3404)
        let mut lookups: Vec<(String, MemIndexEntry)> = Vec::with_capacity(hash_hexes.len());
        {
            let idx = self.mem_index.read();
            for hex in &hash_hexes {
                if let Ok(hash) = hex_to_hash(hex) {
                    if let Some(entry) = idx.lookup(&hash) {
                        lookups.push((hex.clone(), entry));
                    }
                }
            }
        }

        // Group reads by volume for I/O locality
        let mut by_volume: HashMap<u32, Vec<(String, u64, u32)>> = HashMap::new();
        for (hex, entry) in &lookups {
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
        let mut seen = std::collections::HashSet::new();

        // Include pending entries
        {
            let pending = self.pending_index.lock();
            for (hash, entry) in pending.iter() {
                let h = hex::encode(hash);
                seen.insert(h.clone());
                result.push((h, entry.timestamp));
            }
        }

        // Include committed entries (skip those already in pending)
        let iter = table.iter().map_err(db_err)?;
        for item in iter {
            let (key, val) = item.map_err(db_err)?;
            let hash_hex = hex::encode(key.value());
            if !seen.contains(&hash_hex) {
                if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                    result.push((hash_hex, entry.timestamp));
                }
            }
        }

        Ok(result)
    }

    /// Get the write timestamp for a specific hash. Returns None if not found.
    fn get_timestamp(&self, hash_hex: &str) -> PyResult<Option<f64>> {
        let hash = hex_to_hash(hash_hex)?;
        Ok(self.lookup_entry(&hash)?.map(|e| e.timestamp))
    }

    /// Get total number of indexed blobs (committed + pending).
    fn len(&self) -> PyResult<u64> {
        let pending_count = self.pending_index.lock().len() as u64;
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        Ok(table.len().map_err(db_err)? + pending_count)
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
    ///
    /// Releases the GIL during I/O-heavy compaction work (Issue #3408).
    fn compact(&self, py: Python<'_>) -> PyResult<(u32, u64, u64)> {
        py.detach(|| self.do_compact())
    }

    /// Get volume stats: {volume_count, total_blobs, total_bytes, active_volume_size}.
    fn stats(&self) -> PyResult<HashMap<String, u64>> {
        let mut stats = HashMap::new();
        let paths = self.volume_paths.read();
        stats.insert("sealed_volume_count".to_string(), paths.len() as u64);

        let pending_count = self.pending_index.lock().len() as u64;
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        stats.insert(
            "total_blobs".to_string(),
            table.len().map_err(db_err)? + pending_count,
        );
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

        // In-memory index stats (Issue #3404)
        let idx = self.mem_index.read();
        stats.insert("mem_index_entries".to_string(), idx.len() as u64);
        stats.insert("mem_index_bytes".to_string(), idx.memory_bytes() as u64);
        stats.insert("mem_index_volumes".to_string(), idx.volume_count() as u64);
        drop(idx);

        // Compaction stats (Issue #3408)
        stats.insert(
            "compaction_volumes_total".to_string(),
            self.compaction_volumes_total.load(Ordering::Relaxed),
        );
        stats.insert(
            "compaction_blobs_moved_total".to_string(),
            self.compaction_blobs_moved_total.load(Ordering::Relaxed),
        );
        stats.insert(
            "compaction_bytes_reclaimed_total".to_string(),
            self.compaction_bytes_reclaimed_total
                .load(Ordering::Relaxed),
        );

        Ok(stats)
    }

    /// Memory used by the in-memory volume index (bytes). Issue #3404.
    fn index_memory_bytes(&self) -> usize {
        self.mem_index.read().memory_bytes()
    }

    /// Close the engine: seal active volume, save snapshot, close database.
    fn close(&self) -> PyResult<()> {
        if !self.is_open.swap(false, Ordering::SeqCst) {
            return Ok(());
        }
        // Flush pending index entries, then seal active volume
        let _ = self.flush_pending_index();
        let _ = self.do_seal_active();
        // Save snapshot for fast startup next time
        let _ = self.mem_index.read().save_snapshot(&self.snapshot_path());
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

                    // Batch index write via pending_index (Decision #8A).
                    // Replaces per-file redb transactions — flushed at batch boundaries
                    // by do_seal_active() which calls flush_pending_index().
                    let entry = IndexEntry {
                        volume_id,
                        offset,
                        size: data.len() as u32,
                        timestamp: now_unix_secs(),
                        expiry: 0.0, // Migrated content is permanent
                    };
                    {
                        let mut pending = self.pending_index.lock();
                        pending.push((hash, entry));
                    }
                    // Update mem_index for O(1) reads during migration
                    self.mem_index.write().insert(
                        hash,
                        MemIndexEntry {
                            volume_id,
                            offset,
                            size: data.len() as u32,
                            expiry: 0.0,
                        },
                    );

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

    /// Expire entire sealed TTL volumes whose max_expiry has passed (Issue #3405).
    ///
    /// Volume-level expiry: iterates volumes (not entries), checks per-volume
    /// max_expiry, and deletes the entire volume with a single `unlink()`.
    /// All entries for that volume are bulk-removed from mem_index.
    /// No per-file GC scanning needed.
    ///
    /// Returns list of (volume_id, entries_removed) tuples.
    fn expire_ttl_volumes(&self) -> PyResult<Vec<(u32, usize)>> {
        let now = now_unix_secs();
        let mut result: Vec<(u32, usize)> = Vec::new();

        // Phase 1: Identify expired volumes by max_expiry (O(volumes), not O(entries))
        let expired_volume_ids: Vec<u32> = {
            let max_exp = self.volume_max_expiry.read();
            max_exp
                .iter()
                .filter(|(_, &max_exp)| max_exp > 0.0 && now > max_exp)
                .map(|(&vol_id, _)| vol_id)
                .collect()
        };

        if expired_volume_ids.is_empty() {
            return Ok(result);
        }

        // Phase 2: For each expired volume — bulk-remove entries, close FD, unlink file
        for vol_id in &expired_volume_ids {
            // Bulk-remove all entries for this volume from mem_index
            let entries_removed = self.mem_index.write().remove_by_volume(*vol_id);

            // Close cached file descriptor
            self.mem_index.write().close_volume(*vol_id);

            // Delete the volume file (single unlink — the core promise of Issue #3405)
            if let Some(path) = self.volume_paths.read().get(vol_id) {
                let _ = fs::remove_file(path);
            }
            self.volume_paths.write().remove(vol_id);

            // Remove from max_expiry tracker
            self.volume_max_expiry.write().remove(vol_id);

            // Remove entries for this volume from pending buffer
            {
                let mut pending = self.pending_index.lock();
                pending.retain(|(_, entry)| entry.volume_id != *vol_id);
            }

            result.push((*vol_id, entries_removed));
        }

        Ok(result)
    }

    /// Flush expired entries from the redb persistent index.
    ///
    /// Scans redb for entries whose volume_id no longer exists in volume_paths
    /// (already deleted by expire_ttl_volumes). This is the deferred cleanup
    /// step — readers already see expired entries as gone via mem_index.
    ///
    /// Safe to skip on shutdown — startup recovery handles orphaned redb entries.
    fn flush_expired_index(&self) -> PyResult<usize> {
        let mut removed = 0usize;
        let volume_paths = self.volume_paths.read().clone();

        let db = self.db.read();
        let read_txn = db.begin_read().map_err(db_err)?;
        let table = read_txn.open_table(INDEX_TABLE).map_err(db_err)?;

        // Collect keys pointing to deleted volumes (already unlinked by expire_ttl_volumes)
        let mut orphaned_keys: Vec<Vec<u8>> = Vec::new();
        for item in table.iter().map_err(db_err)? {
            let (key, val) = item.map_err(db_err)?;
            if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                if !volume_paths.contains_key(&entry.volume_id) {
                    orphaned_keys.push(key.value().to_vec());
                }
            }
        }
        drop(table);
        drop(read_txn);

        if orphaned_keys.is_empty() {
            return Ok(0);
        }

        // Batch delete in a single write transaction
        let write_txn = db.begin_write().map_err(db_err)?;
        {
            let mut table = write_txn.open_table(INDEX_TABLE).map_err(db_err)?;
            for key in &orphaned_keys {
                if table.remove(key.as_slice()).map_err(db_err)?.is_some() {
                    removed += 1;
                }
            }
        }
        write_txn.commit().map_err(db_err)?;

        Ok(removed)
    }

    /// Seal the active volume only if it has entries (Issue #3405).
    ///
    /// Used by TTL rotation timer: seal at time intervals, but skip if empty.
    fn seal_if_nonempty(&self) -> PyResult<bool> {
        let has_entries = {
            let active = self.active.lock();
            active.as_ref().is_some_and(|v| v.entry_count() > 0)
        };
        if has_entries {
            self.do_seal_active()?;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    // ─── Batch pre-allocation (Issue #3409) ─────────────────────────────────

    /// Filter a list of hashes, returning only those NOT already in the index.
    /// Used for dedup before preallocate (Decision #14A: pre-filter + commit check).
    fn filter_known(&self, hash_hexes: Vec<String>) -> PyResult<Vec<String>> {
        let idx = self.mem_index.read();
        let mut unknown = Vec::with_capacity(hash_hexes.len());
        for hex in hash_hexes {
            if let Ok(hash) = hex_to_hash(&hex) {
                if idx.lookup_raw(&hash).is_none() {
                    unknown.push(hex);
                }
            }
        }
        Ok(unknown)
    }

    /// Reserve N slots for batch writes (Issue #3409).
    ///
    /// Computes aligned offsets for each entry in a single Mutex hold (Decision #3A),
    /// then returns a reservation_id. Callers write data via `write_slot()`
    /// and finalize via `commit_batch()`.
    ///
    /// Auto-splits across volumes if the batch exceeds capacity (Decision #7A).
    /// Reservations are ephemeral — in-memory only (Decision #1A).
    ///
    /// Args:
    ///     sizes: List of data sizes (in bytes) for each entry.
    ///
    /// Returns:
    ///     Reservation ID (u64) for use with write_slot() and commit_batch().
    fn preallocate(&self, sizes: Vec<u32>) -> PyResult<u64> {
        if sizes.is_empty() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Cannot preallocate zero slots",
            ));
        }

        let mut slots = Vec::with_capacity(sizes.len());
        let mut active_guard = self.active.lock();

        for &size in &sizes {
            let aligned_total = compute_entry_aligned_size(size);

            // Ensure active volume exists
            if active_guard.is_none() {
                let vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
                let target = self.get_target_volume_size();
                let vol = ActiveVolume::new(&self.volumes_dir, vol_id, target).map_err(io_err)?;
                self.volume_paths.write().insert(vol_id, vol.path.clone());
                *active_guard = Some(vol);
            }

            // Check if current volume has room (seal + create new if needed)
            let needs_new_volume = {
                let vol = active_guard.as_ref().unwrap();
                vol.write_offset + aligned_total > vol.target_size && vol.write_offset > HEADER_SIZE
            };

            if needs_new_volume {
                // Flush pending so seal can filter correctly
                self.flush_pending_index()?;
                let old_vol = active_guard.take().unwrap();
                self.seal_volume(old_vol)?;

                let vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
                let target = self.get_target_volume_size();
                let new_vol =
                    ActiveVolume::new(&self.volumes_dir, vol_id, target).map_err(io_err)?;
                self.volume_paths
                    .write()
                    .insert(vol_id, new_vol.path.clone());
                *active_guard = Some(new_vol);
            }

            let vol = active_guard.as_mut().unwrap();
            let offset = vol.write_offset;
            vol.write_offset += aligned_total;
            vol.batch_reserved_bytes += aligned_total;

            slots.push(SlotInfo {
                volume_id: vol.volume_id,
                offset,
                data_size: size,
            });
        }

        // Extend the active volume file to cover reserved space (Decision #2A)
        // and seek the file cursor to write_offset so that subsequent sequential
        // append() calls (from put()) land after the reserved space.
        if let Some(vol) = active_guard.as_mut() {
            let current_len = vol.file.metadata().map_err(io_err)?.len();
            if vol.write_offset > current_len {
                vol.file.set_len(vol.write_offset).map_err(io_err)?;
            }
            vol.file
                .seek(SeekFrom::Start(vol.write_offset))
                .map_err(io_err)?;
        }

        drop(active_guard);

        // Open and cache write FDs for each volume used by the batch.
        // Uses RwLock so write_slot can share the fd via read lock for pwrite.
        {
            let mut fds = self.batch_write_fds.write();
            let paths = self.volume_paths.read();
            for slot in &slots {
                if let std::collections::hash_map::Entry::Vacant(e) = fds.entry(slot.volume_id) {
                    if let Some(path) = paths.get(&slot.volume_id) {
                        if let Ok(f) = fs::OpenOptions::new().write(true).open(path) {
                            e.insert(f);
                        }
                    }
                }
            }
        }

        // Store ephemeral reservation (Decision #1A)
        let count = slots.len();
        let res_id = self.next_reservation_id.fetch_add(1, Ordering::Relaxed);
        {
            let mut reservations = self.reservations.lock();
            reservations.insert(
                res_id,
                BatchReservation {
                    slots,
                    hashes: vec![None; count],
                    written: vec![false; count],
                    expires_at: now_unix_secs() + RESERVATION_TIMEOUT_SECS,
                },
            );
        }

        Ok(res_id)
    }

    /// Write data to a pre-allocated slot (Issue #3409).
    ///
    /// Uses pwrite at the pre-assigned offset — no lock contention with other
    /// write_slot calls or with put(). GIL is released during the I/O (Decision #16A).
    ///
    /// Args:
    ///     reservation_id: ID from preallocate().
    ///     slot_index: 0-based index into the reservation's slot list.
    ///     hash_hex: Content hash as hex string.
    ///     data: Blob content (must match the size passed to preallocate).
    fn write_slot(
        &self,
        py: Python<'_>,
        reservation_id: u64,
        slot_index: usize,
        hash_hex: &str,
        data: &[u8],
    ) -> PyResult<()> {
        let hash = hex_to_hash(hash_hex)?;

        // Validate and mark slot as written (brief lock)
        let (volume_id, offset) = {
            let mut reservations = self.reservations.lock();
            let res = reservations.get_mut(&reservation_id).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("Invalid or expired reservation ID")
            })?;

            if now_unix_secs() >= res.expires_at {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "Reservation has expired",
                ));
            }
            if slot_index >= res.slots.len() {
                return Err(pyo3::exceptions::PyIndexError::new_err(
                    "Slot index out of range",
                ));
            }
            if res.written[slot_index] {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "Slot already written",
                ));
            }
            let slot = &res.slots[slot_index];
            if data.len() as u32 != slot.data_size {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Data size {} doesn't match reserved size {}",
                    data.len(),
                    slot.data_size
                )));
            }
            res.hashes[slot_index] = Some(hash);
            res.written[slot_index] = true;
            (slot.volume_id, slot.offset)
        };

        // Build entry: hash(32) + size(4) + flags(1) + data + padding
        let aligned_total = compute_entry_aligned_size(data.len() as u32) as usize;
        let mut buf = vec![0u8; aligned_total];
        buf[0..32].copy_from_slice(&hash);
        buf[32..36].copy_from_slice(&(data.len() as u32).to_le_bytes());
        buf[36] = FLAG_NONE;
        buf[37..37 + data.len()].copy_from_slice(data);

        // pwrite using cached write FD (shared read lock — no contention).
        // write_all_at takes &self so multiple threads can pwrite concurrently.
        // GIL released during I/O (Decision #16A).
        py.detach(|| {
            let fds = self.batch_write_fds.read();
            let file = fds.get(&volume_id).ok_or_else(|| {
                pyo3::exceptions::PyIOError::new_err(format!("Volume {} not found", volume_id))
            })?;

            #[cfg(unix)]
            {
                use std::os::unix::fs::FileExt;
                file.write_all_at(&buf, offset).map_err(io_err)?;
            }
            #[cfg(not(unix))]
            {
                // Fallback: open a new fd for non-unix (no write_all_at)
                let _ = file; // used on unix; silence warning on windows
                drop(fds);
                let vol_path = {
                    let paths = self.volume_paths.read();
                    paths.get(&volume_id).cloned().ok_or_else(|| {
                        pyo3::exceptions::PyIOError::new_err(format!(
                            "Volume {} not found",
                            volume_id
                        ))
                    })?
                };
                let mut f = fs::OpenOptions::new()
                    .write(true)
                    .open(&vol_path)
                    .map_err(io_err)?;
                f.seek(SeekFrom::Start(offset)).map_err(io_err)?;
                f.write_all(&buf).map_err(io_err)?;
            }

            Ok::<_, PyErr>(())
        })?;

        Ok(())
    }

    /// Finalize a batch: fsync, update index in one transaction, update mem_index.
    ///
    /// Two-phase visibility (Decision #4A): entries become readable only after
    /// this method completes. GIL is released during I/O (Decision #16A).
    ///
    /// Args:
    ///     reservation_id: ID from preallocate().
    ///     expiry: Optional expiry timestamp for all entries (0.0 = permanent).
    #[pyo3(signature = (reservation_id, expiry=0.0))]
    fn commit_batch(&self, py: Python<'_>, reservation_id: u64, expiry: f64) -> PyResult<()> {
        // Extract and remove reservation
        let reservation = {
            let mut reservations = self.reservations.lock();
            reservations.remove(&reservation_id).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("Invalid or expired reservation ID")
            })?
        };

        // Verify all slots were written
        for (i, written) in reservation.written.iter().enumerate() {
            if !*written {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Slot {} was not written",
                    i
                )));
            }
        }

        // Build index entries
        let now = now_unix_secs();
        let mut index_entries: Vec<([u8; 32], IndexEntry)> =
            Vec::with_capacity(reservation.slots.len());
        let mut mem_entries: Vec<([u8; 32], MemIndexEntry)> =
            Vec::with_capacity(reservation.slots.len());
        let mut total_new_bytes: u64 = 0;
        let mut volume_id_set: std::collections::HashSet<u32> = std::collections::HashSet::new();
        // TocEntry data for active volume update (fixes Codex bugs #1 and #2)
        let mut toc_updates: Vec<([u8; 32], u32, u32, u64)> =
            Vec::with_capacity(reservation.slots.len());

        for (i, slot) in reservation.slots.iter().enumerate() {
            let hash = reservation.hashes[i].unwrap(); // verified all written above

            // Commit-time dedup check (Decision #14A: defense against TOCTOU races)
            if self.mem_index.read().lookup_raw(&hash).is_some() {
                continue; // already indexed — skip (CAS idempotency)
            }

            index_entries.push((
                hash,
                IndexEntry {
                    volume_id: slot.volume_id,
                    offset: slot.offset,
                    size: slot.data_size,
                    timestamp: now,
                    expiry,
                },
            ));
            mem_entries.push((
                hash,
                MemIndexEntry {
                    volume_id: slot.volume_id,
                    offset: slot.offset,
                    size: slot.data_size,
                    expiry,
                },
            ));
            total_new_bytes += slot.data_size as u64;
            volume_id_set.insert(slot.volume_id);
            toc_updates.push((hash, slot.data_size, slot.volume_id, slot.offset));
        }

        if index_entries.is_empty() {
            return Ok(()); // all entries were duplicates
        }

        let volume_ids: Vec<u32> = volume_id_set.into_iter().collect();

        // Heavy I/O with GIL released (Decision #16A)
        py.detach(move || -> PyResult<()> {
            // fsync each volume using cached write FDs (Decision #15A: data before metadata)
            {
                let fds = self.batch_write_fds.read();
                for vol_id in &volume_ids {
                    if let Some(file) = fds.get(vol_id) {
                        let _ = file.sync_data();
                    }
                }
            }

            // Push to pending_index and flush in single transaction
            {
                let mut pending = self.pending_index.lock();
                pending.extend(index_entries);
            }
            self.flush_pending_index()?;

            // Update mem_index in bulk (Decision #4A: two-phase visibility)
            {
                let mut idx = self.mem_index.write();
                for (hash, entry) in mem_entries {
                    idx.insert(hash, entry);
                }
            }

            // Track per-volume max expiry (Issue #3405)
            if expiry > 0.0 {
                let mut max_exp = self.volume_max_expiry.write();
                for vol_id in &volume_ids {
                    let current = max_exp.entry(*vol_id).or_insert(0.0);
                    if expiry > *current {
                        *current = expiry;
                    }
                }
            }

            self.total_bytes
                .fetch_add(total_new_bytes, Ordering::Relaxed);

            // Add TocEntries to active volume for committed batch slots.
            // This fixes two bugs:
            // 1. close()/Drop checks entry_count() > 0 to decide seal vs delete.
            //    Without TocEntries, batch-only volumes are deleted → data loss.
            // 2. seal() writes TOC records from vol.entries only. Without them,
            //    batch entries are absent from sealed .vol TOC → breaks crash
            //    recovery and parse_volume_toc() (used by tiering).
            //
            // For sealed volumes (from multi-volume spanning during preallocate),
            // the TOC is already finalized. Those entries are preserved via redb;
            // TOC reconciliation on crash recovery fills in any gaps.
            {
                let mut active_guard = self.active.lock();
                if let Some(vol) = active_guard.as_mut() {
                    for &(hash, size, volume_id, offset) in &toc_updates {
                        if volume_id == vol.volume_id {
                            vol.entries.push(TocEntry {
                                hash,
                                offset,
                                size,
                                flags: FLAG_NONE,
                            });
                            let aligned = compute_entry_aligned_size(size);
                            if vol.batch_reserved_bytes >= aligned {
                                vol.batch_reserved_bytes -= aligned;
                            }
                        }
                    }
                }
            }

            // Clean up cached write FDs only if no other reservation uses them
            {
                let still_in_use: std::collections::HashSet<u32> = {
                    let reservations = self.reservations.lock();
                    reservations
                        .values()
                        .flat_map(|r| r.slots.iter().map(|s| s.volume_id))
                        .collect()
                };
                let mut fds = self.batch_write_fds.write();
                for vol_id in &volume_ids {
                    if !still_in_use.contains(vol_id) {
                        fds.remove(vol_id);
                    }
                }
            }

            Ok(())
        })?;

        Ok(())
    }

    /// Batch write: all data in a single Python→Rust call (Issue #3409).
    ///
    /// Optimized bulk import path that eliminates per-entry Python overhead:
    /// - Single GIL release for all entries (not 10K detach/reattach cycles)
    /// - Single index flush at the end (not one every 256 entries)
    /// - Dedup check per entry via mem_index
    ///
    /// This is the recommended path for connector sync and migration.
    /// The 3-step API (preallocate/write_slot/commit_batch) is available
    /// for cases needing fine-grained control.
    ///
    /// Args:
    ///     items: List of (hash_hex, data) tuples.
    ///
    /// Returns:
    ///     Number of new blobs written (excludes duplicates).
    fn batch_put(&self, py: Python<'_>, items: Vec<(String, Vec<u8>)>) -> PyResult<usize> {
        if items.is_empty() {
            return Ok(0);
        }

        // Parse hashes while GIL is held (hex_to_hash needs &str from Python)
        let mut parsed: Vec<([u8; 32], Vec<u8>)> = Vec::with_capacity(items.len());
        for (hash_hex, data) in items {
            let hash = hex_to_hash(&hash_hex)?;
            parsed.push((hash, data));
        }

        // All I/O with GIL released (Decision #16A)
        py.detach(move || -> PyResult<usize> {
            let now = now_unix_secs();
            // (hash, size, volume_id, offset)
            let mut new_entries: Vec<([u8; 32], u32, u32, u64)> = Vec::with_capacity(parsed.len());
            let mut total_new_bytes: u64 = 0;

            // Phase 1: Bulk dedup check (single RwLock read for all entries)
            let known: std::collections::HashSet<[u8; 32]> = {
                let idx = self.mem_index.read();
                parsed
                    .iter()
                    .filter(|(h, _)| idx.lookup_raw(h).is_some())
                    .map(|(h, _)| *h)
                    .collect()
            };

            // Phase 2: Append unknown entries to volumes (active Mutex per entry)
            for (hash, data) in &parsed {
                if known.contains(hash) {
                    continue;
                }

                let (volume_id, offset) = self.append_to_active(hash, data)?;
                new_entries.push((*hash, data.len() as u32, volume_id, offset));

                // Collect for batch pending_index push
                let entry = IndexEntry {
                    volume_id,
                    offset,
                    size: data.len() as u32,
                    timestamp: now,
                    expiry: 0.0,
                };
                {
                    let mut pending = self.pending_index.lock();
                    pending.push((*hash, entry));
                }

                total_new_bytes += data.len() as u64;
            }

            // Phase 3: Single flush for all entries
            self.flush_pending_index()?;

            // Phase 4: Bulk mem_index update (single RwLock write for all entries)
            {
                let mut idx = self.mem_index.write();
                for &(hash, size, volume_id, offset) in &new_entries {
                    idx.insert(
                        hash,
                        MemIndexEntry {
                            volume_id,
                            offset,
                            size,
                            expiry: 0.0,
                        },
                    );
                }
            }

            self.total_bytes
                .fetch_add(total_new_bytes, Ordering::Relaxed);

            Ok(new_entries.len())
        })
    }

    /// Remove expired reservations. Returns the count removed.
    /// Called periodically by the Python transport for cleanup.
    fn expire_reservations(&self) -> usize {
        let now = now_unix_secs();
        let mut reservations = self.reservations.lock();
        let before = reservations.len();
        reservations.retain(|_, res| now < res.expires_at);
        before - reservations.len()
    }
}

// ─── Internal methods (not exposed to Python) ───────────────────────────────

impl BlobPackEngine {
    /// Core put implementation shared by `put()` and `put_with_expiry()`.
    fn put_impl(&self, hash_hex: &str, data: &[u8], expiry: f64) -> PyResult<bool> {
        let hash = hex_to_hash(hash_hex)?;

        // Dedup check: O(1) via in-memory index (Issue #3404)
        // Use lookup_raw to bypass expiry check — we want to dedup even against expired entries
        // that haven't been swept yet (content is still physically present).
        if self.mem_index.read().lookup_raw(&hash).is_some() {
            return Ok(false);
        }

        // Append to active volume
        let (volume_id, offset) = self.append_to_active(&hash, data)?;

        // Buffer index entry (not committed to redb yet)
        let entry = IndexEntry {
            volume_id,
            offset,
            size: data.len() as u32,
            timestamp: now_unix_secs(),
            expiry,
        };

        let should_flush = {
            let mut pending = self.pending_index.lock();
            pending.push((hash, entry));
            pending.len() >= self.index_batch_size
        };

        // Update in-memory index for O(1) reads (Issue #3404)
        self.mem_index.write().insert(
            hash,
            MemIndexEntry {
                volume_id,
                offset,
                size: data.len() as u32,
                expiry,
            },
        );

        // Track per-volume max expiry for volume-level TTL (Issue #3405)
        if expiry > 0.0 {
            let mut max_exp = self.volume_max_expiry.write();
            let current = max_exp.entry(volume_id).or_insert(0.0);
            if expiry > *current {
                *current = expiry;
            }
        }

        // Flush when buffer is full
        if should_flush {
            self.flush_pending_index()?;
        }

        self.total_bytes
            .fetch_add(data.len() as u64, Ordering::Relaxed);

        Ok(true)
    }

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

    /// Lookup an entry from pending buffer or committed index.
    fn lookup_entry(&self, hash: &[u8; 32]) -> PyResult<Option<IndexEntry>> {
        // Check pending buffer first
        {
            let pending = self.pending_index.lock();
            for (h, entry) in pending.iter().rev() {
                if h == hash {
                    return Ok(Some(entry.clone()));
                }
            }
        }

        // Check committed index
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        match table.get(hash.as_slice()).map_err(db_err)? {
            Some(val) => Ok(IndexEntry::from_bytes(val.value())),
            None => Ok(None),
        }
    }

    /// Flush all pending index entries to redb in a single write transaction.
    fn flush_pending_index(&self) -> PyResult<()> {
        let entries: Vec<([u8; 32], IndexEntry)> = {
            let mut pending = self.pending_index.lock();
            if pending.is_empty() {
                return Ok(());
            }
            std::mem::take(&mut *pending)
        };

        let db = self.db.read();
        let txn = db.begin_write().map_err(db_err)?;
        {
            let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
            for (hash, entry) in &entries {
                table
                    .insert(hash.as_slice(), entry.to_bytes().as_slice())
                    .map_err(db_err)?;
            }
        }
        txn.commit().map_err(db_err)?;
        Ok(())
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
                // Flush pending index so seal_volume's cross-reference
                // against the index is accurate.
                self.flush_pending_index()?;
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
        if vol.entry_count() == 0 && vol.write_offset <= HEADER_SIZE {
            // Truly empty (no data, no batch reservations) — delete
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

        if vol.entries.is_empty() && vol.batch_reserved_bytes == 0 {
            // All entries were deleted and no batch reservations — discard
            let _ = fs::remove_file(&vol.path);
            self.volume_paths.write().remove(&vol.volume_id);
            return Ok(());
        }

        let vol_id = vol.volume_id;
        let (sealed_path, _entries) = vol.seal(&self.volumes_dir).map_err(io_err)?;

        // Cache FD for pread access (Issue #3404).
        //
        // `seal_volume` can be reached while `put_impl` is holding a
        // mem_index write lock during the dedup+append window.
        // Re-acquiring that same non-reentrant lock here deadlocks on
        // volume-rollover paths (develop commit abdbfb2e7 reentrant-lock
        // fix). Best-effort: cache the FD only when we can grab the lock
        // immediately; otherwise skip — the next read miss will lazily
        // open the FD.
        if let Some(mut idx) = self.mem_index.try_write() {
            if let Err(e) = idx.open_volume(vol_id, &sealed_path) {
                eprintln!(
                    "Warning: failed to cache volume FD for {}: {}",
                    sealed_path.display(),
                    e
                );
            }
        }

        // Register sealed volume path (replaces the .tmp entry)
        self.volume_paths.write().insert(vol_id, sealed_path);

        Ok(())
    }

    fn do_seal_active(&self) -> PyResult<bool> {
        // Flush pending index entries before sealing so seal_volume's
        // cross-reference check against the index is accurate.
        self.flush_pending_index()?;

        let mut active_guard = self.active.lock();
        if let Some(vol) = active_guard.take() {
            if vol.entry_count() > 0 {
                self.seal_volume(vol)?;
                return Ok(true);
            } else {
                let _ = fs::remove_file(&vol.path);
            }
        }
        Ok(false)
    }

    /// Startup recovery: delete .tmp files, scan .vol files to rebuild state.
    /// Path to the snapshot sidecar file.
    fn snapshot_path(&self) -> PathBuf {
        self.volumes_dir.join("mem_index.bin")
    }

    fn recover_on_startup(&mut self) -> PyResult<()> {
        let entries = fs::read_dir(&self.volumes_dir).map_err(io_err)?;

        let mut max_vol_id: u32 = 0;
        let mut total_bytes: u64 = 0;
        let mut volume_paths: HashMap<u32, PathBuf> = HashMap::new();
        let mut had_tmp_files = false;

        // ── Phase 1: Scan directory — discover volume paths, delete .tmp ──
        // NOTE: We do NOT read TOCs here — defer until we know if reconciliation
        // is needed (snapshot fast path skips TOC reading entirely).
        for entry in entries {
            let entry = entry.map_err(io_err)?;
            let path = entry.path();
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();

            if name.ends_with(".tmp") {
                let _ = fs::remove_file(&path);
                had_tmp_files = true;
                continue;
            }

            if name.ends_with(".vol") {
                // Parse volume_id from filename (vol_XXXXXXXX.vol)
                if let Some(hex) = name
                    .strip_prefix("vol_")
                    .and_then(|s| s.strip_suffix(".vol"))
                {
                    if let Ok(vol_id) = u32::from_str_radix(hex, 16) {
                        max_vol_id = max_vol_id.max(vol_id);
                        volume_paths.insert(vol_id, path);
                    }
                }
            }
        }

        // ── Phase 2: Try snapshot — skip redb + TOC reading on clean startup ──
        let snapshot_path = self.snapshot_path();

        let (mut idx, need_reconciliation) = if !had_tmp_files {
            if let Some(snap_idx) = BlobPackIndex::load_snapshot(&snapshot_path) {
                let redb_count = {
                    let db = self.db.read();
                    let txn = db.begin_read().map_err(db_err)?;
                    let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                    table.len().map_err(db_err)? as usize
                };
                if snap_idx.len() == redb_count && snap_idx.all_volumes_exist(&volume_paths) {
                    (snap_idx, false)
                } else {
                    (Self::load_index_from_redb(self, &volume_paths)?, true)
                }
            } else {
                (Self::load_index_from_redb(self, &volume_paths)?, true)
            }
        } else {
            let _ = fs::remove_file(&snapshot_path);
            (Self::load_index_from_redb(self, &volume_paths)?, true)
        };

        // ── Phase 3: Reconcile (only if snapshot was not used) ──
        // Read TOCs and reconcile only when needed — this is the slow path.
        if need_reconciliation {
            let mut indexed_hashes: std::collections::HashSet<Vec<u8>> = {
                let db = self.db.read();
                let txn = db.begin_read().map_err(db_err)?;
                let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                let mut set =
                    std::collections::HashSet::with_capacity(table.len().map_err(db_err)? as usize);
                for item in table.iter().map_err(db_err)? {
                    let (key, _) = item.map_err(db_err)?;
                    set.insert(key.value().to_vec());
                }
                set
            };

            let now = now_unix_secs();
            let db = self.db.read();
            let txn = db.begin_write().map_err(db_err)?;
            {
                let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                for (vol_id, path) in &volume_paths {
                    match read_volume_toc(path) {
                        Ok((_, toc_entries)) => {
                            for toc_entry in &toc_entries {
                                if toc_entry.flags & FLAG_TOMBSTONE != 0 {
                                    continue;
                                }
                                total_bytes += toc_entry.size as u64;
                                if !indexed_hashes.contains(toc_entry.hash.as_slice()) {
                                    let idx_entry = IndexEntry {
                                        volume_id: *vol_id,
                                        offset: toc_entry.offset,
                                        size: toc_entry.size,
                                        timestamp: now,
                                        expiry: 0.0, // TOC rebuild: no expiry info, assume permanent
                                    };
                                    table
                                        .insert(
                                            toc_entry.hash.as_slice(),
                                            idx_entry.to_bytes().as_slice(),
                                        )
                                        .map_err(db_err)?;
                                    idx.insert(
                                        toc_entry.hash,
                                        MemIndexEntry {
                                            volume_id: *vol_id,
                                            offset: toc_entry.offset,
                                            size: toc_entry.size,
                                            expiry: 0.0,
                                        },
                                    );
                                    indexed_hashes.insert(toc_entry.hash.to_vec());
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!(
                                "Warning: skipping corrupted volume {}: {}",
                                path.display(),
                                e
                            );
                        }
                    }
                }
            }
            txn.commit().map_err(db_err)?;

            // Save snapshot for next startup
            let _ = idx.save_snapshot(&snapshot_path);
        } else {
            // Snapshot path — compute total_bytes from mem_index
            total_bytes = idx.total_content_bytes();
        }

        // ── Phase 4: Open FDs, set state ──
        for (vol_id, path) in &volume_paths {
            if path.extension().is_some_and(|ext| ext == "vol") {
                if let Err(e) = idx.open_volume(*vol_id, path) {
                    eprintln!(
                        "Warning: failed to open volume FD for {}: {}",
                        path.display(),
                        e
                    );
                }
            }
        }

        self.next_volume_id.store(max_vol_id + 1, Ordering::Relaxed);
        self.total_bytes.store(total_bytes, Ordering::Relaxed);
        *self.volume_paths.write() = volume_paths;
        *self.mem_index.write() = idx;

        Ok(())
    }

    /// Load the mem_index from redb in a single pass (slow path).
    /// Also detects and removes stale entries pointing to missing volumes.
    fn load_index_from_redb(
        &self,
        volume_paths: &HashMap<u32, PathBuf>,
    ) -> PyResult<BlobPackIndex> {
        let db = self.db.read();
        let txn = db.begin_read().map_err(db_err)?;
        let table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
        let count = table.len().map_err(db_err)? as usize;
        let mut idx = BlobPackIndex::with_capacity(count);
        let mut stale_keys: Vec<Vec<u8>> = Vec::new();
        let mut max_expiry_map: HashMap<u32, f64> = HashMap::new();

        for item in table.iter().map_err(db_err)? {
            let (key, val) = item.map_err(db_err)?;
            if let Some(entry) = IndexEntry::from_bytes(val.value()) {
                if volume_paths.contains_key(&entry.volume_id) {
                    let mut hash = [0u8; 32];
                    hash.copy_from_slice(key.value());
                    idx.insert(
                        hash,
                        MemIndexEntry {
                            volume_id: entry.volume_id,
                            offset: entry.offset,
                            size: entry.size,
                            expiry: entry.expiry,
                        },
                    );
                    // Rebuild volume_max_expiry from persisted entries (Issue #3405)
                    if entry.expiry > 0.0 {
                        let current = max_expiry_map.entry(entry.volume_id).or_insert(0.0);
                        if entry.expiry > *current {
                            *current = entry.expiry;
                        }
                    }
                } else {
                    stale_keys.push(key.value().to_vec());
                }
            }
        }
        drop(table);
        drop(txn);

        // Remove stale keys
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

        // Persist rebuilt max_expiry map
        *self.volume_max_expiry.write() = max_expiry_map;

        Ok(idx)
    }

    /// Run compaction: find sparse volumes, copy live entries to new volume.
    ///
    /// Issue #3408 improvements:
    /// - TOC-based lookup instead of full index scan (O(T) vs O(N) per volume)
    /// - Batch redb commit after seal (atomic index update)
    /// - Write-ahead ordering: seal new → commit index → delete old
    /// - Sort entries by offset for sequential I/O
    /// - Open old volume file once per compaction (not per-blob)
    /// - Log + preserve old volume on pread errors (no silent data loss)
    /// - Cumulative compaction stats counters
    fn do_compact(&self) -> PyResult<(u32, u64, u64)> {
        let mut volumes_compacted: u32 = 0;
        let mut blobs_moved: u64 = 0;
        let mut bytes_reclaimed: u64 = 0;

        // Phase 1: Find candidate volumes using TOC + mem_index.
        // Read each sealed volume's TOC, check which entries are still live
        // via mem_index (O(1) per entry), compute byte-based dead ratio.
        // dead_ratio = 1 - (live_bytes / total_bytes) per Issue #3408.
        let paths = self.volume_paths.read().clone();
        // (vol_id, path, file_size, dead_ratio, live TOC entries)
        let mut candidates: Vec<(u32, PathBuf, u64, f64, Vec<TocEntry>)> = Vec::new();

        for (vol_id, path) in &paths {
            // Skip .tmp (active) volumes
            if path.extension().is_some_and(|ext| ext == "tmp") {
                continue;
            }
            if let Ok((_, toc_entries)) = read_volume_toc(path) {
                // Compute total bytes from all TOC entries
                let total_bytes: u64 = toc_entries.iter().map(|e| e.size as u64).sum();
                // Filter for live entries using mem_index (O(1) per hash)
                let idx = self.mem_index.read();
                let live_toc: Vec<TocEntry> = toc_entries
                    .into_iter()
                    .filter(|e| e.flags & FLAG_TOMBSTONE == 0 && idx.contains(&e.hash))
                    .collect();
                drop(idx);

                let live_bytes: u64 = live_toc.iter().map(|e| e.size as u64).sum();
                let vol_dead_ratio = dead_ratio(live_bytes, total_bytes);
                if vol_dead_ratio >= self.compaction_sparsity_threshold {
                    let file_size = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
                    candidates.push((*vol_id, path.clone(), file_size, vol_dead_ratio, live_toc));
                }
            }
        }

        // Sort by dead ratio descending (most dead first)
        candidates.sort_by(|a, b| b.3.partial_cmp(&a.3).unwrap_or(std::cmp::Ordering::Equal));

        let mut cycle_budget = self.compaction_bytes_per_cycle as i64;

        for (vol_id, vol_path, vol_file_size, _, mut live_toc) in candidates {
            if live_toc.is_empty() {
                // Entirely dead volume — just delete
                let _ = fs::remove_file(&vol_path);
                self.volume_paths.write().remove(&vol_id);
                self.mem_index.write().close_volume(vol_id);
                bytes_reclaimed += vol_file_size;
                volumes_compacted += 1;
                continue;
            }

            // Sort live entries by offset for sequential I/O (Issue #3408)
            live_toc.sort_by_key(|e| e.offset);

            // Open old volume file once for all reads (Issue #3408)
            let mut old_file = match fs::File::open(&vol_path) {
                Ok(f) => f,
                Err(e) => {
                    eprintln!(
                        "Warning: compaction cannot open volume {}: {}",
                        vol_path.display(),
                        e
                    );
                    continue;
                }
            };

            // Read live blobs and write to new volume
            let new_vol_id = self.next_volume_id.fetch_add(1, Ordering::Relaxed);
            let target = self.get_target_volume_size();
            let mut new_vol =
                ActiveVolume::new(&self.volumes_dir, new_vol_id, target).map_err(io_err)?;

            let total_live = live_toc.len();
            let mut copied: u64 = 0;
            let mut skipped: u64 = 0;
            let mut cycle_exhausted = false;
            // Collect index updates for batch commit
            let mut index_updates: Vec<([u8; 32], IndexEntry)> = Vec::with_capacity(total_live);

            for toc_entry in &live_toc {
                // Read blob from old volume using the already-open file handle
                old_file
                    .seek(SeekFrom::Start(toc_entry.offset + ENTRY_HEADER_SIZE))
                    .map_err(io_err)?;
                let mut buf = vec![0u8; toc_entry.size as usize];
                match old_file.read_exact(&mut buf) {
                    Ok(()) => {
                        let new_offset = new_vol.append(&toc_entry.hash, &buf).map_err(io_err)?;

                        // Look up original index entry to preserve timestamp + expiry
                        let (timestamp, expiry) = self
                            .mem_index
                            .read()
                            .lookup_raw(&toc_entry.hash)
                            .map(|e| {
                                // Read timestamp from redb (mem_index doesn't store it)
                                let ts = self
                                    .lookup_entry(&toc_entry.hash)
                                    .ok()
                                    .flatten()
                                    .map(|ie| ie.timestamp)
                                    .unwrap_or_else(now_unix_secs);
                                (ts, e.expiry)
                            })
                            .unwrap_or_else(|| (now_unix_secs(), 0.0));

                        index_updates.push((
                            toc_entry.hash,
                            IndexEntry {
                                volume_id: new_vol_id,
                                offset: new_offset,
                                size: toc_entry.size,
                                timestamp,
                                expiry,
                            },
                        ));

                        copied += 1;

                        if cycle_budget > 0 {
                            cycle_budget -= toc_entry.size as i64;
                            if cycle_budget <= 0 {
                                cycle_exhausted = true;
                                break;
                            }
                        }
                    }
                    Err(e) => {
                        // Log and skip — do NOT silently lose data (Issue #3408)
                        let hash_hex: String = toc_entry
                            .hash
                            .iter()
                            .map(|b| format!("{:02x}", b))
                            .collect();
                        eprintln!(
                            "Warning: compaction skipped unreadable blob {} in {}: {}",
                            hash_hex,
                            vol_path.display(),
                            e
                        );
                        skipped += 1;
                        continue;
                    }
                }
            }

            // Write-ahead ordering (Issue #3408):
            // 1. Seal new volume (makes it a .vol — survives crash)
            // 2. Batch commit index updates to redb (atomic)
            // 3. Update mem_index
            // 4. Delete old volume (only if all entries accounted for)

            // Step 1: Seal new volume
            if new_vol.entry_count() > 0 {
                let (sealed_path, _) = new_vol.seal(&self.volumes_dir).map_err(io_err)?;
                if let Err(e) = self.mem_index.write().open_volume(new_vol_id, &sealed_path) {
                    eprintln!("Warning: failed to cache compacted volume FD: {}", e);
                }
                self.volume_paths.write().insert(new_vol_id, sealed_path);
            } else {
                let _ = fs::remove_file(&new_vol.path);
            }

            // Step 2: Batch commit all index updates in a single redb transaction
            if !index_updates.is_empty() {
                let db = self.db.read();
                let txn = db.begin_write().map_err(db_err)?;
                {
                    let mut table = txn.open_table(INDEX_TABLE).map_err(db_err)?;
                    for (hash, entry) in &index_updates {
                        table
                            .insert(hash.as_slice(), entry.to_bytes().as_slice())
                            .map_err(db_err)?;
                    }
                }
                txn.commit().map_err(db_err)?;
            }

            // Step 3: Update in-memory index
            {
                let mut idx = self.mem_index.write();
                for (hash, entry) in &index_updates {
                    idx.insert(
                        *hash,
                        MemIndexEntry {
                            volume_id: entry.volume_id,
                            offset: entry.offset,
                            size: entry.size,
                            expiry: entry.expiry,
                        },
                    );
                }
            }

            blobs_moved += copied;

            // Step 4: Delete old volume only if ALL live entries were copied.
            // If rate limit interrupted or pread errors occurred, preserve old volume.
            if copied + skipped >= total_live as u64 && skipped == 0 {
                let _ = fs::remove_file(&vol_path);
                self.volume_paths.write().remove(&vol_id);
                self.mem_index.write().close_volume(vol_id);
                bytes_reclaimed += vol_file_size;
                volumes_compacted += 1;
            }

            if cycle_exhausted {
                break;
            }
        }

        // Update cumulative compaction stats (Issue #3408)
        self.compaction_volumes_total
            .fetch_add(volumes_compacted as u64, Ordering::Relaxed);
        self.compaction_blobs_moved_total
            .fetch_add(blobs_moved, Ordering::Relaxed);
        self.compaction_bytes_reclaimed_total
            .fetch_add(bytes_reclaimed, Ordering::Relaxed);

        Ok((volumes_compacted, blobs_moved, bytes_reclaimed))
    }
}

impl Drop for BlobPackEngine {
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
            expiry: 1700003600.0,
        };
        let bytes = entry.to_bytes();
        let decoded = IndexEntry::from_bytes(&bytes).unwrap();
        assert_eq!(decoded.volume_id, 42);
        assert_eq!(decoded.offset, 1234567890);
        assert_eq!(decoded.size, 9999);
        assert!((decoded.timestamp - 1700000000.5).abs() < f64::EPSILON);
        assert!((decoded.expiry - 1700003600.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_index_entry_v1_compat() {
        // v1 entries (24 bytes) should decode with expiry = 0.0
        let entry = IndexEntry {
            volume_id: 1,
            offset: 100,
            size: 50,
            timestamp: 1700000000.0,
            expiry: 0.0,
        };
        let bytes = entry.to_bytes();
        // Simulate a v1 entry by only passing first 24 bytes
        let decoded = IndexEntry::from_bytes(&bytes[..24]).unwrap();
        assert_eq!(decoded.volume_id, 1);
        assert!((decoded.expiry - 0.0).abs() < f64::EPSILON);
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

        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
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

        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
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

        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
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

        let mut engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 0,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
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

        let mut engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 0,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
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
        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 256, // Very small!
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Write enough data to trigger multiple volume seals
        for i in 0..10u8 {
            let hash = hash_hex(i);
            engine.put(&hash, &[i; 100]).unwrap();
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

        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 4096, // Large enough for all 10 entries in one volume
            compaction_bytes_per_cycle: 0,     // No byte limit for tests
            compaction_sparsity_threshold: 0.3,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Write 10 entries
        for i in 0..10u8 {
            engine.put(&hash_hex(i), &[i; 50]).unwrap();
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

    // ─── Batch pre-allocation tests (Issue #3409) ───────────────────────

    #[test]
    fn test_preallocate_returns_reservation_id() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }
        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        let res_id = engine.preallocate(vec![100, 200, 300]).unwrap();
        assert!(res_id > 0, "Reservation ID should be positive");

        // Should have 3 slots in the reservation
        let reservations = engine.reservations.lock();
        let res = reservations.get(&res_id).unwrap();
        assert_eq!(res.slots.len(), 3);
        assert_eq!(res.slots[0].data_size, 100);
        assert_eq!(res.slots[1].data_size, 200);
        assert_eq!(res.slots[2].data_size, 300);
    }

    #[test]
    fn test_filter_known_excludes_existing() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }
        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Put hash 1 and 2
        engine.put(&hash_hex(1), b"data1").unwrap();
        engine.put(&hash_hex(2), b"data2").unwrap();

        // filter_known should return only hash 3 (unknown)
        let all = vec![hash_hex(1), hash_hex(2), hash_hex(3)];
        let unknown = engine.filter_known(all).unwrap();
        assert_eq!(unknown.len(), 1);
        assert_eq!(unknown[0], hash_hex(3));
    }

    #[test]
    fn test_expire_reservations() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }
        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 1024 * 1024,
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Create a reservation
        let _res_id = engine.preallocate(vec![100]).unwrap();

        // Should NOT expire (fresh reservation)
        assert_eq!(engine.expire_reservations(), 0);
        assert_eq!(engine.reservations.lock().len(), 1);

        // Manually expire by setting expires_at to past
        {
            let mut reservations = engine.reservations.lock();
            for (_, res) in reservations.iter_mut() {
                res.expires_at = 0.0; // expired in the past
            }
        }

        // Now should expire
        assert_eq!(engine.expire_reservations(), 1);
        assert_eq!(engine.reservations.lock().len(), 0);
    }

    #[test]
    fn test_compute_entry_aligned_size() {
        // Entry header is 37 bytes, alignment is 8
        // 37 + 0 = 37 → aligned to 40
        assert_eq!(compute_entry_aligned_size(0), 40);
        // 37 + 3 = 40 → already aligned
        assert_eq!(compute_entry_aligned_size(3), 40);
        // 37 + 4 = 41 → aligned to 48
        assert_eq!(compute_entry_aligned_size(4), 48);
        // 37 + 100 = 137 → aligned to 144
        assert_eq!(compute_entry_aligned_size(100), 144);
    }

    #[test]
    fn test_batch_preallocate_multi_volume_span() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("volume_index.redb");
        let db = Database::create(&db_path).unwrap();
        {
            let txn = db.begin_write().unwrap();
            txn.open_table(INDEX_TABLE).unwrap();
            txn.open_table(META_TABLE).unwrap();
            txn.commit().unwrap();
        }
        let engine = BlobPackEngine {
            volumes_dir: dir.path().to_path_buf(),
            db: RwLock::new(db),
            active: Mutex::new(None),
            next_volume_id: AtomicU32::new(1),
            total_bytes: AtomicU64::new(0),
            volume_paths: RwLock::new(HashMap::new()),
            is_open: AtomicBool::new(true),
            target_volume_size_override: 256, // Very small volumes to force spanning
            compaction_bytes_per_cycle: 0,
            compaction_sparsity_threshold: 0.4,
            pending_index: Mutex::new(Vec::new()),
            index_batch_size: 256,
            mem_index: RwLock::new(BlobPackIndex::new()),
            compaction_volumes_total: AtomicU64::new(0),
            compaction_blobs_moved_total: AtomicU64::new(0),
            compaction_bytes_reclaimed_total: AtomicU64::new(0),
            volume_max_expiry: RwLock::new(HashMap::new()),
            reservations: Mutex::new(HashMap::new()),
            next_reservation_id: AtomicU64::new(1),
            batch_write_fds: RwLock::new(HashMap::new()),
        };

        // Each entry will be ~90 bytes (37 header + 50 data + 1 padding = 88, aligned to 88).
        // With 256 byte volumes (minus 64 byte header = 192 usable), fits ~2 entries per volume.
        // 6 entries should span 3+ volumes.
        let res_id = engine.preallocate(vec![50; 6]).unwrap();

        // Verify reservation has 6 slots
        {
            let reservations = engine.reservations.lock();
            let res = reservations.get(&res_id).unwrap();
            assert_eq!(res.slots.len(), 6);

            // Should reference multiple volumes
            let vol_ids: std::collections::HashSet<u32> =
                res.slots.iter().map(|s| s.volume_id).collect();
            assert!(
                vol_ids.len() >= 2,
                "Expected multi-volume spanning, got {} volume(s)",
                vol_ids.len()
            );
        }
    }
}
