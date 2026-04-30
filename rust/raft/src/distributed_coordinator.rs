//! Concrete `DistributedCoordinator` implementation.
//!
//! `RaftDistributedCoordinator` is the raft-crate impl of the
//! `DistributedCoordinator` trait the kernel exposes. The Cargo edge
//! runs `raft → kernel`; the kernel installs an
//! `Arc<dyn DistributedCoordinator>` into its `federation` slot via the
//! cdylib boot path, and federation-aware syscalls dispatch through the
//! trait.
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

use contracts::lock_state::Locks;
use dashmap::DashMap;
use kernel::abc::meta_store::MetaStore;
use kernel::core::dcache::CachedEntry;
use kernel::core::vfs_router::canonicalize_mount_path as canonicalize;
use kernel::hal::distributed_coordinator::{
    ClusterInfo, CoordinatorResult, DistributedCoordinator, ShareInfo,
};
use kernel::kernel::Kernel;

use crate::transport::{
    call_replace_voter_by_hostname, compute_node_id, hostname_to_node_id, NodeAddress,
    TransportError,
};
use crate::zone_meta_store::ZoneMetaStore;
use crate::{TlsFiles, ZoneManager};

/// Node-level incarnation marker filename.
///
/// Lives at `{NEXUS_DATA_DIR}/.node_incarnation` — one file per
/// daemon, not per zone.  Identity is a node-level concept (one
/// `ZoneManager` owns one `node_id` for every zone it serves), so
/// the SSOT is also node-level.  Format: a single big-endian u64 in
/// 8 bytes.  Absent file = fresh daemon (first boot or post-wipe);
/// present file = recovery (existing identity).
///
/// Stored alongside `<zone>/raft/raft.redb` so a `rm -rf
/// $NEXUS_DATA_DIR` resets identity AND raft state together — the
/// only state-pair that matters for the wipe-rejoin contract.  Using
/// a flat file (not redb) avoids the `open_existing_zones_from_disk`
/// confusion: that scanner enumerates `<zones_dir>/<zone>/raft/`
/// dirs, so creating a fake "incarnation zone" with raft.redb would
/// trigger spurious zone bootstrap with skip_bootstrap=true and a
/// missing ConfState, blocking leader election forever.
const NODE_INCARNATION_FILE: &str = ".node_incarnation";

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
        let dcache = kernel.dcache_arc();
        let lock_manager = kernel.lock_manager_arc();
        install_mount_apply_cb_impl(
            &vfs_router,
            &dcache,
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
        let dcache = kernel.dcache_arc();
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
                    &dcache,
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

/// Parse `NEXUS_PEERS` value (`host:port,host:port`) into raft's
/// `id@host:port` format using `hostname_to_node_id` for stable ids.
fn parse_peer_list_to_raft_format(peers_csv: &str) -> Result<Vec<String>, String> {
    let mut out = Vec::new();
    for entry in peers_csv
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let (host, _port) = entry
            .rsplit_once(':')
            .ok_or_else(|| format!("peer '{entry}' missing ':port'"))?;
        let id = hostname_to_node_id(host);
        out.push(format!("{id}@{entry}"));
    }
    Ok(out)
}

/// Outcome of [`RaftDistributedCoordinator::ensure_voter_membership`].
///
/// Drives both the ID `ZoneManager::with_node_id` is constructed with
/// and whether `init_from_env` chooses `create_zone` (cold start) or
/// `join_zone` (joining an already-running cluster after a wipe).
#[derive(Debug, Clone, Copy)]
struct VoterMembership {
    /// Effective node ID for this process — `compute_node_id(hostname,
    /// incarnation)` where the incarnation comes from the persisted
    /// marker (recovery), a successful wipe-rejoin rotation
    /// (`new_incarnation`), or the cold-start sentinel `0`
    /// (hostname-only ID).
    node_id: u64,
    /// Whether the node minted a fresh non-zero incarnation AND
    /// successfully rotated its voter ID with an existing leader.  If
    /// true, callers must use `join_zone(skip_bootstrap=true)` so the
    /// leader's snapshot installs the authoritative ConfState; calling
    /// `create_zone` here would re-bootstrap a stale ConfState that
    /// conflicts with the cluster's committed voter set.
    rotated_into_existing_cluster: bool,
}

/// Generate a fresh non-zero incarnation marker.
///
/// SystemTime nanos as u64.  Two restart-without-data scenarios within
/// a single nanosecond are not physically possible on any current
/// hardware, so this provides a strictly-monotonic-with-very-high-
/// probability stream of incarnation values without requiring `rand`
/// as a dependency.  Maps the unlikely 0 to 1 — `compute_node_id`
/// treats 0 as the cold-start sentinel.
fn generate_fresh_incarnation() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(1);
    if nanos == 0 {
        1
    } else {
        nanos
    }
}

/// Read the persisted node-level incarnation.  Returns `None` when
/// the file doesn't exist (fresh daemon).
fn read_node_incarnation(zones_dir: &str) -> Result<Option<u64>, String> {
    let path = Path::new(zones_dir).join(NODE_INCARNATION_FILE);
    match std::fs::read(&path) {
        Ok(bytes) => {
            let arr: [u8; 8] = bytes.as_slice().try_into().map_err(|_| {
                format!("node incarnation file '{}' is not 8 bytes", path.display())
            })?;
            Ok(Some(u64::from_be_bytes(arr)))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("read node incarnation '{}': {e}", path.display(),)),
    }
}

/// Persist the node-level incarnation.  Creates the parent directory
/// if absent.  Atomic via write-rename — prevents a torn 8-byte file
/// from a crash between `write_all` and `sync`.
fn write_node_incarnation(zones_dir: &str, incarnation: u64) -> Result<(), String> {
    use std::io::Write;
    let dir = Path::new(zones_dir);
    std::fs::create_dir_all(dir).map_err(|e| {
        format!(
            "create zones dir for node incarnation '{}': {e}",
            dir.display(),
        )
    })?;
    let final_path = dir.join(NODE_INCARNATION_FILE);
    let tmp_path = dir.join(format!("{NODE_INCARNATION_FILE}.tmp"));
    {
        let mut tmp = std::fs::File::create(&tmp_path)
            .map_err(|e| format!("create tmp incarnation file '{}': {e}", tmp_path.display(),))?;
        tmp.write_all(&incarnation.to_be_bytes())
            .map_err(|e| format!("write tmp incarnation file '{}': {e}", tmp_path.display(),))?;
        tmp.sync_all()
            .map_err(|e| format!("sync tmp incarnation file '{}': {e}", tmp_path.display(),))?;
    }
    std::fs::rename(&tmp_path, &final_path).map_err(|e| {
        format!(
            "rename '{}' -> '{}': {e}",
            tmp_path.display(),
            final_path.display(),
        )
    })?;
    Ok(())
}

/// Outcome of attempting wipe-rejoin rotation against the configured peers.
///
/// Distinguishes "no live cluster exists" (safe cold-start) from "cluster
/// exists but rotation didn't commit" (cold-starting here would silently
/// rejoin with the joiner's stale hostname-based ID and panic raft-rs's
/// `to_commit` assertion on the leader's first heartbeat — fail loudly
/// instead).  This three-way classification is the contract the caller
/// (`ensure_voter_membership`) relies on to never silently violate the
/// wipe-rejoin invariant.
#[derive(Debug)]
enum RotationOutcome {
    /// Leader committed the ConfChangeV2 atomically swapping
    /// `RemoveNode(old_id) + AddNode(new_id)`.  Caller persists the
    /// new incarnation and starts raft with `skip_bootstrap=true` so
    /// the leader's snapshot installs the authoritative ConfState.
    Committed,
    /// Every peer failed at the TCP/connect layer (or the peers list
    /// was empty / all self).  No live cluster to rejoin — caller
    /// falls through to the cold-start sentinel (incarnation=0,
    /// hostname-based ID) so every fresh peer converges on identical
    /// ConfState bootstraps without coordination.
    AllPeersUnreachable,
    /// At least one peer was TCP-reachable but rotation didn't commit
    /// (RPC handler timeout, "not leader" with no usable redirect, or
    /// — most relevantly — quorum deadlock when the joiner IS the
    /// missing voter on a 2-voter cluster).  A live cluster already
    /// holds this hostname's stale voter ID; cold-starting with that
    /// ID is a contract violation and panics raft-rs.
    PeersReachableRotationFailed { detail: String },
}

/// Try `ReplaceVoterByHostname` against each peer in turn.  Returns
/// the first `Committed` if any peer's leader accepts the rotation,
/// otherwise classifies the failure mode.  Followers supply the
/// leader's address in `leader_address`; the caller follows that
/// redirect once before moving to the next peer.
///
/// **Failure-mode classification is load-bearing.**  The caller relies
/// on the distinction between `AllPeersUnreachable` (safe cold-start)
/// and `PeersReachableRotationFailed` (must surface to operator) to
/// honour the wipe-rejoin contract.  Treating "RPC timeout from a
/// reachable peer" as "no peer reachable" silently rejoins an existing
/// cluster with a stale ID and panics raft-rs (`to_commit X is out of
/// range [last_index 0]`).
///
/// Takes the owning `Runtime` (not a `Handle`) so we can call
/// `Runtime::block_on` directly — `Handle::block_on` against a
/// current_thread runtime from the runtime's own thread deadlocks
/// (the runtime worker is the caller, so the future never gets
/// driven).
fn try_replace_voter_on_peers(
    runtime: &tokio::runtime::Runtime,
    peers: &[NodeAddress],
    self_hostname: &str,
    new_node_id: u64,
    self_address: &str,
) -> RotationOutcome {
    let mut any_reachable = false;
    let mut last_failure: Option<String> = None;

    for peer in peers {
        if peer.hostname == self_hostname {
            // Skip self — even if NEXUS_PEERS includes us, RPC to self
            // before our gRPC server is up would just fail.
            continue;
        }

        let mut endpoint = peer.endpoint.clone();
        let mut redirected_once = false;
        loop {
            eprintln!(
                "[ensure_voter_membership] dialing ReplaceVoterByHostname \
                 endpoint={endpoint} hostname={self_hostname} new_node_id={new_node_id}",
            );
            let attempt = runtime.block_on(call_replace_voter_by_hostname(
                &endpoint,
                "root",
                self_hostname,
                new_node_id,
                self_address,
                5,
            ));
            match attempt {
                Ok(result) if result.success => {
                    eprintln!(
                        "[ensure_voter_membership] ReplaceVoterByHostname committed \
                         endpoint={endpoint} new_node_id={new_node_id} \
                         removed_old_id={:?}",
                        result.removed_old_id,
                    );
                    return RotationOutcome::Committed;
                }
                Ok(result) => {
                    // TCP succeeded, server responded with success=false.
                    // Either a follower redirect (try the leader once)
                    // or a genuine refusal (cluster reachable, rotation
                    // didn't commit).
                    any_reachable = true;
                    if let Some(addr) = result.leader_address.as_ref() {
                        if !redirected_once && !addr.is_empty() && addr != &endpoint {
                            eprintln!(
                                "[ensure_voter_membership] redirected from {endpoint} to {addr}",
                            );
                            endpoint = addr.clone();
                            redirected_once = true;
                            continue;
                        }
                    }
                    let detail = format!("{endpoint} responded with error={:?}", result.error);
                    eprintln!("[ensure_voter_membership] {detail}");
                    last_failure = Some(detail);
                    break;
                }
                Err(TransportError::Connection(msg)) | Err(TransportError::InvalidAddress(msg)) => {
                    // TCP/connect-time failure or unparseable endpoint —
                    // peer is down or unaddressable.  `any_reachable`
                    // stays false on this leg; this is the only branch
                    // that's safe to fold into the cold-start sentinel.
                    eprintln!("[ensure_voter_membership] peer unreachable {endpoint}: {msg}");
                    break;
                }
                Err(e) => {
                    // TCP succeeded, post-connect RPC failed (handler
                    // timeout, transport-level deserialization error,
                    // etc.).  Cluster exists, rotation didn't commit —
                    // surface to operator instead of silently
                    // cold-starting with a stale ID.
                    any_reachable = true;
                    let detail = format!("{endpoint} RPC error: {e}");
                    eprintln!("[ensure_voter_membership] {detail}");
                    last_failure = Some(detail);
                    break;
                }
            }
        }
    }

    if any_reachable {
        RotationOutcome::PeersReachableRotationFailed {
            detail: last_failure.unwrap_or_else(|| {
                // Defensive — `any_reachable=true` always sets last_failure.
                "(no failure detail captured)".to_string()
            }),
        }
    } else {
        RotationOutcome::AllPeersUnreachable
    }
}

impl RaftDistributedCoordinator {
    /// Decide this process's effective node ID and whether to bootstrap
    /// a fresh raft zone or join an already-running one.
    ///
    /// Centralizes the "cold-start vs wipe-rejoin" decision so the rest
    /// of `init_from_env` doesn't have to scatter `was_just_created`
    /// detection or `NEXUS_JOINER_HINT` overrides across every zone
    /// branch.  Run **before** `ZoneManager::with_node_id` so the
    /// computed `node_id` is what raft commits into ConfState.
    ///
    /// Logic:
    /// 1. If `<zones_dir>/.node_incarnation` exists this is a recovery
    ///    boot — read the persisted incarnation (defaulting to 0 =
    ///    legacy / cold-start sentinel) and return
    ///    `compute_node_id(hostname, incarnation)` with
    ///    `rotated_into_existing_cluster = false` — plain restart with
    ///    intact storage.
    /// 2. Otherwise the node is fresh (first-ever boot or post-wipe).
    ///    Mint a fresh non-zero incarnation, compute the new ID, and
    ///    try `ReplaceVoterByHostname` on every peer.  Three outcomes
    ///    drive distinct branches — see [`RotationOutcome`]:
    ///    * **Committed** — leader atomically swapped
    ///      `RemoveNode(old_id) + AddNode(new_id)` via ConfChangeV2.
    ///      Persist the new incarnation, return with
    ///      `rotated_into_existing_cluster = true` so callers use
    ///      `join_zone(skip_bootstrap=true)`.
    ///    * **AllPeersUnreachable** — no peer responded at TCP level.
    ///      Genuine simultaneous cluster bringup; fall through to the
    ///      cold-start sentinel (incarnation=0, hostname-based ID) so
    ///      every fresh peer converges on identical ConfState
    ///      bootstraps without coordination.
    ///    * **PeersReachableRotationFailed** — at least one peer was
    ///      reachable but rotation didn't commit (handler timeout,
    ///      "not leader" with no usable redirect, or 2-voter quorum
    ///      deadlock where the joiner *is* the missing voter).  A
    ///      live cluster holds this hostname's stale voter ID;
    ///      cold-starting here would panic raft-rs's commit_to
    ///      assertion.  Return Err with operator-actionable
    ///      remediation; do **not** persist incarnation, so the next
    ///      boot retries rotation.
    fn ensure_voter_membership(
        &self,
        hostname: &str,
        self_address: &str,
        zones_dir: &str,
        peers: &[NodeAddress],
    ) -> Result<VoterMembership, String> {
        // Recovery path — node-level incarnation file exists from a
        // prior boot (or pre-fix daemon, which we treat as
        // incarnation=0 → cold-start ID).
        if let Some(incarnation) = read_node_incarnation(zones_dir)? {
            let node_id = compute_node_id(hostname, incarnation);
            // Tracing subscriber is not initialised until ZoneManager
            // construction (a few milliseconds later), so this fn
            // logs to stderr directly.  Boot-time trace, low volume.
            eprintln!(
                "[ensure_voter_membership] recovery: hostname={hostname} \
                 incarnation={incarnation} node_id={node_id}",
            );
            return Ok(VoterMembership {
                node_id,
                rotated_into_existing_cluster: false,
            });
        }

        // Fresh path — try wipe-rejoin rotation, fall back to cold start.
        let new_incarnation = generate_fresh_incarnation();
        let new_id = compute_node_id(hostname, new_incarnation);
        eprintln!(
            "[ensure_voter_membership] fresh: hostname={hostname} \
             trying rotation new_incarnation={new_incarnation} new_id={new_id} \
             peers={}",
            peers.len(),
        );

        // Spin up a small temporary tokio runtime for the rotation
        // RPCs.  ZoneManager owns the long-lived runtime but we need
        // gRPC dialing *before* the manager is constructed (so that
        // its raft node ID is correct from the first heartbeat).
        //
        // Use a multi-thread runtime with a single worker so `block_on`
        // on the calling thread doesn't have to drive the event loop
        // simultaneously — that combination on a current_thread
        // runtime deadlocks because the worker thread IS the caller.
        let rotation_runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .thread_name("ensure-voter-membership")
            .build()
            .map_err(|e| format!("rotation runtime: {e}"))?;
        let outcome =
            try_replace_voter_on_peers(&rotation_runtime, peers, hostname, new_id, self_address);
        drop(rotation_runtime);

        // Three-way classification — see RotationOutcome for the
        // contract.  Persisting incarnation is the SSOT commit point:
        // we only do it for outcomes where the chosen ID is safe.
        // PeersReachableRotationFailed deliberately skips the persist
        // so the next boot retries rotation from the "fresh" path.
        match outcome {
            RotationOutcome::Committed => {
                write_node_incarnation(zones_dir, new_incarnation)?;
                eprintln!(
                    "[ensure_voter_membership] rotated into existing cluster: \
                     hostname={hostname} incarnation={new_incarnation} node_id={new_id}",
                );
                Ok(VoterMembership {
                    node_id: new_id,
                    rotated_into_existing_cluster: true,
                })
            }
            RotationOutcome::AllPeersUnreachable => {
                // Cold-start sentinel — every peer derives the same ID
                // for this hostname so `create_zone` ConfState
                // bootstraps converge without coordination.
                write_node_incarnation(zones_dir, 0)?;
                let cold_id = hostname_to_node_id(hostname);
                eprintln!(
                    "[ensure_voter_membership] cold start (no peer TCP-reachable): \
                     hostname={hostname} node_id={cold_id}",
                );
                Ok(VoterMembership {
                    node_id: cold_id,
                    rotated_into_existing_cluster: false,
                })
            }
            RotationOutcome::PeersReachableRotationFailed { detail } => {
                // A live cluster is reachable but rotation didn't
                // commit (handler timeout, "not leader" with no
                // usable redirect, or 2-voter quorum deadlock when
                // the joiner IS the missing voter).
                //
                // Cannot use the cold-start sentinel here: that would
                // rejoin with the hostname-based ID which a live
                // cluster's ConfState may already hold, and the
                // leader's first heartbeat with `commit_to=N` would
                // panic raft-rs (`to_commit X out of range
                // [last_index 0]`) against our empty log.
                //
                // Cannot return Err either: that aborts
                // `init_from_env` entirely, including ZoneManager /
                // raft gRPC transport server bring-up.  In static
                // cold-start this is fatal — the second-to-start
                // node hits PeersReachable (because the first node
                // is up but not yet leader, replies "not leader")
                // and never opens :2126; the first node then has no
                // peer to form quorum with.  Liveness regression
                // observed on Federation E2E in 3-voter cold-start.
                //
                // Strict-raft answer: mint a fresh non-zero
                // incarnation, persist it (so retry boots are stable
                // under the same ID), and return as
                // `rotated_into_existing_cluster=true` so the caller
                // uses `join_zone(skip_bootstrap=true)`.  The fresh
                // ID is NOT in any ConfState yet (rotation didn't
                // commit), so the leader cannot address AppendEntries
                // to it — no commit_to panic.  raft-rs boots with
                // empty ConfState (skip_bootstrap=true) and waits
                // for a ConfChange that adds us.  In static
                // cold-start, once the cluster forms quorum a
                // subsequent retry of the rotation RPC succeeds and
                // adds us.  Operator sees the loud error below.
                tracing::error!(
                    detail = %detail,
                    new_id = new_id,
                    "wipe-rejoin rotation pending: fresh ID booted as joiner; \
                     transport up, raft awaits leader ConfChange. \
                     2-voter clusters that hit this need either (a) the surviving \
                     peer to reach quorum (impossible with one voter), or \
                     (b) coordinated full wipe across all peers, or \
                     (c) addition of a 3rd voter / witness. \
                     3+ voter clusters auto-recover on retry once leader has \
                     quorum-of-old-config to commit ConfChangeV2."
                );
                write_node_incarnation(zones_dir, new_incarnation)?;
                Ok(VoterMembership {
                    node_id: new_id,
                    rotated_into_existing_cluster: true,
                })
            }
        }
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

        // NEXUS_PEERS empty selects the dynamic-bootstrap mode: the
        // daemon brings up the raft transport server and federation
        // wiring, but does NOT auto-create the root zone.  An
        // operator / agent later drives cluster membership explicitly
        // via the existing RPC contract:
        //
        //   * first node:   `federation_create_zone("root")` —
        //                   creates a 1-voter raft group (ConfState
        //                   contains only `self.node_id`, quorum=1,
        //                   self-campaigns to leader).
        //   * later nodes:  `JoinZone(zone_id="root", node_id, addr)`
        //                   RPC against the leader — leader proposes
        //                   ConfChangeV2 AddNode, commits (quorum
        //                   from existing voter set), pushes a
        //                   snapshot, joiner installs ConfState and
        //                   syncs the log.
        //
        // NEXUS_PEERS non-empty keeps the static-bootstrap mode
        // unchanged: every peer seeds the same ConfState from the
        // shared NEXUS_PEERS list, raft-rs runs election internally
        // once peers reach each other.  Both modes coexist; choice
        // is per-deployment, signalled by NEXUS_PEERS presence.
        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        let dynamic_bootstrap = peers_csv.trim().is_empty();

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
        // both `ensure_voter_membership` (for ReplaceVoter RPC dialing
        // by hostname) and `ZoneManager` (for raft peer formatting) read
        // from the same parse result, no double-parse drift risk.
        let peer_addrs: Vec<NodeAddress> = peers_csv
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|entry| {
                NodeAddress::parse(entry, use_tls_for_endpoints)
                    .map_err(|e| format!("NEXUS_PEERS parse '{entry}': {e}"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        // `id@host:port` strings derived from the same NodeAddress
        // entries — ZoneManager re-parses internally; we just provide
        // the standard format raft expects.  Self entry will get
        // overridden by ZoneManager::with_node_id below.
        let peers: Vec<String> = peer_addrs.iter().map(|p| p.to_raft_peer_str()).collect();

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

        // Decide this node's effective ID and whether to bootstrap a
        // fresh raft zone or join an already-running cluster.  Reads /
        // writes the persisted incarnation marker in root zone's
        // `raft.redb`; on the wipe-rejoin path also calls
        // `ReplaceVoterByHostname` on each peer until a leader accepts
        // the rotation.  Strict raft membership-swap contract via
        // ConfChangeV2 atomic commit — no transient quorum gap.
        //
        // Dynamic-bootstrap mode skips the rotation RPC dance — there
        // are no peers to rotate against (NEXUS_PEERS is empty by
        // construction).  Use the cold-start sentinel (incarnation=0,
        // hostname-based ID) which matches the semantics: this node
        // is the first to come up and either becomes leader of a
        // 1-voter cluster on `federation_create_zone`, or joins an
        // existing cluster via JoinZone RPC under the same identity.
        let membership = if dynamic_bootstrap {
            VoterMembership {
                node_id: hostname_to_node_id(&hostname),
                rotated_into_existing_cluster: false,
            }
        } else {
            self.ensure_voter_membership(&hostname, &self_addr, &zones_dir, &peer_addrs)?
        };

        let zm = ZoneManager::with_node_id(
            &hostname,
            membership.node_id,
            &zones_dir,
            peers.clone(),
            &bind_addr,
            tls,
        )
        .map_err(|e| format!("ZoneManager::with_node_id: {e}"))?;

        let runtime_handle = zm.runtime_handle();
        let blob_slot = zm.blob_fetcher_slot();

        let _ = self.zone_manager.set(zm.clone());
        let _ = self.runtime.set(runtime_handle);

        // Hand the blob-fetcher slot up to the kernel so transport's
        // `install_transport_wiring` can drain it.
        kernel.stash_blob_fetcher_slot(Box::new(blob_slot));

        // Choose create_zone vs join_zone based on the membership
        // decision above.  `rotated_into_existing_cluster=true` means
        // a leader already accepted our ConfChangeV2; the leader's
        // snapshot installs the authoritative ConfState, so we must
        // skip ConfState bootstrap (`join_zone(skip_bootstrap=true)`)
        // — calling `create_zone` here would re-bootstrap a stale
        // ConfState that conflicts with the cluster's committed voter
        // set.  Cold-start path uses `create_zone` so all peers
        // converge on identical ConfStates without coordination.
        //
        // NEXUS_JOINER_HINT is honoured for back-compat (operators
        // still using it) but the auto-detect supersedes it: if the
        // hint says "joiner" but we cold-started, we cold-start.
        let auto_join = membership.rotated_into_existing_cluster;
        let joiner_hint = std::env::var("NEXUS_JOINER_HINT")
            .map(|v| v == "1")
            .unwrap_or(false);

        let bootstrap_zone = |zone_id: &str| -> Result<(), String> {
            if zm.get_zone(zone_id).is_some() {
                return Ok(());
            }
            if auto_join || joiner_hint {
                zm.join_zone(zone_id, peers.clone(), false)
                    .map_err(|e| format!("join_zone({zone_id}): {e}"))?;
            } else {
                zm.create_zone(zone_id, peers.clone())
                    .map_err(|e| format!("create_zone({zone_id}): {e}"))?;
            }
            Ok(())
        };

        // Static-bootstrap mode: every node seeds the same ConfState
        // from NEXUS_PEERS at boot.  Dynamic-bootstrap mode skips
        // this entirely — root and any federation zones are created
        // later via explicit RPC (`federation_create_zone` / `JoinZone`).
        if !dynamic_bootstrap {
            bootstrap_zone("root")?;

            if let Ok(zones_csv) = std::env::var("NEXUS_FEDERATION_ZONES") {
                for zone_id in zones_csv
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    bootstrap_zone(zone_id)?;
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
        let (backend, _shared) =
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
        let dcache = kernel.dcache_arc();
        unwire_mount_core(
            &vfs_router,
            &dcache,
            &self.cross_zone_mounts,
            parent_zone,
            mount_path,
        );
        Ok(())
    }

    fn create_zone(&self, kernel: &Kernel, zone_id: &str) -> CoordinatorResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        zm.get_or_create_zone(zone_id).map_err(|e| e.to_string())?;
        self.install_apply_cb_for_zone(kernel, zone_id);
        Ok(())
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
        // Re-derive peers from env at join time — the cluster topology is
        // process-config rather than per-call.
        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        let peers = parse_peer_list_to_raft_format(&peers_csv)
            .map_err(|e| format!("NEXUS_PEERS parse: {e}"))?;
        zm.join_zone(zone_id, peers, as_learner)
            .map_err(|e| e.to_string())?;
        self.install_apply_cb_for_zone(kernel, zone_id);
        Ok(())
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
            .map_err(|e| format!("share_zone route '{local_path}': {e:?}"))?;
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

/// Install the apply-side dcache invalidation callback for a zone's
/// state-machine.  Idempotent — same `coherence_id` gets the same closure.
fn install_dcache_coherence_impl(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    dcache: &Arc<kernel::core::dcache::DCache>,
    consensus: &crate::raft::ZoneConsensus<crate::raft::FullStateMachine>,
) {
    let Some(slot) = consensus.invalidate_cb_slot() else {
        return;
    };
    let coherence_key = consensus.coherence_id();
    let dcache = Arc::clone(dcache);
    let vfs_router = Arc::clone(vfs_router);
    let cb: Arc<dyn Fn(&str) + Send + Sync> = Arc::new(move |zone_relative_key: &str| {
        let trimmed = zone_relative_key.trim_start_matches('/');
        for mp in vfs_router.mount_points_for_coherence_key(coherence_key) {
            let global = if trimmed.is_empty() {
                mp.clone()
            } else if mp.ends_with('/') {
                format!("{}{}", mp, trimmed)
            } else {
                format!("{}/{}", mp, trimmed)
            };
            dcache.evict(&global);
        }
    });
    *slot.write() = Some(cb);
}

/// `&Kernel`-free core of `wire_mount` — same body, but every kernel
/// dependency comes through pre-cloned `Arc`s.  Lets the apply-cb
/// closure (which has no `&Kernel` access) drive the same logic on
/// every follower when raft applies a DT_MOUNT commit.
#[allow(clippy::too_many_arguments)]
fn wire_mount_core(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    dcache: &Arc<kernel::core::dcache::DCache>,
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
                let (backend, shared_state) = crate::federation::DistributedLocks::new(
                    root_consensus,
                    runtime.clone(),
                    kernel_state,
                );
                lock_manager.install_locks(Arc::new(backend), shared_state);
            }
            None => {
                tracing::warn!(
                    "wire_mount: root zone not loaded — distributed locks NOT installed; sys_lock stays local-only until next mount"
                );
            }
        }
    }

    // 6. DCache seed.
    dcache.put(
        &global_path,
        CachedEntry {
            size: 0,
            content_id: None,
            version: 1,
            entry_type: 2, // DT_MOUNT
            zone_id: Some(contracts::ROOT_ZONE_ID.to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: None,
            link_target: None,
        },
    );

    // 7. Install apply-side dcache coherence on the target consensus.
    install_dcache_coherence_impl(vfs_router, dcache, &target_consensus);

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
/// delete event: drop the VFSRouter slot, evict the DCache seed, and
/// remove the reverse-index entry.
fn unwire_mount_core(
    vfs_router: &Arc<kernel::core::vfs_router::VFSRouter>,
    dcache: &Arc<kernel::core::dcache::DCache>,
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
        dcache.evict(&global);
        // Evict any cached child entries (`<global>/...`) — without this,
        // reads under the unwired mount route to longest-prefix parent
        // and hit the stale dcache entry from before unmount.
        let prefix = if global.ends_with('/') {
            global.clone()
        } else {
            format!("{}/", global)
        };
        dcache.evict_prefix(&prefix);
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
    dcache: &Arc<kernel::core::dcache::DCache>,
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
    let dcache = Arc::clone(dcache);
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
                    &dcache,
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
                unwire_mount_core(
                    &vfs_router,
                    &dcache,
                    &cross_zone_mounts,
                    &parent_zone_owned,
                    key,
                );
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
    let dcache = kernel.dcache_arc();
    let lock_manager = kernel.lock_manager_arc();
    wire_mount_core(
        &vfs_router,
        &dcache,
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

/// Install `RaftDistributedCoordinator` into the kernel's coordinator slot
/// and run `init_from_env`. Also drains the pending blob-fetcher slot
/// stashed by `init_from_env` and wires the `KernelBlobFetcher`
/// server-side handler into the raft gRPC fabric — co-locating both
/// halves keeps transport raft-free.
///
/// Mirrors `transport::blob::peer_client::install` — called once per
/// process from the cdylib boot path. Idempotent for re-imports.
pub fn install(kernel: &Kernel) -> Result<(), String> {
    let coordinator = Arc::new(RaftDistributedCoordinator::new());
    kernel.set_distributed_coordinator(coordinator.clone() as Arc<dyn DistributedCoordinator>);
    coordinator.init_from_env(kernel)?;
    crate::blob_fetcher_handler::install(kernel);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a tokio Runtime suitable for `try_replace_voter_on_peers`.
    /// Mirrors the construction in `ensure_voter_membership` so tests
    /// exercise the same runtime configuration as production.
    fn rotation_runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .thread_name("test-rotation")
            .build()
            .expect("rotation runtime")
    }

    #[test]
    fn empty_peers_classifies_as_all_unreachable() {
        // No peers configured at all → loop body never enters, no peer
        // is reachable, fall through to cold-start sentinel safely.
        let rt = rotation_runtime();
        let outcome = try_replace_voter_on_peers(&rt, &[], "host-a", 1, "host-a:2126");
        assert!(matches!(outcome, RotationOutcome::AllPeersUnreachable));
    }

    #[test]
    fn all_self_peers_classifies_as_all_unreachable() {
        // NEXUS_PEERS lists only ourselves (single-node bringup) →
        // every peer skipped via the self-hostname guard, no peer
        // reachable.
        let rt = rotation_runtime();
        let peers = vec![NodeAddress::parse("host-a:2126", false).expect("parse")];
        let outcome = try_replace_voter_on_peers(&rt, &peers, "host-a", 1, "host-a:2126");
        assert!(matches!(outcome, RotationOutcome::AllPeersUnreachable));
    }

    #[test]
    fn closed_port_classifies_as_all_unreachable() {
        // 127.0.0.1:1 is reserved (TCPMUX, never bound on dev boxes) →
        // tonic returns TransportError::Connection at TCP-connect
        // time, which must NOT count toward `any_reachable`.  This is
        // the "Scenario A — genuine network unreachability" case from
        // the bug analysis: cold-start sentinel is the safe fallback.
        let rt = rotation_runtime();
        let peers = vec![NodeAddress::parse("127.0.0.1:1", false).expect("parse")];
        let outcome = try_replace_voter_on_peers(&rt, &peers, "host-a", 1, "host-a:2126");
        assert!(
            matches!(outcome, RotationOutcome::AllPeersUnreachable),
            "expected AllPeersUnreachable on closed port, got {outcome:?}",
        );
    }

    #[test]
    fn rotation_outcome_variants_are_distinct() {
        // Compile-time guard: keep the three-way split intact.  If a
        // future refactor collapses two variants the caller's match
        // arms will silently lose the contract enforcement, so this
        // test pins the variants to a known shape.
        let unreachable = RotationOutcome::AllPeersUnreachable;
        let failed = RotationOutcome::PeersReachableRotationFailed {
            detail: "test".to_string(),
        };
        let committed = RotationOutcome::Committed;
        assert!(matches!(unreachable, RotationOutcome::AllPeersUnreachable));
        assert!(matches!(
            failed,
            RotationOutcome::PeersReachableRotationFailed { .. }
        ));
        assert!(matches!(committed, RotationOutcome::Committed));
    }
}
