//! Embedded key-value storage using sled.
//!
//! A general-purpose embedded database wrapper that can be reused for:
//! - Raft log storage (current use case)
//! - Local persistent cache
//! - Task/event queues
//! - Session storage
//!
//! # Why sled?
//!
//! - Pure Rust: No C++ dependencies, easy cross-platform builds
//! - Embedded: No network latency, works during network partitions
//! - ACID: Crash-safe with write-ahead logging
//! - Fast: Lock-free concurrent reads, batch writes
//!
//! # Example
//!
//! ```rust,no_run
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

use serde::{de::DeserializeOwned, Serialize};
use std::path::Path;
use thiserror::Error;

/// Errors that can occur during storage operations.
#[derive(Error, Debug)]
pub enum StorageError {
    #[error("sled error: {0}")]
    Sled(#[from] sled::Error),

    #[error("serialization error: {0}")]
    Serialization(#[from] bincode::Error),

    #[error("key not found: {0:?}")]
    NotFound(Vec<u8>),

    #[error("tree not found: {0}")]
    TreeNotFound(String),
}

pub type Result<T> = std::result::Result<T, StorageError>;

/// A wrapper around sled database providing a clean API for embedded storage.
///
/// This is the main entry point for using sled storage. It manages the
/// underlying database and provides access to named trees (namespaces).
#[derive(Clone)]
pub struct SledStore {
    db: sled::Db,
}

impl SledStore {
    /// Open or create a sled database at the given path.
    ///
    /// # Arguments
    ///
    /// * `path` - Path to the database directory
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::SledStore;
    ///
    /// let store = SledStore::open("/var/lib/nexus/raft").unwrap();
    /// ```
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let db = sled::open(path)?;
        Ok(Self { db })
    }

    /// Open a sled database with custom configuration.
    ///
    /// # Arguments
    ///
    /// * `config` - sled::Config for custom settings
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::SledStore;
    ///
    /// let config = sled::Config::new()
    ///     .path("/var/lib/nexus/raft")
    ///     .cache_capacity(1024 * 1024 * 64)  // 64MB cache
    ///     .flush_every_ms(Some(1000));       // Flush every second
    ///
    /// let store = SledStore::open_with_config(config).unwrap();
    /// ```
    pub fn open_with_config(config: sled::Config) -> Result<Self> {
        let db = config.open()?;
        Ok(Self { db })
    }

    /// Open a temporary in-memory database (for testing).
    ///
    /// Data is lost when the store is dropped.
    pub fn open_temporary() -> Result<Self> {
        let db = sled::Config::new().temporary(true).open()?;
        Ok(Self { db })
    }

    /// Get or create a named tree (namespace).
    ///
    /// Trees provide isolation between different data types.
    ///
    /// # Arguments
    ///
    /// * `name` - Name of the tree
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::SledStore;
    ///
    /// let store = SledStore::open_temporary().unwrap();
    ///
    /// // Separate trees for different data
    /// let raft_log = store.tree("raft_log").unwrap();
    /// let raft_meta = store.tree("raft_meta").unwrap();
    /// let cache = store.tree("cache").unwrap();
    /// ```
    pub fn tree(&self, name: &str) -> Result<SledTree> {
        let tree = self.db.open_tree(name)?;
        Ok(SledTree { tree })
    }

    /// Get a value from the default tree.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        Ok(self.db.get(key)?.map(|v| v.to_vec()))
    }

    /// Set a value in the default tree.
    pub fn set(&self, key: &[u8], value: &[u8]) -> Result<()> {
        self.db.insert(key, value)?;
        Ok(())
    }

    /// Delete a key from the default tree.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        Ok(self.db.remove(key)?.map(|v| v.to_vec()))
    }

    /// Flush all pending writes to disk.
    ///
    /// This is called automatically on drop, but can be called manually
    /// for explicit durability guarantees.
    pub fn flush(&self) -> Result<()> {
        self.db.flush()?;
        Ok(())
    }

    /// Flush asynchronously (returns immediately, flush happens in background).
    pub fn flush_async(&self) -> Result<()> {
        self.db.flush_async()?;
        Ok(())
    }

    /// Get database size on disk in bytes.
    pub fn size_on_disk(&self) -> Result<u64> {
        Ok(self.db.size_on_disk()?)
    }

    /// Check if the database was recovered from a previous crash.
    pub fn was_recovered(&self) -> bool {
        self.db.was_recovered()
    }

    /// Generate a monotonically increasing ID.
    ///
    /// Useful for generating unique IDs without coordination.
    pub fn generate_id(&self) -> Result<u64> {
        Ok(self.db.generate_id()?)
    }

    /// Get the raw sled::Db handle for advanced operations.
    pub fn raw(&self) -> &sled::Db {
        &self.db
    }
}

/// A named tree (namespace) within a sled database.
///
/// Trees provide isolation between different types of data.
/// Each tree has its own keyspace.
#[derive(Clone)]
pub struct SledTree {
    tree: sled::Tree,
}

impl SledTree {
    /// Get a value by key.
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        Ok(self.tree.get(key)?.map(|v| v.to_vec()))
    }

    /// Get a value and deserialize it.
    pub fn get_json<T: DeserializeOwned>(&self, key: &[u8]) -> Result<Option<T>> {
        match self.tree.get(key)? {
            Some(bytes) => {
                let value: T = serde_json::from_slice(&bytes)
                    .map_err(|e| StorageError::Serialization(bincode::Error::from(
                        std::io::Error::new(std::io::ErrorKind::InvalidData, e)
                    )))?;
                Ok(Some(value))
            }
            None => Ok(None),
        }
    }

    /// Get a value and deserialize it using bincode (faster, smaller).
    pub fn get_bincode<T: DeserializeOwned>(&self, key: &[u8]) -> Result<Option<T>> {
        match self.tree.get(key)? {
            Some(bytes) => {
                let value: T = bincode::deserialize(&bytes)?;
                Ok(Some(value))
            }
            None => Ok(None),
        }
    }

    /// Set a value by key.
    pub fn set(&self, key: &[u8], value: &[u8]) -> Result<()> {
        self.tree.insert(key, value)?;
        Ok(())
    }

    /// Serialize and set a value using JSON.
    pub fn set_json<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let bytes = serde_json::to_vec(value)
            .map_err(|e| StorageError::Serialization(bincode::Error::from(
                std::io::Error::new(std::io::ErrorKind::InvalidData, e)
            )))?;
        self.tree.insert(key, bytes)?;
        Ok(())
    }

    /// Serialize and set a value using bincode (faster, smaller).
    pub fn set_bincode<T: Serialize>(&self, key: &[u8], value: &T) -> Result<()> {
        let bytes = bincode::serialize(value)?;
        self.tree.insert(key, bytes)?;
        Ok(())
    }

    /// Delete a key.
    pub fn delete(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        Ok(self.tree.remove(key)?.map(|v| v.to_vec()))
    }

    /// Check if a key exists.
    pub fn contains(&self, key: &[u8]) -> Result<bool> {
        Ok(self.tree.contains_key(key)?)
    }

    /// Get the number of entries in this tree.
    pub fn len(&self) -> usize {
        self.tree.len()
    }

    /// Check if the tree is empty.
    pub fn is_empty(&self) -> bool {
        self.tree.is_empty()
    }

    /// Clear all entries in this tree.
    pub fn clear(&self) -> Result<()> {
        self.tree.clear()?;
        Ok(())
    }

    /// Iterate over all key-value pairs.
    pub fn iter(&self) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.tree
            .iter()
            .map(|result| result.map(|(k, v)| (k.to_vec(), v.to_vec())).map_err(Into::into))
    }

    /// Iterate over keys in a range.
    ///
    /// # Arguments
    ///
    /// * `range` - The range of keys to iterate over
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::SledStore;
    ///
    /// let store = SledStore::open_temporary().unwrap();
    /// let tree = store.tree("log").unwrap();
    ///
    /// // Get all entries from index 100 to 200
    /// for entry in tree.range(100u64.to_be_bytes()..200u64.to_be_bytes()) {
    ///     let (key, value) = entry.unwrap();
    ///     // Process entry...
    /// }
    /// ```
    pub fn range<R, K>(&self, range: R) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_
    where
        R: std::ops::RangeBounds<K>,
        K: AsRef<[u8]>,
    {
        self.tree
            .range(range)
            .map(|result| result.map(|(k, v)| (k.to_vec(), v.to_vec())).map_err(Into::into))
    }

    /// Scan keys with a prefix.
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::SledStore;
    ///
    /// let store = SledStore::open_temporary().unwrap();
    /// let tree = store.tree("cache").unwrap();
    ///
    /// // Get all entries with prefix "user:"
    /// for entry in tree.scan_prefix(b"user:") {
    ///     let (key, value) = entry.unwrap();
    ///     // Process entry...
    /// }
    /// ```
    pub fn scan_prefix(&self, prefix: &[u8]) -> impl Iterator<Item = Result<(Vec<u8>, Vec<u8>)>> + '_ {
        self.tree
            .scan_prefix(prefix)
            .map(|result| result.map(|(k, v)| (k.to_vec(), v.to_vec())).map_err(Into::into))
    }

    /// Compare-and-swap operation.
    ///
    /// Atomically sets `key` to `new` if its current value is `expected`.
    /// Returns the actual value before the operation.
    ///
    /// # Arguments
    ///
    /// * `key` - The key to update
    /// * `expected` - The expected current value (None for key doesn't exist)
    /// * `new` - The new value to set (None to delete)
    pub fn compare_and_swap(
        &self,
        key: &[u8],
        expected: Option<&[u8]>,
        new: Option<&[u8]>,
    ) -> Result<std::result::Result<(), Option<Vec<u8>>>> {
        match self.tree.compare_and_swap(key, expected, new)? {
            Ok(()) => Ok(Ok(())),
            Err(cas_error) => Ok(Err(cas_error.current.map(|v| v.to_vec()))),
        }
    }

    /// Fetch and update atomically.
    ///
    /// # Arguments
    ///
    /// * `key` - The key to update
    /// * `f` - Function that takes old value and returns new value
    pub fn fetch_and_update<F>(&self, key: &[u8], f: F) -> Result<Option<Vec<u8>>>
    where
        F: FnMut(Option<&[u8]>) -> Option<Vec<u8>>,
    {
        Ok(self.tree.fetch_and_update(key, f)?.map(|v| v.to_vec()))
    }

    /// Apply a batch of operations atomically.
    ///
    /// # Example
    ///
    /// ```rust,no_run
    /// use nexus_raft::storage::{SledStore, SledBatch};
    ///
    /// let store = SledStore::open_temporary().unwrap();
    /// let tree = store.tree("data").unwrap();
    ///
    /// let mut batch = SledBatch::new();
    /// batch.insert(b"key1", b"value1");
    /// batch.insert(b"key2", b"value2");
    /// batch.remove(b"old_key");
    ///
    /// tree.apply_batch(&batch).unwrap();
    /// ```
    pub fn apply_batch(&self, batch: &SledBatch) -> Result<()> {
        self.tree.apply_batch(batch.inner.clone())?;
        Ok(())
    }

    /// Flush pending writes for this tree.
    pub fn flush(&self) -> Result<()> {
        self.tree.flush()?;
        Ok(())
    }

    /// Get the raw sled::Tree handle for advanced operations.
    pub fn raw(&self) -> &sled::Tree {
        &self.tree
    }
}

/// A batch of operations to apply atomically.
pub struct SledBatch {
    inner: sled::Batch,
}

impl SledBatch {
    /// Create a new empty batch.
    pub fn new() -> Self {
        Self {
            inner: sled::Batch::default(),
        }
    }

    /// Add an insert operation to the batch.
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        self.inner.insert(key, value);
    }

    /// Add a remove operation to the batch.
    pub fn remove(&mut self, key: &[u8]) {
        self.inner.remove(key);
    }
}

impl Default for SledBatch {
    fn default() -> Self {
        Self::new()
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
        assert_eq!(u64::from_be_bytes(entries[0].0.clone().try_into().unwrap()), 3);
        assert_eq!(u64::from_be_bytes(entries[3].0.clone().try_into().unwrap()), 6);
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
