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
//! * `managed_agent_runtime` — cross-repo runtime-body adapter
//!   (today: wraps `sudocode_runtime::spawn_task`). Cluster binary
//!   depends on the same rlib for runtime-body parity.
//!
//! Each peer rlib exposes its own `python::register(&Bound<PyModule>)`
//! function; this cdylib is just the envelope that calls all of them.
//!
//! See `docs/architecture/KERNEL-ARCHITECTURE.md` §6.1 for the
//! cycle-break rationale (why kernel is rlib-only and the cdylib
//! lives in its own crate).

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use kernel::generated_kernel_abi_pyo3::PyKernel;

/// Install ManagedAgentService on `kernel` with the sudocode-runtime
/// spawn adapter wired in. Pure pass-through to
/// [`managed_agent_runtime::install_managed_agent_with_sudocode_spawn`];
/// the adapter struct + cross-repo git-dep on sudocode-runtime live
/// in that rlib so the cluster binary edge can wire the same
/// runtime body without duplicated adapter code.
///
/// Python signature:
///
/// ```python
/// nexus_runtime.nx_managed_agent_install(kernel)
/// ```
#[pyfunction]
#[pyo3(name = "nx_managed_agent_install")]
fn nx_managed_agent_install(py_kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    managed_agent_runtime::install_managed_agent_with_sudocode_spawn(&py_kernel.kernel_arc())
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
    // `SpawnTask` DI trait. Adapter struct + cross-repo git-dep on
    // sudocode-runtime live in `managed-agent-runtime` rlib so the
    // cluster binary can use the same wiring.
    m.add_function(wrap_pyfunction!(nx_managed_agent_install, m)?)?;
    Ok(())
}
