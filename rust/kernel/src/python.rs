//! `kernel::python` — kernel-owned PyO3 surface.
//!
//! [`register`] adds the kernel's `#[pyclass]` / `#[pyfunction]`
//! exports to the parent module.  `nexus-cdylib`'s `#[pymodule] fn
//! nexus_runtime` calls this alongside the peer-crate registers
//! (`lib::python::register`, `backends::python::register`,
//! `services::python::register`, `transport::python::register`,
//! `nexus_raft::pyo3_bindings::register_python_classes`).
//!
//! NOTE: identifiers below are imported via `use crate::…` rather
//! than written as fully-qualified `crate::shm_pipe::…` paths.
//! `scripts/codegen_kernel_abi.py`'s `add_class::<MOD::Name>` regex
//! captures exactly two `::`-separated segments, so a 3-segment
//! `crate::shm_pipe::Foo` silently drops out of the generated stubs.

use crate::{generated_kernel_abi_pyo3, semaphore};
use pyo3::prelude::*;

/// Register kernel-owned `#[pyclass]` / `#[pyfunction]` exports into
/// the parent module.  Called from `nexus-cdylib`'s
/// `#[pymodule] fn nexus_runtime`.
///
/// DT_PIPE / DT_STREAM SHM and stdio backends deliberately do NOT
/// appear here: they are kernel-internal primitives, only constructed
/// inside the kernel via `sys_setattr` and reached from Python through
/// the `sys_read` / `sys_write` syscalls.  Exposing them as pyclasses
/// would let callers attach to the raw mmap/fd surface and bypass the
/// kernel — a layering violation.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // Cross-process semaphore counter (lock-manager–backed).
    m.add_class::<semaphore::VFSSemaphore>()?;
    // PyKernel + supporting context / result types — the syscall
    // surface generated from `kernel.rs` by codegen_kernel_abi.py.
    m.add_class::<generated_kernel_abi_pyo3::PyOperationContext>()?;
    m.add_class::<generated_kernel_abi_pyo3::PyKernel>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysReadResult>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysWriteResult>()?;
    Ok(())
}
