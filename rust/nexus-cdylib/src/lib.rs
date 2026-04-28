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
    // Also exposes `install_federation_wiring(kernel)` (Phase 5
    // anchor): swaps the kernel's NoopFederationProvider for the
    // real RaftFederationProvider so federation-aware syscalls
    // dispatch through the trait.
    kernel::python::register(m)?;
    // Raft / federation — ZoneManager / ZoneHandle / MetaStore.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;
    // Phase 3: services-tier PyO3 entry points (install_audit_hook, …).
    // Registered after `kernel` so PyKernel is in the module's type
    // registry by the time `install_audit_hook` accepts a
    // `PyRef<PyKernel>` parameter.
    services::python::register(m)?;
    // Phase 2: backends-tier PyO3 entry points (BlobPackEngine pyclass)
    // **and** the `BackendFactory` registration — `backends::python::
    // register` calls `kernel::hal::backend_factory::set_factory(
    // Arc::new(DefaultBackendFactory))` so `PyKernel.sys_setattr` can
    // construct concrete backends without the kernel ever knowing the
    // concrete types live in the backends crate.
    backends::python::register(m)?;
    // Phase 4 (full): transport-tier PyO3 surface (gRPC server +
    // federation client) AND the install function that wires the
    // kernel-side `peer_client` slot + `pending_blob_fetcher_slot`
    // to the real concrete impls in transport.  Python's NexusFS
    // boot calls `nexus_kernel.install_transport_wiring(kernel)`
    // exactly once after federation env vars are read.
    transport::python::register(m)?;
    Ok(())
}
