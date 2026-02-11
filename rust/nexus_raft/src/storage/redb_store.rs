//! Embedded key-value storage using redb.
//!
//! A general-purpose embedded database wrapper that can be reused for:
//! - Raft log storage (current use case)
//! - Local persistent cache
//! - Task/event queues
//! - Session storage
//!
//! # Why redb?
//!
//! - Pure Rust: No C++ dependencies, easy cross-platform builds
//! - 1.0 Stable: Production-ready since June 2023
//! - Embedded: No network latency, works during network partitions
//! - ACID: Copy-on-write B-trees with crash safety
//! - Efficient: Predictable disk usage (unlike sled)
//!
//! # Migration from sled
//!
//! This module replaces sled (0.34, perpetual beta) with redb (2.x, stable).
//! The public API is kept identical for backward compatibility.
//! See ADR: docs/rfcs/adr-raft-sled-strategy.md
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::storage::SledStore;
//!
//! let store = SledStore::open("/tmp/mydb").unwrap();
//!
//! // Basic KV operations
//! store.set(b"key", b"value").unwrap();
//! let value = store.get(b"key").unwrap();
//!
//! // Named trees (namespaces)
//! let cache_tree = store.tree("cache").unwrap();
//! cache_tree.set(b"item:1", b"data").unwrap();
//! ```

use redb::{Database, ReadableTable, ReadableTableMetadata, TableDefinition};
use serde::{de::DeserializeOwned, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use thiserror::Error;

/// The default table name used by SledStore direct operations.
const DEFAULT_TABLE: TableDefinition<'static, &[u8], &[u8]> = TableDefinition::new("__default__");

/// Table used for generating monotonic IDs.
const ID_TABLE: TableDefinition<'static, &[u8], &[u8]> = TableDefinition::new("__id_counter__");
const ID_KEY: &[u8] = b"counter";

/// Errors that can occur during storage operations.
#[derive(Error, Debug)]
pub enum StorageError {
    #[error("database error: {0}")]
    Database(String),

    #[error("serialization error: {0}")]
    Serialization(#[from] bincode::Error),

    #[error("key not found: {0:?}")]
    NotFound(Vec<u8>),

    #[error("tree not found: {0}")]
    TreeNotFound(String),
}

pub type Result<T> = std::result::Result<T, StorageError>;

/// Helper to convert any redb error into our StorageError.
fn db_err(e: impl std::fmt::Display) -> StorageError {
    StorageError::Database(e.to_string())
}

/// Leak a string to get a `'static` reference.
///
/// redb's `TableDefinition::new` requires `&'static str`. Since we create
/// a small, fixed number of trees (typically 5-10), leaking a few bytes
/// per tree name is acceptable and simpler than a string intern pool.
fn leak_name(name: &str) -> &'static str {
    Box::leak(name.to_string().into_boxed_str())
}

/// Compute the exclusive upper bound for a prefix scan.
///
/// For prefix `b"user:"`, returns `Some(b"user;")` (last byte incremented).
/// Returns `None` if all bytes are 0xFF (scan to end).
fn prefix_upper_bound(prefix: &[u8]) -> Option<Vec<u8>> {
    let mut end = prefix.to_vec();
    while let Some(last) = end.last_mut() {
        if *last < 0xFF {
            *last += 1;
            return Some(end);
        }
        end.pop();
    }
    None // All 0xFF bytes — scan to end of keyspace
}

/// A wrapper around redb database providing a clean API for embedded storage.
///
/// This is the main entry point for using embedded storage. It manages the
/// underlying database and provides access to named trees (namespaces).
///
/// NOTE: Type is named `SledStore` for backward compatibility during migration.
/// The underlying implementation uses redb, not sled.
#[derive(Clone)]
pub struct SledStore {
    db: Arc<Database>,
    path: Option<PathBuf>,
    /// Keeps temporary directory alive for the lifetime of the store.
    _temp_dir: Option<Arc<tempfile::TempDir>>,
}

impl SledStore {
    /// Open or create a redb database at the given path.
    ///
    /// # Arguments
    ///
    /// * `path` - Path to the database file
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let path = path.as_ref();
        // Ensure parent directory exists
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| db_err(e))?;
        }
        // redb uses a single file, not a directory like sled.
        // If path is a directory, use a file inside it.
        let db_path = if path.is_dir() {
            path.join("data.redb")
        } else if path.extension().is_none() {
            // If no extension, treat as directory path for sled compat
            std::fs::create_dir_all(path).map_err(|e| db_err(e))?;
            path.join("data.redb")
        } else {
            path.to_path_buf()
        };

        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| db_err(e))?;
        }

        let db = Database::create(&db_path).map_err(|e| db_err(e))?;
        Ok(Self {
            db: Arc::new(db),
            path: Some(db_path),
            _temp_dir: None,
        })
    }

    /// Open a temporary in-memory database (for testing).
    ///
    /// Data is lost when the store is dropped.
    pub fn open_temporary() -> Result<Self> {
        let temp_dir = tempfile::TempDir::new().map_err(|e| db_err(e))?;
        let db_path = temp_dir.path().join("temp.redb");
        let db = Database::create(&db_path).map_err(|e| db_err(e))?;
        Ok(Self {
            db: Arc::new(db),
            path: Some(db_path),
            _temp_dir: Some(Arc::new(temp_dir)),
        })
    }

    /// Get or create a named tree (namespace).
    ///
    /// Trees provide isolation between different data types.
    pub fn tree(&self, name: &str) -> Result<SledTree> {
        let static_name = leak_name(name);
        let table_def = TableDefinition::<&[u8], &[u8]>::new(static_name);

        // Ensure the table exists by doing a write transaction
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let _table = write_txn.open_table(table_def).map_err(|e| db_err(e))?;
        }
        write_txn.commit().map_err(|e| db_err(e))?;

        Ok(SledTree {
            db: Arc::clone(&self.db),
            name: static_name,
        })
    }

    /// Get a value from the default tree.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let read_txn = self.db.begin_read().map_err(|e| db_err(e))?;
        match read_txn.open_table(DEFAULT_TABLE) {
            Ok(table) => Ok(table
                .get(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec())),
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(db_err(e)),
        }
    }

    /// Set a value in the default tree.
    pub fn set(&self, key: &[u8], value: &[u8]) -> Result<()> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let mut table = write_txn.open_table(DEFAULT_TABLE).map_err(|e| db_err(e))?;
            table.insert(key, value).map_err(|e| db_err(e))?;
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(())
    }

    /// Delete a key from the default tree.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        let result;
        {
            let mut table = write_txn.open_table(DEFAULT_TABLE).map_err(|e| db_err(e))?;
            result = table
                .remove(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec());
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(result)
    }

    /// Flush all pending writes to disk.
    ///
    /// In redb, commits are always durable, so this is a no-op.
    /// Kept for API compatibility with sled.
    pub fn flush(&self) -> Result<()> {
        // redb commits are always durable — nothing to flush.
        Ok(())
    }

    /// Flush asynchronously.
    ///
    /// In redb, commits are always durable, so this is a no-op.
    pub fn flush_async(&self) {
        // redb commits are always durable.
    }

    /// Get database size on disk in bytes.
    pub fn size_on_disk(&self) -> Result<u64> {
        match &self.path {
            Some(path) => std::fs::metadata(path)
                .map(|m| m.len())
                .map_err(|e| db_err(e)),
            None => Ok(0),
        }
    }

    /// Check if the database was recovered from a previous crash.
    ///
    /// redb is always crash-safe, so this always returns false.
    pub fn was_recovered(&self) -> bool {
        false
    }

    /// Generate a monotonically increasing ID.
    ///
    /// Useful for generating unique IDs without coordination.
    pub fn generate_id(&self) -> Result<u64> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        let id;
        {
            let mut table = write_txn.open_table(ID_TABLE).map_err(|e| db_err(e))?;
            let current = table
                .get(ID_KEY)
                .map_err(|e| db_err(e))?
                .map(|v| {
                    let bytes = v.value();
                    if bytes.len() == 8 {
                        u64::from_be_bytes(bytes.try_into().unwrap())
                    } else {
                        0
                    }
                })
                .unwrap_or(0);
            id = current + 1;
            table
                .insert(ID_KEY, id.to_be_bytes().as_slice())
                .map_err(|e| db_err(e))?;
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(id)
    }

    /// Get the raw redb::Database handle for advanced operations.
    pub fn raw(&self) -> &Database {
        &self.db
    }
}

/// A named tree (namespace) within a redb database.
///
/// Trees provide isolation between different types of data.
/// Each tree has its own keyspace.
///
/// NOTE: Named `SledTree` for backward compatibility. Uses redb tables internally.
#[derive(Clone)]
pub struct SledTree {
    db: Arc<Database>,
    /// Leaked `&'static str` — redb requires static table names.
    /// Acceptable because we create a small, fixed number of trees.
    name: &'static str,
}

impl SledTree {
    /// Get the table definition for this tree.
    fn table_def(&self) -> TableDefinition<'static, &'static [u8], &'static [u8]> {
        TableDefinition::new(self.name)
    }

    /// Get a value by key.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let read_txn = self.db.begin_read().map_err(|e| db_err(e))?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => Ok(table
                .get(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec())),
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(db_err(e)),
        }
    }

    /// Get a value and deserialize it from JSON.
    pub fn get_json<T: DeserializeOwned>(&self, key: &[u8]) -> Result<Option<T>> {
        match self.get(key)? {
            Some(bytes) => {
                let value: T = serde_json::from_slice(&bytes).map_err(|e| {
                    StorageError::Serialization(bincode::Error::from(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        e,
                    )))
                })?;
                Ok(Some(value))
            }
            None => Ok(None),
        }
    }

    /// Get a value and deserialize it using bincode (faster, smaller).
    pub fn get_bincode<T: DeserializeOwned>(&self, key: &[u8]) -> Result<Option<T>> {
        match self.get(key)? {
            Some(bytes) => {
                let value: T = bincode::deserialize(&bytes)?;
                Ok(Some(value))
            }
            None => Ok(None),
        }
    }

    /// Set a value by key.
    pub fn set(&self, key: &[u8], value: &[u8]) -> Result<()> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let mut table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;
            table.insert(key, value).map_err(|e| db_err(e))?;
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(())
    }

    /// Serialize and set a value using JSON.
    pub fn set_json<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let bytes = serde_json::to_vec(value).map_err(|e| {
            StorageError::Serialization(bincode::Error::from(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                e,
            )))
        })?;
        self.set(key, &bytes)
    }

    /// Serialize and set a value using bincode (faster, smaller).
    pub fn set_bincode<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let bytes = bincode::serialize(value)?;
        self.set(key, &bytes)
    }

    /// Delete a key.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        let result;
        {
            let mut table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;
            result = table
                .remove(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec());
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(result)
    }

    /// Check if a key exists.
    pub fn contains(&self, key: &[u8]) -> Result<bool> {
        Ok(self.get(key)?.is_some())
    }

    /// Get the number of entries in this tree.
    pub fn len(&self) -> usize {
        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(_) => return 0,
        };
        match read_txn.open_table(self.table_def()) {
            Ok(table) => table.len().unwrap_or(0) as usize,
            Err(_) => 0,
        }
    }

    /// Check if the tree is empty.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Get the first key-value pair.
    pub fn first(&self) -> Result<Option<(Vec<u8>, Vec<u8>)>> {
        let read_txn = self.db.begin_read().map_err(|e| db_err(e))?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => match table.first().map_err(|e| db_err(e))? {
                Some(entry) => Ok(Some((entry.0.value().to_vec(), entry.1.value().to_vec()))),
                None => Ok(None),
            },
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(db_err(e)),
        }
    }

    /// Get the last key-value pair.
    pub fn last(&self) -> Result<Option<(Vec<u8>, Vec<u8>)>> {
        let read_txn = self.db.begin_read().map_err(|e| db_err(e))?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => match table.last().map_err(|e| db_err(e))? {
                Some(entry) => Ok(Some((entry.0.value().to_vec(), entry.1.value().to_vec()))),
                None => Ok(None),
            },
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(db_err(e)),
        }
    }

    /// Create a new batch for atomic operations.
    pub fn batch(&self) -> TreeBatch {
        TreeBatch {
            db: Arc::clone(&self.db),
            name: self.name,
            batch: SledBatch::new(),
        }
    }

    /// Clear all entries in this tree.
    pub fn clear(&self) -> Result<()> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            // Delete and recreate the table to clear all entries
            let _ = write_txn.delete_table(self.table_def());
            let _table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(())
    }

    /// Iterate over all key-value pairs.
    ///
    /// NOTE: Unlike sled, this collects all entries into memory first.
    /// For large tables, consider using `scan_prefix` with pagination.
    pub fn iter(&self) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.collect_all().into_iter()
    }

    /// Collect all entries (internal helper).
    fn collect_all(&self) -> Vec<Result<(Vec<u8>, Vec<u8>)>> {
        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(db_err(e))],
        };
        match read_txn.open_table(self.table_def()) {
            Ok(table) => {
                let mut results = Vec::new();
                let iter = match table.iter() {
                    Ok(iter) => iter,
                    Err(e) => return vec![Err(db_err(e))],
                };
                for entry in iter {
                    match entry {
                        Ok(entry) => {
                            results.push(Ok((entry.0.value().to_vec(), entry.1.value().to_vec())));
                        }
                        Err(e) => {
                            results.push(Err(db_err(e)));
                        }
                    }
                }
                results
            }
            Err(redb::TableError::TableDoesNotExist(_)) => Vec::new(),
            Err(e) => vec![Err(db_err(e))],
        }
    }

    /// Iterate over keys in a range.
    pub fn range<R, K>(&self, range: R) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_
    where
        R: std::ops::RangeBounds<K>,
        K: AsRef<[u8]>,
    {
        self.collect_range(range).into_iter()
    }

    /// Collect entries in a range (internal helper).
    fn collect_range<R, K>(&self, range: R) -> Vec<Result<(Vec<u8>, Vec<u8>)>>
    where
        R: std::ops::RangeBounds<K>,
        K: AsRef<[u8]>,
    {
        use std::ops::Bound;

        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(db_err(e))],
        };
        match read_txn.open_table(self.table_def()) {
            Ok(table) => {
                // Convert bounds to owned Vec<u8> to avoid lifetime issues
                let start = match range.start_bound() {
                    Bound::Included(k) => Bound::Included(k.as_ref().to_vec()),
                    Bound::Excluded(k) => Bound::Excluded(k.as_ref().to_vec()),
                    Bound::Unbounded => Bound::Unbounded,
                };
                let end = match range.end_bound() {
                    Bound::Included(k) => Bound::Included(k.as_ref().to_vec()),
                    Bound::Excluded(k) => Bound::Excluded(k.as_ref().to_vec()),
                    Bound::Unbounded => Bound::Unbounded,
                };

                let range_bound = (
                    start.as_ref().map(|v| v.as_slice()),
                    end.as_ref().map(|v| v.as_slice()),
                );

                let iter = match table.range::<&[u8]>(range_bound) {
                    Ok(iter) => iter,
                    Err(e) => return vec![Err(db_err(e))],
                };

                let mut results = Vec::new();
                for entry in iter {
                    match entry {
                        Ok(entry) => {
                            results.push(Ok((entry.0.value().to_vec(), entry.1.value().to_vec())));
                        }
                        Err(e) => {
                            results.push(Err(db_err(e)));
                        }
                    }
                }
                results
            }
            Err(redb::TableError::TableDoesNotExist(_)) => Vec::new(),
            Err(e) => vec![Err(db_err(e))],
        }
    }

    /// Scan keys with a prefix.
    ///
    /// Implemented via range scan with computed upper bound.
    pub fn scan_prefix(
        &self,
        prefix: &[u8],
    ) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.collect_prefix(prefix).into_iter()
    }

    /// Collect entries with a prefix (internal helper).
    fn collect_prefix(&self, prefix: &[u8]) -> Vec<Result<(Vec<u8>, Vec<u8>)>> {
        use std::ops::Bound;

        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(db_err(e))],
        };
        match read_txn.open_table(self.table_def()) {
            Ok(table) => {
                let start: Bound<&[u8]> = Bound::Included(prefix);
                let upper = prefix_upper_bound(prefix);
                let range_end: Bound<&[u8]> = match &upper {
                    Some(end_bytes) => Bound::Excluded(end_bytes.as_slice()),
                    None => Bound::Unbounded,
                };

                let iter = match table.range::<&[u8]>((start, range_end)) {
                    Ok(iter) => iter,
                    Err(e) => return vec![Err(db_err(e))],
                };

                let mut results = Vec::new();
                for entry in iter {
                    match entry {
                        Ok(entry) => {
                            results.push(Ok((entry.0.value().to_vec(), entry.1.value().to_vec())));
                        }
                        Err(e) => {
                            results.push(Err(db_err(e)));
                        }
                    }
                }
                results
            }
            Err(redb::TableError::TableDoesNotExist(_)) => Vec::new(),
            Err(e) => vec![Err(db_err(e))],
        }
    }

    /// Compare-and-swap operation.
    ///
    /// Atomically sets `key` to `new` if its current value is `expected`.
    /// Returns the actual value before the operation.
    ///
    /// Implemented via a write transaction (serialized, so atomic).
    pub fn compare_and_swap(
        &self,
        key: &[u8],
        expected: Option<&[u8]>,
        new: Option<&[u8]>,
    ) -> Result<std::result::Result<(), Option<Vec<u8>>>> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let mut table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;

            let current = table
                .get(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec());

            let expected_vec = expected.map(|e| e.to_vec());

            if current == expected_vec {
                match new {
                    Some(new_val) => {
                        table.insert(key, new_val).map_err(|e| db_err(e))?;
                    }
                    None => {
                        table.remove(key).map_err(|e| db_err(e))?;
                    }
                }
            } else {
                return Ok(Err(current));
            }
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(Ok(()))
    }

    /// Fetch and update atomically.
    ///
    /// Implemented via a write transaction (serialized, so atomic).
    pub fn fetch_and_update<F>(&self, key: &[u8], mut f: F) -> Result<Option<Vec<u8>>>
    where
        F: FnMut(Option<&[u8]>) -> Option<Vec<u8>>,
    {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        let old_value;
        {
            let mut table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;

            old_value = table
                .get(key)
                .map_err(|e| db_err(e))?
                .map(|v| v.value().to_vec());

            let new_value = f(old_value.as_deref());
            match new_value {
                Some(val) => {
                    table.insert(key, val.as_slice()).map_err(|e| db_err(e))?;
                }
                None => {
                    table.remove(key).map_err(|e| db_err(e))?;
                }
            }
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(old_value)
    }

    /// Apply a batch of operations atomically.
    pub fn apply_batch(&self, batch: &SledBatch) -> Result<()> {
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let mut table = write_txn
                .open_table(self.table_def())
                .map_err(|e| db_err(e))?;
            for op in &batch.operations {
                match op {
                    BatchOp::Insert(k, v) => {
                        table
                            .insert(k.as_slice(), v.as_slice())
                            .map_err(|e| db_err(e))?;
                    }
                    BatchOp::Remove(k) => {
                        table.remove(k.as_slice()).map_err(|e| db_err(e))?;
                    }
                }
            }
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(())
    }

    /// Flush pending writes for this tree.
    ///
    /// In redb, commits are always durable, so this is a no-op.
    pub fn flush(&self) -> Result<()> {
        Ok(())
    }
}

/// Internal batch operation type.
#[derive(Debug, Clone)]
enum BatchOp {
    Insert(Vec<u8>, Vec<u8>),
    Remove(Vec<u8>),
}

/// A batch of operations to apply atomically.
pub struct SledBatch {
    operations: Vec<BatchOp>,
}

impl SledBatch {
    /// Create a new empty batch.
    pub fn new() -> Self {
        Self {
            operations: Vec::new(),
        }
    }

    /// Add an insert operation to the batch.
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        self.operations
            .push(BatchOp::Insert(key.to_vec(), value.to_vec()));
    }

    /// Add a remove operation to the batch.
    pub fn remove(&mut self, key: &[u8]) {
        self.operations.push(BatchOp::Remove(key.to_vec()));
    }
}

impl Default for SledBatch {
    fn default() -> Self {
        Self::new()
    }
}

/// A batch of operations for a specific tree.
pub struct TreeBatch {
    db: Arc<Database>,
    /// Leaked `&'static str` — shared from SledTree.
    name: &'static str,
    batch: SledBatch,
}

impl TreeBatch {
    /// Add an insert operation to the batch.
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        self.batch.insert(key, value);
    }

    /// Add a remove operation to the batch.
    pub fn remove(&mut self, key: &[u8]) {
        self.batch.remove(key);
    }

    /// Apply all operations atomically.
    pub fn apply(self) -> Result<()> {
        let table_def = TableDefinition::<&[u8], &[u8]>::new(self.name);
        let write_txn = self.db.begin_write().map_err(|e| db_err(e))?;
        {
            let mut table = write_txn.open_table(table_def).map_err(|e| db_err(e))?;
            for op in &self.batch.operations {
                match op {
                    BatchOp::Insert(k, v) => {
                        table
                            .insert(k.as_slice(), v.as_slice())
                            .map_err(|e| db_err(e))?;
                    }
                    BatchOp::Remove(k) => {
                        table.remove(k.as_slice()).map_err(|e| db_err(e))?;
                    }
                }
            }
        }
        write_txn.commit().map_err(|e| db_err(e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_operations() {
        let store = SledStore::open_temporary().unwrap();

        // Test set/get
        store.set(b"key1", b"value1").unwrap();
        assert_eq!(store.get(b"key1").unwrap(), Some(b"value1".to_vec()));

        // Test delete
        store.delete(b"key1").unwrap();
        assert_eq!(store.get(b"key1").unwrap(), None);
    }

    #[test]
    fn test_tree_operations() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("test_tree").unwrap();

        // Test set/get
        tree.set(b"key1", b"value1").unwrap();
        assert_eq!(tree.get(b"key1").unwrap(), Some(b"value1".to_vec()));

        // Test contains
        assert!(tree.contains(b"key1").unwrap());
        assert!(!tree.contains(b"nonexistent").unwrap());

        // Test len
        assert_eq!(tree.len(), 1);
    }

    #[test]
    fn test_json_serialization() {
        use serde::{Deserialize, Serialize};

        #[derive(Debug, Serialize, Deserialize, PartialEq)]
        struct TestData {
            name: String,
            value: i32,
        }

        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("json_test").unwrap();

        let data = TestData {
            name: "test".to_string(),
            value: 42,
        };

        tree.set_json(b"data", &data).unwrap();
        let retrieved: TestData = tree.get_json(b"data").unwrap().unwrap();
        assert_eq!(data, retrieved);
    }

    #[test]
    fn test_bincode_serialization() {
        use serde::{Deserialize, Serialize};

        #[derive(Debug, Serialize, Deserialize, PartialEq)]
        struct TestData {
            id: u64,
            payload: Vec<u8>,
        }

        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("bincode_test").unwrap();

        let data = TestData {
            id: 12345,
            payload: vec![1, 2, 3, 4, 5],
        };

        tree.set_bincode(b"data", &data).unwrap();
        let retrieved: TestData = tree.get_bincode(b"data").unwrap().unwrap();
        assert_eq!(data, retrieved);
    }

    #[test]
    fn test_range_scan() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("range_test").unwrap();

        // Insert entries with numeric keys (big-endian for proper ordering)
        for i in 0u64..10 {
            tree.set(&i.to_be_bytes(), format!("value_{}", i).as_bytes())
                .unwrap();
        }

        // Scan range 3..7
        let entries: Vec<_> = tree
            .range(3u64.to_be_bytes()..7u64.to_be_bytes())
            .collect::<Result<Vec<_>>>()
            .unwrap();

        assert_eq!(entries.len(), 4);
        assert_eq!(
            u64::from_be_bytes(entries[0].0.clone().try_into().unwrap()),
            3
        );
        assert_eq!(
            u64::from_be_bytes(entries[3].0.clone().try_into().unwrap()),
            6
        );
    }

    #[test]
    fn test_prefix_scan() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("prefix_test").unwrap();

        tree.set(b"user:1", b"alice").unwrap();
        tree.set(b"user:2", b"bob").unwrap();
        tree.set(b"user:3", b"charlie").unwrap();
        tree.set(b"item:1", b"book").unwrap();

        let users: Vec<_> = tree
            .scan_prefix(b"user:")
            .collect::<Result<Vec<_>>>()
            .unwrap();

        assert_eq!(users.len(), 3);
    }

    #[test]
    fn test_batch_operations() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("batch_test").unwrap();

        // Pre-insert a key to delete
        tree.set(b"to_delete", b"old").unwrap();

        // Apply batch
        let mut batch = SledBatch::new();
        batch.insert(b"key1", b"value1");
        batch.insert(b"key2", b"value2");
        batch.remove(b"to_delete");

        tree.apply_batch(&batch).unwrap();

        assert_eq!(tree.get(b"key1").unwrap(), Some(b"value1".to_vec()));
        assert_eq!(tree.get(b"key2").unwrap(), Some(b"value2".to_vec()));
        assert_eq!(tree.get(b"to_delete").unwrap(), None);
    }

    #[test]
    fn test_compare_and_swap() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("cas_test").unwrap();

        // Set initial value
        tree.set(b"counter", b"0").unwrap();

        // CAS with correct expected value
        let result = tree
            .compare_and_swap(b"counter", Some(b"0"), Some(b"1"))
            .unwrap();
        assert!(result.is_ok());
        assert_eq!(tree.get(b"counter").unwrap(), Some(b"1".to_vec()));

        // CAS with wrong expected value
        let result = tree
            .compare_and_swap(b"counter", Some(b"0"), Some(b"2"))
            .unwrap();
        assert!(result.is_err());
        assert_eq!(tree.get(b"counter").unwrap(), Some(b"1".to_vec())); // Unchanged
    }

    #[test]
    fn test_generate_id() {
        let store = SledStore::open_temporary().unwrap();

        let id1 = store.generate_id().unwrap();
        let id2 = store.generate_id().unwrap();
        let id3 = store.generate_id().unwrap();

        // IDs should be monotonically increasing
        assert!(id2 > id1);
        assert!(id3 > id2);
    }

    #[test]
    fn test_clear_tree() {
        let store = SledStore::open_temporary().unwrap();
        let tree = store.tree("clear_test").unwrap();

        tree.set(b"key1", b"value1").unwrap();
        tree.set(b"key2", b"value2").unwrap();
        assert_eq!(tree.len(), 2);

        tree.clear().unwrap();
        assert_eq!(tree.len(), 0);
        assert!(tree.is_empty());
    }
}
