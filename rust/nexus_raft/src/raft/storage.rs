//! Raft storage implementation using sled.
//!
//! This module implements the `raft::Storage` trait from tikv/raft-rs
//! using our sled-based storage layer for persistence.

use raft::eraftpb::{ConfState, Entry, HardState, Snapshot};
use raft::{Error as RaftCoreError, RaftState, Storage, StorageError as RaftStorageError};

use crate::storage::{RedbStore, RedbTree};

use super::{RaftError, Result};

// Storage tree names
const TREE_ENTRIES: &str = "raft_entries";
const TREE_STATE: &str = "raft_state";

// State keys
const KEY_HARD_STATE: &[u8] = b"hard_state";
const KEY_CONF_STATE: &[u8] = b"conf_state";
const KEY_SNAPSHOT: &[u8] = b"snapshot";
const KEY_FIRST_INDEX: &[u8] = b"first_index";

/// Raft storage backed by redb.
///
/// This implements the `raft::Storage` trait, providing persistent storage
/// for Raft log entries, hard state, and snapshots.
///
/// # Storage Layout
///
/// ```text
/// redb database
/// ├── raft_entries/     # Log entries (key: index as bytes)
/// │   ├── 1 -> Entry
/// │   ├── 2 -> Entry
/// │   └── ...
/// └── raft_state/       # Raft state
///     ├── hard_state -> HardState (term, vote, commit)
///     ├── conf_state -> ConfState (voters, learners)
///     ├── snapshot -> Snapshot
///     └── first_index -> u64
/// ```
pub struct RaftStorage {
    /// Underlying redb store.
    store: RedbStore,
    /// Tree for log entries.
    entries: RedbTree,
    /// Tree for raft state.
    state: RedbTree,
}

impl RaftStorage {
    /// Create a new Raft storage instance.
    ///
    /// # Arguments
    /// * `store` - The redb store to use for persistence
    pub fn new(store: RedbStore) -> Result<Self> {
        let entries = store.tree(TREE_ENTRIES)?;
        let state = store.tree(TREE_STATE)?;

        let storage = Self {
            store,
            entries,
            state,
        };

        // Initialize first_index if not set
        if storage.state.get(KEY_FIRST_INDEX)?.is_none() {
            storage.state.set(KEY_FIRST_INDEX, &1u64.to_be_bytes())?;
        }

        Ok(storage)
    }

    /// Create Raft storage from a path.
    ///
    /// Appends `raft.redb` to the path so callers can pass a directory
    /// (sled used the path as a directory; redb uses it as a file).
    pub fn open(path: impl AsRef<std::path::Path>) -> Result<Self> {
        let store = RedbStore::open(path.as_ref().join("raft.redb"))?;
        Self::new(store)
    }

    /// Get the first index in the log.
    pub fn first_index_impl(&self) -> Result<u64> {
        match self.state.get(KEY_FIRST_INDEX)? {
            Some(bytes) => {
                let arr: [u8; 8] = bytes
                    .as_slice()
                    .try_into()
                    .map_err(|_| RaftError::Storage("invalid first_index".into()))?;
                Ok(u64::from_be_bytes(arr))
            }
            None => Ok(1),
        }
    }

    /// Get the last index in the log.
    pub fn last_index_impl(&self) -> Result<u64> {
        // Find the last entry by scanning in reverse
        let first = self.first_index_impl()?;

        // Check if there are any entries
        if self.entries.is_empty() {
            return Ok(first.saturating_sub(1));
        }

        // Find last key
        let last_key = self
            .entries
            .last()?
            .map(|(k, _)| -> Result<u64> {
                let arr: [u8; 8] = k
                    .as_slice()
                    .try_into()
                    .map_err(|_| RaftError::Storage("invalid entry key".into()))?;
                Ok(u64::from_be_bytes(arr))
            })
            .transpose()?
            .unwrap_or(first.saturating_sub(1));

        Ok(last_key)
    }

    /// Append entries to the log.
    pub fn append(&self, entries: &[Entry]) -> Result<()> {
        let mut batch = self.entries.batch();

        for entry in entries {
            let key = entry.index.to_be_bytes();
            let value = protobuf::Message::write_to_bytes(entry)
                .map_err(|e| RaftError::Serialization(e.to_string()))?;
            batch.insert(&key, &value);
        }

        batch.apply()?;
        Ok(())
    }

    /// Set the hard state.
    pub fn set_hard_state(&self, hs: &HardState) -> Result<()> {
        let value = protobuf::Message::write_to_bytes(hs)
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        self.state.set(KEY_HARD_STATE, &value)?;
        Ok(())
    }

    /// Set the conf state.
    pub fn set_conf_state(&self, cs: &ConfState) -> Result<()> {
        let value = protobuf::Message::write_to_bytes(cs)
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        self.state.set(KEY_CONF_STATE, &value)?;
        Ok(())
    }

    /// Apply a snapshot.
    pub fn apply_snapshot(&self, snapshot: &Snapshot) -> Result<()> {
        let meta = snapshot.get_metadata();

        // Update first_index to snapshot index + 1
        let new_first = meta.index + 1;
        self.state.set(KEY_FIRST_INDEX, &new_first.to_be_bytes())?;

        // Clear old entries
        self.entries.clear()?;

        // Save snapshot
        let value = protobuf::Message::write_to_bytes(snapshot)
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        self.state.set(KEY_SNAPSHOT, &value)?;

        // Update conf state from snapshot
        self.set_conf_state(meta.get_conf_state())?;

        Ok(())
    }

    /// Compact the log up to the given index.
    ///
    /// Removes all entries before `compact_index` and updates first_index.
    pub fn compact(&self, compact_index: u64) -> Result<()> {
        let first = self.first_index_impl()?;
        if compact_index <= first {
            return Ok(()); // Nothing to compact
        }

        // Remove entries [first, compact_index)
        let mut batch = self.entries.batch();
        for idx in first..compact_index {
            batch.remove(&idx.to_be_bytes());
        }
        batch.apply()?;

        // Update first_index
        self.state
            .set(KEY_FIRST_INDEX, &compact_index.to_be_bytes())?;

        Ok(())
    }

    /// Flush all data to disk.
    pub fn flush(&self) -> Result<()> {
        self.store.flush()?;
        Ok(())
    }

    /// Get entry at the given index.
    fn get_entry(&self, index: u64) -> Result<Option<Entry>> {
        match self.entries.get(&index.to_be_bytes())? {
            Some(bytes) => {
                let entry: Entry = protobuf::Message::parse_from_bytes(&bytes)
                    .map_err(|e| RaftError::Serialization(e.to_string()))?;
                Ok(Some(entry))
            }
            None => Ok(None),
        }
    }
}

impl Storage for RaftStorage {
    fn initial_state(&self) -> raft::Result<RaftState> {
        let hard_state = match self.state.get(KEY_HARD_STATE).map_err(to_raft_error)? {
            Some(bytes) => protobuf::Message::parse_from_bytes(&bytes)
                .map_err(|e| RaftCoreError::Store(RaftStorageError::Other(Box::new(e))))?,
            None => HardState::default(),
        };

        let conf_state = match self.state.get(KEY_CONF_STATE).map_err(to_raft_error)? {
            Some(bytes) => protobuf::Message::parse_from_bytes(&bytes)
                .map_err(|e| RaftCoreError::Store(RaftStorageError::Other(Box::new(e))))?,
            None => ConfState::default(),
        };

        Ok(RaftState {
            hard_state,
            conf_state,
        })
    }

    fn entries(
        &self,
        low: u64,
        high: u64,
        max_size: impl Into<Option<u64>>,
        _context: raft::GetEntriesContext,
    ) -> raft::Result<Vec<Entry>> {
        let first = self.first_index_impl().map_err(to_raft_error)?;
        let last = self.last_index_impl().map_err(to_raft_error)?;

        if low < first {
            return Err(RaftCoreError::Store(RaftStorageError::Compacted));
        }

        if high > last + 1 {
            return Err(RaftCoreError::Store(RaftStorageError::Unavailable));
        }

        let max_size = max_size.into().unwrap_or(u64::MAX);
        let mut entries = Vec::new();
        let mut size: u64 = 0;

        for idx in low..high {
            if let Some(entry) = self.get_entry(idx).map_err(to_raft_error)? {
                let entry_size = protobuf::Message::compute_size(&entry) as u64;

                // Always include at least one entry
                if !entries.is_empty() && size + entry_size > max_size {
                    break;
                }

                size += entry_size;
                entries.push(entry);
            } else {
                return Err(RaftCoreError::Store(RaftStorageError::Unavailable));
            }
        }

        Ok(entries)
    }

    fn term(&self, idx: u64) -> raft::Result<u64> {
        let first = self.first_index_impl().map_err(to_raft_error)?;

        if idx < first {
            // Check if it matches snapshot
            if let Ok(snap) = self.snapshot(0, 0) {
                if snap.get_metadata().index == idx {
                    return Ok(snap.get_metadata().term);
                }
            }
            return Err(RaftCoreError::Store(RaftStorageError::Compacted));
        }

        match self.get_entry(idx).map_err(to_raft_error)? {
            Some(entry) => Ok(entry.term),
            None => Err(RaftCoreError::Store(RaftStorageError::Unavailable)),
        }
    }

    fn first_index(&self) -> raft::Result<u64> {
        self.first_index_impl().map_err(to_raft_error)
    }

    fn last_index(&self) -> raft::Result<u64> {
        self.last_index_impl().map_err(to_raft_error)
    }

    fn snapshot(&self, _request_index: u64, _to: u64) -> raft::Result<Snapshot> {
        match self.state.get(KEY_SNAPSHOT).map_err(to_raft_error)? {
            Some(bytes) => {
                let snapshot: Snapshot = protobuf::Message::parse_from_bytes(&bytes)
                    .map_err(|e| RaftCoreError::Store(RaftStorageError::Other(Box::new(e))))?;
                Ok(snapshot)
            }
            None => Ok(Snapshot::default()),
        }
    }
}

/// Convert our error to raft error.
fn to_raft_error(e: impl std::error::Error + Send + Sync + 'static) -> RaftCoreError {
    RaftCoreError::Store(RaftStorageError::Other(Box::new(e)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn create_test_storage() -> (RaftStorage, TempDir) {
        let dir = TempDir::new().unwrap();
        let storage = RaftStorage::open(dir.path()).unwrap();
        (storage, dir)
    }

    #[test]
    fn test_initial_state() {
        let (storage, _dir) = create_test_storage();

        let state = storage.initial_state().unwrap();
        assert_eq!(state.hard_state, HardState::default());
        assert_eq!(state.conf_state, ConfState::default());
    }

    #[test]
    fn test_first_last_index_empty() {
        let (storage, _dir) = create_test_storage();

        assert_eq!(storage.first_index().unwrap(), 1);
        assert_eq!(storage.last_index().unwrap(), 0);
    }

    #[test]
    fn test_append_and_retrieve() {
        let (storage, _dir) = create_test_storage();

        // Create entries
        let mut entries = vec![];
        for i in 1..=5 {
            let mut entry = Entry::default();
            entry.index = i;
            entry.term = 1;
            entry.data = format!("data-{}", i).into_bytes().into();
            entries.push(entry);
        }

        // Append
        storage.append(&entries).unwrap();

        // Check indices
        assert_eq!(storage.first_index().unwrap(), 1);
        assert_eq!(storage.last_index().unwrap(), 5);

        // Retrieve entries
        let retrieved = storage
            .entries(1, 6, None, raft::GetEntriesContext::empty(false))
            .unwrap();
        assert_eq!(retrieved.len(), 5);
        assert_eq!(retrieved[0].index, 1);
        assert_eq!(retrieved[4].index, 5);
    }

    #[test]
    fn test_hard_state() {
        let (storage, _dir) = create_test_storage();

        let mut hs = HardState::default();
        hs.term = 5;
        hs.vote = 2;
        hs.commit = 10;

        storage.set_hard_state(&hs).unwrap();

        let state = storage.initial_state().unwrap();
        assert_eq!(state.hard_state.term, 5);
        assert_eq!(state.hard_state.vote, 2);
        assert_eq!(state.hard_state.commit, 10);
    }

    #[test]
    fn test_compact() {
        let (storage, _dir) = create_test_storage();

        // Append entries
        let mut entries = vec![];
        for i in 1..=10 {
            let mut entry = Entry::default();
            entry.index = i;
            entry.term = 1;
            entries.push(entry);
        }
        storage.append(&entries).unwrap();

        // Compact up to index 5
        storage.compact(5).unwrap();

        // First index should now be 5
        assert_eq!(storage.first_index().unwrap(), 5);

        // Entries 1-4 should be compacted
        let result = storage.entries(1, 5, None, raft::GetEntriesContext::empty(false));
        assert!(matches!(
            result,
            Err(RaftCoreError::Store(RaftStorageError::Compacted))
        ));

        // Entries 5-10 should still be available
        let entries = storage
            .entries(5, 11, None, raft::GetEntriesContext::empty(false))
            .unwrap();
        assert_eq!(entries.len(), 6);
    }
}
