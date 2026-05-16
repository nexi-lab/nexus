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
