//! `backends::python` — backends-tier PyO3 surface.
//!
//! Mirrors `kernel::python::register`, `services::python::register`,
//! and `transport::python::register` — single entry point that the
//! `nexus-cdylib` `#[pymodule] fn nexus_runtime` invokes to register
//! every PyO3 class / function this crate owns.
//!
//! Two responsibilities:
//!
//! 1. **`#[pyclass]` registration** — currently `BlobPackEngine`
//!    (was `VolumeEngine` Rust-side, anchored in Python under
//!    `name = "VolumeEngine"`).
//! 2. **`BackendFactory` registration** — installs
//!    [`factory::DefaultBackendFactory`] into the kernel's
//!    `OnceLock<Arc<dyn BackendFactory>>` so `PyKernel::sys_setattr`
//!    can construct concrete backends on mount creation without
//!    kernel ever importing `backends`.

pub mod factory;

use pyo3::prelude::*;
use std::sync::Arc;

/// Register every backends-tier PyO3 export into the parent module
/// **and** install the global `BackendFactory` for `sys_setattr`.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_runtime` after
/// `kernel::python::register`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // ── #[pyclass] registrations ────────────────────────────────────
    // Phase 2 / Phase 0.5: BlobPackEngine pyclass — anchored to
    // Python name "VolumeEngine" for ABI compat.
    m.add_class::<crate::storage::blob_pack::BlobPackEngine>()?;

    // OpenAI inference (§10 D3) — GIL-free HTTP calls, was registered
    // in `kernel::python::register` pre-Phase-2.  Now lives in
    // `backends::transports::api::ai::openai::inference`.
    #[cfg(feature = "connectors")]
    {
        use pyo3::wrap_pyfunction;
        m.add_function(wrap_pyfunction!(
            crate::transports::api::ai::openai::inference::openai_chat_completion,
            m
        )?)?;
        m.add_function(wrap_pyfunction!(
            crate::transports::api::ai::openai::inference::openai_chat_completion_stream,
            m
        )?)?;
    }

    // ── BackendFactory boot wiring ──────────────────────────────────
    // `set_factory` returns Err(existing) when a factory is already
    // registered — Python may re-import the module within the same
    // process during reloads, so swallow the duplicate-set error.
    let _ = kernel::hal::backend_factory::set_factory(Arc::new(factory::DefaultBackendFactory));

    Ok(())
}
