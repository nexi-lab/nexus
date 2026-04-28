//! Concrete `FederationProvider` implementation.
//!
//! Phase H of the rust-workspace restructure put the
//! `RaftFederationProvider` impl in the raft crate (after the `kernel
//! → raft` Cargo edge flipped to `raft → kernel`).  The kernel
//! installs an `Arc<dyn FederationProvider>` into its `federation` slot
//! via the cdylib boot path; federation-aware syscalls dispatch through
//! the trait.
//!
//! ## Provider shape
//!
//! `RaftFederationProvider` owns the federation-side state that pre-Phase-5
//! lived directly on `Kernel`:
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
use kernel::hal::federation::{BlobFetcherSlot, FederationProvider, FederationResult};
use kernel::kernel::Kernel;

use crate::transport::hostname_to_node_id;
use crate::zone_meta_store::ZoneMetaStore;
use crate::{TlsFiles, ZoneManager};

/// Triple keyed by target zone: `(parent_zone_id, mount_path, global_path)`.
type CrossZoneMountTuple = (String, String, String);

/// Raft-backed `FederationProvider` impl.
///
/// All state is `OnceLock` so the provider is `Send + Sync + 'static`
/// without interior mutability noise.  `init_from_env` populates the
/// slots; subsequent calls observe a stable snapshot.
pub struct RaftFederationProvider {
    zone_manager: OnceLock<Arc<ZoneManager>>,
    runtime: OnceLock<tokio::runtime::Handle>,
    bootstrap_done: AtomicBool,
    /// Reverse index `target_zone_id → [(parent_zone, mount_path, global_path)]`
    /// — derived cache for `wire_mount` reconstruction logic, populated as
    /// federation mounts get wired.  Node-local: replication SSOT lives in
    /// the DT_MOUNT entries on the metastore, this is only a fast-lookup
    /// shadow; rebuilt from scratch on process restart by the reconcile loop.
    cross_zone_mounts: DashMap<String, Vec<CrossZoneMountTuple>>,
}

impl RaftFederationProvider {
    pub fn new() -> Self {
        Self {
            zone_manager: OnceLock::new(),
            runtime: OnceLock::new(),
            bootstrap_done: AtomicBool::new(false),
            cross_zone_mounts: DashMap::new(),
        }
    }

    fn zm(&self) -> Option<&Arc<ZoneManager>> {
        self.zone_manager.get()
    }
}

impl Default for RaftFederationProvider {
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

impl FederationProvider for RaftFederationProvider {
    fn init_from_env(&self, kernel: &Kernel) -> FederationResult<bool> {
        // Idempotent — if zone manager already exists, treat as
        // "already initialised" and report no-op.
        if self.zone_manager.get().is_some() {
            return Ok(false);
        }

        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        if peers_csv.trim().is_empty() {
            return Ok(false);
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

        let peers = parse_peer_list_to_raft_format(&peers_csv)
            .map_err(|e| format!("NEXUS_PEERS parse: {e}"))?;

        // TLS detection — disabled when NEXUS_RAFT_TLS=false (E2E).
        let tls_disabled = std::env::var("NEXUS_RAFT_TLS")
            .map(|v| v.eq_ignore_ascii_case("false") || v == "0")
            .unwrap_or(false)
            || std::env::var("NEXUS_NO_TLS")
                .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                .unwrap_or(false);

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

        let zm = ZoneManager::new(&hostname, &zones_dir, peers.clone(), &bind_addr, tls)
            .map_err(|e| format!("ZoneManager::new: {e}"))?;

        let runtime_handle = zm.runtime_handle();
        let blob_slot = zm.blob_fetcher_slot();

        let _ = self.zone_manager.set(zm.clone());
        let _ = self.runtime.set(runtime_handle);

        // Hand the blob-fetcher slot up to the kernel so transport's
        // `install_transport_wiring` can drain it.
        kernel.stash_blob_fetcher_slot(Box::new(blob_slot));

        // Bootstrap the root zone idempotently.
        let joiner_hint = std::env::var("NEXUS_JOINER_HINT")
            .map(|v| v == "1")
            .unwrap_or(false);

        if zm.get_zone("root").is_none() {
            if joiner_hint {
                zm.join_zone("root", peers.clone(), false)
                    .map_err(|e| format!("join_zone(root): {e}"))?;
            } else {
                zm.create_zone("root", peers.clone())
                    .map_err(|e| format!("create_zone(root): {e}"))?;
            }
        }

        if let Ok(zones_csv) = std::env::var("NEXUS_FEDERATION_ZONES") {
            for zone_id in zones_csv
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                if zm.get_zone(zone_id).is_none() {
                    if joiner_hint {
                        zm.join_zone(zone_id, peers.clone(), false)
                            .map_err(|e| format!("join_zone({zone_id}): {e}"))?;
                    } else {
                        zm.create_zone(zone_id, peers.clone())
                            .map_err(|e| format!("create_zone({zone_id}): {e}"))?;
                    }
                }
            }
        }

        self.bootstrap_done.store(true, Ordering::Release);
        tracing::info!("federation bootstrap complete (hostname={hostname})");
        Ok(true)
    }

    fn is_initialized(&self, _kernel: &Kernel) -> bool {
        self.zone_manager.get().is_some()
    }

    fn bind_address(&self, kernel: &Kernel) -> Option<String> {
        kernel.self_address_string()
    }

    fn hostname(&self, kernel: &Kernel) -> Option<String> {
        kernel.self_address_string()
    }

    fn list_zones(&self, _kernel: &Kernel) -> Vec<String> {
        self.zm().map(|zm| zm.list_zones()).unwrap_or_default()
    }

    fn metastore_for_zone(
        &self,
        _kernel: &Kernel,
        zone_id: &str,
    ) -> FederationResult<Arc<dyn MetaStore>> {
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

    fn locks_for_zone(&self, kernel: &Kernel, zone_id: &str) -> FederationResult<Arc<dyn Locks>> {
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

    fn remote_read_blob(
        &self,
        kernel: &Kernel,
        _zone_id: &str,
        path: &str,
        content_id: &str,
    ) -> FederationResult<Vec<u8>> {
        let client = kernel.peer_client_arc();
        let key = if !content_id.is_empty() {
            content_id
        } else {
            path
        };
        client.fetch("", key)
    }

    fn wire_mount(
        &self,
        kernel: &Kernel,
        parent_zone: &str,
        mount_path: &str,
        target_zone: &str,
    ) -> FederationResult<()> {
        wire_mount_impl(self, kernel, parent_zone, mount_path, target_zone)
    }

    fn stash_blob_fetcher_slot(&self, kernel: &Kernel, slot: BlobFetcherSlot) {
        kernel.stash_blob_fetcher_slot(slot);
    }

    fn take_blob_fetcher_slot(&self, kernel: &Kernel) -> Option<BlobFetcherSlot> {
        kernel.take_pending_blob_fetcher_slot()
    }

    fn create_zone(&self, _kernel: &Kernel, zone_id: &str) -> FederationResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        zm.get_or_create_zone(zone_id).map_err(|e| e.to_string())?;
        Ok(())
    }

    fn remove_zone(&self, _kernel: &Kernel, zone_id: &str, force: bool) -> FederationResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        zm.remove_zone(zone_id, force).map_err(|e| e.to_string())
    }

    fn join_zone(&self, _kernel: &Kernel, zone_id: &str, as_learner: bool) -> FederationResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        // Re-derive peers from env at join time — the cluster topology is
        // process-config rather than per-call.
        let peers_csv = std::env::var("NEXUS_PEERS").unwrap_or_default();
        let peers = parse_peer_list_to_raft_format(&peers_csv)
            .map_err(|e| format!("NEXUS_PEERS parse: {e}"))?;
        zm.join_zone(zone_id, peers, as_learner)
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn zone_share(
        &self,
        _kernel: &Kernel,
        _parent_zone: &str,
        _prefix: &str,
        _new_zone: &str,
    ) -> FederationResult<u64> {
        Err("zone_share: not yet wired through trait".into())
    }

    fn register_share(
        &self,
        _kernel: &Kernel,
        local_path: &str,
        zone_id: &str,
    ) -> FederationResult<()> {
        let zm = self.zm().ok_or("federation not active")?;
        zm.register_share(local_path, zone_id)
            .map_err(|e| e.to_string())
    }

    fn lookup_share(
        &self,
        _kernel: &Kernel,
        remote_path: &str,
    ) -> FederationResult<Option<String>> {
        let zm = self.zm().ok_or("federation not active")?;
        zm.lookup_share(remote_path).map_err(|e| e.to_string())
    }

    fn zone_links_count(&self, _kernel: &Kernel, _zone_id: &str) -> FederationResult<i64> {
        Ok(0)
    }

    fn zone_cluster_info(
        &self,
        _kernel: &Kernel,
        zone_id: &str,
    ) -> FederationResult<Vec<(String, serde_json::Value)>> {
        let zm = self.zm().ok_or("federation not active")?;
        let status = zm.cluster_status(zone_id);
        Ok(vec![
            ("zone_id".to_string(), status.zone_id.into()),
            ("node_id".to_string(), status.node_id.into()),
            ("has_store".to_string(), status.has_store.into()),
            ("is_leader".to_string(), status.is_leader.into()),
            ("leader_id".to_string(), status.leader_id.into()),
            ("term".to_string(), status.term.into()),
            ("commit_index".to_string(), status.commit_index.into()),
            ("applied_index".to_string(), status.applied_index.into()),
            ("voter_count".to_string(), status.voter_count.into()),
            ("witness_count".to_string(), status.witness_count.into()),
        ])
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

/// Wire a federation mount into the kernel — port of the pre-Phase-5
/// `kernel::wire_federation_mount_impl`.  Looks up the target zone's
/// raft consensus, builds a `ZoneMetaStore`, registers the mount in
/// VFSRouter, swaps the lock backend on first federated mount, seeds
/// the DCache and installs the apply-side coherence callback.
fn wire_mount_impl(
    provider: &RaftFederationProvider,
    kernel: &Kernel,
    parent_zone_id: &str,
    mount_path: &str,
    target_zone_id: &str,
) -> FederationResult<()> {
    tracing::info!(
        parent_zone_id = %parent_zone_id,
        mount_path = %mount_path,
        target_zone_id = %target_zone_id,
        "wire_mount_impl entered"
    );

    let zm = provider.zm().ok_or("federation not active")?;
    let runtime = provider
        .runtime
        .get()
        .ok_or("federation runtime not initialised")?;
    let registry = zm.registry();

    // 1. Look up target zone — not-yet-local is a no-op (reconcile
    //    loop / future apply events re-drive when the zone arrives).
    let Some(target_consensus) = registry.get_node(target_zone_id) else {
        tracing::warn!(
            target_zone_id = %target_zone_id,
            "wire_mount: target zone not loaded locally — deferring"
        );
        return Ok(());
    };

    // 2. Reconstruct the global VFS path.
    let global_path =
        match reconstruct_global_path(&provider.cross_zone_mounts, parent_zone_id, mount_path) {
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

    // 3. Build a ZoneMetaStore rooted at global_path against the
    //    target's state machine — reuses the root mount's CAS backend.
    let vfs_router = kernel.vfs_router_arc();
    let dcache = kernel.dcache_arc();
    let lock_manager = kernel.lock_manager_arc();
    let metastore: Arc<dyn MetaStore> = ZoneMetaStore::new_arc(
        target_consensus.clone(),
        runtime.clone(),
        global_path.clone(),
    );
    let root_canonical = canonicalize("/", contracts::ROOT_ZONE_ID);
    let root_backend = vfs_router
        .get_canonical(&root_canonical)
        .and_then(|e| e.backend.clone());

    // 4. Install into VFSRouter under the root zone — federation
    //    mounts live in the root zone's path space on every node.
    //    Tag with `target_zone_id` so routing carries the destination
    //    zone (caller's ambient may differ).
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
    //    locks bound to the ROOT zone's consensus (every peer always
    //    has root, so all nodes agree on which state machine holds
    //    lock state).  Idempotent via `locks_installed()`.
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

    // 6. DCache seed so sys_stat on the mount point resolves locally
    //    without a metastore round-trip.
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
        },
    );

    // 7. Install apply-side dcache coherence on the target consensus.
    install_dcache_coherence_impl(&vfs_router, &dcache, &target_consensus);

    // 8. Update reverse index `target_zone → [(parent, mount_path, global)]`.
    //    Dedup so replayed apply events don't double-register.
    let mut bucket = provider
        .cross_zone_mounts
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

/// Install `RaftFederationProvider` into the kernel's federation slot
/// and run `init_from_env`.
///
/// Mirrors `transport::blob::peer_client::install` — called once per
/// process from the cdylib boot path.  Idempotent for re-imports.
pub fn install(kernel: &Kernel) -> Result<(), String> {
    let provider = Arc::new(RaftFederationProvider::new());
    kernel.set_federation(provider.clone() as Arc<dyn FederationProvider>);
    provider.init_from_env(kernel)?;
    Ok(())
}
