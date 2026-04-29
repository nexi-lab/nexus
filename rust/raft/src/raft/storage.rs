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
/// Per-storage node-incarnation marker (Issue: fresh-join panic).
///
/// Persisted ONCE on the first ``RaftStorage::new`` against a fresh redb;
/// thereafter read on every restart.  Wiping the data dir resets the
/// marker, which lets ``ensure_voter_membership`` mint a new node ID
/// instead of reusing the prior one — avoiding raft-rs's
/// ``handle_heartbeat`` panic when a wiped node rejoins a running
/// cluster (leader's in-memory ``Progress[old_id].matched`` is stale and
/// would cause ``commit_to(K)`` against ``last_index=0``).
const KEY_INCARNATION: &[u8] = b"incarnation";

/// Default incarnation used during cold-start cluster bootstrap.
///
/// All nodes converge on this value at first boot so the IDs they each
/// derive from ``compute_node_id(hostname, DEFAULT_INCARNATION)`` match
/// across the cluster's ConfState.  Only used when no leader is
/// reachable (``ensure_voter_membership`` cold-start path); a wiped
/// node that finds an existing leader mints a fresh u64 instead.
#[allow(dead_code)] // Wired up in the ensure_voter_membership commit on this branch.
pub const DEFAULT_INCARNATION: u64 = 0;

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
///     ├── first_index -> u64
///     └── incarnation -> u64  (set on first ::new, never overwritten)
/// ```
pub struct RaftStorage {
    /// Underlying redb store.
    store: RedbStore,
    /// Tree for log entries.
    entries: RedbTree,
    /// Tree for raft state.
    state: RedbTree,
    /// Whether this storage was just initialized (no prior incarnation).
    ///
    /// Set to ``true`` when ``new`` runs against a fresh redb (no
    /// ``KEY_INCARNATION`` row); ``false`` when reopening an existing
    /// store.  ``ensure_voter_membership`` reads this to decide whether
    /// to attempt the wipe-rejoin rotation flow vs. a normal recovery.
    /// In-memory only — recomputed every process start from on-disk state.
    was_just_created: bool,
}

impl RaftStorage {
    /// Create a new Raft storage instance.
    ///
    /// # Arguments
    /// * `store` - The redb store to use for persistence
    pub fn new(store: RedbStore) -> Result<Self> {
        let entries = store.tree(TREE_ENTRIES)?;
        let state = store.tree(TREE_STATE)?;

        // Detect fresh-vs-existing redb by probing for an incarnation
        // marker.  Absent → first ::new on this store: the bootstrap
        // path will write ``DEFAULT_INCARNATION`` once it knows whether
        // this is cold-start or wipe-rejoin.  Present → recovery; the
        // marker stays as-is.
        let was_just_created = state.get(KEY_INCARNATION)?.is_none();

        let storage = Self {
            store,
            entries,
            state,
            was_just_created,
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

    /// Whether this `RaftStorage` was opened against a freshly-created
    /// redb (no prior incarnation marker on disk).
    ///
    /// Reset on every process boot from durable state — ``true`` on the
    /// first ``new`` after a wipe (or first boot ever), ``false`` after
    /// any subsequent ``set_incarnation`` persists the marker.
    pub fn was_just_created(&self) -> bool {
        self.was_just_created
    }

    /// Read the persisted incarnation marker, or ``None`` if this is a
    /// freshly-created storage that hasn't been bootstrapped yet.
    pub fn incarnation(&self) -> Result<Option<u64>> {
        match self.state.get(KEY_INCARNATION)? {
            Some(bytes) => {
                let arr: [u8; 8] = bytes
                    .as_slice()
                    .try_into()
                    .map_err(|_| RaftError::Storage("invalid incarnation marker".into()))?;
                Ok(Some(u64::from_be_bytes(arr)))
            }
            None => Ok(None),
        }
    }

    /// Persist the incarnation marker.  Idempotent: callers may call
    /// repeatedly with the same value (e.g. on every restart with the
    /// existing value re-asserted).  Once written, the marker survives
    /// until the redb file is wiped.
    pub fn set_incarnation(&self, incarnation: u64) -> Result<()> {
        self.state
            .set(KEY_INCARNATION, &incarnation.to_be_bytes())?;
        Ok(())
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

    /// Store a snapshot without clearing existing entries.
    ///
    /// Used by the leader after ConfChange(AddNode) to prepare a snapshot
    /// that raft-rs sends to lagging followers via `Storage::snapshot()`.
    /// Unlike [`apply_snapshot`], this preserves existing log entries
    /// (the leader still needs them for other followers).
    pub fn store_snapshot(&self, snapshot: &Snapshot) -> Result<()> {
        let value = protobuf::Message::write_to_bytes(snapshot)
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        self.state.set(KEY_SNAPSHOT, &value)?;
        Ok(())
    }

    /// Apply a snapshot (receiver side — clears log and updates state).
    ///
    /// All four operations (update first_index, clear entries, save snapshot,
    /// update conf state) are performed in a **single redb WriteTransaction**
    /// so a crash cannot leave storage internally inconsistent.
    pub fn apply_snapshot(&self, snapshot: &Snapshot) -> Result<()> {
        let meta = snapshot.get_metadata();

        // Serialize before opening the transaction
        let snapshot_bytes = protobuf::Message::write_to_bytes(snapshot)
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        let conf_state_bytes = protobuf::Message::write_to_bytes(meta.get_conf_state())
            .map_err(|e| RaftError::Serialization(e.to_string()))?;
        let new_first = meta.index + 1;

        // Single atomic transaction across both tables
        let db = self.state.raw_db();
        let state_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.state.name());
        let entries_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.entries.name());

        let write_txn = db
            .begin_write()
            .map_err(|e| RaftError::Storage(e.to_string()))?;
        {
            // Update first_index and save snapshot + conf state
            let mut state_table = write_txn
                .open_table(state_def)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
            state_table
                .insert(KEY_FIRST_INDEX, new_first.to_be_bytes().as_slice())
                .map_err(|e| RaftError::Storage(e.to_string()))?;
            state_table
                .insert(KEY_SNAPSHOT, snapshot_bytes.as_slice())
                .map_err(|e| RaftError::Storage(e.to_string()))?;
            state_table
                .insert(KEY_CONF_STATE, conf_state_bytes.as_slice())
                .map_err(|e| RaftError::Storage(e.to_string()))?;

            // Clear old entries: delete and recreate the entries table
            drop(state_table);
            write_txn
                .delete_table(entries_def)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
            write_txn
                .open_table(entries_def)
                .map_err(|e| RaftError::Storage(e.to_string()))?;
        }
        write_txn
            .commit()
            .map_err(|e| RaftError::Storage(e.to_string()))?;

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
            let entry = Entry {
                index: i,
                term: 1,
                data: format!("data-{}", i).into_bytes().into(),
                ..Default::default()
            };
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

        let hs = HardState {
            term: 5,
            vote: 2,
            commit: 10,
            ..Default::default()
        };

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
            let entry = Entry {
                index: i,
                term: 1,
                ..Default::default()
            };
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

    // ---------------------------------------------------------------
    // Incarnation marker tests (fresh-join wipe-rejoin fix)
    // ---------------------------------------------------------------

    #[test]
    fn fresh_storage_reports_just_created() {
        let (storage, _dir) = create_test_storage();
        assert!(storage.was_just_created());
        assert_eq!(storage.incarnation().unwrap(), None);
    }

    #[test]
    fn set_incarnation_persists_across_reopen() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().to_path_buf();

        // First open: fresh, mint incarnation.
        let storage = RaftStorage::open(&path).unwrap();
        assert!(storage.was_just_created());
        storage.set_incarnation(0xDEAD_BEEF).unwrap();
        // Drop to release the redb lock before reopening.
        drop(storage);

        // Re-open: was_just_created flips to false; incarnation reads back.
        let reopened = RaftStorage::open(&path).unwrap();
        assert!(!reopened.was_just_created());
        assert_eq!(reopened.incarnation().unwrap(), Some(0xDEAD_BEEF));
    }

    #[test]
    fn set_incarnation_idempotent() {
        let (storage, _dir) = create_test_storage();
        storage.set_incarnation(42).unwrap();
        storage.set_incarnation(42).unwrap();
        assert_eq!(storage.incarnation().unwrap(), Some(42));
        // Overwriting with a different value is allowed at the storage layer
        // — caller policy (ensure_voter_membership) decides when to rotate.
        storage.set_incarnation(99).unwrap();
        assert_eq!(storage.incarnation().unwrap(), Some(99));
    }

    // ---------------------------------------------------------------
    // Snapshot apply tests (Issue #3031 / 9A)
    // ---------------------------------------------------------------

    fn make_snapshot(index: u64, term: u64, voters: &[u64]) -> Snapshot {
        let mut snap = Snapshot::default();
        let meta = snap.mut_metadata();
        meta.index = index;
        meta.term = term;
        let cs = meta.mut_conf_state();
        cs.voters = voters.to_vec();
        snap.data = format!("snapshot-at-{}", index).into_bytes().into();
        snap
    }

    #[test]
    fn test_apply_snapshot_basic() {
        let (storage, _dir) = create_test_storage();

        let snap = make_snapshot(10, 2, &[1, 2, 3]);
        storage.apply_snapshot(&snap).unwrap();

        // Verify first_index updated
        assert_eq!(storage.first_index().unwrap(), 11);

        // Verify snapshot is readable
        let stored = storage.snapshot(0, 0).unwrap();
        assert_eq!(stored.get_metadata().index, 10);
        assert_eq!(stored.get_metadata().term, 2);

        // Verify conf state updated
        let state = storage.initial_state().unwrap();
        assert_eq!(state.conf_state.voters, vec![1, 2, 3]);
    }

    #[test]
    fn test_apply_snapshot_clears_existing_entries() {
        let (storage, _dir) = create_test_storage();

        // Append some entries first
        let mut entries = vec![];
        for i in 1..=5 {
            let entry = Entry {
                index: i,
                term: 1,
                data: format!("data-{}", i).into_bytes().into(),
                ..Default::default()
            };
            entries.push(entry);
        }
        storage.append(&entries).unwrap();
        assert_eq!(storage.last_index().unwrap(), 5);

        // Apply snapshot at index 10 — should clear all entries
        let snap = make_snapshot(10, 2, &[1, 2]);
        storage.apply_snapshot(&snap).unwrap();

        // Old entries should be gone (first_index = 11, last_index = 10 i.e. empty)
        assert_eq!(storage.first_index().unwrap(), 11);
        let result = storage.entries(1, 6, None, raft::GetEntriesContext::empty(false));
        assert!(result.is_err());
    }

    #[test]
    fn test_apply_snapshot_overwrites_previous_snapshot() {
        let (storage, _dir) = create_test_storage();

        // Apply first snapshot
        let snap1 = make_snapshot(5, 1, &[1, 2]);
        storage.apply_snapshot(&snap1).unwrap();
        assert_eq!(storage.first_index().unwrap(), 6);

        // Apply second snapshot at higher index
        let snap2 = make_snapshot(15, 3, &[1, 2, 3, 4]);
        storage.apply_snapshot(&snap2).unwrap();

        // Verify second snapshot overwrites first
        assert_eq!(storage.first_index().unwrap(), 16);
        let stored = storage.snapshot(0, 0).unwrap();
        assert_eq!(stored.get_metadata().index, 15);
        assert_eq!(stored.get_metadata().term, 3);
        let state = storage.initial_state().unwrap();
        assert_eq!(state.conf_state.voters, vec![1, 2, 3, 4]);
    }

    #[test]
    fn test_apply_snapshot_persists_across_reopen() {
        let dir = TempDir::new().unwrap();

        // Apply snapshot and drop storage
        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            let snap = make_snapshot(20, 5, &[1, 2, 3]);
            storage.apply_snapshot(&snap).unwrap();
        }

        // Reopen and verify all fields persisted atomically
        {
            let storage = RaftStorage::open(dir.path()).unwrap();
            assert_eq!(storage.first_index().unwrap(), 21);

            let stored = storage.snapshot(0, 0).unwrap();
            assert_eq!(stored.get_metadata().index, 20);
            assert_eq!(stored.get_metadata().term, 5);

            let state = storage.initial_state().unwrap();
            assert_eq!(state.conf_state.voters, vec![1, 2, 3]);
        }
    }
}
