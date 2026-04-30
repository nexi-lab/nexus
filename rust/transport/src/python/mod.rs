//! `transport::python` — transport-tier PyO3 surface.
//!
//! Mirrors `kernel::python::register`, `services::python::register`,
//! `backends::python::register` — single entry point the
//! `nexus-cdylib` `#[pymodule] fn nexus_runtime` invokes.
//!
//! Front-door services tier: registers the VFS gRPC server pyclass +
//! starter pyfunction. Driver-outgoing RPC clients (peer-blob,
//! federation) live in `rpc::python::register` instead — including
//! the `install_transport_wiring` Python entry point that wires the
//! kernel's `peer_client` slot.

use pyo3::prelude::*;

use crate::grpc;

/// Register every transport-tier PyO3 export into the parent module.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_class::<grpc::PyVfsGrpcServerHandle>()?;
    m.add_function(wrap_pyfunction!(grpc::start_vfs_grpc_server, m)?)?;
    Ok(())
}
