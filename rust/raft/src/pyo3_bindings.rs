// PyO3 #[pymethods] macro generates `.into()` conversions for PyErr that
// clippy flags as useless. This is a known PyO3 + clippy interaction.
#![allow(clippy::useless_conversion)]

//! PyO3 Python bindings for Nexus MetaStore (sled state machine).
//!
//! Three drivers are exposed:
//! - `MetaStore`: Direct redb access for embedded mode (~5μs per op).
//! - `ZoneManager`: Multi-zone Raft registry owner (creates/manages zones).
//! - `ZoneHandle`: Per-zone Raft node handle (metadata/lock operations).
//!
//! # Python Usage
//!
//! ```python
//! from _nexus_raft import MetaStore
//!
//! # Direct redb access (embedded mode)
//! store = MetaStore("/var/lib/nexus/metadata")
//! store.set_metadata("/path/to/file", metadata_bytes)
//! metadata = store.get_metadata("/path/to/file")
//!
//! from _nexus_raft import ZoneManager
//!
//! # Multi-zone Raft consensus
//! mgr = ZoneManager("nexus-1", "/var/lib/nexus/zones", "0.0.0.0:2126")
//! handle = mgr.create_zone("default", ["2@peer:2126"])
//! handle.set_metadata("/path/to/file", metadata_bytes)  # replicated
//! ```

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::raft::{
    Command, CommandResult, FullStateMachine, HolderInfo as RustHolderInfo,
    LockAcquireResult as RustLockAcquireResult, LockInfo as RustLockInfo, StateMachine,
};
use crate::storage::RedbStore;

// =========================================================================
// Consistency mode constants (SSOT for all PyO3 bindings)
// =========================================================================

/// Strong Consistency — wait for Raft commit before returning.
const CONSISTENCY_SC: &str = "sc";
/// Eventual Consistency — fire-and-forget (propose + return immediately).
const CONSISTENCY_EC: &str = "ec";

/// Validate consistency mode string. Returns Ok(()) for "sc"/"ec", Err otherwise.
fn validate_consistency(consistency: &str) -> PyResult<()> {
    match consistency {
        CONSISTENCY_SC | CONSISTENCY_EC => Ok(()),
        _ => Err(PyRuntimeError::new_err(format!(
            "Invalid consistency mode '{}': expected '{}' or '{}'",
            consistency, CONSISTENCY_SC, CONSISTENCY_EC
        ))),
    }
}

/// Python lock-mode string constants (F4 C2).
const LOCK_MODE_EXCLUSIVE: &str = "exclusive";
const LOCK_MODE_SHARED: &str = "shared";

/// Parse the Python `mode` parameter into a Rust `LockMode`.
///
/// Accepts `"exclusive"` / `"shared"`, case-insensitive. `"mutex"`
/// and `"semaphore"` are explicitly rejected — those are the
/// computed display labels for `max_holders`, not the per-holder
/// conflict mode.
fn parse_lock_mode(s: &str) -> PyResult<crate::prelude::LockMode> {
    use crate::prelude::LockMode;
    match s.to_ascii_lowercase().as_str() {
        LOCK_MODE_EXCLUSIVE => Ok(LockMode::Exclusive),
        LOCK_MODE_SHARED => Ok(LockMode::Shared),
        other => Err(PyRuntimeError::new_err(format!(
            "Invalid lock mode '{}': expected '{}' or '{}'",
            other, LOCK_MODE_EXCLUSIVE, LOCK_MODE_SHARED
        ))),
    }
}

/// Render a `LockMode` back to its string form for the Python side.
fn lock_mode_str(mode: crate::prelude::LockMode) -> &'static str {
    match mode {
        crate::prelude::LockMode::Exclusive => LOCK_MODE_EXCLUSIVE,
        crate::prelude::LockMode::Shared => LOCK_MODE_SHARED,
    }
}

/// Python-compatible holder info.
#[pyclass]
#[derive(Clone)]
pub struct PyHolderInfo {
    #[pyo3(get)]
    pub lock_id: String,
    #[pyo3(get)]
    pub holder_info: String,
    /// Per-holder conflict mode (F4 C2): `"exclusive"` or
    /// `"shared"`. Not to be confused with the lock-level display
    /// label ("mutex"/"semaphore"), which is computed from
    /// `max_holders` on the Python side and never stored.
    #[pyo3(get)]
    pub mode: String,
    #[pyo3(get)]
    pub acquired_at: u64,
    #[pyo3(get)]
    pub expires_at: u64,
}

impl From<RustHolderInfo> for PyHolderInfo {
    fn from(h: RustHolderInfo) -> Self {
        Self {
            lock_id: h.lock_id,
            holder_info: h.holder_info,
            mode: lock_mode_str(h.mode).to_string(),
            acquired_at: h.acquired_at,
            expires_at: h.expires_at,
        }
    }
}

/// Python-compatible lock state result.
#[pyclass]
#[derive(Clone)]
pub struct PyLockState {
    #[pyo3(get)]
    pub acquired: bool,
    #[pyo3(get)]
    pub current_holders: u32,
    #[pyo3(get)]
    pub max_holders: u32,
    #[pyo3(get)]
    pub holders: Vec<PyHolderInfo>,
}

impl From<RustLockAcquireResult> for PyLockState {
    fn from(s: RustLockAcquireResult) -> Self {
        Self {
            acquired: s.acquired,
            current_holders: s.current_holders,
            max_holders: s.max_holders,
            holders: s.holders.into_iter().map(|h| h.into()).collect(),
        }
    }
}

/// Python-compatible lock info.
#[pyclass]
#[derive(Clone)]
pub struct PyLockInfo {
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub max_holders: u32,
    #[pyo3(get)]
    pub holders: Vec<PyHolderInfo>,
}

impl From<RustLockInfo> for PyLockInfo {
    fn from(l: RustLockInfo) -> Self {
        Self {
            path: l.path,
            max_holders: l.max_holders,
            holders: l.holders.into_iter().map(|h| h.into()).collect(),
        }
    }
}

/// Embedded metastore driver — direct redb state machine access.
///
/// Provides FFI access to the redb KV store without Raft consensus.
/// Used for embedded mode and as the base layer for EC mode (future).
///
/// Performance: ~5μs per operation.
#[pyclass]
pub struct PyMetaStore {
    store: RedbStore,
    sm: FullStateMachine,
    next_index: u64,
}

#[pymethods]
impl PyMetaStore {
    /// Create a new MetaStore instance.
    ///
    /// Args:
    ///     path: Path to the redb database directory.
    ///
    /// Returns:
    ///     MetaStore instance.
    ///
    /// Raises:
    ///     RuntimeError: If the database cannot be opened.
    #[new]
    pub fn new(path: &str) -> PyResult<Self> {
        let store = RedbStore::open(path)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to open redb: {}", e)))?;
        let sm = FullStateMachine::new(&store).map_err(|e| {
            PyRuntimeError::new_err(format!("Failed to create state machine: {}", e))
        })?;
        let next_index = sm.last_applied_index() + 1;

        Ok(Self {
            store,
            sm,
            next_index,
        })
    }

    /// Get the next log index for commands.
    pub fn next_index(&self) -> u64 {
        self.next_index
    }

    /// Get the last applied log index.
    pub fn last_applied_index(&self) -> u64 {
        self.sm.last_applied_index()
    }

    // =========================================================================
    // Metadata Operations
    // =========================================================================

    /// Set metadata for a path.
    ///
    /// Args:
    ///     path: The file path (key).
    ///     value: Serialized metadata bytes.
    ///     consistency: "sc" (default) or "ec". Embedded mode always applies synchronously.
    ///
    /// Returns:
    ///     Always None (embedded mode has no replication, writes are immediately durable).
    #[pyo3(signature = (path, value, consistency="sc"))]
    pub fn set_metadata(
        &mut self,
        path: &str,
        value: Vec<u8>,
        consistency: &str,
    ) -> PyResult<Option<u64>> {
        validate_consistency(consistency)?;
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        self.apply_command(cmd)?;
        Ok(None)
    }

    /// Compare-and-swap metadata for a path.
    ///
    /// Atomically writes metadata only if the current version matches
    /// `expected_version`. This is the foundation for optimistic
    /// concurrency control (OCC) — zero race window.
    ///
    /// Args:
    ///     path: The file path (key).
    ///     value: Serialized metadata bytes.
    ///     expected_version: Expected current version (0 = create-only).
    ///     consistency: "sc" (default) or "ec".
    ///
    /// Returns:
    ///     Tuple of (success: bool, current_version: int).
    #[pyo3(signature = (path, value, expected_version, consistency="sc"))]
    pub fn cas_set_metadata(
        &mut self,
        path: &str,
        value: Vec<u8>,
        expected_version: u32,
        consistency: &str,
    ) -> PyResult<(bool, u32)> {
        validate_consistency(consistency)?;
        let cmd = Command::CasSetMetadata {
            key: path.to_string(),
            value,
            expected_version,
        };
        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::CasResult {
                success,
                current_version,
            } => Ok((success, current_version)),
            _ => Err(PyRuntimeError::new_err("Unexpected CAS result type")),
        }
    }

    /// Get metadata for a path.
    ///
    /// Args:
    ///     path: The file path.
    ///
    /// Returns:
    ///     Serialized metadata bytes, or None if not found.
    pub fn get_metadata(&self, path: &str) -> PyResult<Option<Vec<u8>>> {
        self.sm
            .get_metadata(path)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to get metadata: {}", e)))
    }

    /// Get metadata for multiple paths in a single FFI call.
    ///
    /// Args:
    ///     paths: List of file paths to look up.
    ///
    /// Returns:
    ///     List of (path, metadata_bytes_or_none) tuples.
    pub fn get_metadata_multi(
        &self,
        paths: Vec<String>,
    ) -> PyResult<Vec<(String, Option<Vec<u8>>)>> {
        self.sm
            .get_metadata_multi(&paths)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to get metadata multi: {}", e)))
    }

    /// Delete metadata for a path.
    ///
    /// Args:
    ///     path: The file path.
    ///     consistency: "sc" (default) or "ec". Embedded mode always applies synchronously.
    ///
    /// Returns:
    ///     Always None (embedded mode has no replication, writes are immediately durable).
    #[pyo3(signature = (path, consistency="sc"))]
    pub fn delete_metadata(&mut self, path: &str, consistency: &str) -> PyResult<Option<u64>> {
        validate_consistency(consistency)?;
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        self.apply_command(cmd)?;
        Ok(None)
    }

    /// Check if an EC write token has been replicated.
    ///
    /// Embedded mode has no replication — always returns None.
    pub fn is_committed(&self, _token: u64) -> Option<String> {
        None
    }

    /// List all metadata with a prefix.
    ///
    /// Args:
    ///     prefix: Path prefix to filter by.
    ///
    /// Returns:
    ///     List of (path, metadata_bytes) tuples.
    pub fn list_metadata(&self, prefix: &str) -> PyResult<Vec<(String, Vec<u8>)>> {
        self.sm
            .list_metadata(prefix)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to list metadata: {}", e)))
    }

    /// Set multiple metadata entries in a single batch operation.
    ///
    /// Args:
    ///     items: List of (path, value_bytes) tuples to set.
    ///
    /// Returns:
    ///     Number of entries set.
    pub fn batch_set_metadata(&mut self, items: Vec<(String, Vec<u8>)>) -> PyResult<usize> {
        let count = items.len();
        for (path, value) in &items {
            let cmd = Command::SetMetadata {
                key: path.clone(),
                value: value.clone(),
            };
            self.apply_command(cmd)?;
        }
        Ok(count)
    }

    /// Atomically adjust a metadata counter by a signed delta.
    ///
    /// Read-modify-write in a single operation. The value is stored as
    /// i64 big-endian in the metadata tree. Result clamped to >= 0.
    ///
    /// Args:
    ///     key: The metadata key (e.g., "__i_links_count__").
    ///     delta: Signed adjustment (+1 to increment, -1 to decrement).
    ///
    /// Returns:
    ///     New counter value after adjustment.
    pub fn adjust_counter(&mut self, key: &str, delta: i64) -> PyResult<i64> {
        let cmd = Command::AdjustCounter {
            key: key.to_string(),
            delta,
        };
        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::Value(bytes) => {
                let arr: [u8; 8] = bytes
                    .try_into()
                    .map_err(|_| PyRuntimeError::new_err("Invalid counter value"))?;
                Ok(i64::from_be_bytes(arr))
            }
            _ => Err(PyRuntimeError::new_err("Unexpected result type")),
        }
    }

    /// Delete multiple metadata entries in a single batch operation.
    ///
    /// Args:
    ///     keys: List of paths to delete.
    ///
    /// Returns:
    ///     Number of entries deleted.
    pub fn batch_delete_metadata(&mut self, keys: Vec<String>) -> PyResult<usize> {
        let count = keys.len();
        for key in &keys {
            let cmd = Command::DeleteMetadata { key: key.clone() };
            self.apply_command(cmd)?;
        }
        Ok(count)
    }

    /// Count metadata entries matching a prefix.
    ///
    /// Args:
    ///     prefix: Path prefix to count by.
    ///
    /// Returns:
    ///     Number of matching entries.
    pub fn count_metadata(&self, prefix: &str) -> PyResult<usize> {
        let entries = self
            .sm
            .list_metadata(prefix)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to count metadata: {}", e)))?;
        Ok(entries.len())
    }

    // =========================================================================
    // Lock Operations
    // =========================================================================

    /// Acquire a distributed lock.
    ///
    /// Args:
    ///     path: Resource path to lock.
    ///     lock_id: Unique lock ID (typically a UUID).
    ///     max_holders: Maximum concurrent holders (1 = mutex, >1 = semaphore).
    ///     ttl_secs: Lock TTL in seconds.
    ///     holder_info: Description of the holder (e.g., "agent:xxx").
    ///
    /// Returns:
    ///     LockState with acquisition result.
    #[pyo3(signature = (path, lock_id, max_holders=1, ttl_secs=30, holder_info="", mode="exclusive"))]
    pub fn acquire_lock(
        &mut self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
        mode: &str,
    ) -> PyResult<PyLockState> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
            mode: parse_lock_mode(mode)?,
            now_secs: crate::prelude::FullStateMachine::now(),
        };

        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::LockResult(state) => Ok(state.into()),
            _ => Err(PyRuntimeError::new_err("Unexpected result type")),
        }
    }

    /// Release a distributed lock.
    ///
    /// Args:
    ///     path: Resource path.
    ///     lock_id: Lock ID to release.
    ///
    /// Returns:
    ///     True if holder was found and released, False if not owned or not found.
    pub fn release_lock(&mut self, path: &str, lock_id: &str) -> PyResult<bool> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::Success => Ok(true),
            CommandResult::Error(_) => Ok(false), // Not owned or not found
            _ => Ok(false),
        }
    }

    /// Extend a lock's TTL.
    ///
    /// Args:
    ///     path: Resource path.
    ///     lock_id: Lock ID to extend.
    ///     new_ttl_secs: New TTL in seconds from now.
    ///
    /// Returns:
    ///     True if holder was found and TTL extended, False if not owned or not found.
    pub fn extend_lock(&mut self, path: &str, lock_id: &str, new_ttl_secs: u32) -> PyResult<bool> {
        let cmd = Command::ExtendLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            new_ttl_secs,
            now_secs: crate::prelude::FullStateMachine::now(),
        };
        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::Success => Ok(true),
            CommandResult::Error(_) => Ok(false), // Not owned or not found
            _ => Ok(false),
        }
    }

    /// Get lock info for a path.
    ///
    /// Args:
    ///     path: Resource path.
    ///
    /// Returns:
    ///     LockInfo if lock exists, None otherwise.
    pub fn get_lock(&self, path: &str) -> PyResult<Option<PyLockInfo>> {
        self.sm
            .get_lock(path)
            .map(|opt| opt.map(|l| l.into()))
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to get lock: {}", e)))
    }

    /// List all locks matching a prefix.
    ///
    /// Args:
    ///     prefix: Key prefix to filter by (e.g., "zone_id:" for zone-scoped locks).
    ///     limit: Maximum number of results to return.
    ///
    /// Returns:
    ///     List of LockInfo for matching locks.
    #[pyo3(signature = (prefix="", limit=1000))]
    pub fn list_locks(&self, prefix: &str, limit: usize) -> PyResult<Vec<PyLockInfo>> {
        self.sm
            .list_locks(prefix, limit)
            .map(|locks| locks.into_iter().map(|l| l.into()).collect())
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to list locks: {}", e)))
    }

    /// Force-release all holders of a lock (admin operation).
    ///
    /// Args:
    ///     path: Resource path to force-release.
    ///
    /// Returns:
    ///     True if a lock was found and released, False if no lock exists.
    pub fn force_release_lock(&mut self, path: &str) -> PyResult<bool> {
        // Get current lock info
        let lock_info = self
            .sm
            .get_lock(path)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to get lock: {}", e)))?;

        match lock_info {
            Some(info) if !info.holders.is_empty() => {
                // Release each holder
                for holder in &info.holders {
                    let cmd = Command::ReleaseLock {
                        path: path.to_string(),
                        lock_id: holder.lock_id.clone(),
                    };
                    let _ = self.apply_command_raw(cmd)?;
                }
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    // =========================================================================
    // Revision Counter Operations (Issue #1330, Phase 4.2)
    // =========================================================================

    /// Atomically increment and return the new revision for a zone.
    ///
    /// Uses redb's dedicated REVISIONS_TABLE with single-writer transactions.
    /// No Python lock needed — redb's write transaction provides atomicity.
    ///
    /// Args:
    ///     zone_id: The zone to increment revision for.
    ///
    /// Returns:
    ///     The new revision number after incrementing.
    pub fn increment_revision(&self, zone_id: &str) -> PyResult<u64> {
        self.store
            .increment_revision(zone_id)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to increment revision: {}", e)))
    }

    /// Get the current revision for a zone without incrementing.
    ///
    /// Args:
    ///     zone_id: The zone to get revision for.
    ///
    /// Returns:
    ///     The current revision number (0 if not found).
    pub fn get_revision(&self, zone_id: &str) -> PyResult<u64> {
        self.store
            .get_revision(zone_id)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to get revision: {}", e)))
    }

    // =========================================================================
    // Snapshot Operations
    // =========================================================================

    /// Create a snapshot of the current state.
    ///
    /// Returns:
    ///     Serialized snapshot bytes.
    pub fn snapshot(&self) -> PyResult<Vec<u8>> {
        self.sm
            .snapshot()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create snapshot: {}", e)))
    }

    /// Restore state from a snapshot.
    ///
    /// Args:
    ///     data: Snapshot bytes from a previous snapshot() call.
    pub fn restore_snapshot(&mut self, data: &[u8]) -> PyResult<()> {
        self.sm
            .restore_snapshot(data)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to restore snapshot: {}", e)))?;
        self.next_index = self.sm.last_applied_index() + 1;
        Ok(())
    }

    /// Flush all pending writes to disk.
    pub fn flush(&self) -> PyResult<()> {
        self.store
            .flush()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to flush: {}", e)))
    }
}

impl PyMetaStore {
    /// Apply a command and return success/failure.
    fn apply_command(&mut self, cmd: Command) -> PyResult<bool> {
        let result = self.apply_command_raw(cmd)?;
        match result {
            CommandResult::Success => Ok(true),
            CommandResult::Error(e) => Err(PyRuntimeError::new_err(e)),
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::CasResult { success, .. } => Ok(success),
            CommandResult::Value(_) => Ok(true),
        }
    }

    /// Apply a command and return the raw result.
    fn apply_command_raw(&mut self, cmd: Command) -> PyResult<CommandResult> {
        let index = self.next_index;
        self.next_index += 1;

        self.sm
            .apply(index, &cmd)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to apply command: {}", e)))
    }
}

// =============================================================================
// R20.18.6: PyZoneManager + PyZoneHandle pyclass shells deleted.
//
// Both wrappers previously existed only to let Python construct /
// interact with raft zones. R20.18.2-R20.18.5 moved federation
// bootstrap into Kernel::init_federation_from_env and cut every
// Python caller over to PyKernel syscalls + zone_* thin shims. The
// pure-Rust `crate::zone_manager::ZoneManager` and
// `crate::zone_handle::ZoneHandle` types remain as kernel-internal
// SSOT; nothing crosses the PyO3 boundary as a ZoneHandle any more.
//
// `parse_consistency` helper was only used by the deleted pymethods;
// it went with them.
// =============================================================================

// =============================================================================
// Standalone join_cluster function (K3s-style pre-provision)
// =============================================================================

/// Join an existing cluster by provisioning TLS certificates from the leader.
///
/// Called BEFORE ZoneManager is created. Connects to the leader using TLS
/// without certificate verification (TOFU), then verifies the CA fingerprint
/// from the join token after receipt.
///
/// Args:
///     peer_address: Leader's gRPC address (e.g., "10.0.0.1:2126").
///     join_token: K3s-style join token ("K10<password>::server:<ca_fingerprint>").
///     node_id: This node's ID.
///     tls_dir: Directory to write ca.pem, node.pem, node-key.pem.
#[cfg(all(feature = "grpc", has_protos))]
#[pyfunction]
fn join_cluster(
    peer_address: &str,
    join_token: &str,
    hostname: &str,
    tls_dir: &str,
) -> PyResult<()> {
    use crate::transport::call_join_cluster;

    let node_id = crate::transport::hostname_to_node_id(hostname);

    // Parse join token: K10<password>::server:<ca_fingerprint>
    let token_prefix = "K10";
    let separator = "::server:";
    if !join_token.starts_with(token_prefix) {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: must start with 'K10'",
        ));
    }
    let body = &join_token[token_prefix.len()..];
    let sep_pos = body.find(separator).ok_or_else(|| {
        PyRuntimeError::new_err("Invalid join token: missing '::server:' separator")
    })?;
    let password = &body[..sep_pos];
    let expected_fingerprint = &body[sep_pos + separator.len()..];

    if password.is_empty() {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: empty password",
        ));
    }
    if !expected_fingerprint.starts_with("SHA256:") {
        return Err(PyRuntimeError::new_err(
            "Invalid join token: fingerprint must start with 'SHA256:'",
        ));
    }

    // Build endpoint URL
    let endpoint = if peer_address.starts_with("http") {
        peer_address.to_string()
    } else {
        format!("http://{}", peer_address)
    };

    // Create a temporary Tokio runtime for the blocking call
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

    let result = runtime
        .block_on(call_join_cluster(
            &endpoint, node_id, "", // node_address — not needed for pre-provision
            "root", password, 30, // timeout_secs
        ))
        .map_err(|e| PyRuntimeError::new_err(format!("JoinCluster RPC failed: {}", e)))?;

    // Verify CA fingerprint matches the join token
    let ca_fingerprint = crate::transport::certgen::ca_fingerprint_from_pem(&result.ca_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to compute CA fingerprint: {}", e)))?;
    if ca_fingerprint != expected_fingerprint {
        return Err(PyRuntimeError::new_err(format!(
            "CA fingerprint mismatch: expected '{}', got '{}'",
            expected_fingerprint, ca_fingerprint
        )));
    }

    // Write certs to disk
    let dir = std::path::Path::new(tls_dir);
    std::fs::create_dir_all(dir)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to create TLS dir: {}", e)))?;

    std::fs::write(dir.join("ca.pem"), &result.ca_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to write ca.pem: {}", e)))?;
    std::fs::write(dir.join("node.pem"), &result.node_cert_pem)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node.pem: {}", e)))?;

    // Write private key with restricted permissions
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true).mode(0o600);
        use std::io::Write;
        let mut f = opts
            .open(dir.join("node-key.pem"))
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
        f.write_all(&result.node_key_pem)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(dir.join("node-key.pem"), &result.node_key_pem)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to write node-key.pem: {}", e)))?;
    }

    Ok(())
}

/// Derive a deterministic node ID from a hostname (exposed to Python).
#[cfg(all(feature = "grpc", has_protos))]
#[pyfunction]
fn hostname_to_node_id(hostname: &str) -> u64 {
    crate::transport::hostname_to_node_id(hostname)
}

/// Register raft's PyO3 classes on the calling crate's Python module.
///
/// F2 C8 (Option A): raft is an rlib inside the ``nexus_runtime`` cdylib
/// now — the old ``#[pymodule] fn _nexus_raft`` is gone. Kernel's own
/// ``#[pymodule]`` calls this function to expose ``MetaStore`` /
/// ``ZoneManager`` / ``ZoneHandle`` from the single ``nexus_runtime``
/// Python module. Kept ``pub`` so ``kernel::lib::nexus_runtime`` can
/// reach it via the ``nexus_raft_lib::register_python_classes`` path.
pub fn register_python_classes(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMetaStore>()?;
    m.add_class::<PyLockState>()?;
    m.add_class::<PyLockInfo>()?;
    m.add_class::<PyHolderInfo>()?;
    // R20.18.6: PyZoneManager + PyZoneHandle pyclass registrations removed
    // with the pyclasses themselves. Python drives zones via kernel's
    // zone_* PyO3 methods + federation_rpc shim.
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(join_cluster, m)?)?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(hostname_to_node_id, m)?)?;

    #[cfg(feature = "grpc")]
    {
        use crate::federation::tofu::{PyTofuTrustStore, PyTrustedZone};
        m.add_class::<PyTofuTrustStore>()?;
        m.add_class::<PyTrustedZone>()?;
    }
    m.add_function(wrap_pyfunction!(install_federation_wiring_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_is_initialized_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_create_zone_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_remove_zone_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_join_zone_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_share_zone_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_lookup_share_py, m)?)?;
    m.add_function(wrap_pyfunction!(federation_cluster_info_py, m)?)?;
    m.add_function(wrap_pyfunction!(
        federation_start_replication_scanner_py,
        m
    )?)?;
    Ok(())
}

/// Federation readiness probe — replacement for the old
/// `kernel.mount_reconciliation_done` PyO3 method.  Returns `True`
/// once the DistributedCoordinator has bootstrapped (ZoneManager
/// exists, root zone loaded). Used by
/// `fastapi_server._federation_rpc_active` to decide whether to mount
/// the FederationRPCService.
///
/// Derives readiness from `list_zones`: federation is active when at
/// least one zone is loaded, which is true post-`install` (root zone
/// is the first zone to materialise).
#[pyfunction]
#[pyo3(name = "federation_is_initialized")]
fn federation_is_initialized_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
) -> PyResult<bool> {
    let k = kernel.kernel_ref();
    Ok(!k.distributed_coordinator().list_zones(k).is_empty())
}

/// Python-facing one-shot install: replaces the kernel's
/// `NoopDistributedCoordinator` with the real
/// `RaftDistributedCoordinator` and runs `init_from_env` so the
/// ZoneManager bootstraps from `NEXUS_PEERS` / `NEXUS_HOSTNAME` /
/// `NEXUS_BIND_ADDR` / `NEXUS_ADVERTISE_ADDR` / `NEXUS_DATA_DIR` /
/// `NEXUS_RAFT_TLS`. Idempotent — re-imports observe the
/// already-initialised state.
#[pyfunction]
#[pyo3(name = "install_federation_wiring")]
fn install_federation_wiring_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
) -> PyResult<()> {
    crate::distributed_coordinator::install(kernel.kernel_ref())
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

/// Federation control-plane: create a raft zone (no mount).
///
/// Standalone zone creation — analogous to Linux `mkfs.<type>`,
/// which is a userspace utility that prepares a filesystem image
/// without involving `mount(2)`.  Service-tier callers reach this
/// through `nexus_runtime.federation_create_zone(kernel, zone_id)`;
/// it is intentionally NOT a method on `PyKernel` because zone
/// lifecycle is a federation-driver concern, not a kernel primitive.
/// Mount-tied creation continues to flow through
/// `kernel.sys_setattr(path, DT_MOUNT, target_zone_id=…)` which
/// auto-creates the zone via the same trait method.
#[pyfunction]
#[pyo3(name = "federation_create_zone")]
fn federation_create_zone_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    zone_id: &str,
) -> PyResult<String> {
    let k = kernel.kernel_ref();
    k.distributed_coordinator()
        .create_zone(k, zone_id)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(zone_id.to_string())
}

/// Federation control-plane: remove a raft zone.  Cascade-unmount
/// happens inside the provider impl.  `force=true` honors the
/// POSIX-style `unlink while i_links > 0` bypass for replication
/// races on followers.
#[pyfunction]
#[pyo3(name = "federation_remove_zone")]
#[pyo3(signature = (kernel, zone_id, force=false))]
fn federation_remove_zone_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    zone_id: &str,
    force: bool,
) -> PyResult<()> {
    let k = kernel.kernel_ref();
    k.distributed_coordinator()
        .remove_zone(k, zone_id, force)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

/// Federation control-plane: join an existing raft zone advertised
/// by a peer.  `as_learner=true` joins as a non-voting learner;
/// `false` joins as a voter (default).
#[pyfunction]
#[pyo3(name = "federation_join_zone")]
#[pyo3(signature = (kernel, zone_id, as_learner=false))]
fn federation_join_zone_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    zone_id: &str,
    as_learner: bool,
) -> PyResult<String> {
    let k = kernel.kernel_ref();
    k.distributed_coordinator()
        .join_zone(k, zone_id, as_learner)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(zone_id.to_string())
}

/// Federation control-plane: atomic share — create `new_zone_id`,
/// copy the subtree under `local_path` into it, and register the
/// `local_path → new_zone_id` mapping in the raft-replicated share
/// registry. Single op replaces the prior three-step
/// `create_zone + zone_share + register_share` orchestration.
///
/// Returns a Python dict with `zone_id` and `copied_entries`.
#[pyfunction]
#[pyo3(name = "federation_share_zone")]
fn federation_share_zone_py<'py>(
    py: pyo3::Python<'py>,
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    local_path: &str,
    new_zone_id: &str,
) -> PyResult<pyo3::Bound<'py, pyo3::types::PyDict>> {
    use pyo3::types::PyDict;
    let k = kernel.kernel_ref();
    let info = k
        .distributed_coordinator()
        .share_zone(k, local_path, new_zone_id)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let dict = PyDict::new(py);
    dict.set_item("zone_id", info.zone_id)?;
    dict.set_item("copied_entries", info.copied_entries)?;
    Ok(dict)
}

/// Federation control-plane: look up a previously-registered share
/// by remote path. Returns the `zone_id` string when found; `None`
/// when the path was never shared on any cluster member.
#[pyfunction]
#[pyo3(name = "federation_lookup_share")]
fn federation_lookup_share_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    remote_path: &str,
) -> PyResult<Option<String>> {
    let k = kernel.kernel_ref();
    let info = k
        .distributed_coordinator()
        .lookup_share(k, remote_path)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(info.map(|i| i.zone_id))
}

/// Federation introspection: bundled cluster status for `zone_id` —
/// leader identity, raft term, replication counts, mount link count.
/// Single round-trip for all introspection fields. Returns a Python
/// dict with zone_id / node_id / leader_id / term / commit_index /
/// applied_index / voter_count / witness_count / links_count /
/// has_store / is_leader.
#[pyfunction]
#[pyo3(name = "federation_cluster_info")]
fn federation_cluster_info_py<'py>(
    py: pyo3::Python<'py>,
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    zone_id: &str,
) -> PyResult<pyo3::Bound<'py, pyo3::types::PyDict>> {
    use pyo3::types::PyDict;
    let k = kernel.kernel_ref();
    let info = k
        .distributed_coordinator()
        .cluster_info(k, zone_id)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let dict = PyDict::new(py);
    dict.set_item("zone_id", info.zone_id)?;
    dict.set_item("node_id", info.node_id)?;
    dict.set_item("has_store", info.has_store)?;
    dict.set_item("is_leader", info.is_leader)?;
    dict.set_item("leader_id", info.leader_id)?;
    dict.set_item("term", info.term)?;
    dict.set_item("commit_index", info.commit_index)?;
    dict.set_item("applied_index", info.applied_index)?;
    dict.set_item("voter_count", info.voter_count)?;
    dict.set_item("witness_count", info.witness_count)?;
    dict.set_item("links_count", info.links_count)?;
    Ok(dict)
}

/// Federation control-plane: spawn a `ReplicationScanner` for `zone_id`
/// against `policies_json`.  Replaces the previous `kernel.start_replication_scanner`
/// PyKernel method — sits at the raft control-plane surface (parallel
/// to `register_share` / `lookup_share`) since the kernel never invokes
/// the scanner directly; Python boot opts in per zone+mount.
///
/// The returned scanner runs in a dedicated thread until process exit.
/// (Stop semantics are exposed via `ReplicationScanner::stop()`; not yet
/// surfaced to Python — the scanner ends with the process.)
#[pyfunction]
#[pyo3(name = "federation_start_replication_scanner")]
fn federation_start_replication_scanner_py(
    kernel: PyRef<'_, kernel::generated_kernel_abi_pyo3::PyKernel>,
    zone_id: &str,
    policies_json: &str,
    interval_ms: u64,
) -> PyResult<()> {
    let kernel_arc = kernel.kernel_arc();
    crate::replication_scanner::install_for_zone(kernel_arc, zone_id, policies_json, interval_ms)
        .map(|_| ())
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

// =============================================================================
// Unit tests: federation mount helpers (R16.1b)
// =============================================================================
//
// End-to-end mount success / idempotent / auto-create paths need a full
// ZoneConsensus + tokio runtime; those are exercised by the federation
// E2E suite (docker) gated at R12. Here we cover the pure helper
// surface that backs those flows — encoder, decoder, and field fidelity.

#[cfg(all(test, feature = "grpc", has_protos))]
mod mount_helpers_tests {
    use crate::zone_manager::{
        decode_file_metadata, encode_file_metadata, path_matches_prefix, DT_DIR, DT_MOUNT,
        I_LINKS_COUNT_KEY,
    };

    /// Mount + dir entries round-trip through encode/decode with the
    /// expected field fidelity: DT_MOUNT keeps ``target_zone_id``,
    /// DT_DIR carries empty ``target_zone_id``, and at a shared path
    /// the two only differ in ``entry_type`` / ``target_zone_id``
    /// (the identifying pair after the schema cleanup that dropped
    /// ``backend_name``).
    #[test]
    fn encode_file_metadata_roundtrip_fidelity() {
        let mount_bytes = encode_file_metadata("/x", DT_MOUNT, "zone-a", "zone-b");
        let dir_bytes = encode_file_metadata("/x", DT_DIR, "zone-a", "");

        let m = decode_file_metadata(&mount_bytes).unwrap();
        assert_eq!(m.path, "/x");
        assert_eq!(m.entry_type, DT_MOUNT);
        assert_eq!(m.zone_id, "zone-a");
        assert_eq!(m.target_zone_id, "zone-b");

        let d = decode_file_metadata(&dir_bytes).unwrap();
        assert_eq!(d.entry_type, DT_DIR);
        assert_eq!(d.target_zone_id, "");

        // Mount + dir at the same path differ only in entry_type / target_zone_id.
        assert_eq!(m.path, d.path);
        assert_eq!(m.zone_id, d.zone_id);
        assert_ne!(m.entry_type, d.entry_type);
        assert_ne!(m.target_zone_id, d.target_zone_id);
    }

    /// R16.3 boundary: accepts self + descendants separated by ``/``,
    /// rejects siblings with shared stems and non-descendants, matches
    /// everything when the normalized prefix is empty (share-the-whole-
    /// zone path). Covered in one table-driven test.
    #[test]
    fn path_matches_prefix_matrix() {
        let cases: &[(&str, &str, bool)] = &[
            // (path, prefix, expected)
            ("/usr/alice", "/usr/alice", true),         // self
            ("/usr/alice/", "/usr/alice", true),        // trailing slash
            ("/usr/alice/foo", "/usr/alice", true),     // direct child
            ("/usr/alice/foo/bar", "/usr/alice", true), // grandchild
            ("/usr/alicebob", "/usr/alice", false),     // sibling — shared stem
            ("/usr/alice-temp", "/usr/alice", false),   // sibling — shared stem
            ("/usr", "/usr/alice", false),              // ancestor, not descendant
            ("/etc/passwd", "/usr/alice", false),       // unrelated
            ("/", "", true),                            // empty prefix ≡ whole zone
            ("/a", "", true),
            ("/foo/bar", "", true),
        ];
        for (path, prefix, expected) in cases {
            assert_eq!(
                path_matches_prefix(path, prefix),
                *expected,
                "path_matches_prefix({path:?}, {prefix:?})",
            );
        }
    }

    #[test]
    fn i_links_count_key_matches_python_constant() {
        // Guard rail: the Rust constant must match
        // ``RaftMetadataStore._KEY_LINKS_COUNT`` in
        // ``src/nexus/storage/raft_metadata_store.py`` — a mismatch here
        // means Rust-side AdjustCounter writes to a different raft-log key
        // than the Python reader expects, and federation nlink tracking
        // silently diverges.
        assert_eq!(I_LINKS_COUNT_KEY, "__i_links_count__");
    }
}
