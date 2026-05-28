//! Pure-Rust `ZoneManager` — multi-zone raft registry owner.
//!
//! Kernel-internal: the kernel crate owns an `Arc<ZoneManager>` and
//! reads env vars at `Kernel::new()` time to bootstrap federation
//! without any PyO3 seam. Never exposed to Python.

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
    call_delete_zone, call_join_cluster, hostname_to_node_id, NodeAddress, RaftGrpcServer,
    ServerConfig, TlsConfig,
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

/// Sync façade bridge to the inner runtime's async work.
///
/// `ZoneManager` exposes a sync API to its callers (PyO3 wrappers,
/// `nexusd-cluster`, `federation_server` daemon, kernel) and owns an
/// inner tokio `Runtime` that drives the raft `transport_loop`,
/// driver tasks, tonic gRPC server / clients, and `spawn_blocking`
/// redb I/O — all of which are `async fn` because tonic + tokio
/// `select!` make raft transport an async task by construction.
/// Bridging the sync façade to that async core requires `block_on`
/// on the inner runtime's handle. Two callsite shapes coexist:
///
/// * **Sync caller** (PyO3 main thread; binary `fn main()` before
///   it spawns its runtime): no outer tokio context. `Handle::block_on`
///   parks the calling thread on the inner runtime — straight
///   forward, no panic.
/// * **Async caller** (anything reachable from `#[tokio::main]` or
///   inside `tokio::spawn` — `nexusd-cluster::run_daemon`'s topology
///   loop, `federation_server::main`, `distributed_coordinator` async
///   helpers): the calling thread is registered as a worker of an
///   *outer* runtime. `Handle::block_on` panics on a worker thread
///   (tokio refuses to park a worker because it would deadlock when
///   the awaited future depends on a task that needs the same worker
///   pool). `tokio::task::block_in_place` releases the worker
///   temporarily — work-stealing covers in its absence — so we can
///   safely `block_on` the inner runtime within the closure.
///
/// `Handle::try_current()` resolves the two cases at runtime;
/// `block_in_place` requires a `multi_thread` outer runtime, which
/// every async caller of `ZoneManager` already uses
/// (`#[tokio::main(flavor = "multi_thread")]`).
fn bridge_block_on<F>(handle: &tokio::runtime::Handle, fut: F) -> F::Output
where
    F: std::future::Future,
{
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::task::block_in_place(|| handle.block_on(fut))
    } else {
        handle.block_on(fut)
    }
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
    match bridge_block_on(handle, node.propose(cmd))? {
        CommandResult::Success | CommandResult::Value(_) => Ok(()),
        CommandResult::Error(e) => Err(RaftError::Raft(e)),
        other => Err(RaftError::InvalidState(format!(
            "unexpected propose result: {:?}",
            other
        ))),
    }
}

pub(crate) fn propose_delete_metadata(
    handle: &tokio::runtime::Handle,
    node: &ZoneConsensus<FullStateMachine>,
    key: &str,
) -> Result<()> {
    let cmd = Command::DeleteMetadata {
        key: key.to_string(),
    };
    match bridge_block_on(handle, node.propose(cmd))? {
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
    match bridge_block_on(handle, node.propose(cmd))? {
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
    /// `Option` solely to support `Drop` taking ownership and switching
    /// to `Runtime::shutdown_background()` when the surrounding caller
    /// is itself running inside a tokio runtime — naive drop of a
    /// `Runtime` from an async context panics ("Cannot drop a runtime
    /// in a context where blocking is not allowed"). Always `Some`
    /// for the lifetime of the `ZoneManager`; only `take`n in `Drop`.
    runtime: Option<tokio::runtime::Runtime>,
    shutdown_tx: tokio::sync::Mutex<Option<tokio::sync::watch::Sender<bool>>>,
    node_id: u64,
    use_tls: bool,
    /// Remembered peer list (the `peers` arg from construction), in
    /// `id@host:port` form. Used when `get_or_create_zone` auto-creates
    /// a zone during `sys_setattr(DT_MOUNT)` — every zone in a
    /// federation shares the same raft peer topology, so the peer
    /// list is cluster-wide, not per-zone.
    default_peers: Vec<String>,
    /// Late-bindable slot the kernel populates with a `BlobFetcher`
    /// once its root mount backend is ready. Shared with the gRPC
    /// server so `ZoneApiService::read_blob` serves once the kernel
    /// installs an impl. Stays empty on slim / no-federation runtimes
    /// (the RPC is still advertised but returns `NotFound`).
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
        Self::with_node_id(
            hostname,
            hostname_to_node_id(hostname),
            base_path,
            peers,
            bind_addr,
            tls,
            None,
            None,
        )
    }

    /// Create a `ZoneManager` with an explicit node ID.
    ///
    /// Used by `RaftDistributedCoordinator::init_from_env` to bind the
    /// node ID returned by `read_or_mint_node_id` (opaque random u64
    /// persisted at `<NEXUS_DATA_DIR>/.node_id`).  `peers` is the
    /// hostname → endpoint **address book** parsed from `NEXUS_PEERS`;
    /// it seeds the transport peer map for raft messaging but is **not**
    /// the source of truth for ConfState — ConfState is mutated only
    /// by ConfChange (AddNode / RemoveNode) driven by JoinZone.  The
    /// witness binary path uses [`Self::new`] which derives the ID
    /// from hostname (witnesses don't wipe-rejoin).
    /// `self_address` is this node's advertise address (e.g.,
    /// `"http://10.0.0.3:2126"` or `"10.0.0.3:2126"`).  Carried in every
    /// outbound `StepMessageRequest.sender_address` so peers learn
    /// `(self.node_id -> self_address)` from inbound messages — the
    /// runtime SSOT for transport routing under the opaque-ID contract.
    /// `None` disables sender-address advertisement; use only for
    /// tests / sync-only deployments where peers already have the
    /// address via env seeding.
    #[allow(clippy::too_many_arguments)]
    pub fn with_node_id(
        hostname: &str,
        node_id: u64,
        base_path: &str,
        peers: Vec<String>,
        bind_addr: &str,
        tls: Option<TlsFiles>,
        self_address: Option<String>,
        extra_grpc_services: Option<tonic::service::Routes>,
    ) -> Result<Arc<Self>> {
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

        // This runtime hosts BOTH the tonic gRPC server (accept loop +
        // per-connection HTTP/2 handshakes for raft and VFS) and every
        // zone's `transport_loop` (which does synchronous redb disk I/O
        // in `advance`). A hardcoded 2-worker pool starves the
        // accept/handshake path under load — new connections finish the
        // TCP handshake but never get an HTTP/2 SETTINGS frame, so peers
        // see most join/replication dials time out while heartbeats on
        // established connections survive. Size to the host like the
        // outer daemon runtime, with a floor that keeps the accept path
        // live even on small multi-zone hosts.
        let worker_threads =
            contracts::recommended_worker_threads(contracts::MIN_SERVER_RUNTIME_WORKERS);
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(worker_threads)
            .enable_all()
            .thread_name("nexus-zone-mgr")
            .build()
            .map_err(|e| RaftError::Config(format!("Failed to create runtime: {}", e)))?;

        let registry = Arc::new(ZoneRaftRegistry::with_tls(
            PathBuf::from(base_path),
            node_id,
            tls_config.clone(),
        ));
        if let Some(ref addr) = self_address {
            registry.set_self_address(addr.clone());
        }

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
        // Disk → in-memory zone re-hydration is sync now (no campaign,
        // no nested runtime). Calling it directly avoids the
        // `Handle::block_on` panic that hit `nexusd-cluster`'s
        // `#[tokio::main]` async path — the worker thread driving
        // `ZoneManager::new` would otherwise be parked on the inner
        // runtime's block_on, which tokio rejects to prevent deadlock.
        registry
            .open_existing_zones_from_disk(peer_addrs.clone(), runtime.handle())
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
        if let Some(extra) = extra_grpc_services {
            server = server.with_extra_services(extra);
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
            "ZoneManager node {} hostname={} started (bind={}, tls={})",
            node_id,
            hostname,
            bind_addr,
            use_tls,
        );

        Ok(Arc::new(Self {
            registry,
            runtime: Some(runtime),
            shutdown_tx: tokio::sync::Mutex::new(Some(shutdown_tx)),
            node_id,
            use_tls,
            default_peers: peers,
            blob_fetcher_slot,
            pending_mounts: parking_lot::Mutex::new(BTreeMap::new()),
        }))
    }

    /// Hand the shared `BlobFetcher` slot back to the kernel so it
    /// can install a concrete fetcher once its root mount backend is
    /// wired. Clone-cheap (just an `Arc`).
    pub fn blob_fetcher_slot(&self) -> crate::blob_fetcher::BlobFetcherSlot {
        self.blob_fetcher_slot.clone()
    }

    /// Cluster-wide peer list remembered from construction, in
    /// `id@host:port` form. Used by `sys_setattr(DT_MOUNT)`'s
    /// leader-side create-on-mount path so zone auto-creation picks
    /// up the federation's peer topology without re-parsing env vars.
    pub fn default_peers(&self) -> &[String] {
        &self.default_peers
    }

    /// Current cluster-wide peer roster in `id@host:port` form.
    ///
    /// The env-derived `default_peers` are only the cold-start seed. Once
    /// root membership changes through wipe-rejoin rotation, root's live
    /// peer map is the authoritative address roster for newly-created
    /// zones.
    pub fn current_peer_strings(&self) -> Vec<String> {
        if let Some(peers) = self.registry.get_peers("root") {
            if !peers.is_empty() {
                let mut out: Vec<String> =
                    peers.values().map(NodeAddress::to_raft_peer_str).collect();
                out.sort();
                return out;
            }
        }
        self.default_peers.clone()
    }

    /// Get an existing zone handle, or create one with the current
    /// root-zone peer roster and return a handle to it. Called from
    /// `Kernel::sys_setattr(DT_MOUNT)` leader path so the caller
    /// doesn't have to specify peers (same federation = same peers).
    /// Idempotent: subsequent calls for an existing zone skip the
    /// raft ConfState bootstrap and return the cached node.
    pub fn get_or_create_zone(&self, zone_id: &str) -> Result<Arc<ZoneHandle>> {
        if let Some(h) = self.get_zone(zone_id) {
            return Ok(h);
        }
        self.create_zone(zone_id, self.current_peer_strings())
    }

    /// This node's ID.
    pub fn node_id(&self) -> u64 {
        self.node_id
    }

    /// Tokio runtime handle — used by `ZoneHandle` construction + apply
    /// helpers that need to `block_on` raft proposals.
    pub fn runtime_handle(&self) -> tokio::runtime::Handle {
        self.rt().handle().clone()
    }

    /// Internal runtime accessor. The field is `Option` solely so
    /// `Drop` can take ownership and switch to `shutdown_background()`
    /// when the caller's thread is itself running on a tokio runtime;
    /// the option is `Some` for the entire lifetime up to that point,
    /// so this `expect` is unreachable in practice.
    fn rt(&self) -> &tokio::runtime::Runtime {
        self.runtime.as_ref().expect("runtime present until Drop")
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
    ///
    /// Idempotent — calling `create_zone` for an existing zone with
    /// the **same** address book (same set of `(hostname, port)`
    /// tuples) returns the existing handle.  A **different** address
    /// book returns
    /// [`RaftError::ZoneAlreadyExistsWithDifferentMembership`] so
    /// operator config drift surfaces loudly instead of silently
    /// re-bootstrapping the local replica.  The address book carries
    /// no role in ConfState (which is mutated only by ConfChange via
    /// JoinZone) so two callers passing equivalent peer lists are
    /// genuinely equivalent.
    pub fn create_zone(&self, zone_id: &str, peers: Vec<String>) -> Result<Arc<ZoneHandle>> {
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| RaftError::Config(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<Result<Vec<_>>>()?;

        // Idempotency: zone already loaded.  Compare existing
        // address-book entries by `(hostname, port)` ignoring the
        // raft-id prefix — under the opaque-ID contract two parses of
        // the same NEXUS_PEERS entry can carry different witness-derived
        // ids but represent the same physical peer.
        if self.get_zone(zone_id).is_some() {
            let existing = self
                .registry
                .get_peers(zone_id)
                .map(|map| {
                    let mut set: std::collections::BTreeSet<(String, u16)> = map
                        .values()
                        .map(|addr| (addr.hostname.clone(), addr.port))
                        .collect();
                    set.remove(&(String::new(), 0));
                    set
                })
                .unwrap_or_default();
            let requested: std::collections::BTreeSet<(String, u16)> = peer_addrs
                .iter()
                .map(|a| (a.hostname.clone(), a.port))
                .collect();
            if existing == requested {
                return Ok(self.get_zone(zone_id).expect("re-checked above"));
            }
            let to_strs = |set: &std::collections::BTreeSet<(String, u16)>| -> Vec<String> {
                set.iter().map(|(h, p)| format!("{h}:{p}")).collect()
            };
            return Err(RaftError::ZoneAlreadyExistsWithDifferentMembership {
                actual: to_strs(&existing),
                requested: to_strs(&requested),
            });
        }

        let node = self
            .registry
            .create_zone(zone_id, peer_addrs, self.rt().handle())
            .map_err(|e| RaftError::Raft(format!("Failed to create zone: {}", e)))?;

        Ok(ZoneHandle::new(
            node,
            self.rt().handle().clone(),
            zone_id.to_string(),
        ))
    }

    /// Join an existing zone as a Voter (learner=false) or Learner (learner=true).
    ///
    /// Sets up the local Raft state machine with skip_bootstrap=true so the
    /// leader's snapshot installs the correct ConfState. The caller is
    /// responsible for sending a JoinZone RPC to the leader (via
    /// PyFederationClient::request_join_zone) with the same learner flag so
    /// the leader proposes AddNode vs AddLearnerNode accordingly.
    pub fn join_zone(
        &self,
        zone_id: &str,
        peers: Vec<String>,
        learner: bool,
    ) -> Result<Arc<ZoneHandle>> {
        let peer_addrs: Vec<NodeAddress> = peers
            .iter()
            .map(|s| {
                NodeAddress::parse(s.trim(), self.use_tls)
                    .map_err(|e| RaftError::Config(format!("Invalid peer '{}': {}", s, e)))
            })
            .collect::<Result<Vec<_>>>()?;

        let node = self
            .registry
            .join_zone(zone_id, peer_addrs, learner, self.rt().handle())
            .map_err(|e| RaftError::Raft(format!("Failed to join zone: {}", e)))?;

        Ok(ZoneHandle::new(
            node,
            self.rt().handle().clone(),
            zone_id.to_string(),
        ))
    }

    /// Get an existing zone handle, or `None`.
    pub fn get_zone(&self, zone_id: &str) -> Option<Arc<ZoneHandle>> {
        self.registry
            .get_node(zone_id)
            .map(|node| ZoneHandle::new(node, self.rt().handle().clone(), zone_id.to_string()))
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
    ///        * Step 1 — DT_MOUNT write: write DT_DIR + DT_MOUNT in
    ///          the parent zone. Skipped if a DT_MOUNT to the same
    ///          target is already present (idempotency).
    ///        * Step 2 — link bump: bump the target zone's
    ///          `i_links_count` to reflect every pending mount
    ///          referencing it.
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

            // Step 1: DT_MOUNT in parent zone.
            if let Err(err) = self.write_mount_entry(&parent_zone, &local_path, target_zone) {
                tracing::debug!(
                    "DT_MOUNT write deferred for {} (parent={} target={}): {}",
                    global_path,
                    parent_zone,
                    target_zone,
                    err
                );
                remaining.insert(global_path.clone(), target_zone.clone());
                // Still treat as active so deeper mounts route correctly
                // once the DT_MOUNT write lands on a later tick.
                active.insert(global_path.clone(), target_zone.clone());
                continue;
            }

            // Step 2: ensure target zone's i_links_count >= expected.
            let want = expected.get(target_zone).copied().unwrap_or(0);
            if let Err(err) = self.ensure_links_count(target_zone, want) {
                tracing::debug!(
                    "link-count bump deferred for {} (target={}): {}",
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
        let handle = self.rt().handle().clone();

        let existing = bridge_block_on(
            &handle,
            node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata("/")),
        )
        .map_err(|e| RaftError::Raft(format!("get root metadata: {}", e)))?;
        if existing.is_some() {
            return Ok(true);
        }

        // Try to write — propose forwards to leader if we're a follower
        // and reachable; errors mean leader unreachable / not elected.
        let bytes = encode_file_metadata("/", DT_DIR, root_zone_id, "");
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
        let handle = self.rt().handle().clone();

        let existing = bridge_block_on(
            &handle,
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
            let dir_bytes = encode_file_metadata(local_path, DT_DIR, parent_zone, "");
            propose_set_metadata(&handle, &parent_node, local_path, dir_bytes)?;
        }

        let mount_bytes = encode_file_metadata(local_path, DT_MOUNT, parent_zone, target_zone);
        propose_set_metadata(&handle, &parent_node, local_path, mount_bytes)
    }

    /// Bump `target_zone`'s `i_links_count` if it is below `expected`.
    /// Idempotent: re-reads the counter after each propose so repeated
    /// calls converge without overshooting.
    fn ensure_links_count(&self, target_zone: &str, expected: i64) -> Result<()> {
        let target_node = self.registry.get_node(target_zone).ok_or_else(|| {
            RaftError::InvalidState(format!("Target zone '{}' not found", target_zone))
        })?;
        let handle = self.rt().handle().clone();
        let key = I_LINKS_COUNT_KEY.to_string();

        loop {
            let bytes = bridge_block_on(&handle, {
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
        let bytes = bridge_block_on(self.rt().handle(), async move {
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

        let peers = self.registry.get_peers(zone_id).unwrap_or_default();
        let self_id = self.registry.node_id();
        bridge_block_on(self.rt().handle(), async {
            for (peer_id, peer) in peers {
                if peer_id == self_id || peer.hostname.to_ascii_lowercase().starts_with("witness") {
                    continue;
                }
                call_delete_zone(&peer.endpoint, zone_id, force, 10)
                    .await
                    .map_err(|e| {
                        RaftError::Raft(format!(
                            "Failed to remove zone '{zone_id}' from peer {}: {e}",
                            peer.endpoint
                        ))
                    })?;
            }
            Ok::<(), RaftError>(())
        })?;

        bridge_block_on(self.rt().handle(), self.registry.remove_zone(zone_id))
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
        bridge_block_on(self.rt().handle(), async move {
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

        let handle = self.rt().handle().clone();

        let existing = bridge_block_on(
            &handle,
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
        let mount_bytes =
            encode_file_metadata(mount_path, DT_MOUNT, parent_zone_id, target_zone_id);
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
        let handle = self.rt().handle().clone();
        let registry = self.registry.clone();

        let existing = bridge_block_on(
            &handle,
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

        // share_subtree_core writes (`propose_set_metadata`) target
        // ``new_node``, which is leader-required.  ``parent_node`` is
        // only read via ``with_state_machine`` (sequential-consistency
        // local read — no leader needed).  Offline callers (e.g.
        // ``nexusd-cluster share``) hit this immediately after
        // ``create_zone``: the new 1-voter raft tick driver has not
        // yet run its election cycle, so the first propose lands
        // before ``cached_role`` is set to ``LEADER`` and fails with
        // ``NotLeader { leader_hint: Some(self_id) }``.  Wait once,
        // here, so every caller (offline CLI, online RPC, future
        // binding) is safe by construction — single SSOT for this
        // precondition rather than per-caller defensive waits.
        //
        // 10 s covers ~60 election windows; a 1-voter zone that has
        // not elected itself within that timeframe means the raft
        // tick driver did not start, which is a fatal bug worth
        // failing loud about.
        if !new_node.wait_for_leader(std::time::Duration::from_secs(10)) {
            return Err(RaftError::Raft(format!(
                "share_subtree_core: new zone '{}' did not elect self as leader \
                 within 10s (leader hint: {:?}); raft tick driver may not be running",
                new_zone_id,
                new_node.leader_id(),
            )));
        }

        let normalized_prefix = prefix.trim_end_matches('/').to_string();
        if normalized_prefix.is_empty() && prefix != "/" {
            return Err(RaftError::InvalidState(format!(
                "share_subtree: empty prefix (got '{}')",
                prefix
            )));
        }

        let handle = self.rt().handle().clone();
        let registry = self.registry.clone();

        let scan_prefix = if normalized_prefix.is_empty() {
            "/".to_string()
        } else {
            normalized_prefix.clone()
        };
        let entries = bridge_block_on(
            &handle,
            parent_node
                .with_state_machine(move |sm: &FullStateMachine| sm.list_metadata(&scan_prefix)),
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

            // Clone every existing field (content_id / size / mime_type / timestamps /
            // permissions) so readers on the new zone can find the CAS blob
            // and serve reads. Only `path`, `zone_id`, and `entry_type` are
            // overridden for the rebased copy. The previous call went through
            // `encode_file_metadata` which took a six-arg subset and dropped
            // every other field — the shared file showed up in sys_stat but
            // had `content_id=None`, so `try_remote_fetch` couldn't look up the
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

    // ── Shared-zone registry (peer discovery) ────────────────────────
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
        let value = encode_file_metadata(&key, DT_REG, contracts::ROOT_ZONE_ID, zone_id);
        let handle = self.rt().handle().clone();
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
        let handle = self.rt().handle().clone();
        let bytes = bridge_block_on(
            &handle,
            root.with_state_machine(move |sm: &FullStateMachine| sm.get_metadata(&key)),
        )
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
        if let Some(tx) = bridge_block_on(self.rt().handle(), async {
            self.shutdown_tx.lock().await.take()
        }) {
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
        // Drop the inner runtime non-blockingly when we're sitting
        // inside another tokio runtime: a naive `Runtime` drop blocks
        // until every spawned task exits, and tokio refuses to block
        // on a worker thread ("Cannot drop a runtime in a context
        // where blocking is not allowed"). `shutdown_background`
        // schedules cleanup off-thread; the alternative `drop` path
        // (sync caller, no outer runtime) is the natural blocking
        // shutdown.
        if let Some(rt) = self.runtime.take() {
            if tokio::runtime::Handle::try_current().is_ok() {
                rt.shutdown_background();
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

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    /// Reusable ZoneManager fixture binding to an OS-allocated port so
    /// parallel tests don't collide.  Tests must drop the returned
    /// TempDir last so the on-disk redb is unlinked after the manager
    /// shuts its runtime down.
    fn make_zm(node_id: u64) -> (Arc<ZoneManager>, TempDir) {
        let dir = TempDir::new().expect("tempdir");
        let zm = ZoneManager::with_node_id(
            "test-host",
            node_id,
            dir.path().to_str().expect("utf-8"),
            vec![],
            "127.0.0.1:0",
            None,
            None,
            None,
        )
        .expect("ZoneManager");
        (zm, dir)
    }

    #[test]
    fn test_create_zone_repeated_same_peers_is_noop() {
        // Calling create_zone twice with the same address book must be
        // idempotent — second call returns the existing handle, does
        // NOT re-bootstrap or shadow the original raft group.
        let (zm, _dir) = make_zm(1);
        let peers = vec!["nexus-2:2126".to_string(), "nexus-3:2126".to_string()];
        let h1 = zm.create_zone("z1", peers.clone()).expect("first create");
        let h2 = zm
            .create_zone("z1", peers)
            .expect("second create idempotent");
        assert_eq!(h1.zone_id(), h2.zone_id());
    }

    #[test]
    fn test_create_zone_with_different_peers_errors() {
        // Calling create_zone twice with a different address book is
        // an operator-config error — surface it explicitly so config
        // drift doesn't silently mutate ConfState.
        let (zm, _dir) = make_zm(1);
        zm.create_zone("z1", vec!["nexus-2:2126".to_string()])
            .expect("first create");
        let result = zm.create_zone(
            "z1",
            vec!["nexus-2:2126".to_string(), "nexus-3:2126".to_string()],
        );
        match result {
            Err(RaftError::ZoneAlreadyExistsWithDifferentMembership { .. }) => {}
            Err(other) => {
                panic!("expected ZoneAlreadyExistsWithDifferentMembership, got {other:?}")
            }
            Ok(_) => panic!("expected ZoneAlreadyExistsWithDifferentMembership, got Ok"),
        }
    }

    #[test]
    fn test_share_subtree_core_after_fresh_create_no_notleader_race() {
        // Regression: ``share_subtree_core`` writes (``propose_set_metadata``)
        // target the freshly-created ``new_zone`` raft group.  Before the
        // SSOT wait moved into the helper, callers had to ``wait_for_leader``
        // themselves between ``create_zone`` and ``share_subtree_core``;
        // forgetting that (or waiting on the wrong zone, like the offline
        // ``nexusd-cluster share`` CLI did against ``parent_zone``) raced the
        // raft election and surfaced as ``NotLeader { leader_hint:
        // Some(self_id) }`` ~400 ms into the call.  This pin keeps the
        // contract: the helper owns its leadership precondition, so
        // back-to-back ``create_zone`` + ``share_subtree_core`` on a
        // single-node cluster must succeed without external waits.
        let (zm, _dir) = make_zm(1);
        // Seed a parent zone with one entry under ``/shared`` so the
        // share copies something non-trivial — the propose path against
        // ``new_zone`` is what we're really exercising.
        let parent = zm
            .create_zone(contracts::ROOT_ZONE_ID, vec![])
            .expect("root create");
        assert!(
            parent.wait_for_leader(std::time::Duration::from_secs(5)),
            "root must elect itself as 1-voter leader"
        );
        let meta = encode_file_metadata("/shared", DT_DIR, contracts::ROOT_ZONE_ID, "");
        let handle = zm.rt().handle().clone();
        let root_node = zm
            .registry
            .get_node(contracts::ROOT_ZONE_ID)
            .expect("root node");
        propose_set_metadata(&handle, &root_node, "/shared", meta).expect("seed /shared");

        // Fresh shared zone — created immediately before the share call
        // so the raft tick driver has had no time to elect.  Under the
        // old contract this raced and returned NotLeader.
        zm.create_zone("sharedzone", vec![])
            .expect("shared zone create");
        let copied = zm
            .share_subtree_core(contracts::ROOT_ZONE_ID, "/shared", "sharedzone")
            .expect("share_subtree_core must wait for new_zone leader internally");
        assert!(copied >= 1, "at least the /shared root entry copied");
    }

    #[test]
    fn test_create_zone_after_join_with_persisted_conf_state_is_noop() {
        // Cross-process restart: zone "z1" was created in a prior
        // process (1-voter under the opaque-ID contract), redb files
        // persist on disk.  A subsequent process loading from disk
        // must no-op `create_zone` rather than reset the ConfState —
        // restart paths must always preserve persisted membership.
        let dir = TempDir::new().expect("tempdir");
        let path = dir.path().to_str().expect("utf-8").to_string();
        {
            let zm =
                ZoneManager::with_node_id("h1", 1, &path, vec![], "127.0.0.1:0", None, None, None)
                    .expect("zm-1");
            zm.create_zone("z1", vec![]).expect("create");
            // Drop zm — runtime + gRPC server shut down, redb files
            // remain on disk.
        }
        let zm = ZoneManager::with_node_id("h1", 1, &path, vec![], "127.0.0.1:0", None, None, None)
            .expect("zm-2");
        // open_existing_zones_from_disk should have re-loaded "z1"
        // with the persisted ConfState; create_zone with identical
        // (empty) peer list must hit the idempotency branch.
        assert!(zm.get_zone("z1").is_some(), "zone reloaded from disk");
        let _ = zm
            .create_zone("z1", vec![])
            .expect("create idempotent on restart");
    }
}
