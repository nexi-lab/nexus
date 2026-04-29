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

use crate::{acp, generated_kernel_abi_pyo3, semaphore};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

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
    // ACP service wiring — hand-written hooks (boot install + Python
    // AgentRegistry bridge + on-terminate callbacks). Hosts
    // AgentKind::UNMANAGED agents (subprocess + ACP-over-stdio).
    m.add_function(wrap_pyfunction!(acp::pyo3::nx_acp_install, m)?)?;
    m.add_function(wrap_pyfunction!(acp::pyo3::nx_acp_set_agent_registry, m)?)?;
    m.add_function(wrap_pyfunction!(
        acp::pyo3::nx_acp_register_on_terminate,
        m
    )?)?;
    // Generic Rust-service dispatch — same lookup the tonic Call
    // handler uses, exposed for in-process Python callers so we don't
    // grow per-service shortcuts (acp / managed_agent / future
    // services) that would each need their own audit-bypass review.
    m.add_function(wrap_pyfunction!(nx_kernel_dispatch_rust_call, m)?)?;
    Ok(())
}

/// Generic in-process Rust-service dispatch entry point.
///
/// Mirrors the lookup the tonic `Call` handler runs internally
/// (`Kernel::dispatch_rust_call`). Returns:
/// * `Some(bytes)` — the service handled the call and returned a
///   JSON-encoded response.
/// * `None` — `service` does not resolve as a Rust-flavoured entry
///   in the kernel's `ServiceRegistry`. Python-side callers should
///   fall through to their existing `dispatch_method` path so the
///   195 `@rpc_expose` services keep working.
///
/// Errors map onto `PyRuntimeError` with the Display formatting of
/// `RustCallError` so callers can match on the message prefix
/// (`method not found`, `invalid argument:`, `internal:`).
///
/// Single primitive — no per-service `nx_<svc>_dispatch` wrappers,
/// so audit / permission hooks added to the dispatch path land in
/// one place.
#[pyfunction]
fn nx_kernel_dispatch_rust_call<'py>(
    py: Python<'py>,
    py_kernel: PyRef<'_, generated_kernel_abi_pyo3::PyKernel>,
    service: &str,
    method: &str,
    payload: &[u8],
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let kernel = std::sync::Arc::clone(&py_kernel.inner);
    // RustService::dispatch may run an async tokio block_on
    // internally; release the GIL so other Python tasks can run.
    let outcome = py.detach(|| kernel.dispatch_rust_call(service, method, payload));
    match outcome {
        None => Ok(None),
        Some(Ok(bytes)) => Ok(Some(PyBytes::new(py, &bytes))),
        Some(Err(e)) => Err(PyRuntimeError::new_err(e.to_string())),
    }
}
