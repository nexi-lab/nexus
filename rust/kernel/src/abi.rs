//! `KernelAbi` — the canonical Rust syscall + accessor surface for
//! every in-process Rust service.
//!
//! All Rust services (in-tree `services::*` and any future
//! managed-agent runtime that lives alongside them) reach kernel
//! syscalls through `K: KernelAbi` instead of holding a concrete
//! `Arc<Kernel>`. The same generic codepath compiles for production
//! (`K = Kernel`, monomorphised at link time → identical perf to a
//! direct inherent call) and for unit tests (`K = MockKernel`).
//!
//! Layered against KERNEL-ARCHITECTURE.md §6.1: the analogue of
//! Linux's `include/linux/` syscall ABI surface, lifted into Rust as
//! a single trait. The trait declaration lives in `kernel::abi`
//! rather than in the `contracts` crate to keep the
//! kernel-internal result types (`SysReadResult`, `KernelError`,
//! `WalStreamCore`, …) on their existing module path; promoting
//! them to `contracts` is a future cleanup that does not block the
//! ABI shape.
//!
//! Surface scope: only what services actually call today (cf. the
//! per-service inventory in nexus-para-pc-3 plan §"KernelAbi Trait
//! Surface Inventory"). Methods are added as new consumers need
//! them; not 1:1 with `Kernel`'s full pub fn list.

use std::sync::Arc;

use contracts::{OperationContext, RustService};

use crate::core::agents::registry::AgentRegistry;
use crate::core::dispatch::NativeInterceptHook;
use crate::core::stream::wal::WalStreamCore;
use crate::core::vfs_router::VFSRouter;
use crate::hal::distributed_coordinator::DistributedCoordinator;
use crate::kernel::{
    KernelError, SysReadResult, SysSetAttrResult, SysUnlinkResult, SysWriteResult,
};
use crate::meta_store::FileMetadata;

/// Canonical syscall + accessor surface that every Rust service
/// uses to reach the kernel.
///
/// Bounds: `Send + Sync + 'static` so consumers can pass
/// `Arc<K>` across thread boundaries (the managed-agent runtime
/// spawns OS threads that hold a kernel handle).
pub trait KernelAbi: Send + Sync + 'static {
    // ── Syscalls ─────────────────────────────────────────────────────

    fn sys_read(
        &self,
        path: &str,
        ctx: &OperationContext,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError>;

    fn sys_write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError>;

    fn sys_unlink(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysUnlinkResult, KernelError>;

    /// Service-facing subset of `Kernel::sys_setattr`. Covers the
    /// DT_DIR / DT_LINK / DT_STREAM / UPDATE shapes services use;
    /// DT_MOUNT params (backend / metastore / raft_backend / source /
    /// remote_metastore) stay on the inherent `sys_setattr` because
    /// they are kernel-construction concerns, not service-call
    /// concerns.
    #[allow(clippy::too_many_arguments)]
    fn sys_setattr_simple(
        &self,
        path: &str,
        entry_type: i32,
        zone_id: &str,
        capacity: usize,
        io_profile: &str,
        mime_type: Option<&str>,
        link_target: Option<&str>,
    ) -> Result<SysSetAttrResult, KernelError>;

    /// Backend-direct readdir (bypasses native hooks). Used by ACP
    /// to scan `/__sys__/agents/` and `/__proc__/` without firing
    /// the agent-status pre-hook on every entry.
    fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String>;

    // ── Metastore (single-key surface used by services) ──────────────

    fn metastore_get(&self, path: &str) -> Result<Option<FileMetadata>, KernelError>;
    fn metastore_delete(&self, path: &str) -> Result<bool, KernelError>;

    // ── Hook + service registration ─────────────────────────────────

    fn register_native_hook(&self, hook: Box<dyn NativeInterceptHook>);

    fn register_rust_service(
        &self,
        name: &str,
        svc: Arc<dyn RustService>,
        deps: Vec<String>,
    ) -> Result<(), String>;

    // ── Accessors returning shared SSOT handles ─────────────────────

    fn agent_registry(&self) -> &Arc<AgentRegistry>;

    fn distributed_coordinator(&self) -> Arc<dyn DistributedCoordinator>;

    fn vfs_router_arc(&self) -> Arc<VFSRouter>;

    // ── Audit stream lifecycle ──────────────────────────────────────

    fn prepare_audit_stream(
        &self,
        zone_id: &str,
        stream_path: &str,
    ) -> Result<Arc<WalStreamCore>, KernelError>;
}

// ── `impl KernelAbi for Kernel` ──────────────────────────────────────
//
// Pure forwarder — every method delegates to the inherent fn of the
// same name on `Kernel`. Monomorphisation at the binary link site
// inlines through the trait dispatch back to the inherent call,
// recovering 100% of the direct-call perf.

impl KernelAbi for crate::kernel::Kernel {
    fn sys_read(
        &self,
        path: &str,
        ctx: &OperationContext,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError> {
        Self::sys_read(self, path, ctx, timeout_ms, offset)
    }

    fn sys_write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError> {
        Self::sys_write(self, path, ctx, content, offset)
    }

    fn sys_unlink(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysUnlinkResult, KernelError> {
        Self::sys_unlink(self, path, ctx, recursive)
    }

    fn sys_setattr_simple(
        &self,
        path: &str,
        entry_type: i32,
        zone_id: &str,
        capacity: usize,
        io_profile: &str,
        mime_type: Option<&str>,
        link_target: Option<&str>,
    ) -> Result<SysSetAttrResult, KernelError> {
        self.sys_setattr(
            path,
            entry_type,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            io_profile,
            zone_id,
            /* is_external */ false,
            capacity,
            /* read_fd */ None,
            /* write_fd */ None,
            mime_type,
            /* modified_at_ms */ None,
            link_target,
            /* source */ None,
            /* remote_metastore */ None,
        )
    }

    fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String> {
        Self::sys_readdir_backend(self, path, zone_id)
    }

    fn metastore_get(&self, path: &str) -> Result<Option<FileMetadata>, KernelError> {
        Self::metastore_get(self, path)
    }

    fn metastore_delete(&self, path: &str) -> Result<bool, KernelError> {
        Self::metastore_delete(self, path)
    }

    fn register_native_hook(&self, hook: Box<dyn NativeInterceptHook>) {
        Self::register_native_hook(self, hook)
    }

    fn register_rust_service(
        &self,
        name: &str,
        svc: Arc<dyn RustService>,
        deps: Vec<String>,
    ) -> Result<(), String> {
        Self::register_rust_service(self, name, svc, deps)
    }

    fn agent_registry(&self) -> &Arc<AgentRegistry> {
        Self::agent_registry(self)
    }

    fn distributed_coordinator(&self) -> Arc<dyn DistributedCoordinator> {
        Self::distributed_coordinator(self)
    }

    fn vfs_router_arc(&self) -> Arc<VFSRouter> {
        Self::vfs_router_arc(self)
    }

    fn prepare_audit_stream(
        &self,
        zone_id: &str,
        stream_path: &str,
    ) -> Result<Arc<WalStreamCore>, KernelError> {
        Self::prepare_audit_stream(self, zone_id, stream_path)
    }
}
