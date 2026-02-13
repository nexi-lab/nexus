// PyO3 #[pymethods] macro generates `.into()` conversions for PyErr that
// clippy flags as useless. This is a known PyO3 + clippy interaction.
#![allow(clippy::useless_conversion)]

//! PyO3 Python bindings for Nexus Metastore (sled state machine).
//!
//! Two drivers are exposed:
//! - `Metastore`: Direct sled access for embedded/EC mode (~5μs per op).
//! - `RaftConsensus`: Full Raft consensus for SC mode (writes replicated to peers).
//!
//! Both share the same `FullStateMachine` (sled KV). The difference is whether
//! writes pass through Raft consensus before being applied.
//!
//! # Python Usage
//!
//! ```python
//! from _nexus_raft import Metastore
//!
//! # Direct sled access (embedded mode)
//! store = Metastore("/var/lib/nexus/metadata")
//! store.set_metadata("/path/to/file", metadata_bytes)
//! metadata = store.get_metadata("/path/to/file")
//!
//! from _nexus_raft import RaftConsensus
//!
//! # SC mode with Raft consensus
//! node = RaftConsensus(1, "/var/lib/nexus/metadata", "0.0.0.0:2126", ["2@peer:2126"])
//! node.set_metadata("/path/to/file", metadata_bytes)  # replicated
//! ```

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::raft::{
    Command, CommandResult, FullStateMachine, HolderInfo as RustHolderInfo,
    LockInfo as RustLockInfo, LockState as RustLockState, StateMachine,
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

/// Python-compatible holder info.
#[pyclass(name = "HolderInfo")]
#[derive(Clone)]
pub struct PyHolderInfo {
    #[pyo3(get)]
    pub lock_id: String,
    #[pyo3(get)]
    pub holder_info: String,
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

impl From<RustLockState> for PyLockState {
    fn from(s: RustLockState) -> Self {
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
    #[pyo3(signature = (path, lock_id, max_holders=1, ttl_secs=30, holder_info=""))]
    pub fn acquire_lock(
        &mut self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> PyResult<PyLockState> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
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
// RaftConsensus: Full Raft consensus participant for SC (Strong Consistency) mode
// =============================================================================

/// Raft consensus metastore driver — all writes go through Raft consensus.
///
/// Embeds a full Raft participant with gRPC server inside the Python process.
/// Writes go through Raft consensus (replicated to peers).
/// Reads come from the local state machine (~5us).
///
/// Per-op SC/EC hints: callers pass `consistency="sc"` (default) or `"ec"`
/// to `set_metadata()` / `delete_metadata()`. EC writes submit to Raft but
/// return immediately (~5-10μs) without waiting for commit confirmation.
/// Lock operations always use SC for correctness.
///
/// This class is only available when built with `--features full` (grpc + python).
#[cfg(all(feature = "grpc", has_protos))]
#[pyclass(name = "RaftConsensus")]
pub struct PyRaftConsensus {
    /// ZoneConsensus handle (Clone + Send + Sync). The driver is owned by TransportLoop.
    node: crate::raft::ZoneConsensus<FullStateMachine>,
    /// Background Tokio runtime (owns the gRPC server + transport loop threads).
    runtime: tokio::runtime::Runtime,
    /// Shutdown signal sender.
    shutdown_tx: Option<tokio::sync::watch::Sender<bool>>,
    /// Node ID for status queries.
    node_id: u64,
}

#[cfg(all(feature = "grpc", has_protos))]
#[pymethods]
impl PyRaftConsensus {
    /// Create a new RaftConsensus node.
    ///
    /// This starts a full Raft participant with an embedded gRPC server
    /// and transport loop. The gRPC server accepts inter-node Raft messages.
    /// The transport loop drives ticks, applies committed entries, and
    /// sends outgoing messages to peers.
    ///
    /// Args:
    ///     node_id: Unique node ID within the cluster (1-indexed).
    ///     db_path: Path to the redb database directory.
    ///     bind_addr: gRPC bind address (e.g., "0.0.0.0:2126").
    ///     advertise_addr: Address other nodes use to reach this node (e.g., "http://10.0.0.2:2126").
    ///         Defaults to "http://{bind_addr}" if not provided.
    ///     peers: List of peer addresses in "id@host:port" format.
    ///
    /// Raises:
    ///     RuntimeError: If the node cannot be created or the server cannot start.
    #[new]
    #[pyo3(signature = (node_id, db_path, bind_addr="0.0.0.0:2126", advertise_addr=None, peers=vec![]))]
    pub fn new(node_id: u64, db_path: &str, bind_addr: &str, advertise_addr: Option<&str>, peers: Vec<String>) -> PyResult<Self> {
        use crate::raft::ZoneRaftRegistry;
        use crate::transport::{NodeAddress, RaftGrpcServer, ServerConfig};
        use std::sync::Arc;

        // Parse peer addresses
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim())
                    .map_err(|e| PyRuntimeError::new_err(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<PyResult<Vec<_>>>()?;

        // Parse bind address
        let bind_socket: std::net::SocketAddr = bind_addr.parse().map_err(|e| {
            PyRuntimeError::new_err(format!("Invalid bind address '{}': {}", bind_addr, e))
        })?;

        // Build Tokio runtime for background tasks
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-raft-bg")
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

        // Create zone registry and register a single zone
        // (ZoneRaftRegistry::create_zone spawns TransportLoop + auto-campaigns for single-node)
        let registry = Arc::new(ZoneRaftRegistry::new(
            std::path::PathBuf::from(db_path),
            node_id,
        ));

        let node = registry
            .create_zone("default", peer_addrs, runtime.handle())
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create zone: {}", e)))?;

        // Shutdown signal for gRPC server
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start gRPC server in background
        let config = ServerConfig {
            bind_address: bind_socket,
            ..Default::default()
        };
        let self_addr = advertise_addr
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("http://{}", bind_addr));
        let server = RaftGrpcServer::new(registry.clone(), config, self_addr);
        let shutdown_rx_server = shutdown_rx.clone();
        runtime.spawn(async move {
            let shutdown = async move {
                let mut rx = shutdown_rx_server;
                let _ = rx.changed().await;
            };
            if let Err(e) = server.serve_with_shutdown(shutdown).await {
                tracing::error!("Raft gRPC server error: {}", e);
            }
        });

        tracing::info!(
            "RaftConsensus node {} started (bind={}, peers={})",
            node_id,
            bind_addr,
            peers.len()
        );

        Ok(Self {
            node,
            runtime,
            shutdown_tx: Some(shutdown_tx),
            node_id,
        })
    }

    // =========================================================================
    // Metadata Operations (writes go through consensus)
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

    /// Get metadata for a path (local read, no consensus).
    pub fn get_metadata(&self, py: Python<'_>, path: &str) -> PyResult<Option<Vec<u8>>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.allow_threads(|| {
            self.runtime.block_on(async {
                node.with_state_machine(|sm| sm.get_metadata(&path))
                    .await
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to get metadata: {}", e)))
            })
        })
    }

    /// Get metadata for multiple paths in a single FFI call (local read, no consensus).
    pub fn get_metadata_multi(
        &self,
        py: Python<'_>,
        paths: Vec<String>,
    ) -> PyResult<Vec<(String, Option<Vec<u8>>)>> {
        let node = self.node.clone();
        py.allow_threads(|| {
            self.runtime.block_on(async {
                node.with_state_machine(|sm| sm.get_metadata_multi(&paths))
                    .await
                    .map_err(|e| {
                        PyRuntimeError::new_err(format!("Failed to get metadata multi: {}", e))
                    })
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
        py.allow_threads(|| {
            self.runtime.block_on(async {
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
    // Lock Operations (writes go through consensus)
    // =========================================================================

    /// Acquire a distributed lock (replicated through Raft consensus).
    #[pyo3(signature = (path, lock_id, max_holders=1, ttl_secs=30, holder_info=""))]
    pub fn acquire_lock(
        &self,
        py: Python<'_>,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> PyResult<PyLockState> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
        };
        let result = self.propose_command_raw(py, cmd)?;
        match result {
            CommandResult::LockResult(state) => Ok(state.into()),
            _ => Err(PyRuntimeError::new_err("Unexpected result type")),
        }
    }

    /// Release a distributed lock (replicated through Raft consensus).
    pub fn release_lock(&self, py: Python<'_>, path: &str, lock_id: &str) -> PyResult<bool> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        let result = self.propose_command_raw(py, cmd)?;
        Ok(matches!(result, CommandResult::Success))
    }

    /// Extend a lock's TTL (replicated through Raft consensus).
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
        };
        let result = self.propose_command_raw(py, cmd)?;
        Ok(matches!(result, CommandResult::Success))
    }

    /// Get lock info for a path (local read, no consensus).
    pub fn get_lock(&self, py: Python<'_>, path: &str) -> PyResult<Option<PyLockInfo>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.allow_threads(|| {
            self.runtime.block_on(async {
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
        py.allow_threads(|| {
            self.runtime.block_on(async {
                node.with_state_machine(|sm| sm.list_locks(&prefix, limit))
                    .await
                    .map(|locks| locks.into_iter().map(|l| l.into()).collect())
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to list locks: {}", e)))
            })
        })
    }

    // =========================================================================
    // Cluster Status
    // =========================================================================

    /// Check if this node is the current leader (atomic read, no I/O).
    pub fn is_leader(&self) -> bool {
        self.node.is_leader()
    }

    /// Get the current leader ID (None if unknown, atomic read).
    pub fn leader_id(&self) -> Option<u64> {
        self.node.leader_id()
    }

    /// Get this node's ID.
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    // =========================================================================
    // Lifecycle
    // =========================================================================

    /// Gracefully shut down the Raft node, gRPC server, and transport loop.
    pub fn shutdown(&mut self) -> PyResult<()> {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
            tracing::info!("RaftConsensus node {} shutting down", self.node_id);
        }
        Ok(())
    }

    /// Flush all pending writes to disk.
    pub fn flush(&self) -> PyResult<()> {
        // State machine flush is handled by redb internally (commits are durable)
        Ok(())
    }
}

#[cfg(all(feature = "grpc", has_protos))]
impl PyRaftConsensus {
    /// True Local-First EC write — bypasses Raft, returns WAL token.
    fn propose_command_ec_local(&self, py: Python<'_>, cmd: Command) -> PyResult<u64> {
        let node = self.node.clone();
        py.allow_threads(|| {
            self.runtime
                .block_on(node.propose_ec_local(cmd))
                .map_err(|e| PyRuntimeError::new_err(format!("EC local write failed: {}", e)))
        })
    }

    /// Propose a command through consensus and return success/failure (SC path).
    fn propose_command(&self, py: Python<'_>, cmd: Command) -> PyResult<bool> {
        let result = self.propose_command_raw(py, cmd)?;
        match result {
            CommandResult::Success => Ok(true),
            CommandResult::Error(e) => Err(PyRuntimeError::new_err(e)),
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::Value(_) => Ok(true),
        }
    }

    /// Propose a command through consensus and return the raw result (SC path).
    fn propose_command_raw(&self, py: Python<'_>, cmd: Command) -> PyResult<CommandResult> {
        let node = self.node.clone();
        py.allow_threads(|| {
            self.runtime
                .block_on(node.propose(cmd))
                .map_err(|e| PyRuntimeError::new_err(format!("Propose failed: {}", e)))
        })
    }
}

#[cfg(all(feature = "grpc", has_protos))]
impl Drop for PyRaftConsensus {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
    }
}

// =============================================================================
// ZoneManager: Multi-zone Raft registry exposed to Python
// =============================================================================

/// Multi-zone Raft manager — owns the registry, runtime, and gRPC server.
///
/// Unlike `RaftConsensus` (single zone, backward-compatible), `ZoneManager`
/// supports creating/removing multiple independent Raft zones that share
/// a single gRPC port and Tokio runtime.
///
/// # Python Usage
///
/// ```python
/// from _nexus_raft import ZoneManager
///
/// mgr = ZoneManager(1, "/var/lib/nexus/zones", "0.0.0.0:2126")
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
    ///     node_id: This node's ID (shared across all zones).
    ///     base_path: Base directory for zone sled databases.
    ///     bind_addr: gRPC bind address (e.g., "0.0.0.0:2126").
    ///     advertise_addr: Address other nodes use to reach this node (e.g., "http://10.0.0.2:2126").
    ///         Defaults to "http://{bind_addr}" if not provided.
    #[new]
    #[pyo3(signature = (node_id, base_path, bind_addr="0.0.0.0:2126", advertise_addr=None))]
    pub fn new(node_id: u64, base_path: &str, bind_addr: &str, advertise_addr: Option<&str>) -> PyResult<Self> {
        use crate::raft::ZoneRaftRegistry;
        use crate::transport::{RaftGrpcServer, ServerConfig};
        use std::sync::Arc;

        let bind_socket: std::net::SocketAddr = bind_addr.parse().map_err(|e| {
            PyRuntimeError::new_err(format!("Invalid bind address '{}': {}", bind_addr, e))
        })?;

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-zone-mgr")
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

        let registry = Arc::new(ZoneRaftRegistry::new(
            std::path::PathBuf::from(base_path),
            node_id,
        ));

        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        let config = ServerConfig {
            bind_address: bind_socket,
            ..Default::default()
        };
        let self_addr = advertise_addr
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("http://{}", bind_addr));
        let server = RaftGrpcServer::new(registry.clone(), config, self_addr);
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

        tracing::info!("ZoneManager node {} started (bind={})", node_id, bind_addr);

        Ok(Self {
            registry,
            runtime,
            shutdown_tx: Some(shutdown_tx),
            node_id,
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
                NodeAddress::parse(s.trim())
                    .map_err(|e| PyRuntimeError::new_err(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<PyResult<Vec<_>>>()?;

        let node = self
            .registry
            .create_zone(zone_id, peer_addrs, self.runtime.handle())
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
                NodeAddress::parse(s.trim())
                    .map_err(|e| PyRuntimeError::new_err(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<PyResult<Vec<_>>>()?;

        let node = self
            .registry
            .join_zone(zone_id, peer_addrs, self.runtime.handle())
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
    pub fn node_id(&self) -> u64 {
        self.node_id
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
/// Provides the same metadata/lock operations as `RaftConsensus` but scoped
/// to one zone. Obtained from `ZoneManager.create_zone()` or `.get_zone()`.
#[cfg(all(feature = "grpc", has_protos))]
#[pyclass(name = "ZoneHandle")]
pub struct PyZoneHandle {
    node: crate::raft::ZoneConsensus<FullStateMachine>,
    runtime_handle: tokio::runtime::Handle,
    zone_id: String,
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

    /// Get metadata for a path (local read, no consensus).
    pub fn get_metadata(&self, py: Python<'_>, path: &str) -> PyResult<Option<Vec<u8>>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.allow_threads(|| {
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
        py.allow_threads(|| {
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
    #[pyo3(signature = (path, lock_id, max_holders=1, ttl_secs=30, holder_info=""))]
    pub fn acquire_lock(
        &self,
        py: Python<'_>,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> PyResult<PyLockState> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
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
        };
        let result = self.propose_command_raw(py, cmd)?;
        Ok(matches!(result, CommandResult::Success))
    }

    /// Get lock info (local read, no consensus).
    pub fn get_lock(&self, py: Python<'_>, path: &str) -> PyResult<Option<PyLockInfo>> {
        let node = self.node.clone();
        let path = path.to_string();
        py.allow_threads(|| {
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
        py.allow_threads(|| {
            self.runtime_handle.block_on(async {
                node.with_state_machine(|sm| sm.list_locks(&prefix, limit))
                    .await
                    .map(|locks| locks.into_iter().map(|l| l.into()).collect())
                    .map_err(|e| PyRuntimeError::new_err(format!("Failed to list locks: {}", e)))
            })
        })
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
}

#[cfg(all(feature = "grpc", has_protos))]
impl PyZoneHandle {
    /// True Local-First EC write — bypasses Raft, returns WAL token.
    fn propose_command_ec_local(&self, py: Python<'_>, cmd: Command) -> PyResult<u64> {
        let node = self.node.clone();
        py.allow_threads(|| {
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
            CommandResult::Value(_) => Ok(true),
        }
    }

    fn propose_command_raw(&self, py: Python<'_>, cmd: Command) -> PyResult<CommandResult> {
        let node = self.node.clone();
        py.allow_threads(|| {
            self.runtime_handle
                .block_on(node.propose(cmd))
                .map_err(|e| PyRuntimeError::new_err(format!("Propose failed: {}", e)))
        })
    }
}

/// Python module initialization.
/// Module name: _nexus_raft (consistent with _nexus_fast)
/// Import as: from _nexus_raft import Metastore, RaftConsensus, ZoneManager
#[pymodule]
fn _nexus_raft(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMetastore>()?;
    m.add_class::<PyLockState>()?;
    m.add_class::<PyLockInfo>()?;
    m.add_class::<PyHolderInfo>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyRaftConsensus>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyZoneManager>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyZoneHandle>()?;
    Ok(())
}
