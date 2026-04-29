//! `rpc::python` — driver-outgoing RPC clients PyO3 surface.
//!
//! Single entry point that the `nexus-cdylib` `#[pymodule] fn nexus_runtime`
//! invokes to register every PyO3 class / function this crate owns:
//!
//! * `PyFederationClient` — federation peer client used by the Python
//!   federation_rpc shim for discover / join flows.
//! * `install_transport_wiring(kernel)` — Python entry point that
//!   replaces kernel's `NoopPeerBlobClient` with the real rpc-side
//!   `PeerBlobClient` impl. Existing Python callers keep the same
//!   import path (`nexus_runtime.install_transport_wiring`).

use kernel::generated_kernel_abi_pyo3::PyKernel;
use pyo3::prelude::*;

/// Register every rpc-tier PyO3 export into the parent module.
///
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_runtime` after
/// `kernel::python::register`. Idempotent on duplicate-imports.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_class::<crate::federation::PyFederationClient>()?;
    m.add_function(wrap_pyfunction!(install_transport_wiring, m)?)?;
    Ok(())
}

/// Python-facing one-shot install: replaces kernel's
/// `NoopPeerBlobClient` with the real `rpc::peer_blob::PeerBlobClient`.
/// Idempotent — safe to call from `nexus.__init__`'s boot path even
/// after Python re-imports the module.
#[pyfunction]
#[pyo3(name = "install_transport_wiring")]
fn install_transport_wiring(kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    crate::peer_blob::install(kernel.kernel_ref());
    Ok(())
}
