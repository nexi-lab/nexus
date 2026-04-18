// PyO3 #[pymethods] macro generates `.into()` conversions for PyErr that
// clippy flags as useless. This is a known PyO3 + clippy interaction.
#![allow(clippy::useless_conversion)]

//! PyO3 Python bindings for Nexus Metastore (sled state machine).
//!
//! Three drivers are exposed:
//! - `Metastore`: Direct redb access for embedded mode (~5μs per op).
//! - `ZoneManager`: Multi-zone Raft registry owner (creates/manages zones).
//! - `ZoneHandle`: Per-zone Raft node handle (metadata/lock operations).
//!
//! # Python Usage
//!
//! ```python
//! from _nexus_raft import Metastore
//!
//! # Direct redb access (embedded mode)
//! store = Metastore("/var/lib/nexus/metadata")
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
#[pyclass(name = "HolderInfo")]
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
#[pyclass(name = "LockState")]
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
#[pyclass(name = "LockInfo")]
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
#[pyclass(name = "Metastore")]
pub struct PyMetastore {
    store: RedbStore,
    sm: FullStateMachine,
    next_index: u64,
}

#[pymethods]
impl PyMetastore {
    /// Create a new Metastore instance.
    ///
    /// Args:
    ///     path: Path to the redb database directory.
    ///
    /// Returns:
    ///     Metastore instance.
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

impl PyMetastore {
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
// ZoneManager: Multi-zone Raft registry exposed to Python
// =============================================================================

/// Multi-zone Raft manager — owns the registry, runtime, and gRPC server.
///
/// Supports creating/removing multiple independent Raft zones that share
/// a single gRPC port and Tokio runtime.
///
/// # Python Usage
///
/// ```python
/// from _nexus_raft import ZoneManager
///
/// mgr = ZoneManager("nexus-1", "/var/lib/nexus/zones", "0.0.0.0:2126")
/// handle = mgr.create_zone("alpha", ["2@peer:2126"], lazy=False)
/// handle.set_metadata("/file.txt", b"...")
/// handle.get_metadata("/file.txt")
/// mgr.list_zones()  # ["alpha"]
/// ```
#[cfg(all(feature = "grpc", has_protos))]
#[pyclass(name = "ZoneManager")]
pub struct PyZoneManager {
    registry: std::sync::Arc<crate::raft::ZoneRaftRegistry>,
    runtime: tokio::runtime::Runtime,
    shutdown_tx: Option<tokio::sync::watch::Sender<bool>>,
    node_id: u64,
    use_tls: bool,
}

#[cfg(all(feature = "grpc", has_protos))]
#[pymethods]
impl PyZoneManager {
    /// Create a new ZoneManager.
    ///
    /// Starts a Tokio runtime and gRPC server. Zones are added dynamically
    /// with `create_zone()`.
    ///
    /// Args:
    ///     hostname: This node's hostname (node_id derived via SHA-256).
    ///     base_path: Base directory for zone sled databases.
    ///     bind_addr: gRPC bind address (e.g., "0.0.0.0:2126").
    ///     tls_cert_path: Path to PEM certificate file (mTLS). All three TLS paths must be set, or none.
    ///     tls_key_path: Path to PEM private key file (mTLS).
    ///     tls_ca_path: Path to PEM CA certificate file (mTLS).
    ///     ca_key_path: Path to CA private key file (read once at startup for server-side cert signing).
    ///     join_token_hash: SHA-256 hash of join token password (for JoinCluster verification).
    #[new]
    #[pyo3(signature = (hostname, base_path, bind_addr="0.0.0.0:2126", tls_cert_path=None, tls_key_path=None, tls_ca_path=None, ca_key_path=None, join_token_hash=None))]
    #[allow(clippy::too_many_arguments)] // PyO3 constructor — Python API needs flat keyword args
    pub fn new(
        hostname: &str,
        base_path: &str,
        bind_addr: &str,
        tls_cert_path: Option<&str>,
        tls_key_path: Option<&str>,
        tls_ca_path: Option<&str>,
        ca_key_path: Option<&str>,
        join_token_hash: Option<&str>,
    ) -> PyResult<Self> {
        use crate::raft::ZoneRaftRegistry;
        use crate::transport::{RaftGrpcServer, ServerConfig, TlsConfig};
        use std::sync::Arc;

        let node_id = crate::transport::hostname_to_node_id(hostname);

        // Initialize Rust tracing (once) so gRPC server logs are visible.
        // Uses RUST_LOG env var (e.g., "info,nexus_raft=debug").
        static TRACING_INIT: std::sync::Once = std::sync::Once::new();
        TRACING_INIT.call_once(|| {
            let _ = tracing_subscriber::fmt()
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("info".parse().unwrap()),
                )
                .try_init();
        });

        // Parse TLS config from file paths (all-or-nothing)
        let tls_config = match (tls_cert_path, tls_key_path, tls_ca_path) {
            (Some(cert), Some(key), Some(ca)) => {
                let cert_pem = std::fs::read(cert).map_err(|e| {
                    PyRuntimeError::new_err(format!("Failed to read TLS cert '{}': {}", cert, e))
                })?;
                let key_pem = std::fs::read(key).map_err(|e| {
                    PyRuntimeError::new_err(format!("Failed to read TLS key '{}': {}", key, e))
                })?;
                let ca_pem = std::fs::read(ca).map_err(|e| {
                    PyRuntimeError::new_err(format!("Failed to read TLS CA '{}': {}", ca, e))
                })?;
                Some(TlsConfig {
                    cert_pem,
                    key_pem,
                    ca_pem,
                })
            }
            (None, None, None) => None,
            _ => {
                return Err(PyRuntimeError::new_err(
                    "TLS requires all three: tls_cert_path, tls_key_path, tls_ca_path",
                ))
            }
        };

        let bind_socket: std::net::SocketAddr = bind_addr.parse().map_err(|e| {
            PyRuntimeError::new_err(format!("Invalid bind address '{}': {}", bind_addr, e))
        })?;

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-zone-mgr")
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

        let registry = Arc::new(ZoneRaftRegistry::with_tls(
            std::path::PathBuf::from(base_path),
            node_id,
            tls_config.clone(),
        ));

        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        let config = ServerConfig {
            bind_address: bind_socket,
            tls: tls_config.clone(),
            ..Default::default()
        };
        let use_tls = tls_config.is_some();
        let mut server = RaftGrpcServer::new(registry.clone(), config);
        // Configure JoinCluster RPC support if join token is available.
        // Read CA key from disk once — held in memory for server-side cert signing.
        if let (Some(ca_key_path), Some(token_hash)) = (ca_key_path, join_token_hash) {
            let ca_key_pem = std::fs::read(ca_key_path).map_err(|e| {
                PyRuntimeError::new_err(format!("Failed to read CA key for JoinCluster: {}", e))
            })?;
            server = server.with_join_config(ca_key_pem, token_hash.to_string());
        }
        let shutdown_rx_server = shutdown_rx.clone();
        runtime.spawn(async move {
            let shutdown = async move {
                let mut rx = shutdown_rx_server;
                let _ = rx.changed().await;
            };
            if let Err(e) = server.serve_with_shutdown(shutdown).await {
                tracing::error!("ZoneManager gRPC server error: {}", e);
            }
        });

        tracing::info!(
            "ZoneManager node {} started (bind={}, tls={})",
            node_id,
            bind_addr,
            use_tls,
        );

        Ok(Self {
            registry,
            runtime,
            shutdown_tx: Some(shutdown_tx),
            node_id,
            use_tls,
        })
    }

    /// Create a new zone with its own Raft group.
    ///
    /// Args:
    ///     zone_id: Unique zone identifier.
    ///     peers: Peer addresses in "id@host:port" format.
    ///
    /// Returns:
    ///     ZoneHandle for the new zone.
    #[pyo3(signature = (zone_id, peers=vec![]))]
    pub fn create_zone(&self, zone_id: &str, peers: Vec<String>) -> PyResult<PyZoneHandle> {
        use crate::transport::NodeAddress;

        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| PyRuntimeError::new_err(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<PyResult<Vec<_>>>()?;

        // PyO3 is the sync↔async boundary here: Python is sync, but setup
        // needs to .await a campaign / snapshot restore. block_on is safe
        // because this thread is outside any tokio runtime — the rule it
        // violates ("Cannot start a runtime from within a runtime") only
        // applies to callers already inside an async context.
        let node = self
            .runtime
            .handle()
            .block_on(
                self.registry
                    .create_zone(zone_id, peer_addrs, self.runtime.handle()),
            )
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create zone: {}", e)))?;

        Ok(PyZoneHandle {
            node,
            runtime_handle: self.runtime.handle().clone(),
            zone_id: zone_id.to_string(),
        })
    }

    /// Join an existing zone as a new Voter.
    ///
    /// Creates a local ZoneConsensus for this zone without bootstrapping ConfState.
    /// After calling this, send a JoinZone RPC to the leader — the leader will
    /// propose ConfChange(AddNode) and auto-send a snapshot.
    ///
    /// Args:
    ///     zone_id: Zone to join.
    ///     peers: Existing peer addresses in "id@host:port" format.
    ///
    /// Returns:
    ///     ZoneHandle for the joined zone.
    #[pyo3(signature = (zone_id, peers=vec![]))]
    pub fn join_zone(&self, zone_id: &str, peers: Vec<String>) -> PyResult<PyZoneHandle> {
        use crate::transport::NodeAddress;

        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| PyRuntimeError::new_err(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<PyResult<Vec<_>>>()?;

        // See create_zone for why we use runtime.block_on here (PyO3
        // sync↔async boundary).
        let node = self
            .runtime
            .handle()
            .block_on(
                self.registry
                    .join_zone(zone_id, peer_addrs, self.runtime.handle()),
            )
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to join zone: {}", e)))?;

        Ok(PyZoneHandle {
            node,
            runtime_handle: self.runtime.handle().clone(),
            zone_id: zone_id.to_string(),
        })
    }

    /// Get a handle for an existing zone.
    ///
    /// Returns:
    ///     ZoneHandle if zone exists, None otherwise.
    pub fn get_zone(&self, zone_id: &str) -> Option<PyZoneHandle> {
        self.registry.get_node(zone_id).map(|node| PyZoneHandle {
            node,
            runtime_handle: self.runtime.handle().clone(),
            zone_id: zone_id.to_string(),
        })
    }

    /// Remove a zone, shutting down its transport loop.
    pub fn remove_zone(&self, zone_id: &str) -> PyResult<()> {
        self.registry
            .remove_zone(zone_id)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to remove zone: {}", e)))
    }

    /// List all zone IDs.
    pub fn list_zones(&self) -> Vec<String> {
        self.registry.list_zones()
    }

    /// Get this node's ID.
    #[getter]
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    /// Set search capabilities for a zone (Issue #3147, Phase 2).
    ///
    /// Called by Python search daemon at startup to register real capabilities.
    /// The Rust gRPC handler reads these when remote nodes query capabilities.
    ///
    /// # Arguments
    /// * `zone_id` — Zone to set capabilities for.
    /// * `device_tier` — "phone", "laptop", or "server".
    /// * `search_modes` — List of supported modes: "keyword", "semantic", "hybrid".
    /// * `has_graph` — Whether graph search is available.
    /// * `embedding_model` — Embedding model name (empty string if none).
    /// * `embedding_dimensions` — Embedding vector dimensions (0 if none).
    pub fn set_search_capabilities(
        &self,
        zone_id: &str,
        device_tier: &str,
        search_modes: Vec<String>,
        has_graph: bool,
        embedding_model: &str,
        embedding_dimensions: i32,
    ) -> PyResult<()> {
        use crate::raft::SearchCapabilitiesInfo;

        self.registry.set_search_capabilities(
            zone_id,
            SearchCapabilitiesInfo {
                device_tier: device_tier.to_string(),
                search_modes,
                embedding_model: embedding_model.to_string(),
                embedding_dimensions,
                has_graph,
            },
        );
        Ok(())
    }

    /// Gracefully shut down all zones and the gRPC server.
    pub fn shutdown(&mut self) -> PyResult<()> {
        self.registry.shutdown_all();
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
        tracing::info!("ZoneManager node {} shut down", self.node_id);
        Ok(())
    }
}

#[cfg(all(feature = "grpc", has_protos))]
impl Drop for PyZoneManager {
    fn drop(&mut self) {
        self.registry.shutdown_all();
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
    }
}

// =============================================================================
// ZoneHandle: Per-zone Raft node handle (lightweight, returned by ZoneManager)
// =============================================================================

/// Handle to a single zone's Raft node.
///
/// Provides metadata/lock operations scoped to one zone.
/// Obtained from `ZoneManager.create_zone()` or `.get_zone()`.
#[cfg(all(feature = "grpc", has_protos))]
#[pyclass(name = "ZoneHandle")]
pub struct PyZoneHandle {
    node: crate::raft::ZoneConsensus<FullStateMachine>,
    runtime_handle: tokio::runtime::Handle,
    zone_id: String,
}

#[cfg(all(feature = "grpc", has_protos))]
impl PyZoneHandle {
    /// Cheap clone of the underlying `ZoneConsensus` (Arc-based internally).
    ///
    /// Exposed so sibling crates (specifically ``nexus_kernel``'s
    /// ``raft_metastore`` module) can construct a ``Metastore`` impl over
    /// the same Raft state machine without touching private fields. The
    /// ``#[pymethods]`` block below can't hold this because it would
    /// require the return type to be a Python class.
    pub fn consensus_node(&self) -> crate::raft::ZoneConsensus<FullStateMachine> {
        self.node.clone()
    }

    /// Clone the tokio runtime handle for the zone manager.
    ///
    /// Used by ``nexus_kernel::raft_metastore::ZoneMetastore`` to bridge
    /// the sync ``Metastore`` trait onto Raft's async ``propose`` API.
    pub fn runtime_handle(&self) -> tokio::runtime::Handle {
        self.runtime_handle.clone()
    }
}

#[cfg(all(feature = "grpc", has_protos))]
#[pymethods]
impl PyZoneHandle {
    /// Get this zone's ID.
    pub fn zone_id(&self) -> &str {
        &self.zone_id
    }

    // =========================================================================
    // Metadata Operations (all writes go through Raft consensus)
    // =========================================================================

    /// Set metadata for a path.
    ///
    /// Args:
    ///     path: The file path (key).
    ///     value: Serialized metadata bytes.
    ///     consistency: "sc" (default, wait for commit) or "ec" (local write + WAL token).
    ///
    /// Returns:
    ///     EC mode: write token (int) for polling via is_committed().
    ///     SC mode: None (write is already committed when this returns).
    #[pyo3(signature = (path, value, consistency="sc"))]
    pub fn set_metadata(
        &self,
        py: Python<'_>,
        path: &str,
        value: Vec<u8>,
        consistency: &str,
    ) -> PyResult<Option<u64>> {
        validate_consistency(consistency)?;
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        match consistency {
            CONSISTENCY_EC => Ok(Some(self.propose_command_ec_local(py, cmd)?)),
            _ => {
                self.propose_command(py, cmd)?;
                Ok(None)
            }
        }
    }

    /// Compare-and-swap metadata for a path (replicated through consensus).
    ///
    /// Args:
    ///     path: The file path (key).
    ///     value: Serialized metadata bytes.
    ///     expected_version: Expected current version (0 = create-only).
    ///     consistency: "sc" (default, wait for commit) or "ec" (local write).
    ///
    /// Returns:
    ///     Tuple of (success: bool, current_version: int).
    #[pyo3(signature = (path, value, expected_version, consistency="sc"))]
    pub fn cas_set_metadata(
        &self,
        py: Python<'_>,
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
        let result = self.propose_command_raw(py, cmd)?;
        match result {
            CommandResult::CasResult {
                success,
                current_version,
            } => Ok((success, current_version)),
            _ => Err(PyRuntimeError::new_err("Unexpected CAS result type")),
        }
    }

    /// Atomically adjust a metadata counter by a signed delta (Raft-replicated).
    ///
    /// The read-modify-write happens during apply() on each node,
    /// serialized by Raft — no lost updates under concurrency.
    ///
    /// Args:
    ///     key: The metadata key (e.g., "__i_links_count__").
    ///     delta: Signed adjustment (+1 to increment, -1 to decrement).
    ///
    /// Returns:
    ///     New counter value after adjustment.
    pub fn adjust_counter(&self, py: Python<'_>, key: &str, delta: i64) -> PyResult<i64> {
        let cmd = Command::AdjustCounter {
            key: key.to_string(),
            delta,
        };
        let result = self.propose_command_raw(py, cmd)?;
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

    /// Get metadata for a path (local read, no consensus).
    pub fn get_metadata(&self, py: Python<'_>, path: &str) -> PyResult<Option<Vec<u8>>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.detach(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.get_metadata(&path))
                    .await
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to get metadata: {}", e)))
            })
        })
    }

    /// Delete metadata for a path.
    ///
    /// Args:
    ///     path: The file path.
    ///     consistency: "sc" (default, wait for commit) or "ec" (local write + WAL token).
    ///
    /// Returns:
    ///     EC mode: write token (int) for polling via is_committed().
    ///     SC mode: None (write is already committed when this returns).
    #[pyo3(signature = (path, consistency="sc"))]
    pub fn delete_metadata(
        &self,
        py: Python<'_>,
        path: &str,
        consistency: &str,
    ) -> PyResult<Option<u64>> {
        validate_consistency(consistency)?;
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        match consistency {
            CONSISTENCY_EC => Ok(Some(self.propose_command_ec_local(py, cmd)?)),
            _ => {
                self.propose_command(py, cmd)?;
                Ok(None)
            }
        }
    }

    /// List all metadata with a prefix (local read, no consensus).
    pub fn list_metadata(&self, py: Python<'_>, prefix: &str) -> PyResult<Vec<(String, Vec<u8>)>> {
        let node = self.node.clone();
        let prefix = prefix.to_string();
        py.detach(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.list_metadata(&prefix))
                    .await
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to list metadata: {}", e)))
            })
        })
    }

    /// Check if an EC write token has been replicated to a majority.
    ///
    /// Args:
    ///     token: Write token returned by set_metadata/delete_metadata with consistency="ec".
    ///
    /// Returns:
    ///     "committed" — replicated to majority.
    ///     "pending" — local only, awaiting replication.
    ///     None — invalid token or no replication log.
    pub fn is_committed(&self, token: u64) -> Option<String> {
        self.node.is_committed(token).map(|s| s.to_string())
    }

    // =========================================================================
    // Lock Operations (always SC)
    // =========================================================================

    /// Acquire a distributed lock (always replicated through consensus).
    #[pyo3(signature = (path, lock_id, max_holders=1, ttl_secs=30, holder_info="", mode="exclusive"))]
    #[allow(clippy::too_many_arguments)]
    pub fn acquire_lock(
        &self,
        py: Python<'_>,
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
        let result = self.propose_command_raw(py, cmd)?;
        match result {
            CommandResult::LockResult(state) => Ok(state.into()),
            _ => Err(PyRuntimeError::new_err("Unexpected result type")),
        }
    }

    /// Release a distributed lock (replicated through consensus).
    pub fn release_lock(&self, py: Python<'_>, path: &str, lock_id: &str) -> PyResult<bool> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        let result = self.propose_command_raw(py, cmd)?;
        Ok(matches!(result, CommandResult::Success))
    }

    /// Extend a lock's TTL (replicated through consensus).
    pub fn extend_lock(
        &self,
        py: Python<'_>,
        path: &str,
        lock_id: &str,
        new_ttl_secs: u32,
    ) -> PyResult<bool> {
        let cmd = Command::ExtendLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            new_ttl_secs,
            now_secs: crate::prelude::FullStateMachine::now(),
        };
        let result = self.propose_command_raw(py, cmd)?;
        Ok(matches!(result, CommandResult::Success))
    }

    /// Get lock info (local read, no consensus).
    pub fn get_lock(&self, py: Python<'_>, path: &str) -> PyResult<Option<PyLockInfo>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.detach(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.get_lock(&path))
                    .await
                    .map(|opt| opt.map(|l| l.into()))
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to get lock: {}", e)))
            })
        })
    }

    /// List all locks matching a prefix (local read, no consensus).
    #[pyo3(signature = (prefix="", limit=1000))]
    pub fn list_locks(
        &self,
        py: Python<'_>,
        prefix: &str,
        limit: usize,
    ) -> PyResult<Vec<PyLockInfo>> {
        let node = self.node.clone();
        let prefix = prefix.to_string();
        py.detach(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.list_locks(&prefix, limit))
                    .await
                    .map(|locks| locks.into_iter().map(|l| l.into()).collect())
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to list locks: {}", e)))
            })
        })
    }

    // =========================================================================
    // Batch Operations
    // =========================================================================

    /// Get metadata for multiple paths in a single FFI call (local read, no consensus).
    ///
    /// Args:
    ///     paths: List of file paths to look up.
    ///
    /// Returns:
    ///     List of (path, metadata_bytes_or_none) tuples.
    pub fn get_metadata_multi(
        &self,
        py: Python<'_>,
        paths: Vec<String>,
    ) -> PyResult<Vec<(String, Option<Vec<u8>>)>> {
        let node = self.node.clone();
        py.detach(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.get_metadata_multi(&paths))
                    .await
                    .map_err(|e| {
                        PyRuntimeError::new_err(format!("Failed to get metadata multi: {}", e))
                    })
            })
        })
    }

    /// Set multiple metadata entries via Raft consensus.
    ///
    /// Each entry is proposed as an individual Raft command (matching
    /// Metastore's per-item semantics). Not batch-atomic — partial
    /// success is possible if a proposal fails mid-batch.
    ///
    /// Args:
    ///     items: List of (path, value_bytes) tuples to set.
    ///
    /// Returns:
    ///     Number of entries set.
    pub fn batch_set_metadata(
        &self,
        py: Python<'_>,
        items: Vec<(String, Vec<u8>)>,
    ) -> PyResult<usize> {
        let count = items.len();
        for (path, value) in items {
            let cmd = Command::SetMetadata { key: path, value };
            self.propose_command(py, cmd)?;
        }
        Ok(count)
    }

    /// Delete multiple metadata entries via Raft consensus.
    ///
    /// Each entry is proposed as an individual Raft command.
    ///
    /// Args:
    ///     keys: List of paths to delete.
    ///
    /// Returns:
    ///     Number of entries deleted.
    pub fn batch_delete_metadata(&self, py: Python<'_>, keys: Vec<String>) -> PyResult<usize> {
        let count = keys.len();
        for key in keys {
            let cmd = Command::DeleteMetadata { key };
            self.propose_command(py, cmd)?;
        }
        Ok(count)
    }

    // =========================================================================
    // Cluster Status
    // =========================================================================

    /// Check if this node is the current leader (atomic read, no I/O).
    pub fn is_leader(&self) -> bool {
        self.node.is_leader()
    }

    /// Get the current leader ID (None if unknown).
    pub fn leader_id(&self) -> Option<u64> {
        self.node.leader_id()
    }

    // F2 C8 (Option A): federation metastore bridging now lives on the
    // ``nexus_kernel`` side (rust/kernel/src/raft_metastore.rs). It
    // takes a ``&PyZoneHandle`` via ``consensus_node()`` /
    // ``runtime_handle()``, builds a ``ZoneMetastore``, and installs
    // it on the kernel's mount_metastores map. No cross-crate PyO3
    // handoff — raft and kernel are both inside the same cdylib now.
}

#[cfg(all(feature = "grpc", has_protos))]
impl PyZoneHandle {
    /// True Local-First EC write — bypasses Raft, returns WAL token.
    fn propose_command_ec_local(&self, py: Python<'_>, cmd: Command) -> PyResult<u64> {
        let node = self.node.clone();
        py.detach(|| {
            self.runtime_handle
                .block_on(node.propose_ec_local(cmd))
                .map_err(|e| PyRuntimeError::new_err(format!("EC local write failed: {}", e)))
        })
    }

    fn propose_command(&self, py: Python<'_>, cmd: Command) -> PyResult<bool> {
        let result = self.propose_command_raw(py, cmd)?;
        match result {
            CommandResult::Success => Ok(true),
            CommandResult::Error(e) => Err(PyRuntimeError::new_err(e)),
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::CasResult { success, .. } => Ok(success),
            CommandResult::Value(_) => Ok(true),
        }
    }

    fn propose_command_raw(&self, py: Python<'_>, cmd: Command) -> PyResult<CommandResult> {
        let node = self.node.clone();
        py.detach(|| {
            self.runtime_handle
                .block_on(node.propose(cmd))
                .map_err(|e| PyRuntimeError::new_err(format!("Propose failed: {}", e)))
        })
    }
}

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
    let parsed_token =
        crate::transport::parse_join_token(join_token).map_err(PyRuntimeError::new_err)?;
    let password = parsed_token.password;
    let expected_fingerprint = parsed_token.ca_fingerprint;

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
            "root", &password, 30, // timeout_secs
        ))
        .map_err(|e| PyRuntimeError::new_err(format!("JoinCluster RPC failed: {}", e)))?;

    // Verify CA fingerprint matches the join token
    let ca_fingerprint = crate::transport::ca_fingerprint_from_pem(&result.ca_pem)
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
/// F2 C8 (Option A): raft is an rlib inside the ``nexus_kernel`` cdylib
/// now — the old ``#[pymodule] fn _nexus_raft`` is gone. Kernel's own
/// ``#[pymodule]`` calls this function to expose ``Metastore`` /
/// ``ZoneManager`` / ``ZoneHandle`` from the single ``nexus_kernel``
/// Python module. Kept ``pub`` so ``kernel::lib::nexus_kernel`` can
/// reach it via the ``nexus_raft_lib::register_python_classes`` path.
pub fn register_python_classes(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMetastore>()?;
    m.add_class::<PyLockState>()?;
    m.add_class::<PyLockInfo>()?;
    m.add_class::<PyHolderInfo>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyZoneManager>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyZoneHandle>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(join_cluster, m)?)?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_function(wrap_pyfunction!(hostname_to_node_id, m)?)?;
    Ok(())
}
