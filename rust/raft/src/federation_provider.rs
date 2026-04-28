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
use kernel::abc::meta_store::MetaStore;
use kernel::hal::federation::{BlobFetcherSlot, FederationProvider, FederationResult};
use kernel::kernel::Kernel;

use crate::transport::hostname_to_node_id;
use crate::{TlsFiles, ZoneManager};

/// Raft-backed `FederationProvider` impl.
///
/// All state is `OnceLock` so the provider is `Send + Sync + 'static`
/// without interior mutability noise.  `init_from_env` populates the
/// slots; subsequent calls observe a stable snapshot.
pub struct RaftFederationProvider {
    zone_manager: OnceLock<Arc<ZoneManager>>,
    runtime: OnceLock<tokio::runtime::Handle>,
    bootstrap_done: AtomicBool,
}

impl RaftFederationProvider {
    pub fn new() -> Self {
        Self {
            zone_manager: OnceLock::new(),
            runtime: OnceLock::new(),
            bootstrap_done: AtomicBool::new(false),
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

    fn locks_for_zone(&self, _kernel: &Kernel, zone_id: &str) -> FederationResult<Arc<dyn Locks>> {
        let _ = zone_id;
        Err("locks_for_zone: not yet wired through trait".into())
    }

    fn wal_stream_for_zone(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _stream_id: &str,
        _prefix: &str,
    ) -> FederationResult<Arc<dyn kernel::stream::StreamBackend>> {
        Err("wal_stream_for_zone: not yet wired through trait".into())
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
        _kernel: &Kernel,
        _parent_zone: &str,
        _mount_path: &str,
        _target_zone: &str,
    ) -> FederationResult<()> {
        Err("wire_mount: not yet wired through trait".into())
    }

    fn start_replication_scanner(
        &self,
        _kernel: &Kernel,
        _zone_id: &str,
        _policies_json: &str,
        _interval_ms: u64,
    ) -> FederationResult<Box<dyn std::any::Any + Send + Sync>> {
        Err("start_replication_scanner: not yet wired through trait".into())
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
        _local_path: &str,
        _zone_id: &str,
    ) -> FederationResult<()> {
        Err("register_share: not yet wired through trait".into())
    }

    fn lookup_share(
        &self,
        _kernel: &Kernel,
        _remote_path: &str,
    ) -> FederationResult<Option<String>> {
        Ok(None)
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

    fn append_wal_entry(
        &self,
        _kernel: &Kernel,
        zone_id: &str,
        entry_type: u8,
        vfs_path: &str,
        seq: u64,
        entry: Vec<u8>,
    ) -> FederationResult<u64> {
        let zm = self.zm().ok_or("federation not active")?;
        let consensus = zm
            .registry()
            .get_node(zone_id)
            .ok_or_else(|| format!("zone {zone_id} not loaded"))?;
        let runtime = self.runtime.get().cloned().ok_or("runtime missing")?;
        let key = encode_wal_key(entry_type, vfs_path, seq);
        let wal = crate::wal_stream_backend::RaftWalConsensus::new(consensus, runtime);
        crate::wal_stream_backend::WalConsensus::append(&wal, &key, &entry)?;
        Ok(seq)
    }

    fn get_wal_entry(
        &self,
        _kernel: &Kernel,
        zone_id: &str,
        entry_type: u8,
        vfs_path: &str,
        seq: u64,
    ) -> FederationResult<Option<Vec<u8>>> {
        let zm = self.zm().ok_or("federation not active")?;
        let consensus = zm
            .registry()
            .get_node(zone_id)
            .ok_or_else(|| format!("zone {zone_id} not loaded"))?;
        let runtime = self.runtime.get().cloned().ok_or("runtime missing")?;
        let key = encode_wal_key(entry_type, vfs_path, seq);
        let wal = crate::wal_stream_backend::RaftWalConsensus::new(consensus, runtime);
        crate::wal_stream_backend::WalConsensus::get(&wal, &key)
    }
}

/// Encode `(entry_type, vfs_path, seq)` into a redb key.  The
/// `entry_type` byte tags the keyspace (DT_PIPE vs DT_STREAM) so pipe
/// and stream entries at distinct VFS paths share `TREE_STREAM_ENTRIES`
/// without ever colliding — defense in depth even though the VFS
/// invariant (path is pipe XOR stream) already prevents collision at
/// the same path.  Format: `"<entry_type>:<vfs_path>:<seq>"` —
/// human-readable for debugging; `entry_type` rendered as decimal so
/// the prefix range stays adjacent in lexicographic order.
fn encode_wal_key(entry_type: u8, vfs_path: &str, seq: u64) -> String {
    format!("{entry_type}:{vfs_path}:{seq}")
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
