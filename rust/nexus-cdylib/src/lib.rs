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
    // Raft / federation — ZoneManager / ZoneHandle / Metastore.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;
    Ok(())
}
