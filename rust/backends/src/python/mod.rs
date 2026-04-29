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
//! 2. **`ObjectStoreProvider` registration** — installs
//!    [`factory::DefaultObjectStoreProvider`] into the kernel's
//!    `OnceLock<Arc<dyn ObjectStoreProvider>>` so `PyKernel::sys_setattr`
//!    constructs concrete backends through the §3.B.2 trait without
//!    kernel reaching into `backends`.

pub mod factory;

use pyo3::prelude::*;
use std::sync::Arc;

/// Register every backends-tier PyO3 export into the parent module
/// **and** install the global `ObjectStoreProvider` for `sys_setattr`.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_runtime` after
/// `kernel::python::register`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // ── #[pyclass] registrations ────────────────────────────────────
    // BlobPackEngine pyclass — anchored to Python name "VolumeEngine"
    // for ABI compat.
    m.add_class::<crate::storage::blob_pack::BlobPackEngine>()?;

    // OpenAI inference (§10 D3) — GIL-free HTTP calls, lives in
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

    // ── ObjectStoreProvider boot wiring ─────────────────────────────
    // `set_provider` returns Err(existing) when a provider is already
    // registered — Python may re-import the module within the same
    // process during reloads, so swallow the duplicate-set error.
    let _ = kernel::hal::object_store_provider::set_provider(Arc::new(
        factory::DefaultObjectStoreProvider,
    ));

    Ok(())
}
