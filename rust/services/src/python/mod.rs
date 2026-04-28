//! `services::python` — services-tier PyO3 surface.
//!
//! Single `register(m)` entry point that the `nexus-cdylib` crate's
//! `#[pymodule] fn nexus_kernel` invokes alongside `lib::python::register`,
//! `kernel::python::register`, etc.  Same compositional pattern as every
//! other peer crate's PyO3 boundary.
//!
//! ## Currently exposed
//!
//! * `install_audit_hook(kernel, zone_id, stream_path)` — service-tier
//!   DI entry point that builds + registers a `services::audit::AuditHook`
//!   on the given Kernel.  Replaces the old `kernel.start_audit_hook()`
//!   PyO3 method on PyKernel — Phase 3 moved hook construction out of
//!   the kernel cdylib boundary into the owning service tier.

use kernel::generated_kernel_abi_pyo3::PyKernel;
use pyo3::prelude::*;

use crate::audit;

/// Install an `AuditHook` on `kernel` for `zone_id`, backed by a
/// WAL-replicated DT_STREAM at `stream_path`.
///
/// Replaces the pre-Phase-3 `kernel.start_audit_hook(zone_id, stream_path)`
/// PyO3 method on PyKernel.  Service-tier owns hook lifecycle now —
/// kernel only exposes `prepare_audit_stream` (stream lifecycle) +
/// `register_native_hook` (LSM-style in-tree API), and this function
/// composes the two with the local `AuditHook::new`.
///
/// Python signature:
///
/// ```python
/// nexus_kernel.install_audit_hook(kernel, zone_id="root", stream_path="/audit/traces/")
/// ```
#[pyfunction]
#[pyo3(name = "install_audit_hook")]
fn install_audit_hook_py(
    kernel: PyRef<'_, PyKernel>,
    zone_id: &str,
    stream_path: &str,
) -> PyResult<()> {
    audit::install(kernel.kernel_ref(), zone_id, stream_path)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))
}

/// Register every services-tier PyO3 export into the parent module.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_kernel`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(install_audit_hook_py, m)?)?;
    // Phase 3 restructure plan #6: tasks pyclasses (PyTaskEngine /
    // PyTaskRecord / PyQueueStats) folded into nexus_kernel cdylib —
    // standalone _nexus_tasks.so retired.
    crate::tasks::register_python(m)?;
    Ok(())
}
