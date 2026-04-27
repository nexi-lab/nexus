//! `kernel::python` — kernel cdylib surface.
//!
//! Phase 0 lifted the body of `#[pymodule] fn nexus_kernel` out of
//! `kernel/src/lib.rs` into this module's [`register`] so the
//! dedicated `nexus-cdylib` crate owns the `#[pymodule]` envelope
//! and Cargo no longer sees a kernel cdylib.
//!
//! Each peer rlib in the workspace exposes its own `python::register`
//! mirroring this file (`lib::python::register`,
//! `backends::python::register` after Phase 2, etc.); the cdylib
//! calls all of them in sequence.
//!
//! Kept narrow: only kernel-owned `#[pyclass]` / `#[pyfunction]`
//! exports. Connector / backend / federation pyclasses migrate out
//! in Phases 2–4 — those lines vanish from this file as the
//! corresponding source files leave `kernel/src/`.
//!
//! NOTE: identifiers below are imported via `use crate::…` rather than
//! written as fully-qualified `crate::shm_pipe::…` paths. This is *not*
//! cosmetic — `scripts/codegen_kernel_abi.py`'s `add_class::<MOD::Name>`
//! regex captures exactly two `::`-separated segments, so a 3-segment
//! `crate::shm_pipe::Foo` would silently drop out of the generated stubs.

use crate::{generated_kernel_abi_pyo3, semaphore};
#[cfg(unix)]
use crate::{shm_pipe, shm_stream, stdio_stream};
use pyo3::prelude::*;

/// Register every kernel-owned PyO3 export into the parent module.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_kernel`.
///
/// Phase 4 (full): `PyVfsGrpcServerHandle` + `start_vfs_grpc_server` +
/// `PyFederationClient` moved out of this register fn into
/// `transport::python::register` because the kernel crate no longer
/// depends on `transport` (the dependency edge inverted: transport →
/// kernel only).  The cdylib calls both register fns in sequence.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // Phase 2: openai_inference moved to backends (registered through
    // backends::python::register).  CAS Volume Engine (now BlobPackEngine)
    // also moved to backends::storage::blob_pack.
    // VFSLockManager deleted — I/O lock is now internal to LockManager,
    // accessed through Kernel syscalls (sys_read/sys_write/sys_copy).
    // MemoryPipeBackend / MemoryStreamBackend are kernel-internal only
    // (no #[pyclass]). Python accesses IPC buffers through
    // kernel.create_pipe / create_stream.
    #[cfg(unix)]
    m.add_class::<shm_pipe::SharedMemoryPipeBackend>()?;
    #[cfg(unix)]
    m.add_class::<shm_stream::SharedMemoryStreamBackend>()?;
    // R20.18.6: `WalStreamBackend` pyclass removed. Users reach the
    // raft-backed stream through `sys_setattr(DT_STREAM, io_profile="wal")`;
    // `WalStreamCore` now impls `StreamBackend` and registers with
    // `stream_manager` alongside the other backends.
    // Subprocess-stdio accumulation stream (Unix raw-fd pump).
    #[cfg(unix)]
    m.add_class::<stdio_stream::StdioStreamBackend>()?;
    m.add_class::<semaphore::VFSSemaphore>()?;
    // Kernel (Issue #1868 — PyKernel wraps pure Rust Kernel).
    m.add_class::<generated_kernel_abi_pyo3::PyOperationContext>()?;
    m.add_class::<generated_kernel_abi_pyo3::PyKernel>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysReadResult>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysWriteResult>()?;
    Ok(())
}
