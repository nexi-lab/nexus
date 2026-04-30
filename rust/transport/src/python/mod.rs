//! `transport::python` — transport-tier PyO3 surface.
//!
//! Mirrors `kernel::python::register`, `services::python::register`,
//! `backends::python::register` — single entry point the
//! `nexus-cdylib` `#[pymodule] fn nexus_runtime` invokes.
//!
//! Registers:
//!
//! * `PyVfsGrpcServerHandle` + `start_vfs_grpc_server` — in-bound
//!   VFS gRPC server.
//! * `PyFederationClient` — out-bound federation peer client used by
//!   the Python federation_rpc shim.
//! * `install_transport_wiring(kernel)` — Python entry point that
//!   replaces the kernel's `NoopPeerBlobClient` with the real
//!   `transport::peer_blob::PeerBlobClient` impl.

use kernel::generated_kernel_abi_pyo3::PyKernel;
use pyo3::prelude::*;

use crate::{federation, grpc};

/// Register every transport-tier PyO3 export into the parent module.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_class::<grpc::PyVfsGrpcServerHandle>()?;
    m.add_function(wrap_pyfunction!(grpc::start_vfs_grpc_server, m)?)?;
    m.add_class::<federation::PyFederationClient>()?;
    m.add_function(wrap_pyfunction!(install_transport_wiring, m)?)?;
    Ok(())
}

/// Python-facing one-shot install: replaces kernel's
/// `NoopPeerBlobClient` with the real `transport::peer_blob::PeerBlobClient`.
/// Idempotent — safe to call from `nexus.__init__`'s boot path even
/// after Python re-imports the module.
#[pyfunction]
#[pyo3(name = "install_transport_wiring")]
fn install_transport_wiring(kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    crate::peer_blob::install(kernel.kernel_ref());
    Ok(())
}
