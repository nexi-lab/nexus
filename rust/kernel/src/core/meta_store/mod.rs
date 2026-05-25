//! MetaStore pillar — kernel-internal concrete impls.
//!
//! The trait declaration + helper types live in
//! `crate::abc::meta_store`; this module is the home for the
//! kernel-internal *implementation*:
//!
//! * [`LocalMetaStore`] — redb-backed durable impl (~5μs reads).
//!   Used everywhere — bare-kernel boot opens one against a tempdir
//!   so tests / quickstarts have a working SSOT without explicit
//!   ``set_metastore_path``; production swaps in a real path.
//!
//! Remote / federation impls live in their respective neighbours:
//! [`remote`] (gRPC proxy) and `raft::meta_store`.

pub mod remote;

// Re-export the trait surface from `abc/` so callers writing
// `use crate::core::meta_store::{MetaStore, FileMetadata, …}` (or the
// flat `crate::meta_store::…` shim) reach the canonical declaration in
// `crate::abc::meta_store` without churn. This is a stable compat
// alias, not a parallel declaration.
pub use crate::abc::meta_store::{
    pas_update_content_id, FileMetadata, MetaStore, MetaStoreError, PaginatedList, PathEtag,
    PathValueStr, PutIfVersionResult, DT_DIR, DT_EXTERNAL_STORAGE, DT_LINK, DT_MOUNT, DT_PIPE,
    DT_REG, DT_STREAM,
};

use dashmap::DashMap;

// pas_update_content_id is defined in crate::abc::meta_store and re-exported
// above. ``LocalMetaStore`` (below) uses it via the re-export.

// ── LocalMetaStore — single-node redb-backed metastore ──────────────────
//
// "redb" is a shared implementation detail — the Raft state machine
// also uses redb underneath. The distinguishing axis is "single-node vs
// raft-replicated", captured by the Local / Zone naming pair.

use redb::{Database, ReadableDatabase, ReadableTable, TableDefinition};
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
///
/// **Internal cache** — every `MetaStore` impl backed by a slow store
/// (disk / RPC / raft) carries its own `cache: DashMap` projection of
/// hot entries.  `get` consults the cache first and populates on miss;
/// `put` commits the store first and refreshes the cache row on
/// commit success, so a failed commit can never leave a phantom hit
/// in the cache; `delete` invalidates the cache before the store
/// delete so concurrent readers cannot observe a stale hit after the
/// row is gone. Cache management is metastore-internal and
/// transparent to callers — there is no separate metadata cache that
/// callers can consult.
pub struct LocalMetaStore {
    db: Arc<Database>,
    cache: DashMap<String, FileMetadata>,
}

impl LocalMetaStore {
    /// Open or create a redb database at the given path.
    pub fn open(path: &Path) -> Result<Self, MetaStoreError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| MetaStoreError::IOError(format!("mkdir {}: {e}", parent.display())))?;
        }
        let cache_bytes = std::env::var("NEXUS_REDB_CACHE_MB")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(64)
            * 1024
            * 1024;
        let db = Database::builder()
            .set_cache_size(cache_bytes)
            .create(path)
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

        Ok(Self {
            db: Arc::new(db),
            cache: DashMap::new(),
        })
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

/// Compact binary serialization for FileMetadata (v4).
///
/// Field order: tag (u8 = 4), path, size, content_id?, version (u32),
/// entry_type (u8), zone_id?, mime_type?, created_at_ms?,
/// modified_at_ms?, last_writer_address?, target_zone_id?,
/// link_target?, gen (u64), owner_id?.
///
/// Strings carry a u32 length prefix; `Option<_>` fields are framed by
/// a 1-byte present flag (0 = absent, no payload; 1 = payload follows).
///
/// The reader also accepts tag `3` — records written before `gen`
/// landed — and substitutes `gen = 0` for those.
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

    buf.push(4); // version tag - v4 appends gen:u64 after the v3 fields.
    write_str(&mut buf, &meta.path);
    buf.extend_from_slice(&meta.size.to_le_bytes());
    write_opt_str(&mut buf, &meta.content_id);
    buf.extend_from_slice(&meta.version.to_le_bytes());
    buf.push(meta.entry_type);
    write_opt_str(&mut buf, &meta.zone_id);
    write_opt_str(&mut buf, &meta.mime_type);
    write_opt_i64(&mut buf, meta.created_at_ms);
    write_opt_i64(&mut buf, meta.modified_at_ms);
    write_opt_str(&mut buf, &meta.last_writer_address);
    write_opt_str(&mut buf, &meta.target_zone_id);
    write_opt_str(&mut buf, &meta.link_target);
    buf.extend_from_slice(&meta.gen.to_le_bytes());
    write_opt_str(&mut buf, &meta.owner_id);

    buf
}

fn deserialize_metadata(data: &[u8]) -> Result<FileMetadata, MetaStoreError> {
    if data.is_empty() {
        return Err(MetaStoreError::IOError("empty record".into()));
    }
    let tag = data[0];
    if tag != 3 && tag != 4 {
        return Err(MetaStoreError::IOError(format!(
            "unsupported FileMetadata serialization tag {tag}; expected 3 or 4"
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

    let content_id = read_opt_str(data, &mut pos)?;

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
    let target_zone_id = read_opt_str(data, &mut pos).ok().flatten();
    let link_target = read_opt_str(data, &mut pos).ok().flatten();
    let gen = if tag >= 4 {
        if pos + 8 > data.len() {
            return Err(MetaStoreError::IOError("truncated gen".into()));
        }
        let g = u64::from_le_bytes(data[pos..pos + 8].try_into().unwrap());
        pos += 8;
        g
    } else {
        0
    };
    let owner_id = read_opt_str(data, &mut pos).ok().flatten();

    Ok(FileMetadata {
        path,
        size,
        content_id,
        gen,
        version,
        entry_type,
        zone_id,
        mime_type,
        created_at_ms,
        modified_at_ms,
        target_zone_id,
        last_writer_address,
        link_target,
        owner_id,
    })
}

impl MetaStore for LocalMetaStore {
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetaStoreError> {
        // Internal cache fast path — see struct docstring for invariants.
        if let Some(cached) = self.cache.get(path) {
            return Ok(Some(cached.clone()));
        }
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
                let meta = deserialize_metadata(data)?;
                // Populate cache from store result.
                self.cache.insert(path.to_string(), meta.clone());
                Ok(Some(meta))
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
        // Write-through cache update — store commit succeeded, refresh
        // the cache row so subsequent get() observes the new value.
        self.cache.insert(path.to_string(), metadata);
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetaStoreError> {
        // Invalidate cache before store delete — concurrent readers
        // observe either "cache empty → fall through to store" or
        // "store missing" depending on race timing, but never a stale
        // hit after the store delete.
        self.cache.remove(path);
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
        // same txn via a range scan on the "path\0..." prefix. The upper
        // bound bumps the path's final byte (path + '\u{1}'), which is
        // strictly greater than any "path\0...suffix" key — the
        // alternative "start + '\u{1}'" left the range as
        // [path\0, path\0\u{1}) and missed every real "path\0key" entry
        // because letters sort after '\u{1}'.
        {
            let mut fm_table = txn
                .open_table(FILE_METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open fm table: {e}")))?;
            let start = fm_composite_key(path, "");
            let mut end = path.to_string();
            end.push('\u{1}');
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
        // Write-through cache: refresh every put row after the redb
        // commit succeeds, mirroring single-key `put`.
        for (path, meta) in items {
            self.cache.insert(path.clone(), meta.clone());
        }
        Ok(())
    }

    /// Single write transaction for all deletes — optimal for redb.
    fn delete_batch(&self, paths: &[String]) -> Result<usize, MetaStoreError> {
        // Invalidate cache up-front (same race-safety reasoning as `delete`).
        for path in paths {
            self.cache.remove(path);
        }
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
                Ok(Some(guard)) => {
                    let meta = deserialize_metadata(guard.value())?;
                    self.cache.insert(path.clone(), meta.clone());
                    results.push(Some(meta));
                }
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
        // Refresh cache only on successful CAS commit; failed CAS leaves
        // the existing cache row alone.
        if result.success {
            self.cache.insert(path, metadata);
        }
        Ok(result)
    }

    /// Single write txn: rewrite `old_path` and all children under
    /// `old_path + "/"` to their new names. Keys are rewritten in place
    /// (remove + insert) since redb has no rename primitive.
    fn rename_path(
        &self,
        old_path: &str,
        new_path: &str,
        is_pas: bool,
    ) -> Result<(), MetaStoreError> {
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
            for (old_key, new_key, bytes) in &to_rewrite {
                let mut meta = deserialize_metadata(bytes)?;
                meta.path = new_key.clone();
                if is_pas {
                    pas_update_content_id(&mut meta, old_key, new_key);
                }
                let new_bytes = serialize_metadata(&meta);
                table
                    .remove(old_key.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb remove: {e}")))?;
                table
                    .insert(new_key.as_str(), new_bytes.as_slice())
                    .map_err(|e| MetaStoreError::IOError(format!("redb insert: {e}")))?;
            }
            // Rewrite auxiliary file_metadata side-car entries the same
            // way: every "old\0k" key becomes "new\0k". Done in the same
            // write txn so rename is atomic across both tables.
            let mut fm_table = txn
                .open_table(FILE_METADATA_TABLE)
                .map_err(|e| MetaStoreError::IOError(format!("redb open fm table: {e}")))?;
            let mut fm_to_rewrite: Vec<(String, String, Vec<u8>)> = Vec::new();
            for (old_key, new_key, _) in &to_rewrite {
                let start = fm_composite_key(old_key, "");
                let mut end = old_key.clone();
                end.push('\u{1}');
                let iter = fm_table
                    .range(start.as_str()..end.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb fm range: {e}")))?;
                for entry in iter {
                    let (k, v) =
                        entry.map_err(|e| MetaStoreError::IOError(format!("redb fm iter: {e}")))?;
                    let old_fm = k.value().to_string();
                    let suffix = old_fm
                        .strip_prefix(&format!("{old_key}\0"))
                        .unwrap_or("")
                        .to_string();
                    let new_fm = fm_composite_key(new_key, &suffix);
                    fm_to_rewrite.push((old_fm, new_fm, v.value().to_vec()));
                }
            }
            for (old_fm, new_fm, bytes) in fm_to_rewrite {
                fm_table
                    .remove(old_fm.as_str())
                    .map_err(|e| MetaStoreError::IOError(format!("redb fm remove: {e}")))?;
                fm_table
                    .insert(new_fm.as_str(), bytes.as_slice())
                    .map_err(|e| MetaStoreError::IOError(format!("redb fm insert: {e}")))?;
            }
        }
        txn.commit()
            .map_err(|e| MetaStoreError::IOError(format!("redb commit: {e}")))?;
        // Invalidate any cached rows under the old name (top-level +
        // children).  Subsequent `get(new_path...)` repopulates from
        // the redb store; we deliberately do NOT pre-populate because
        // the rewritten metadata may have transformed fields (PAS
        // content_id rewrite) we'd need to mirror here.
        self.cache.remove(old_path);
        let old_prefix_for_cache = format!("{}/", old_path.trim_end_matches('/'));
        self.cache
            .retain(|k, _| !k.starts_with(&old_prefix_for_cache));
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

    #[test]
    fn serialize_roundtrip_preserves_gen() {
        let meta = FileMetadata {
            path: "/gen.txt".to_string(),
            size: 9,
            content_id: Some("hash".to_string()),
            version: 2,
            entry_type: 0,
            zone_id: Some("root".to_string()),
            mime_type: Some("text/plain".to_string()),
            created_at_ms: Some(10),
            modified_at_ms: Some(20),
            last_writer_address: Some("nexus-1:2028".to_string()),
            target_zone_id: None,
            link_target: None,
            gen: 42,
            owner_id: None,
        };

        let restored = deserialize_metadata(&serialize_metadata(&meta)).unwrap();

        assert_eq!(restored.gen, 42);
        assert_eq!(restored.path, "/gen.txt");
        assert_eq!(restored.content_id.as_deref(), Some("hash"));
    }

    #[test]
    fn deserialize_v3_metadata_defaults_gen_to_zero() {
        let meta = FileMetadata {
            path: "/old.txt".to_string(),
            size: 1,
            content_id: Some("oldhash".to_string()),
            version: 1,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
            gen: 99,
            owner_id: None,
        };
        let mut bytes = serialize_metadata(&meta);
        bytes[0] = 3;
        // Truncate gen (8 bytes) + owner_id opt-str (1 byte for None tag)
        bytes.truncate(bytes.len() - 9);

        let restored = deserialize_metadata(&bytes).unwrap();

        assert_eq!(restored.gen, 0);
    }

    #[test]
    fn deserialize_truncated_v4_generation_errors() {
        let meta = FileMetadata {
            path: "/truncated.txt".to_string(),
            size: 1,
            content_id: Some("hash".to_string()),
            version: 1,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
            gen: 7,
            owner_id: None,
        };
        let mut bytes = serialize_metadata(&meta);
        // Remove owner_id opt-str (1 byte for None tag) + last byte of gen
        bytes.truncate(bytes.len() - 2);

        let err = deserialize_metadata(&bytes).unwrap_err();

        assert!(matches!(err, MetaStoreError::IOError(msg) if msg == "truncated gen"));
    }

    /// Binary serialize↔deserialize round-trip covers both a DT_REG
    /// entry and a DT_MOUNT entry so entry_type survives intact.
    #[test]
    fn test_serialize_roundtrip() {
        let cases = [
            FileMetadata {
                path: "/test/file.txt".to_string(),
                size: 1024,
                content_id: Some("hash123".to_string()),
                gen: 0,
                version: 3,
                entry_type: 0, // DT_REG
                zone_id: Some("root".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: Some("nexus-1:2028".to_string()),
                target_zone_id: None,
                link_target: None,
                owner_id: None,
            },
            FileMetadata {
                path: "/mnt/peer".to_string(),
                size: 0,
                content_id: None,
                gen: 0,
                version: 1,
                entry_type: 2, // DT_MOUNT
                zone_id: Some("zone-a".to_string()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: Some("zone-a".to_string()),
                link_target: None,
                owner_id: None,
            },
        ];
        for meta in &cases {
            let restored = deserialize_metadata(&serialize_metadata(meta)).unwrap();
            assert_eq!(restored.path, meta.path);
            assert_eq!(restored.size, meta.size);
            assert_eq!(restored.content_id, meta.content_id);
            assert_eq!(restored.version, meta.version);
            assert_eq!(restored.entry_type, meta.entry_type);
            assert_eq!(restored.zone_id, meta.zone_id);
            assert_eq!(restored.mime_type, meta.mime_type);
            assert_eq!(restored.last_writer_address, meta.last_writer_address);
            assert_eq!(restored.target_zone_id, meta.target_zone_id);
        }
    }

    fn mk_meta(path: &str, version: u32) -> FileMetadata {
        FileMetadata {
            path: path.to_string(),
            size: 0,
            content_id: None,
            gen: 0,
            version,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
            owner_id: None,
        }
    }

    /// Open a fresh tempfile-backed ``LocalMetaStore`` for one test.
    /// The returned ``TempDir`` MUST be bound to a local with a name (or
    /// kept alive otherwise) — when it drops, redb's exclusive lock is
    /// released and the ephemeral file is unlinked.
    fn fresh_local() -> (tempfile::TempDir, LocalMetaStore) {
        let td = tempfile::tempdir().unwrap();
        let ms = LocalMetaStore::open(&td.path().join("ms.redb")).unwrap();
        (td, ms)
    }

    #[test]
    fn local_put_if_version_vacant_accepts_zero() {
        let (_td, ms) = fresh_local();
        let r = ms.put_if_version(mk_meta("/a", 1), 0).unwrap();
        assert!(r.success);
        assert_eq!(r.current_version, 1);
    }

    #[test]
    fn local_put_if_version_conflict_returns_current() {
        let (_td, ms) = fresh_local();
        ms.put("/a", mk_meta("/a", 3)).unwrap();
        let r = ms.put_if_version(mk_meta("/a", 4), 2).unwrap();
        assert!(!r.success);
        assert_eq!(r.current_version, 3);
        assert_eq!(ms.get("/a").unwrap().unwrap().version, 3);
    }

    #[test]
    fn local_rename_path_moves_entry_and_children() {
        let (_td, ms) = fresh_local();
        ms.put("/old", mk_meta("/old", 1)).unwrap();
        ms.put("/old/child", mk_meta("/old/child", 1)).unwrap();
        ms.put("/old/sub/deep", mk_meta("/old/sub/deep", 1))
            .unwrap();
        ms.set_file_metadata("/old/child", "tag", "value".to_string())
            .unwrap();

        ms.rename_path("/old", "/new", true).unwrap();

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
    fn local_set_and_get_file_metadata() {
        let (_td, ms) = fresh_local();
        ms.set_file_metadata("/x", "parsed_text", "hello".to_string())
            .unwrap();
        assert_eq!(
            ms.get_file_metadata("/x", "parsed_text").unwrap(),
            Some("hello".to_string())
        );
        assert_eq!(ms.get_file_metadata("/x", "missing").unwrap(), None);
    }

    #[test]
    fn local_is_implicit_directory() {
        let (_td, ms) = fresh_local();
        ms.put("/dir/a", mk_meta("/dir/a", 1)).unwrap();
        assert!(ms.is_implicit_directory("/dir").unwrap());
        assert!(!ms.is_implicit_directory("/empty").unwrap());
    }

    #[test]
    fn local_list_paginated_slices_and_returns_cursor() {
        let (_td, ms) = fresh_local();
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
    fn local_delete_clears_file_metadata() {
        let (_td, ms) = fresh_local();
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
            content_id: None,
            gen: 0,
            version: 1,
            entry_type: 0,
            zone_id: None,
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
            owner_id: None,
        };
        let data = serialize_metadata(&meta);
        let restored = deserialize_metadata(&data).unwrap();
        assert_eq!(restored.path, "/x");
        assert!(restored.content_id.is_none());
        assert!(restored.zone_id.is_none());
        assert!(restored.mime_type.is_none());
    }
}
