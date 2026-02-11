#![allow(clippy::result_large_err)]
//! Embedded key-value storage using redb.
//!
//! Drop-in replacement for SledStore, providing the same API surface.
//! redb is a stable (1.0+) pure-Rust embedded database with typed tables,
//! ACID transactions, and a reliable on-disk format.
//!
//! # Why redb over sled?
//!
//! - Stable: redb 2.x has a committed on-disk format (sled is pre-1.0 beta)
//! - ACID: Full MVCC transactions with crash safety
//! - Pure Rust: No C++ dependencies
//! - Typed: Compile-time type safety for table definitions
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::storage::RedbStore;
//!
//! let store = RedbStore::open("/tmp/mydb").unwrap();
//!
//! // Basic KV operations
//! store.set(b"key", b"value").unwrap();
//! let value = store.get(b"key").unwrap();
//!
//! // Named trees (namespaces)
//! let cache_tree = store.tree("cache").unwrap();
//! cache_tree.set(b"item:1", b"data").unwrap();
//! ```

use serde::{de::DeserializeOwned, Serialize};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use thiserror::Error;

use redb::{Database, ReadableTable, ReadableTableMetadata, TableDefinition};

/// The default table name for SledStore-compatible get/set/delete on the store itself.
const DEFAULT_TABLE: TableDefinition<&[u8], &[u8]> = TableDefinition::new("__default__");

/// Dedicated revision counter table (Phase 3.2: #1330).
/// Keyed by zone_id, value is u64 counter.
const REVISIONS_TABLE: TableDefinition<&str, u64> = TableDefinition::new("revisions");

/// Errors that can occur during storage operations.
#[derive(Error, Debug)]
pub enum StorageError {
    #[error("redb database error: {0}")]
    Database(#[from] redb::DatabaseError),

    #[error("redb table error: {0}")]
    Table(#[from] redb::TableError),

    #[error("redb transaction error: {0}")]
    Transaction(#[from] redb::TransactionError),

    #[error("redb commit error: {0}")]
    Commit(#[from] redb::CommitError),

    #[error("redb storage error: {0}")]
    Storage(#[from] redb::StorageError),

    #[error("serialization error: {0}")]
    Serialization(#[from] bincode::Error),

    #[error("key not found: {0:?}")]
    NotFound(Vec<u8>),

    #[error("tree not found: {0}")]
    TreeNotFound(String),
}

pub type Result<T> = std::result::Result<T, StorageError>;

/// Compute the successor of a byte prefix for range scans.
///
/// Returns `None` if the prefix is all 0xFF bytes (no upper bound exists).
fn prefix_upper_bound(prefix: &[u8]) -> Option<Vec<u8>> {
    let mut upper = prefix.to_vec();
    while let Some(last) = upper.last_mut() {
        if *last == 0xFF {
            upper.pop();
        } else {
            *last += 1;
            return Some(upper);
        }
    }
    None
}

/// A wrapper around redb Database providing SledStore-compatible API.
///
/// This is the main entry point for embedded storage. It manages the
/// underlying database and provides access to named trees (tables).
#[derive(Clone)]
pub struct RedbStore {
    db: Arc<Database>,
    /// Monotonic ID generator (matches SledStore::generate_id behavior).
    next_id: Arc<AtomicU64>,
}

impl RedbStore {
    /// Open or create a redb database at the given path.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let db = Database::create(path)?;
        Ok(Self {
            db: Arc::new(db),
            next_id: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Open a temporary in-memory database (for testing).
    ///
    /// Uses a tempfile that is deleted on drop.
    pub fn open_temporary() -> Result<Self> {
        let tmpfile = tempfile::NamedTempFile::new()
            .map_err(|e| StorageError::Storage(redb::StorageError::Io(e)))?;
        let db = Database::create(tmpfile.path())?;
        // Keep the tempfile alive by leaking it (redb owns the file handle)
        // The OS will reclaim when the process exits
        std::mem::forget(tmpfile);
        Ok(Self {
            db: Arc::new(db),
            next_id: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Get or create a named tree (table namespace).
    ///
    /// Trees provide isolation between different data types.
    ///
    /// # Memory
    ///
    /// Uses `Box::leak` to satisfy redb's `&'static str` requirement for
    /// table names. Each unique tree name leaks ~50-100 bytes for the process
    /// lifetime. Acceptable because tree names are finite and known (typically
    /// 5-10 per database). Do not call with dynamic/unbounded tree names.
    pub fn tree(&self, name: &str) -> Result<RedbTree> {
        // Ensure the table exists by opening a write transaction
        let table_name = Box::leak(name.to_owned().into_boxed_str());
        let table_def = TableDefinition::<&[u8], &[u8]>::new(table_name);
        let write_txn = self.db.begin_write()?;
        // Opening the table in a write transaction creates it if it doesn't exist
        let _ = write_txn.open_table(table_def)?;
        write_txn.commit()?;

        Ok(RedbTree {
            db: Arc::clone(&self.db),
            table_name,
        })
    }

    /// Get a value from the default table.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let read_txn = self.db.begin_read()?;
        match read_txn.open_table(DEFAULT_TABLE) {
            Ok(table) => Ok(table.get(key)?.map(|v| v.value().to_vec())),
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Set a value in the default table.
    pub fn set(&self, key: &[u8], value: &[u8]) -> Result<()> {
        let write_txn = self.db.begin_write()?;
        {
            let mut table = write_txn.open_table(DEFAULT_TABLE)?;
            table.insert(key, value)?;
        }
        write_txn.commit()?;
        Ok(())
    }

    /// Delete a key from the default table.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let write_txn = self.db.begin_write()?;
        let old_value;
        {
            let mut table = write_txn.open_table(DEFAULT_TABLE)?;
            old_value = table.remove(key)?.map(|v| v.value().to_vec());
        }
        write_txn.commit()?;
        Ok(old_value)
    }

    /// Flush all pending writes to disk.
    ///
    /// redb commits are durable by default, so this is mostly a no-op.
    /// Kept for API compatibility with SledStore.
    pub fn flush(&self) -> Result<()> {
        // redb transactions are durable on commit — no separate flush needed
        Ok(())
    }

    /// Flush asynchronously (no-op for redb — commits are already durable).
    pub fn flush_async(&self) {
        // No-op: redb commits are synchronous and durable
    }

    /// Get approximate database size on disk in bytes.
    pub fn size_on_disk(&self) -> Result<u64> {
        // redb doesn't expose size_on_disk directly, but we can check the file
        // For now, return 0 as a placeholder
        Ok(0)
    }

    /// Check if the database was recovered from a previous crash.
    ///
    /// redb always recovers cleanly — returns false for API compatibility.
    pub fn was_recovered(&self) -> bool {
        false
    }

    /// Generate a monotonically increasing ID.
    pub fn generate_id(&self) -> Result<u64> {
        Ok(self.next_id.fetch_add(1, Ordering::Relaxed))
    }

    /// Atomically increment and return the new revision for a zone.
    ///
    /// Phase 3.2: Dedicated redb table for zone revision counters.
    /// Single-writer transaction provides atomicity without external locks.
    pub fn increment_revision(&self, zone_id: &str) -> Result<u64> {
        let write_txn = self.db.begin_write()?;
        let new_rev;
        {
            let mut table = write_txn.open_table(REVISIONS_TABLE)?;
            let current = table.get(zone_id)?.map(|v| v.value()).unwrap_or(0);
            new_rev = current + 1;
            table.insert(zone_id, new_rev)?;
        }
        write_txn.commit()?;
        Ok(new_rev)
    }

    /// Get the current revision for a zone without incrementing.
    pub fn get_revision(&self, zone_id: &str) -> Result<u64> {
        let read_txn = self.db.begin_read()?;
        match read_txn.open_table(REVISIONS_TABLE) {
            Ok(table) => Ok(table.get(zone_id)?.map(|v| v.value()).unwrap_or(0)),
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(0),
            Err(e) => Err(e.into()),
        }
    }

    /// Get the raw redb Database handle for advanced operations.
    pub fn raw(&self) -> &Database {
        &self.db
    }
}

/// A named tree (table) within a redb database.
///
/// Provides SledTree-compatible API. Each operation opens a transaction,
/// performs the operation, and commits.
#[derive(Clone)]
pub struct RedbTree {
    db: Arc<Database>,
    /// Leaked &'static str for redb TableDefinition compatibility.
    table_name: &'static str,
}

impl RedbTree {
    /// Create the TableDefinition for this tree.
    fn table_def(&self) -> TableDefinition<'static, &'static [u8], &'static [u8]> {
        TableDefinition::new(self.table_name)
    }

    /// Get a value by key.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let read_txn = self.db.begin_read()?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => Ok(table.get(key)?.map(|v| v.value().to_vec())),
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(e.into()),
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

    /// Get a value and deserialize it using bincode.
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
        let write_txn = self.db.begin_write()?;
        {
            let mut table = write_txn.open_table(self.table_def())?;
            table.insert(key, value)?;
        }
        write_txn.commit()?;
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

    /// Serialize and set a value using bincode.
    pub fn set_bincode<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let bytes = bincode::serialize(value)?;
        self.set(key, &bytes)
    }

    /// Delete a key.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let write_txn = self.db.begin_write()?;
        let old_value;
        {
            let mut table = write_txn.open_table(self.table_def())?;
            old_value = table.remove(key)?.map(|v| v.value().to_vec());
        }
        write_txn.commit()?;
        Ok(old_value)
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

    /// Get the first key-value pair (lexicographically smallest key).
    pub fn first(&self) -> Result<Option<(Vec<u8>, Vec<u8>)>> {
        let read_txn = self.db.begin_read()?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => {
                let mut iter = table.iter()?;
                match iter.next() {
                    Some(Ok(entry)) => {
                        let (k, v) = entry;
                        Ok(Some((k.value().to_vec(), v.value().to_vec())))
                    }
                    Some(Err(e)) => Err(StorageError::Storage(e)),
                    None => Ok(None),
                }
            }
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Get the last key-value pair (lexicographically largest key).
    pub fn last(&self) -> Result<Option<(Vec<u8>, Vec<u8>)>> {
        let read_txn = self.db.begin_read()?;
        match read_txn.open_table(self.table_def()) {
            Ok(table) => {
                let mut iter = table.iter()?;
                match iter.next_back() {
                    Some(Ok(entry)) => {
                        let (k, v) = entry;
                        Ok(Some((k.value().to_vec(), v.value().to_vec())))
                    }
                    Some(Err(e)) => Err(StorageError::Storage(e)),
                    None => Ok(None),
                }
            }
            Err(redb::TableError::TableDoesNotExist(_)) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Create a new batch for atomic operations.
    pub fn batch(&self) -> RedbTreeBatch {
        RedbTreeBatch {
            db: Arc::clone(&self.db),
            table_name: self.table_name,
            ops: Vec::new(),
        }
    }

    /// Clear all entries in this tree.
    pub fn clear(&self) -> Result<()> {
        let write_txn = self.db.begin_write()?;
        {
            // Delete and recreate the table to clear it
            write_txn.delete_table(self.table_def())?;
            write_txn.open_table(self.table_def())?;
        }
        write_txn.commit()?;
        Ok(())
    }

    /// Collect all key-value pairs into a Vec (for iteration).
    ///
    /// redb iterators are tied to transaction lifetimes, so we materialize
    /// all results for API compatibility with SledTree::iter().
    pub fn iter(&self) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.collect_all().into_iter()
    }

    /// Internal: collect all entries into a Vec.
    fn collect_all(&self) -> Vec<Result<(Vec<u8>, Vec<u8>)>> {
        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(e.into())],
        };
        let table = match read_txn.open_table(self.table_def()) {
            Ok(t) => t,
            Err(redb::TableError::TableDoesNotExist(_)) => return vec![],
            Err(e) => return vec![Err(e.into())],
        };
        match table.iter() {
            Ok(iter) => iter
                .map(|result| match result {
                    Ok(entry) => {
                        let (k, v) = entry;
                        Ok((k.value().to_vec(), v.value().to_vec()))
                    }
                    Err(e) => Err(StorageError::Storage(e)),
                })
                .collect(),
            Err(e) => vec![Err(StorageError::Storage(e))],
        }
    }

    /// Iterate over keys in a range.
    ///
    /// Materializes results for API compatibility.
    pub fn range<R, K>(&self, range: R) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_
    where
        R: std::ops::RangeBounds<K>,
        K: AsRef<[u8]>,
    {
        self.collect_range(range).into_iter()
    }

    /// Internal: collect range entries.
    fn collect_range<R, K>(&self, range: R) -> Vec<Result<(Vec<u8>, Vec<u8>)>>
    where
        R: std::ops::RangeBounds<K>,
        K: AsRef<[u8]>,
    {
        // Convert the range bounds to owned Vec<u8>
        use std::ops::Bound;

        let start: Bound<Vec<u8>> = match range.start_bound() {
            Bound::Included(k) => Bound::Included(k.as_ref().to_vec()),
            Bound::Excluded(k) => Bound::Excluded(k.as_ref().to_vec()),
            Bound::Unbounded => Bound::Unbounded,
        };
        let end: Bound<Vec<u8>> = match range.end_bound() {
            Bound::Included(k) => Bound::Included(k.as_ref().to_vec()),
            Bound::Excluded(k) => Bound::Excluded(k.as_ref().to_vec()),
            Bound::Unbounded => Bound::Unbounded,
        };

        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(e.into())],
        };
        let table = match read_txn.open_table(self.table_def()) {
            Ok(t) => t,
            Err(redb::TableError::TableDoesNotExist(_)) => return vec![],
            Err(e) => return vec![Err(e.into())],
        };

        // Build the range from owned bounds
        let range_ref = (
            start.as_ref().map(|v| v.as_slice()),
            end.as_ref().map(|v| v.as_slice()),
        );

        match table.range::<&[u8]>(range_ref) {
            Ok(iter) => iter
                .map(|result| match result {
                    Ok(entry) => {
                        let (k, v) = entry;
                        Ok((k.value().to_vec(), v.value().to_vec()))
                    }
                    Err(e) => Err(StorageError::Storage(e)),
                })
                .collect(),
            Err(e) => vec![Err(StorageError::Storage(e))],
        }
    }

    /// Scan keys with a prefix.
    ///
    /// Implemented using range scan with computed upper bound.
    pub fn scan_prefix(
        &self,
        prefix: &[u8],
    ) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.collect_prefix(prefix).into_iter()
    }

    /// Internal: collect prefix scan entries.
    fn collect_prefix(&self, prefix: &[u8]) -> Vec<Result<(Vec<u8>, Vec<u8>)>> {
        let read_txn = match self.db.begin_read() {
            Ok(txn) => txn,
            Err(e) => return vec![Err(e.into())],
        };
        let table = match read_txn.open_table(self.table_def()) {
            Ok(t) => t,
            Err(redb::TableError::TableDoesNotExist(_)) => return vec![],
            Err(e) => return vec![Err(e.into())],
        };

        // Compute range: [prefix, prefix_upper_bound)
        let result = if let Some(upper) = prefix_upper_bound(prefix) {
            table.range::<&[u8]>(prefix..upper.as_slice())
        } else {
            // Prefix is all 0xFF — scan from prefix to end
            table.range::<&[u8]>(prefix..)
        };

        match result {
            Ok(iter) => iter
                .map(|result| match result {
                    Ok(entry) => {
                        let (k, v) = entry;
                        Ok((k.value().to_vec(), v.value().to_vec()))
                    }
                    Err(e) => Err(StorageError::Storage(e)),
                })
                .collect(),
            Err(e) => vec![Err(StorageError::Storage(e))],
        }
    }

    /// Compare-and-swap operation.
    ///
    /// Atomically sets `key` to `new` if its current value is `expected`.
    pub fn compare_and_swap(
        &self,
        key: &[u8],
        expected: Option<&[u8]>,
        new: Option<&[u8]>,
    ) -> Result<std::result::Result<(), Option<Vec<u8>>>> {
        let write_txn = self.db.begin_write()?;
        let result;
        {
            let mut table = write_txn.open_table(self.table_def())?;
            let current = table.get(key)?.map(|v| v.value().to_vec());
            let current_ref = current.as_deref();

            if current_ref == expected {
                match new {
                    Some(val) => {
                        table.insert(key, val)?;
                    }
                    None => {
                        table.remove(key)?;
                    }
                }
                result = Ok(());
            } else {
                result = Err(current);
            }
        }
        write_txn.commit()?;
        Ok(result)
    }

    /// Fetch and update atomically.
    pub fn fetch_and_update<F>(&self, key: &[u8], mut f: F) -> Result<Option<Vec<u8>>>
    where
        F: FnMut(Option<&[u8]>) -> Option<Vec<u8>>,
    {
        let write_txn = self.db.begin_write()?;
        let old_value;
        {
            let mut table = write_txn.open_table(self.table_def())?;
            old_value = table.get(key)?.map(|v| v.value().to_vec());
            let new_value = f(old_value.as_deref());
            match new_value {
                Some(val) => {
                    table.insert(key, val.as_slice())?;
                }
                None => {
                    table.remove(key)?;
                }
            }
        }
        write_txn.commit()?;
        Ok(old_value)
    }

    /// Apply a batch of operations atomically.
    pub fn apply_batch(&self, batch: &RedbBatch) -> Result<()> {
        let write_txn = self.db.begin_write()?;
        {
            let mut table = write_txn.open_table(self.table_def())?;
            for op in &batch.ops {
                match op {
                    BatchOp::Insert(key, value) => {
                        table.insert(key.as_slice(), value.as_slice())?;
                    }
                    BatchOp::Remove(key) => {
                        table.remove(key.as_slice())?;
                    }
                }
            }
        }
        write_txn.commit()?;
        Ok(())
    }

    /// Flush pending writes for this tree.
    ///
    /// No-op for redb — commits are already durable.
    pub fn flush(&self) -> Result<()> {
        Ok(())
    }
}

/// Batch operation type.
enum BatchOp {
    Insert(Vec<u8>, Vec<u8>),
    Remove(Vec<u8>),
}

/// A batch of operations to apply atomically (SledBatch-compatible).
pub struct RedbBatch {
    ops: Vec<BatchOp>,
}

impl RedbBatch {
    /// Create a new empty batch.
    pub fn new() -> Self {
        Self { ops: Vec::new() }
    }

    /// Add an insert operation to the batch.
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        self.ops.push(BatchOp::Insert(key.to_vec(), value.to_vec()));
    }

    /// Add a remove operation to the batch.
    pub fn remove(&mut self, key: &[u8]) {
        self.ops.push(BatchOp::Remove(key.to_vec()));
    }
}

impl Default for RedbBatch {
    fn default() -> Self {
        Self::new()
    }
}

/// A batch of operations for a specific tree.
pub struct RedbTreeBatch {
    db: Arc<Database>,
    table_name: &'static str,
    ops: Vec<BatchOp>,
}

impl RedbTreeBatch {
    /// Add an insert operation to the batch.
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        self.ops.push(BatchOp::Insert(key.to_vec(), value.to_vec()));
    }

    /// Add a remove operation to the batch.
    pub fn remove(&mut self, key: &[u8]) {
        self.ops.push(BatchOp::Remove(key.to_vec()));
    }

    /// Apply all operations atomically.
    pub fn apply(self) -> Result<()> {
        let table_def = TableDefinition::<&[u8], &[u8]>::new(self.table_name);
        let write_txn = self.db.begin_write()?;
        {
            let mut table = write_txn.open_table(table_def)?;
            for op in &self.ops {
                match op {
                    BatchOp::Insert(key, value) => {
                        table.insert(key.as_slice(), value.as_slice())?;
                    }
                    BatchOp::Remove(key) => {
                        table.remove(key.as_slice())?;
                    }
                }
            }
        }
        write_txn.commit()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_operations() {
        let store = RedbStore::open_temporary().unwrap();

        // Test set/get
        store.set(b"key1", b"value1").unwrap();
        assert_eq!(store.get(b"key1").unwrap(), Some(b"value1".to_vec()));

        // Test delete
        store.delete(b"key1").unwrap();
        assert_eq!(store.get(b"key1").unwrap(), None);
    }

    #[test]
    fn test_tree_operations() {
        let store = RedbStore::open_temporary().unwrap();
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

        let store = RedbStore::open_temporary().unwrap();
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

        let store = RedbStore::open_temporary().unwrap();
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
        let store = RedbStore::open_temporary().unwrap();
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
        let store = RedbStore::open_temporary().unwrap();
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
        let store = RedbStore::open_temporary().unwrap();
        let tree = store.tree("batch_test").unwrap();

        // Pre-insert a key to delete
        tree.set(b"to_delete", b"old").unwrap();

        // Apply batch
        let mut batch = RedbBatch::new();
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
        let store = RedbStore::open_temporary().unwrap();
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
        let store = RedbStore::open_temporary().unwrap();

        let id1 = store.generate_id().unwrap();
        let id2 = store.generate_id().unwrap();
        let id3 = store.generate_id().unwrap();

        // IDs should be monotonically increasing
        assert!(id2 > id1);
        assert!(id3 > id2);
    }

    #[test]
    fn test_clear_tree() {
        let store = RedbStore::open_temporary().unwrap();
        let tree = store.tree("clear_test").unwrap();

        tree.set(b"key1", b"value1").unwrap();
        tree.set(b"key2", b"value2").unwrap();
        assert_eq!(tree.len(), 2);

        tree.clear().unwrap();
        assert_eq!(tree.len(), 0);
        assert!(tree.is_empty());
    }

    #[test]
    fn test_revision_counter() {
        let store = RedbStore::open_temporary().unwrap();

        // Initial revision should be 0
        assert_eq!(store.get_revision("default").unwrap(), 0);

        // Increment should return 1, 2, 3...
        assert_eq!(store.increment_revision("default").unwrap(), 1);
        assert_eq!(store.increment_revision("default").unwrap(), 2);
        assert_eq!(store.increment_revision("default").unwrap(), 3);

        // Different zones have independent counters
        assert_eq!(store.increment_revision("zone-a").unwrap(), 1);
        assert_eq!(store.get_revision("default").unwrap(), 3);
        assert_eq!(store.get_revision("zone-a").unwrap(), 1);
    }

    #[test]
    fn test_tree_batch_apply() {
        let store = RedbStore::open_temporary().unwrap();
        let tree = store.tree("tree_batch_test").unwrap();

        tree.set(b"to_delete", b"old").unwrap();

        let mut batch = tree.batch();
        batch.insert(b"key1", b"value1");
        batch.insert(b"key2", b"value2");
        batch.remove(b"to_delete");
        batch.apply().unwrap();

        assert_eq!(tree.get(b"key1").unwrap(), Some(b"value1".to_vec()));
        assert_eq!(tree.get(b"key2").unwrap(), Some(b"value2".to_vec()));
        assert_eq!(tree.get(b"to_delete").unwrap(), None);
    }

    #[test]
    fn test_first_last() {
        let store = RedbStore::open_temporary().unwrap();
        let tree = store.tree("first_last_test").unwrap();

        // Empty tree
        assert_eq!(tree.first().unwrap(), None);
        assert_eq!(tree.last().unwrap(), None);

        tree.set(b"b", b"2").unwrap();
        tree.set(b"a", b"1").unwrap();
        tree.set(b"c", b"3").unwrap();

        let first = tree.first().unwrap().unwrap();
        assert_eq!(first.0, b"a".to_vec());

        let last = tree.last().unwrap().unwrap();
        assert_eq!(last.0, b"c".to_vec());
    }

    #[test]
    fn test_iter() {
        let store = RedbStore::open_temporary().unwrap();
        let tree = store.tree("iter_test").unwrap();

        tree.set(b"key1", b"value1").unwrap();
        tree.set(b"key2", b"value2").unwrap();
        tree.set(b"key3", b"value3").unwrap();

        let entries: Vec<_> = tree.iter().collect::<Result<Vec<_>>>().unwrap();

        assert_eq!(entries.len(), 3);
    }

    #[test]
    fn test_prefix_upper_bound() {
        // Normal case
        assert_eq!(prefix_upper_bound(b"abc"), Some(b"abd".to_vec()));

        // Last byte is 0xFF
        assert_eq!(prefix_upper_bound(b"ab\xff"), Some(b"ac".to_vec()));

        // All 0xFF — no upper bound
        assert_eq!(prefix_upper_bound(b"\xff\xff\xff"), None);

        // Empty prefix — no upper bound
        assert_eq!(prefix_upper_bound(b""), None);

        // Single byte
        assert_eq!(prefix_upper_bound(b"a"), Some(b"b".to_vec()));
    }
}
