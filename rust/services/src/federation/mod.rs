//! `FederationService` — Rust-flavoured service-tier surface for the
//! federation control plane.  Replaces the Python `FederationRPCService`
//! at `src/nexus/server/rpc/services/federation_rpc.py`.
//!
//! Most methods are thin wrappers over kernel surfaces that already
//! live in Rust:
//!
//!   * `kernel.distributed_coordinator()` — `is_initialized` /
//!     `remove_zone` / `join_zone` / `share_zone` / `lookup_share` /
//!     `cluster_info` (impl in `rust/raft/src/distributed_coordinator.rs`).
//!   * `kernel.sys_setattr DT_MOUNT` — handles auto-create-on-mount via
//!     the same coordinator trait, so `federation_create_zone` /
//!     `federation_mount` collapse to the same syscall.
//!   * `kernel.sys_unlink` — `federation_unmount` is just unlinking the
//!     mount entry.
//!   * `kernel.sys_readdir_backend("/__sys__/zones/", "root")` —
//!     `federation_list_zones` walks the procfs zones namespace.
//!
//! The 9 ported methods cover the federation control-plane surface.
//! `federation_export_zone` / `federation_import_zone` are split into
//! a separate `portability` Rust service (see task #49) — they're zone-
//! bundle backup/migration utilities, not federation core logic.

use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::json;

use kernel::kernel::Kernel;
use kernel::service_registry::{RustCallError, RustService};

// ── Method-name constants (versioned) ────────────────────────────────

pub const NAME: &str = "federation";

// ── Request / response shapes (JSON over the wire) ───────────────────

#[derive(Deserialize)]
struct CreateZoneRequest {
    zone_id: String,
}

#[derive(Deserialize)]
struct RemoveZoneRequest {
    zone_id: String,
    #[serde(default)]
    force: bool,
}

#[derive(Deserialize)]
struct JoinRequest {
    /// Reserved for out-of-cluster bootstrap; raft already replicates
    /// the share registry to every cluster member, so the lookup is
    /// fully local.
    #[serde(default)]
    #[allow(dead_code)]
    peer_addr: String,
    remote_path: String,
    local_path: String,
}

#[derive(Deserialize)]
struct ShareRequest {
    local_path: String,
    #[serde(default)]
    zone_id: Option<String>,
}

#[derive(Deserialize)]
struct MountRequest {
    parent_zone: String,
    path: String,
    target_zone: String,
    #[serde(default)]
    source: Option<String>,
}

#[derive(Deserialize)]
struct UnmountRequest {
    parent_zone: String,
    path: String,
}

#[derive(Deserialize)]
struct ClusterInfoRequest {
    zone_id: String,
}

// ── Service ──────────────────────────────────────────────────────────

pub struct FederationService {
    kernel: Arc<Kernel>,
}

impl FederationService {
    pub fn new(kernel: Arc<Kernel>) -> Self {
        Self { kernel }
    }

    /// Install into a freshly-constructed kernel: enlist as a Rust
    /// service so `Kernel::dispatch_rust_call("federation", method, ...)`
    /// resolves here.
    pub fn install(kernel: &Arc<Kernel>) -> Result<(), String> {
        let svc = Arc::new(Self::new(Arc::clone(kernel)));
        kernel.register_rust_service(NAME, svc as Arc<dyn RustService>, Vec::new())
    }

    // ── Method impls ─────────────────────────────────────────────────

    fn create_zone(&self, req: CreateZoneRequest) -> Result<Vec<u8>, RustCallError> {
        // Standalone-create RPC retained for back-compat (CLI / older
        // agents).  Body routes through the syscall surface — no PyO3
        // shortcut to the HAL trait.  ``sys_setattr DT_MOUNT`` auto-
        // creates the underlying raft group when the federation
        // provider is initialised; the synthetic mount entry lives
        // under ``/__fed_zones__/{zone_id}`` so it's segregated from
        // the normal namespace and clean for ``federation_remove_zone``
        // to cascade-unmount.
        let zone_id = req.zone_id;
        // Best-effort mkdir parent (idempotent on already-exists).
        let _ = self.sys_setattr_mount("/__fed_zones__", 1, "", "", None);
        let synthetic_path = format!("/__fed_zones__/{zone_id}");
        match self.sys_setattr_mount(&synthetic_path, 2, "", &zone_id, None) {
            Ok(_) => {}
            Err(msg) => {
                let lower = msg.to_lowercase();
                if !lower.contains("already")
                    && !lower.contains("exists")
                    && !lower.contains("dt_mount")
                {
                    return Err(RustCallError::Internal(msg));
                }
            }
        }
        Ok(serde_json::to_vec(&json!({"zone_id": zone_id}))
            .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn remove_zone(&self, req: RemoveZoneRequest) -> Result<Vec<u8>, RustCallError> {
        // Cascade-unmount happens inside the DistributedCoordinator
        // impl. `force=true` honors the POSIX-style `unlink while
        // i_links > 0` bypass for replication races on followers.
        self.kernel
            .distributed_coordinator()
            .remove_zone(&self.kernel, &req.zone_id, req.force)
            .map_err(RustCallError::Internal)?;
        Ok(serde_json::to_vec(&json!({
            "zone_id": req.zone_id,
            "removed": true,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn join(&self, req: JoinRequest) -> Result<Vec<u8>, RustCallError> {
        // Discovery uses the raft-replicated share registry in the
        // root zone — `remote_path → zone_id` is already on this node
        // once the sharing node's `federation_share` commits.
        let share_path = format!("/__sys__/shares{}", req.remote_path);
        let stat = self.kernel.sys_stat(&share_path, "root").ok_or_else(|| {
            RustCallError::InvalidArgument(format!(
                "No share registered for '{}'. The sharing node must call \
                 federation_share before federation_join.",
                req.remote_path
            ))
        })?;
        let zone_id = stat.zone_id.clone().filter(|z| !z.is_empty()).ok_or_else(|| {
            RustCallError::InvalidArgument(format!(
                "Share at '{}' has no zone_id (not yet registered)",
                req.remote_path
            ))
        })?;

        // Join the zone's raft group via the federation control-plane.
        self.kernel
            .distributed_coordinator()
            .join_zone(&self.kernel, &zone_id, false)
            .map_err(RustCallError::Internal)?;

        // Mount the shared zone at `local_path` so VFS routing reaches
        // it.  Derive the local parent zone via sys_stat on the parent
        // directory.
        let parent_dir = req.local_path.rsplit_once('/').map(|(p, _)| p).unwrap_or("/");
        let parent_dir = if parent_dir.is_empty() { "/" } else { parent_dir };
        let parent_zone = self
            .kernel
            .sys_stat(parent_dir, "root")
            .and_then(|s| s.zone_id)
            .filter(|z| !z.is_empty())
            .unwrap_or_else(|| "root".to_string());

        let mount_resp = self.mount(MountRequest {
            parent_zone: parent_zone.clone(),
            path: req.local_path.clone(),
            target_zone: zone_id.clone(),
            source: None,
        })?;
        let mount_obj: serde_json::Value = serde_json::from_slice(&mount_resp)
            .map_err(|e| RustCallError::Internal(e.to_string()))?;

        Ok(serde_json::to_vec(&json!({
            "zone_id": zone_id,
            "remote_path": req.remote_path,
            "local_path": req.local_path,
            "parent_zone": parent_zone,
            "mount": mount_obj,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn share(&self, req: ShareRequest) -> Result<Vec<u8>, RustCallError> {
        // Atomic create + copy + register through the
        // DistributedCoordinator trait. Path decomposition (parent
        // zone, prefix) happens inside the impl via VFSRouter.
        let new_zone_id = req.zone_id.unwrap_or_else(|| {
            let suffix: String = uuid_hex8();
            format!("share-{suffix}")
        });
        let info = self
            .kernel
            .distributed_coordinator()
            .share_zone(&self.kernel, &req.local_path, &new_zone_id)
            .map_err(RustCallError::Internal)?;
        Ok(serde_json::to_vec(&json!({
            "zone_id": info.zone_id,
            "copied_entries": info.copied_entries,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn mount(&self, req: MountRequest) -> Result<Vec<u8>, RustCallError> {
        // DT_MOUNT entry_type=2 (see rust/kernel/src/core/dcache.rs).
        // Backend params unused for federation mounts — the kernel
        // resolves the metastore via DistributedCoordinator::metastore_for_zone.
        self.sys_setattr_mount(
            &req.path,
            2,
            "federation",
            &req.target_zone,
            req.source.as_deref(),
        )
        .map_err(RustCallError::Internal)?;
        Ok(serde_json::to_vec(&json!({
            "parent_zone": req.parent_zone,
            "path": req.path,
            "target_zone": req.target_zone,
            "source": req.source,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn unmount(&self, req: UnmountRequest) -> Result<Vec<u8>, RustCallError> {
        // Federation is the trusted in-tree module for mount lifecycle —
        // call `Kernel::unmount_federation` directly (Tier 3 kernel module
        // API, Linux `EXPORT_SYMBOL` analogue) instead of routing through
        // generic `sys_unlink`.  The generic syscall has to look up the
        // DT_MOUNT row's metadata to discover the entry type, but the
        // DT_MOUNT row lives in the PARENT zone's metastore (where
        // `dlc.mount` wrote it) while routing for `path == mount_point`
        // returns the mount being unlinked — a cold-dcache lookup lands
        // in the wrong state machine and silently misses, leaving an
        // orphaned mount in VFSRouter.  `unmount_federation` skips that
        // dispatch dance and goes straight to the SSOT
        // (`DriverLifecycleCoordinator::unmount`) which deletes via the
        // parent zone's metastore + evicts dcache + removes the route.
        //
        // We need the target zone to drive `dlc.unmount`'s VFSRouter
        // remove; recover it from the routing table (longest-prefix on
        // `path` matches the mount itself, so route.zone_id is the
        // target).  Falls back to parent_zone for the
        // already-unmounted / never-mounted case so the call stays a
        // no-op (idempotent, matches POSIX `umount` semantics).
        let target_zone = self
            .kernel
            .vfs_router_arc()
            .route(&req.path, &req.parent_zone)
            .ok()
            .map(|r| r.zone_id)
            .filter(|z| !z.is_empty())
            .unwrap_or_else(|| req.parent_zone.clone());
        self.kernel.unmount_federation(&req.path, &target_zone);
        Ok(serde_json::to_vec(&json!({
            "parent_zone": req.parent_zone,
            "path": req.path,
            "target_zone": target_zone,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn list_zones(&self) -> Result<Vec<u8>, RustCallError> {
        // /__sys__/zones/ procfs view — read-only, kernel-internal
        // synthesised entries.
        let zone_ids = self.kernel.sys_readdir_backend("/__sys__/zones/", "root");
        let zones: Vec<serde_json::Value> = zone_ids
            .iter()
            .map(|zid| {
                let links_count = self
                    .kernel
                    .distributed_coordinator()
                    .cluster_info(&self.kernel, zid)
                    .map(|info| info.links_count)
                    .unwrap_or(0);
                json!({"zone_id": zid, "links_count": links_count})
            })
            .collect();
        Ok(serde_json::to_vec(&json!({
            "zones": zones,
            "node_id": zone_ids,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    fn cluster_info(&self, req: ClusterInfoRequest) -> Result<Vec<u8>, RustCallError> {
        let info = self
            .kernel
            .distributed_coordinator()
            .cluster_info(&self.kernel, &req.zone_id)
            .map_err(RustCallError::Internal)?;
        Ok(serde_json::to_vec(&json!({
            "zone_id": info.zone_id,
            "node_id": info.node_id,
            "has_store": info.has_store,
            "is_leader": info.is_leader,
            "leader_id": info.leader_id,
            "term": info.term,
            "commit_index": info.commit_index,
            "applied_index": info.applied_index,
            "voter_count": info.voter_count,
            "witness_count": info.witness_count,
            "links_count": info.links_count,
        }))
        .map_err(|e| RustCallError::Internal(e.to_string()))?)
    }

    // ── sys_setattr DT_MOUNT helper ──────────────────────────────────

    /// Thin wrapper around `Kernel::sys_setattr` for the DT_DIR /
    /// DT_MOUNT entry types this service emits.  Federation mounts
    /// don't ship a Python-built backend instance — the kernel auto-
    /// resolves the metastore via the DistributedCoordinator trait.
    fn sys_setattr_mount(
        &self,
        path: &str,
        entry_type: i32,
        backend_name: &str,
        zone_id: &str,
        source: Option<&str>,
    ) -> Result<(), String> {
        self.kernel
            .sys_setattr(
                path,
                entry_type,
                backend_name,
                /* backend */ None,
                /* metastore */ None,
                /* raft_backend */ None,
                /* io_profile */ "memory",
                zone_id,
                /* is_external */ false,
                /* capacity */ 0,
                /* read_fd */ None,
                /* write_fd */ None,
                /* mime_type */ None,
                /* modified_at_ms */ None,
                /* link_target */ None,
                source,
            )
            .map(|_| ())
            .map_err(|e| format!("sys_setattr({path}, type={entry_type}): {e:?}"))
    }
}

// ── RustService dispatch ─────────────────────────────────────────────

impl RustService for FederationService {
    fn name(&self) -> &str {
        NAME
    }

    fn start(&self) -> Result<(), String> {
        Ok(())
    }

    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    /// Wire-form names match the Python `FederationRPCService`
    /// `@rpc_expose` surface so callers don't change.  Routed by the
    /// tonic Call handler via `resolve_rust_dispatch`'s `federation_`
    /// prefix mapping.
    fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        match method {
            "federation_create_zone" => {
                let req: CreateZoneRequest = parse(payload)?;
                self.create_zone(req)
            }
            "federation_remove_zone" => {
                let req: RemoveZoneRequest = parse(payload)?;
                self.remove_zone(req)
            }
            "federation_join" => {
                let req: JoinRequest = parse(payload)?;
                self.join(req)
            }
            "federation_share" => {
                let req: ShareRequest = parse(payload)?;
                self.share(req)
            }
            "federation_mount" => {
                let req: MountRequest = parse(payload)?;
                self.mount(req)
            }
            "federation_unmount" => {
                let req: UnmountRequest = parse(payload)?;
                self.unmount(req)
            }
            "federation_list_zones" => self.list_zones(),
            "federation_cluster_info" => {
                let req: ClusterInfoRequest = parse(payload)?;
                self.cluster_info(req)
            }
            _ => Err(RustCallError::NotFound),
        }
    }
}

fn parse<T: for<'de> Deserialize<'de>>(payload: &[u8]) -> Result<T, RustCallError> {
    if payload.is_empty() {
        // Empty payload — `serde_json::from_slice(&[])` errors.  Default
        // to `{}` so methods with all-default fields still parse.
        return serde_json::from_slice(b"{}").map_err(|e| RustCallError::InvalidArgument(e.to_string()));
    }
    serde_json::from_slice(payload).map_err(|e| RustCallError::InvalidArgument(e.to_string()))
}

fn uuid_hex8() -> String {
    // Lightweight 8-hex-char id; we only need uniqueness within the
    // share namespace, not cryptographic quality.
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{:08x}", (nanos as u32) ^ ((nanos >> 32) as u32))
}

// ── Serialize helper for the JoinResponse / etc. ──────────────────────
// (No-op crate marker so `Serialize` import isn't unused when methods
//  evolve to use it.)
#[allow(dead_code)]
fn _serialize_marker<T: Serialize>(_v: &T) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_has_canonical_name() {
        let kernel = Arc::new(Kernel::new());
        let svc = FederationService::new(Arc::clone(&kernel));
        assert_eq!(svc.name(), NAME);
        assert_eq!(NAME, "federation");
    }

    #[test]
    fn dispatch_returns_not_found_for_unknown_method() {
        let kernel = Arc::new(Kernel::new());
        let svc = FederationService::new(kernel);
        match svc.dispatch("federation_unknown_method", b"{}") {
            Err(RustCallError::NotFound) => {}
            other => panic!("expected NotFound, got {other:?}"),
        }
    }

    #[test]
    fn dispatch_handles_empty_payload_for_no_arg_method() {
        // list_zones takes no params; empty payload must parse as `{}`.
        let kernel = Arc::new(Kernel::new());
        let svc = FederationService::new(kernel);
        let resp = svc
            .dispatch("federation_list_zones", b"")
            .expect("list_zones with empty payload should succeed");
        let parsed: serde_json::Value =
            serde_json::from_slice(&resp).expect("response is valid JSON");
        assert!(parsed.get("zones").is_some(), "response carries 'zones' field: {parsed}");
    }

    #[test]
    fn dispatch_rejects_invalid_payload_for_create_zone() {
        let kernel = Arc::new(Kernel::new());
        let svc = FederationService::new(kernel);
        // missing zone_id
        match svc.dispatch("federation_create_zone", b"{}") {
            Err(RustCallError::InvalidArgument(_)) => {}
            other => panic!("expected InvalidArgument, got {other:?}"),
        }
    }
}
