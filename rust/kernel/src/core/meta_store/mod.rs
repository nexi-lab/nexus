//! MetaStore pillar — Rust kernel metadata contract.
//!
//! Rust equivalent of Python `MetastoreABC` (one of the Four Storage Pillars).
//! Provides ordered key-value storage for file metadata (inodes, config, topology).
//!
//! Local impl: LocalMetaStore (redb crate, ~5μs reads).
//! Remote impl: gRPC client (existing network boundary).
//!
//! Issue #1868: Pure Rust ABI — no PyO3 dependency.
//! PyMetaStoreAdapter lives in generated_store.rs (auto-generated).

// Phase C nested layout:
//   core/metastore/mod.rs    — was kernel/src/metastore.rs
//   core/metastore/remote.rs — was kernel/src/remote_metastore.rs
pub mod remote;

/// Metadata record for a single file/directory.
///
/// Mirrors the Python `FileMetadata` fields needed by the Rust kernel.
///
/// Schema notes:
/// - `path` is the authoritative file identifier. Read-side backend dispatch
///   uses it (minus the local mount prefix) — there is no separate
///   `physical_path` because for path-addressed backends it is just
///   `path - mount_prefix`, and for content-addressed backends `etag` is
///   the key.
/// - `last_writer_address` records `host:port` of the node that performed
///   the most recent write (overwritten on every successful write). Pure
///   descriptive metadata — the kernel does not interpret it. Higher
///   layers (e.g. federation) compare it against the local node address
///   to route content fetches. There is no per-record `backend_name`:
///   each node picks its backend from its own mount table.
#[derive(Clone, Debug, Default)]
pub struct FileMetadata {
    pub path: String,
    pub size: u64,
    pub etag: Option<String>,
    pub version: u32,
    pub entry_type: u8,
    pub zone_id: Option<String>,
    pub mime_type: Option<String>,
    /// Creation timestamp (Unix epoch milliseconds). Populated by
    /// ``kernel::sys_write`` on first write; subsequent overwrites preserve
    /// it via the dcache snapshot.
    pub created_at_ms: Option<i64>,
    /// Last modification timestamp (Unix epoch milliseconds). Updated on
    /// every write.
    pub modified_at_ms: Option<i64>,
    /// `host:port` of the node that performed the most recent write
    /// (overwritten on every successful write). Set by the kernel from
    /// its self-published address. Higher layers (federation) interpret
    /// it; the kernel only stores and forwards. `None` on single-node
    /// deployments without a published address.
    pub last_writer_address: Option<String>,
}

/// Error type for MetaStore operations.
#[derive(Debug)]
pub enum MetaStoreError {
    /// Key not found.
    NotFound(String),
    /// Underlying I/O or storage error.
    IOError(String),
}

/// Result of a `put_if_version` optimistic-concurrency check.
///
/// Naming note: "CAS" in the kernel already means **Content-Addressed
/// Storage** (see `cas_engine.rs`) — the blob pillar. This struct is
/// the unrelated *compare-and-swap* primitive used by the metastore's
/// version guard on `put`, so it is spelled out in full to avoid
/// collision with the CAS blob namespace.
#[derive(Debug, Clone, Copy)]
pub struct PutIfVersionResult {
    /// True if the write was applied.
    pub success: bool,
    /// Version currently in the store after this call (new version on
    /// success, existing version on conflict).
    pub current_version: u32,
}

/// `(path, optional value)` pairs used by bulk auxiliary-metadata reads
/// and bulk content-id lookups. Values are UTF-8 strings — every real
/// caller stores text (`parsed_text`, `parser_name`, JSON-encoded
/// blobs), so the kernel boundary avoids a `PyBytes` GIL crossing.
pub type PathValueStr = (String, Option<String>);
pub type PathEtag = (String, Option<String>);

/// One page of a paginated list scan.
#[derive(Debug, Default, Clone)]
pub struct PaginatedList {
    pub items: Vec<FileMetadata>,
    pub next_cursor: Option<String>,
    pub has_more: bool,
    pub total_count: usize,
}

/// MetaStore pillar — kernel metadata contract.
///
/// Rust equivalent of Python `MetastoreABC`.
/// Local impls (redb) implement directly; remote impls go through
/// existing gRPC network boundaries.
///
/// 5 abstract methods matching the Python ABC:
///   - get, put, delete, list, exists
///
/// **Key contract (R20.3)**: callers always pass full global paths —
/// including the mount-point prefix. Impls that store zone-relative
/// internally (``ZoneMetaStore``) translate at their boundary so
/// federation-layer concerns never leak up. Returned ``FileMetadata.path``
/// values are likewise full paths.
pub trait MetaStore: Send + Sync {
    /// Get metadata for a path. Returns None if not found.
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetaStoreError>;

    /// Put metadata at a path (insert or update).
    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetaStoreError>;

    /// Delete metadata at a path. Returns true if it existed.
    fn delete(&self, path: &str) -> Result<bool, MetaStoreError>;

    /// List all metadata entries under a prefix.
    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError>;

    /// Check if a path exists in the metastore.
    fn exists(&self, path: &str) -> Result<bool, MetaStoreError>;

    /// Batch put: store multiple metadata records.
    /// Default impl loops single puts. Override for single-transaction batch.
    fn put_batch(&self, items: &[(String, FileMetadata)]) -> Result<(), MetaStoreError> {
        for (path, meta) in items {
            self.put(path, meta.clone())?;
        }
        Ok(())
    }

    /// Batch get: retrieve metadata for multiple paths.
    /// Default impl loops single gets.
    fn get_batch(&self, paths: &[String]) -> Result<Vec<Option<FileMetadata>>, MetaStoreError> {
        let mut results = Vec::with_capacity(paths.len());
        for path in paths {
            results.push(self.get(path)?);
        }
        Ok(results)
    }

    /// Batch delete: remove metadata for multiple paths.
    /// Returns number of entries that existed and were deleted.
    /// Default impl loops single deletes. Override for single-transaction batch.
    fn delete_batch(&self, paths: &[String]) -> Result<usize, MetaStoreError> {
        let mut count = 0;
        for path in paths {
            if self.delete(path)? {
                count += 1;
            }
        }
        Ok(count)
    }

    /// Compare-and-swap put: write only if current stored version equals
    /// `expected_version`. Returns a `PutIfVersionResult` whose `current_version`
    /// field reflects the state after this call (caller can use the
    /// mismatch case to rebuild a retry).
    ///
    /// Default impl is racy (get → compare → put). Redb overrides with a
    /// single write txn; ZoneMetaStore overrides with a raft propose.
    fn put_if_version(
        &self,
        metadata: FileMetadata,
        expected_version: u32,
    ) -> Result<PutIfVersionResult, MetaStoreError> {
        let path = metadata.path.clone();
        let current = self.get(&path)?;
        let current_ver = current.as_ref().map(|m| m.version).unwrap_or(0);
        if current_ver != expected_version {
            return Ok(PutIfVersionResult {
                success: false,
                current_version: current_ver,
            });
        }
        let new_ver = metadata.version;
        self.put(&path, metadata)?;
        Ok(PutIfVersionResult {
            success: true,
            current_version: new_ver,
        })
    }

    /// Rename a path (and optionally all children, if the path is a
    /// directory with entries under `old_path + "/"`).
    ///
    /// Default impl: rewrites `old_path` entry and every entry under
    /// `old_path + "/"` prefix via get → put(new_key) → delete(old_key).
    /// Not atomic under concurrent writers — callers that need
    /// atomicity override (redb uses a single write txn).
    fn rename_path(&self, old_path: &str, new_path: &str) -> Result<(), MetaStoreError> {
        if old_path == new_path {
            return Ok(());
        }
        if let Some(mut meta) = self.get(old_path)? {
            meta.path = new_path.to_string();
            self.put(new_path, meta)?;
            self.delete(old_path)?;
        }
        let old_prefix = format!("{}/", old_path.trim_end_matches('/'));
        let new_prefix = format!("{}/", new_path.trim_end_matches('/'));
        let children = self.list(&old_prefix)?;
        for mut child in children {
            let suffix = match child.path.strip_prefix(&old_prefix) {
                Some(s) => s.to_string(),
                None => continue,
            };
            let old_child = child.path.clone();
            let new_child = format!("{}{}", new_prefix, suffix);
            child.path = new_child.clone();
            self.put(&new_child, child)?;
            self.delete(&old_child)?;
        }
        Ok(())
    }

    /// Store an auxiliary key/value blob attached to a path (e.g.
    /// `parsed_text`, tags, observer state). Separate namespace from the
    /// `FileMetadata` struct fields.
    ///
    /// Default impl returns an error — each concrete impl must provide
    /// its own storage (DashMap sidecar, second redb table, raft
    /// command).
    fn set_file_metadata(
        &self,
        path: &str,
        key: &str,
        value: String,
    ) -> Result<(), MetaStoreError> {
        let _ = (path, key, value);
        Err(MetaStoreError::IOError(
            "set_file_metadata not implemented for this metastore".into(),
        ))
    }

    /// Read an auxiliary key/value blob. Default impl returns `Ok(None)`.
    fn get_file_metadata(&self, path: &str, key: &str) -> Result<Option<String>, MetaStoreError> {
        let _ = (path, key);
        Ok(None)
    }

    /// Bulk read a single auxiliary key across multiple paths. Default
    /// impl loops `get_file_metadata`.
    fn get_file_metadata_bulk(
        &self,
        paths: &[String],
        key: &str,
    ) -> Result<Vec<PathValueStr>, MetaStoreError> {
        let mut out = Vec::with_capacity(paths.len());
        for p in paths {
            out.push((p.clone(), self.get_file_metadata(p, key)?));
        }
        Ok(out)
    }

    /// Return true if `path` has any children under `path + "/"`.
    fn is_implicit_directory(&self, path: &str) -> Result<bool, MetaStoreError> {
        let prefix = format!("{}/", path.trim_end_matches('/'));
        let children = self.list(&prefix)?;
        Ok(!children.is_empty())
    }

    /// Paginated list. Default impl materializes `list(prefix)` and
    /// slices. Override for backends where streaming matters.
    fn list_paginated(
        &self,
        prefix: &str,
        recursive: bool,
        limit: usize,
        cursor: Option<&str>,
    ) -> Result<PaginatedList, MetaStoreError> {
        let mut all = self.list(prefix)?;
        if !recursive {
            let depth = prefix.trim_end_matches('/').matches('/').count() + 1;
            all.retain(|m| m.path.trim_end_matches('/').matches('/').count() == depth);
        }
        let start: usize = cursor.and_then(|c| c.parse().ok()).unwrap_or(0);
        let end = (start + limit).min(all.len());
        let items = all[start..end].to_vec();
        let has_more = end < all.len();
        let next_cursor = if has_more {
            Some(end.to_string())
        } else {
            None
        };
        Ok(PaginatedList {
            items,
            next_cursor,
            has_more,
            total_count: all.len(),
        })
    }

    /// Bulk fetch content IDs (etags) for many paths. Default impl
    /// loops `get` and returns the etag from each record.
    fn batch_get_content_ids(&self, paths: &[String]) -> Result<Vec<PathEtag>, MetaStoreError> {
        let mut out = Vec::with_capacity(paths.len());
        for p in paths {
            let etag = self.get(p)?.and_then(|m| m.etag);
            out.push((p.clone(), etag));
        }
        Ok(out)
    }

    /// Opaque identity for "stores backed by the SAME underlying state"
    /// (R20.6 option B).
    ///
    /// Two ``Arc<dyn MetaStore>`` can correspond to different VFS mount
    /// points yet share the same physical storage — the canonical case
    /// is a single federation zone surfaced under ``/corp`` AND
    /// ``/family/work`` (crosslink). R20.3 gave each crosslink its own
    /// ``ZoneMetaStore`` (different ``mount_point``), so ``Arc::ptr_eq``
    /// no longer suffices to find every mount that shares the same zone.
    ///
    /// Return ``Some(usize)`` with a stable integer key for all
    /// metastores that share physical storage (``Arc::as_ptr`` of the
    /// shared handle works well — integer comparison, no lifetime
    /// entanglement). Return ``None`` when the metastore is standalone
    /// (``LocalMetaStore``) — the default.
    ///
    /// Used by ``VFSRouter::mount_points_for_coherence_key`` to fan
    /// out apply-side dcache invalidation across crosslinks.
    fn coherence_key(&self) -> Option<usize> {
        None
    }
}

// PyMetaStoreAdapter + conversion helpers (extract_metadata, to_python_metadata)
// are in generated_pyo3.rs — auto-generated by scripts/codegen_kernel_abi.py.
// This file stays language-agnostic (pure Rust ABI).

// ── MemoryMetaStore — pure Rust in-memory metastore (tests + minimal mode) ──
//
// Replaces the Python ``DictMetastore`` test helper. Same semantics — a flat
// path → FileMetadata map — but lives inside the Rust kernel so
// ``kernel.sys_write`` can persist through it without crossing the GIL or
// requiring a temp file. Construction is via
// ``Kernel::set_memory_metastore`` (PyKernel exposes the same).

use dashmap::DashMap;

/// In-memory metastore impl backed by a ``DashMap`` (concurrent, no I/O).
pub struct MemoryMetaStore {
    entries: DashMap<String, FileMetadata>,
    /// Auxiliary per-file metadata (e.g. `parsed_text`, tags). Separate
    /// namespace from the main `entries` map. Outer key is the file path,
    /// inner key is the metadata key (e.g. `"parsed_text"`), value is a
    /// UTF-8 string — callers that want to store structured data JSON-
    /// encode it themselves at the boundary (see `metadata_export.py`).
    file_metadata: DashMap<String, DashMap<String, String>>,
}

impl MemoryMetaStore {
    pub fn new() -> Self {
        Self {
            entries: DashMap::new(),
            file_metadata: DashMap::new(),
        }
    }
}

impl Default for MemoryMetaStore {
    fn default() -> Self {
        Self::new()
    }
}

impl MetaStore for MemoryMetaStore {
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
        Ok(self.entries.get(path).map(|e| e.clone()))
    }

    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetaStoreError> {
        self.entries.insert(path.to_string(), metadata);
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetaStoreError> {
        self.file_metadata.remove(path);
        Ok(self.entries.remove(path).is_some())
    }

    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError> {
        let mut out = Vec::new();
        for entry in self.entries.iter() {
            if entry.key().starts_with(prefix) {
                out.push(entry.value().clone());
            }
        }
        Ok(out)
    }

    fn exists(&self, path: &str) -> Result<bool, MetaStoreError> {
        Ok(self.entries.contains_key(path))
    }

    fn put_if_version(
        &self,
        metadata: FileMetadata,
        expected_version: u32,
    ) -> Result<PutIfVersionResult, MetaStoreError> {
        let path = metadata.path.clone();
        use dashmap::mapref::entry::Entry;
        match self.entries.entry(path) {
            Entry::Vacant(slot) => {
                if expected_version != 0 {
                    return Ok(PutIfVersionResult {
                        success: false,
                        current_version: 0,
                    });
                }
                let new_ver = metadata.version;
                slot.insert(metadata);
                Ok(PutIfVersionResult {
                    success: true,
                    current_version: new_ver,
                })
            }
            Entry::Occupied(mut slot) => {
                let current_ver = slot.get().version;
                if current_ver != expected_version {
                    return Ok(PutIfVersionResult {
                        success: false,
                        current_version: current_ver,
                    });
                }
                let new_ver = metadata.version;
                slot.insert(metadata);
                Ok(PutIfVersionResult {
                    success: true,
                    current_version: new_ver,
                })
            }
        }
    }

    fn rename_path(&self, old_path: &str, new_path: &str) -> Result<(), MetaStoreError> {
        if old_path == new_path {
            return Ok(());
        }
        if let Some((_, mut meta)) = self.entries.remove(old_path) {
            meta.path = new_path.to_string();
            self.entries.insert(new_path.to_string(), meta);
            if let Some((_, fm)) = self.file_metadata.remove(old_path) {
                self.file_metadata.insert(new_path.to_string(), fm);
            }
        }
        let old_prefix = format!("{}/", old_path.trim_end_matches('/'));
        let new_prefix = format!("{}/", new_path.trim_end_matches('/'));
        let child_keys: Vec<String> = self
            .entries
            .iter()
            .filter_map(|e| {
                if e.key().starts_with(&old_prefix) {
                    Some(e.key().clone())
                } else {
                    None
                }
            })
            .collect();
        for old_child in child_keys {
            if let Some((_, mut meta)) = self.entries.remove(&old_child) {
                let suffix = old_child
                    .strip_prefix(&old_prefix)
                    .map(|s| s.to_string())
                    .unwrap_or_default();
                let new_child = format!("{}{}", new_prefix, suffix);
                meta.path = new_child.clone();
                self.entries.insert(new_child.clone(), meta);
                if let Some((_, fm)) = self.file_metadata.remove(&old_child) {
                    self.file_metadata.insert(new_child, fm);
                }
            }
        }
        Ok(())
    }

    fn set_file_metadata(
        &self,
        path: &str,
        key: &str,
        value: String,
    ) -> Result<(), MetaStoreError> {
        let inner = self.file_metadata.entry(path.to_string()).or_default();
        inner.insert(key.to_string(), value);
        Ok(())
    }

    fn get_file_metadata(&self, path: &str, key: &str) -> Result<Option<String>, MetaStoreError> {
        Ok(self
            .file_metadata
            .get(path)
            .and_then(|inner| inner.get(key).map(|v| v.value().clone())))
    }
}

// ── LocalMetaStore — single-node redb-backed metastore ──────────────────
//
// Historic name: RedbMetaStore. Renamed R20.4 because "redb" is a
// shared implementation detail — the Raft state machine also uses
// redb underneath. The distinguishing axis is "single-node vs
// raft-replicated", captured by the Local / Zone naming pair.

use redb::{Database, ReadableTable, TableDefinition};
use std::path::Path;
use std::sync::Arc;

/// redb table: path (str) → serialized FileMetadata (bytes).
///
/// Serialization: compact binary format (not JSON — too slow for hot path).
/// Fields are written in fixed order; strings are length-prefixed.
const METADATA_TABLE: TableDefinition<&str, &[u8]> = TableDefinition::new("metadata");

/// redb table: "path\0key" → auxiliary metadata value bytes. Mirrors the
/// Python `DictMetastore._file_metadata` dict-of-dicts, flattened into a
/// single table with a composite key so range-scans can enumerate all
/// keys for a given path.
const FILE_METADATA_TABLE: TableDefinition<&str, &[u8]> = TableDefinition::new("file_metadata");

/// Single-node (non-replicated) MetaStore backed by redb — ~5μs reads,
/// zero GIL.
///
/// Used by standalone deployments; federation mounts install a
/// ``ZoneMetaStore`` instead (same on-disk crate, raft-replicated).
pub(crate) struct LocalMetaStore {
    db: Arc<Database>,
}

impl LocalMetaStore {
    /// Open or create a redb database at the given path.
    pub fn open(path: &Path) -> Result<Self, MetaStoreError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| MetaStoreError::IOError(format!("mkdir {}: {e}", parent.display())))?;
        }
        let db = Database::create(path)
            .map_err(|e| MetaStoreError::IOError(format!("redb open {}: {e}", path.display())))?;

        // Ensure tables exist (single empty write txn on first open)
        let txn = db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb begin_write: {e}")))?;
        {
            let _table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            let _fm_table = txn.open_table(FILE_METADATA_TABLE).map_err(|e| {
                MetaStoreError::IOError(format!("redb open file_metadata table: {e}"))
            })?;
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;

        Ok(Self { db: Arc::new(db) })
    }
}

/// Compose the flat `FILE_METADATA_TABLE` key `path\0key`.
fn fm_composite_key(path: &str, key: &str) -> String {
    let mut s = String::with_capacity(path.len() + key.len() + 1);
    s.push_str(path);
    s.push('\0');
    s.push_str(key);
    s
}

/// Compact binary serialization for FileMetadata.
///
/// Format: [version_tag:u8=2][path_len:u32][path][backend_name_len:u32][backend_name]
///         [physical_path_len:u32][physical_path][size:u64]
///         [has_etag:u8][etag_len:u32][etag][version:u32][entry_type:u8]
///         [has_zone_id:u8][zone_id_len:u32][zone_id]
///         [has_mime_type:u8][mime_type_len:u32][mime_type]
///         [has_created_at:u8][created_at:i64]
///         [has_modified_at:u8][modified_at:i64]
///         [optional trailing bytes — tolerated for forward-compat]
///
/// version_tag distinguishes v1 (no timestamps) from v2 (timestamps appended).
/// v1 has no leading tag; the first byte is the path length, which always
/// has a high byte of 0 for paths shorter than 16M, while v2 starts with 2.
/// Older redb files that carry a trailing ``target_zone_id`` extension
/// (R16.1a, reverted in R20.1) deserialize fine — the reader stops at the
/// last tracked field and ignores any remaining bytes.
fn serialize_metadata(meta: &FileMetadata) -> Vec<u8> {
    let mut buf = Vec::with_capacity(280);

    fn write_str(buf: &mut Vec<u8>, s: &str) {
        buf.extend_from_slice(&(s.len() as u32).to_le_bytes());
        buf.extend_from_slice(s.as_bytes());
    }
    fn write_opt_str(buf: &mut Vec<u8>, s: &Option<String>) {
        match s {
            Some(v) => {
                buf.push(1);
                write_str(buf, v);
            }
            None => buf.push(0),
        }
    }
    fn write_opt_i64(buf: &mut Vec<u8>, v: Option<i64>) {
        match v {
            Some(n) => {
                buf.push(1);
                buf.extend_from_slice(&n.to_le_bytes());
            }
            None => buf.push(0),
        }
    }

    buf.push(3); // version tag — v3 dropped backend_name/physical_path,
                 // added last_writer_address as the trailing optional slot.
    write_str(&mut buf, &meta.path);
    buf.extend_from_slice(&meta.size.to_le_bytes());
    write_opt_str(&mut buf, &meta.etag);
    buf.extend_from_slice(&meta.version.to_le_bytes());
    buf.push(meta.entry_type);
    write_opt_str(&mut buf, &meta.zone_id);
    write_opt_str(&mut buf, &meta.mime_type);
    write_opt_i64(&mut buf, meta.created_at_ms);
    write_opt_i64(&mut buf, meta.modified_at_ms);
    write_opt_str(&mut buf, &meta.last_writer_address);

    buf
}

fn deserialize_metadata(data: &[u8]) -> Result<FileMetadata, MetaStoreError> {
    if data.is_empty() {
        return Err(MetaStoreError::IOError("empty record".into()));
    }
    // Only the current v3 format is recognised. Older v1/v2 records are
    // intentionally not supported — the schema cleanup that introduced v3
    // dropped backend_name and physical_path slots and added
    // last_writer_address, so any pre-cleanup data is wipe-and-rebuild.
    if data[0] != 3 {
        return Err(MetaStoreError::IOError(format!(
            "unsupported FileMetadata serialization tag {}; expected 3 (older formats no longer readable — data dir must be wiped post-schema-cleanup)",
            data[0]
        )));
    }
    let mut pos = 1usize;

    fn read_str(data: &[u8], pos: &mut usize) -> Result<String, MetaStoreError> {
        if *pos + 4 > data.len() {
            return Err(MetaStoreError::IOError("truncated string length".into()));
        }
        let len = u32::from_le_bytes(data[*pos..*pos + 4].try_into().unwrap()) as usize;
        *pos += 4;
        if *pos + len > data.len() {
            return Err(MetaStoreError::IOError("truncated string data".into()));
        }
        let s = std::str::from_utf8(&data[*pos..*pos + len])
            .map_err(|e| MetaStoreError::IOError(format!("invalid utf8: {e}")))?
            .to_string();
        *pos += len;
        Ok(s)
    }
    fn read_opt_str(data: &[u8], pos: &mut usize) -> Result<Option<String>, MetaStoreError> {
        if *pos >= data.len() {
            return Err(MetaStoreError::IOError("truncated optional flag".into()));
        }
        let flag = data[*pos];
        *pos += 1;
        if flag == 0 {
            Ok(None)
        } else {
            read_str(data, pos).map(Some)
        }
    }

    let path = read_str(data, &mut pos)?;

    if pos + 8 > data.len() {
        return Err(MetaStoreError::IOError("truncated size".into()));
    }
    let size = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
    pos += 8;

    let etag = read_opt_str(data, &mut pos)?;

    if pos + 4 > data.len() {
        return Err(MetaStoreError::IOError("truncated version".into()));
    }
    let version = u32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
    pos += 4;

    if pos >= data.len() {
        return Err(MetaStoreError::IOError("truncated entry_type".into()));
    }
    let entry_type = data[pos];
    pos += 1;

    let zone_id = read_opt_str(data, &mut pos)?;
    let mime_type = read_opt_str(data, &mut pos)?;

    fn read_opt_i64(data: &[u8], pos: &mut usize) -> Result<Option<i64>, MetaStoreError> {
        if *pos >= data.len() {
            return Ok(None);
        }
        let flag = data[*pos];
        *pos += 1;
        if flag == 0 {
            return Ok(None);
        }
        if *pos + 8 > data.len() {
            return Err(MetaStoreError::IOError("truncated i64".into()));
        }
        let n = i64::from_le_bytes(data[*pos..*pos + 8].try_into().unwrap());
        *pos += 8;
        Ok(Some(n))
    }

    let created_at_ms = read_opt_i64(data, &mut pos)?;
    let modified_at_ms = read_opt_i64(data, &mut pos)?;
    // Trailing optional slots may grow over time; missing reads return None.
    let last_writer_address = read_opt_str(data, &mut pos).ok().flatten();

    let _ = pos;

    Ok(FileMetadata {
        path,
        size,
        etag,
        version,
        entry_type,
        zone_id,
        mime_type,
        created_at_ms,
        modified_at_ms,
        last_writer_address,
    })
}

impl MetaStore for LocalMetaStore {
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
        let txn = self
            .db
            .begin_read()
            .map_err(|e| MetaStoreError::IOError(format!("redb read txn: {e}")))?;
        let table = txn
            .open_table(METADATA_TABLE)
            .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
        match table.get(path) {
            Ok(Some(guard)) => {
                let data = guard.value();
                deserialize_metadata(data).map(Some)
            }
            Ok(None) => Ok(None),
            Err(e) => Err(MetaStoreError::IOError(format!("redb get: {e}"))),
        }
    }

    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetaStoreError> {
        let data = serialize_metadata(&metadata);
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            table
                .insert(path, data.as_slice())
                .map_err(|e| MetaStoreError::IOError(format!("redb insert: {e}")))?;
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetaStoreError> {
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        let existed;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            existed = table
                .remove(path)
                .map_err(|e| MetaStoreError::IOError(format!("redb remove: {e}")))?
                .is_some();
        }
        // Drop any auxiliary file_metadata entries for this path in the
        // same txn via a range scan on the "path\0..." prefix.
        {
            let mut fm_table = txn
                .open_table(FILE_METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open fm table: {e}")))?;
            let start = fm_composite_key(path, "");
            let mut end = start.clone();
            end.push('\u{1}'); // next byte after null separator
            let keys: Vec<String> = {
                let iter = fm_table
                    .range(start.as_str()..end.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb fm range: {e}")))?;
                let mut keys = Vec::new();
                for entry in iter {
                    let (k, _) =
                        entry.map_err(|e| MetaStoreError::IOError(format!("redb fm iter: {e}")))?;
                    keys.push(k.value().to_string());
                }
                keys
            };
            for k in keys {
                fm_table
                    .remove(k.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb fm remove: {e}")))?;
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(existed)
    }

    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError> {
        let txn = self
            .db
            .begin_read()
            .map_err(|e| MetaStoreError::IOError(format!("redb read txn: {e}")))?;
        let table = txn
            .open_table(METADATA_TABLE)
            .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;

        let mut results = Vec::new();

        if prefix.is_empty() {
            // Empty prefix = full table scan
            let iter = table
                .iter()
                .map_err(|e| MetaStoreError::IOError(format!("redb iter: {e}")))?;
            for entry in iter {
                let (_, value) =
                    entry.map_err(|e| MetaStoreError::IOError(format!("redb iter: {e}")))?;
                results.push(deserialize_metadata(value.value())?);
            }
        } else {
            // Range scan: prefix..prefix with last byte incremented
            let mut range_end = prefix.to_string();
            if let Some(last) = range_end.pop() {
                range_end.push(char::from_u32(last as u32 + 1).unwrap_or(char::MAX));
            }
            let iter = table
                .range(prefix..range_end.as_str())
                .map_err(|e| MetaStoreError::IOError(format!("redb range: {e}")))?;
            for entry in iter {
                let (_, value) =
                    entry.map_err(|e| MetaStoreError::IOError(format!("redb iter: {e}")))?;
                results.push(deserialize_metadata(value.value())?);
            }
        }
        Ok(results)
    }

    fn exists(&self, path: &str) -> Result<bool, MetaStoreError> {
        let txn = self
            .db
            .begin_read()
            .map_err(|e| MetaStoreError::IOError(format!("redb read txn: {e}")))?;
        let table = txn
            .open_table(METADATA_TABLE)
            .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
        table
            .get(path)
            .map(|opt| opt.is_some())
            .map_err(|e| MetaStoreError::IOError(format!("redb get: {e}")))
    }

    /// Single write transaction for all items — optimal for redb.
    fn put_batch(&self, items: &[(String, FileMetadata)]) -> Result<(), MetaStoreError> {
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            for (path, meta) in items {
                let data = serialize_metadata(meta);
                table
                    .insert(path.as_str(), data.as_slice())
                    .map_err(|e| MetaStoreError::IOError(format!("redb insert: {e}")))?;
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(())
    }

    /// Single write transaction for all deletes — optimal for redb.
    fn delete_batch(&self, paths: &[String]) -> Result<usize, MetaStoreError> {
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        let mut count = 0;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            for path in paths {
                if table
                    .remove(path.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb remove: {e}")))?
                    .is_some()
                {
                    count += 1;
                }
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(count)
    }

    /// Single read transaction for all paths.
    fn get_batch(&self, paths: &[String]) -> Result<Vec<Option<FileMetadata>>, MetaStoreError> {
        let txn = self
            .db
            .begin_read()
            .map_err(|e| MetaStoreError::IOError(format!("redb read txn: {e}")))?;
        let table = txn
            .open_table(METADATA_TABLE)
            .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
        let mut results = Vec::with_capacity(paths.len());
        for path in paths {
            match table.get(path.as_str()) {
                Ok(Some(guard)) => results.push(Some(deserialize_metadata(guard.value())?)),
                Ok(None) => results.push(None),
                Err(e) => return Err(MetaStoreError::IOError(format!("redb get_batch: {e}"))),
            }
        }
        Ok(results)
    }

    /// Single write txn: read current version, compare, write on match.
    fn put_if_version(
        &self,
        metadata: FileMetadata,
        expected_version: u32,
    ) -> Result<PutIfVersionResult, MetaStoreError> {
        let path = metadata.path.clone();
        let new_ver = metadata.version;
        let data = serialize_metadata(&metadata);
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        let result;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            let current_ver = match table.get(path.as_str()) {
                Ok(Some(guard)) => deserialize_metadata(guard.value())?.version,
                Ok(None) => 0,
                Err(e) => {
                    return Err(MetaStoreError::IOError(format!(
                        "redb put_if_version get: {e}"
                    )))
                }
            };
            if current_ver != expected_version {
                result = PutIfVersionResult {
                    success: false,
                    current_version: current_ver,
                };
            } else {
                table
                    .insert(path.as_str(), data.as_slice())
                    .map_err(|e| MetaStoreError::IOError(format!("redb cas insert: {e}")))?;
                result = PutIfVersionResult {
                    success: true,
                    current_version: new_ver,
                };
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(result)
    }

    /// Single write txn: rewrite `old_path` and all children under
    /// `old_path + "/"` to their new names. Keys are rewritten in place
    /// (remove + insert) since redb has no rename primitive.
    fn rename_path(&self, old_path: &str, new_path: &str) -> Result<(), MetaStoreError> {
        if old_path == new_path {
            return Ok(());
        }
        let old_prefix = format!("{}/", old_path.trim_end_matches('/'));
        let new_prefix = format!("{}/", new_path.trim_end_matches('/'));
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        {
            let mut table = txn
                .open_table(METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open_table: {e}")))?;
            // Gather everything first (top-level + children) so the range
            // iterator / remove guards all drop before we start inserting.
            let mut to_rewrite: Vec<(String, String, Vec<u8>)> = Vec::new();
            {
                let top_bytes = table
                    .get(old_path)
                    .map_err(|e| MetaStoreError::IOError(format!("redb get: {e}")))?
                    .map(|guard| guard.value().to_vec());
                if let Some(bytes) = top_bytes {
                    to_rewrite.push((old_path.to_string(), new_path.to_string(), bytes));
                }
                let mut range_end = old_prefix.clone();
                if let Some(last) = range_end.pop() {
                    range_end.push(char::from_u32(last as u32 + 1).unwrap_or(char::MAX));
                }
                let iter = table
                    .range(old_prefix.as_str()..range_end.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb range: {e}")))?;
                for entry in iter {
                    let (k, v) =
                        entry.map_err(|e| MetaStoreError::IOError(format!("redb iter: {e}")))?;
                    let old_child = k.value().to_string();
                    let suffix = old_child
                        .strip_prefix(&old_prefix)
                        .map(|s| s.to_string())
                        .unwrap_or_default();
                    let new_child = format!("{}{}", new_prefix, suffix);
                    to_rewrite.push((old_child, new_child, v.value().to_vec()));
                }
            }
            for (old_key, new_key, bytes) in to_rewrite {
                let mut meta = deserialize_metadata(&bytes)?;
                meta.path = new_key.clone();
                let new_bytes = serialize_metadata(&meta);
                table
                    .remove(old_key.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb remove: {e}")))?;
                table
                    .insert(new_key.as_str(), new_bytes.as_slice())
                    .map_err(|e| MetaStoreError::IOError(format!("redb insert: {e}")))?;
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        Ok(())
    }

    fn set_file_metadata(
        &self,
        path: &str,
        key: &str,
        value: String,
    ) -> Result<(), MetaStoreError> {
        let composite = fm_composite_key(path, key);
        let txn = self
            .db
            .begin_write()
            .map_err(|e| MetaStoreError::IOError(format!("redb write txn: {e}")))?;
        {
            let mut table = txn
                .open_table(FILE_METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open fm table: {e}")))?;
            table
                .insert(composite.as_str(), value.as_bytes())
                .map_err(|e| MetaStoreError::IOError(format!("redb fm insert: {e}")))?;
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb fm commit: {e}")))?;
        Ok(())
    }

    fn get_file_metadata(&self, path: &str, key: &str) -> Result<Option<String>, MetaStoreError> {
        let composite = fm_composite_key(path, key);
        let txn = self
            .db
            .begin_read()
            .map_err(|e| MetaStoreError::IOError(format!("redb read txn: {e}")))?;
        let table = txn
            .open_table(FILE_METADATA_TABLE)
            .map_err(|e| MetaStoreError::IOError(format!("redb open fm table: {e}")))?;
        match table.get(composite.as_str()) {
            Ok(Some(guard)) => {
                let bytes = guard.value();
                let s = std::str::from_utf8(bytes).map_err(|e| {
                    MetaStoreError::IOError(format!("redb fm utf8 decode {path}/{key}: {e}"))
                })?;
                Ok(Some(s.to_string()))
            }
            Ok(None) => Ok(None),
            Err(e) => Err(MetaStoreError::IOError(format!("redb fm get: {e}"))),
        }
    }
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Binary serialize↔deserialize round-trip covers both a DT_REG
    /// entry and a DT_MOUNT entry so entry_type survives intact. Target
    /// zone is never carried by the kernel struct (federation-only —
    /// lives on the raft proto itself), so we only assert the fields
    /// the kernel tracks.
    #[test]
    fn test_serialize_roundtrip() {
        let cases = [
            FileMetadata {
                path: "/test/file.txt".to_string(),
                size: 1024,
                etag: Some("hash123".to_string()),
                version: 3,
                entry_type: 0, // DT_REG
                zone_id: Some("root".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: Some("nexus-1:2028".to_string()),
            },
            FileMetadata {
                path: "/mnt/peer".to_string(),
                size: 0,
                etag: None,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some("zone-a".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
            },
        ];
        for meta in &cases {
            let restored = deserialize_metadata(&serialize_metadata(meta)).unwrap();
            assert_eq!(restored.path, meta.path);
            assert_eq!(restored.size, meta.size);
            assert_eq!(restored.etag, meta.etag);
            assert_eq!(restored.version, meta.version);
            assert_eq!(restored.entry_type, meta.entry_type);
            assert_eq!(restored.zone_id, meta.zone_id);
            assert_eq!(restored.mime_type, meta.mime_type);
            assert_eq!(restored.last_writer_address, meta.last_writer_address);
        }
    }

    fn mk_meta(path: &str, version: u32) -> FileMetadata {
        FileMetadata {
            path: path.to_string(),
            size: 0,
            etag: None,
            version,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
        }
    }

    #[test]
    fn memory_put_if_version_vacant_accepts_zero() {
        let ms = MemoryMetaStore::new();
        let r = ms.put_if_version(mk_meta("/a", 1), 0).unwrap();
        assert!(r.success);
        assert_eq!(r.current_version, 1);
    }

    #[test]
    fn memory_put_if_version_conflict_returns_current() {
        let ms = MemoryMetaStore::new();
        ms.put("/a", mk_meta("/a", 3)).unwrap();
        let r = ms.put_if_version(mk_meta("/a", 4), 2).unwrap();
        assert!(!r.success);
        assert_eq!(r.current_version, 3);
        assert_eq!(ms.get("/a").unwrap().unwrap().version, 3);
    }

    #[test]
    fn memory_rename_path_moves_entry_and_children() {
        let ms = MemoryMetaStore::new();
        ms.put("/old", mk_meta("/old", 1)).unwrap();
        ms.put("/old/child", mk_meta("/old/child", 1)).unwrap();
        ms.put("/old/sub/deep", mk_meta("/old/sub/deep", 1))
            .unwrap();
        ms.set_file_metadata("/old/child", "tag", "value".to_string())
            .unwrap();

        ms.rename_path("/old", "/new").unwrap();

        assert!(ms.get("/old").unwrap().is_none());
        assert!(ms.get("/old/child").unwrap().is_none());
        assert!(ms.get("/old/sub/deep").unwrap().is_none());
        assert_eq!(ms.get("/new").unwrap().unwrap().path, "/new");
        assert_eq!(ms.get("/new/child").unwrap().unwrap().path, "/new/child");
        assert_eq!(
            ms.get("/new/sub/deep").unwrap().unwrap().path,
            "/new/sub/deep"
        );
        assert_eq!(
            ms.get_file_metadata("/new/child", "tag").unwrap(),
            Some("value".to_string())
        );
    }

    #[test]
    fn memory_set_and_get_file_metadata() {
        let ms = MemoryMetaStore::new();
        ms.set_file_metadata("/x", "parsed_text", "hello".to_string())
            .unwrap();
        assert_eq!(
            ms.get_file_metadata("/x", "parsed_text").unwrap(),
            Some("hello".to_string())
        );
        assert_eq!(ms.get_file_metadata("/x", "missing").unwrap(), None);
    }

    #[test]
    fn memory_is_implicit_directory() {
        let ms = MemoryMetaStore::new();
        ms.put("/dir/a", mk_meta("/dir/a", 1)).unwrap();
        assert!(ms.is_implicit_directory("/dir").unwrap());
        assert!(!ms.is_implicit_directory("/empty").unwrap());
    }

    #[test]
    fn memory_list_paginated_slices_and_returns_cursor() {
        let ms = MemoryMetaStore::new();
        for i in 0..5 {
            let p = format!("/{i:02}");
            ms.put(&p, mk_meta(&p, 1)).unwrap();
        }
        let page = ms.list_paginated("", true, 2, None).unwrap();
        assert_eq!(page.items.len(), 2);
        assert!(page.has_more);
        assert_eq!(page.total_count, 5);
        let page2 = ms
            .list_paginated("", true, 2, page.next_cursor.as_deref())
            .unwrap();
        assert_eq!(page2.items.len(), 2);
        assert!(page2.has_more);
    }

    #[test]
    fn memory_delete_clears_file_metadata() {
        let ms = MemoryMetaStore::new();
        ms.put("/x", mk_meta("/x", 1)).unwrap();
        ms.set_file_metadata("/x", "k", "v".to_string()).unwrap();
        ms.delete("/x").unwrap();
        assert_eq!(ms.get_file_metadata("/x", "k").unwrap(), None);
    }

    #[test]
    fn test_serialize_all_none() {
        let meta = FileMetadata {
            path: "/x".to_string(),
            size: 0,
            etag: None,
            version: 1,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
        };
        let data = serialize_metadata(&meta);
        let restored = deserialize_metadata(&data).unwrap();
        assert_eq!(restored.path, "/x");
        assert!(restored.etag.is_none());
        assert!(restored.zone_id.is_none());
        assert!(restored.mime_type.is_none());
    }
}
