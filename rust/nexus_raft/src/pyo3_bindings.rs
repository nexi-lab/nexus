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
use crate::storage::SledStore;

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

/// Embedded metastore driver — direct sled state machine access.
///
/// Provides FFI access to the sled KV store without Raft consensus.
/// Used for embedded mode and as the base layer for EC mode (future).
///
/// Performance: ~5μs per operation.
#[pyclass(name = "Metastore")]
pub struct PyMetastore {
    store: SledStore,
    sm: FullStateMachine,
    next_index: u64,
}

#[pymethods]
impl PyMetastore {
    /// Create a new Metastore instance.
    ///
    /// Args:
    ///     path: Path to the sled database directory.
    ///
    /// Returns:
    ///     Metastore instance.
    ///
    /// Raises:
    ///     RuntimeError: If the database cannot be opened.
    #[new]
    pub fn new(path: &str) -> PyResult<Self> {
        let store = SledStore::open(path)
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to open sled: {}", e)))?;
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
    ///
    /// Returns:
    ///     True if successful.
    pub fn set_metadata(&mut self, path: &str, value: Vec<u8>) -> PyResult<bool> {
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        self.apply_command(cmd)
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

    /// Delete metadata for a path.
    ///
    /// Args:
    ///     path: The file path.
    ///
    /// Returns:
    ///     True if successful.
    pub fn delete_metadata(&mut self, path: &str) -> PyResult<bool> {
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        self.apply_command(cmd)
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

/// Raft consensus metastore driver — SC (Strong Consistency) mode.
///
/// Embeds a full Raft participant with gRPC server inside the Python process.
/// Writes go through Raft consensus (replicated to peers).
/// Reads come from the local state machine (~5us).
///
/// This class is only available when built with `--features full` (grpc + python).
#[cfg(all(feature = "grpc", has_protos))]
#[pyclass(name = "RaftConsensus")]
pub struct PyRaftConsensus {
    /// Shared RaftNode (also used by gRPC server and TransportLoop).
    node: std::sync::Arc<crate::raft::RaftNode<FullStateMachine>>,
    /// Background Tokio runtime (owns the gRPC server + transport loop threads).
    runtime: tokio::runtime::Runtime,
    /// Shutdown signal sender.
    shutdown_tx: Option<tokio::sync::watch::Sender<bool>>,
    /// Node ID for status queries.
    node_id: u64,
    /// EC mode: metadata writes apply locally + fire-and-forget propose (lazy consensus).
    /// SC mode (default): all writes wait for Raft consensus before ACK.
    /// Lock operations always use SC regardless of this flag.
    lazy_consensus: bool,
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
    ///     db_path: Path to the sled database directory.
    ///     bind_addr: gRPC bind address (e.g., "0.0.0.0:2126").
    ///     peers: List of peer addresses in "id@host:port" format.
    ///     lazy: If True, metadata writes use EC (lazy consensus). Default: False (SC).
    ///
    /// Raises:
    ///     RuntimeError: If the node cannot be created or the server cannot start.
    #[new]
    #[pyo3(signature = (node_id, db_path, bind_addr="0.0.0.0:2126", peers=vec![], lazy=false))]
    pub fn new(
        node_id: u64,
        db_path: &str,
        bind_addr: &str,
        peers: Vec<String>,
        lazy: bool,
    ) -> PyResult<Self> {
        use crate::transport::{
            NodeAddress, RaftClientPool, RaftServer, ServerConfig, TransportLoop,
        };

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

        let config = ServerConfig {
            bind_address: bind_socket,
            ..Default::default()
        };

        // Create RaftServer (which creates RaftNode + storage)
        let server = RaftServer::with_config(node_id, db_path, config, peer_addrs.clone())
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create Raft server: {}", e)))?;

        let node = server.node();

        // Build Tokio runtime for background tasks
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-raft-bg")
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("Failed to create runtime: {}", e)))?;

        // Shutdown signal
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        // Start transport loop in background
        let peer_map = peer_addrs.into_iter().map(|p| (p.id, p)).collect();
        let transport_loop = TransportLoop::new(node.clone(), peer_map, RaftClientPool::new());
        let shutdown_rx_transport = shutdown_rx.clone();
        runtime.spawn(transport_loop.run(shutdown_rx_transport));

        // Start gRPC server in background
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

        // Single-node: campaign immediately to become leader
        if peers.is_empty() {
            let campaign_node = node.clone();
            runtime.spawn(async move {
                // Give the transport loop a moment to start
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                if let Err(e) = campaign_node.campaign().await {
                    tracing::error!("Failed to campaign: {}", e);
                }
            });
        }

        let mode = if lazy { "EC (lazy)" } else { "SC" };
        tracing::info!(
            "RaftConsensus node {} started (mode={}, bind={}, peers={})",
            node_id,
            mode,
            bind_addr,
            peers.len()
        );

        Ok(Self {
            node,
            runtime,
            shutdown_tx: Some(shutdown_tx),
            node_id,
            lazy_consensus: lazy,
        })
    }

    // =========================================================================
    // Metadata Operations (writes go through consensus)
    // =========================================================================

    /// Set metadata for a path.
    /// SC mode: waits for Raft consensus (replicated).
    /// EC mode: applies locally + fire-and-forget propose (lazy replication).
    pub fn set_metadata(&self, py: Python<'_>, path: &str, value: Vec<u8>) -> PyResult<bool> {
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        if self.lazy_consensus {
            self.lazy_propose_metadata(py, cmd)
        } else {
            self.propose_command(py, cmd)
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

    /// Delete metadata for a path.
    /// SC mode: waits for Raft consensus.
    /// EC mode: applies locally + fire-and-forget propose.
    pub fn delete_metadata(&self, py: Python<'_>, path: &str) -> PyResult<bool> {
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        if self.lazy_consensus {
            self.lazy_propose_metadata(py, cmd)
        } else {
            self.propose_command(py, cmd)
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

    /// Check if this node is using lazy consensus (EC mode).
    pub fn is_lazy(&self) -> bool {
        self.lazy_consensus
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

    /// Check if this node is the current leader.
    pub fn is_leader(&self, py: Python<'_>) -> PyResult<bool> {
        let node = self.node.clone();
        Ok(py.allow_threads(|| self.runtime.block_on(node.is_leader())))
    }

    /// Get the current leader ID (None if unknown).
    pub fn leader_id(&self, py: Python<'_>) -> PyResult<Option<u64>> {
        let node = self.node.clone();
        Ok(py.allow_threads(|| self.runtime.block_on(node.leader_id())))
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
        // State machine flush is handled by sled internally
        Ok(())
    }
}

#[cfg(all(feature = "grpc", has_protos))]
impl PyRaftConsensus {
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

    /// EC path: apply metadata command locally + propose with retry for replication.
    ///
    /// The local apply is immediate (~5μs), giving read-after-write consistency.
    /// The background propose replicates to peers via Raft consensus with retry.
    /// Metadata operations are idempotent (upsert), so double-apply from Raft
    /// committing the same entry is safe.
    ///
    /// Retry guarantees "eventual" in eventual consistency: the propose will be
    /// retried with exponential backoff until it succeeds or max attempts is reached.
    fn lazy_propose_metadata(&self, py: Python<'_>, cmd: Command) -> PyResult<bool> {
        let node = self.node.clone();
        let cmd_for_propose = cmd.clone();

        // 1. Apply to local state machine immediately (read-after-write)
        py.allow_threads(|| {
            self.runtime.block_on(async {
                node.with_state_machine_mut(|sm| {
                    let index = sm.last_applied_index() + 1;
                    sm.apply(index, &cmd)
                })
                .await
                .map_err(|e| PyRuntimeError::new_err(format!("Local apply failed: {}", e)))
            })
        })?;

        // 2. Background propose with retry (guarantees "eventual")
        let node_bg = self.node.clone();
        self.runtime.spawn(async move {
            use std::time::Duration;
            const MAX_ATTEMPTS: u32 = 50;
            const BASE_DELAY_MS: u64 = 100;
            const MAX_DELAY_MS: u64 = 10_000;

            for attempt in 1..=MAX_ATTEMPTS {
                match node_bg.propose(cmd_for_propose.clone()).await {
                    Ok(_) => {
                        if attempt > 1 {
                            tracing::info!("EC propose succeeded on attempt {}", attempt);
                        }
                        return;
                    }
                    Err(e) => {
                        let delay = Duration::from_millis(
                            (BASE_DELAY_MS * 2u64.saturating_pow(attempt - 1)).min(MAX_DELAY_MS),
                        );
                        tracing::warn!(
                            "EC propose attempt {}/{} failed: {} (retry in {:?})",
                            attempt,
                            MAX_ATTEMPTS,
                            e,
                            delay
                        );
                        tokio::time::sleep(delay).await;
                    }
                }
            }
            tracing::error!(
                "EC propose gave up after {} attempts — write is local-only",
                MAX_ATTEMPTS
            );
        });

        Ok(true)
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

/// Python module initialization.
/// Module name: _nexus_raft (consistent with _nexus_fast)
/// Import as: from _nexus_raft import Metastore, RaftConsensus
#[pymodule]
fn _nexus_raft(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMetastore>()?;
    m.add_class::<PyLockState>()?;
    m.add_class::<PyLockInfo>()?;
    m.add_class::<PyHolderInfo>()?;
    #[cfg(all(feature = "grpc", has_protos))]
    m.add_class::<PyRaftConsensus>()?;
    Ok(())
}
