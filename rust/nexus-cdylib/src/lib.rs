//! `nexus-cdylib` — the single Python entry-point cdylib for Nexus.
//!
//! This crate is a *build artifact*, not an architectural tier
//! (Linux's `make bzImage` analogue — bundles the rlibs into one
//! loadable image). It owns the sole `#[pymodule] fn nexus_runtime`
//! across the workspace and pulls together the rlibs that compose
//! the runtime:
//!
//! * [`lib`]              — pure-Rust algorithms (libc analogue)
//! * [`kernel`]           — pillars + primitives + syscalls
//! * [`nexus_raft`]       — Raft / federation
//! * (Phase 2)             `backends`  — driver impls
//! * (Phase 3)             `services`  — audit / permission / agents / tasks
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
    // Phase 3: services-tier PyO3 entry points (install_audit_hook,
    // PyTaskEngine / PyTaskRecord / PyQueueStats — task-queue pyclasses
    // folded in by Phase 3 restructure plan #6).  Registered after
    // `kernel` so PyKernel is in the module's type registry by the
    // time `install_audit_hook` accepts a `PyRef<PyKernel>` parameter.
    services::python::register(m)?;
    // Backends-tier PyO3 entry points (BlobPackEngine pyclass) **and**
    // the `ObjectStoreProvider` registration — `backends::python::
    // register` calls `kernel::hal::object_store_provider::set_provider(
    // Arc::new(DefaultObjectStoreProvider))` so `PyKernel.sys_setattr`
    // constructs concrete backends through the §3.B.2 trait without
    // the kernel reaching into the backends crate.
    backends::python::register(m)?;
    // Front-door services tier: VFS gRPC server pyclass + starter.
    transport::python::register(m)?;
    // Driver-outgoing RPC clients: PyFederationClient pyclass +
    // `install_transport_wiring(kernel)` Python entry point that
    // wires kernel's `peer_client` slot to the real
    // `rpc::peer_blob::PeerBlobClient` impl. Python's NexusFS boot
    // calls `nexus_runtime.install_transport_wiring(kernel)` once
    // after federation env vars are read.
    rpc::python::register(m)?;
    Ok(())
}
