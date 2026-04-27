//! `nexus-cdylib` — the single Python entry-point cdylib for Nexus.
//!
//! This crate is a *build artifact*, not an architectural tier
//! (Linux's `make bzImage` analogue — bundles the rlibs into one
//! loadable image). It owns the sole `#[pymodule] fn nexus_kernel`
//! across the workspace and pulls together the rlibs that compose
//! the runtime:
//!
//! * [`lib`]              — pure-Rust algorithms (libc analogue)
//! * [`kernel`]           — pillars + primitives + syscalls
//! * [`nexus_raft`]       — Raft / federation
//! * (Phase 2)             `backends`  — driver impls
//! * (Phase 3)             `services`  — audit / permission / agents
//! * (Phase 4)             `transport` — gRPC / RPC / IPC / federation client / blob fetch
//!
//! Each peer rlib exposes its own `python::register(&Bound<PyModule>)`
//! function; this cdylib is just the envelope that calls all of them.
//!
//! See `docs/architecture/KERNEL-ARCHITECTURE.md` §6.1 for the
//! cycle-break rationale (why kernel is rlib-only and the cdylib
//! lives in its own crate).

use pyo3::prelude::*;

#[pymodule]
fn nexus_kernel(m: &Bound<PyModule>) -> PyResult<()> {
    // §6 lib (libc analogue) — pure-Rust algorithm wrappers.
    lib::python::register(m)?;
    // §3 / §4 kernel — pillars + primitives + #[pyclass] surface.
    kernel::python::register(m)?;
    // Raft / federation — ZoneManager / ZoneHandle / MetaStore.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;
    // Phase 3: services-tier PyO3 entry points (install_audit_hook, …).
    // Registered after `kernel` so PyKernel is in the module's type
    // registry by the time `install_audit_hook` accepts a
    // `PyRef<PyKernel>` parameter.
    services::python::register(m)?;
    // Phase 4 transport-tier PyO3 entry points (PyVfsGrpcServerHandle +
    // start_vfs_grpc_server + PyFederationClient) registered through
    // `kernel::python::register` for now — see `kernel/src/transport/`
    // module's docstring for why those files live in kernel rather
    // than the transport crate.
    //
    // Phase 2: backends-tier PyO3 entry points (BlobPackEngine pyclass)
    // **and** the `BackendFactory` registration — `backends::python::
    // register` calls `kernel::hal::backend_factory::set_factory(
    // Arc::new(DefaultBackendFactory))` so `PyKernel.sys_setattr` can
    // construct concrete backends without the kernel ever knowing the
    // concrete types live in the backends crate.
    backends::python::register(m)?;
    Ok(())
}
