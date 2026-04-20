//! State machine trait for Raft consensus.
//!
//! The state machine defines what operations can be applied through Raft.
//! For STRONG_HA zones, this includes metadata and lock operations
//! (NOT file data - that stays in CAS/S3).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
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

    /// Append a raw-byte entry to a dedicated stream table (R19.1b').
    ///
    /// Used by the kernel's WAL stream backend to persist ordered
    /// stream entries without shoehorning the payload through the
    /// ``FileMetadata`` proto. Stored in ``TREE_STREAM_ENTRIES``
    /// (distinct from ``TREE_METADATA``) so list scans and snapshot
    /// walkers do not confuse stream payload with file metadata.
    AppendStreamEntry {
        /// Full stream key (typically ``/__wal_stream__/<id>/<seq>``).
        key: String,
        /// Raw payload bytes — no encoding applied.
        data: Vec<u8>,
    },

    /// Delete a single stream entry by key (R19.1b').
    DeleteStreamEntry {
        /// Stream key to remove.
        key: String,
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

    /// Return a shared atomic counter reflecting ``last_applied_index``.
    ///
    /// Implementors that need to expose applied progress to sync readers
    /// outside the state-machine's async RwLock return an ``Arc`` clone
    /// of the atomic they own internally. Default is ``None`` — callers
    /// that need the signal must then fall back to ``last_applied_index``
    /// under the async lock.
    ///
    /// Only ``FullStateMachine`` implements this; witness / in-memory
    /// test state machines return ``None`` because nothing outside
    /// raft reads their applied progress.
    fn last_applied_shared(&self) -> Option<Arc<AtomicU64>> {
        None
    }

    /// Optional apply-side cache-invalidation slot.
    ///
    /// State machines that back a kernel DCache (notably
    /// ``FullStateMachine``) return their shared ``Arc<RwLock<...>>``
    /// so a downstream consumer (``ZoneConsensus`` and, transitively,
    /// the kernel) can ``write().replace()`` a callback that fires on
    /// every committed metadata mutation. State machines that have no
    /// such coherence concern (witness, direct-drive test harnesses)
    /// return ``None`` via the default impl, and apply stays a pure
    /// no-op on that front.
    #[allow(clippy::type_complexity)]
    fn invalidate_cb_slot(
        &self,
    ) -> Option<Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&str) + Send + Sync>>>>> {
        None
    }

    /// Optional apply-side DT_MOUNT slot (R20.16.2).
    ///
    /// Symmetric to [`invalidate_cb_slot`] but fires only for DT_MOUNT
    /// Set / Delete commits — the kernel installs a closure that wires
    /// / unwires federation mounts synchronously at apply time on every
    /// replica. The closure captures the parent ``zone_id`` at install
    /// time (install is per-zone, mirroring
    /// ``install_federation_dcache_coherence``), so the event payload
    /// carries only data that varies per-apply: the mount key and, on
    /// Set, the decoded proto fields needed for wiring.
    ///
    /// Only [`FullStateMachine`] exposes a slot; witness / in-memory
    /// state machines return ``None`` so apply stays a no-op.
    #[cfg(feature = "grpc")]
    #[allow(clippy::type_complexity)]
    fn mount_apply_cb_slot(
        &self,
    ) -> Option<Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&MountApplyEvent) + Send + Sync>>>>> {
        None
    }
}

/// A DT_MOUNT apply event delivered to [`StateMachine::mount_apply_cb_slot`]
/// callbacks (R20.16.2).
///
/// Fires after the apply write transaction commits. All payload fields
/// derive from replicated state (raft log `Command` + state-machine
/// `FileMetadata` proto), so every replica firing this event observes
/// identical data — no node-local truth is introduced.
///
/// The callback's parent ``zone_id`` is captured by the install site
/// (kernel's ``install_federation_mount_coherence(consensus)``) as a
/// closure binding, not carried on the event. This matches the pattern
/// used by ``invalidate_cb``.
#[cfg(feature = "grpc")]
#[derive(Debug, Clone)]
pub enum MountApplyEvent {
    /// DT_MOUNT upsert. ``target_zone_id`` + ``backend_name`` come from
    /// the decoded ``FileMetadata`` proto written by this apply —
    /// snapshotted at fire time so a subsequent apply to the same key
    /// cannot race the callback into wiring the wrong target.
    ///
    /// Post-R20.16.8 / R20.16.9 the event does NOT carry io_profile /
    /// readonly / admin_only — those knobs were retired as YAGNI.
    Set {
        key: String,
        target_zone_id: String,
        backend_name: String,
    },
    /// DT_MOUNT delete. No proto payload — the entry was removed inside
    /// the same apply txn, so callers look up the prior mount via their
    /// own reverse index (e.g. kernel ``cross_zone_mounts``) to drive
    /// cascade-unmount.
    Delete { key: String },
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
/// Dedicated redb tree for raw-byte stream entries (R19.1b').
///
/// Holds ``Command::AppendStreamEntry`` payloads separate from
/// ``TREE_METADATA`` so the WAL stream backend does not pollute file
/// metadata scans / snapshots with hex-encoded payload rows. Keys are
/// opaque strings (the kernel picks a ``/__wal_stream__/<id>/<seq>``
/// convention); values are raw bytes.
const TREE_STREAM_ENTRIES: &str = "sm_stream_entries";
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
    /// Raw-byte stream entries tree (R19.1b') — key -> opaque bytes.
    ///
    /// Distinct from ``metadata`` so WAL stream payloads never appear
    /// in file-listing scans / snapshots that walk ``sm_metadata``.
    stream_entries: RedbTree,
    /// Advisory lock SSOT — shared with the kernel's `LockManager`.
    advisory: Arc<Mutex<LockState>>,
    /// Last applied metadata/Noop log index (persisted to redb).
    ///
    /// Gates metadata-command idempotency during log replay —
    /// `AdjustCounter` would double-count otherwise. Lock commands
    /// are idempotent under full replay (acquire/release cycles
    /// cancel out) so they ignore this guard and always apply.
    ///
    /// Held as ``Arc<AtomicU64>`` so the ZoneConsensus handle can
    /// publish its current value to sync Python readers (gate tests,
    /// monitoring) without acquiring the state-machine's async
    /// RwLock. The state machine is the SSOT; the Arc is how we
    /// surface it, not a shadow copy.
    last_applied: Arc<AtomicU64>,
    /// Apply-side DT_MOUNT slot (R20.16.2) — shared
    /// ``Arc<RwLock<Option<Arc<Fn(&MountApplyEvent)>>>>`` so the kernel
    /// mount-wiring owner can install / replace the callback *after*
    /// this state machine is moved into ``ZoneConsensus``.
    ///
    /// Fires on every committed DT_MOUNT ``SetMetadata`` /
    /// ``DeleteMetadata`` so the kernel can wire / unwire a federation
    /// child-zone mount into the local ``MountTable`` on every replica
    /// (including the leader — the leader also goes through apply).
    /// Set carries the decoded ``FileMetadata`` proto snapshot so the
    /// callback doesn't race a subsequent apply to the same key; Delete
    /// carries only the key because the proto was removed in the same
    /// txn.
    ///
    /// ``None`` when no federation mount coherence is wired (tests,
    /// witness nodes). Send-site is gated on ``is_some()`` so apply
    /// stays a no-op when unset. Callback panics are surfaced via
    /// ``catch_unwind`` so apply can't be poisoned per raft's "apply
    /// must not fail" rule.
    #[cfg(feature = "grpc")]
    #[allow(clippy::type_complexity)]
    mount_apply_cb: Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&MountApplyEvent) + Send + Sync>>>>,
    /// Apply-side dcache invalidation slot — shared ``Arc<RwLock>``
    /// so the kernel DCache owner can install / replace the callback
    /// *after* this state machine is moved into ``ZoneConsensus``.
    ///
    /// Fires on every committed metadata mutation (``SetMetadata`` /
    /// ``CasSetMetadata`` / ``DeleteMetadata``) so the kernel's
    /// DCache can evict stale entries on nodes that did not originate
    /// the write (leader-forwarded writes, catch-up replication).
    ///
    /// The callback receives the zone-relative key actually mutated;
    /// the installer closes over mount-point → global-path translation
    /// so the state machine stays ignorant of the VFS layout above it.
    ///
    /// ``None`` when no kernel DCache is wired (tests, witness nodes).
    /// Send-site is gated on ``is_some()`` so apply stays a no-op when
    /// unset. Callback panics are surfaced via ``catch_unwind`` so
    /// apply can't be poisoned per raft's "apply must not fail" rule.
    #[allow(clippy::type_complexity)]
    invalidate_cb: Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&str) + Send + Sync>>>>,
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
        let stream_entries = store.tree(TREE_STREAM_ENTRIES)?;

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
            stream_entries,
            advisory,
            last_applied: Arc::new(AtomicU64::new(last_applied)),
            #[cfg(feature = "grpc")]
            mount_apply_cb: Arc::new(parking_lot::RwLock::new(None)),
            invalidate_cb: Arc::new(parking_lot::RwLock::new(None)),
        })
    }

    /// Return a clone of the shared ``last_applied`` atomic so a caller
    /// outside the state-machine's RwLock can publish "state machine has
    /// this index" as an atomic read. The state machine remains the
    /// SSOT; this is how the value is surfaced, not a shadow.
    pub fn last_applied_shared_arc(&self) -> Arc<AtomicU64> {
        Arc::clone(&self.last_applied)
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

    /// Peek at a key's current committed value and decide whether it's
    /// a DT_MOUNT entry. Used by ``apply`` as a pre-read before the
    /// write txn to capture the pre-delete payload-classification for
    /// ``DeleteMetadata`` commands (R20.16.2).
    ///
    /// Apply is serial, so no concurrent writer can mutate ``key``
    /// between this pre-read and the write txn that performs the
    /// delete. The read happens in its own short read txn.
    ///
    /// Returns ``true`` iff the current value decodes as a
    /// ``FileMetadata`` with ``entry_type == DT_MOUNT``. Decode failure
    /// or missing key both return ``false`` — those cases aren't
    /// DT_MOUNT and don't need a Delete event.
    #[cfg(feature = "grpc")]
    fn peek_is_dt_mount(&self, key: &str) -> bool {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        const DT_MOUNT: i32 = 2;
        let Ok(Some(bytes)) = self.metadata.get(key.as_bytes()) else {
            return false;
        };
        match ProtoFileMetadata::decode(bytes.as_slice()) {
            Ok(p) => p.entry_type == DT_MOUNT,
            Err(_) => false,
        }
    }

    /// Fire the apply-side DT_MOUNT callback for a committed command
    /// (R20.16.2).
    ///
    /// Set path: decode the ``SetMetadata`` value; if it's a DT_MOUNT
    /// with non-empty ``target_zone_id``, build a snapshot event from
    /// the proto and invoke the callback.
    ///
    /// Delete path: the caller has already done the pre-read (inside
    /// ``apply`` before the write txn) and hands us ``delete_mount_key``
    /// = ``Some(key)`` iff the deleted entry was DT_MOUNT.
    ///
    /// Failure modes never propagate out of ``apply``:
    /// - no callback installed → no-op
    /// - non-SetMetadata / non-DeleteMetadata command → no-op
    /// - proto decode fails on Set → ``warn!`` (upstream writer wrote
    ///   garbage; apply can't reject committed entries)
    /// - entry not DT_MOUNT or empty target on Set → no-op (normal)
    /// - callback panics → ``catch_unwind`` logs ``error!`` and
    ///   returns; apply proceeds (raft "apply must not fail")
    #[cfg(feature = "grpc")]
    fn emit_mount_apply_event(&self, command: &Command, delete_mount_key: Option<&str>) {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        // Snapshot the cb under the read lock, release before invoking
        // — callback must not reacquire this lock (future installer
        // would deadlock) and must stay short to not stall apply.
        let cb_opt = self.mount_apply_cb.read().clone();
        let Some(cb) = cb_opt else {
            return;
        };

        let event = match command {
            Command::SetMetadata { key, value } => {
                let proto = match ProtoFileMetadata::decode(value.as_slice()) {
                    Ok(p) => p,
                    Err(e) => {
                        tracing::warn!(
                            path = %key,
                            error = %e,
                            "mount-apply: FileMetadata decode failed on apply (non-FileMetadata SetMetadata?)",
                        );
                        return;
                    }
                };
                const DT_MOUNT: i32 = 2;
                if proto.entry_type != DT_MOUNT || proto.target_zone_id.is_empty() {
                    return;
                }
                MountApplyEvent::Set {
                    key: key.clone(),
                    target_zone_id: proto.target_zone_id,
                    backend_name: proto.backend_name,
                }
            }
            Command::DeleteMetadata { key } => match delete_mount_key {
                Some(k) if k == key => MountApplyEvent::Delete { key: key.clone() },
                _ => return,
            },
            _ => return,
        };

        if let Err(payload) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| cb(&event)))
        {
            tracing::error!(
                payload = ?payload,
                "mount-apply: callback panicked; continuing apply — mount wiring may be incomplete",
            );
        }
    }

    /// Fire the apply-side dcache invalidation callback for the key
    /// mutated by this committed command.
    ///
    /// Fires for ``SetMetadata`` / ``CasSetMetadata`` / ``DeleteMetadata``
    /// (the only variants that change a user-visible metastore key).
    /// ``AdjustCounter`` mutates internal counters (``__i_links_count__``)
    /// that the dcache does not cache, so it is intentionally skipped.
    ///
    /// The callback is wrapped in ``catch_unwind`` so a panicking
    /// installer (bad closure capture, dropped dcache) cannot poison
    /// apply — raft requires apply to be infallible on the happy path.
    fn emit_invalidate_event(&self, command: &Command) {
        // Snapshot the cb under the read lock, release before invoking
        // — the callback must never acquire this lock back (would
        // deadlock a future installer) and must not take long enough
        // to stall the apply loop.
        let cb_opt = self.invalidate_cb.read().clone();
        let Some(cb) = cb_opt else {
            return;
        };
        let key = match command {
            Command::SetMetadata { key, .. }
            | Command::CasSetMetadata { key, .. }
            | Command::DeleteMetadata { key } => key.as_str(),
            _ => return,
        };
        let key_owned = key.to_string();
        if let Err(payload) =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| cb(&key_owned)))
        {
            tracing::error!(
                key = %key_owned,
                payload = ?payload,
                "invalidate-event: callback panicked; continuing apply — dcache may be stale for this key"
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

    /// Iterate every DT_MOUNT entry in this state machine, returning
    /// ``(key, target_zone_id, backend_name)`` triples (R20.16.4).
    ///
    /// Used by the kernel's startup replay to re-drive every historic
    /// federation mount through ``wire_federation_mount`` — apply-cb
    /// only fires on new applies, so restart-from-snapshot wouldn't
    /// otherwise wire the mounts already in state.
    ///
    /// Lenient: skips entries that fail to decode or aren't DT_MOUNT
    /// or have an empty target_zone_id.
    #[cfg(feature = "grpc")]
    pub fn iter_dt_mount_entries(&self) -> Result<Vec<(String, String, String)>> {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        let mut result = Vec::new();
        for (key, bytes) in self.list_metadata("/")? {
            let Ok(proto) = ProtoFileMetadata::decode(bytes.as_slice()) else {
                continue;
            };
            const DT_MOUNT: i32 = 2;
            if proto.entry_type == DT_MOUNT && !proto.target_zone_id.is_empty() {
                result.push((key, proto.target_zone_id, proto.backend_name));
            }
        }
        Ok(result)
    }

    /// Get a stream entry by key (R19.1b').
    ///
    /// Looks up the opaque bytes previously stored by
    /// ``Command::AppendStreamEntry``. Returns ``Ok(None)`` if absent.
    pub fn get_stream_entry(&self, key: &str) -> Result<Option<Vec<u8>>> {
        Ok(self.stream_entries.get(key.as_bytes())?)
    }

    /// Delete every stream entry whose key begins with ``prefix`` (R19.1b').
    ///
    /// Returns the number of rows removed. Used when a WAL stream is
    /// closed-and-dropped en masse — lets the caller avoid issuing
    /// one ``DeleteStreamEntry`` per seq.
    ///
    /// This reads the current keys under the prefix, then deletes
    /// each one — all inside a single redb write transaction. Not
    /// routed through raft because callers (snapshot prune, cleanup)
    /// handle replication separately; for raft-driven deletion use
    /// ``Command::DeleteStreamEntry`` per key.
    pub fn delete_stream_prefix(&self, prefix: &str) -> Result<u64> {
        let db = self.stream_entries.raw_db();
        let table_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.stream_entries.name());
        let write_txn = db
            .begin_write()
            .map_err(|e| super::RaftError::Storage(e.to_string()))?;
        let mut removed: u64 = 0;
        {
            let mut table = write_txn
                .open_table(table_def)
                .map_err(|e| super::RaftError::Storage(e.to_string()))?;

            // Collect matching keys first — redb does not allow
            // delete-while-iterating on the same table handle.
            let mut keys: Vec<Vec<u8>> = Vec::new();
            {
                let iter = table
                    .range::<&[u8]>(prefix.as_bytes()..)
                    .map_err(|e| super::RaftError::Storage(e.to_string()))?;
                for entry in iter {
                    let (k, _v) = entry.map_err(|e| super::RaftError::Storage(e.to_string()))?;
                    let kb = k.value().to_vec();
                    if !kb.starts_with(prefix.as_bytes()) {
                        break;
                    }
                    keys.push(kb);
                }
            }
            for k in keys {
                if table
                    .remove(k.as_slice())
                    .map_err(|e| super::RaftError::Storage(e.to_string()))?
                    .is_some()
                {
                    removed += 1;
                }
            }
        }
        write_txn
            .commit()
            .map_err(|e| super::RaftError::Storage(e.to_string()))?;
        Ok(removed)
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
///
/// ``stream_entries`` is serialized with a ``#[serde(default)]`` so
/// snapshots produced before R19.1b' (no stream table) still restore —
/// absent entries become an empty map on the target replica.
#[derive(Debug, Serialize, Deserialize)]
struct Snapshot {
    /// All metadata entries.
    metadata: HashMap<String, Vec<u8>>,
    /// All stream entries (R19.1b').
    #[serde(default)]
    stream_entries: HashMap<String, Vec<u8>>,
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
            Command::AppendStreamEntry { key, data } => {
                self.stream_entries.set(key.as_bytes(), data)?;
                Ok(CommandResult::Success)
            }
            Command::DeleteStreamEntry { key } => {
                self.stream_entries.delete(key.as_bytes())?;
                Ok(CommandResult::Success)
            }
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

            Command::AppendStreamEntry { key, data } => {
                let stream_def =
                    redb::TableDefinition::<&[u8], &[u8]>::new(self.stream_entries.name());
                let mut table = txn
                    .open_table(stream_def)
                    .map_err(|e| super::RaftError::Storage(format!("open stream_entries: {e}")))?;
                table
                    .insert(key.as_bytes(), data.as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert stream_entry: {e}")))?;
                Ok(CommandResult::Success)
            }

            Command::DeleteStreamEntry { key } => {
                let stream_def =
                    redb::TableDefinition::<&[u8], &[u8]>::new(self.stream_entries.name());
                let mut table = txn
                    .open_table(stream_def)
                    .map_err(|e| super::RaftError::Storage(format!("open stream_entries: {e}")))?;
                table
                    .remove(key.as_bytes())
                    .map_err(|e| super::RaftError::Storage(format!("remove stream_entry: {e}")))?;
                Ok(CommandResult::Success)
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
            | Command::DeleteMetadata { .. }
            | Command::AppendStreamEntry { .. }
            | Command::DeleteStreamEntry { .. } => self.execute(command),
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
            let cur = self.last_applied.load(Ordering::Relaxed);
            if index > cur {
                self.last_applied.store(index, Ordering::Release);
            }
            return Ok(result);
        }

        // Metadata path: skip if we've already applied this index.
        // Protects `AdjustCounter` and similar non-idempotent
        // commands from double-replay on restart.
        if index <= self.last_applied.load(Ordering::Relaxed) {
            return Ok(CommandResult::Success);
        }

        // R20.16.2: capture pre-delete DT_MOUNT classification BEFORE
        // the write txn removes the entry. Apply is serial, so no
        // concurrent writer can slip in between this read and the txn
        // that performs the delete. Only DeleteMetadata needs the
        // pre-capture — for SetMetadata the proto is on the command
        // itself, accessible from emit_mount_apply_event.
        #[cfg(feature = "grpc")]
        let delete_mount_key: Option<String> = match command {
            Command::DeleteMetadata { key } if self.peek_is_dt_mount(key) => Some(key.clone()),
            _ => None,
        };

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

        // Update in-memory state only after successful commit. Release
        // ordering pairs with Acquire loads in sync readers (e.g. the
        // PyO3 gate helper) so a reader observing a new last_applied
        // value also sees the metadata write that preceded it.
        self.last_applied.store(index, Ordering::Release);

        // R20.16.2: fire the DT_MOUNT apply-side callback *after*
        // commit. Any failure is caught (catch_unwind on panic; no-op
        // on missing callback) — returning Err from apply poisons the
        // state machine per raft's "apply must not fail" invariant,
        // and the callback is strictly a side-effect.
        #[cfg(feature = "grpc")]
        self.emit_mount_apply_event(command, delete_mount_key.as_deref());

        // Cache coherence: notify the kernel DCache that this key's
        // metadata changed so nodes that didn't originate the write
        // (leader-forwarded follower writes, catch-up replication)
        // drop the stale dcache entry on next sys_stat / sys_read.
        self.emit_invalidate_event(command);

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

        // R19.1b': serialize stream_entries as its own map. Keys are
        // opaque so no ``__``-prefix filtering here; consumers rely on
        // the dedicated tree to keep them separate from file metadata.
        let mut stream_entries = HashMap::new();
        for item in self.stream_entries.iter() {
            let (key, value) = item?;
            if let Ok(k) = String::from_utf8(key) {
                stream_entries.insert(k, value);
            }
        }

        // Snapshot the advisory map under its own mutex. One clone of
        // the BTreeMap is cheap (shallow tree copy) and lets us drop
        // the mutex before bincoding.
        let advisory = self.advisory.lock().clone();

        let snapshot = Snapshot {
            metadata,
            stream_entries,
            advisory,
            last_applied: self.last_applied.load(Ordering::Relaxed),
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

        let stream_def = redb::TableDefinition::<&[u8], &[u8]>::new(self.stream_entries.name());

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

            // R19.1b': same atomic transaction restores stream_entries.
            // ``delete_table`` wipes the previous state; then reinsert
            // the snapshot contents. Pre-R19.1b' snapshots carry an
            // empty map here (serde(default)), so the table ends up
            // empty — matching pre-R19.1b' behavior where it did not
            // exist.
            write_txn.delete_table(stream_def).map_err(|e| {
                super::RaftError::Storage(format!("delete stream_entries table: {e}"))
            })?;
            let mut stream_table = write_txn.open_table(stream_def).map_err(|e| {
                super::RaftError::Storage(format!("open stream_entries table: {e}"))
            })?;
            for (key, value) in &snapshot.stream_entries {
                stream_table
                    .insert(key.as_bytes(), value.as_slice())
                    .map_err(|e| super::RaftError::Storage(format!("insert stream_entry: {e}")))?;
            }
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
        self.last_applied
            .store(snapshot.last_applied, Ordering::Release);

        Ok(())
    }

    fn last_applied_index(&self) -> u64 {
        self.last_applied.load(Ordering::Acquire)
    }

    fn last_applied_shared(&self) -> Option<Arc<AtomicU64>> {
        Some(self.last_applied_shared_arc())
    }

    /// Return the shared apply-side invalidation slot so downstream
    /// holders (``ZoneConsensus``, kernel ``DLC``) can install a
    /// callback that fires on every committed metadata mutation.
    fn invalidate_cb_slot(
        &self,
    ) -> Option<Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&str) + Send + Sync>>>>> {
        Some(Arc::clone(&self.invalidate_cb))
    }

    /// Return the shared apply-side DT_MOUNT slot so downstream
    /// holders (``ZoneConsensus``, kernel federation wiring) can
    /// install a callback that fires on every committed DT_MOUNT
    /// Set / Delete (R20.16.2).
    #[cfg(feature = "grpc")]
    fn mount_apply_cb_slot(
        &self,
    ) -> Option<Arc<parking_lot::RwLock<Option<Arc<dyn Fn(&MountApplyEvent) + Send + Sync>>>>> {
        Some(Arc::clone(&self.mount_apply_cb))
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

    /// R20.16.2 apply-side DT_MOUNT callback — one flow covering every
    /// branch: DT_MOUNT Set with an installed callback fires exactly
    /// one Set event with the right payload; DT_DIR Set never fires;
    /// DT_MOUNT Delete fires a Delete event; DT_REG Delete never fires;
    /// a state machine with no callback applies normally (callback is
    /// pure side-effect — apply must be unaffected).
    #[cfg(feature = "grpc")]
    #[test]
    fn apply_mount_apply_cb_fires_only_on_dt_mount() {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;
        use std::sync::Mutex as StdMutex;

        fn encode(entry_type: i32, zone: &str, target: &str, backend: &str) -> Vec<u8> {
            ProtoFileMetadata {
                entry_type,
                zone_id: zone.to_string(),
                target_zone_id: target.to_string(),
                backend_name: backend.to_string(),
                ..Default::default()
            }
            .encode_to_vec()
        }

        // Callback installed: DT_MOUNT Set emits a Set event with the
        // decoded target + backend snapshot.
        let store = RedbStore::open_temporary().unwrap();
        let sm = FullStateMachine::new(&store).unwrap();
        let events: Arc<StdMutex<Vec<MountApplyEvent>>> = Arc::new(StdMutex::new(Vec::new()));
        let slot = sm
            .mount_apply_cb_slot()
            .expect("FullStateMachine exposes a mount-apply slot");
        let events_cb = Arc::clone(&events);
        *slot.write() = Some(Arc::new(move |e: &MountApplyEvent| {
            events_cb.lock().unwrap().push(e.clone());
        }));
        let mut sm = sm;

        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/mnt/peer".into(),
                value: encode(2, "zone-a", "zone-b", "cas-local"), // DT_MOUNT
            },
        )
        .unwrap();
        {
            let log = events.lock().unwrap();
            assert_eq!(log.len(), 1, "DT_MOUNT Set must fire exactly once");
            match &log[0] {
                MountApplyEvent::Set {
                    key,
                    target_zone_id,
                    backend_name,
                } => {
                    assert_eq!(key, "/mnt/peer");
                    assert_eq!(target_zone_id, "zone-b");
                    assert_eq!(backend_name, "cas-local");
                }
                other => panic!("expected Set event, got {other:?}"),
            }
        }

        // DT_DIR Set → no event.
        sm.apply(
            2,
            &Command::SetMetadata {
                key: "/docs".into(),
                value: encode(1, "zone-a", "", ""), // DT_DIR
            },
        )
        .unwrap();
        assert_eq!(
            events.lock().unwrap().len(),
            1,
            "DT_DIR must not fire a mount-apply event"
        );

        // DT_MOUNT Delete → fires one Delete event with the key.
        sm.apply(
            3,
            &Command::DeleteMetadata {
                key: "/mnt/peer".into(),
            },
        )
        .unwrap();
        {
            let log = events.lock().unwrap();
            assert_eq!(log.len(), 2, "DT_MOUNT Delete must fire once");
            match &log[1] {
                MountApplyEvent::Delete { key } => assert_eq!(key, "/mnt/peer"),
                other => panic!("expected Delete event, got {other:?}"),
            }
        }

        // DT_REG Set + Delete → no events (DT_REG is not DT_MOUNT).
        sm.apply(
            4,
            &Command::SetMetadata {
                key: "/file.txt".into(),
                value: encode(0, "zone-a", "", ""), // DT_REG
            },
        )
        .unwrap();
        sm.apply(
            5,
            &Command::DeleteMetadata {
                key: "/file.txt".into(),
            },
        )
        .unwrap();
        assert_eq!(
            events.lock().unwrap().len(),
            2,
            "DT_REG set/delete must not fire"
        );

        // No callback installed: DT_MOUNT applies normally, no panic.
        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        let res = sm2
            .apply(
                1,
                &Command::SetMetadata {
                    key: "/mnt/peer".into(),
                    value: encode(2, "zone-a", "zone-b", "cas-local"),
                },
            )
            .unwrap();
        assert!(matches!(res, CommandResult::Success));
        assert_eq!(sm2.last_applied_index(), 1, "apply unaffected without cb");
    }

    /// Apply-side mount callback panics must not poison apply.
    #[cfg(feature = "grpc")]
    #[test]
    fn apply_mount_apply_cb_panic_does_not_poison_apply() {
        use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
        use prost::Message as ProstMessage;

        let store = RedbStore::open_temporary().unwrap();
        let sm = FullStateMachine::new(&store).unwrap();
        let slot = sm.mount_apply_cb_slot().unwrap();
        *slot.write() = Some(Arc::new(|_e: &MountApplyEvent| {
            panic!("intentional test panic");
        }));
        let mut sm = sm;

        let value = ProtoFileMetadata {
            entry_type: 2,
            target_zone_id: "zone-b".into(),
            backend_name: "cas-local".into(),
            ..Default::default()
        }
        .encode_to_vec();

        let res = sm.apply(
            1,
            &Command::SetMetadata {
                key: "/mnt/peer".into(),
                value,
            },
        );
        assert!(res.is_ok(), "callback panic must not fail apply");
        assert_eq!(sm.last_applied_index(), 1);
    }

    /// Apply-side invalidation callback — fires once per committed
    /// metadata mutation, skips non-mutating variants, survives
    /// callback panics without poisoning apply.
    #[test]
    fn apply_invalidate_callback_fires_on_metadata_mutations_only() {
        use std::sync::atomic::AtomicUsize;
        use std::sync::Arc as StdArc;

        let store = RedbStore::open_temporary().unwrap();
        let sm = FullStateMachine::new(&store).unwrap();
        let calls = StdArc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let slot = sm
            .invalidate_cb_slot()
            .expect("FullStateMachine exposes a slot");
        let calls_cb = StdArc::clone(&calls);
        *slot.write() = Some(Arc::new(move |key: &str| {
            calls_cb.lock().unwrap().push(key.to_string());
        }));
        let mut sm = sm;

        // SetMetadata → fires with the mutated key.
        sm.apply(
            1,
            &Command::SetMetadata {
                key: "/a".into(),
                value: vec![0u8; 4],
            },
        )
        .unwrap();
        sm.apply(2, &Command::DeleteMetadata { key: "/b".into() })
            .unwrap();
        sm.apply(
            3,
            &Command::CasSetMetadata {
                key: "/c".into(),
                value: vec![0u8; 4],
                expected_version: 0,
            },
        )
        .unwrap();
        sm.apply(
            4,
            &Command::AdjustCounter {
                key: "__i_links_count__".into(),
                delta: 1,
            },
        )
        .unwrap();
        assert_eq!(
            *calls.lock().unwrap(),
            vec!["/a".to_string(), "/b".to_string(), "/c".to_string()],
            "callback must fire on Set/Cas/Delete, not on AdjustCounter"
        );

        // Panicking callback does not poison the state machine.
        let panic_count = StdArc::new(AtomicUsize::new(0));
        let panic_count_cb = StdArc::clone(&panic_count);
        *slot.write() = Some(Arc::new(move |_key: &str| {
            panic_count_cb.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            panic!("intentional callback panic");
        }));
        let res = sm.apply(
            5,
            &Command::SetMetadata {
                key: "/d".into(),
                value: vec![0u8; 4],
            },
        );
        assert!(res.is_ok(), "apply must not propagate callback panic");
        assert_eq!(
            panic_count.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "panicking callback still ran once"
        );
        assert_eq!(sm.last_applied_index(), 5);

        // Slot cleared → no more invocations on subsequent applies.
        *slot.write() = None;
        sm.apply(
            6,
            &Command::SetMetadata {
                key: "/e".into(),
                value: vec![0u8; 4],
            },
        )
        .unwrap();
        // calls vec is frozen at length 3 — /d / /e did not append because
        // the panicking cb was replaced before /d succeeded-without-append,
        // and /e ran with slot = None.
        assert_eq!(calls.lock().unwrap().len(), 3);
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

    /// R19.1b' — ``AppendStreamEntry`` stores raw bytes in the
    /// dedicated ``sm_stream_entries`` tree (not ``sm_metadata``),
    /// and ``get_stream_entry`` reads them back untouched.
    #[test]
    fn stream_entry_roundtrip_raw_bytes() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();

        let payload: Vec<u8> = (0u8..=255).collect();
        sm.apply(
            1,
            &Command::AppendStreamEntry {
                key: "/__wal_stream__/s/0".into(),
                data: payload.clone(),
            },
        )
        .unwrap();

        assert_eq!(
            sm.get_stream_entry("/__wal_stream__/s/0").unwrap(),
            Some(payload)
        );
        // Stream entries do NOT appear in list_metadata — different
        // redb tree. Confirms no pollution of file-metadata scans.
        assert!(sm.list_metadata("/__wal_stream__/").unwrap().is_empty());
    }

    /// ``DeleteStreamEntry`` removes the row; subsequent get returns
    /// ``None``.
    #[test]
    fn stream_entry_delete_by_key() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        sm.apply(
            1,
            &Command::AppendStreamEntry {
                key: "/s/0".into(),
                data: b"hello".to_vec(),
            },
        )
        .unwrap();
        assert!(sm.get_stream_entry("/s/0").unwrap().is_some());
        sm.apply(2, &Command::DeleteStreamEntry { key: "/s/0".into() })
            .unwrap();
        assert!(sm.get_stream_entry("/s/0").unwrap().is_none());
    }

    /// ``delete_stream_prefix`` drops every row under the prefix and
    /// leaves others alone.
    #[test]
    fn stream_entry_delete_prefix_scopes_correctly() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        for i in 0..5 {
            sm.apply(
                i + 1,
                &Command::AppendStreamEntry {
                    key: format!("/__wal_stream__/a/{i}"),
                    data: vec![i as u8],
                },
            )
            .unwrap();
        }
        sm.apply(
            10,
            &Command::AppendStreamEntry {
                key: "/__wal_stream__/b/0".into(),
                data: b"keep".to_vec(),
            },
        )
        .unwrap();

        let removed = sm.delete_stream_prefix("/__wal_stream__/a/").unwrap();
        assert_eq!(removed, 5);
        assert!(sm
            .get_stream_entry("/__wal_stream__/a/0")
            .unwrap()
            .is_none());
        assert_eq!(
            sm.get_stream_entry("/__wal_stream__/b/0").unwrap(),
            Some(b"keep".to_vec())
        );
    }

    /// Snapshot + restore round-trips stream entries intact.
    #[test]
    fn stream_entry_snapshot_restore_roundtrip() {
        let store = RedbStore::open_temporary().unwrap();
        let mut sm = FullStateMachine::new(&store).unwrap();
        sm.apply(
            1,
            &Command::AppendStreamEntry {
                key: "/s/0".into(),
                data: vec![0xde, 0xad, 0xbe, 0xef],
            },
        )
        .unwrap();
        sm.apply(
            2,
            &Command::AppendStreamEntry {
                key: "/s/1".into(),
                data: vec![0x00, 0xff],
            },
        )
        .unwrap();

        let snap_bytes = sm.snapshot().unwrap();

        let store2 = RedbStore::open_temporary().unwrap();
        let mut sm2 = FullStateMachine::new(&store2).unwrap();
        sm2.restore_snapshot(&snap_bytes).unwrap();

        assert_eq!(
            sm2.get_stream_entry("/s/0").unwrap(),
            Some(vec![0xde, 0xad, 0xbe, 0xef])
        );
        assert_eq!(
            sm2.get_stream_entry("/s/1").unwrap(),
            Some(vec![0x00, 0xff])
        );
    }
}
