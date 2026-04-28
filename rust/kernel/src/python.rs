//! `kernel::python` — kernel-owned PyO3 surface.
//!
//! [`register`] adds the kernel's `#[pyclass]` / `#[pyfunction]`
//! exports to the parent module.  `nexus-cdylib`'s `#[pymodule] fn
//! nexus_kernel` calls this alongside the peer-crate registers
//! (`lib::python::register`, `backends::python::register`,
//! `services::python::register`, `transport::python::register`,
//! `nexus_raft::pyo3_bindings::register_python_classes`).
//!
//! NOTE: identifiers below are imported via `use crate::…` rather
//! than written as fully-qualified `crate::shm_pipe::…` paths.
//! `scripts/codegen_kernel_abi.py`'s `add_class::<MOD::Name>` regex
//! captures exactly two `::`-separated segments, so a 3-segment
//! `crate::shm_pipe::Foo` silently drops out of the generated stubs.

use crate::{generated_kernel_abi_pyo3, raft_federation_provider, semaphore};
#[cfg(unix)]
use crate::{shm_pipe, shm_stream, stdio_stream};
use pyo3::prelude::*;

/// Register kernel-owned `#[pyclass]` / `#[pyfunction]` exports into
/// the parent module.  Called from `nexus-cdylib`'s
/// `#[pymodule] fn nexus_kernel`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // Shared-memory IPC primitives — Unix-only because the underlying
    // SHM impl uses POSIX `shm_open` / `mmap`.
    #[cfg(unix)]
    m.add_class::<shm_pipe::SharedMemoryPipeBackend>()?;
    #[cfg(unix)]
    m.add_class::<shm_stream::SharedMemoryStreamBackend>()?;
    #[cfg(unix)]
    m.add_class::<stdio_stream::StdioStreamBackend>()?;
    // Cross-process semaphore counter (lock-manager–backed).
    m.add_class::<semaphore::VFSSemaphore>()?;
    // PyKernel + supporting context / result types — the syscall
    // surface generated from `kernel.rs` by codegen_kernel_abi.py.
    m.add_class::<generated_kernel_abi_pyo3::PyOperationContext>()?;
    m.add_class::<generated_kernel_abi_pyo3::PyKernel>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysReadResult>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysWriteResult>()?;
    // Phase 5 anchor — install RaftFederationProvider into the kernel's
    // federation slot. Mirrors transport::install_transport_wiring.
    m.add_function(wrap_pyfunction!(install_federation_wiring, m)?)?;
    Ok(())
}

/// One-shot install: replace the kernel's `NoopFederationProvider`
/// with `RaftFederationProvider` so federation-aware syscalls
/// dispatch through the trait.  Idempotent — safe to call from
/// `nexus.__init__`'s boot path even after Python re-imports the
/// module.
#[pyfunction]
#[pyo3(name = "install_federation_wiring")]
fn install_federation_wiring(
    kernel: PyRef<'_, generated_kernel_abi_pyo3::PyKernel>,
) -> PyResult<()> {
    raft_federation_provider::install(kernel.kernel_ref());
    Ok(())
}
