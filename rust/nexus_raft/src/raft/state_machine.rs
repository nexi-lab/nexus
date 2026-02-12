//! State machine trait for Raft consensus.
//!
//! The state machine defines what operations can be applied through Raft.
//! For STRONG_HA zones, this includes metadata and lock operations
//! (NOT file data - that stays in CAS/S3).

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::storage::{RedbStorageError, RedbStore, RedbTree};

use super::Result;

/// Command to be replicated through Raft.
///
/// Commands are serialized and stored in the Raft log, then applied
/// to the state machine when committed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Command {
    /// Set a key-value pair in metadata.
    SetMetadata {
        /// The key (typically a file path).
        key: String,
        /// The value (serialized metadata).
        value: Vec<u8>,
    },

    /// Delete a metadata entry.
    DeleteMetadata {
        /// The key to delete.
        key: String,
    },

    /// Acquire a distributed lock (supports semaphore).
    ///
    /// If `max_holders == 1`, this is an exclusive mutex.
    /// If `max_holders > 1`, this is a semaphore that allows
    /// up to `max_holders` concurrent holders.
    AcquireLock {
        /// Resource path being locked.
        path: String,
        /// Unique lock ID for this holder (UUID).
        lock_id: String,
        /// Maximum number of concurrent holders (1 = mutex, >1 = semaphore).
        max_holders: u32,
        /// Lock expiration in seconds.
        ttl_secs: u32,
        /// Information about the holder (e.g., "agent:xxx").
        holder_info: String,
    },

    /// Release a distributed lock.
    ReleaseLock {
        /// Resource path.
        path: String,
        /// Lock ID of the holder releasing.
        lock_id: String,
    },

    /// Extend lock TTL.
    ExtendLock {
        /// Resource path.
        path: String,
        /// Lock ID of the holder.
        lock_id: String,
        /// New TTL in seconds (from now).
        new_ttl_secs: u32,
    },

    /// No-op command (used for leader election confirmation).
    Noop,
}

/// Result of applying a command.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum CommandResult {
    /// Command succeeded.
    Success,

    /// Command succeeded with a value.
    Value(Vec<u8>),

    /// Lock acquisition result.
    LockResult(LockState),

    /// Command failed.
    Error(String),
}

/// State of a lock acquisition attempt.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockState {
    /// Whether the lock was acquired.
    pub acquired: bool,
    /// Current number of holders.
    pub current_holders: u32,
    /// Maximum allowed holders.
    pub max_holders: u32,
    /// If not acquired, who are the current holders.
    pub holders: Vec<HolderInfo>,
}

/// Information about a lock holder.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HolderInfo {
    /// Unique lock ID (UUID).
    pub lock_id: String,
    /// Holder description (e.g., "agent:xxx").
    pub holder_info: String,
    /// When the lock was acquired (Unix timestamp).
    pub acquired_at: u64,
    /// When the lock expires (Unix timestamp).
    pub expires_at: u64,
}

/// Lock entry stored in the state machine.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockInfo {
    /// Resource path.
    pub path: String,
    /// Maximum concurrent holders.
    pub max_holders: u32,
    /// Current holders.
    pub holders: Vec<HolderInfo>,
}

impl LockInfo {
    /// Create a new lock with the first holder.
    fn new(path: String, max_holders: u32, first_holder: HolderInfo) -> Self {
        Self {
            path,
            max_holders,
            holders: vec![first_holder],
        }
    }

    /// Check if the lock can accept more holders.
    fn can_acquire(&self) -> bool {
        self.holders.len() < self.max_holders as usize
    }

    /// Check if a specific lock_id already holds the lock.
    fn has_holder(&self, lock_id: &str) -> bool {
        self.holders.iter().any(|h| h.lock_id == lock_id)
    }

    /// Add a new holder.
    fn add_holder(&mut self, holder: HolderInfo) {
        self.holders.push(holder);
    }

    /// Remove a holder by lock_id.
    fn remove_holder(&mut self, lock_id: &str) -> bool {
        let len_before = self.holders.len();
        self.holders.retain(|h| h.lock_id != lock_id);
        self.holders.len() < len_before
    }

    /// Remove expired holders and return true if any were removed.
    fn remove_expired(&mut self, now: u64) -> bool {
        let len_before = self.holders.len();
        self.holders.retain(|h| h.expires_at > now);
        self.holders.len() < len_before
    }

    /// Extend a holder's TTL.
    fn extend_ttl(&mut self, lock_id: &str, new_expires_at: u64) -> bool {
        for holder in &mut self.holders {
            if holder.lock_id == lock_id {
                holder.expires_at = new_expires_at;
                return true;
            }
        }
        false
    }

    /// Check if the lock has no holders.
    fn is_empty(&self) -> bool {
        self.holders.is_empty()
    }

    /// Get current lock state for response.
    fn to_state(&self, acquired: bool) -> LockState {
        LockState {
            acquired,
            current_holders: self.holders.len() as u32,
            max_holders: self.max_holders,
            holders: self.holders.clone(),
        }
    }
}

/// State machine trait that must be implemented by applications.
///
/// The state machine processes committed Raft log entries and maintains
/// the application state. For Nexus STRONG_HA zones, this handles:
///
/// - File metadata (path -> hash, size, mtime, permissions)
/// - Distributed locks (semaphore-style with owner tracking)
///
/// File content is NOT stored in the state machine - it remains in
/// the content-addressable storage (CAS) backend (S3, GCS, local).
pub trait StateMachine: Send + Sync {
    /// Apply a committed command to the state machine.
    ///
    /// This is called when a log entry is committed (replicated to a quorum).
    /// The implementation must be deterministic - given the same sequence of
    /// commands, all nodes must reach the same state.
    ///
    /// # Arguments
    /// * `index` - Log index of the entry being applied
    /// * `command` - The command to apply
    ///
    /// # Returns
    /// Result of applying the command
    fn apply(&mut self, index: u64, command: &Command) -> Result<CommandResult>;

    /// Create a snapshot of the current state.
    ///
    /// Snapshots are used to compact the Raft log and for catch-up of
    /// lagging followers. Returns serialized state that can be restored
    /// with `restore_snapshot`.
    ///
    /// For witness nodes, this returns an empty snapshot (they don't
    /// store state machine data).
    fn snapshot(&self) -> Result<Vec<u8>>;

    /// Restore state from a snapshot.
    ///
    /// Called when a node receives a snapshot from the leader (typically
    /// when the node is far behind or just joined the cluster).
    fn restore_snapshot(&mut self, data: &[u8]) -> Result<()>;

    /// Get the last applied log index.
    ///
    /// Used to determine which log entries need to be applied after restart.
    fn last_applied_index(&self) -> u64;
}

/// A no-op state machine for witness nodes (in-memory, for testing).
///
/// Witness nodes participate in Raft voting but don't apply state machine
/// commands. They only store the Raft log (for leader election and replication).
/// This makes them cheaper to run while still contributing to quorum.
#[derive(Debug, Default)]
pub struct WitnessStateMachineInMemory {
    last_applied: u64,
}

impl WitnessStateMachineInMemory {
    /// Create a new witness state machine.
    pub fn new() -> Self {
        Self { last_applied: 0 }
    }
}

impl StateMachine for WitnessStateMachineInMemory {
    fn apply(&mut self, index: u64, _command: &Command) -> Result<CommandResult> {
        self.last_applied = index;
        Ok(CommandResult::Success)
    }

    fn snapshot(&self) -> Result<Vec<u8>> {
        Ok(vec![])
    }

    fn restore_snapshot(&mut self, _data: &[u8]) -> Result<()> {
        Ok(())
    }

    fn last_applied_index(&self) -> u64 {
        self.last_applied
    }
}

// Tree name for witness log storage
const TREE_WITNESS_LOG: &str = "witness_log";
const KEY_WITNESS_LAST_INDEX: &[u8] = b"__witness_last_index__";

/// Persistent witness state machine backed by redb.
///
/// Stores log entries for vote validation but doesn't apply commands.
/// This is used for production witness nodes.
pub struct WitnessStateMachine {
    log_tree: RedbTree,
    last_index: u64,
}

impl WitnessStateMachine {
    /// Create a new witness state machine with storage.
    pub fn new(store: &RedbStore) -> Result<Self> {
        let log_tree = store.tree(TREE_WITNESS_LOG)?;

        // Load last index from storage
        let last_index = log_tree
            .get(KEY_WITNESS_LAST_INDEX)?
            .map(|v| {
                if v.len() == 8 {
                    let bytes: [u8; 8] = [v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]];
                    u64::from_le_bytes(bytes)
                } else {
                    0
                }
            })
            .unwrap_or(0);

        Ok(Self {
            log_tree,
            last_index,
        })
    }

    /// Store a log entry (for vote validation).
    ///
    /// # Errors
    /// Returns an error if the storage operation fails.
    pub fn store_log_entry(&mut self, index: u64, data: &[u8]) -> Result<()> {
        let key = format!("log:{:020}", index);
        self.log_tree.set(key.as_bytes(), data)?;

        if index > self.last_index {
            self.last_index = index;
            self.log_tree
                .set(KEY_WITNESS_LAST_INDEX, &index.to_le_bytes())?;
        }
        Ok(())
    }

    /// Get a log entry by index.
    pub fn get_log_entry(&self, index: u64) -> Option<Vec<u8>> {
        let key = format!("log:{:020}", index);
        self.log_tree.get(key.as_bytes()).ok().flatten()
    }
}

impl StateMachine for WitnessStateMachine {
    fn apply(&mut self, index: u64, _command: &Command) -> Result<CommandResult> {
        // Witness nodes don't apply commands - they just track the index
        self.last_index = index;
        Ok(CommandResult::Success)
    }

    fn snapshot(&self) -> Result<Vec<u8>> {
        // Witness nodes return empty snapshots
        Ok(vec![])
    }

    fn restore_snapshot(&mut self, _data: &[u8]) -> Result<()> {
        // Witness nodes don't restore state
        Ok(())
    }

    fn last_applied_index(&self) -> u64 {
        self.last_index
    }
}

// Tree names for FullStateMachine
const TREE_METADATA: &str = "sm_metadata";
const TREE_LOCKS: &str = "sm_locks";
const KEY_LAST_APPLIED: &[u8] = b"__last_applied__";

/// Full state machine for STRONG_HA zones.
///
/// Stores metadata and locks in redb for persistence. This is used
/// by leader and follower nodes (not witnesses).
///
/// # Storage Layout
///
/// ```text
/// redb database
/// ├── sm_metadata/        # File metadata (key: path)
/// │   ├── "/zone/file1" -> FileMetadata (serialized)
/// │   ├── "/zone/file2" -> FileMetadata (serialized)
/// │   └── ...
/// └── sm_locks/           # Distributed locks (key: path)
///     ├── "/zone/file1" -> LockInfo (serialized)
///     └── ...
/// ```
pub struct FullStateMachine {
    /// Metadata tree: path -> serialized FileMetadata.
    metadata: RedbTree,
    /// Locks tree: path -> serialized LockInfo.
    locks: RedbTree,
    /// Last applied log index.
    last_applied: u64,
}

impl FullStateMachine {
    /// Create a new full state machine.
    ///
    /// # Arguments
    /// * `store` - The redb store to use for persistence
    pub fn new(store: &RedbStore) -> Result<Self> {
        let metadata = store.tree(TREE_METADATA)?;
        let locks = store.tree(TREE_LOCKS)?;

        // Load last_applied from metadata tree
        let last_applied = match metadata.get(KEY_LAST_APPLIED)? {
            Some(bytes) => {
                let arr: [u8; 8] = bytes
                    .try_into()
                    .map_err(|_| super::RaftError::Storage("invalid last_applied".into()))?;
                u64::from_be_bytes(arr)
            }
            None => 0,
        };

        Ok(Self {
            metadata,
            locks,
            last_applied,
        })
    }

    /// Get current Unix timestamp.
    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
    }

    /// Apply SetMetadata command.
    fn apply_set_metadata(&self, key: &str, value: &[u8]) -> Result<CommandResult> {
        self.metadata.set(key.as_bytes(), value)?;
        Ok(CommandResult::Success)
    }

    /// Apply DeleteMetadata command.
    fn apply_delete_metadata(&self, key: &str) -> Result<CommandResult> {
        self.metadata.delete(key.as_bytes())?;
        Ok(CommandResult::Success)
    }

    /// Apply AcquireLock command.
    fn apply_acquire_lock(
        &self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> Result<CommandResult> {
        let now = Self::now();
        let expires_at = now + ttl_secs as u64;

        let new_holder = HolderInfo {
            lock_id: lock_id.to_string(),
            holder_info: holder_info.to_string(),
            acquired_at: now,
            expires_at,
        };

        // Try to get existing lock
        let mut lock_info: LockInfo = match self.locks.get(path.as_bytes())? {
            Some(bytes) => bincode::deserialize(&bytes)?,
            None => {
                // No existing lock - create new one
                let lock = LockInfo::new(path.to_string(), max_holders, new_holder);
                let serialized = bincode::serialize(&lock)?;
                self.locks.set(path.as_bytes(), &serialized)?;
                return Ok(CommandResult::LockResult(lock.to_state(true)));
            }
        };

        // Clean up expired holders first
        lock_info.remove_expired(now);

        // Check if this lock_id already holds the lock (idempotent)
        if lock_info.has_holder(lock_id) {
            // Already holding - extend TTL
            lock_info.extend_ttl(lock_id, expires_at);
            let serialized = bincode::serialize(&lock_info)?;
            self.locks.set(path.as_bytes(), &serialized)?;
            return Ok(CommandResult::LockResult(lock_info.to_state(true)));
        }

        // Check if we can acquire (within max_holders limit)
        // Also check max_holders matches (can't mix mutex and semaphore)
        if lock_info.max_holders != max_holders {
            // Mismatch in lock type - deny
            return Ok(CommandResult::LockResult(lock_info.to_state(false)));
        }

        if lock_info.can_acquire() {
            // Add new holder
            lock_info.add_holder(new_holder);
            let serialized = bincode::serialize(&lock_info)?;
            self.locks.set(path.as_bytes(), &serialized)?;
            Ok(CommandResult::LockResult(lock_info.to_state(true)))
        } else {
            // Cannot acquire - at capacity
            Ok(CommandResult::LockResult(lock_info.to_state(false)))
        }
    }

    /// Apply ReleaseLock command.
    ///
    /// Returns Success if the holder was found and removed.
    /// Returns Error if the lock doesn't exist or the holder is not found.
    fn apply_release_lock(&self, path: &str, lock_id: &str) -> Result<CommandResult> {
        let mut lock_info: LockInfo = match self.locks.get(path.as_bytes())? {
            Some(bytes) => bincode::deserialize(&bytes)?,
            None => {
                // No lock exists - error (not owned)
                return Ok(CommandResult::Error("Lock not found".to_string()));
            }
        };

        // Remove the holder - returns true if holder was found and removed
        if !lock_info.remove_holder(lock_id) {
            // Holder not found - error (not owned or already released)
            return Ok(CommandResult::Error("Lock holder not found".to_string()));
        }

        if lock_info.is_empty() {
            // No more holders - delete the lock entry
            self.locks.delete(path.as_bytes())?;
        } else {
            // Save updated lock
            let serialized = bincode::serialize(&lock_info)?;
            self.locks.set(path.as_bytes(), &serialized)?;
        }

        Ok(CommandResult::Success)
    }

    /// Apply ExtendLock command.
    fn apply_extend_lock(
        &self,
        path: &str,
        lock_id: &str,
        new_ttl_secs: u32,
    ) -> Result<CommandResult> {
        let now = Self::now();
        let new_expires_at = now + new_ttl_secs as u64;

        let mut lock_info: LockInfo = match self.locks.get(path.as_bytes())? {
            Some(bytes) => bincode::deserialize(&bytes)?,
            None => {
                // No lock exists - error
                return Ok(CommandResult::Error("Lock not found".to_string()));
            }
        };

        // Remove expired holders first
        lock_info.remove_expired(now);

        if lock_info.extend_ttl(lock_id, new_expires_at) {
            let serialized = bincode::serialize(&lock_info)?;
            self.locks.set(path.as_bytes(), &serialized)?;
            Ok(CommandResult::Success)
        } else {
            Ok(CommandResult::Error("Lock holder not found".to_string()))
        }
    }

    /// Save last_applied index to storage.
    fn save_last_applied(&self, index: u64) -> Result<()> {
        self.metadata.set(KEY_LAST_APPLIED, &index.to_be_bytes())?;
        Ok(())
    }

    /// Get metadata by path.
    pub fn get_metadata(&self, path: &str) -> Result<Option<Vec<u8>>> {
        Ok(self.metadata.get(path.as_bytes())?)
    }

    /// List all metadata with prefix.
    pub fn list_metadata(&self, prefix: &str) -> Result<Vec<(String, Vec<u8>)>> {
        let mut result = Vec::new();
        for item in self.metadata.scan_prefix(prefix.as_bytes()) {
            let (key, value) = item?;
            if let Ok(path) = String::from_utf8(key) {
                // Skip internal keys
                if !path.starts_with("__") {
                    result.push((path, value));
                }
            }
        }
        Ok(result)
    }

    /// Get lock info by path.
    pub fn get_lock(&self, path: &str) -> Result<Option<LockInfo>> {
        match self.locks.get(path.as_bytes())? {
            Some(bytes) => Ok(Some(bincode::deserialize(&bytes)?)),
            None => Ok(None),
        }
    }

    /// List all locks matching a prefix.
    pub fn list_locks(&self, prefix: &str, limit: usize) -> Result<Vec<LockInfo>> {
        let mut result = Vec::new();
        // Helper closure to process iterator items
        let mut collect = |items: &mut dyn Iterator<
            Item = std::result::Result<(Vec<u8>, Vec<u8>), RedbStorageError>,
        >|
         -> Result<()> {
            for item in items {
                if result.len() >= limit {
                    break;
                }
                let (_, value) = item?;
                let lock_info: LockInfo = bincode::deserialize(&value)?;
                if !lock_info.holders.is_empty() {
                    result.push(lock_info);
                }
            }
            Ok(())
        };
        if prefix.is_empty() {
            collect(&mut self.locks.iter())?;
        } else {
            collect(&mut self.locks.scan_prefix(prefix.as_bytes()))?;
        }
        Ok(result)
    }
}

/// Snapshot format for FullStateMachine.
#[derive(Debug, Serialize, Deserialize)]
struct Snapshot {
    /// All metadata entries.
    metadata: HashMap<String, Vec<u8>>,
    /// All lock entries.
    locks: HashMap<String, LockInfo>,
    /// Last applied index.
    last_applied: u64,
}

impl StateMachine for FullStateMachine {
    fn apply(&mut self, index: u64, command: &Command) -> Result<CommandResult> {
        // Skip if already applied (idempotent)
        if index <= self.last_applied {
            return Ok(CommandResult::Success);
        }

        let result = match command {
            Command::SetMetadata { key, value } => self.apply_set_metadata(key, value),
            Command::DeleteMetadata { key } => self.apply_delete_metadata(key),
            Command::AcquireLock {
                path,
                lock_id,
                max_holders,
                ttl_secs,
                holder_info,
            } => self.apply_acquire_lock(path, lock_id, *max_holders, *ttl_secs, holder_info),
            Command::ReleaseLock { path, lock_id } => self.apply_release_lock(path, lock_id),
            Command::ExtendLock {
                path,
                lock_id,
                new_ttl_secs,
            } => self.apply_extend_lock(path, lock_id, *new_ttl_secs),
            Command::Noop => Ok(CommandResult::Success),
        };

        // Update last_applied
        self.last_applied = index;
        self.save_last_applied(index)?;

        result
    }

    fn snapshot(&self) -> Result<Vec<u8>> {
        let mut metadata = HashMap::new();
        for item in self.metadata.iter() {
            let (key, value) = item?;
            if let Ok(path) = String::from_utf8(key) {
                // Skip internal keys
                if !path.starts_with("__") {
                    metadata.insert(path, value);
                }
            }
        }

        let mut locks = HashMap::new();
        for item in self.locks.iter() {
            let (key, value) = item?;
            if let Ok(path) = String::from_utf8(key) {
                if let Ok(lock_info) = bincode::deserialize(&value) {
                    locks.insert(path, lock_info);
                }
            }
        }

        let snapshot = Snapshot {
            metadata,
            locks,
            last_applied: self.last_applied,
        };

        Ok(bincode::serialize(&snapshot)?)
    }

    fn restore_snapshot(&mut self, data: &[u8]) -> Result<()> {
        let snapshot: Snapshot = bincode::deserialize(data)?;

        // Clear existing data
        self.metadata.clear()?;
        self.locks.clear()?;

        // Restore metadata
        for (path, value) in snapshot.metadata {
            self.metadata.set(path.as_bytes(), &value)?;
        }

        // Restore locks
        for (path, lock_info) in snapshot.locks {
            let serialized = bincode::serialize(&lock_info)?;
            self.locks.set(path.as_bytes(), &serialized)?;
        }

        // Restore last_applied
        self.last_applied = snapshot.last_applied;
        self.save_last_applied(snapshot.last_applied)?;

        Ok(())
    }

    fn last_applied_index(&self) -> u64 {
        self.last_applied
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_witness_state_machine() {
        let mut sm = WitnessStateMachineInMemory::new();

        // Apply some commands
        let cmd = Command::SetMetadata {
            key: "test".into(),
            value: vec![1, 2, 3],
        };

        let result = sm.apply(1, &cmd).unwrap();
        assert!(matches!(result, CommandResult::Success));
        assert_eq!(sm.last_applied_index(), 1);

        let result = sm.apply(2, &Command::Noop).unwrap();
        assert!(matches!(result, CommandResult::Success));
        assert_eq!(sm.last_applied_index(), 2);

        // Snapshot should be empty
        let snapshot = sm.snapshot().unwrap();
        assert!(snapshot.is_empty());
    }

    #[test]
    fn test_command_serialization() {
        let cmd = Command::AcquireLock {
            path: "/data/test.txt".into(),
            lock_id: "uuid-123".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test".into(),
        };

        let serialized = bincode::serialize(&cmd).unwrap();
        let deserialized: Command = bincode::deserialize(&serialized).unwrap();

        match deserialized {
            Command::AcquireLock {
                path,
                lock_id,
                max_holders,
                ttl_secs,
                holder_info,
            } => {
                assert_eq!(path, "/data/test.txt");
                assert_eq!(lock_id, "uuid-123");
                assert_eq!(max_holders, 3);
                assert_eq!(ttl_secs, 30);
                assert_eq!(holder_info, "agent:test");
            }
            _ => panic!("wrong command type"),
        }
    }

    #[test]
    fn test_full_state_machine_metadata() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Set metadata
        let cmd = Command::SetMetadata {
            key: "/test/file.txt".into(),
            value: b"metadata".to_vec(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        assert!(matches!(result, CommandResult::Success));

        // Get metadata
        let value = sm.get_metadata("/test/file.txt").unwrap();
        assert_eq!(value, Some(b"metadata".to_vec()));

        // Delete metadata
        let cmd = Command::DeleteMetadata {
            key: "/test/file.txt".into(),
        };
        let result = sm.apply(2, &cmd).unwrap();
        assert!(matches!(result, CommandResult::Success));

        let value = sm.get_metadata("/test/file.txt").unwrap();
        assert!(value.is_none());
    }

    #[test]
    fn test_full_state_machine_mutex_lock() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire mutex (max_holders = 1)
        let cmd = Command::AcquireLock {
            path: "/test/file.txt".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test1".into(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 1);
        } else {
            panic!("Expected LockResult");
        }

        // Try to acquire same mutex with different holder - should fail
        let cmd = Command::AcquireLock {
            path: "/test/file.txt".into(),
            lock_id: "holder-2".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
        };
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(!state.acquired);
            assert_eq!(state.current_holders, 1);
        } else {
            panic!("Expected LockResult");
        }

        // Release lock
        let cmd = Command::ReleaseLock {
            path: "/test/file.txt".into(),
            lock_id: "holder-1".into(),
        };
        let result = sm.apply(3, &cmd).unwrap();
        assert!(matches!(result, CommandResult::Success));

        // Now holder-2 can acquire
        let cmd = Command::AcquireLock {
            path: "/test/file.txt".into(),
            lock_id: "holder-2".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
        };
        let result = sm.apply(4, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
        } else {
            panic!("Expected LockResult");
        }
    }

    #[test]
    fn test_full_state_machine_semaphore_lock() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire semaphore with max_holders = 3
        let cmd = Command::AcquireLock {
            path: "/test/resource".into(),
            lock_id: "holder-1".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test1".into(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 1);
            assert_eq!(state.max_holders, 3);
        } else {
            panic!("Expected LockResult");
        }

        // Second holder can also acquire
        let cmd = Command::AcquireLock {
            path: "/test/resource".into(),
            lock_id: "holder-2".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
        };
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 2);
        } else {
            panic!("Expected LockResult");
        }

        // Third holder can also acquire
        let cmd = Command::AcquireLock {
            path: "/test/resource".into(),
            lock_id: "holder-3".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test3".into(),
        };
        let result = sm.apply(3, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 3);
        } else {
            panic!("Expected LockResult");
        }

        // Fourth holder should fail - at capacity
        let cmd = Command::AcquireLock {
            path: "/test/resource".into(),
            lock_id: "holder-4".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test4".into(),
        };
        let result = sm.apply(4, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(!state.acquired);
            assert_eq!(state.current_holders, 3);
        } else {
            panic!("Expected LockResult");
        }

        // Release one slot
        let cmd = Command::ReleaseLock {
            path: "/test/resource".into(),
            lock_id: "holder-2".into(),
        };
        sm.apply(5, &cmd).unwrap();

        // Now fourth holder can acquire
        let cmd = Command::AcquireLock {
            path: "/test/resource".into(),
            lock_id: "holder-4".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test4".into(),
        };
        let result = sm.apply(6, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 3);
        } else {
            panic!("Expected LockResult");
        }
    }

    #[test]
    fn test_full_state_machine_snapshot_restore() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Add some data
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/file1".into(),
                value: b"data1".to_vec(),
            },
        )
        .unwrap();
        sm.apply(
            2,
            &Command::SetMetadata {
                key: "/file2".into(),
                value: b"data2".to_vec(),
            },
        )
        .unwrap();
        sm.apply(
            3,
            &Command::AcquireLock {
                path: "/file1".into(),
                lock_id: "lock-1".into(),
                max_holders: 1,
                ttl_secs: 3600,
                holder_info: "agent:test".into(),
            },
        )
        .unwrap();

        // Take snapshot
        let snapshot_data = sm.snapshot().unwrap();

        // Create new state machine and restore
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.restore_snapshot(&snapshot_data).unwrap();

        // Verify data
        assert_eq!(sm2.get_metadata("/file1").unwrap(), Some(b"data1".to_vec()));
        assert_eq!(sm2.get_metadata("/file2").unwrap(), Some(b"data2".to_vec()));
        assert!(sm2.get_lock("/file1").unwrap().is_some());
        assert_eq!(sm2.last_applied_index(), 3);
    }

    #[test]
    fn test_lock_idempotent_acquire() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire lock
        let cmd = Command::AcquireLock {
            path: "/test/file.txt".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test1".into(),
        };
        sm.apply(1, &cmd).unwrap();

        // Acquire again with same lock_id - should succeed (idempotent)
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.current_holders, 1); // Still 1, not 2
        } else {
            panic!("Expected LockResult");
        }
    }

    /// Test that expired holders are cleaned up during acquire.
    #[test]
    fn test_lock_ttl_expiry_during_acquire() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire a lock with 1-second TTL
        let cmd = Command::AcquireLock {
            path: "/test/expire".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 1,
            holder_info: "agent:test1".into(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
        } else {
            panic!("Expected LockResult");
        }

        // Wait for TTL to expire
        std::thread::sleep(std::time::Duration::from_secs(2));

        // Another holder should be able to acquire because the first expired
        let cmd2 = Command::AcquireLock {
            path: "/test/expire".into(),
            lock_id: "holder-2".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
        };
        let result = sm.apply(2, &cmd2).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired, "Should acquire after expiry");
            assert_eq!(state.current_holders, 1);
            // Verify it's holder-2, not holder-1
            assert_eq!(state.holders[0].lock_id, "holder-2");
        } else {
            panic!("Expected LockResult");
        }
    }

    /// Test that mixing mutex and semaphore max_holders is rejected.
    #[test]
    fn test_lock_type_mismatch() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire a semaphore lock (max_holders = 3)
        let cmd = Command::AcquireLock {
            path: "/test/mismatch".into(),
            lock_id: "holder-1".into(),
            max_holders: 3,
            ttl_secs: 30,
            holder_info: "agent:test1".into(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
        } else {
            panic!("Expected LockResult");
        }

        // Try to acquire as mutex (max_holders = 1) — should be rejected
        let cmd2 = Command::AcquireLock {
            path: "/test/mismatch".into(),
            lock_id: "holder-2".into(),
            max_holders: 1, // Mismatch: 1 != 3
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
        };
        let result = sm.apply(2, &cmd2).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(!state.acquired, "Should reject mismatched max_holders");
        } else {
            panic!("Expected LockResult");
        }
    }

    /// Test that snapshots include expired holders (they're cleaned on acquire, not snapshot).
    #[test]
    fn test_expired_holders_in_snapshot() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire a lock with 1-second TTL
        let cmd = Command::AcquireLock {
            path: "/test/snap-expire".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 1,
            holder_info: "agent:test1".into(),
        };
        sm.apply(1, &cmd).unwrap();

        // Wait for TTL to expire
        std::thread::sleep(std::time::Duration::from_secs(2));

        // Take snapshot — should still include the expired holder
        // (cleanup happens during acquire, not snapshot)
        let snapshot_data = sm.snapshot().unwrap();

        // Restore to a new state machine
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.restore_snapshot(&snapshot_data).unwrap();

        // The expired lock should be present in the restored state
        let lock = sm2.get_lock("/test/snap-expire").unwrap();
        assert!(lock.is_some(), "Expired lock should persist in snapshot");
        let lock_info = lock.unwrap();
        assert_eq!(lock_info.holders.len(), 1);
        assert_eq!(lock_info.holders[0].lock_id, "holder-1");
    }

    /// Test edge cases with max_holders boundary values.
    #[test]
    fn test_lock_max_holders_boundary() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Acquire with max_holders = u32::MAX (should work)
        let cmd = Command::AcquireLock {
            path: "/test/boundary".into(),
            lock_id: "holder-1".into(),
            max_holders: u32::MAX,
            ttl_secs: 30,
            holder_info: "agent:test1".into(),
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
            assert_eq!(state.max_holders, u32::MAX);
        } else {
            panic!("Expected LockResult");
        }

        // Noop should be handled cleanly
        let result = sm.apply(2, &Command::Noop).unwrap();
        assert!(matches!(result, CommandResult::Success));

        // Re-applying an already applied index should be idempotent
        let cmd2 = Command::SetMetadata {
            key: "/test/dup".into(),
            value: b"data".to_vec(),
        };
        let result = sm.apply(1, &cmd2).unwrap(); // index 1 already applied
        assert!(
            matches!(result, CommandResult::Success),
            "Re-applying old index should succeed (no-op)"
        );
        // The metadata should NOT be set (skipped due to idempotency)
        assert!(sm.get_metadata("/test/dup").unwrap().is_none());
    }
}
