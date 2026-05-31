//! Concrete `DistributedCoordinator` implementation.
//!
//! `RaftDistributedCoordinator` is the raft-crate impl of the
//! `DistributedCoordinator` trait the kernel exposes. The Cargo edge
//! runs `raft → kernel`; the kernel installs an
//! `Arc<dyn DistributedCoordinator>` into its `federation` slot from
//! the binary boot path, and federation-aware syscalls dispatch through
//! the trait.
//!
//! ## Provider shape
//!
//! `RaftDistributedCoordinator` owns the federation-side state:
//!
//! * `Arc<ZoneManager>` — per-zone Raft groups + gRPC server.
//! * `Arc<ZoneRaftRegistry>` — zone-id → ZoneConsensus lookup.
//! * `tokio::runtime::Handle` — kernel-shared runtime for raft proposes.
//! * `mount_reconciliation_done` — the "federation bootstrap finished"
//!   atomic flag previously read by `/healthz/ready`.
//!
//! Trait methods receive `kernel: &Kernel` so they can reach kernel-side
//! primitives (vfs_router, dcache, peer_client, set_self_address) without
//! holding back-references; the provider only owns the raft-side state.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use contracts::lock_state::Locks;
use dashmap::DashMap;
use kernel::abc::meta_store::MetaStore;
use kernel::core::vfs_router::canonicalize_mount_path as canonicalize;
use kernel::hal::distributed_coordinator::{
    ClusterInfo, CoordinatorResult, DistributedCoordinator, ShareInfo,
};
use kernel::kernel::Kernel;

use crate::transport::NodeAddress;
use crate::zone_meta_store::ZoneMetaStore;
use crate::{TlsFiles, ZoneManager};

/// Node-level random ID filename — opaque random u64 minted at first
/// daemon boot, persisted across restarts, regenerated after a wipe.
///
/// Format: 8 bytes BE u64.  Absent = fresh daemon — mint a new ID
/// and persist.  Present = restart — reuse the persisted ID.
///
/// Architecture: `docs/architecture/federation-memo.md` § 6.3.1.
const NODE_ID_FILE: &str = ".node_id";

/// Cadence for `bootstrap_or_join_zone`'s JoinZone retry loop when
/// every peer in NEXUS_PEERS is unreachable.  Indefinite by design —
/// the daemon waits for the operator to bring up the first peer with
/// `NEXUS_BOOTSTRAP_NEW=1`; any deadline here would just make the
/// failure mode "silently exit on misconfig" instead of "stay up and
/// retry".
const JOIN_ZONE_RETRY_INTERVAL: Duration = Duration::from_secs(2);

/// Per-attempt timeout for the JoinZone RPC during bootstrap.
const JOIN_ZONE_RPC_TIMEOUT_SECS: u64 = 5;
const FOUNDER_REJOIN_PROBE_ATTEMPTS: u32 = 3;

/// Triple keyed by target zone: `(parent_zone_id, mount_path, global_path)`.
type CrossZoneMountTuple = (String, String, String);

/// Raft-backed `DistributedCoordinator` impl.
///
/// All state is `OnceLock` so the provider is `Send + Sync + 'static`
/// without interior mutability noise.  `init_from_env` populates the
/// slots; subsequent calls observe a stable snapshot.
pub struct RaftDistributedCoordinator {
    zone_manager: OnceLock<Arc<ZoneManager>>,
    runtime: OnceLock<tokio::runtime::Handle>,
    bootstrap_done: AtomicBool,
    /// Reverse index `target_zone_id → [(parent_zone, mount_path, global_path)]`
    /// — derived cache for `wire_mount` reconstruction logic, populated as
    /// federation mounts get wired.  Node-local: replication SSOT lives in
    /// the DT_MOUNT entries on the metastore, this is only a fast-lookup
    /// shadow; rebuilt from scratch on process restart by the reconcile loop.
    ///
    /// Wrapped in `Arc` so the apply-cb closures (one per parent zone)
    /// can capture a cheap clone — they can't borrow from `&self`.
    cross_zone_mounts: Arc<DashMap<String, Vec<CrossZoneMountTuple>>>,
}

impl RaftDistributedCoordinator {
    pub fn new() -> Self {
        Self {
            zone_manager: OnceLock::new(),
            runtime: OnceLock::new(),
            bootstrap_done: AtomicBool::new(false),
            cross_zone_mounts: Arc::new(DashMap::new()),
        }
    }

    /// Wire this provider against an already-built kernel, zone
    /// manager, and tokio runtime; then activate every DT_MOUNT entry
    /// already present on disk.  Idempotent.
    ///
    /// This is the subset of [`init_from_env`]'s boot work that cluster-
    /// profile binaries (`nexusd-cluster`) also need: those binaries
    /// build their own [`Kernel`] and [`ZoneManager`] directly rather
    /// than going through `init_from_env`, so without this method the
    /// DT_MOUNT apply-cb is never installed on `root` (or any other
    /// loaded zone) — DT_MOUNT entries committed via `share --mount-at`
    /// / `join` / `apply_topology` would write into the raft state
    /// machine but never make it into [`VFSRouter`], and routing for
    /// the mounted path silently falls through to the parent backend.
    ///
    /// Caller invariants:
    ///   * `zm.list_zones()` already includes every zone whose mounts
    ///     should be replayed — call this **after** the
    ///     restart/bootstrap dispatch that loads zones from disk.
    ///   * `runtime` is the tokio runtime that owns the zone manager's
    ///     transport loops (typically `zm.runtime_handle()`).
    pub fn install_with_kernel(
        &self,
        zm: Arc<ZoneManager>,
        runtime: tokio::runtime::Handle,
        kernel: &Kernel,
    ) {
        // Slots are `OnceLock`; second-set silently drops, so calling
        // this twice with the same wiring is a no-op rather than an
        // error.
        let _ = self.zone_manager.set(zm.clone());
        let _ = self.runtime.set(runtime);

        // Apply-cb install on every loaded zone — root, federation
        // zones from `NEXUS_FEDERATION_ZONES`, zones restored from
        // disk after restart.  Mirrors `init_from_env` lines 1191-1199.
        for zone_id in zm.list_zones() {
            self.install_apply_cb_for_zone(kernel, &zone_id);
        }

        // Replay scan — apply-cb only fires on NEW raft applies, so
        // without this a restart leaves restored DT_MOUNTs unwired in
        // VFSRouter / DCache.
        self.replay_existing_mounts(kernel);
    }

    fn zm(&self) -> Option<&Arc<ZoneManager>> {
        self.zone_manager.get()
    }

    /// Install the DT_MOUNT apply-cb on `zone_id`'s consensus.  Called
    /// from boot (`init_from_env` for root + listed federation zones)
    /// and from `create_zone` so every locally-loaded zone fires
    /// `wire_mount_core` on raft-applied DT_MOUNT events — the
    /// follower-side mechanism that keeps cross-zone routing in sync.
    /// Idempotent — re-installation replaces the closure with an
    /// equivalent one on the same `coherence_id`.
    fn install_apply_cb_for_zone(&self, kernel: &Kernel, zone_id: &str) {
        let Some(zm) = self.zm() else {
            return;
        };
        let Some(runtime) = self.runtime.get() else {
            return;
        };
        let Some(consensus) = zm.registry().get_node(zone_id) else {
            tracing::debug!(zone_id = %zone_id, "install_apply_cb_for_zone: zone not loaded yet");
            return;
        };
        let vfs_router = kernel.vfs_router_arc();
        let lock_manager = kernel.lock_manager_arc();
        install_mount_apply_cb_impl(
            &vfs_router,
            &lock_manager,
            &zm.registry(),
            runtime,
            &self.cross_zone_mounts,
            zone_id,
            &consensus,
        );
    }

    /// Re-wire every DT_MOUNT entry already applied in any zone's state
    /// machine.  The apply-cb only fires on NEW raft applies, so without
    /// this replay a restart leaves restored mounts unwired in VFSRouter
    /// / DCache — followers fail every cross-zone read until the next
    /// fresh DT_MOUNT lands.  Topological retry handles parent→child
    /// ordering (a nested mount can't wire until its parent's mount is
    /// in `cross_zone_mounts`).
    fn replay_existing_mounts(&self, kernel: &Kernel) {
        let Some(zm) = self.zm() else {
            return;
        };
        let Some(runtime) = self.runtime.get() else {
            return;
        };
        let registry = zm.registry();
        let vfs_router = kernel.vfs_router_arc();
        let lock_manager = kernel.lock_manager_arc();

        let mut pending: Vec<(String, String, String)> = Vec::new();
        for zone_id in zm.list_zones() {
            let Some(consensus) = registry.get_node(&zone_id) else {
                continue;
            };
            let entries = consensus.iter_dt_mount_entries().unwrap_or_default();
            for (key, target_zone_id) in entries {
                pending.push((zone_id.clone(), key, target_zone_id));
            }
        }

        if pending.is_empty() {
            return;
        }
        tracing::info!(
            count = pending.len(),
            "replay_existing_mounts: scanning DT_MOUNT entries"
        );

        // Topological retry: a nested mount needs its parent's
        // cross_zone_mounts entry to reconstruct the global path.  Cap
        // rounds at pending.len()+1 so a misconfigured cycle errors
        // instead of looping forever.
        let max_rounds = pending.len() + 1;
        for _ in 0..max_rounds {
            if pending.is_empty() {
                break;
            }
            let mut progressed = false;
            pending.retain(|(parent_zone_id, mount_path, target_zone_id)| {
                let r = wire_mount_core(
                    &vfs_router,
                    &lock_manager,
                    &registry,
                    runtime,
                    &self.cross_zone_mounts,
                    parent_zone_id,
                    mount_path,
                    target_zone_id,
                );
                match r {
                    Ok(()) => {
                        if self.cross_zone_mounts.contains_key(target_zone_id) {
                            progressed = true;
                            false // wired — drop from pending
                        } else {
                            true // wire_mount_core deferred (parent not ready) — retry
                        }
                    }
                    Err(_) => false, // permanent failure — give up
                }
            });
            if !progressed {
                break;
            }
        }
        if !pending.is_empty() {
            tracing::warn!(
                pending = pending.len(),
                "replay_existing_mounts: {} entries left unwired (likely missing parent zone)",
                pending.len(),
            );
        }
    }
}

impl Default for RaftDistributedCoordinator {
    fn default() -> Self {
        Self::new()
    }
}

/// Bootstrap mode declared by the operator at daemon start.
///
/// Pre-this-enum, `bootstrap_or_join_zone` would dispatch its three
/// branches purely from runtime state inference (data-dir empty?
/// `NEXUS_BOOTSTRAP_NEW=1`?  peers non-empty?).  That implicit
/// dispatch made it possible to mix scenarios — point a `share`
/// command's `--data-dir` at a static-bootstrap data dir and the CLI
/// would happily treat it as runtime state of an in-progress dynamic
/// cluster.  Empirically caused leadership-chase confusion during
/// cross-machine smoke and conflated two intent layers.
///
/// Now operator must declare intent up front:
///
///   * `Static` — env-driven cluster formation. Empty data dir means
///     bootstrap/join from env: empty `--peers` = single-node founder
///     by default (every cluster starts as a 1-voter); non-empty
///     `--peers` = joiner. Existing `root` state is accepted as a
///     container/process restart because orchestration normally
///     restarts with the same env and persisted volume.
///   * `Dynamic` — daemon comes up rootless; operator drives zone
///     formation via runtime API (`nexusd-cluster share` / `join`,
///     or Python `nexusd federation_create_zone`). Env vars and
///     `--peers` related to bootstrap are REJECTED. Data dir MUST be
///     empty.  No root zone is created at boot; the daemon serves
///     the gRPC surface and waits for runtime zone-management calls.
///   * `Restart` — data dir holds persisted ConfState from a prior
///     boot; resume from it. Bootstrap-related env vars / flags are
///     REJECTED (state on disk is the SSOT).
///
/// Validation runs once at boot and fails loud on any contradiction
/// — operator gets a clear error rather than discovering implicit
/// behaviour months later.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BootstrapMode {
    Static,
    Dynamic,
    Restart,
}

impl BootstrapMode {
    /// Parse a textual mode declaration.  Accepts case-insensitive
    /// `"static"` / `"dynamic"` / `"restart"`.
    pub fn parse(s: &str) -> Result<Self, String> {
        match s.trim().to_ascii_lowercase().as_str() {
            "static" => Ok(BootstrapMode::Static),
            "dynamic" => Ok(BootstrapMode::Dynamic),
            "restart" => Ok(BootstrapMode::Restart),
            other => Err(format!(
                "invalid bootstrap mode '{other}' — expected one of: static, dynamic, restart",
            )),
        }
    }

    /// Human-readable name (lowercase) for log lines and error messages.
    pub fn as_str(self) -> &'static str {
        match self {
            BootstrapMode::Static => "static",
            BootstrapMode::Dynamic => "dynamic",
            BootstrapMode::Restart => "restart",
        }
    }
}

/// Validate that the declared bootstrap mode is consistent with
/// runtime state and operator inputs.  Fails loud at boot time so
/// misconfiguration surfaces before the daemon's gRPC server starts
/// rather than as a silent stall later.
///
/// Inputs:
///   * `mode` — operator declaration (CLI flag or env var).
///   * `data_dir_has_root` — `<data_dir>/root/raft/` exists, i.e.
///     persisted ConfState is present.  Caller checks the filesystem.
///   * `bootstrap_new_set` — operator passed `NEXUS_BOOTSTRAP_NEW=1`
///     or equivalent CLI flag.
///   * `peers_non_empty` — operator passed `NEXUS_PEERS` /
///     `--peers` with at least one entry (after
///     `validate_peers_excludes_self`).
///
/// Rules:
///   * `Static`: data dir may be empty (fresh bootstrap/join) or
///     already hold root state (container/process restart with the
///     same env).  Both `bootstrap_new_set` and `peers_non_empty` are
///     OPTIONAL — empty/empty is the single-node founder default
///     (`bootstrap_or_join_zone` creates a 1-voter zone alone), and
///     non-empty peers triggers the joiner retry loop on empty state.
///   * `Dynamic`: data dir MUST be empty (no persisted state);
///     `bootstrap_new_set` and `peers_non_empty` MUST both be false
///     (runtime API drives, not env).
///   * `Restart`: data dir MUST be non-empty (state to resume);
///     `bootstrap_new_set` and `peers_non_empty` MUST both be false
///     (state on disk is authoritative).
pub fn validate_bootstrap_mode(
    mode: BootstrapMode,
    data_dir_has_root: bool,
    bootstrap_new_set: bool,
    peers_non_empty: bool,
) -> Result<(), String> {
    match mode {
        BootstrapMode::Static => {
            // Existing `root` state under static mode is a normal
            // container restart: the process receives the same env as
            // the first boot, while persisted ConfState remains the
            // source of truth. `bootstrap_or_join_zone` checks loaded
            // state first and resumes instead of re-creating.
            let _ = data_dir_has_root;
            // Empty `bootstrap_new_set` AND empty `peers_non_empty` is
            // the single-node founder default — every cluster starts
            // as a 1-voter group.  Non-empty peers triggers the
            // joiner retry loop on empty state.  Both can coexist
            // (explicit founder declaration with hostname-only peers
            // list).
            Ok(())
        }
        BootstrapMode::Dynamic => {
            if data_dir_has_root {
                return Err(
                    "bootstrap mode = dynamic, but data dir already holds a 'root' zone — \
                     dynamic mode requires a fresh data dir; runtime share/join builds the \
                     cluster from scratch.  Either pass mode = restart, or wipe the data dir."
                        .to_string(),
                );
            }
            if bootstrap_new_set {
                return Err(
                    "bootstrap mode = dynamic forbids NEXUS_BOOTSTRAP_NEW — that flag belongs \
                     to static mode.  Drop the flag, or switch to mode = static."
                        .to_string(),
                );
            }
            if peers_non_empty {
                return Err(
                    "bootstrap mode = dynamic forbids NEXUS_PEERS / --peers — peer addresses \
                     enter via runtime share/join commands, not env.  Drop the flag, or \
                     switch to mode = static."
                        .to_string(),
                );
            }
            Ok(())
        }
        BootstrapMode::Restart => {
            if !data_dir_has_root {
                return Err(
                    "bootstrap mode = restart, but data dir is empty — there is no persisted \
                     state to resume from.  Either pass mode = static (with peers/flag) or \
                     mode = dynamic (clean start)."
                        .to_string(),
                );
            }
            if bootstrap_new_set {
                return Err(
                    "bootstrap mode = restart forbids NEXUS_BOOTSTRAP_NEW — persisted state \
                     is the source of truth.  Drop the flag."
                        .to_string(),
                );
            }
            if peers_non_empty {
                return Err(
                    "bootstrap mode = restart forbids NEXUS_PEERS / --peers — persisted \
                     ConfState carries the address book.  Drop the flag."
                        .to_string(),
                );
            }
            Ok(())
        }
    }
}

/// Read the persisted node ID, or mint a fresh random one and persist it.
///
/// SSOT for raft node identity under the opaque-ID contract.  See
/// [`NODE_ID_FILE`] for the rationale and on-disk format.
///
/// First-ever boot (and post-`rm -rf $NEXUS_DATA_DIR` rejoin) lands
/// in the mint branch: `rand::random::<u64>()` produces a fresh ID,
/// retried once if it happens to be 0 (raft-rs reserves 0 as
/// "no node").  The mint is atomic — `write` to `<file>.tmp` then
/// `rename` to `<file>` — so a crash between sample and persist
/// either leaves the old ID intact or no file at all (next boot
/// re-mints).  Two daemons sharing a data dir would race here, but
/// that configuration is operator error: a single
/// `<NEXUS_DATA_DIR>` is bound to a single daemon.
/// Mint or load the node-identity file at `<zones_dir>/.node_id`.
///
/// Public so non-`init_from_env` boot paths (cluster-profile binary
/// `nexusd-cluster::run_daemon`) share the same SSOT for raft node
/// identity.  See `bootstrap_or_join_zone` for why opaque random IDs
/// are required under raft-rs 0.7's stale-`Progress` heartbeat
/// invariant.
pub fn read_or_mint_node_id(zones_dir: &str) -> Result<u64, String> {
    use std::io::Write;

    let dir = Path::new(zones_dir);
    let final_path = dir.join(NODE_ID_FILE);

    match std::fs::read(&final_path) {
        Ok(bytes) => {
            let arr: [u8; 8] = bytes
                .as_slice()
                .try_into()
                .map_err(|_| format!("node id file '{}' is not 8 bytes", final_path.display()))?;
            let id = u64::from_be_bytes(arr);
            if id == 0 {
                return Err(format!(
                    "node id file '{}' contains 0 (reserved by raft-rs); \
                     wipe `<NEXUS_DATA_DIR>` and retry",
                    final_path.display(),
                ));
            }
            tracing::info!(node_id = id, "node_id loaded from disk");
            Ok(id)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            std::fs::create_dir_all(dir)
                .map_err(|e| format!("create zones dir for node id '{}': {e}", dir.display(),))?;
            // raft-rs reserves 0 as "no node" — retry once on the
            // astronomically rare 1/2^64 collision.
            let mut id = rand::random::<u64>();
            if id == 0 {
                id = rand::random::<u64>();
                if id == 0 {
                    return Err("rand::random() returned 0 twice".to_string());
                }
            }
            let tmp_path = dir.join(format!("{NODE_ID_FILE}.tmp"));
            {
                let mut tmp = std::fs::File::create(&tmp_path).map_err(|e| {
                    format!("create tmp node id file '{}': {e}", tmp_path.display())
                })?;
                tmp.write_all(&id.to_be_bytes())
                    .map_err(|e| format!("write tmp node id file '{}': {e}", tmp_path.display()))?;
                tmp.sync_all()
                    .map_err(|e| format!("sync tmp node id file '{}': {e}", tmp_path.display()))?;
            }
            std::fs::rename(&tmp_path, &final_path).map_err(|e| {
                format!(
                    "rename '{}' -> '{}': {e}",
                    tmp_path.display(),
                    final_path.display(),
                )
            })?;
            tracing::info!(node_id = id, "node_id minted and persisted");
            Ok(id)
        }
        Err(e) => Err(format!("read node id '{}': {e}", final_path.display())),
    }
}

/// Validate that the parsed peer address book excludes `self_address`.
///
/// Contract under PR #3996+: `NEXUS_PEERS` (or `--peers` for
/// `nexusd-cluster`) lists OTHER nodes only.  Self goes in the
/// ConfState via `create_zone(self)` on the founder or `AddNode(self)`
/// on a joiner — not via the address book.  Listing self inside the
/// address book was a vestige of the pre-#3996 contract where
/// `peer_addrs` doubled as the ConfState voter list, and today it is
/// a footgun: if `self_address` does not exactly string-match the
/// self entry (e.g. operator passed an IP for the peer entry but the
/// hostname-derived `self_address` falls back to the OS hostname),
/// the JoinZone retry loop tries to RPC self, registers the zone
/// locally with `skip_bootstrap=true`, and stalls because there is no
/// real leader to grant `AddNode`.
///
/// Fail-loud here so the misconfiguration surfaces at boot rather
/// than as a silent stall after `Zone 'root' registered (peers=1)`.
pub fn validate_peers_excludes_self(
    peer_addrs: &[NodeAddress],
    self_address: &str,
) -> Result<(), String> {
    for peer in peer_addrs {
        let peer_hostport = peer
            .endpoint
            .trim_start_matches("https://")
            .trim_start_matches("http://");
        if peer_hostport == self_address {
            return Err(format!(
                "peer list contains self ('{self_address}'); under the PR #3996 \
                 opaque-ID contract NEXUS_PEERS / --peers must list OTHER nodes \
                 only.  Self joins the cluster via NEXUS_BOOTSTRAP_NEW=1 \
                 (founder) or AddNode-on-leader (joiner), not the address book.",
            ));
        }
    }
    Ok(())
}

fn joiner_local_zone_peer_seeds(peer_addrs: &[NodeAddress]) -> Vec<String> {
    let mut seeds: Vec<String> = peer_addrs
        .iter()
        .map(NodeAddress::to_raft_peer_str)
        .collect();
    seeds.sort();
    seeds
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EmptyStorageBootstrapPlan {
    FoundImmediately,
    ProbePeersThenFound,
    JoinPeers,
}

fn empty_storage_bootstrap_plan(
    bootstrap_new: bool,
    peer_addrs: &[NodeAddress],
) -> EmptyStorageBootstrapPlan {
    if peer_addrs.is_empty() {
        EmptyStorageBootstrapPlan::FoundImmediately
    } else if bootstrap_new {
        EmptyStorageBootstrapPlan::ProbePeersThenFound
    } else {
        EmptyStorageBootstrapPlan::JoinPeers
    }
}

fn create_founder_zone(
    zm: &ZoneManager,
    zone_id: &str,
    node_id: u64,
    self_address: &str,
    bootstrap_new: bool,
    peers_empty: bool,
) -> Result<(), String> {
    tracing::info!(
        node_id,
        zone = %zone_id,
        self_address = %self_address,
        bootstrap_new,
        peers_empty,
        "founder path — creating 1-voter zone. Other nodes JoinZone here.",
    );
    let self_peer = format!("{node_id}@{self_address}");
    zm.create_zone(zone_id, vec![self_peer])
        .map_err(|e| format!("create_zone({zone_id}): {e}"))
        .map(|_| ())
}

fn block_on_zone_manager<F>(zm: &ZoneManager, fut: F) -> F::Output
where
    F: std::future::Future,
{
    let handle = zm.runtime_handle();
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::task::block_in_place(|| handle.block_on(fut))
    } else {
        handle.block_on(fut)
    }
}

fn remove_local_probe_zone(zm: &ZoneManager, zone_id: &str) {
    if zm.get_zone(zone_id).is_none() {
        return;
    }
    if let Err(e) = block_on_zone_manager(zm, zm.registry().remove_zone(zone_id)) {
        tracing::warn!(
            zone = %zone_id,
            error = %e,
            "founder rejoin probe cleanup failed before local create",
        );
    }
}

// Same rationale as `bootstrap_or_join_zone` above — every arg is a
// primitive descriptor the caller already names, bundling them adds
// boilerplate without expressive gain.
#[allow(clippy::too_many_arguments)]
fn attempt_join_zone_round(
    zm: &ZoneManager,
    zone_id: &str,
    node_id: u64,
    self_address: &str,
    peer_addrs: &[NodeAddress],
    join_runtime: &tokio::runtime::Runtime,
    rpc_timeout_secs: u64,
    as_learner: bool,
) -> Result<(), String> {
    let mut last_err = String::new();
    for peer in peer_addrs {
        let mut endpoint = peer.endpoint.clone();
        let mut redirected_once = false;
        loop {
            // Order matters: register locally with skip_bootstrap
            // FIRST so this node's gRPC server can serve append-
            // entries from the leader once AddNode commits.
            if zm.get_zone(zone_id).is_none() {
                let zone_peers = joiner_local_zone_peer_seeds(peer_addrs);
                tracing::info!(
                    zone = %zone_id,
                    seed_count = zone_peers.len(),
                    seed_peers = ?zone_peers,
                    "local join_zone seed peers",
                );
                if let Err(e) = zm.join_zone(zone_id, zone_peers, /* learner */ as_learner) {
                    last_err = format!("local join_zone setup failed: {e}");
                    tracing::warn!(
                        endpoint = %endpoint,
                        zone = %zone_id,
                        error = %e,
                        "local join_zone setup failed; will retry next peer",
                    );
                    break;
                }
            }
            let attempt = join_runtime.block_on(crate::transport::call_join_zone_rpc(
                &endpoint,
                zone_id,
                node_id,
                self_address,
                as_learner,
                rpc_timeout_secs,
            ));
            match attempt {
                Ok(result) if result.success => {
                    tracing::info!(
                        endpoint = %endpoint,
                        zone = %zone_id,
                        node_id,
                        "joined zone via leader",
                    );
                    return Ok(());
                }
                Ok(result) => {
                    if let Some(addr) = result.leader_address.as_ref() {
                        if !redirected_once && !addr.is_empty() && addr != &endpoint {
                            tracing::info!(
                                from = %endpoint,
                                to = %addr,
                                zone = %zone_id,
                                "JoinZone redirect to leader",
                            );
                            endpoint = addr.clone();
                            redirected_once = true;
                            continue;
                        }
                    }
                    last_err = format!("{}: {:?}", endpoint, result.error);
                    tracing::debug!(
                        endpoint = %endpoint,
                        zone = %zone_id,
                        error = ?result.error,
                        "JoinZone non-success; trying next peer",
                    );
                    break;
                }
                Err(e) => {
                    last_err = format!("{}: {e}", endpoint);
                    tracing::debug!(
                        endpoint = %endpoint,
                        zone = %zone_id,
                        error = %e,
                        "JoinZone RPC error; trying next peer",
                    );
                    break;
                }
            }
        }
    }
    Err(last_err)
}

fn record_join_attempt(
    attempt: Result<(), String>,
    attempts: &mut u32,
    max_attempts: Option<u32>,
    zone_id: &str,
) -> Result<bool, String> {
    match attempt {
        Ok(()) => Ok(true),
        Err(last_err) => {
            *attempts = attempts.saturating_add(1);
            if let Some(max) = max_attempts {
                if *attempts >= max {
                    let attempt_count = *attempts;
                    return Err(format!(
                        "JoinZone({zone_id}): no peer accepted after {attempt_count} attempts; \
                         leader unreachable or quorum lost on remote zone. Last error: {last_err}",
                    ));
                }
            }
            Ok(false)
        }
    }
}

/// Bring a zone online — dispatch table for the three bootstrap
/// branches under the opaque-ID contract.  Generalised over `zone_id`
/// so the same SSOT machinery serves:
///
///   * `init_from_env` and `nexusd-cluster::run_daemon` for the root
///     zone (`zone_id="root"`, `max_attempts=None` — daemon boot path
///     wants forever-retry on misconfig).
///   * `nexusd-cluster::run_join` for non-root zones via the offline
///     `join` subcommand (`zone_id=<remote_zone>`,
///     `max_attempts=Some(N)` — CLI must terminate, so cap retries).
///   * Future Python `nexusd` `federation_create_zone` /
///     `federation_join` RPC handlers can call the same helper.
///
/// Branches:
///
///   1. **Restart** — `zm.get_zone(zone_id).is_some()` means
///      `open_existing_zones_from_disk` already loaded the local
///      replica from `<zones_dir>/<zone_id>/raft/raft.redb`.  Persisted
///      ConfState is authoritative; we just resume from it.
///
///   2. **Fresh create** — `bootstrap_new=true`.  Create a 1-voter
///      cluster consisting of `self.node_id` only.  Other nodes will
///      land in branch 3 and JoinZone here.  Without the operator
///      flag this path is forbidden — accidentally creating a second
///      1-voter cluster on a joiner would partition the federation.
///
///   3. **Wait-and-join** — empty storage, no flag.  Loop calling
///      JoinZone RPC against each peer in the address book.  The
///      leader's response commits a `ConfChangeV2(AddNode(self))`;
///      we locally `join_zone(skip_bootstrap=true)` first so the
///      leader's snapshot installs the authoritative ConfState.
///      `max_attempts=None` retries forever (daemon boot — misconfig
///      surfaces as "daemon stays up retrying" rather than a silent
///      exit); `max_attempts=Some(N)` bounds the loop to N rounds
///      (CLI — operator command must terminate).
///
/// Returns `Err` with a descriptive string if `max_attempts` was
/// exhausted without a successful JoinZone — caller surfaces this to
/// the operator.
///
/// `as_learner` selects the membership role on the **joiner** branch
/// (branch 3) — the leader proposes `AddLearnerNode` instead of
/// `AddNode`, so the new node receives full replication but does not
/// count toward quorum.  Picking the right value is a contract
/// distinction, not an operator knob:
///
///   * **`as_learner=false`** — root zone bootstrap.  Every node in a
///     root cluster votes; quorum is essential for any write.  Use
///     ≥3 voters (+optional witness) in production so single-node
///     loss does not lose quorum.
///   * **`as_learner=true`**  — subtree share / mount.  The zone has
///     one authoritative owner (the `share` creator); joiners are
///     readers / replicas that should never affect the owner's
///     ability to commit.  This makes wipe-rejoin safe by
///     construction — losing a learner has zero quorum impact, so
///     SSD swap / OS reinstall / device migration cannot strand the
///     zone in `not leader` deadlock the way a 2-voter pattern can.
///
/// The branch-1 (restart) and branch-2 (founder) paths ignore
/// `as_learner` — restart resumes from persisted ConfState (which
/// already reflects historical role assignments), and a founder
/// always seeds itself as the 1-voter author of the cluster.
// `clippy::too_many_arguments`: every argument here is a primitive
// boot-time descriptor (zone id, node id, address, peer list, three
// flags).  Bundling them into a struct would force every caller —
// `init_from_env`, `run_daemon`, `run_join`, future Python RPC
// handlers — to import that struct just to populate the fields one
// by one with the same names.  Net readability loss for zero
// expressive gain, so we keep the explicit signature and silence
// the lint locally.
#[allow(clippy::too_many_arguments)]
pub fn bootstrap_or_join_zone(
    zm: &ZoneManager,
    zone_id: &str,
    node_id: u64,
    self_address: &str,
    peer_addrs: &[NodeAddress],
    bootstrap_new: bool,
    max_attempts: Option<u32>,
    as_learner: bool,
) -> Result<(), String> {
    // Branch 1: zone already loaded from disk.
    if zm.get_zone(zone_id).is_some() {
        tracing::info!(
            node_id,
            zone = %zone_id,
            "zone loaded from persisted storage; resuming from ConfState",
        );
        return Ok(());
    }

    // Branch 2: founder — either operator explicit (`bootstrap_new`)
    // or implicit (empty peers = single-node alone).
    //
    // Every raft cluster starts as a 1-voter group; whether the
    // operator declares founder intent via the flag or by simply
    // not configuring peers, the next step is the same: create the
    // zone with self as the only voter.  Other nodes will JoinZone
    // here later.
    //
    //   * `bootstrap_new=true`  → explicit founder declaration.
    //     Required for multi-node deployments where the founder
    //     does list peer addresses (so it can dial them once they
    //     come up) but is the one originating the cluster.
    //   * `peer_addrs.is_empty()` → no peers configured = alone.
    //     Single-node default — create own root.
    let plan = empty_storage_bootstrap_plan(bootstrap_new, peer_addrs);
    if matches!(plan, EmptyStorageBootstrapPlan::FoundImmediately) {
        create_founder_zone(
            zm,
            zone_id,
            node_id,
            self_address,
            bootstrap_new,
            peer_addrs.is_empty(),
        )?;
        return Ok(());
    }

    // Branch 3: peers configured, no flag, storage empty — joiner.
    // Loop on JoinZone RPC until a leader accepts.
    tracing::info!(
        node_id,
        zone = %zone_id,
        peer_count = peer_addrs.len(),
        max_attempts = ?max_attempts,
        "empty storage with peers, no bootstrap flag — retrying JoinZone",
    );

    // Spin a small temporary multi-thread runtime for the JoinZone
    // RPCs.  ZoneManager owns the long-lived runtime, but `block_on`
    // on its handle from this thread (the kernel boot thread) is
    // unsafe if the boot thread is itself a worker of an outer
    // runtime.  A fresh multi-thread runtime with one worker
    // sidesteps the issue cleanly — we drop it once we've joined.
    let join_runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .thread_name("nexus-bootstrap-join")
        .build()
        .map_err(|e| format!("bootstrap join runtime: {e}"))?;

    if matches!(plan, EmptyStorageBootstrapPlan::ProbePeersThenFound) {
        tracing::info!(
            node_id,
            zone = %zone_id,
            peer_count = peer_addrs.len(),
            attempts = FOUNDER_REJOIN_PROBE_ATTEMPTS,
            "founder path — probing peers for an existing live cluster before creating local root",
        );
        let mut last_err = String::new();
        for _ in 0..FOUNDER_REJOIN_PROBE_ATTEMPTS {
            match attempt_join_zone_round(
                zm,
                zone_id,
                node_id,
                self_address,
                peer_addrs,
                &join_runtime,
                JOIN_ZONE_RPC_TIMEOUT_SECS,
                as_learner,
            ) {
                Ok(()) => return Ok(()),
                Err(e) => last_err = e,
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        tracing::info!(
            node_id,
            zone = %zone_id,
            last_error = %last_err,
            "founder probe found no existing cluster; creating local 1-voter zone",
        );
        remove_local_probe_zone(zm, zone_id);
        create_founder_zone(
            zm,
            zone_id,
            node_id,
            self_address,
            bootstrap_new,
            peer_addrs.is_empty(),
        )?;
        return Ok(());
    }

    let mut attempts: u32 = 0;
    loop {
        let joined = record_join_attempt(
            attempt_join_zone_round(
                zm,
                zone_id,
                node_id,
                self_address,
                peer_addrs,
                &join_runtime,
                JOIN_ZONE_RPC_TIMEOUT_SECS,
                as_learner,
            ),
            &mut attempts,
            max_attempts,
            zone_id,
        )?;
        if joined {
            return Ok(());
        }
        std::thread::sleep(JOIN_ZONE_RETRY_INTERVAL);
    }
}

impl RaftDistributedCoordinator {
    /// Boot-time init from environment variables (`NEXUS_HOSTNAME`,
    /// `NEXUS_PEERS`, `NEXUS_BIND_ADDR`, `NEXUS_DATA_DIR`,
    /// `NEXUS_RAFT_TLS`, …). Idempotent — `Ok(false)` when federation
    /// was already initialised, `Ok(true)` on first successful init.
    ///
    /// Inherent (not on trait): boot-time wiring, fires once per
    /// process from [`install`], outside the runtime trait surface.
    pub fn init_from_env(&self, kernel: &Kernel) -> CoordinatorResult<bool> {
        // Idempotent — if zone manager already exists, treat as
        // "already initialised" and report no-op.
        if self.zone_manager.get().is_some() {
            return Ok(false);
        }

        // Bootstrap contract — single classic-aligned path:
        //
        //   1. `node_id` is an opaque random u64 minted at first daemon
        //      boot, persisted to `<NEXUS_DATA_DIR>/.node_id`.  Wipe-
        //      rejoin mints a fresh ID; raft-rs's `Progress[new_id]`
        //      starts at `matched=0`, so the first heartbeat carries
        //      `m.commit=0` and cannot trip `RaftLog::commit_to`'s
        //      stale-`Progress` panic.
        //   2. NEXUS_PEERS is a hostname → endpoint address book only.
        //      It seeds the transport peer map for raft messaging; it
        //      is **not** the source of truth for ConfState.  ConfState
        //      is mutated by ConfChange (AddNode / RemoveNode), driven
        //      by JoinZone RPC.
        //   3. Empty storage + `NEXUS_BOOTSTRAP_NEW=1` →
        //      `create_zone("root")` 1-voter cluster.  Empty storage +
        //      flag unset → block on JoinZone forever.  Non-empty
        //      storage → resume from persisted ConfState.
        //
        // See `bootstrap_or_join_zone` for the dispatch table.
        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        let bootstrap_new = std::env::var("NEXUS_BOOTSTRAP_NEW")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        if std::env::var("NEXUS_JOINER_HINT").is_ok() {
            tracing::warn!(
                "NEXUS_JOINER_HINT is no longer honored — bootstrap mode is \
                 auto-detected from on-disk state + NEXUS_BOOTSTRAP_NEW.  \
                 Drop the env var; this warning is non-fatal."
            );
        }

        let hostname = std::env::var("NEXUS_HOSTNAME").ok().unwrap_or_else(|| {
            #[cfg(unix)]
            {
                std::process::Command::new("hostname")
                    .output()
                    .ok()
                    .and_then(|o| String::from_utf8(o.stdout).ok())
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "localhost".to_string())
            }
            #[cfg(not(unix))]
            {
                std::env::var("COMPUTERNAME").unwrap_or_else(|_| "localhost".to_string())
            }
        });

        let bind_addr =
            std::env::var("NEXUS_BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:2126".to_string());

        let self_addr = std::env::var("NEXUS_ADVERTISE_ADDR")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| {
                let raft_port = bind_addr
                    .rsplit_once(':')
                    .and_then(|(_, p)| p.parse::<u16>().ok())
                    .unwrap_or(2126);
                format!("{hostname}:{raft_port}")
            });
        kernel.set_self_address(&self_addr);
        tracing::info!(self_address = %self_addr, "federation: self-address published");

        let zones_dir = std::env::var("NEXUS_DATA_DIR").unwrap_or_else(|_| {
            std::env::var("NEXUS_STATE_DIR")
                .map(|s| format!("{s}/zones"))
                .unwrap_or_else(|_| "./nexus-zones".to_string())
        });

        // TLS detection — disabled when NEXUS_RAFT_TLS=false (E2E).
        let tls_disabled = std::env::var("NEXUS_RAFT_TLS")
            .map(|v| v.eq_ignore_ascii_case("false") || v == "0")
            .unwrap_or(false)
            || std::env::var("NEXUS_NO_TLS")
                .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                .unwrap_or(false);
        let use_tls_for_endpoints = !tls_disabled;

        // Parse NEXUS_PEERS once into structured NodeAddress entries —
        // address book only.  ZoneManager seeds its transport peer map
        // from this; ConfState is independent (mutated only by
        // ConfChange via JoinZone).
        let peer_addrs: Vec<NodeAddress> = peers_csv
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|entry| {
                NodeAddress::parse(entry, use_tls_for_endpoints)
                    .map_err(|e| format!("NEXUS_PEERS parse '{entry}': {e}"))
            })
            .collect::<Result<Vec<_>, _>>()?;

        // Reject "self listed in NEXUS_PEERS" early — the only way
        // self enters the cluster post-#3996 is through `create_zone`
        // (founder) or AddNode-on-leader (joiner).  See
        // `validate_peers_excludes_self` for the full rationale.
        validate_peers_excludes_self(&peer_addrs, &self_addr)?;

        // Operator declares bootstrap intent up front when federation
        // is in play.  "In play" = any of: data dir already holds a
        // root zone (restart), `NEXUS_BOOTSTRAP_NEW=1` (explicit
        // founder), or non-empty peers (joiner).  Tests / single-
        // node dev workflows that touch none of those signals skip
        // federation entirely — `bootstrap_mode` stays `None` and
        // `bootstrap_or_join_zone` is not called below.
        let data_dir_has_root = Path::new(&zones_dir).join("root").join("raft").exists();
        let federation_in_play = data_dir_has_root || bootstrap_new || !peer_addrs.is_empty();
        let bootstrap_mode: Option<BootstrapMode> = if federation_in_play {
            let mode_str = std::env::var("NEXUS_BOOTSTRAP_MODE").map_err(|_| {
                "NEXUS_BOOTSTRAP_MODE is required when bootstrapping federation \
                 (NEXUS_PEERS, NEXUS_BOOTSTRAP_NEW, or persisted root zone state \
                 detected).  Pass one of: static, dynamic, restart.  \
                 See BootstrapMode docs in nexus_raft."
                    .to_string()
            })?;
            let mode = BootstrapMode::parse(&mode_str)?;
            validate_bootstrap_mode(
                mode,
                data_dir_has_root,
                bootstrap_new,
                !peer_addrs.is_empty(),
            )?;
            tracing::info!(
                mode = mode.as_str(),
                bootstrap_new,
                peers_non_empty = !peer_addrs.is_empty(),
                data_dir_has_root,
                "bootstrap mode validated",
            );
            Some(mode)
        } else {
            None
        };

        let tls = if tls_disabled {
            None
        } else {
            let tls_dir = Path::new(&zones_dir).join("tls");
            let ca_path = tls_dir.join("ca.pem");
            let cert_path = tls_dir.join("node.pem");
            let key_path = tls_dir.join("node-key.pem");
            if ca_path.exists() && cert_path.exists() && key_path.exists() {
                Some(TlsFiles {
                    ca_path,
                    cert_path,
                    key_path,
                    ca_key_path: None,
                    join_token_hash: None,
                })
            } else {
                None
            }
        };

        std::fs::create_dir_all(&zones_dir)
            .map_err(|e| format!("create zones dir '{zones_dir}': {e}"))?;

        // SSOT for raft node identity.  First boot mints a random u64
        // and persists `<zones_dir>/.node_id`; restart loads the
        // persisted value.  Decoupling node_id from hostname satisfies
        // raft-rs's stale-`Progress` heartbeat invariant under wipe-
        // rejoin — a wiped follower's fresh random ID has
        // `Progress[new_id].matched=0` from the moment AddNode commits,
        // so heartbeats with `m.commit=0` cannot trip `commit_to`'s
        // panic.  Witness binaries still derive ID from hostname (see
        // `lib::transport_primitives::hostname_to_node_id`) — they
        // never wipe-rejoin in practice and live at well-known
        // addresses, so the contract doesn't apply there.
        let node_id = read_or_mint_node_id(&zones_dir)?;
        let peers: Vec<String> = peer_addrs
            .iter()
            .map(NodeAddress::to_raft_peer_str)
            .collect();
        let _ = use_tls_for_endpoints; // peer_addrs already carry tls scheme

        let zm = ZoneManager::with_node_id(
            &hostname,
            node_id,
            &zones_dir,
            peers,
            &bind_addr,
            tls,
            Some(self_addr.clone()),
            None,
        )
        .map_err(|e| format!("ZoneManager::with_node_id: {e}"))?;

        let runtime_handle = zm.runtime_handle();
        let blob_slot = zm.blob_fetcher_slot();

        let _ = self.zone_manager.set(zm.clone());
        let _ = self.runtime.set(runtime_handle);

        // Hand the blob-fetcher slot up to the kernel so transport's
        // `install_transport_wiring` can drain it.
        kernel.stash_blob_fetcher_slot(Box::new(blob_slot));

        // Bring root zone online based on the declared mode.
        //
        //   * Static / Restart: `bootstrap_or_join_zone` dispatches —
        //     empty peers + empty storage → 1-voter single-node
        //     default; non-empty peers → joiner retry loop;
        //     persisted state → resume.
        //   * Dynamic: skip — daemon comes up rootless, operator
        //     drives `create_zone` via runtime API.
        //   * `None` (no federation in play): skip — caller did not
        //     ask for federation init (typical for tests / single-
        //     node dev workflows that strip env vars).
        //
        // `max_attempts=None` blocks indefinitely under the joiner
        // branch (no deadline) so misconfig surfaces as "daemon
        // stays up retrying" rather than "daemon exits after timeout".
        match bootstrap_mode {
            Some(BootstrapMode::Static) | Some(BootstrapMode::Restart) => {
                bootstrap_or_join_zone(
                    zm.as_ref(),
                    "root",
                    node_id,
                    &self_addr,
                    &peer_addrs,
                    bootstrap_new,
                    /* max_attempts */ None,
                    /* as_learner   */ false,
                )?;
            }
            Some(BootstrapMode::Dynamic) => {
                tracing::info!(
                    node_id,
                    "bootstrap mode = dynamic; daemon up rootless — operator drives \
                     create_zone via runtime API",
                );
            }
            None => {
                tracing::debug!(
                    node_id,
                    "no federation intent declared — skipping root zone bootstrap",
                );
            }
        }

        // Federation zones listed in `NEXUS_FEDERATION_ZONES` are
        // brought up only when this node bootstrapped root (1-voter
        // owner).  Joiners receive these zones via the standard
        // mount-with-source / share flows once they've joined root —
        // bootstrapping them locally on a joiner would create a
        // duplicate raft group with disjoint state.
        if bootstrap_new {
            if let Ok(zones_csv) = std::env::var("NEXUS_FEDERATION_ZONES") {
                for zone_id in zones_csv
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    if zm.get_zone(zone_id).is_none() {
                        zm.create_zone(zone_id, zm.current_peer_strings())
                            .map_err(|e| format!("create_zone({zone_id}): {e}"))?;
                    }
                }
            }
        }

        // Install the DT_MOUNT apply-cb on every zone the ZoneManager
        // loaded — root, env-listed federation zones, AND zones restored
        // from disk after a restart.  Without this, restored zones lose
        // their wire_mount path on followers and DT_MOUNT replays go
        // unwired.  Idempotent — re-installation replaces with an
        // equivalent closure.
        for zone_id in zm.list_zones() {
            self.install_apply_cb_for_zone(kernel, &zone_id);
        }

        // Replay scan: each restored zone may already hold DT_MOUNT
        // entries in its applied state machine.  The apply-cb only
        // fires on NEW applies, so without this scan a restart leaves
        // restored mounts unwired in VFSRouter / DCache.
        self.replay_existing_mounts(kernel);

        self.bootstrap_done.store(true, Ordering::Release);
        tracing::info!("federation bootstrap complete (hostname={hostname})");
        Ok(true)
    }
}

impl DistributedCoordinator for RaftDistributedCoordinator {
    fn list_zones(&self, _kernel: &Kernel) -> Vec<String> {
        self.zm().map(|zm| zm.list_zones()).unwrap_or_default()
    }

    fn is_initialized(&self, _kernel: &Kernel) -> bool {
        // SSOT — `bootstrap_done` is set at the end of `init_from_env`
        // regardless of whether any zones were bootstrapped.  The
        // default trait impl falls back to `!list_zones().is_empty()`,
        // which is a SHADOW of init readiness that misclassifies
        // dynamic-bootstrap mode (init complete, zones empty until
        // `create_zone("root")` is invoked).  Override it.
        self.bootstrap_done.load(Ordering::Acquire)
    }

    fn metastore_for_zone(
        &self,
        _kernel: &Kernel,
        zone_id: &str,
    ) -> CoordinatorResult<Arc<dyn MetaStore>> {
        let zm = self.zm().ok_or("federation not active")?;
        let consensus = zm
            .registry()
            .get_node(zone_id)
            .ok_or_else(|| format!("zone {zone_id} not loaded"))?;
        let runtime = self.runtime.get().cloned().ok_or("runtime missing")?;
        // Mount point: root zone shows under "/", named zones under "/<id>".
        let mount_point = if zone_id == "root" {
            "/".to_string()
        } else {
            format!("/{zone_id}")
        };
        let store: Arc<dyn MetaStore> = Arc::new(crate::zone_meta_store::ZoneMetaStore::new(
            consensus,
            runtime,
            mount_point,
        ));
        Ok(store)
    }

    fn locks_for_zone(&self, kernel: &Kernel, zone_id: &str) -> CoordinatorResult<Arc<dyn Locks>> {
        let zm = self.zm().ok_or("federation not active")?;
        let runtime = self
            .runtime
            .get()
            .ok_or("federation runtime not initialised")?;
        let consensus = zm
            .registry()
            .get_node(zone_id)
            .ok_or_else(|| format!("zone '{zone_id}' not loaded locally"))?;
        let kernel_state = kernel.lock_manager_arc().advisory_state_arc();
        let backend =
            crate::federation::DistributedLocks::new(consensus, runtime.clone(), kernel_state);
        Ok(Arc::new(backend))
    }

    fn wire_mount(
        &self,
        kernel: &Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> CoordinatorResult<()> {
        wire_mount_impl(self, kernel, parent_zone, mount_path, target_zone)
    }

    fn unwire_mount(
        &self,
        kernel: &Kernel,
        parent_zone: &str,
        mount_path: &str,
    ) -> CoordinatorResult<()> {
        let vfs_router = kernel.vfs_router_arc();
        unwire_mount_core(
            &vfs_router,
            &self.cross_zone_mounts,
            parent_zone,
            mount_path,
        );
        Ok(())
    }

    /// Create or join a federation zone at runtime.
    ///
    /// Coordinated through the root-zone leader to avoid split-brain
    /// under the opaque-ID contract — random data-plane node IDs make
    /// hostname-derived ConfState convergence (the old contract's
    /// implicit serializer) impossible.
    ///
    ///   - **Root-zone leader**: founder.  Creates `zone_id` as a
    ///     1-voter cluster locally.  Subsequent JoinZone calls from
    ///     followers grow the voter set.
    ///   - **Root-zone follower**: joiner.  Loops `JoinZone` RPC at
    ///     the root-leader's address (and other peers as a fallback)
    ///     until the leader has created the zone and the AddNode
    ///     for this node commits.  Bounded by 30s so syscall
    ///     callers see a fail rather than hang forever; operators
    ///     re-issue.
    #[allow(clippy::result_large_err)]
    fn create_zone(&self, kernel: &Kernel, zone_id: &str) -> CoordinatorResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        if zm.get_zone(zone_id).is_some() {
            self.install_apply_cb_for_zone(kernel, zone_id);
            return Ok(());
        }
        let runtime = self
            .runtime
            .get()
            .ok_or("federation runtime not initialised")?;
        let self_id = zm.node_id();
        let registry = zm.registry();
        let self_address = registry.self_address();

        // Root-leader path: founder creates 1-voter cluster locally.
        let am_root_leader = zm.get_zone("root").map(|h| h.is_leader()).unwrap_or(false);
        if am_root_leader {
            let self_peer = format!("{self_id}@{self_address}");
            zm.create_zone(zone_id, vec![self_peer])
                .map_err(|e| format!("create_zone({zone_id}): {e}"))?;
            self.install_apply_cb_for_zone(kernel, zone_id);

            // 1-voter zone — campaign immediately so this node is the
            // leader for the AddNode proposals below.  Without an
            // explicit campaign, raft-rs waits a full election_tick
            // (~100 ms) before self-voting, and any propose_conf_change
            // in that window returns NotLeader.
            let zone_handle = zm
                .get_zone(zone_id)
                .ok_or_else(|| format!("create_zone({zone_id}): just-created zone not visible"))?;
            let consensus = zone_handle.consensus_node();
            let campaign_fut = consensus.campaign();
            let campaign_result = if tokio::runtime::Handle::try_current().is_ok() {
                tokio::task::block_in_place(|| runtime.block_on(campaign_fut))
            } else {
                runtime.block_on(campaign_fut)
            };
            campaign_result.map_err(|e| format!("create_zone({zone_id}) campaign: {e}"))?;

            // Auto-invite witness voters present in NEXUS_PEERS.  The
            // witness's id is hostname-derived (well-known per F3) so
            // the leader can address it directly without waiting for
            // JoinZone.  Without this, witness never receives raft
            // traffic for new federation zones (its ConfState lacks
            // them) and the cluster loses witness's quorum-1
            // protection on every dynamically-created zone.
            //
            // If the witness is unreachable at create time the AddNode
            // still commits (1-voter quorum on this leader) but the
            // resulting 2-voter ConfState raises quorum-required to
            // 2/2 until witness comes up — same exposure as the OLD
            // hostname-deterministic ConfState bootstrap, operationally
            // equivalent.
            let root_peers = registry.get_peers("root").unwrap_or_default();
            for peer in root_peers.values() {
                if !peer.hostname.to_ascii_lowercase().starts_with("witness") {
                    continue;
                }
                let address_bytes = peer.endpoint.as_bytes().to_vec();
                let fut = consensus.propose_conf_change(
                    raft::eraftpb::ConfChangeType::AddNode,
                    peer.id,
                    address_bytes,
                );
                let result = if tokio::runtime::Handle::try_current().is_ok() {
                    tokio::task::block_in_place(|| runtime.block_on(fut))
                } else {
                    runtime.block_on(fut)
                };
                if let Err(e) = result {
                    tracing::warn!(
                        zone = %zone_id,
                        witness_id = peer.id,
                        endpoint = %peer.endpoint,
                        error = %e,
                        "auto-invite witness AddNode failed; cluster proceeds 1-voter",
                    );
                } else {
                    tracing::info!(
                        zone = %zone_id,
                        witness_id = peer.id,
                        endpoint = %peer.endpoint,
                        "Witness auto-invited as voter on federation zone create",
                    );
                }
            }
            return Ok(());
        }

        // Follower path: JoinZone via peers.  Address book comes from
        // root zone's peer map (populated via inbound StepMessage).
        let peer_addrs = registry.get_peers("root").unwrap_or_default();
        if peer_addrs.is_empty() {
            return Err(format!(
                "create_zone({zone_id}): not root-leader and no peers known",
            ));
        }

        let deadline = Instant::now() + Duration::from_secs(30);
        let mut last_err = String::new();
        while Instant::now() < deadline {
            // Re-resolve root leader on each iteration — it may emerge
            // (election) or change (failover) during retry.
            let leader_id = zm.get_zone("root").and_then(|h| h.leader_id()).unwrap_or(0);
            let candidates: Vec<NodeAddress> = if leader_id != 0 {
                peer_addrs.get(&leader_id).into_iter().cloned().collect()
            } else {
                peer_addrs.values().cloned().collect()
            };

            // Register the zone locally with skip_bootstrap=true so
            // the leader's snapshot can install the authoritative
            // ConfState the moment AddNode commits.  Idempotent.
            if zm.get_zone(zone_id).is_none() {
                let peer_seed_addrs: Vec<NodeAddress> = peer_addrs.values().cloned().collect();
                let local_peer_seeds = joiner_local_zone_peer_seeds(&peer_seed_addrs);
                if !local_peer_seeds.is_empty() {
                    tracing::info!(
                        zone = %zone_id,
                        seed_count = local_peer_seeds.len(),
                        seed_peers = ?local_peer_seeds,
                        "local federation zone seed peers",
                    );
                    let _ = zm.join_zone(zone_id, local_peer_seeds, false);
                }
            }

            for peer in &candidates {
                if peer.id == self_id {
                    continue;
                }
                let fut = crate::transport::call_join_zone_rpc(
                    &peer.endpoint,
                    zone_id,
                    self_id,
                    &self_address,
                    /* as_learner */ false,
                    5,
                );
                let attempt = if tokio::runtime::Handle::try_current().is_ok() {
                    tokio::task::block_in_place(|| runtime.block_on(fut).map_err(|e| e.to_string()))
                } else {
                    runtime.block_on(fut).map_err(|e| e.to_string())
                };
                match attempt {
                    Ok(r) if r.success => {
                        self.install_apply_cb_for_zone(kernel, zone_id);
                        return Ok(());
                    }
                    Ok(r) => last_err = format!("{}: {:?}", peer.endpoint, r.error),
                    Err(e) => last_err = format!("{}: {e}", peer.endpoint),
                }
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        Err(format!(
            "create_zone({zone_id}): JoinZone exhausted after 30s: {last_err}"
        ))
    }

    fn remove_zone(&self, _kernel: &Kernel, zone_id: &str, force: bool) -> CoordinatorResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        let runtime = self
            .runtime
            .get()
            .ok_or("federation runtime not initialised")?;
        // Cascade-unmount every DT_MOUNT pointing at `zone_id` BEFORE
        // dropping the consensus.  Apply-cb on each parent zone fires
        // `unwire_mount_core` so VFSRouter / DCache cleanup propagates
        // to every peer via raft.  Without this, parents keep stale
        // routing entries and reads under the dead mount-point silently
        // succeed against the (now-orphaned) target consensus Arc.
        let mounts = self
            .cross_zone_mounts
            .get(zone_id)
            .map(|v| v.clone())
            .unwrap_or_default();
        tracing::debug!(
            zone_id = %zone_id,
            force = force,
            mount_count = mounts.len(),
            "remove_zone cascade-unmount entry"
        );
        for (parent_zone_id, mount_path, _global) in &mounts {
            if let Some(parent) = zm.registry().get_node(parent_zone_id) {
                if let Err(e) =
                    crate::zone_manager::propose_delete_metadata(runtime, &parent, mount_path)
                {
                    if !force {
                        return Err(format!(
                            "cascade-unmount {parent_zone_id}:{mount_path} failed: {e}"
                        ));
                    }
                    tracing::warn!(
                        parent = %parent_zone_id,
                        mount = %mount_path,
                        error = %e,
                        "remove_zone(force=true): DT_MOUNT delete propose failed; continuing"
                    );
                }
            }
        }
        zm.remove_zone(zone_id, force).map_err(|e| e.to_string())
    }

    fn join_zone(&self, kernel: &Kernel, zone_id: &str, as_learner: bool) -> CoordinatorResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        let peers = zm.current_peer_strings();
        zm.join_zone(zone_id, peers, as_learner)
            .map_err(|e| e.to_string())?;
        self.install_apply_cb_for_zone(kernel, zone_id);
        Ok(())
    }

    fn join_cluster(
        &self,
        kernel: &Kernel,
        zone_id: &str,
        leader_addr: &str,
        as_learner: bool,
    ) -> CoordinatorResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        let runtime = self
            .runtime
            .get()
            .ok_or("federation runtime not initialised")?;
        if leader_addr.trim().is_empty() {
            return Err("join_cluster: leader_addr must not be empty".to_string());
        }

        // Step 1: set up local raft replica with skip_bootstrap=true so the
        // leader's snapshot is the authoritative ConfState source.  We seed
        // the address book with the leader so outgoing AppendEntries acks
        // and vote responses can reach it from the moment the snapshot
        // installs.
        let leader_peer = vec![leader_addr.to_string()];
        if zm.get_zone(zone_id).is_none() {
            zm.join_zone(zone_id, leader_peer, as_learner)
                .map_err(|e| format!("join_cluster local setup: {e}"))?;
        }

        // Step 2: send JoinZone RPC to the leader.  Followers self-redirect
        // via JoinZoneResponse.leader_address; we follow the redirect once
        // before surfacing the failure.
        let self_id = zm.node_id();
        let self_address = kernel
            .self_address_string()
            .ok_or("join_cluster: self_address not published — federation not initialised")?;
        let mut endpoint = leader_addr.to_string();
        let mut redirected_once = false;
        loop {
            let attempt = runtime.block_on(crate::transport::call_join_zone_rpc(
                &endpoint,
                zone_id,
                self_id,
                &self_address,
                as_learner,
                10,
            ));
            match attempt {
                Ok(result) if result.success => {
                    tracing::info!(
                        zone_id = %zone_id,
                        endpoint = %endpoint,
                        self_id = self_id,
                        "join_cluster: leader committed ConfChangeV2 AddNode"
                    );
                    self.install_apply_cb_for_zone(kernel, zone_id);
                    return Ok(());
                }
                Ok(result) => {
                    if let Some(addr) = result.leader_address.as_ref() {
                        if !redirected_once && !addr.is_empty() && addr != &endpoint {
                            endpoint = addr.clone();
                            redirected_once = true;
                            continue;
                        }
                    }
                    return Err(format!(
                        "join_cluster: peer {endpoint} rejected — error={:?}",
                        result.error
                    ));
                }
                Err(e) => {
                    return Err(format!("join_cluster: RPC to {endpoint} failed: {e}"));
                }
            }
        }
    }

    fn share_zone(
        &self,
        kernel: &Kernel,
        local_path: &str,
        new_zone_id: &str,
    ) -> CoordinatorResult<ShareInfo> {
        let zm = self.zm().ok_or("federation not active")?;
        // Atomic create + copy + register: materialise the zone first
        // so it is visible to followers before content lands.
        zm.get_or_create_zone(new_zone_id)
            .map_err(|e| e.to_string())?;
        self.install_apply_cb_for_zone(kernel, new_zone_id);
        // Decompose `local_path` via VFSRouter — the closest mount
        // point's zone_id is the parent, and the path tail under that
        // mount is the prefix passed to `share_subtree_core`.
        let route = kernel
            .vfs_router_arc()
            .route(local_path, contracts::ROOT_ZONE_ID)
            .ok_or_else(|| format!("share_zone route '{local_path}': no mount covers path"))?;
        let parent_zone = route.zone_id.clone();
        let prefix = if route.backend_path.is_empty() {
            "/".to_string()
        } else {
            format!("/{}", route.backend_path)
        };
        let copied = zm
            .share_subtree_core(&parent_zone, &prefix, new_zone_id)
            .map_err(|e| e.to_string())?;
        zm.register_share(local_path, new_zone_id)
            .map_err(|e| e.to_string())?;
        Ok(ShareInfo {
            zone_id: new_zone_id.to_string(),
            copied_entries: copied as u64,
        })
    }

    fn lookup_share(
        &self,
        _kernel: &Kernel,
        remote_path: &str,
    ) -> CoordinatorResult<Option<ShareInfo>> {
        let zm = self.zm().ok_or("federation not active")?;
        let zone_id = zm.lookup_share(remote_path).map_err(|e| e.to_string())?;
        Ok(zone_id.map(|zid| ShareInfo {
            zone_id: zid,
            copied_entries: 0,
        }))
    }

    fn cluster_info(&self, _kernel: &Kernel, zone_id: &str) -> CoordinatorResult<ClusterInfo> {
        let zm = self.zm().ok_or("federation not active")?;
        let status = zm.cluster_status(zone_id);
        // links_count comes from the coordinator's reverse index
        // (mounts pointing at `zone_id`). Node-local cache derived
        // from DT_MOUNT entries — matches what `wire_mount` populates
        // as apply-cb fires.
        let links_count = self
            .cross_zone_mounts
            .get(zone_id)
            .map(|v| v.len() as i64)
            .unwrap_or(0);
        Ok(ClusterInfo {
            zone_id: status.zone_id,
            node_id: status.node_id,
            has_store: status.has_store,
            is_leader: status.is_leader,
            leader_id: status.leader_id,
            term: status.term,
            commit_index: status.commit_index,
            applied_index: status.applied_index,
            voter_count: status.voter_count,
            witness_count: status.witness_count,
            links_count,
        })
    }
}

/// Reconstruct the global VFS path for a DT_MOUNT entry.  Root-zone parents
/// already publish a global path; nested mounts pre-pend the parent's own
/// global path looked up via `cross_zone_mounts`.
fn reconstruct_global_path(
    cross_zone_mounts: &DashMap<String, Vec<CrossZoneMountTuple>>,
    parent_zone_id: &str,
    mount_path: &str,
) -> Option<String> {
    if parent_zone_id == contracts::ROOT_ZONE_ID || parent_zone_id.is_empty() {
        return Some(mount_path.to_string());
    }
    let parent_global = cross_zone_mounts
        .get(parent_zone_id)
        .and_then(|v| v.iter().map(|(_, _, g)| g.clone()).min())?;
    if mount_path == parent_global || mount_path.starts_with(&format!("{}/", parent_global)) {
        Some(mount_path.to_string())
    } else if mount_path == "/" {
        Some(parent_global)
    } else {
        Some(format!("{}{}", parent_global, mount_path))
    }
}

// `install_dcache_coherence_impl` is gone — migrated to
// `Kernel::install_zone_apply_invalidator`.  The closure body
// (translate zone-relative key → every global mount point → evict
// dcache) lives kernel-side now so federation no longer holds
// `Arc<DCache>`.

/// Kernel-built dcache mutation helpers, threaded into the federation
/// `&Kernel`-free core of `wire_mount` — same body, but every kernel
/// dependency comes through pre-cloned `Arc`s.  Lets the apply-cb
/// closure (which has no `&Kernel` access) drive the same logic on
/// every follower when raft applies a DT_MOUNT commit.
#[allow(clippy::too_many_arguments)]
fn wire_mount_core(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    lock_manager: &Arc<kernel::core::lock::LockManager>,
    registry: &Arc<crate::raft::ZoneRaftRegistry>,
    runtime: &tokio::runtime::Handle,
    cross_zone_mounts: &DashMap<String, Vec<CrossZoneMountTuple>>,
    parent_zone_id: &str,
    mount_path: &str,
    target_zone_id: &str,
) -> CoordinatorResult<()> {
    tracing::debug!(
        parent_zone_id = %parent_zone_id,
        mount_path = %mount_path,
        target_zone_id = %target_zone_id,
        "wire_mount_core entered"
    );

    // 1. Look up target zone.
    let Some(target_consensus) = registry.get_node(target_zone_id) else {
        tracing::warn!(
            target_zone_id = %target_zone_id,
            "wire_mount: target zone not loaded locally — deferring"
        );
        return Ok(());
    };

    // 2. Reconstruct the global VFS path.
    let global_path = match reconstruct_global_path(cross_zone_mounts, parent_zone_id, mount_path) {
        Some(g) => g,
        None => {
            tracing::warn!(
                parent_zone_id = %parent_zone_id,
                mount_path = %mount_path,
                "wire_mount: reconstruct_global_path returned None"
            );
            return Ok(());
        }
    };

    // 3. Build a ZoneMetaStore rooted at global_path against the target's
    //    state machine — reuses the root mount's CAS backend.
    let metastore: Arc<dyn MetaStore> = ZoneMetaStore::new_arc(
        target_consensus.clone(),
        runtime.clone(),
        global_path.clone(),
    );
    let root_canonical = canonicalize("/", contracts::ROOT_ZONE_ID);
    let root_backend = vfs_router
        .get_canonical(&root_canonical)
        .and_then(|e| e.backend.clone());

    // 4. Install into VFSRouter under the root zone.
    vfs_router.add_federation_mount(
        &global_path,
        contracts::ROOT_ZONE_ID,
        root_backend,
        target_zone_id,
        false,
    );
    let canonical = canonicalize(&global_path, contracts::ROOT_ZONE_ID);
    vfs_router.install_metastore(&canonical, metastore);

    // 5. LockManager upgrade on first federated mount — distributed
    //    locks bound to the ROOT zone's consensus.
    if !lock_manager.locks_installed() {
        match registry.get_node(contracts::ROOT_ZONE_ID) {
            Some(root_consensus) => {
                tracing::info!(
                    parent_zone = %parent_zone_id,
                    mount_path = %mount_path,
                    "wire_mount: installing distributed locks bound to ROOT zone"
                );
                let kernel_state = lock_manager.advisory_state_arc();
                let backend = crate::federation::DistributedLocks::new(
                    root_consensus,
                    runtime.clone(),
                    kernel_state,
                );
                lock_manager.install_locks(Arc::new(backend));
            }
            None => {
                tracing::warn!(
                    "wire_mount: root zone not loaded — distributed locks NOT installed; sys_lock stays local-only until next mount"
                );
            }
        }
    }

    // 6. DT_MOUNT mount-point synthesis: ``sys_stat`` /``sys_unlink``
    // synthesise a DT_MOUNT result directly from the routing structure
    // (kernel/io.rs); no kernel-side cache row needs seeding here.
    //
    // 7. Apply-side cache coherence: each ZoneMetaStore self-registers
    // an invalidator on its consensus during ``ZoneMetaStore::new``
    // (raft/zone_meta_store.rs), so installing one here would just
    // duplicate the registration.

    // 8. Update reverse index.
    let mut bucket = cross_zone_mounts
        .entry(target_zone_id.to_string())
        .or_default();
    let tuple = (
        parent_zone_id.to_string(),
        mount_path.to_string(),
        global_path,
    );
    if !bucket.contains(&tuple) {
        bucket.push(tuple);
    }
    Ok(())
}

/// Reverse the bookkeeping done by `wire_mount_core` for a DT_MOUNT
/// delete event: drop the VFSRouter slot and remove the reverse-index
/// entry. Cache eviction for the unwired mount happens automatically:
/// ``vfs_router.remove`` drops the per-mount ``Arc<dyn MetaStore>``
/// (its internal ``DashMap`` cache goes with it), and the unwired
/// mount no longer routes so subsequent ``sys_stat`` synthesises the
/// absence of a DT_MOUNT directly from the empty routing entry.
fn unwire_mount_core(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    cross_zone_mounts: &DashMap<String, Vec<CrossZoneMountTuple>>,
    parent_zone_id: &str,
    mount_path: &str,
) {
    tracing::debug!(parent_zone_id = %parent_zone_id, mount_path = %mount_path, "unwire_mount_core entered");
    let mut remove_empty: Option<String> = None;
    let mut unwired_global: Option<String> = None;
    for mut entry in cross_zone_mounts.iter_mut() {
        let bucket = entry.value_mut();
        if let Some(pos) = bucket
            .iter()
            .position(|(p, m, _)| p == parent_zone_id && m == mount_path)
        {
            let (_, _, global) = bucket.remove(pos);
            unwired_global = Some(global);
            if bucket.is_empty() {
                remove_empty = Some(entry.key().clone());
            }
            break;
        }
    }
    if let Some(target) = remove_empty {
        cross_zone_mounts.remove(&target);
    }
    if let Some(global) = unwired_global {
        vfs_router.remove(&global, contracts::ROOT_ZONE_ID);
    }
}

/// Install the apply-side DT_MOUNT callback on `consensus` so every
/// raft-replicated DT_MOUNT commit drives `wire_mount_core` /
/// `unwire_mount_core` — the mechanism that keeps cross-zone routing
/// in sync on **every** follower (not just the leader that handled
/// the original `sys_setattr`).
#[allow(clippy::too_many_arguments)]
fn install_mount_apply_cb_impl(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    lock_manager: &Arc<kernel::core::lock::LockManager>,
    registry: &Arc<crate::raft::ZoneRaftRegistry>,
    runtime: &tokio::runtime::Handle,
    cross_zone_mounts: &Arc<DashMap<String, Vec<CrossZoneMountTuple>>>,
    parent_zone_id: &str,
    consensus: &crate::raft::ZoneConsensus<crate::raft::FullStateMachine>,
) {
    let Some(slot) = consensus.mount_apply_cb_slot() else {
        tracing::warn!(parent_zone_id = %parent_zone_id, "install_mount_apply_cb: slot returned None");
        return;
    };
    let vfs_router = Arc::clone(vfs_router);
    let lock_manager = Arc::clone(lock_manager);
    let registry = Arc::clone(registry);
    let runtime = runtime.clone();
    let cross_zone_mounts = Arc::clone(cross_zone_mounts);
    let parent_zone_owned = parent_zone_id.to_string();

    use crate::raft::MountApplyEvent;
    let cb: Arc<dyn Fn(&MountApplyEvent) + Send + Sync> =
        Arc::new(move |event: &MountApplyEvent| match event {
            MountApplyEvent::Set {
                key,
                target_zone_id,
            } => {
                let _ = wire_mount_core(
                    &vfs_router,
                    &lock_manager,
                    &registry,
                    &runtime,
                    &cross_zone_mounts,
                    &parent_zone_owned,
                    key,
                    target_zone_id,
                );
            }
            MountApplyEvent::Delete { key } => {
                unwire_mount_core(&vfs_router, &cross_zone_mounts, &parent_zone_owned, key);
            }
        });
    *slot.write() = Some(cb);
    tracing::info!(parent_zone_id = %parent_zone_id, "install_mount_apply_cb: slot set");
}

/// Wire a federation mount synchronously from the leader's
/// `sys_setattr` path.  Followers reach the same logic through the
/// `mount_apply_cb` installed by `install_mount_apply_cb_impl` —
/// kernel.rs's `wire_mount` call is best-effort fast-path; correctness
/// rests on the apply-cb.
fn wire_mount_impl(
    provider: &RaftDistributedCoordinator,
    kernel: &Kernel,
    parent_zone_id: &str,
    mount_path: &str,
    target_zone_id: &str,
) -> CoordinatorResult<()> {
    let zm = provider.zm().ok_or("federation not active")?;
    let runtime = provider
        .runtime
        .get()
        .ok_or("federation runtime not initialised")?;
    let registry = zm.registry();
    let vfs_router = kernel.vfs_router_arc();
    let lock_manager = kernel.lock_manager_arc();

    // Quorum-replication contract: a DT_MOUNT entry committed on the
    // parent zone is observed by every voter via raft replication, so
    // every peer's apply-cb fires here.  If the target zone's raft
    // replica isn't loaded locally yet, create it now using the same
    // peer roster as root — every peer derives the identical
    // ConfState seed and the new zone's raft group converges on a
    // leader once a quorum of peers have run through this path.
    //
    // Without this auto-create, only the originating peer's local
    // sys_setattr ever instantiates the target zone; followers stay
    // empty, the new zone's voter set has no quorum, and `cluster_info`
    // never reports a leader_id — the symptom that broke
    // `_wait_zone_ready` in the federation E2E suite.
    if registry.get_node(target_zone_id).is_none() {
        let zone_peers = zm.current_peer_strings();
        if !zone_peers.is_empty() {
            tracing::info!(
                target_zone_id = %target_zone_id,
                peers = ?zone_peers,
                "wire_mount: target zone not loaded locally — auto-creating raft replica"
            );
            if let Err(e) = zm.create_zone(target_zone_id, zone_peers) {
                tracing::warn!(
                    target_zone_id = %target_zone_id,
                    error = %e,
                    "wire_mount: target zone auto-create failed; wire_mount_core will defer"
                );
            } else {
                provider.install_apply_cb_for_zone(kernel, target_zone_id);
            }
        }
    }

    wire_mount_core(
        &vfs_router,
        &lock_manager,
        &registry,
        runtime,
        &provider.cross_zone_mounts,
        parent_zone_id,
        mount_path,
        target_zone_id,
    )?;

    // Best-effort: also install the apply-cb on the parent zone so future
    // DT_MOUNT commits (this one or later) on every follower fire
    // `wire_mount_core`.  Idempotent — re-installing replaces the closure
    // with an equivalent one.
    provider.install_apply_cb_for_zone(kernel, parent_zone_id);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn read_or_mint_node_id_mints_then_loads() {
        // Round-trip: first call mints + persists, second call returns
        // the same value from disk.  Pin the file format (8 bytes BE).
        let dir = TempDir::new().expect("tempdir");
        let path = dir.path().to_str().expect("utf-8");
        let id1 = read_or_mint_node_id(path).expect("first mint");
        assert_ne!(id1, 0, "minted id must be non-zero");
        let id2 = read_or_mint_node_id(path).expect("second load");
        assert_eq!(id1, id2, "load must return persisted id");

        // Sanity: file is exactly 8 bytes BE u64.
        let bytes = std::fs::read(dir.path().join(NODE_ID_FILE)).expect("read");
        assert_eq!(bytes.len(), 8, ".node_id must be 8 bytes");
        let parsed = u64::from_be_bytes(bytes.try_into().expect("array"));
        assert_eq!(parsed, id1);
    }

    #[test]
    fn read_or_mint_node_id_rejects_zero_on_disk() {
        // Pre-write 0 — operator panic-recover scenario, surface
        // loudly rather than silently re-minting (which would change
        // the cluster's view of this node's identity).
        let dir = TempDir::new().expect("tempdir");
        let path = dir.path().join(NODE_ID_FILE);
        std::fs::write(&path, [0u8; 8]).expect("write zero");
        let result = read_or_mint_node_id(dir.path().to_str().expect("utf-8"));
        assert!(result.is_err(), "must reject zero id on disk");
    }

    fn parse_peer(s: &str) -> NodeAddress {
        NodeAddress::parse(s, /* use_tls */ false).expect("parse peer")
    }

    #[test]
    fn validate_peers_excludes_self_accepts_other_peers_only() {
        // Address book lists OTHER nodes — happy path under the
        // PR #3996 contract.
        let peers = vec![parse_peer("100.64.0.21:2126")];
        validate_peers_excludes_self(&peers, "100.64.0.26:2126")
            .expect("other-peers-only must be accepted");
    }

    #[test]
    fn validate_peers_excludes_self_rejects_self_in_list() {
        // Operator pasted self into NEXUS_PEERS — fail-loud at parse
        // time rather than letting the JoinZone retry loop stall on
        // a self-RPC that never gets a leader.
        let peers = vec![
            parse_peer("100.64.0.26:2126"),
            parse_peer("100.64.0.21:2126"),
        ];
        let err = validate_peers_excludes_self(&peers, "100.64.0.26:2126")
            .expect_err("self-in-peers must be rejected");
        assert!(
            err.contains("contains self"),
            "error must name the contract violation, got: {err}",
        );
    }

    #[test]
    fn validate_peers_excludes_self_handles_explicit_id_prefix() {
        // `id@host:port` form: same self-detection logic must apply.
        let peers = vec![parse_peer("9999@100.64.0.26:2126")];
        let err = validate_peers_excludes_self(&peers, "100.64.0.26:2126")
            .expect_err("self-in-peers must be rejected even with explicit id prefix");
        assert!(err.contains("contains self"));
    }

    #[test]
    fn validate_peers_excludes_self_empty_list_is_ok() {
        // Founder mode with no other peers yet.
        validate_peers_excludes_self(&[], "100.64.0.26:2126").expect("empty list ok");
    }

    #[test]
    fn bootstrap_joiner_local_seed_includes_all_configured_peers() {
        let peers = vec![
            parse_peer("nexus-1:2126"),
            parse_peer("witness:2126"),
            parse_peer("nexus-3:2126"),
        ];

        let seeds = joiner_local_zone_peer_seeds(&peers);

        let mut expected = vec![
            parse_peer("nexus-1:2126").to_raft_peer_str(),
            parse_peer("nexus-3:2126").to_raft_peer_str(),
            parse_peer("witness:2126").to_raft_peer_str(),
        ];
        expected.sort();

        assert_eq!(
            seeds, expected,
            "joiner local root registration must seed the full address book, not just the dialed peer",
        );
    }

    #[test]
    fn founder_with_peers_probes_existing_cluster_before_creating() {
        let peers = vec![parse_peer("nexus-2:2126"), parse_peer("witness:2126")];

        assert_eq!(
            empty_storage_bootstrap_plan(true, &peers),
            EmptyStorageBootstrapPlan::ProbePeersThenFound,
            "a wiped static founder with live peers must try to rejoin before founding a split-brain root",
        );
        assert_eq!(
            empty_storage_bootstrap_plan(true, &[]),
            EmptyStorageBootstrapPlan::FoundImmediately,
            "single-node founder still creates immediately",
        );
        assert_eq!(
            empty_storage_bootstrap_plan(false, &peers),
            EmptyStorageBootstrapPlan::JoinPeers,
            "joiners keep the indefinite JoinZone retry behavior",
        );
    }

    #[test]
    fn join_retry_loop_stops_after_successful_join() {
        let mut attempts = 3;

        let joined = record_join_attempt(Ok(()), &mut attempts, None, "root").unwrap();

        assert!(joined, "successful JoinZone must break the retry loop");
        assert_eq!(
            attempts, 3,
            "successful JoinZone must not count as a failed retry",
        );
    }

    #[test]
    fn join_retry_loop_counts_failures_and_honors_max_attempts() {
        let mut attempts = 0;

        let joined = record_join_attempt(
            Err("leader unavailable".to_string()),
            &mut attempts,
            Some(2),
            "root",
        )
        .unwrap();
        assert!(!joined);
        assert_eq!(attempts, 1);

        let err = record_join_attempt(
            Err("leader still unavailable".to_string()),
            &mut attempts,
            Some(2),
            "root",
        )
        .expect_err("second failed attempt should hit max_attempts");

        assert!(err.contains("after 2 attempts"));
        assert!(err.contains("leader still unavailable"));
    }

    // ── BootstrapMode parsing + validation ─────────────────────────────

    #[test]
    fn bootstrap_mode_parse_accepts_canonical_names() {
        assert_eq!(
            BootstrapMode::parse("static").unwrap(),
            BootstrapMode::Static
        );
        assert_eq!(
            BootstrapMode::parse("dynamic").unwrap(),
            BootstrapMode::Dynamic
        );
        assert_eq!(
            BootstrapMode::parse("restart").unwrap(),
            BootstrapMode::Restart
        );
        // Case-insensitive
        assert_eq!(
            BootstrapMode::parse("STATIC").unwrap(),
            BootstrapMode::Static
        );
        // Trims whitespace
        assert_eq!(
            BootstrapMode::parse("  dynamic  ").unwrap(),
            BootstrapMode::Dynamic
        );
    }

    #[test]
    fn bootstrap_mode_parse_rejects_unknown() {
        let err = BootstrapMode::parse("auto").expect_err("unknown mode rejected");
        assert!(err.contains("static, dynamic, restart"));
    }

    #[test]
    fn validate_static_happy_paths() {
        // Single-node default: empty/empty — every cluster starts as
        // a 1-voter group, so this is the natural single-node UX.
        validate_bootstrap_mode(BootstrapMode::Static, false, false, false)
            .expect("single-node default ok");
        // Founder with explicit BOOTSTRAP_NEW (redundant under
        // empty peers but a legal documented alias).
        validate_bootstrap_mode(BootstrapMode::Static, false, true, false).expect("founder ok");
        // Joiner: peers non-empty, BOOTSTRAP_NEW unset, data dir empty.
        validate_bootstrap_mode(BootstrapMode::Static, false, false, true).expect("joiner ok");
        // Belt-and-suspenders: both set is OK (founder with peer hint).
        validate_bootstrap_mode(BootstrapMode::Static, false, true, true).expect("both ok");
    }

    #[test]
    fn validate_static_allows_existing_state_for_container_restart() {
        validate_bootstrap_mode(BootstrapMode::Static, true, true, true)
            .expect("static bootstrap with persisted state resumes after container restart");
    }

    #[test]
    fn validate_dynamic_happy_path() {
        // Pure dynamic: empty state, no flags.
        validate_bootstrap_mode(BootstrapMode::Dynamic, false, false, false)
            .expect("clean dynamic ok");
    }

    #[test]
    fn validate_dynamic_rejects_existing_state() {
        let err = validate_bootstrap_mode(BootstrapMode::Dynamic, true, false, false)
            .expect_err("dynamic on existing state must be rejected");
        assert!(err.contains("dynamic mode requires a fresh data dir"));
    }

    #[test]
    fn validate_dynamic_rejects_bootstrap_new() {
        let err = validate_bootstrap_mode(BootstrapMode::Dynamic, false, true, false)
            .expect_err("dynamic + BOOTSTRAP_NEW must be rejected");
        assert!(err.contains("forbids NEXUS_BOOTSTRAP_NEW"));
    }

    #[test]
    fn validate_dynamic_rejects_peers() {
        let err = validate_bootstrap_mode(BootstrapMode::Dynamic, false, false, true)
            .expect_err("dynamic + peers must be rejected");
        assert!(err.contains("forbids NEXUS_PEERS"));
    }

    #[test]
    fn validate_restart_happy_path() {
        validate_bootstrap_mode(BootstrapMode::Restart, true, false, false)
            .expect("restart with state ok");
    }

    #[test]
    fn validate_restart_rejects_empty_state() {
        let err = validate_bootstrap_mode(BootstrapMode::Restart, false, false, false)
            .expect_err("restart on empty state must be rejected");
        assert!(err.contains("data dir is empty"));
    }

    #[test]
    fn validate_restart_rejects_bootstrap_new() {
        let err = validate_bootstrap_mode(BootstrapMode::Restart, true, true, false)
            .expect_err("restart + BOOTSTRAP_NEW must be rejected");
        assert!(err.contains("forbids NEXUS_BOOTSTRAP_NEW"));
    }

    #[test]
    fn validate_restart_rejects_peers() {
        let err = validate_bootstrap_mode(BootstrapMode::Restart, true, false, true)
            .expect_err("restart + peers must be rejected");
        assert!(err.contains("forbids NEXUS_PEERS"));
    }
}
