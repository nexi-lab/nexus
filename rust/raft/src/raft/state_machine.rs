//! State machine trait for Raft consensus.
//!
//! The state machine defines what operations can be applied through Raft.
//! For STRONG_HA zones, this includes metadata and lock operations
//! (NOT file data - that stays in CAS/S3).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use parking_lot::Mutex;
use redb::ReadableTable;
use serde::{Deserialize, Serialize};

use crate::storage::{RedbStore, RedbTree};

// Advisory lock types are the shared SSOT, defined in `contracts::lock_state`.
// Re-exported from this module (and from `crate::raft`) so existing
// callers keep their `use raft::{LockMode, ...}` paths.
pub use contracts::lock_state::{
    HolderInfo, LockAcquireResult, LockEntry, LockInfo, LockMode, LockState,
};

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

    /// Acquire a distributed lock (exclusive or shared).
    ///
    /// Two orthogonal dimensions:
    ///
    /// * `max_holders` — capacity. `1` = mutex, `>1` = semaphore /
    ///   reader-writer. Also used as the computed "mode" display
    ///   label in Python (`"mutex"` vs. `"semaphore"`), which stays
    ///   computed — never stored.
    /// * `mode` — conflict rule for *this acquire*. `Exclusive`
    ///   requires the caller to be the sole holder; `Shared` may
    ///   coexist with other `Shared` holders up to `max_holders` but
    ///   is blocked by any `Exclusive` holder. `max_holders=1 +
    ///   Exclusive` is a classic mutex; `max_holders>1 + Shared` is a
    ///   reader-writer lock with N concurrent readers.
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
        /// Conflict mode for this acquire (Exclusive or Shared).
        ///
        /// Added in F4 C1. Pre-F4 snapshots deserialize with
        /// `Exclusive` defaults (see the snapshot version byte in
        /// `FullStateMachine::snapshot` / `restore_snapshot`).
        mode: LockMode,
        /// Wall-clock timestamp captured at proposal time (Unix secs).
        /// All replicas use this value instead of local clocks to ensure
        /// deterministic state machine application (Issue #3029 / Bug 1).
        now_secs: u64,
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
        /// Wall-clock timestamp captured at proposal time (Unix secs).
        /// All replicas use this value instead of local clocks to ensure
        /// deterministic state machine application (Issue #3029 / Bug 1).
        now_secs: u64,
    },

    /// Compare-and-swap metadata: write only if current version matches.
    CasSetMetadata {
        /// The key (typically a file path).
        key: String,
        /// The value (serialized metadata).
        value: Vec<u8>,
        /// Expected version (0 = create-only).
        expected_version: u32,
    },

    /// Atomically adjust a metadata counter by a signed delta.
    ///
    /// Read-modify-write happens in `apply()` — serial by Raft guarantee.
    /// The value is stored as `i64` big-endian in the metadata tree.
    /// Result is clamped to `>= 0`.
    AdjustCounter {
        /// The metadata key (e.g., `"__i_links_count__"`).
        key: String,
        /// Signed delta to add (positive = increment, negative = decrement).
        delta: i64,
    },

    /// Force-release ALL holders on a lock (admin override).
    ForceReleaseLock {
        /// Resource path.
        path: String,
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
    LockResult(LockAcquireResult),

    /// Compare-and-swap result.
    CasResult {
        /// Whether the swap succeeded.
        success: bool,
        /// Current version after the operation.
        current_version: u32,
    },

    /// Command failed.
    Error(String),
}

// Advisory lock types — `LockMode`, `HolderInfo`, `LockInfo`,
// `LockAcquireResult`, `LockEntry`, `LockState` — are now defined in
// `contracts::lock_state` and re-exported at the top of this file. All
// state-transition logic lives on `LockState` (the shared BTreeMap-based
// SSOT) so the local `LockManager` path and the raft apply path go
// through the same primitives under the same mutex.

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

    /// Apply a command locally for EC (eventual consistency) writes.
    ///
    /// Unlike [`apply`], this bypasses Raft index tracking — the write
    /// is not associated with any Raft log entry. Only metadata operations
    /// (SetMetadata, DeleteMetadata) are supported; lock operations require
    /// linearizability and must use SC (Raft consensus).
    ///
    /// Default implementation returns an error (not all state machines
    /// support local writes — e.g., witness nodes).
    fn apply_local(&mut self, _command: &Command) -> Result<CommandResult> {
        Err(super::RaftError::InvalidState(
            "Local EC writes not supported on this state machine".into(),
        ))
    }

    /// Apply an EC command with LWW (Last Writer Wins) conflict resolution.
    ///
    /// Used by the peer-receive path to reject stale writes. Compares the
    /// incoming entry's timestamp against the existing metadata's `modified_at`.
    ///
    /// Default: delegates to [`apply_local`] (no LWW check). Override in
    /// state machines that store FileMetadata (i.e., [`FullStateMachine`]).
    fn apply_ec_with_lww(
        &mut self,
        command: &Command,
        _entry_timestamp: u64,
    ) -> Result<CommandResult> {
        self.apply_local(command)
    }

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
    ///
    /// Handles endianness migration: existing deployments stored `last_index`
    /// as little-endian, but the rest of the codebase uses big-endian. On load,
    /// we detect the format by checking which interpretation yields a valid
    /// Raft index (small positive number) and migrate to big-endian on next write.
    pub fn new(store: &RedbStore) -> Result<Self> {
        let log_tree = store.tree(TREE_WITNESS_LOG)?;

        // Load last index, auto-detecting LE vs BE encoding
        let last_index = log_tree
            .get(KEY_WITNESS_LAST_INDEX)?
            .map(|v| {
                if v.len() == 8 {
                    let bytes: [u8; 8] = [v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]];
                    let be_val = u64::from_be_bytes(bytes);
                    let le_val = u64::from_le_bytes(bytes);

                    // Heuristic: valid Raft indices are small positive numbers.
                    // If BE gives a huge number but LE gives a reasonable one,
                    // the data is in the old LE format.
                    if be_val > 1_000_000_000 && le_val <= 1_000_000_000 {
                        le_val // old LE format — will be re-written as BE on next store
                    } else {
                        be_val // new BE format (or both are reasonable — BE is preferred)
                    }
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
            // Always write big-endian (consistent with rest of codebase)
            self.log_tree
                .set(KEY_WITNESS_LAST_INDEX, &index.to_be_bytes())?;
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
const KEY_LAST_APPLIED: &[u8] = b"__last_applied__";

// R14: Advisory locks no longer have a redb tree. The BTreeMap in
// `Arc<Mutex<LockState>>` is the single source of truth; persistence
// happens via raft snapshots. This preserves raft's "apply = atomic
// commit point" contract — reads and writes observe the same state
// under the same mutex, and there is no two-phase window between a
// BTreeMap mirror and a redb row where a crash could leave them
// divergent. On startup the BTreeMap is rebuilt from a snapshot
// (`restore_snapshot`) plus log replay; see `FullStateMachine::apply`
// for the replay semantics.

// ---------------------------------------------------------------------------
// LWW (Last Writer Wins) helpers for EC conflict resolution
// ---------------------------------------------------------------------------

/// Decode a serialized FileMetadata protobuf and extract the `modified_at` field.
///
/// Used for LWW comparison on `SetMetadata`: both incoming and existing values
/// are decoded and their `modified_at` ISO 8601 strings compared lexicographically.
///
/// Returns empty string on decode failure (sorts before any real timestamp,
/// meaning corrupted data always gets overwritten).
#[cfg(feature = "grpc")]
fn decode_modified_at(bytes: &[u8]) -> String {
    use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
    use prost::Message as ProstMessage;

    ProtoFileMetadata::decode(bytes)
        .map(|fm| fm.modified_at)
        .unwrap_or_default()
}

/// Decode a serialized FileMetadata protobuf and parse `modified_at` to Unix seconds.
///
/// Used for LWW comparison on `DeleteMetadata`: the entry's u64 timestamp is
/// compared against the existing value's parsed `modified_at`.
///
/// Returns 0 on decode/parse failure (treat as infinitely old).
#[cfg(feature = "grpc")]
fn decode_modified_at_unix(bytes: &[u8]) -> u64 {
    use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
    use prost::Message as ProstMessage;

    ProtoFileMetadata::decode(bytes)
        .ok()
        .and_then(|fm| {
            time::OffsetDateTime::parse(
                &fm.modified_at,
                &time::format_description::well_known::Rfc3339,
            )
            .ok()
        })
        .map(|dt| dt.unix_timestamp() as u64)
        .unwrap_or(0)
}

/// Full state machine for STRONG_HA zones.
///
/// Metadata lives in redb for durability; advisory locks live in an
/// in-memory `Arc<Mutex<LockState>>` BTreeMap that is the single source
/// of truth shared with the kernel's `LockManager`. This matches the
/// raft invariant that apply is an atomic commit point — readers and
/// writers both hit the same mutex, so there is no divergence window.
///
/// # Storage Layout
///
/// ```text
/// redb database
/// └── sm_metadata/        # File metadata (key: path)
///     ├── "/zone/file1" -> FileMetadata (serialized)
///     ├── "/zone/file2" -> FileMetadata (serialized)
///     └── ...
///
/// in-memory
/// └── advisory: Arc<Mutex<LockState>> # Advisory locks (BTreeMap)
/// ```
///
/// Advisory-lock persistence happens through raft snapshots: `snapshot`
/// serializes the BTreeMap under the mutex; `restore_snapshot`
/// deserializes and replaces the BTreeMap under the same mutex. Between
/// snapshots, the raft log is the durable record — advisory state is
/// rebuilt by log replay on restart.
pub struct FullStateMachine {
    /// Metadata tree: path -> serialized FileMetadata.
    metadata: RedbTree,
    /// Advisory lock SSOT — shared with the kernel's `LockManager`.
    advisory: Arc<Mutex<LockState>>,
    /// Last applied metadata/Noop log index (persisted to redb).
    ///
    /// Gates metadata-command idempotency during log replay —
    /// `AdjustCounter` would double-count otherwise. Lock commands
    /// are idempotent under full replay (acquire/release cycles
    /// cancel out) so they ignore this guard and always apply.
    last_applied: u64,
    /// DT_MOUNT apply-side event channel (R16.2).
    ///
    /// Populated by ``ZoneRaftRegistry::setup_zone`` via
    /// ``set_mount_event_tx`` when the registry has been handed a
    /// sender by ``PyZoneManager``. ``apply`` decodes each
    /// ``Command::SetMetadata`` value after the write txn commits and,
    /// if it describes a DT_MOUNT entry, sends one ``MountEvent`` on
    /// this channel for the Python DLC consumer to pick up.
    ///
    /// ``None`` for state machines that aren't attached to a
    /// ``PyZoneManager`` (tests, witness nodes, direct-drive
    /// integration harnesses) — the send site is gated on
    /// ``is_some()`` so apply remains a no-op for those.
    #[cfg(feature = "grpc")]
    mount_event_tx: Option<super::mount_event::MountEventTx>,
    /// The zone this state machine belongs to. Populated when a
    /// ``mount_event_tx`` is attached so the emitted event carries
    /// the parent zone id without re-plumbing it through every
    /// apply call site. ``String::new()`` when no tx is attached.
    #[cfg(feature = "grpc")]
    zone_id_for_events: String,
}

impl FullStateMachine {
    /// Create a new full state machine with its own advisory-lock Arc.
    ///
    /// Callers that need to share the advisory map with a kernel
    /// `LockManager` should use [`FullStateMachine::with_advisory`]
    /// and pre-build the Arc there.
    pub fn new(store: &RedbStore) -> Result<Self> {
        Self::with_advisory(store, Arc::new(Mutex::new(LockState::new())))
    }

    /// Create a new full state machine that shares its advisory map
    /// with the provided `Arc<Mutex<LockState>>`. Used by the kernel's
    /// `LockManager::upgrade_to_distributed` path so local holders
    /// survive the upgrade and every reader on the node sees the same
    /// state.
    pub fn with_advisory(store: &RedbStore, advisory: Arc<Mutex<LockState>>) -> Result<Self> {
        let metadata = store.tree(TREE_METADATA)?;

        // Load last_applied from metadata tree.
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
            advisory,
            last_applied,
            #[cfg(feature = "grpc")]
            mount_event_tx: None,
            #[cfg(feature = "grpc")]
            zone_id_for_events: String::new(),
        })
    }

    /// Attach a DT_MOUNT apply-event sender + owning zone id (R16.2).
    ///
    /// Called once by ``ZoneRaftRegistry::setup_zone`` after the SM is
    /// constructed but before it's moved into ``ZoneConsensus``; only
    /// takes effect if the registry was pre-configured with a sender
    /// by ``PyZoneManager``. Idempotent: replacing an existing sender
    /// just drops the previous handle — raft contract is preserved
    /// because the send site never fails the apply path.
    #[cfg(feature = "grpc")]
    pub fn set_mount_event_tx(&mut self, zone_id: String, tx: super::mount_event::MountEventTx) {
        self.mount_event_tx = Some(tx);
        self.zone_id_for_events = zone_id;
    }

    /// Clone the shared advisory-lock handle. Used by the kernel's
    /// `LockManager::upgrade_to_distributed` to adopt the state
    /// machine's `Arc<Mutex<LockState>>` after the zone is set up.
    pub fn advisory_state(&self) -> Arc<Mutex<LockState>> {
        self.advisory.clone()
    }

    /// Get current Unix timestamp. Public so proposal sites can capture
    /// the timestamp before it enters the replicated command.
    pub fn now() -> u64 {
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

    /// Decode a committed ``Command::SetMetadata`` value and, if it
    /// describes a DT_MOUNT entry with a non-empty ``target_zone_id``,
    /// push a ``MountEvent`` onto the attached sender (R16.2).
    ///
    /// Failure modes never propagate out of ``apply``:
    /// - no sender attached → no-op
    /// - non-SetMetadata command → no-op
    /// - proto decode fails → ``warn!`` (indicates upstream writer
    ///   wrote garbage — a bug elsewhere, but apply can't reject
    ///   committed entries)
    /// - entry is not DT_MOUNT or has empty target → no-op (normal)
    /// - channel closed → ``error!`` (consumer gone; PyZoneManager
    ///   being torn down or the consumer task panicked — in both
    ///   cases we've lost the DLC wiring side-effect, which is
    ///   recoverable by restart but worth surfacing)
    #[cfg(feature = "grpc")]
    fn emit_mount_event(&self, command: &Command) {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        let Some(tx) = self.mount_event_tx.as_ref() else {
            return;
        };
        let (key, value) = match command {
            Command::SetMetadata { key, value } => (key, value),
            _ => return,
        };

        // DT_MOUNT entries are the only payloads we route. Decode is
        // lenient: a decode failure here means some non-FileMetadata
        // SetMetadata value sneaked in (e.g. an internal counter key
        // overwritten via SetMetadata by a non-standard caller). Log
        // once and move on — do NOT fail apply.
        let proto = match ProtoFileMetadata::decode(value.as_slice()) {
            Ok(p) => p,
            Err(e) => {
                tracing::warn!(
                    zone = %self.zone_id_for_events,
                    path = %key,
                    error = %e,
                    "mount-event: FileMetadata decode failed on apply (non-FileMetadata SetMetadata?)",
                );
                return;
            }
        };

        // Only DT_MOUNT (entry_type == 2) with a non-empty target
        // drives DLC wiring. Every other entry_type (DT_REG, DT_DIR,
        // DT_PIPE, DT_STREAM, DT_EXTERNAL_STORAGE) is ignored.
        const DT_MOUNT: i32 = 2;
        if proto.entry_type != DT_MOUNT || proto.target_zone_id.is_empty() {
            return;
        }

        let event = super::mount_event::MountEvent {
            parent_zone_id: self.zone_id_for_events.clone(),
            mount_path: key.clone(),
            target_zone_id: proto.target_zone_id,
        };

        if let Err(e) = tx.send(event) {
            tracing::error!(
                zone = %self.zone_id_for_events,
                path = %key,
                error = %e,
                "mount-event: send failed (consumer gone); DT_MOUNT DLC wiring missed on this apply",
            );
        }
    }

    /// Apply AdjustCounter command — atomic read-modify-write in apply().
    ///
    /// Reads the current i64 value (0 if absent), adds delta, clamps to >= 0,
    /// writes back. All within the serial `apply()` — no race possible.
    /// Returns the new value as `Value(i64 big-endian bytes)`.
    fn apply_adjust_counter(&self, key: &str, delta: i64) -> Result<CommandResult> {
        let current = self
            .metadata
            .get(key.as_bytes())?
            .and_then(|b| <[u8; 8]>::try_from(b.as_slice()).ok())
            .map(i64::from_be_bytes)
            .unwrap_or(0);
        let new_val = (current + delta).max(0);
        self.metadata.set(key.as_bytes(), &new_val.to_be_bytes())?;
        Ok(CommandResult::Value(new_val.to_be_bytes().to_vec()))
    }

    /// Apply CasSetMetadata command — atomic compare-and-swap on version.
    ///
    /// Reads the current value and conditionally writes within a **single
    /// redb WriteTransaction**. This prevents TOCTOU races: no concurrent
    /// writer can observe the same version and succeed.
    fn apply_cas_set_metadata(
        &self,
        key: &str,
        value: &[u8],
        expected_version: u32,
    ) -> Result<CommandResult> {
        let db = self.metadata.raw_db();
        let table_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.metadata.name());
        let write_txn = db
            .begin_write()
            .map_err(|e| super::RaftError::Storage(e.to_string()))?;

        let result;
        {
            let mut table = write_txn
                .open_table(table_def)
                .map_err(|e| super::RaftError::Storage(e.to_string()))?;

            let current_version = match table
                .get(key.as_bytes())
                .map_err(|e: redb::StorageError| super::RaftError::Storage(e.to_string()))?
            {
                Some(guard) => Self::extract_version(guard.value()),
                None => 0,
            };

            if current_version != expected_version {
                result = CommandResult::CasResult {
                    success: false,
                    current_version,
                };
            } else {
                table
                    .insert(key.as_bytes(), value)
                    .map_err(|e| super::RaftError::Storage(e.to_string()))?;

                // The new version is embedded in `value` (serialized by Python).
                // Return expected_version + 1 as a hint, but the authoritative
                // version is in the serialized bytes.
                result = CommandResult::CasResult {
                    success: true,
                    current_version: expected_version + 1,
                };
            }
        }

        write_txn
            .commit()
            .map_err(|e| super::RaftError::Storage(e.to_string()))?;
        Ok(result)
    }

    /// Extract the version field from serialized FileMetadata.
    ///
    /// Supports both protobuf (field 9, varint) and JSON formats.
    /// Returns 0 if extraction fails (treat as "never written").
    fn extract_version(bytes: &[u8]) -> u32 {
        // Try protobuf first: field 9 = tag (9 << 3 | 0) = 72 = 0x48
        // Scan for tag byte 0x48 followed by a varint
        let mut i = 0;
        while i < bytes.len() {
            let tag_byte = bytes[i];
            let field_number = tag_byte >> 3;
            let wire_type = tag_byte & 0x07;

            if field_number == 9 && wire_type == 0 {
                // Found version field — decode varint
                i += 1;
                if i < bytes.len() {
                    return Self::decode_varint(&bytes[i..]) as u32;
                }
            }

            // Skip to next field based on wire type
            i += 1;
            match wire_type {
                0 => {
                    // Varint: skip bytes with MSB set
                    while i < bytes.len() && bytes[i] & 0x80 != 0 {
                        i += 1;
                    }
                    i += 1; // skip final byte
                }
                1 => i += 8, // 64-bit
                2 => {
                    // Length-delimited
                    let (len, consumed) = Self::decode_varint_with_len(&bytes[i..]);
                    i += consumed + len as usize;
                }
                5 => i += 4, // 32-bit
                _ => break,  // unknown wire type
            }
        }

        // Protobuf extraction failed — try JSON fallback
        if let Ok(text) = std::str::from_utf8(bytes) {
            if let Some(pos) = text.find("\"version\"") {
                // Simple JSON extraction: find "version": <number>
                let after = &text[pos + 9..];
                if let Some(colon) = after.find(':') {
                    let num_str = after[colon + 1..].trim_start();
                    let end = num_str
                        .find(|c: char| !c.is_ascii_digit())
                        .unwrap_or(num_str.len());
                    if let Ok(v) = num_str[..end].parse::<u32>() {
                        return v;
                    }
                }
            }
        }

        0 // default: treat as never written
    }

    /// Decode a protobuf varint from bytes.
    fn decode_varint(bytes: &[u8]) -> u64 {
        let mut result: u64 = 0;
        let mut shift = 0u32;
        for &byte in bytes {
            result |= ((byte & 0x7F) as u64) << shift;
            if byte & 0x80 == 0 {
                break;
            }
            shift += 7;
        }
        result
    }

    /// Decode a protobuf varint and return (value, bytes_consumed).
    fn decode_varint_with_len(bytes: &[u8]) -> (u64, usize) {
        let mut result: u64 = 0;
        let mut shift = 0u32;
        for (i, &byte) in bytes.iter().enumerate() {
            result |= ((byte & 0x7F) as u64) << shift;
            if byte & 0x80 == 0 {
                return (result, i + 1);
            }
            shift += 7;
        }
        (result, bytes.len())
    }

    /// Apply DeleteMetadata command.
    fn apply_delete_metadata(&self, key: &str) -> Result<CommandResult> {
        self.metadata.delete(key.as_bytes())?;
        Ok(CommandResult::Success)
    }

    /// Apply AcquireLock — delegates to `LockState::apply_acquire` under
    /// the shared advisory mutex.
    ///
    /// `now` is the wall-clock timestamp captured at proposal time so
    /// all replicas reach identical state (#3029 / Bug 1).
    #[allow(clippy::too_many_arguments)]
    fn apply_acquire_lock(
        &self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
        mode: LockMode,
        now: u64,
    ) -> Result<CommandResult> {
        let mut guard = self.advisory.lock();
        let result =
            guard.apply_acquire(path, lock_id, max_holders, ttl_secs, holder_info, mode, now);
        Ok(CommandResult::LockResult(result))
    }

    /// Apply ReleaseLock — delegates to `LockState::apply_release`.
    fn apply_release_lock(&self, path: &str, lock_id: &str) -> Result<CommandResult> {
        let mut guard = self.advisory.lock();
        if guard.apply_release(path, lock_id) {
            Ok(CommandResult::Success)
        } else if guard.get_lock(path).is_none() {
            Ok(CommandResult::Error("Lock not found".to_string()))
        } else {
            Ok(CommandResult::Error("Lock holder not found".to_string()))
        }
    }

    /// Apply ForceReleaseLock — delegates to `LockState::apply_force_release`.
    fn apply_force_release_lock(&self, path: &str) -> Result<CommandResult> {
        let mut guard = self.advisory.lock();
        if guard.apply_force_release(path) {
            Ok(CommandResult::Success)
        } else {
            Ok(CommandResult::Error("Lock not found".to_string()))
        }
    }

    /// Apply ExtendLock — delegates to `LockState::apply_extend`.
    fn apply_extend_lock(
        &self,
        path: &str,
        lock_id: &str,
        new_ttl_secs: u32,
        now: u64,
    ) -> Result<CommandResult> {
        let mut guard = self.advisory.lock();
        if guard.apply_extend(path, lock_id, new_ttl_secs, now) {
            Ok(CommandResult::Success)
        } else if guard.get_lock(path).is_none() {
            Ok(CommandResult::Error("Lock not found".to_string()))
        } else {
            Ok(CommandResult::Error("Lock holder not found".to_string()))
        }
    }

    /// Get metadata by path.
    pub fn get_metadata(&self, path: &str) -> Result<Option<Vec<u8>>> {
        Ok(self.metadata.get(path.as_bytes())?)
    }

    /// Get metadata for multiple paths in a single call.
    pub fn get_metadata_multi(&self, paths: &[String]) -> Result<Vec<(String, Option<Vec<u8>>)>> {
        paths
            .iter()
            .map(|path| self.get_metadata(path).map(|opt| (path.clone(), opt)))
            .collect()
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

    /// Get lock info by path (reads the shared advisory map).
    pub fn get_lock(&self, path: &str) -> Result<Option<LockInfo>> {
        Ok(self.advisory.lock().get_lock(path))
    }

    /// List all locks matching a prefix (reads the shared advisory map).
    pub fn list_locks(&self, prefix: &str, limit: usize) -> Result<Vec<LockInfo>> {
        Ok(self.advisory.lock().list_locks(prefix, limit))
    }
}

/// Snapshot format for FullStateMachine.
#[derive(Debug, Serialize, Deserialize)]
struct Snapshot {
    /// All metadata entries.
    metadata: HashMap<String, Vec<u8>>,
    /// Advisory lock SSOT at snapshot time (clone of the BTreeMap).
    advisory: LockState,
    /// Last applied index.
    last_applied: u64,
}

impl FullStateMachine {
    /// Shared command dispatch — the actual redb operations.
    ///
    /// Used by `apply_local()` (EC) and `apply_ec_with_lww()`. Each sub-method
    /// opens its own redb transaction internally.
    ///
    /// For the Raft `apply()` path, use `execute_in_txn()` instead — it runs
    /// inside a caller-provided transaction for atomicity with `last_applied`.
    fn execute(&self, command: &Command) -> Result<CommandResult> {
        match command {
            Command::SetMetadata { key, value } => self.apply_set_metadata(key, value),
            Command::CasSetMetadata {
                key,
                value,
                expected_version,
            } => self.apply_cas_set_metadata(key, value, *expected_version),
            Command::DeleteMetadata { key } => self.apply_delete_metadata(key),
            Command::AcquireLock {
                path,
                lock_id,
                max_holders,
                ttl_secs,
                holder_info,
                mode,
                now_secs,
            } => self.apply_acquire_lock(
                path,
                lock_id,
                *max_holders,
                *ttl_secs,
                holder_info,
                *mode,
                *now_secs,
            ),
            Command::ReleaseLock { path, lock_id } => self.apply_release_lock(path, lock_id),
            Command::ForceReleaseLock { path } => self.apply_force_release_lock(path),
            Command::ExtendLock {
                path,
                lock_id,
                new_ttl_secs,
                now_secs,
            } => self.apply_extend_lock(path, lock_id, *new_ttl_secs, *now_secs),
            Command::AdjustCounter { key, delta } => self.apply_adjust_counter(key, *delta),
            Command::Noop => Ok(CommandResult::Success),
        }
    }

    /// Execute a metadata/Noop command inside a caller-provided redb write
    /// transaction. Lock commands don't flow through this path — they only
    /// mutate the in-memory advisory `LockState` and never touch redb.
    ///
    /// This is the transactional variant of `execute()`, used by `apply()` to
    /// ensure metadata mutations and the `last_applied` marker are persisted
    /// atomically in a single redb transaction (matching etcd/CockroachDB/TiKV
    /// practice). Without this, a crash between execute and save_last_applied
    /// could cause non-idempotent commands (e.g. AdjustCounter) to replay.
    fn execute_metadata_in_txn(
        &self,
        txn: &redb::WriteTransaction,
        command: &Command,
    ) -> Result<CommandResult> {
        let meta_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.metadata.name());

        match command {
            Command::SetMetadata { key, value } => {
                let mut table = txn
                    .open_table(meta_def)
                    .map_err(|e| super::RaftError::Storage(format!("open metadata: {e}")))?;
                table
                    .insert(key.as_bytes(), value.as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert metadata: {e}")))?;
                Ok(CommandResult::Success)
            }

            Command::CasSetMetadata {
                key,
                value,
                expected_version,
            } => {
                let mut table = txn
                    .open_table(meta_def)
                    .map_err(|e| super::RaftError::Storage(format!("open metadata: {e}")))?;
                let current = table
                    .get(key.as_bytes())
                    .map_err(|e| super::RaftError::Storage(format!("get metadata: {e}")))?
                    .map(|v| v.value().to_vec());
                let current_version = match &current {
                    Some(bytes) => Self::extract_version(bytes),
                    None => 0,
                };
                if current_version != *expected_version {
                    return Ok(CommandResult::CasResult {
                        success: false,
                        current_version,
                    });
                }
                table
                    .insert(key.as_bytes(), value.as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert metadata: {e}")))?;
                Ok(CommandResult::CasResult {
                    success: true,
                    current_version: expected_version + 1,
                })
            }

            Command::DeleteMetadata { key } => {
                let mut table = txn
                    .open_table(meta_def)
                    .map_err(|e| super::RaftError::Storage(format!("open metadata: {e}")))?;
                table
                    .remove(key.as_bytes())
                    .map_err(|e| super::RaftError::Storage(format!("remove metadata: {e}")))?;
                Ok(CommandResult::Success)
            }

            Command::AdjustCounter { key, delta } => {
                let mut table = txn
                    .open_table(meta_def)
                    .map_err(|e| super::RaftError::Storage(format!("open metadata: {e}")))?;
                let current = table
                    .get(key.as_bytes())
                    .map_err(|e| super::RaftError::Storage(format!("get metadata: {e}")))?
                    .and_then(|v| <[u8; 8]>::try_from(v.value()).ok())
                    .map(i64::from_be_bytes)
                    .unwrap_or(0);
                let new_val = (current + delta).max(0);
                table
                    .insert(key.as_bytes(), new_val.to_be_bytes().as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert counter: {e}")))?;
                Ok(CommandResult::Value(new_val.to_be_bytes().to_vec()))
            }

            Command::Noop => Ok(CommandResult::Success),

            // Lock commands never flow here.
            Command::AcquireLock { .. }
            | Command::ReleaseLock { .. }
            | Command::ForceReleaseLock { .. }
            | Command::ExtendLock { .. } => Err(super::RaftError::InvalidState(
                "execute_metadata_in_txn called with a lock command".into(),
            )),
        }
    }

    fn is_lock_command(command: &Command) -> bool {
        matches!(
            command,
            Command::AcquireLock { .. }
                | Command::ReleaseLock { .. }
                | Command::ForceReleaseLock { .. }
                | Command::ExtendLock { .. }
        )
    }
}

impl StateMachine for FullStateMachine {
    fn apply_local(&mut self, command: &Command) -> Result<CommandResult> {
        match command {
            Command::SetMetadata { .. }
            | Command::CasSetMetadata { .. }
            | Command::DeleteMetadata { .. } => self.execute(command),
            _ => Err(super::RaftError::InvalidState(
                "Only metadata operations (set/delete) support EC local writes".into(),
            )),
        }
    }

    #[cfg(feature = "grpc")]
    fn apply_ec_with_lww(
        &mut self,
        command: &Command,
        entry_timestamp: u64,
    ) -> Result<CommandResult> {
        match command {
            Command::SetMetadata { key, value } => {
                // LWW: compare incoming vs existing modified_at (ISO 8601 lexicographic)
                if let Some(existing) = self.metadata.get(key.as_bytes())? {
                    let incoming_ts = decode_modified_at(value);
                    let existing_ts = decode_modified_at(&existing);
                    if incoming_ts < existing_ts {
                        tracing::trace!(
                            key,
                            incoming = incoming_ts.as_str(),
                            existing = existing_ts.as_str(),
                            "LWW: skipping stale SetMetadata from peer"
                        );
                        return Ok(CommandResult::Success);
                    }
                }
                self.apply_set_metadata(key, value)
            }
            Command::DeleteMetadata { key } => {
                // LWW: compare entry timestamp (u64) vs existing modified_at (parsed to u64)
                if let Some(existing) = self.metadata.get(key.as_bytes())? {
                    let existing_unix = decode_modified_at_unix(&existing);
                    if entry_timestamp < existing_unix {
                        tracing::trace!(
                            key,
                            entry_ts = entry_timestamp,
                            existing_ts = existing_unix,
                            "LWW: skipping stale DeleteMetadata from peer"
                        );
                        return Ok(CommandResult::Success);
                    }
                }
                self.apply_delete_metadata(key)
            }
            _ => Err(super::RaftError::InvalidState(
                "Only metadata operations support EC writes".into(),
            )),
        }
    }

    fn apply(&mut self, index: u64, command: &Command) -> Result<CommandResult> {
        // Lock commands: mutate the in-memory advisory map under its
        // own mutex. They are idempotent under full log replay
        // (acquire/release cycles cancel out, TTL expiry is
        // deterministic from now_secs in the command), so they skip
        // the `last_applied` idempotency guard. This is what lets a
        // follower rebuild its BTreeMap from the log on restart even
        // when `last_applied` has been persisted — the metadata side
        // still uses the guard, but the advisory side needs every
        // committed entry replayed.
        if Self::is_lock_command(command) {
            let result = match command {
                Command::AcquireLock {
                    path,
                    lock_id,
                    max_holders,
                    ttl_secs,
                    holder_info,
                    mode,
                    now_secs,
                } => self.apply_acquire_lock(
                    path,
                    lock_id,
                    *max_holders,
                    *ttl_secs,
                    holder_info,
                    *mode,
                    *now_secs,
                )?,
                Command::ReleaseLock { path, lock_id } => self.apply_release_lock(path, lock_id)?,
                Command::ForceReleaseLock { path } => self.apply_force_release_lock(path)?,
                Command::ExtendLock {
                    path,
                    lock_id,
                    new_ttl_secs,
                    now_secs,
                } => self.apply_extend_lock(path, lock_id, *new_ttl_secs, *now_secs)?,
                _ => unreachable!("is_lock_command filtered non-lock variants"),
            };
            // Track high-water mark in memory for monitoring; we don't
            // persist it for lock-only entries because the idempotency
            // check doesn't apply to them and because their persistent
            // record lives in the raft log + snapshot, not redb.
            if index > self.last_applied {
                self.last_applied = index;
            }
            return Ok(result);
        }

        // Metadata path: skip if we've already applied this index.
        // Protects `AdjustCounter` and similar non-idempotent
        // commands from double-replay on restart.
        if index <= self.last_applied {
            return Ok(CommandResult::Success);
        }

        // Atomic apply: execute the metadata command AND persist
        // `last_applied` in a single redb write transaction. This
        // matches etcd (boltdb txn), CockroachDB (Pebble WriteBatch),
        // and TiKV (RocksDB WriteBatch). Without atomicity, a crash
        // between execute() and save_last_applied() would cause
        // non-idempotent commands to replay on restart, silently
        // diverging from other replicas.
        let db = self.metadata.raw_db();
        let meta_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.metadata.name());

        let write_txn = match db.begin_write() {
            Ok(txn) => txn,
            Err(e) => {
                panic!(
                    "Fatal: cannot begin write transaction for apply at index {}: {}. \
                     Node must be restored from snapshot to recover.",
                    index, e
                );
            }
        };

        // Execute the command within the transaction.
        // Storage errors during apply of committed entries are non-deterministic
        // and unrecoverable — if this replica fails but others succeed, state
        // has diverged. Following etcd/CockroachDB: panic to prevent silent
        // divergence (node must be restored from snapshot).
        let result = match self.execute_metadata_in_txn(&write_txn, command) {
            Ok(result) => result,
            Err(e) => {
                panic!(
                    "Fatal: storage error applying committed entry at index {}: {}. \
                     Node must be restored from snapshot to recover.",
                    index, e
                );
            }
        };

        // Persist last_applied in the SAME transaction — atomic with the
        // command mutation. On crash, either both are persisted or neither.
        match write_txn.open_table(meta_def) {
            Ok(mut table) => {
                if let Err(e) = table.insert(KEY_LAST_APPLIED, index.to_be_bytes().as_slice()) {
                    panic!(
                        "Fatal: failed to write last_applied in apply txn at index {}: {}. \
                         Node must be restored from snapshot to recover.",
                        index, e
                    );
                }
            }
            Err(e) => {
                panic!(
                    "Fatal: failed to open metadata table for last_applied at index {}: {}. \
                     Node must be restored from snapshot to recover.",
                    index, e
                );
            }
        }

        if let Err(e) = write_txn.commit() {
            panic!(
                "Fatal: failed to commit apply transaction at index {}: {}. \
                 Node must be restored from snapshot to recover.",
                index, e
            );
        }

        // Update in-memory state only after successful commit
        self.last_applied = index;

        // R16.2: fire a DT_MOUNT apply event *after* commit. Any send
        // failure is logged but never propagated — returning Err from
        // apply poisons the state machine per raft's "apply must not
        // fail" invariant, and the event is strictly a side-effect.
        #[cfg(feature = "grpc")]
        self.emit_mount_event(command);

        Ok(result)
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

        // Snapshot the advisory map under its own mutex. One clone of
        // the BTreeMap is cheap (shallow tree copy) and lets us drop
        // the mutex before bincoding.
        let advisory = self.advisory.lock().clone();

        let snapshot = Snapshot {
            metadata,
            advisory,
            last_applied: self.last_applied,
        };

        Ok(bincode::serialize(&snapshot)?)
    }

    fn restore_snapshot(&mut self, data: &[u8]) -> Result<()> {
        let snapshot: Snapshot = bincode::deserialize(data)?;

        // Atomic restore for metadata: clear + repopulate in a single
        // redb transaction. Advisory locks are in-memory only — they
        // are replaced under their own mutex after the redb commit.
        let db = self.metadata.raw_db();
        let meta_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.metadata.name());

        let write_txn = db.begin_write().map_err(|e| {
            super::RaftError::Storage(format!("begin_write for snapshot restore: {e}"))
        })?;

        {
            write_txn
                .delete_table(meta_def)
                .map_err(|e| super::RaftError::Storage(format!("delete metadata table: {e}")))?;
            let mut meta_table = write_txn
                .open_table(meta_def)
                .map_err(|e| super::RaftError::Storage(format!("open metadata table: {e}")))?;
            for (path, value) in &snapshot.metadata {
                meta_table
                    .insert(path.as_bytes(), value.as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert metadata: {e}")))?;
            }
            // Persist last_applied inside the same transaction
            meta_table
                .insert(
                    KEY_LAST_APPLIED,
                    snapshot.last_applied.to_be_bytes().as_slice(),
                )
                .map_err(|e| super::RaftError::Storage(format!("insert last_applied: {e}")))?;
        }

        write_txn
            .commit()
            .map_err(|e| super::RaftError::Storage(format!("commit snapshot restore: {e}")))?;

        // Replace advisory state under its mutex. Single acquisition
        // preserves the atomicity invariant: any concurrent reader
        // sees either the full pre-restore map or the full post-
        // restore map, never a torn in-between.
        {
            let mut guard = self.advisory.lock();
            *guard = snapshot.advisory;
        }

        // Update in-memory state only after both writes succeed.
        self.last_applied = snapshot.last_applied;

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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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
                mode,
                now_secs,
            } => {
                assert_eq!(path, "/data/test.txt");
                assert_eq!(lock_id, "uuid-123");
                assert_eq!(max_holders, 3);
                assert_eq!(ttl_secs, 30);
                assert_eq!(holder_info, "agent:test");
                assert_eq!(mode, LockMode::Exclusive);
                assert_eq!(now_secs, 1000);
            }
            _ => panic!("wrong command type"),
        }
    }

    /// R16.2 apply-event hook — one flow covering every branch:
    /// DT_MOUNT with a sender fires exactly one event with the right
    /// payload; DT_DIR never fires; a state machine with no sender
    /// applies normally (hook is pure side-effect — apply must be
    /// unaffected).
    #[cfg(feature = "grpc")]
    #[test]
    fn apply_mount_event_hook_fires_only_on_dt_mount() {
        use crate::raft::mount_event::MountEvent;
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        fn encode(entry_type: i32, zone: &str, target: &str) -> Vec<u8> {
            ProtoFileMetadata {
                entry_type,
                zone_id: zone.to_string(),
                target_zone_id: target.to_string(),
                ..Default::default()
            }
            .encode_to_vec()
        }

        // Sender attached: DT_MOUNT emits once with correct payload.
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<MountEvent>();
        sm.set_mount_event_tx("zone-a".to_string(), tx);

        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/mnt/peer".into(),
                value: encode(2, "zone-a", "zone-b"), // DT_MOUNT
            },
        )
        .unwrap();
        let event = rx.try_recv().expect("DT_MOUNT apply must emit");
        assert_eq!(event.parent_zone_id, "zone-a");
        assert_eq!(event.mount_path, "/mnt/peer");
        assert_eq!(event.target_zone_id, "zone-b");
        assert!(rx.try_recv().is_err(), "only one event per apply");

        // DT_DIR with the same sender → no event.
        sm.apply(
            2,
            &Command::SetMetadata {
                key: "/docs".into(),
                value: encode(1, "zone-a", ""), // DT_DIR
            },
        )
        .unwrap();
        assert!(rx.try_recv().is_err(), "DT_DIR must not emit a mount event");

        // No sender attached: DT_MOUNT applies normally, no panic.
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        let res = sm2
            .apply(
                1,
                &Command::SetMetadata {
                    key: "/mnt/peer".into(),
                    value: encode(2, "zone-a", "zone-b"),
                },
            )
            .unwrap();
        assert!(matches!(res, CommandResult::Success));
        assert_eq!(sm2.last_applied_index(), 1, "apply unaffected without hook");
    }

    /// Determinism regression test (Issue #3029 / Bug 1):
    /// Two state machines applying the same commands must produce byte-identical snapshots.

    #[test]
    fn test_state_machine_determinism() {
        let store1 = RedbStore::open_temporary().unwrap();
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm1 = FullStateMachine::new(&store1).unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();

        // Build a sequence of commands with explicit timestamps
        let commands: Vec<(u64, Command)> = vec![
            (
                1,
                Command::SetMetadata {
                    key: "/file1".into(),
                    value: b"data1".to_vec(),
                },
            ),
            (
                2,
                Command::AcquireLock {
                    path: "/file1".into(),
                    lock_id: "lock-1".into(),
                    max_holders: 1,
                    ttl_secs: 60,
                    holder_info: "agent:a".into(),
                    mode: LockMode::Exclusive,
                    now_secs: 1000,
                },
            ),
            (
                3,
                Command::AcquireLock {
                    path: "/file2".into(),
                    lock_id: "lock-2".into(),
                    max_holders: 3,
                    ttl_secs: 30,
                    holder_info: "agent:b".into(),
                    mode: LockMode::Exclusive,
                    now_secs: 1001,
                },
            ),
            (
                4,
                Command::ExtendLock {
                    path: "/file1".into(),
                    lock_id: "lock-1".into(),
                    new_ttl_secs: 120,
                    now_secs: 1010,
                },
            ),
            (
                5,
                Command::ReleaseLock {
                    path: "/file2".into(),
                    lock_id: "lock-2".into(),
                },
            ),
            // Acquire after TTL-based expiry cleanup
            (
                6,
                Command::AcquireLock {
                    path: "/file2".into(),
                    lock_id: "lock-3".into(),
                    max_holders: 1,
                    ttl_secs: 60,
                    holder_info: "agent:c".into(),
                    mode: LockMode::Exclusive,
                    now_secs: 2000, // well past lock-2's 30s TTL
                },
            ),
        ];

        // Apply identical commands to both state machines
        for (idx, cmd) in &commands {
            sm1.apply(*idx, cmd).unwrap();
            sm2.apply(*idx, cmd).unwrap();
        }

        // Snapshots must be logically identical (HashMap serialization order may vary).
        let snap1 = sm1.snapshot().unwrap();
        let snap2 = sm2.snapshot().unwrap();
        let decoded1: Snapshot = bincode::deserialize(&snap1).unwrap();
        let decoded2: Snapshot = bincode::deserialize(&snap2).unwrap();
        assert_eq!(decoded1.metadata, decoded2.metadata, "Metadata diverged");
        assert_eq!(
            decoded1.advisory.locks, decoded2.advisory.locks,
            "Locks diverged"
        );
        assert_eq!(
            decoded1.last_applied, decoded2.last_applied,
            "last_applied diverged"
        );
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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
            mode: LockMode::Shared,
            now_secs: 1000,
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
            mode: LockMode::Shared,
            now_secs: 1000,
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
            mode: LockMode::Shared,
            now_secs: 1000,
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
            mode: LockMode::Shared,
            now_secs: 1000,
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
            mode: LockMode::Shared,
            now_secs: 1000,
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
                mode: LockMode::Exclusive,
                now_secs: 1000,
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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

        // Acquire a lock with 1-second TTL at time 1000
        let cmd = Command::AcquireLock {
            path: "/test/expire".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 1,
            holder_info: "agent:test1".into(),
            mode: LockMode::Exclusive,
            now_secs: 1000,
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::LockResult(state) = result {
            assert!(state.acquired);
        } else {
            panic!("Expected LockResult");
        }

        // Another holder acquires at time 1002 (after the 1s TTL expired)
        // No sleep needed — deterministic timestamps from the command.
        let cmd2 = Command::AcquireLock {
            path: "/test/expire".into(),
            lock_id: "holder-2".into(),
            max_holders: 1,
            ttl_secs: 30,
            holder_info: "agent:test2".into(),
            mode: LockMode::Exclusive,
            now_secs: 1002,
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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

        // Acquire a lock with 1-second TTL at time 1000 (expires at 1001)
        let cmd = Command::AcquireLock {
            path: "/test/snap-expire".into(),
            lock_id: "holder-1".into(),
            max_holders: 1,
            ttl_secs: 1,
            holder_info: "agent:test1".into(),
            mode: LockMode::Exclusive,
            now_secs: 1000,
        };
        sm.apply(1, &cmd).unwrap();

        // Take snapshot — should still include the expired holder
        // (cleanup happens during acquire, not snapshot; the lock expired at 1001)
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
            mode: LockMode::Exclusive,
            now_secs: 1000,
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

    // ───────────────────────────────────────────────────────────────
    // F4 C1 — LockMode + RW semantics
    // ───────────────────────────────────────────────────────────────

    /// Helper: build an AcquireLock command with the given mode.
    fn acquire_cmd(
        path: &str,
        lock_id: &str,
        max_holders: u32,
        mode: LockMode,
        now_secs: u64,
    ) -> Command {
        Command::AcquireLock {
            path: path.into(),
            lock_id: lock_id.into(),
            max_holders,
            ttl_secs: 60,
            holder_info: format!("agent:{lock_id}"),
            mode,
            now_secs,
        }
    }

    #[test]
    fn test_f4_exclusive_blocks_exclusive() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        let c1 = acquire_cmd("/rw/a", "h1", 1, LockMode::Exclusive, 1000);
        let c2 = acquire_cmd("/rw/a", "h2", 1, LockMode::Exclusive, 1000);

        match sm.apply(1, &c1).unwrap() {
            CommandResult::LockResult(s) => assert!(s.acquired),
            _ => panic!("LockResult"),
        }
        match sm.apply(2, &c2).unwrap() {
            CommandResult::LockResult(s) => assert!(!s.acquired),
            _ => panic!("LockResult"),
        }
    }

    #[test]
    fn test_f4_shared_coexists_up_to_max() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // max_holders=3, three Shared holders all acquire.
        for (idx, id) in ["r1", "r2", "r3"].iter().enumerate() {
            let cmd = acquire_cmd("/rw/b", id, 3, LockMode::Shared, 1000);
            match sm.apply((idx + 1) as u64, &cmd).unwrap() {
                CommandResult::LockResult(s) => assert!(s.acquired, "{} should acquire", id),
                _ => panic!("LockResult"),
            }
        }

        // Fourth Shared holder fails — at capacity.
        let c4 = acquire_cmd("/rw/b", "r4", 3, LockMode::Shared, 1000);
        match sm.apply(4, &c4).unwrap() {
            CommandResult::LockResult(s) => assert!(!s.acquired),
            _ => panic!("LockResult"),
        }
    }

    #[test]
    fn test_f4_exclusive_blocked_by_shared() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        let shared = acquire_cmd("/rw/c", "r1", 3, LockMode::Shared, 1000);
        let excl = acquire_cmd("/rw/c", "w1", 3, LockMode::Exclusive, 1000);

        match sm.apply(1, &shared).unwrap() {
            CommandResult::LockResult(s) => assert!(s.acquired),
            _ => panic!("LockResult"),
        }
        // Exclusive-after-Shared: fail-fast (no waiter queue).
        match sm.apply(2, &excl).unwrap() {
            CommandResult::LockResult(s) => assert!(!s.acquired),
            _ => panic!("LockResult"),
        }
    }

    #[test]
    fn test_f4_shared_blocked_by_exclusive() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        let excl = acquire_cmd("/rw/d", "w1", 3, LockMode::Exclusive, 1000);
        let shared = acquire_cmd("/rw/d", "r1", 3, LockMode::Shared, 1000);

        match sm.apply(1, &excl).unwrap() {
            CommandResult::LockResult(s) => assert!(s.acquired),
            _ => panic!("LockResult"),
        }
        match sm.apply(2, &shared).unwrap() {
            CommandResult::LockResult(s) => assert!(!s.acquired),
            _ => panic!("LockResult"),
        }
    }

    #[test]
    fn test_f4_snapshot_roundtrip_with_mode() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Build a Shared reader-writer lock.
        sm.apply(7, &acquire_cmd("/rw/f", "r1", 3, LockMode::Shared, 1000))
            .unwrap();

        let snap = sm.snapshot().unwrap();
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.restore_snapshot(&snap).unwrap();

        let lock = sm2.get_lock("/rw/f").unwrap().unwrap();
        assert_eq!(lock.holders[0].mode, LockMode::Shared);
        assert_eq!(lock.max_holders, 3);
    }

    #[test]
    fn test_cas_set_metadata_create_new() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // CAS create: expected_version=0, key does not exist → success
        let cmd = Command::CasSetMetadata {
            key: "/cas/new.txt".into(),
            value: b"data-v1".to_vec(),
            expected_version: 0,
        };
        let result = sm.apply(1, &cmd).unwrap();
        if let CommandResult::CasResult {
            success,
            current_version,
        } = result
        {
            assert!(success, "CAS create should succeed");
            assert_eq!(current_version, 1);
        } else {
            panic!("Expected CasResult");
        }

        // Verify data was written
        assert_eq!(
            sm.get_metadata("/cas/new.txt").unwrap(),
            Some(b"data-v1".to_vec())
        );
    }

    #[test]
    fn test_cas_set_metadata_version_mismatch() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Write initial data
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/cas/file.txt".into(),
                value: b"initial".to_vec(),
            },
        )
        .unwrap();

        // CAS with wrong expected_version → failure
        let cmd = Command::CasSetMetadata {
            key: "/cas/file.txt".into(),
            value: b"updated".to_vec(),
            expected_version: 5, // wrong version
        };
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::CasResult {
            success,
            current_version,
        } = result
        {
            assert!(!success, "CAS should fail on version mismatch");
            // current_version depends on what extract_version returns for raw bytes
            assert_eq!(current_version, 0); // raw bytes without protobuf → 0
        } else {
            panic!("Expected CasResult");
        }

        // Verify data was NOT overwritten
        assert_eq!(
            sm.get_metadata("/cas/file.txt").unwrap(),
            Some(b"initial".to_vec())
        );
    }

    #[test]
    fn test_cas_set_metadata_create_exists() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Write initial data with a version field (JSON format, version=1)
        let json_data = br#"{"path":"/cas/exists.txt","version":1,"size":6}"#;
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/cas/exists.txt".into(),
                value: json_data.to_vec(),
            },
        )
        .unwrap();

        // CAS create (expected_version=0) when file already exists with version=1 → failure
        let cmd = Command::CasSetMetadata {
            key: "/cas/exists.txt".into(),
            value: b"new-data".to_vec(),
            expected_version: 0,
        };
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::CasResult {
            success,
            current_version,
        } = result
        {
            assert!(!success, "CAS create should fail when file exists");
            assert_eq!(current_version, 1);
        } else {
            panic!("Expected CasResult");
        }

        // Verify data was NOT overwritten
        assert_eq!(
            sm.get_metadata("/cas/exists.txt").unwrap(),
            Some(json_data.to_vec())
        );
    }

    #[test]
    fn test_cas_set_metadata_json_version_extraction() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Write JSON metadata with version field
        let json_data = br#"{"path":"/test","version":3,"size":100}"#;
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/cas/json.txt".into(),
                value: json_data.to_vec(),
            },
        )
        .unwrap();

        // CAS with correct version → success
        let cmd = Command::CasSetMetadata {
            key: "/cas/json.txt".into(),
            value: br#"{"path":"/test","version":4,"size":200}"#.to_vec(),
            expected_version: 3,
        };
        let result = sm.apply(2, &cmd).unwrap();
        if let CommandResult::CasResult { success, .. } = result {
            assert!(success, "CAS should succeed with correct JSON version");
        } else {
            panic!("Expected CasResult");
        }

        // CAS with wrong version → failure
        let cmd2 = Command::CasSetMetadata {
            key: "/cas/json.txt".into(),
            value: br#"{"path":"/test","version":5,"size":300}"#.to_vec(),
            expected_version: 3, // stale — actual is 4 now
        };
        let result = sm.apply(3, &cmd2).unwrap();
        if let CommandResult::CasResult {
            success,
            current_version,
        } = result
        {
            assert!(!success, "CAS should fail with stale version");
            assert_eq!(current_version, 4);
        } else {
            panic!("Expected CasResult");
        }
    }

    #[test]
    fn test_adjust_counter() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        // Increment from zero
        let result = sm
            .apply(
                1,
                &Command::AdjustCounter {
                    key: "__i_links_count__".into(),
                    delta: 1,
                },
            )
            .unwrap();
        if let CommandResult::Value(bytes) = result {
            let val = i64::from_be_bytes(bytes.try_into().unwrap());
            assert_eq!(val, 1);
        } else {
            panic!("Expected Value result");
        }

        // Increment again
        let result = sm
            .apply(
                2,
                &Command::AdjustCounter {
                    key: "__i_links_count__".into(),
                    delta: 1,
                },
            )
            .unwrap();
        if let CommandResult::Value(bytes) = result {
            let val = i64::from_be_bytes(bytes.try_into().unwrap());
            assert_eq!(val, 2);
        } else {
            panic!("Expected Value result");
        }

        // Decrement
        let result = sm
            .apply(
                3,
                &Command::AdjustCounter {
                    key: "__i_links_count__".into(),
                    delta: -1,
                },
            )
            .unwrap();
        if let CommandResult::Value(bytes) = result {
            let val = i64::from_be_bytes(bytes.try_into().unwrap());
            assert_eq!(val, 1);
        } else {
            panic!("Expected Value result");
        }

        // Decrement below zero should clamp to 0
        let result = sm
            .apply(
                4,
                &Command::AdjustCounter {
                    key: "__i_links_count__".into(),
                    delta: -100,
                },
            )
            .unwrap();
        if let CommandResult::Value(bytes) = result {
            let val = i64::from_be_bytes(bytes.try_into().unwrap());
            assert_eq!(val, 0);
        } else {
            panic!("Expected Value result");
        }
    }

    #[test]
    fn test_apply_idempotency_guard() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        let cmd = Command::SetMetadata {
            key: "/test".into(),
            value: b"data".to_vec(),
        };
        sm.apply(1, &cmd).unwrap();
        assert_eq!(sm.last_applied_index(), 1);
        let result = sm
            .apply(
                1,
                &Command::DeleteMetadata {
                    key: "/test".into(),
                },
            )
            .unwrap();
        assert!(matches!(result, CommandResult::Success));
        assert_eq!(sm.get_metadata("/test").unwrap(), Some(b"data".to_vec()));
        assert_eq!(sm.last_applied_index(), 1);
        let result = sm.apply(0, &Command::Noop).unwrap();
        assert!(matches!(result, CommandResult::Success));
        assert_eq!(sm.last_applied_index(), 1);
    }

    #[test]
    fn test_apply_advances_last_applied_sequentially() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        for i in 1..=5 {
            sm.apply(i, &Command::Noop).unwrap();
            assert_eq!(sm.last_applied_index(), i);
        }
        let sm2 = FullStateMachine::new(&store).unwrap();
        assert_eq!(sm2.last_applied_index(), 5);
    }

    #[test]
    fn test_apply_skips_gaps_correctly() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        sm.apply(1, &Command::Noop).unwrap();
        sm.apply(
            5,
            &Command::SetMetadata {
                key: "/test".into(),
                value: b"data".to_vec(),
            },
        )
        .unwrap();
        assert_eq!(sm.last_applied_index(), 5);
        assert_eq!(sm.get_metadata("/test").unwrap(), Some(b"data".to_vec()));
    }

    #[test]
    fn test_restore_snapshot_corrupt_data_preserves_state() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/existing".into(),
                value: b"original".to_vec(),
            },
        )
        .unwrap();
        assert_eq!(sm.last_applied_index(), 1);
        let result = sm.restore_snapshot(b"this is not valid bincode");
        assert!(result.is_err(), "corrupt snapshot should return error");
        assert_eq!(
            sm.get_metadata("/existing").unwrap(),
            Some(b"original".to_vec())
        );
        assert_eq!(sm.last_applied_index(), 1);
    }

    #[test]
    fn test_restore_snapshot_empty_data() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        let result = sm.restore_snapshot(b"");
        assert!(result.is_err(), "empty snapshot should return error");
    }

    #[test]
    fn test_restore_snapshot_overwrites_existing_data() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/old_file".into(),
                value: b"old_data".to_vec(),
            },
        )
        .unwrap();
        sm.apply(
            2,
            &Command::AcquireLock {
                path: "/old_file".into(),
                lock_id: "lock-old".into(),
                max_holders: 1,
                ttl_secs: 3600,
                holder_info: "agent:old".into(),
                mode: LockMode::Exclusive,
                now_secs: 1000,
            },
        )
        .unwrap();
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.apply(
            1,
            &Command::SetMetadata {
                key: "/new_file".into(),
                value: b"new_data".to_vec(),
            },
        )
        .unwrap();
        let snapshot_data = sm2.snapshot().unwrap();
        sm.restore_snapshot(&snapshot_data).unwrap();
        assert!(sm.get_metadata("/old_file").unwrap().is_none());
        assert!(sm.get_lock("/old_file").unwrap().is_none());
        assert_eq!(
            sm.get_metadata("/new_file").unwrap(),
            Some(b"new_data".to_vec())
        );
        assert_eq!(sm.last_applied_index(), 1);
    }

    #[test]
    fn test_restore_snapshot_persists_atomically() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.apply(
            1,
            &Command::SetMetadata {
                key: "/persisted".into(),
                value: b"value".to_vec(),
            },
        )
        .unwrap();
        sm2.apply(
            2,
            &Command::AcquireLock {
                path: "/persisted".into(),
                lock_id: "lock-1".into(),
                max_holders: 1,
                ttl_secs: 3600,
                holder_info: "agent:test".into(),
                mode: LockMode::Exclusive,
                now_secs: 1000,
            },
        )
        .unwrap();
        let snapshot_data = sm2.snapshot().unwrap();
        sm.restore_snapshot(&snapshot_data).unwrap();

        // Metadata persists across reopens (redb-backed).
        let sm3 = FullStateMachine::new(&store).unwrap();
        assert_eq!(
            sm3.get_metadata("/persisted").unwrap(),
            Some(b"value".to_vec())
        );
        assert_eq!(sm3.last_applied_index(), 2);

        // Advisory locks are in-memory only after R14 — reopening
        // constructs a fresh empty `Arc<Mutex<LockState>>`. Rebuilding
        // the BTreeMap is the job of the raft replay + snapshot restore
        // path on startup, not this plain `new(&store)` constructor.
        assert!(sm3.get_lock("/persisted").unwrap().is_none());

        // The same-instance sm (the one that received restore_snapshot)
        // does hold the advisory state — the mutex was repopulated in
        // place.
        assert!(sm.get_lock("/persisted").unwrap().is_some());
    }

    // ═══════════════════════════════════════════════════════════════
    // R14 — SSOT invariants: shared Arc, atomicity, rehydration
    // ═══════════════════════════════════════════════════════════════

    /// The `advisory_state()` handle is a clone of the Arc the apply
    /// path writes into — mutations made via `sm.apply(AcquireLock)`
    /// must be visible to any external holder of the same Arc without
    /// taking a second snapshot or restart.
    #[test]
    fn r14_apply_is_visible_through_shared_arc() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        let shared = sm.advisory_state();

        sm.apply(
            1,
            &Command::AcquireLock {
                path: "/r14/a".into(),
                lock_id: "h1".into(),
                max_holders: 1,
                ttl_secs: 60,
                holder_info: "agent".into(),
                mode: LockMode::Exclusive,
                now_secs: 1000,
            },
        )
        .unwrap();

        // The external Arc sees the committed state immediately — no
        // ReadIndex, no redb round-trip.
        let guard = shared.lock();
        let entry = guard.get_lock("/r14/a").expect("lock should exist");
        assert_eq!(entry.holders.len(), 1);
        assert_eq!(entry.holders[0].lock_id, "h1");
    }

    /// Pre-populating the advisory Arc **before** constructing the
    /// state machine must survive: the state machine's apply path
    /// operates on that same Arc (no copy / replace on construction).
    /// This models the kernel's upgrade path where LockManager hands
    /// its Arc into FullStateMachine::with_advisory.
    #[test]
    fn r14_with_advisory_preserves_preexisting_holders() {
        use std::sync::Arc;

        let store = RedbStore::open_temporary().unwrap();
        let advisory = Arc::new(Mutex::new(LockState::new()));

        // Simulate local-mode kernel holders that existed pre-upgrade.
        {
            let mut guard = advisory.lock();
            guard.apply_acquire(
                "/r14/pre",
                "local-h1",
                1,
                60,
                "agent",
                LockMode::Exclusive,
                1000,
            );
        }

        let sm = FullStateMachine::with_advisory(&store, advisory.clone()).unwrap();

        // The state machine sees the pre-existing holder via shared Arc.
        assert!(sm.get_lock("/r14/pre").unwrap().is_some());
    }

    /// Snapshot serialization of the advisory BTreeMap is lossless —
    /// snapshot → fresh state machine → restore_snapshot produces an
    /// identical map.
    #[test]
    fn r14_snapshot_roundtrip_advisory_only() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        for (idx, (path, id, mode)) in [
            ("/r14/a", "h1", LockMode::Exclusive),
            ("/r14/b", "r1", LockMode::Shared),
            ("/r14/b", "r2", LockMode::Shared),
        ]
        .iter()
        .enumerate()
        {
            sm.apply(
                (idx + 1) as u64,
                &Command::AcquireLock {
                    path: path.to_string(),
                    lock_id: id.to_string(),
                    max_holders: 3,
                    ttl_secs: 60,
                    holder_info: "agent".into(),
                    mode: *mode,
                    now_secs: 1000,
                },
            )
            .unwrap();
        }

        let snap = sm.snapshot().unwrap();

        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.restore_snapshot(&snap).unwrap();

        let a = sm2.get_lock("/r14/a").unwrap().unwrap();
        assert_eq!(a.holders.len(), 1);
        assert_eq!(a.holders[0].lock_id, "h1");

        let b = sm2.get_lock("/r14/b").unwrap().unwrap();
        assert_eq!(b.holders.len(), 2);
        assert_eq!(b.max_holders, 3);
    }

    /// Lock commands are idempotent under full log replay — applying
    /// the same committed entry a second time (simulating a post-
    /// restart replay) produces the same final state, not a double-
    /// apply. This is the invariant that lets the apply() lock-path
    /// skip the `index <= last_applied` guard without corrupting state.
    #[test]
    fn r14_lock_replay_is_idempotent() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        let cmd = Command::AcquireLock {
            path: "/r14/replay".into(),
            lock_id: "h1".into(),
            max_holders: 1,
            ttl_secs: 60,
            holder_info: "agent".into(),
            mode: LockMode::Exclusive,
            now_secs: 1000,
        };
        sm.apply(1, &cmd).unwrap();
        // Replay: raft-rs may re-emit the same committed entry on
        // restart if our reported applied lags. Apply must be a no-op.
        sm.apply(1, &cmd).unwrap();

        let info = sm.get_lock("/r14/replay").unwrap().unwrap();
        assert_eq!(info.holders.len(), 1);
    }

    /// Apply and external reads share a single mutex. A reader can
    /// never observe a half-applied lock (an entry where max_holders
    /// was updated but the new holder hasn't been appended yet)
    /// because `LockState::apply_acquire` runs as a single
    /// mutate-under-guard step. Stress this with N writers + M
    /// readers; every read snapshot must reflect a complete,
    /// consistent state.
    #[test]
    fn r14_apply_and_read_are_atomic_under_contention() {
        use std::sync::atomic::{AtomicU64, Ordering};
        use std::sync::Arc;

        let store = RedbStore::open_temporary().unwrap();
        let sm = Arc::new(std::sync::Mutex::new(
            FullStateMachine::new(&store).unwrap(),
        ));
        let shared = sm.lock().unwrap().advisory_state();

        let next_idx = Arc::new(AtomicU64::new(1));
        let writer_count = 32;
        let reader_count = 32;

        let mut handles = Vec::new();

        for i in 0..writer_count {
            let sm = sm.clone();
            let next_idx = next_idx.clone();
            handles.push(std::thread::spawn(move || {
                let idx = next_idx.fetch_add(1, Ordering::Relaxed);
                let path = format!("/r14/stress/{}", i % 8);
                let id = format!("h{}", i);
                sm.lock()
                    .unwrap()
                    .apply(
                        idx,
                        &Command::AcquireLock {
                            path,
                            lock_id: id,
                            max_holders: 32,
                            ttl_secs: 60,
                            holder_info: "agent".into(),
                            mode: LockMode::Shared,
                            now_secs: 1000,
                        },
                    )
                    .unwrap();
            }));
        }

        for _ in 0..reader_count {
            let shared = shared.clone();
            handles.push(std::thread::spawn(move || {
                for _ in 0..100 {
                    let guard = shared.lock();
                    // Invariant: for every entry with holders,
                    // `max_holders` is non-zero (seeded atomically
                    // with the first push).
                    for entry in guard.locks.values() {
                        if !entry.holders.is_empty() {
                            assert!(
                                entry.max_holders > 0,
                                "observed entry with holders but max_holders=0",
                            );
                        }
                    }
                }
            }));
        }

        for h in handles {
            h.join().unwrap();
        }

        let guard = shared.lock();
        let total_holders: usize = guard.locks.values().map(|e| e.holders.len()).sum();
        assert!(total_holders > 0);
        assert!(total_holders <= writer_count as usize);
    }

    /// Snapshot taken while apply is in-flight captures a
    /// point-in-time state — no torn reads of the BTreeMap. The
    /// snapshot's BTreeMap clone happens under the advisory mutex
    /// (same as apply), so either the apply completed before the
    /// clone (snapshot sees it) or after (snapshot doesn't). Never
    /// neither / both.
    #[test]
    fn r14_snapshot_under_concurrent_apply_is_consistent() {
        use std::sync::atomic::{AtomicU64, Ordering};
        use std::sync::Arc;

        let store = RedbStore::open_temporary().unwrap();
        let sm = Arc::new(std::sync::Mutex::new(
            FullStateMachine::new(&store).unwrap(),
        ));
        let next_idx = Arc::new(AtomicU64::new(1));
        let snapshots = Arc::new(std::sync::Mutex::new(Vec::<Vec<u8>>::new()));

        let sm_w = sm.clone();
        let next_idx_w = next_idx.clone();
        let writer = std::thread::spawn(move || {
            for i in 0..50 {
                let idx = next_idx_w.fetch_add(1, Ordering::Relaxed);
                sm_w.lock()
                    .unwrap()
                    .apply(
                        idx,
                        &Command::AcquireLock {
                            path: format!("/r14/sn/{}", i),
                            lock_id: format!("h{}", i),
                            max_holders: 1,
                            ttl_secs: 60,
                            holder_info: "agent".into(),
                            mode: LockMode::Exclusive,
                            now_secs: 1000,
                        },
                    )
                    .unwrap();
            }
        });

        let sm_s = sm.clone();
        let snapshots_s = snapshots.clone();
        let snapper = std::thread::spawn(move || {
            for _ in 0..5 {
                let bytes = sm_s.lock().unwrap().snapshot().unwrap();
                snapshots_s.lock().unwrap().push(bytes);
            }
        });

        writer.join().unwrap();
        snapper.join().unwrap();

        // Every captured snapshot must deserialize and reproduce a
        // self-consistent map — holders only on entries whose
        // `max_holders > 0`.
        for bytes in snapshots.lock().unwrap().iter() {
            let snap: Snapshot = bincode::deserialize(bytes).unwrap();
            for entry in snap.advisory.locks.values() {
                if !entry.holders.is_empty() {
                    assert!(entry.max_holders > 0);
                }
            }
        }
    }
}
