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

#[cfg(feature = "connectors")]
use crate::openai_inference;
use crate::{federation_client, generated_kernel_abi_pyo3, grpc_server, semaphore, volume_engine};
#[cfg(unix)]
use crate::{shm_pipe, shm_stream, stdio_stream};
use pyo3::prelude::*;

/// Register every kernel-owned PyO3 export into the parent module.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_kernel`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // OpenAI inference (§10 D3) — GIL-free HTTP calls. Stays kernel-side
    // through Phase 2 connector migration; later moves to backends.
    #[cfg(feature = "connectors")]
    {
        m.add_function(wrap_pyfunction!(
            openai_inference::openai_chat_completion,
            m
        )?)?;
        m.add_function(wrap_pyfunction!(
            openai_inference::openai_chat_completion_stream,
            m
        )?)?;
    }
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
    // CAS Volume Engine (Issue #3403). Phase 2 moves the Rust impl into
    // `backends::storage::blob_pack` and renames the type to
    // `BlobPackEngine` (kept `#[pyclass(name = "VolumeEngine")]` alias
    // for one release for Python compat).
    m.add_class::<volume_engine::VolumeEngine>()?;
    m.add_class::<grpc_server::PyVfsGrpcServerHandle>()?;
    m.add_function(pyo3::wrap_pyfunction!(
        grpc_server::start_vfs_grpc_server,
        m
    )?)?;
    // Kernel (Issue #1868 — PyKernel wraps pure Rust Kernel).
    m.add_class::<generated_kernel_abi_pyo3::PyOperationContext>()?;
    m.add_class::<generated_kernel_abi_pyo3::PyKernel>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysReadResult>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysWriteResult>()?;
    // Federation peer gRPC client (R16.5b). Phase 4 moves the Rust impl
    // into `transport::federation` and this `add_class` line follows.
    m.add_class::<federation_client::PyFederationClient>()?;
    Ok(())
}
