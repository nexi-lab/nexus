//! `nexus-cdylib` — the single Python entry-point cdylib for Nexus.
//!
//! This crate is a *build artifact*, not an architectural tier
//! (Linux's `make bzImage` analogue — bundles the rlibs into one
//! loadable image). It owns the sole `#[pymodule] fn nexus_runtime`
//! across the workspace and pulls together the rlibs that compose
//! the runtime:
//!
//! * [`lib`] — pure-Rust algorithms + transport primitives (libc
//!   analogue, §6 tier-neutral)
//! * [`kernel`] — pillars + primitives + syscalls
//! * [`nexus_raft`] — Raft / federation
//! * `backends` — driver impls
//! * `services` — post-syscall hooks (audit / permission / agents / tasks)
//! * `transport` — network surface tier (VFS gRPC server + IPC + driver-outgoing clients)
//!
//! Each peer rlib exposes its own `python::register(&Bound<PyModule>)`
//! function; this cdylib is just the envelope that calls all of them.
//!
//! See `docs/architecture/KERNEL-ARCHITECTURE.md` §6.1 for the
//! cycle-break rationale (why kernel is rlib-only and the cdylib
//! lives in its own crate).

use std::sync::Arc;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use kernel::core::agents::registry::AgentDescriptor;
use kernel::generated_kernel_abi_pyo3::PyKernel;
use kernel::kernel::Kernel;
use services::managed_agent::{
    install_managed_agent_with_spawn, SpawnHandle as ServiceSpawnHandle, SpawnTask,
};

// ── Sudo-code adapter ──────────────────────────────────────────────────
//
// Concrete `SpawnTask<Kernel>` impl that wraps
// `sudocode_runtime::spawn_task::spawn_task_echo::<K>(...)`.
//
// Lives in the cdylib (not services rlib, not in a dedicated adapter
// crate) because the binary-edge cdylib is the allowed seam for
// cross-repo runtime deps — services rlib stays runtime-agnostic so
// the cluster slim binary can ship without sudocode.
//
// ## v1 echo → v2 ConversationRuntime upgrade path
//
// sudocode PR#100 introduced `spawn_task` v2 which drives a full
// `ConversationRuntime` per-pid. The v2 signature requires
// `ApiClient + ToolExecutor + SystemPrompt + PermissionPolicy +
// state_callback` — providers that the cdylib adapter must construct.
// Until those factories land (tracked as `spawn_task v2 full
// integration`), the adapter calls `spawn_task_echo` which retains
// the v1 echo round-trip body. The v2 upgrade is:
//
//   1. Add `sudocode_runtime::spawn_task::spawn_task` (7-param) call
//   2. Construct ApiClient from StartSessionRequest.model label
//   3. Construct ToolExecutor using KernelFsBackend
//   4. Build SystemPrompt from VFS `/agents/{name}/config/`
//   5. Wire state_callback → AgentRegistry::update_state
//
// Generic-K monomorphisation: `spawn_task_echo` is generic over
// `K: KernelAbi + Send + Sync + 'static` and `SudoCodeSpawnAdapter`
// impls `SpawnTask<Kernel>` — the compiler monomorphises against
// the concrete kernel type (no per-`sys_read` vtable cost).

struct SudoCodeSpawnHandle(sudocode_runtime::spawn_task::SpawnHandle);

impl ServiceSpawnHandle for SudoCodeSpawnHandle {
    fn abort(&self) {
        // Idempotent — `HookAbortSignal::abort` flips an
        // `AtomicBool::store(true, Ordering::Release)` so concurrent
        // callers (on_terminate observer + an in-flight
        // `cancel(Session)`) both succeed.
        self.0.abort_signal.abort();
    }
}

struct SudoCodeSpawnAdapter;

impl SpawnTask<Kernel> for SudoCodeSpawnAdapter {
    fn spawn(
        &self,
        kernel: Arc<Kernel>,
        desc: AgentDescriptor,
    ) -> Box<dyn ServiceSpawnHandle> {
        // v1 echo body — polls /proc/{pid}/chat-with-me and echoes
        // inbound prompts back. Swapped for the full ConversationRuntime
        // body once the ApiClient/ToolExecutor factory wiring lands.
        let handle = sudocode_runtime::spawn_task::spawn_task_echo::<Kernel>(kernel, desc);
        Box::new(SudoCodeSpawnHandle(handle))
    }
}

/// Install ManagedAgentService on `kernel` with the sudocode-runtime
/// spawn adapter wired in. This is the cdylib (Python wheel) entry
/// for Python deployments; pure-Rust slim builds (cluster binary)
/// call `services::managed_agent::install_managed_agent` directly
/// without a runtime body.
///
/// Python signature:
///
/// ```python
/// nexus_runtime.nx_managed_agent_install(kernel)
/// ```
#[pyfunction]
#[pyo3(name = "nx_managed_agent_install")]
fn nx_managed_agent_install(py_kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    let provider: Arc<dyn SpawnTask<Kernel>> = Arc::new(SudoCodeSpawnAdapter);
    install_managed_agent_with_spawn(&py_kernel.kernel_arc(), provider)
        .map_err(PyRuntimeError::new_err)
}

#[pymodule]
fn nexus_runtime(m: &Bound<PyModule>) -> PyResult<()> {
    // §6 lib (libc analogue) — pure-Rust algorithm wrappers.
    lib::python::register(m)?;
    // §3 / §4 kernel — pillars + primitives + #[pyclass] surface.
    // The cdylib boot path calls `install_federation_wiring(kernel)`
    // to swap the kernel's `NoopDistributedCoordinator` for the real
    // `RaftDistributedCoordinator` so federation-aware syscalls
    // dispatch through the §3.B Control-Plane HAL trait.
    kernel::python::register(m)?;
    // Raft / federation — ZoneManager / ZoneHandle / MetaStore.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;
    // Services-tier PyO3 entry points (install_audit_hook +
    // PyTaskEngine / PyTaskRecord / PyQueueStats task-queue pyclasses).
    // Registered after `kernel` so PyKernel is in the module's type
    // registry by the time `install_audit_hook` accepts a
    // `PyRef<PyKernel>` parameter.
    services::python::register(m)?;
    // Backends-tier PyO3 entry points (BlobPackEngine pyclass) **and**
    // the `ObjectStoreProvider` registration — `backends::python::
    // register` calls `kernel::hal::object_store_provider::set_provider(
    // Arc::new(DefaultObjectStoreProvider))` so `PyKernel.sys_setattr`
    // constructs concrete backends through the §3.B.2 trait without
    // the kernel reaching into the backends crate.
    backends::python::register(m)?;
    // Network surface tier: in-bound VFS gRPC server (PyVfsGrpcServerHandle
    // + start_vfs_grpc_server) plus out-bound clients (PyFederationClient
    // + `install_transport_wiring(kernel)` for peer-blob client wiring).
    // Python's NexusFS boot calls
    // `nexus_runtime.install_transport_wiring(kernel)` once after
    // federation env vars are read.
    transport::python::register(m)?;
    // ManagedAgentService boot install — Python wheel deployment
    // wires sudocode-runtime as the runtime body via the
    // `SpawnTask` DI trait. ManagedAgentService is the single
    // integration point; the SudoCodeSpawnAdapter above just
    // provides the concrete provider for the cdylib path.
    m.add_function(wrap_pyfunction!(nx_managed_agent_install, m)?)?;
    Ok(())
}
