//! `KernelAbi` — the canonical Rust syscall surface that every
//! in-process Rust service uses to reach the kernel.
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
//! kernel-internal result types (`SysReadResult`, `KernelError`, …)
//! on their existing module path.
//!
//! ## Surface scope
//!
//! Trait methods are 1:1 with the inherent `Kernel::sys_*` syscalls —
//! same name, same signature. No invented syscalls. No
//! kernel-internal struct accessors (`vfs_router_arc`,
//! `agent_registry`, `distributed_coordinator`, …); services that
//! need those reach them through the production-only
//! `impl ManagedAgentService<Kernel>` install paths or through
//! syscalls (a future `/__sys__/agents/{pid}/...` metadata-syscall
//! migration tracks the AgentRegistry case).
//!
//! `is_federation_initialized` is the one high-level probe in the
//! trait — it wraps `distributed_coordinator().is_initialized(self)`
//! because services need the boolean, not the coordinator handle.

use std::sync::Arc;

use contracts::{OperationContext, RustService};

use crate::core::dispatch::{FileEvent, NativeInterceptHook};
use crate::kernel::{
    KernelError, StatResult, SysReadResult, SysSetAttrResult, SysUnlinkResult, SysWriteResult,
};

/// Canonical syscall surface that every Rust service uses to reach
/// the kernel.
///
/// Bounds: `Send + Sync + 'static` so consumers can pass `Arc<K>`
/// across thread boundaries (the managed-agent runtime spawns OS
/// threads that hold a kernel handle).
pub trait KernelAbi: Send + Sync + 'static {
    // ── Syscalls (1:1 with inherent `Kernel::sys_*`) ────────────────

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

    /// Full inherent `sys_setattr` signature (17 args). Kernel-internal
    /// types (`Arc<dyn ObjectStore>`, `Arc<dyn MetaStore>`, `Box<dyn
    /// Any + Send + Sync>`) appear here because the trait lives in
    /// the kernel crate. Service callers that don't touch DT_MOUNT
    /// pass `""` / `None` for the mount-only params; production
    /// labelling (`/* backend */ None`, `/* metastore */ None`, …)
    /// keeps callsites readable.
    #[allow(clippy::too_many_arguments)]
    fn sys_setattr(
        &self,
        path: &str,
        entry_type: i32,
        backend_name: &str,
        backend: Option<Arc<dyn crate::abc::object_store::ObjectStore>>,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<Box<dyn std::any::Any + Send + Sync>>,
        io_profile: &str,
        zone_id: &str,
        is_external: bool,
        capacity: usize,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        mime_type: Option<&str>,
        modified_at_ms: Option<i64>,
        link_target: Option<&str>,
        source: Option<&str>,
        remote_metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
    ) -> Result<SysSetAttrResult, KernelError>;

    fn sys_stat(&self, path: &str, zone_id: &str) -> Option<StatResult>;

    /// Backend-direct readdir (bypasses native hooks). Used by ACP
    /// to scan `/__sys__/agents/` and `/__proc__/` without firing
    /// the agent-status pre-hook on every entry.
    fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String>;

    /// DT_PIPE creation helper. Used by `AcpSubprocess::spawn` to
    /// surface the agent's stdio fds inside VFS as
    /// `/{zone}/proc/{pid}/fd/{0,1,2}`. Stays a dedicated method
    /// because the `read_fd` / `write_fd` shape is pipe-specific and
    /// does not generalise into the generic `sys_setattr` matrix.
    fn setattr_pipe(
        &self,
        path: &str,
        capacity: usize,
        io_profile: &str,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        zone_id: &str,
    ) -> Result<SysSetAttrResult, KernelError>;

    // ── Event watch (inotify equivalent) ──────────────────────────

    /// Block until a file event matching `pattern` fires, or timeout.
    /// Returns `None` on timeout or when `timeout_ms == 0` (non-blocking
    /// try). Callers re-arm by calling again with a new `sys_watch`.
    ///
    /// Used by managed-agent runtimes to replace polling with
    /// event-driven blocking on `/proc/{pid}/chat-with-me` mailboxes.
    fn sys_watch(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent>;

    // ── Install-time control plane (LSM-style hook + Rust service
    //    registry) ────────────────────────────────────────────────

    fn register_native_hook(&self, hook: Box<dyn NativeInterceptHook>);

    fn register_rust_service(
        &self,
        name: &str,
        svc: Arc<dyn RustService>,
        deps: Vec<String>,
    ) -> Result<(), String>;

    // ── High-level federation probe ─────────────────────────────────

    /// True once `init_federation_from_env` has completed — the same
    /// readiness probe `setattr_mount` uses. Wraps
    /// `distributed_coordinator().is_initialized(self)` so service
    /// callers don't need to reach the coordinator handle.
    fn is_federation_initialized(&self) -> bool;
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

    fn sys_setattr(
        &self,
        path: &str,
        entry_type: i32,
        backend_name: &str,
        backend: Option<Arc<dyn crate::abc::object_store::ObjectStore>>,
        metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
        raft_backend: Option<Box<dyn std::any::Any + Send + Sync>>,
        io_profile: &str,
        zone_id: &str,
        is_external: bool,
        capacity: usize,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        mime_type: Option<&str>,
        modified_at_ms: Option<i64>,
        link_target: Option<&str>,
        source: Option<&str>,
        remote_metastore: Option<Arc<dyn crate::meta_store::MetaStore>>,
    ) -> Result<SysSetAttrResult, KernelError> {
        Self::sys_setattr(
            self,
            path,
            entry_type,
            backend_name,
            backend,
            metastore,
            raft_backend,
            io_profile,
            zone_id,
            is_external,
            capacity,
            read_fd,
            write_fd,
            mime_type,
            modified_at_ms,
            link_target,
            source,
            remote_metastore,
        )
    }

    fn sys_stat(&self, path: &str, zone_id: &str) -> Option<StatResult> {
        Self::sys_stat(self, path, zone_id)
    }

    fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String> {
        Self::sys_readdir_backend(self, path, zone_id)
    }

    fn setattr_pipe(
        &self,
        path: &str,
        capacity: usize,
        io_profile: &str,
        read_fd: Option<i32>,
        write_fd: Option<i32>,
        zone_id: &str,
    ) -> Result<SysSetAttrResult, KernelError> {
        Self::setattr_pipe(self, path, capacity, io_profile, read_fd, write_fd, zone_id)
    }

    fn sys_watch(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent> {
        Self::sys_watch(self, pattern, timeout_ms)
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

    fn is_federation_initialized(&self) -> bool {
        self.distributed_coordinator().is_initialized(self)
    }
}
