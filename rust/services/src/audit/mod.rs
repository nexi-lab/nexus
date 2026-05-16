//! AuditHook — native Rust [`NativeInterceptHook`] that records VFS operations
//! to a WAL-backed DT_STREAM audit log.
//!
//! Hot-path cost: AuditRecord struct construction + mpsc::SyncSender::try_send
//! (~100–300 ns). JSON serialization and `kernel.sys_write` happen in
//! a background thread, entirely off the VFS dispatch critical path.
//!
//! Per the architecture's `services` ⊥ `backends` ⊥ `transport` ⊥
//! `raft` peer-crate split, construction + registration is owned by the
//! service tier (this module's [`install`] function); the kernel only
//! exposes the syscall surface (`sys_setattr` for stream creation,
//! `sys_write` for stream appends, `register_native_hook` for hook
//! installation).
//!
//! ## Boot wiring (Linux LSM analogue)
//!
//! ```ignore
//! services::audit::install(&kernel, "root", "/__sys__/audit/traces/")?;
//! // 1. kernel.sys_setattr(stream_path, DT_STREAM, …, "wal", zone)
//! //    — service-side syscall; kernel composes the WAL stream.
//! // 2. AuditHook::new(kernel, stream_path, zone)
//! //    — service concern: hook impl that writes back via sys_write.
//! // 3. kernel.register_native_hook(Box::new(hook))
//! //    — install-time control plane (LSM-style EXPORT_SYMBOL).
//! ```

use std::collections::HashSet;
use std::sync::mpsc;
use std::sync::Arc;

use chrono::SecondsFormat;
use contracts::{is_system_path, OperationContext};
use parking_lot::Mutex;
use serde::Serialize;

use kernel::abi::KernelAbi;
use kernel::core::dispatch::{FileEvent, FileEventType, HookContext, MutationObserver, NativeInterceptHook};
use kernel::kernel::{Kernel, KernelError};

/// DT_STREAM entry-type discriminant (mirrors `kernel::core::dcache::DT_STREAM`).
const DT_STREAM: i32 = 4;

/// A single VFS operation record, serialised to JSON and appended to the
/// audit WAL stream.
#[derive(Debug, Serialize)]
pub struct AuditRecord {
    /// Schema version — increment when fields are added/removed.
    pub v: u8,
    /// ISO-8601 timestamp with millisecond precision.
    pub ts: String,
    pub agent_id: String,
    pub user_id: String,
    pub zone_id: String,
    /// VFS operation name: "write", "read", "delete", "rename", …
    pub op: &'static str,
    pub path: String,
    /// "ok" (only successful operations are audited; pre-hook aborts are not).
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_new: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub new_path: Option<String>,
}

/// VFS audit hook — implements `NativeInterceptHook` so it can be registered
/// with `kernel.register_native_hook` and receive post-dispatch callbacks
/// without crossing the PyO3 boundary.
///
/// Holds an `Arc<K>` to the kernel + the audit stream path; on each
/// post-hook the record is serialised in a background thread and
/// appended via `kernel.sys_write(audit_path, …)`. DT_STREAM
/// short-circuits inside `sys_write` (kernel `io.rs`), so audit writes
/// don't recursively re-enter the audit hook.
pub struct AuditHook<K: KernelAbi> {
    sender: mpsc::SyncSender<AuditRecord>,
    _kernel: Arc<K>,
}

impl<K: KernelAbi> AuditHook<K> {
    /// Background flush channel capacity. At ~300 B per JSON record this is
    /// ~2.5 MB worst-case before try_send drops records (best-effort audit).
    const CHANNEL_CAP: usize = 8192;

    /// Create an AuditHook that appends records to `audit_path` via
    /// `kernel.sys_write`. Spawns a background flush thread that
    /// serialises records to JSON and calls the syscall.
    ///
    /// The caller MUST pass an `audit_path` under
    /// [`contracts::SYSTEM_PATH_PREFIX`]. Native (unnamed) hooks like
    /// AuditHook need to keep their own state under `/__sys__/` so
    /// `on_post`'s `is_system_path()` short-circuit covers
    /// self-writes uniformly with the rest of the kernel-internal
    /// namespace — see [`Self::on_post`].
    pub fn new(kernel: Arc<K>, audit_path: String, zone_id: String) -> Self {
        debug_assert!(
            is_system_path(&audit_path),
            "AuditHook stream path must live under {} (got {audit_path:?}); \
             native unnamed hooks store state in the kernel-internal \
             namespace so the on_post system-path guard covers self-writes.",
            contracts::SYSTEM_PATH_PREFIX,
        );

        let (tx, rx) = mpsc::sync_channel::<AuditRecord>(Self::CHANNEL_CAP);
        let kernel_for_thread = Arc::clone(&kernel);

        std::thread::Builder::new()
            .name("audit-flush".into())
            .spawn(move || {
                let ctx = audit_writer_ctx(&zone_id);
                while let Ok(record) = rx.recv() {
                    match serde_json::to_vec(&record) {
                        Ok(json) => {
                            if let Err(e) =
                                kernel_for_thread.sys_write(&audit_path, &ctx, &json, 0)
                            {
                                tracing::warn!(error = ?e, "audit stream write failed");
                            }
                        }
                        Err(e) => {
                            tracing::warn!(error = %e, "audit record serialisation failed");
                        }
                    }
                }
            })
            .expect("failed to spawn audit flush thread");

        Self {
            sender: tx,
            _kernel: kernel,
        }
    }

    fn build_record(ctx: &HookContext, op: &'static str) -> AuditRecord {
        let path = ctx.path().to_string();
        let id = ctx.identity();
        let (size_bytes, is_new, new_path) = match ctx {
            HookContext::Write(c) => (c.size_bytes, Some(c.is_new_file), None),
            HookContext::Read(c) => (c.content.as_ref().map(|b| b.len() as u64), None, None),
            HookContext::Rename(c) => (None, None, Some(c.new_path.clone())),
            _ => (None, None, None),
        };
        AuditRecord {
            v: 1,
            ts: chrono::Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true),
            agent_id: id.agent_id.clone(),
            user_id: id.user_id.clone(),
            zone_id: id.zone_id.clone(),
            op,
            path,
            status: "ok",
            size_bytes,
            is_new,
            new_path,
        }
    }
}

/// `OperationContext` that the audit-flush thread uses for its
/// `sys_write` calls. Marked `is_system = true` so the audit writer
/// bypasses ReBAC / permission checks — audit is infrastructure, not a
/// user-issued op.
fn audit_writer_ctx(zone_id: &str) -> OperationContext {
    let mut ctx = OperationContext::new(
        /* user_id */ "audit",
        zone_id,
        /* is_admin */ true,
        /* agent_id */ Some("audit"),
        /* is_system */ true,
    );
    ctx.subject_type = "service".to_string();
    ctx
}

impl<K: KernelAbi> NativeInterceptHook for AuditHook<K> {
    fn name(&self) -> &str {
        "audit"
    }

    fn on_post(&self, ctx: &HookContext) {
        // Hook self-exclusion: AuditHook is "unnamed" (LSM-style
        // global; runs on every path) and the audit-flush thread
        // itself writes records via `kernel.sys_write(audit_path,
        // ...)`. Without this guard, every audit write re-enters
        // `on_post`, enqueues another record, writes again, and
        // recurses.
        //
        // The kernel contract for native unnamed hooks is that their
        // state lives under [`contracts::SYSTEM_PATH_PREFIX`]
        // (`/__sys__/`), the same prefix Python's
        // `PermissionHook._is_system_path()` uses to break recursion
        // (PR #3890 CI hang). `AuditHook::new` debug-asserts the
        // caller followed that contract, so the single
        // `is_system_path` check here covers the audit-stream
        // self-write — no separate `path == audit_path` is needed.
        if is_system_path(ctx.path()) {
            return;
        }
        let op = match ctx {
            HookContext::Write(_) => "write",
            HookContext::Read(_) => "read",
            HookContext::Delete(_) => "delete",
            HookContext::Rename(_) => "rename",
            HookContext::Mkdir(_) => "mkdir",
            HookContext::Rmdir(_) => "rmdir",
            HookContext::Copy(_) => "copy",
            HookContext::Stat(_) => "stat",
            HookContext::Access(_) => "access",
            HookContext::WriteBatch(_) => "write_batch",
        };
        let record = Self::build_record(ctx, op);
        // Non-blocking — drop silently on backpressure (audit is best-effort).
        let _ = self.sender.try_send(record);
    }
}

/// Boot-time DI entry point — install an `AuditHook` for `zone_id`.
///
/// Service-tier responsibility (this whole module). Three steps, each
/// crossing a clean tier boundary:
///
/// 1. `kernel.sys_setattr(stream_path, DT_STREAM, …, "wal", zone_id, …)`
///    — syscall; kernel composes the WAL stream backed by the
///    coordinator's per-zone metastore + registers it with
///    `StreamManager` + seeds the inode.
/// 2. `AuditHook::new(kernel, stream_path, zone_id)` — local services
///    concern: build the hook impl that holds an `Arc<K>` for syscall
///    callbacks.
/// 3. `kernel.register_native_hook(Box::new(hook))` — install-time
///    control plane (LSM-style); kernel records the hook in its
///    native dispatch registry without ever knowing the concrete type.
///
/// Idempotent: `sys_setattr` for an existing DT_STREAM is a no-op
/// re-open; the `register_native_hook` side is not — calling `install`
/// twice for the same zone double-registers the hook. Callers
/// (typically `nexus.__init__` boot path) call this exactly once per zone.
pub fn install<K: KernelAbi>(
    kernel: Arc<K>,
    zone_id: &str,
    stream_path: &str,
) -> Result<(), KernelError> {
    setup_audit_stream(kernel.as_ref(), zone_id, stream_path)?;
    let hook = AuditHook::new(
        Arc::clone(&kernel),
        stream_path.to_string(),
        zone_id.to_string(),
    );
    kernel.register_native_hook(Box::new(hook));
    Ok(())
}

/// Register the audit DT_STREAM locally without installing the
/// generator hook. Used by audit-node deployments that join
/// production zones as raft learners — they need the WAL stream
/// registered in the local `stream_manager` so `stream_read_batch`
/// returns committed records (replicated by raft into their local
/// MetaStore), but they do NOT generate VFS ops of their own and so
/// must not register the `AuditHook` writer.
///
/// Idempotent on repeated calls per zone (same shape as `install`).
pub fn prepare_stream_only<K: KernelAbi>(
    kernel: &K,
    zone_id: &str,
    stream_path: &str,
) -> Result<(), KernelError> {
    setup_audit_stream(kernel, zone_id, stream_path)
}

/// Boot-time install for the root zone + auto-wire for every zone
/// that mounts later. Companion to [`install`] for deployments that
/// want audit on every zone, not just the one named at boot.
///
/// Steps:
///   1. Install AuditHook + DT_STREAM for `root_zone_id` (one call
///      to [`install`]) so the boot-time stream is ready before any
///      VFS op fires.
///   2. Register a [`ZoneAuditAutoWire`] [`MutationObserver`] on
///      `kernel` filtering [`FileEventType::Mount`]. Each Mount
///      event maps to a per-zone [`install`] call (the same code
///      path used at boot for `root_zone_id`), guarded by an
///      internal `HashSet<String>` so a re-mount is a harmless
///      no-op.
///
/// `K = Kernel`-specific (not generic over `K: KernelAbi`) because
/// `kernel.register_observer` is a kernel-internal accessor — same
/// reason `ManagedAgentService::install_returning` is gated to
/// `K = Kernel`. Slim builds that ship a non-Kernel `K` use
/// [`install`] for the single boot zone and don't get the auto-wire.
///
/// See `docs/architecture/nexus-integration-architecture.md` §5.3
/// for the architectural rationale.
pub fn install_root(
    kernel: &Arc<Kernel>,
    root_zone_id: &str,
    stream_path: &str,
) -> Result<(), KernelError> {
    install(Arc::clone(kernel), root_zone_id, stream_path)?;

    let mut seeded = HashSet::new();
    seeded.insert(root_zone_id.to_string());

    let auto_wire = Arc::new(ZoneAuditAutoWire {
        kernel: Arc::clone(kernel),
        installed: Mutex::new(seeded),
        stream_path: stream_path.to_string(),
    });
    kernel.register_observer(
        auto_wire,
        ZoneAuditAutoWire::OBSERVER_NAME.to_string(),
        FileEventType::Mount as u32,
    );
    Ok(())
}

/// [`MutationObserver`] that auto-installs AuditHook for newly
/// mounted zones. Registered exactly once by [`install_root`] and
/// keeps its own `HashSet` of zones it has already installed for so
/// re-mount events are no-ops.
struct ZoneAuditAutoWire {
    kernel: Arc<Kernel>,
    /// Zones the observer has already wired AuditHook for. The
    /// root zone is seeded into this set by [`install_root`] before
    /// the observer is registered, so a re-mount of the root zone
    /// also no-ops.
    installed: Mutex<HashSet<String>>,
    stream_path: String,
}

impl ZoneAuditAutoWire {
    pub(crate) const OBSERVER_NAME: &'static str = "audit-zone-auto-wire";
}

impl MutationObserver for ZoneAuditAutoWire {
    fn on_mutation(&self, event: &FileEvent) {
        let zone_id = match event.zone_id() {
            Some(z) if !z.is_empty() => z.to_string(),
            _ => return,
        };
        // First-writer wins on the HashSet — drop the lock before
        // calling install() so a re-entrant Mount event (impossible
        // today, defensive) wouldn't deadlock.
        {
            let mut guard = self.installed.lock();
            if !guard.insert(zone_id.clone()) {
                return;
            }
        }
        if let Err(e) = install(Arc::clone(&self.kernel), &zone_id, &self.stream_path) {
            tracing::warn!(
                zone = %zone_id,
                error = ?e,
                "audit auto-wire install_for_zone failed",
            );
        }
    }
}

fn setup_audit_stream<K: KernelAbi>(
    kernel: &K,
    zone_id: &str,
    stream_path: &str,
) -> Result<(), KernelError> {
    kernel.sys_setattr(
        stream_path,
        DT_STREAM,
        /* backend_name */ "",
        /* backend */ None,
        /* metastore */ None,
        /* raft_backend */ None,
        /* io_profile */ "wal",
        zone_id,
        /* is_external */ false,
        /* capacity */ 0,
        /* read_fd */ None,
        /* write_fd */ None,
        /* mime_type */ None,
        /* modified_at_ms */ None,
        /* content_id */ None,
        /* size */ None,
        /* version */ None,
        /* created_at_ms */ None,
        /* link_target */ None,
        /* source */ None,
        /* remote_metastore */ None,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use kernel::kernel::Kernel;

    fn fresh_kernel() -> Arc<Kernel> {
        Arc::new(Kernel::new())
    }

    // ── Test federation fixture ──────────────────────────────────────
    //
    // `audit::install`'s `io_profile="wal"` codepath requires a
    // `DistributedCoordinator` so the WAL stream backend can route
    // through `coordinator.metastore_for_zone(zone)`. A bare
    // `Kernel::new()` ships with `NoopDistributedCoordinator` which
    // returns `is_initialized=false` — the stream setattr rejects
    // with `"io_profile=wal requires federation"`.
    //
    // `TestFederationCoordinator` is the minimal stub: reports
    // `is_initialized=true` and hands back a per-zone tempdir-backed
    // `LocalMetaStore` from `metastore_for_zone` (lazily created and
    // cached per zone, so cross-zone tests get the production-style
    // "different zones have different metastores" semantics rather
    // than sharing one store across zones). Inspired by the kernel's
    // own federation_wal_e2e fixture (`rust/kernel/src/kernel/mod.rs:
    // 3991-4065`) but with multi-zone support, kept local to audit
    // tests to avoid promoting it to the kernel public surface.
    mod federation_stub {
        use std::collections::HashMap;
        use std::sync::Arc;

        use kernel::abc::meta_store::MetaStore;
        use kernel::hal::distributed_coordinator::{
            ClusterInfo, CoordinatorResult, DistributedCoordinator, ShareInfo,
        };
        use kernel::kernel::Kernel;
        use kernel::meta_store::LocalMetaStore;
        use parking_lot::Mutex;
        use tempfile::TempDir;

        pub(super) struct TestFederationCoordinator {
            tempdir: TempDir,
            /// Per-zone metastore cache. Lazily populated on each
            /// `metastore_for_zone(zone)` call so the test fixture
            /// matches production's "every zone owns its own
            /// metastore" semantics.
            stores: Mutex<HashMap<String, Arc<dyn MetaStore>>>,
        }

        impl TestFederationCoordinator {
            pub(super) fn new() -> Self {
                Self {
                    tempdir: TempDir::new().expect("tempdir for audit fed-stub"),
                    stores: Mutex::new(HashMap::new()),
                }
            }
        }

        impl DistributedCoordinator for TestFederationCoordinator {
            fn list_zones(&self, _kernel: &Kernel) -> Vec<String> {
                self.stores.lock().keys().cloned().collect()
            }
            fn is_initialized(&self, _kernel: &Kernel) -> bool {
                true
            }
            fn cluster_info(&self, _: &Kernel, _: &str) -> CoordinatorResult<ClusterInfo> {
                Err("test coordinator: cluster_info unused".into())
            }
            fn create_zone(&self, _: &Kernel, _: &str) -> CoordinatorResult<()> {
                Ok(())
            }
            fn remove_zone(&self, _: &Kernel, _: &str, _: bool) -> CoordinatorResult<()> {
                Err("test coordinator: remove_zone unused".into())
            }
            fn join_zone(&self, _: &Kernel, _: &str, _: bool) -> CoordinatorResult<()> {
                Err("test coordinator: join_zone unused".into())
            }
            fn wire_mount(&self, _: &Kernel, _: &str, _: &str, _: &str) -> CoordinatorResult<()> {
                Ok(())
            }
            fn unwire_mount(&self, _: &Kernel, _: &str, _: &str) -> CoordinatorResult<()> {
                Err("test coordinator: unwire_mount unused".into())
            }
            fn share_zone(&self, _: &Kernel, _: &str, _: &str) -> CoordinatorResult<ShareInfo> {
                Err("test coordinator: share_zone unused".into())
            }
            fn lookup_share(
                &self,
                _: &Kernel,
                _: &str,
            ) -> CoordinatorResult<Option<ShareInfo>> {
                Ok(None)
            }
            fn metastore_for_zone(
                &self,
                _: &Kernel,
                zone_id: &str,
            ) -> CoordinatorResult<Arc<dyn MetaStore>> {
                let mut stores = self.stores.lock();
                if let Some(existing) = stores.get(zone_id) {
                    return Ok(Arc::clone(existing));
                }
                let path = self
                    .tempdir
                    .path()
                    .join(format!("zone-{zone_id}.redb"));
                let store: Arc<dyn MetaStore> = Arc::new(
                    LocalMetaStore::open(&path)
                        .map_err(|e| format!("LocalMetaStore::open({path:?}): {e:?}"))?,
                );
                stores.insert(zone_id.to_string(), Arc::clone(&store));
                Ok(store)
            }
            fn locks_for_zone(
                &self,
                _: &Kernel,
                _: &str,
            ) -> CoordinatorResult<Arc<dyn contracts::lock_state::Locks>> {
                Err("test coordinator: locks_for_zone unused".into())
            }
        }
    }

    /// Build a `Kernel` with `TestFederationCoordinator` installed
    /// so `is_federation_initialized()` returns true and the WAL
    /// stream backend can route through the per-zone metastore.
    fn fresh_federated_kernel() -> Arc<Kernel> {
        let kernel = Arc::new(Kernel::new());
        kernel.set_distributed_coordinator(Arc::new(
            federation_stub::TestFederationCoordinator::new(),
        )
            as Arc<dyn kernel::hal::distributed_coordinator::DistributedCoordinator>);
        kernel
    }

    /// Smoke test for the federation stub: `install_root` against a
    /// federated kernel composes the root-zone audit DT_STREAM
    /// successfully (and is idempotent on re-install).
    #[test]
    fn install_root_against_federated_kernel_composes_root_audit_stream() {
        let kernel = fresh_federated_kernel();
        install_root(&kernel, "root", "/__sys__/audit/traces/").expect("install_root");

        // Idempotent: a second install_root call surfaces — install()
        // re-uses the existing DT_STREAM (sys_setattr DT_STREAM is a
        // no-op re-open), the auto-wire HashSet has "root" seeded
        // again (no-op insert), and a second observer registration is
        // harmless beyond a slight per-dispatch cost.
        install_root(&kernel, "root", "/__sys__/audit/traces/").expect("install_root idempotent");
    }

    /// Helper that issues `sys_setattr DT_MOUNT` for a new zone with
    /// the federated-test-friendly default args (empty backend_name,
    /// `None` for backend / metastore / raft_backend, `"memory"`
    /// io_profile for the mount itself — the per-zone audit DT_STREAM
    /// `install()` creates is what uses `"wal"`).
    fn mount_zone(kernel: &Arc<Kernel>, path: &str, zone_id: &str) {
        use kernel::meta_store::DT_MOUNT;
        kernel
            .sys_setattr(
                path,
                DT_MOUNT as i32,
                /* backend_name */ "",
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
                /* content_id */ None,
                /* size */ None,
                /* version */ None,
                /* created_at_ms */ None,
                /* link_target */ None,
                /* source */ None,
                /* remote_metastore */ None,
            )
            .expect("sys_setattr DT_MOUNT");
    }

    /// Mount → auto-wire → observer-records-zone integration test.
    ///
    /// `install_root` registers a private `ZoneAuditAutoWire`
    /// instance internally, so the test can't get a handle to that
    /// one. Instead the test constructs its own `ZoneAuditAutoWire`,
    /// registers it on the kernel with the same `FileEventType::Mount`
    /// mask `install_root` would use, and asserts that a real
    /// `sys_setattr DT_MOUNT` reaches `on_mutation` with the new
    /// zone's id — proving the kernel's Mount-dispatch wiring (C7) +
    /// observer dispatch path land at the auto-wire correctly.
    ///
    /// Re-mount idempotency is exercised end-to-end too: a second
    /// `sys_setattr DT_MOUNT` for the same zone fires the observer
    /// again, but the `installed` HashSet still has exactly one
    /// entry per zone afterwards.
    #[test]
    fn dt_mount_dispatch_reaches_zone_audit_auto_wire_observer() {
        let kernel = fresh_federated_kernel();

        // Pre-seed the audit DT_STREAM for the root zone via
        // `install` so the observer's per-zone `install` calls can
        // succeed against the federated kernel.
        install(Arc::clone(&kernel), "root", "/__sys__/audit/traces/").expect("install root");

        // Construct + register the auto-wire ourselves so we can
        // inspect its `installed` HashSet after the dispatch fires.
        let auto_wire = Arc::new(ZoneAuditAutoWire {
            kernel: Arc::clone(&kernel),
            installed: Mutex::new({
                let mut s = HashSet::new();
                s.insert("root".to_string());
                s
            }),
            stream_path: "/__sys__/audit/traces/".to_string(),
        });
        kernel.register_observer(
            Arc::clone(&auto_wire) as Arc<dyn MutationObserver>,
            ZoneAuditAutoWire::OBSERVER_NAME.to_string(),
            FileEventType::Mount as u32,
        );

        // Mount audit-z1 through the real syscall path — dispatches
        // FileEventType::Mount → auto-wire on_mutation fires.
        mount_zone(&kernel, "/mnt/audit-z1", "audit-z1");
        assert!(
            auto_wire.installed.lock().contains("audit-z1"),
            "auto-wire HashSet should contain audit-z1 after Mount dispatch",
        );

        // Mount audit-z2 — fires for the new zone too.
        mount_zone(&kernel, "/mnt/audit-z2", "audit-z2");
        assert!(auto_wire.installed.lock().contains("audit-z2"));

        // Re-mount audit-z1 — fires again, but HashSet entries don't
        // multiply: still exactly 3 (root + audit-z1 + audit-z2).
        mount_zone(&kernel, "/mnt/audit-z1", "audit-z1");
        let installed = auto_wire.installed.lock();
        assert_eq!(
            installed.len(),
            3,
            "re-mount of audit-z1 must not duplicate the HashSet entry",
        );
    }

    /// `ZoneAuditAutoWire` records each zone in its HashSet exactly
    /// once and no-ops on a re-mount. Drives the observer directly
    /// (skipping the `install` call by short-circuiting through the
    /// HashSet-already-contains check) so the test doesn't depend on
    /// federation being wired — `install`'s `io_profile="wal"`
    /// requires `DistributedCoordinator`, which a bare
    /// `Kernel::new()` doesn't have.
    #[test]
    fn zone_audit_auto_wire_dedups_by_zone_id() {
        let kernel = fresh_kernel();
        let mut seeded = HashSet::new();
        // Pre-seed every zone we'll fire events for — the HashSet
        // check short-circuits before install() runs, so we exercise
        // only the dedup logic without needing federation.
        seeded.insert("z1".to_string());
        seeded.insert("z2".to_string());

        let auto_wire = ZoneAuditAutoWire {
            kernel,
            installed: Mutex::new(seeded),
            stream_path: "/__sys__/audit/traces/".to_string(),
        };

        // Fire Mount events for already-seeded zones — short-circuits
        // via HashSet check, install() never runs, no panic on missing
        // federation.
        auto_wire.on_mutation(&FileEvent::with_zone(FileEventType::Mount, "/mnt/z1", "z1"));
        auto_wire.on_mutation(&FileEvent::with_zone(FileEventType::Mount, "/mnt/z1", "z1"));
        auto_wire.on_mutation(&FileEvent::with_zone(FileEventType::Mount, "/mnt/z2", "z2"));

        // Events with an empty zone_id are ignored (early return);
        // proves the observer doesn't index `installed` on a blank
        // key.
        auto_wire.on_mutation(&FileEvent::with_zone(
            FileEventType::Mount,
            "/mnt/no-zone",
            "",
        ));

        let installed = auto_wire.installed.lock();
        assert!(installed.contains("z1"));
        assert!(installed.contains("z2"));
        // Still exactly the two we seeded — no spurious inserts from
        // re-mounts or from zone-less events.
        assert_eq!(installed.len(), 2);
    }
}
