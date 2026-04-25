//! Pure-Rust `ZoneManager` — multi-zone raft registry owner.
//!
//! Extracted from `PyZoneManager` (pyo3_bindings.rs) in R20.18.1 so the
//! kernel crate can own an `Arc<ZoneManager>` internally, reading env
//! vars at `Kernel::new()` time to bootstrap federation without any
//! PyO3 seam.
//!
//! Per v20.10 boundary rule: `ZoneManager` is kernel-internal and MUST
//! NOT be exposed to Python. The `PyZoneManager` wrapper in
//! pyo3_bindings.rs is transitional — R20.18.6 deletes it after Python
//! callers are cut over to syscalls in R20.18.5.

#![cfg(all(feature = "grpc", has_protos))]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;

#[allow(unused_imports)]
use crate::raft::StateMachine;
use crate::raft::{
    Command, CommandResult, FullStateMachine, RaftError, Result, ZoneConsensus, ZoneRaftRegistry,
};
use crate::transport::{
    call_join_cluster, hostname_to_node_id, NodeAddress, RaftGrpcServer, ServerConfig, TlsConfig,
};
use crate::zone_handle::ZoneHandle;

// ── Federation mount helpers ─────────────────────────────────────────

/// DirEntryType codes — must match proto/nexus/core/metadata.proto and
/// Python constants in `src/nexus/contracts/metadata.py`.
pub(crate) const DT_REG: i32 = 0;
pub(crate) const DT_DIR: i32 = 1;
pub(crate) const DT_MOUNT: i32 = 2;

/// Raft counter key for a zone's POSIX i_links_count.
pub(crate) const I_LINKS_COUNT_KEY: &str = "__i_links_count__";

/// Encode a minimal `FileMetadata` proto for federation mount writes.
pub(crate) fn encode_file_metadata(
    path: &str,
    entry_type: i32,
    zone_id: &str,
    target_zone_id: &str,
) -> Vec<u8> {
    use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
    use prost::Message;

    let proto = ProtoFileMetadata {
        path: path.to_string(),
        entry_type,
        zone_id: zone_id.to_string(),
        target_zone_id: target_zone_id.to_string(),
        ..Default::default()
    };
    proto.encode_to_vec()
}

/// Decode `FileMetadata` proto bytes.
pub(crate) fn decode_file_metadata(
    bytes: &[u8],
) -> std::result::Result<crate::transport::proto::nexus::core::FileMetadata, prost::DecodeError> {
    use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
    use prost::Message;
    ProtoFileMetadata::decode(bytes)
}

/// Is `path` either `normalized_prefix` or a descendant at `/` boundary?
pub(crate) fn path_matches_prefix(path: &str, normalized_prefix: &str) -> bool {
    if normalized_prefix.is_empty() {
        true
    } else {
        path == normalized_prefix || {
            path.len() > normalized_prefix.len()
                && path.starts_with(normalized_prefix)
                && path.as_bytes()[normalized_prefix.len()] == b'/'
        }
    }
}

fn propose_set_metadata(
    handle: &tokio::runtime::Handle,
    node: &ZoneConsensus<FullStateMachine>,
    key: &str,
    value: Vec<u8>,
) -> Result<()> {
    let cmd = Command::SetMetadata {
        key: key.to_string(),
        value,
    };
    match handle.block_on(node.propose(cmd))? {
        CommandResult::Success | CommandResult::Value(_) => Ok(()),
        CommandResult::Error(e) => Err(RaftError::Raft(e)),
        other => Err(RaftError::InvalidState(format!(
            "unexpected propose result: {:?}",
            other
        ))),
    }
}

fn propose_adjust_counter(
    handle: &tokio::runtime::Handle,
    node: &ZoneConsensus<FullStateMachine>,
    key: &str,
    delta: i64,
) -> Result<i64> {
    let cmd = Command::AdjustCounter {
        key: key.to_string(),
        delta,
    };
    match handle.block_on(node.propose(cmd))? {
        CommandResult::Value(bytes) => {
            let arr: [u8; 8] = bytes.try_into().map_err(|_| {
                RaftError::InvalidState("invalid counter value encoding".to_string())
            })?;
            Ok(i64::from_be_bytes(arr))
        }
        // Forwarded to leader over gRPC: RaftResponse drops the
        // counter bytes, so the apply's Value(new_count) comes back
        // as Success. The counter mutation did land in state; we
        // just don't know the new value.
        CommandResult::Success => Ok(i64::MIN),
        CommandResult::Error(e) => Err(RaftError::Raft(e)),
        other => Err(RaftError::InvalidState(format!(
            "unexpected counter result: {:?}",
            other
        ))),
    }
}

// ── ZoneManager ─────────────────────────────────────────────────────

/// Aggregate cluster status for one zone — flat dict fields.
/// Returned by [`ZoneManager::cluster_status`].
#[derive(Debug, Clone)]
pub struct ClusterStatus {
    pub zone_id: String,
    pub node_id: u64,
    pub has_store: bool,
    pub is_leader: bool,
    pub leader_id: u64,
    pub term: u64,
    pub commit_index: u64,
    pub applied_index: u64,
    pub voter_count: usize,
    pub witness_count: usize,
}

/// TLS configuration for a `ZoneManager` (all three fields required together).
#[derive(Debug, Clone)]
pub struct TlsFiles {
    pub cert_path: PathBuf,
    pub key_path: PathBuf,
    pub ca_path: PathBuf,
    /// CA private key read once at startup for server-side cert signing
    /// during `JoinCluster` RPC handling.
    pub ca_key_path: Option<PathBuf>,
    /// SHA-256 hash of the join token password for verifying
    /// incoming `JoinCluster` requests.
    pub join_token_hash: Option<String>,
}

/// Multi-zone raft registry owner (pure Rust, kernel-internal).
pub struct ZoneManager {
    registry: Arc<ZoneRaftRegistry>,
    runtime: tokio::runtime::Runtime,
    shutdown_tx: tokio::sync::Mutex<Option<tokio::sync::watch::Sender<bool>>>,
    node_id: u64,
    use_tls: bool,
    /// R20.18.3: remembered peer list (the `peers` arg from construction),
    /// in `id@host:port` form. Used when `get_or_create_zone` auto-creates
    /// a zone during `sys_setattr(DT_MOUNT)` — every zone in a federation
    /// shares the same raft peer topology, so the peer list is cluster-
    /// wide not per-zone.
    default_peers: Vec<String>,
    /// R20.18.7: late-bindable slot the kernel populates with a
    /// `BlobFetcher` once its root mount backend is ready. Shared with
    /// the gRPC server so `ZoneApiService::read_blob` serves once the
    /// kernel installs an impl. Stays empty on slim / no-federation
    /// runtimes (the RPC is still advertised but returns `NotFound`).
    blob_fetcher_slot: crate::blob_fetcher::BlobFetcherSlot,
    /// Static topology mounts staged by `bootstrap_static`, drained
    /// incrementally by `apply_topology` as parent + target zones'
    /// leaders settle. BTreeMap so parent paths process before children.
    /// Empty when no static topology is configured.
    pending_mounts: parking_lot::Mutex<BTreeMap<String, String>>,
}

impl ZoneManager {
    /// Create a new `ZoneManager`.
    ///
    /// Starts a tokio runtime + gRPC server. Enumerates + reopens every
    /// previously-persisted zone from disk before the gRPC server
    /// accepts traffic (R15.e).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        hostname: &str,
        base_path: &str,
        peers: Vec<String>,
        bind_addr: &str,
        tls: Option<TlsFiles>,
    ) -> Result<Arc<Self>> {
        let node_id = hostname_to_node_id(hostname);

        // Initialize tracing once.
        static TRACING_INIT: std::sync::Once = std::sync::Once::new();
        TRACING_INIT.call_once(|| {
            let _ = tracing_subscriber::fmt()
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("info".parse().unwrap()),
                )
                .try_init();
        });

        let tls_config = if let Some(ref t) = tls {
            let cert_pem = std::fs::read(&t.cert_path).map_err(|e| {
                RaftError::Config(format!(
                    "Failed to read TLS cert '{}': {}",
                    t.cert_path.display(),
                    e
                ))
            })?;
            let key_pem = std::fs::read(&t.key_path).map_err(|e| {
                RaftError::Config(format!(
                    "Failed to read TLS key '{}': {}",
                    t.key_path.display(),
                    e
                ))
            })?;
            let ca_pem = std::fs::read(&t.ca_path).map_err(|e| {
                RaftError::Config(format!(
                    "Failed to read TLS CA '{}': {}",
                    t.ca_path.display(),
                    e
                ))
            })?;
            Some(TlsConfig {
                cert_pem,
                key_pem,
                ca_pem,
            })
        } else {
            None
        };

        let bind_socket: std::net::SocketAddr = bind_addr.parse().map_err(|e| {
            RaftError::Config(format!("Invalid bind address '{}': {}", bind_addr, e))
        })?;

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-zone-mgr")
            .build()
            .map_err(|e| RaftError::Config(format!("Failed to create runtime: {}", e)))?;

        let registry = Arc::new(ZoneRaftRegistry::with_tls(
            PathBuf::from(base_path),
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

        // Enumerate + reopen local zone storage BEFORE gRPC accepts
        // traffic. Otherwise a vote/heartbeat arriving during restart
        // could silently drop messages or re-bootstrap with peers=0.
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), use_tls)
                    .map_err(|e| RaftError::Config(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<Result<Vec<_>>>()?;
        let enum_handle = runtime.handle().clone();
        let enum_registry = registry.clone();
        let enum_peers = peer_addrs.clone();
        runtime
            .handle()
            .block_on(async move {
                enum_registry
                    .open_existing_zones_from_disk(enum_peers, &enum_handle)
                    .await
            })
            .map_err(|e| RaftError::Raft(format!("Failed to enumerate zones on startup: {}", e)))?;

        let blob_fetcher_slot = crate::blob_fetcher::new_blob_fetcher_slot();
        let mut server = RaftGrpcServer::new(registry.clone(), config)
            .with_blob_fetcher_slot(blob_fetcher_slot.clone());
        // Configure JoinCluster RPC support if join token + CA key
        // are available — leader-side TLS signing for new joiners.
        if let (Some(ref t), Some(ref ca_key_path), Some(ref token_hash)) = (
            tls.as_ref(),
            tls.as_ref().and_then(|t| t.ca_key_path.as_ref()),
            tls.as_ref().and_then(|t| t.join_token_hash.as_ref()),
        ) {
            let _ = t; // silence unused warning; selective binding above
            let ca_key_pem = std::fs::read(ca_key_path).map_err(|e| {
                RaftError::Config(format!("Failed to read CA key for JoinCluster: {}", e))
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

        Ok(Arc::new(Self {
            registry,
            runtime,
            shutdown_tx: tokio::sync::Mutex::new(Some(shutdown_tx)),
            node_id,
            use_tls,
            default_peers: peers,
            blob_fetcher_slot,
            pending_mounts: parking_lot::Mutex::new(BTreeMap::new()),
        }))
    }

    /// R20.18.7: hand the shared `BlobFetcher` slot back to the kernel
    /// so it can install a concrete fetcher once its root mount backend
    /// is wired. Clone-cheap (just an `Arc`).
    pub fn blob_fetcher_slot(&self) -> crate::blob_fetcher::BlobFetcherSlot {
        self.blob_fetcher_slot.clone()
    }

    /// R20.18.3: cluster-wide peer list remembered from construction,
    /// in `id@host:port` form. Used by `sys_setattr(DT_MOUNT)`'s
    /// leader-side create-on-mount path so zone auto-creation picks
    /// up the federation's peer topology without re-parsing env vars.
    pub fn default_peers(&self) -> &[String] {
        &self.default_peers
    }

    /// R20.18.3: get an existing zone handle, or create one with the
    /// remembered `default_peers()` and return a handle to it.
    /// Called from `Kernel::sys_setattr(DT_MOUNT)` leader path so the
    /// caller doesn't have to specify peers (same federation = same
    /// peers). Idempotent: subsequent calls for an existing zone
    /// skip the raft ConfState bootstrap and return the cached node.
    pub fn get_or_create_zone(&self, zone_id: &str) -> Result<Arc<ZoneHandle>> {
        if let Some(h) = self.get_zone(zone_id) {
            return Ok(h);
        }
        self.create_zone(zone_id, self.default_peers.clone())
    }

    /// This node's ID.
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    /// Tokio runtime handle — used by `ZoneHandle` construction + apply
    /// helpers that need to `block_on` raft proposals.
    pub fn runtime_handle(&self) -> tokio::runtime::Handle {
        self.runtime.handle().clone()
    }

    /// The internal zone registry — kernel uses this for apply-cb
    /// installation (per v20.10 `install_federation_mount_coherence`).
    pub fn registry(&self) -> Arc<ZoneRaftRegistry> {
        self.registry.clone()
    }

    /// List all zone IDs loaded on this node.
    pub fn list_zones(&self) -> Vec<String> {
        self.registry.list_zones()
    }

    /// Create a new zone (raft group) on this node.
    pub fn create_zone(&self, zone_id: &str, peers: Vec<String>) -> Result<Arc<ZoneHandle>> {
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| RaftError::Config(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<Result<Vec<_>>>()?;

        let node = self
            .runtime
            .handle()
            .block_on(
                self.registry
                    .create_zone(zone_id, peer_addrs, self.runtime.handle()),
            )
            .map_err(|e| RaftError::Raft(format!("Failed to create zone: {}", e)))?;

        Ok(ZoneHandle::new(
            node,
            self.runtime.handle().clone(),
            zone_id.to_string(),
        ))
    }

    /// Join an existing zone as a new Voter.
    pub fn join_zone(&self, zone_id: &str, peers: Vec<String>) -> Result<Arc<ZoneHandle>> {
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| RaftError::Config(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<Result<Vec<_>>>()?;

        let node = self
            .runtime
            .handle()
            .block_on(
                self.registry
                    .join_zone(zone_id, peer_addrs, self.runtime.handle()),
            )
            .map_err(|e| RaftError::Raft(format!("Failed to join zone: {}", e)))?;

        Ok(ZoneHandle::new(
            node,
            self.runtime.handle().clone(),
            zone_id.to_string(),
        ))
    }

    /// Get an existing zone handle, or `None`.
    pub fn get_zone(&self, zone_id: &str) -> Option<Arc<ZoneHandle>> {
        self.registry
            .get_node(zone_id)
            .map(|node| ZoneHandle::new(node, self.runtime.handle().clone(), zone_id.to_string()))
    }

    /// Static Day-1 cluster formation: idempotently create raft groups
    /// for every zone in the federation, then stage `mounts` for the
    /// next `apply_topology()` pass.
    ///
    /// All nodes in the cluster call this with identical parameters
    /// during startup. Each node initializes its own raft state machine
    /// for every zone (no cross-node consensus required at this stage —
    /// raft-rs handles election once peers can reach each other).
    ///
    /// `mounts` maps a global path to its target zone id (e.g.
    /// `{"/corp": "corp", "/corp/eng": "corp-eng"}`). Storage is purely
    /// in-memory; mounts are applied lazily by `apply_topology()` once
    /// the relevant parent + target leaders are reachable. Calling
    /// `bootstrap_static` again replaces the pending set.
    ///
    /// Mirrors Python `ZoneManager.bootstrap_static`. Idempotent.
    pub fn bootstrap_static(
        &self,
        zones: &[String],
        peers: Vec<String>,
        mounts: &BTreeMap<String, String>,
    ) -> Result<()> {
        for zone_id in zones {
            if self.get_zone(zone_id).is_some() {
                tracing::debug!("Zone '{}' already exists, skipping", zone_id);
                continue;
            }
            self.create_zone(zone_id, peers.clone())?;
        }
        let mut pending = self.pending_mounts.lock();
        pending.clear();
        for (path, target) in mounts {
            pending.insert(path.clone(), target.clone());
        }
        Ok(())
    }

    /// Snapshot of mounts staged by `bootstrap_static` that have not
    /// yet been applied. Empty when topology has converged.
    pub fn pending_mounts(&self) -> BTreeMap<String, String> {
        self.pending_mounts.lock().clone()
    }

    /// Drive Day-1 topology toward convergence. Idempotent and
    /// crash-safe — every node calls this on a health-check tick;
    /// each writes only the zones for which it can reach the leader.
    /// Mirrors Python `ZoneManager._apply_topology` (the trickiest
    /// piece of the federation orchestrator).
    ///
    /// Steps:
    ///   1. Create the root "/" entry in `root_zone_id` if missing
    ///      (needs root-zone leader).
    ///   2. Walk pending mounts in path-depth order (parents first).
    ///      For each:
    ///        * Resolve the actual parent zone via longest-prefix
    ///          match against already-applied mounts (nested mount
    ///          handling: e.g. `/corp/eng` is owned by `corp`, not
    ///          root).
    ///        * Phase A: write DT_DIR + DT_MOUNT in the parent zone.
    ///          Skipped if a DT_MOUNT to the same target is already
    ///          present (idempotency).
    ///        * Phase B: bump the target zone's `i_links_count` to
    ///          reflect every pending mount referencing it.
    ///   3. Per-mount errors leave that mount in `pending_mounts` for
    ///      the next call. Only `Ok(true)` indicates full convergence.
    pub fn apply_topology(&self, root_zone_id: &str) -> Result<bool> {
        // Root "/" entry must exist before any mount path resolution.
        if !self.ensure_root_entry(root_zone_id)? {
            return Ok(false);
        }

        let snapshot = self.pending_mounts.lock().clone();
        if snapshot.is_empty() {
            return Ok(true);
        }

        // Sort by path depth so a parent mount lands before its children
        // (longest-prefix nested mount resolution depends on it).
        let mut sorted: Vec<(String, String)> = snapshot.into_iter().collect();
        sorted.sort_by_key(|(path, _)| path.matches('/').count());

        // Per-target expected link counts: every pending mount pointing
        // at the same zone increments its i_links_count once.
        let mut expected: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
        for (_, target) in &sorted {
            *expected.entry(target.clone()).or_insert(0) += 1;
        }

        let mut active: BTreeMap<String, String> = BTreeMap::new();
        let mut remaining: BTreeMap<String, String> = BTreeMap::new();

        for (global_path, target_zone) in &sorted {
            // Resolve the parent zone via longest-prefix match against
            // already-applied mounts. Falls back to root.
            let mut parent_zone = root_zone_id.to_string();
            let mut local_path = global_path.clone();
            for (mp, mz) in active.iter().rev() {
                if global_path.len() > mp.len()
                    && global_path.starts_with(mp.as_str())
                    && global_path.as_bytes()[mp.len()] == b'/'
                {
                    parent_zone = mz.clone();
                    local_path = global_path[mp.len()..].to_string();
                    break;
                }
            }

            // Phase A: DT_MOUNT in parent zone.
            if let Err(err) = self.write_mount_entry(&parent_zone, &local_path, target_zone) {
                tracing::debug!(
                    "Phase A deferred for {} (parent={} target={}): {}",
                    global_path,
                    parent_zone,
                    target_zone,
                    err
                );
                remaining.insert(global_path.clone(), target_zone.clone());
                // Still treat as active so deeper mounts route correctly
                // once Phase A lands on a later tick.
                active.insert(global_path.clone(), target_zone.clone());
                continue;
            }

            // Phase B: ensure target zone's i_links_count >= expected.
            let want = expected.get(target_zone).copied().unwrap_or(0);
            if let Err(err) = self.ensure_links_count(target_zone, want) {
                tracing::debug!(
                    "Phase B deferred for {} (target={}): {}",
                    global_path,
                    target_zone,
                    err
                );
                remaining.insert(global_path.clone(), target_zone.clone());
                active.insert(global_path.clone(), target_zone.clone());
                continue;
            }

            active.insert(global_path.clone(), target_zone.clone());
        }

        let total = sorted.len();
        let pending_after = remaining.len();
        *self.pending_mounts.lock() = remaining;

        if pending_after == 0 {
            tracing::info!(
                "Static topology applied: {} mounts via raft consensus",
                total
            );
            Ok(true)
        } else {
            tracing::info!(
                "Topology progress: {}/{} mounts applied, {} pending",
                total - pending_after,
                total,
                pending_after,
            );
            Ok(false)
        }
    }

    /// Ensure the root `/` DT_DIR exists in `root_zone_id`. Returns
    /// `Ok(false)` if this node is not the leader yet (caller should
    /// retry on the next tick); `Ok(true)` once present.
    fn ensure_root_entry(&self, root_zone_id: &str) -> Result<bool> {
        let Some(node) = self.registry.get_node(root_zone_id) else {
            return Ok(false);
        };
        let handle = self.runtime.handle().clone();

        let existing = handle
            .block_on(node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata("/")))
            .map_err(|e| RaftError::Raft(format!("get root metadata: {}", e)))?;
        if existing.is_some() {
            return Ok(true);
        }

        // Try to write — propose forwards to leader if we're a follower
        // and reachable; errors mean leader unreachable / not elected.
        let bytes = encode_file_metadata("/", "virtual", "", DT_DIR, root_zone_id, "");
        match propose_set_metadata(&handle, &node, "/", bytes) {
            Ok(()) => {
                tracing::info!("Root '/' created in zone '{}'", root_zone_id);
                Ok(true)
            }
            Err(err) => {
                tracing::debug!("Root '/' creation deferred: {}", err);
                Ok(false)
            }
        }
    }

    /// Write a DT_MOUNT entry at `local_path` in `parent_zone`. No-op
    /// if a DT_MOUNT to the same `target_zone` is already present.
    /// Also auto-creates a DT_DIR placeholder if `local_path` is
    /// missing (mkdir -p semantics, matches `mount()` behavior).
    fn write_mount_entry(
        &self,
        parent_zone: &str,
        local_path: &str,
        target_zone: &str,
    ) -> Result<()> {
        let parent_node = self.registry.get_node(parent_zone).ok_or_else(|| {
            RaftError::InvalidState(format!("Parent zone '{}' not found", parent_zone))
        })?;
        let handle = self.runtime.handle().clone();

        let existing = handle
            .block_on(
                parent_node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(local_path)),
            )
            .map_err(|e| RaftError::Raft(format!("get_metadata: {}", e)))?
            .map(|bytes| decode_file_metadata(&bytes))
            .transpose()
            .map_err(|e| RaftError::Raft(format!("decode existing: {}", e)))?;

        if let Some(ref meta) = existing {
            if meta.entry_type == DT_MOUNT && meta.target_zone_id == target_zone {
                return Ok(());
            }
        } else {
            let dir_bytes =
                encode_file_metadata(local_path, "virtual", "", DT_DIR, parent_zone, "");
            propose_set_metadata(&handle, &parent_node, local_path, dir_bytes)?;
        }

        let mount_bytes =
            encode_file_metadata(local_path, "mount", "", DT_MOUNT, parent_zone, target_zone);
        propose_set_metadata(&handle, &parent_node, local_path, mount_bytes)
    }

    /// Bump `target_zone`'s `i_links_count` if it is below `expected`.
    /// Idempotent: re-reads the counter after each propose so repeated
    /// calls converge without overshooting.
    fn ensure_links_count(&self, target_zone: &str, expected: i64) -> Result<()> {
        let target_node = self.registry.get_node(target_zone).ok_or_else(|| {
            RaftError::InvalidState(format!("Target zone '{}' not found", target_zone))
        })?;
        let handle = self.runtime.handle().clone();
        let key = I_LINKS_COUNT_KEY.to_string();

        loop {
            let bytes = handle
                .block_on({
                    let key = key.clone();
                    let node = target_node.clone();
                    async move {
                        node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(&key))
                            .await
                    }
                })
                .map_err(|e| RaftError::Raft(format!("get_metadata: {}", e)))?;
            let current = bytes
                .and_then(|b| <[u8; 8]>::try_from(b.as_slice()).ok())
                .map(i64::from_be_bytes)
                .unwrap_or(0);
            if current >= expected {
                return Ok(());
            }
            propose_adjust_counter(&handle, &target_node, I_LINKS_COUNT_KEY, expected - current)?;
            // Loop re-reads to confirm convergence (counter forwarding
            // returns Success without the new value, so we can't trust
            // the propose return).
        }
    }

    /// Read a zone's POSIX `i_links_count` (mount references).
    ///
    /// Returns `0` for zones that have never been mounted (key absent).
    /// Returns `Ok(None)` if the zone itself is not registered locally.
    pub fn get_links_count(&self, zone_id: &str) -> Result<Option<i64>> {
        let Some(node) = self.registry.get_node(zone_id) else {
            return Ok(None);
        };
        let key = I_LINKS_COUNT_KEY.to_string();
        let bytes = self.runtime.handle().block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(&key))
                .await
        })?;
        let count = bytes
            .and_then(|b| <[u8; 8]>::try_from(b.as_slice()).ok())
            .map(i64::from_be_bytes)
            .unwrap_or(0);
        Ok(Some(count))
    }

    /// Remove a zone — shut down transport loop, delete on-disk dir.
    ///
    /// POSIX semantics: when `force` is false, refuses removal while
    /// `i_links_count > 0` (i.e. the zone is still mounted somewhere).
    /// Mirrors Python `ZoneManager.remove_zone(force=...)`.
    pub fn remove_zone(&self, zone_id: &str, force: bool) -> Result<()> {
        if !force {
            if let Some(count) = self.get_links_count(zone_id)? {
                if count > 0 {
                    return Err(RaftError::InvalidState(format!(
                        "Zone '{}' still has {} reference(s) (i_links_count > 0); \
                         unmount all references first, or pass force=true.",
                        zone_id, count
                    )));
                }
            }
        }
        self.runtime
            .handle()
            .block_on(self.registry.remove_zone(zone_id))
            .map_err(|e| RaftError::Raft(format!("Failed to remove zone: {}", e)))
    }

    /// Peer roster for a zone: `(id, hostname, endpoint, is_witness)`.
    /// Empty list if zone unknown. Witness = hostname starts with
    /// `witness` (convention).
    pub fn zone_peers(&self, zone_id: &str) -> Vec<(u64, String, String, bool)> {
        match self.registry.get_peers(zone_id) {
            None => Vec::new(),
            Some(peers) => peers
                .into_values()
                .map(|p| {
                    let is_witness = p.hostname.to_ascii_lowercase().starts_with("witness");
                    (p.id, p.hostname, p.endpoint, is_witness)
                })
                .collect(),
        }
    }

    /// One-shot authoritative status snapshot for one zone. See
    /// `ClusterStatus` docs.
    pub fn cluster_status(&self, zone_id: &str) -> ClusterStatus {
        let Some(node) = self.registry.get_node(zone_id) else {
            return ClusterStatus {
                zone_id: zone_id.to_string(),
                node_id: self.node_id,
                has_store: false,
                is_leader: false,
                leader_id: 0,
                term: 0,
                commit_index: 0,
                applied_index: 0,
                voter_count: 0,
                witness_count: 0,
            };
        };
        let (mut voter_count, mut witness_count) = (0usize, 0usize);
        if let Some(peers) = self.registry.get_peers(zone_id) {
            for (_, p) in peers {
                if p.hostname.to_ascii_lowercase().starts_with("witness") {
                    witness_count += 1;
                } else {
                    voter_count += 1;
                }
            }
        }
        ClusterStatus {
            zone_id: zone_id.to_string(),
            node_id: self.node_id,
            has_store: true,
            is_leader: node.is_leader(),
            leader_id: node.leader_id().unwrap_or(0),
            term: node.term(),
            commit_index: node.commit_index(),
            applied_index: node.applied_index(),
            voter_count,
            witness_count,
        }
    }

    /// Find which zone stores metadata for `path`; `(zone_id, bytes)`.
    /// Iterates the live registry; used by federation join / nested
    /// mount resolution where a root-only lookup would miss.
    pub fn lookup_path(&self, path: &str) -> Result<Option<(String, Vec<u8>)>> {
        let registry = self.registry.clone();
        let path = path.to_string();
        self.runtime.handle().block_on(async move {
            for zone_id in registry.list_zones() {
                let Some(node) = registry.get_node(&zone_id) else {
                    continue;
                };
                let found = node
                    .with_state_machine(|sm: &FullStateMachine| sm.get_metadata(&path))
                    .await;
                match found {
                    Ok(Some(bytes)) => return Ok(Some((zone_id, bytes))),
                    Ok(None) => continue,
                    Err(e) => {
                        return Err(e);
                    }
                }
            }
            Ok(None)
        })
    }

    /// Mount target zone at `mount_path` inside parent zone (NFS-style).
    ///
    /// Writes a DT_MOUNT FileMetadata entry at `mount_path` in the
    /// parent zone's raft-replicated metastore, then bumps the target
    /// zone's `__i_links_count__`. Auto-creates a DT_DIR at
    /// `mount_path` if absent. Idempotent when already DT_MOUNT to the
    /// same target.
    pub fn mount(
        &self,
        parent_zone_id: &str,
        mount_path: &str,
        target_zone_id: &str,
        increment_links: bool,
    ) -> Result<()> {
        let parent_node = self.registry.get_node(parent_zone_id).ok_or_else(|| {
            RaftError::InvalidState(format!("Parent zone '{}' not found", parent_zone_id))
        })?;
        let target_node = self.registry.get_node(target_zone_id).ok_or_else(|| {
            RaftError::InvalidState(format!("Target zone '{}' not found", target_zone_id))
        })?;

        let handle = self.runtime.handle().clone();

        let existing = handle
            .block_on(
                parent_node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(mount_path)),
            )
            .map_err(|e| RaftError::Raft(format!("get_metadata: {}", e)))?
            .map(|bytes| decode_file_metadata(&bytes))
            .transpose()
            .map_err(|e| RaftError::Raft(format!("decode existing: {}", e)))?;

        if let Some(ref meta) = existing {
            if meta.entry_type == DT_MOUNT {
                if meta.target_zone_id == target_zone_id {
                    // Idempotent.
                    return Ok(());
                }
                return Err(RaftError::InvalidState(format!(
                    "Mount point '{}' is already a DT_MOUNT in zone '{}'. Unmount first.",
                    mount_path, parent_zone_id
                )));
            }
            if meta.entry_type != DT_DIR {
                return Err(RaftError::InvalidState(format!(
                    "Mount point '{}' is not a directory (type={}) in zone '{}'. \
                     Mount points must be directories.",
                    mount_path, meta.entry_type, parent_zone_id
                )));
            }
        } else {
            // Auto-create DT_DIR (mkdir -p semantics).
            let dir_bytes = encode_file_metadata(mount_path, DT_DIR, parent_zone_id, "");
            propose_set_metadata(&handle, &parent_node, mount_path, dir_bytes)?;
        }

        // Replace DT_DIR with DT_MOUNT (shadows original contents).
        let mount_bytes = encode_file_metadata(
            mount_path,
            DT_MOUNT,
            parent_zone_id,
            target_zone_id,
        );
        propose_set_metadata(&handle, &parent_node, mount_path, mount_bytes)?;

        if increment_links {
            propose_adjust_counter(&handle, &target_node, I_LINKS_COUNT_KEY, 1)?;
        }

        Ok(())
    }

    /// Remove a mount point, restoring DT_DIR. Returns the former
    /// target zone id so the caller can decrement remote links.
    pub fn unmount(&self, parent_zone_id: &str, mount_path: &str) -> Result<Option<String>> {
        let parent_node = self.registry.get_node(parent_zone_id).ok_or_else(|| {
            RaftError::InvalidState(format!("Parent zone '{}' not found", parent_zone_id))
        })?;
        let handle = self.runtime.handle().clone();
        let registry = self.registry.clone();

        let existing = handle
            .block_on(
                parent_node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(mount_path)),
            )
            .map_err(|e| RaftError::Raft(format!("get_metadata: {}", e)))?
            .map(|bytes| decode_file_metadata(&bytes))
            .transpose()
            .map_err(|e| RaftError::Raft(format!("decode existing: {}", e)))?;

        let existing = match existing {
            Some(m) if m.entry_type == DT_MOUNT => m,
            _ => {
                return Err(RaftError::InvalidState(format!(
                    "'{}' is not a mount point in zone '{}'",
                    mount_path, parent_zone_id
                )));
            }
        };

        let target_zone_id_opt: Option<String> = if existing.target_zone_id.is_empty() {
            None
        } else {
            Some(existing.target_zone_id.clone())
        };

        // Restore DT_DIR at the mount point.
        let dir_bytes = encode_file_metadata(mount_path, DT_DIR, parent_zone_id, "");
        propose_set_metadata(&handle, &parent_node, mount_path, dir_bytes)?;

        if let Some(ref target_id) = target_zone_id_opt {
            if let Some(target_node) = registry.get_node(target_id) {
                propose_adjust_counter(&handle, &target_node, I_LINKS_COUNT_KEY, -1)?;
            }
            // Target not locally hosted → remote leader's job.
        }

        Ok(target_zone_id_opt)
    }

    /// Copy every FileMetadata entry under `prefix` in `parent_zone_id`
    /// into `new_zone_id` with path rebased; bump `i_links_count` on
    /// every locally-hosted nested DT_MOUNT target. Returns count.
    pub fn share_subtree_core(
        &self,
        parent_zone_id: &str,
        prefix: &str,
        new_zone_id: &str,
    ) -> Result<usize> {
        let parent_node = self.registry.get_node(parent_zone_id).ok_or_else(|| {
            RaftError::InvalidState(format!("Parent zone '{}' not found", parent_zone_id))
        })?;
        let new_node = self.registry.get_node(new_zone_id).ok_or_else(|| {
            RaftError::InvalidState(format!(
                "Target zone '{}' not found (was create_zone called?)",
                new_zone_id
            ))
        })?;

        let normalized_prefix = prefix.trim_end_matches('/').to_string();
        if normalized_prefix.is_empty() && prefix != "/" {
            return Err(RaftError::InvalidState(format!(
                "share_subtree: empty prefix (got '{}')",
                prefix
            )));
        }

        let handle = self.runtime.handle().clone();
        let registry = self.registry.clone();

        let scan_prefix = if normalized_prefix.is_empty() {
            "/".to_string()
        } else {
            normalized_prefix.clone()
        };
        let entries = handle
            .block_on(
                parent_node.with_state_machine(move |sm: &FullStateMachine| {
                    sm.list_metadata(&scan_prefix)
                }),
            )
            .map_err(|e| RaftError::Raft(format!("list_metadata: {}", e)))?;

        let mut copied: usize = 0;
        let mut nested_mount_targets: Vec<String> = Vec::new();
        let mut root_written = false;

        for (path, value) in entries {
            if !path_matches_prefix(&path, &normalized_prefix) {
                continue;
            }
            let proto = match decode_file_metadata(&value) {
                Ok(p) => p,
                Err(e) => {
                    tracing::warn!(
                        zone = %parent_zone_id,
                        path = %path,
                        error = %e,
                        "share_subtree: skipping entry with undecodable FileMetadata",
                    );
                    continue;
                }
            };

            let (rebased_path, rebased_entry_type) = if path == normalized_prefix {
                root_written = true;
                ("/".to_string(), DT_DIR)
            } else {
                let mut relative = path[normalized_prefix.len()..].to_string();
                if !relative.starts_with('/') {
                    relative.insert(0, '/');
                }
                if proto.entry_type == DT_MOUNT && !proto.target_zone_id.is_empty() {
                    nested_mount_targets.push(proto.target_zone_id.clone());
                }
                (relative, proto.entry_type)
            };

            // Clone every existing field (etag / size / mime_type / timestamps /
            // permissions) so readers on the new zone can find the CAS blob
            // and serve reads. Only `path`, `zone_id`, and `entry_type` are
            // overridden for the rebased copy. The previous call went through
            // `encode_file_metadata` which took a six-arg subset and dropped
            // every other field — the shared file showed up in sys_stat but
            // had `etag=None`, so `try_remote_fetch` couldn't look up the
            // CAS hash and cross-node reads failed with "File not found".
            use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
            use prost::Message;
            let rebased_proto = ProtoFileMetadata {
                path: rebased_path.clone(),
                zone_id: new_zone_id.to_string(),
                entry_type: rebased_entry_type,
                ..proto
            };
            let rebased_bytes = rebased_proto.encode_to_vec();
            propose_set_metadata(&handle, &new_node, &rebased_path, rebased_bytes)?;
            copied += 1;
        }

        if !root_written {
            let root_bytes = encode_file_metadata("/", DT_DIR, new_zone_id, "");
            propose_set_metadata(&handle, &new_node, "/", root_bytes)?;
        }

        for target_id in &nested_mount_targets {
            if let Some(target_node) = registry.get_node(target_id) {
                propose_adjust_counter(&handle, &target_node, I_LINKS_COUNT_KEY, 1)?;
            }
        }

        Ok(copied)
    }

    // ── Shared-zone registry (R20.17a peer discovery) ─────────────────
    //
    // SSOT for federation share metadata lives in the root zone's raft
    // state machine, keyed under `SHARE_REGISTRY_PREFIX + origin_path`.
    // One `FileMetadata` row per shared subtree carries
    // `target_zone_id = new_zone_id`.  Because the root zone is raft-
    // replicated to every cluster member, a peer that wants to join a
    // shared zone just queries its **local** root-zone state — no
    // separate peer-discovery RPC needed.

    /// Canonical registry key for `origin_path`.
    fn share_registry_key(origin_path: &str) -> String {
        // Concatenate the reserved prefix with the absolute origin path.
        // Origin paths are VFS-global ("/corp/eng/shared-X") → result is
        // "/__shares__/corp/eng/shared-X". Empty / non-absolute inputs
        // fall back to prefix itself (caller should have validated).
        if origin_path.is_empty() || !origin_path.starts_with('/') {
            contracts::SHARE_REGISTRY_PREFIX.to_string()
        } else {
            format!("{}{}", contracts::SHARE_REGISTRY_PREFIX, origin_path)
        }
    }

    /// Register `origin_path → zone_id` in the root zone's share
    /// registry.  Must be called on the raft leader (propose forwards
    /// to leader on follower, so follower-side calls also work but
    /// incur one extra RPC hop).  Idempotent: re-registering the same
    /// pair is a no-op at steady state (raft log gets a duplicate
    /// SetMetadata, state machine settles to the same value).
    pub fn register_share(&self, origin_path: &str, zone_id: &str) -> Result<()> {
        let root = self
            .registry
            .get_node(contracts::ROOT_ZONE_ID)
            .ok_or_else(|| {
                RaftError::InvalidState(
                    "root zone not found — share registry requires a live root raft group"
                        .to_string(),
                )
            })?;
        let key = Self::share_registry_key(origin_path);
        // Encode as a DT_REG FileMetadata; target_zone_id carries the
        // advertised zone. backend_name is a marker that tells readers
        // "this is a share registry row, not a user inode".
        let value =
            encode_file_metadata(&key, DT_REG, contracts::ROOT_ZONE_ID, zone_id);
        let handle = self.runtime.handle().clone();
        propose_set_metadata(&handle, &root, &key, value)
    }

    /// Resolve `origin_path` back to the zone id a peer shared it as.
    /// Returns `None` when no share has been registered for that path
    /// (caller treats as "zone not advertised").
    pub fn lookup_share(&self, origin_path: &str) -> Result<Option<String>> {
        let root = self
            .registry
            .get_node(contracts::ROOT_ZONE_ID)
            .ok_or_else(|| {
                RaftError::InvalidState(
                    "root zone not found — share registry requires a live root raft group"
                        .to_string(),
                )
            })?;
        let key = Self::share_registry_key(origin_path);
        let handle = self.runtime.handle().clone();
        let bytes = handle
            .block_on(root.with_state_machine(move |sm: &FullStateMachine| sm.get_metadata(&key)))
            .map_err(|e| RaftError::Raft(format!("lookup_share: {}", e)))?;
        let Some(bytes) = bytes else {
            return Ok(None);
        };
        let proto = decode_file_metadata(&bytes).map_err(|e| {
            RaftError::InvalidState(format!("share registry row decode failed: {}", e))
        })?;
        if proto.target_zone_id.is_empty() {
            Ok(None)
        } else {
            Ok(Some(proto.target_zone_id))
        }
    }

    /// Gracefully shut down all zones and the gRPC server.
    pub fn shutdown(&self) {
        self.registry.shutdown_all();
        if let Some(tx) = self
            .runtime
            .block_on(async { self.shutdown_tx.lock().await.take() })
        {
            let _ = tx.send(true);
        }
        tracing::info!("ZoneManager node {} shut down", self.node_id);
    }
}

impl Drop for ZoneManager {
    fn drop(&mut self) {
        self.registry.shutdown_all();
        // Best-effort shutdown signal.
        if let Ok(mut guard) = self.shutdown_tx.try_lock() {
            if let Some(tx) = guard.take() {
                let _ = tx.send(true);
            }
        }
    }
}

// ── Join-cluster helper (pre-ZoneManager-exists TLS bootstrap) ────────

/// K3s-style join: connect to leader with TOFU TLS, verify CA
/// fingerprint from the join token, write `ca.pem` / `node.pem` /
/// `node-key.pem` into `tls_dir`. Called BEFORE ZoneManager exists.
pub fn join_cluster_and_provision_tls(
    peer_address: &str,
    join_token: &str,
    hostname: &str,
    tls_dir: &str,
) -> Result<()> {
    let node_id = hostname_to_node_id(hostname);

    let token_prefix = "K10";
    let separator = "::server:";
    if !join_token.starts_with(token_prefix) {
        return Err(RaftError::Config(
            "Invalid join token: must start with 'K10'".to_string(),
        ));
    }
    let body = &join_token[token_prefix.len()..];
    let sep_pos = body.find(separator).ok_or_else(|| {
        RaftError::Config("Invalid join token: missing '::server:' separator".to_string())
    })?;
    let password = &body[..sep_pos];
    let expected_fingerprint = &body[sep_pos + separator.len()..];

    if password.is_empty() {
        return Err(RaftError::Config(
            "Invalid join token: empty password".to_string(),
        ));
    }
    if !expected_fingerprint.starts_with("SHA256:") {
        return Err(RaftError::Config(
            "Invalid join token: fingerprint must start with 'SHA256:'".to_string(),
        ));
    }

    let endpoint = if peer_address.starts_with("http") {
        peer_address.to_string()
    } else {
        format!("http://{}", peer_address)
    };

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| RaftError::Config(format!("Failed to create runtime: {}", e)))?;

    let result = runtime
        .block_on(call_join_cluster(
            &endpoint, node_id, "", "root", password, 30,
        ))
        .map_err(|e| RaftError::Raft(format!("JoinCluster RPC failed: {}", e)))?;

    let ca_fingerprint = crate::transport::certgen::ca_fingerprint_from_pem(&result.ca_pem)
        .map_err(|e| RaftError::Raft(format!("Failed to compute CA fingerprint: {}", e)))?;
    if ca_fingerprint != expected_fingerprint {
        return Err(RaftError::Raft(format!(
            "CA fingerprint mismatch: expected '{}', got '{}'",
            expected_fingerprint, ca_fingerprint
        )));
    }

    let dir = std::path::Path::new(tls_dir);
    std::fs::create_dir_all(dir)
        .map_err(|e| RaftError::Config(format!("Failed to create TLS dir: {}", e)))?;

    std::fs::write(dir.join("ca.pem"), &result.ca_pem)
        .map_err(|e| RaftError::Config(format!("Failed to write ca.pem: {}", e)))?;
    std::fs::write(dir.join("node.pem"), &result.node_cert_pem)
        .map_err(|e| RaftError::Config(format!("Failed to write node.pem: {}", e)))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true).mode(0o600);
        use std::io::Write;
        let mut f = opts
            .open(dir.join("node-key.pem"))
            .map_err(|e| RaftError::Config(format!("Failed to write node-key.pem: {}", e)))?;
        f.write_all(&result.node_key_pem)
            .map_err(|e| RaftError::Config(format!("Failed to write node-key.pem: {}", e)))?;
    }
    #[cfg(not(unix))]
    {
        std::fs::write(dir.join("node-key.pem"), &result.node_key_pem)
            .map_err(|e| RaftError::Config(format!("Failed to write node-key.pem: {}", e)))?;
    }

    Ok(())
}
