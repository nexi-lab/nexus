//! `transport::python` — transport-tier PyO3 surface.
//!
//! Mirrors `kernel::python::register`, `services::python::register`,
//! `backends::python::register` — single entry point the
//! `nexus-cdylib` `#[pymodule] fn nexus_runtime` invokes.
//!
//! Phase 4 (full): `PyVfsGrpcServerHandle` + `start_vfs_grpc_server` +
//! `PyFederationClient` registered here now that the crate split has
//! cleared the cycle.

use pyo3::prelude::*;

use kernel::generated_kernel_abi_pyo3::PyKernel;

// Bring the pyclass + pyfunction names into scope so the
// `add_class::<MOD::Name>` regex in `scripts/codegen_kernel_abi.py`
// (which captures exactly two `::`-separated segments) sees them as
// `grpc::Foo` / `federation::Bar` rather than the fully-qualified
// 3-segment `crate::grpc::Foo` form.
use crate::{federation, grpc};

/// Register every transport-tier PyO3 export into the parent module
/// **and** install the kernel's transport-tier wiring (peer-blob
/// client + blob fetcher) at module init.
///
/// The wiring is keyed off `Kernel`'s own pending slots (`peer_client`
/// is a `RwLock<Arc<dyn PeerBlobClient>>` defaulting to Noop;
/// `pending_blob_fetcher_slot` is a `Mutex<Option<...>>` populated
/// during `init_federation_from_env`).  Both are drained / replaced
/// here once the cdylib has linked kernel + transport together.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // PyO3 surface — same three exports as pre-Phase-4-full.
    m.add_class::<grpc::PyVfsGrpcServerHandle>()?;
    m.add_function(wrap_pyfunction!(grpc::start_vfs_grpc_server, m)?)?;
    m.add_class::<federation::PyFederationClient>()?;

    // Phase 4 (full) install hook — bridges kernel HAL traits to
    // their concrete impls in this crate.  Fired once per Python
    // process at module import.
    m.add_function(wrap_pyfunction!(install_transport_wiring, m)?)?;

    Ok(())
}

/// Python-facing one-shot install: replaces kernel's `NoopPeerBlobClient`
/// with the real `transport::blob::peer_client::PeerBlobClient`.
/// Idempotent — safe to call from `nexus.__init__`'s boot path even
/// after Python re-imports the module.
///
/// The server-side blob-fetcher handler (KernelBlobFetcher) is installed
/// separately by the raft crate's own boot hook
/// (`nexus_raft::blob_fetcher_handler::install`), reached from the
/// cdylib's `#[pymodule]` body — the transport crate stays raft-free.
#[pyfunction]
#[pyo3(name = "install_transport_wiring")]
fn install_transport_wiring(kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    crate::blob::peer_client::install(kernel.kernel_ref());
    Ok(())
}
