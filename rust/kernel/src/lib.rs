#![allow(clippy::useless_conversion)]

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Canonical root zone identifier — re-exported from the ``contracts``
/// crate (the Rust mirror of ``nexus.contracts.constants``) so kernel
/// users can reach it via ``nexus_kernel::ROOT_ZONE_ID`` without pulling
/// another workspace dep. Prefer this constant over hardcoded ``"root"``
/// literals.
pub use contracts::ROOT_ZONE_ID;

// ── §3 / §4 trait + primitive surface (Phase C) ───────────────────────
// Every kernel-internal pillar nested under `core/` so the kernel
// file set is self-evident. Driver / service / transport impls move
// out into parallel crates in Phases D–G.
pub mod core;

// Phase B holding pen for ObjectStore impls (CasLocalBackend,
// PathLocalBackend, LocalConnectorBackend) — Phase D lifts them into
// `backends/`. `backend` is a flat re-export shim over the trait
// (in `core::traits::object_store`) plus the impls so the 17 callers
// using `use crate::backend::{ObjectStore, ...}` keep working through
// the transition.
mod _backend_impls;
pub mod backend;

// ── Phase C compat shims ─────────────────────────────────────────────
// Pre-Phase-C kernel modules are re-exported from `core::*` under
// their flat names so callers do not churn. These shims retire as
// callers migrate to canonical `crate::core::*` paths in Phases D–G.
//
// Visibility mirrors the original (`pub mod` stays `pub use`,
// private `mod` stays `pub(crate) use`).
pub(crate) use core::dcache;
pub(crate) use core::dispatch;
pub(crate) use core::dispatch::hook_registry;
pub(crate) use core::dlc;
pub(crate) use core::file_watch;
pub use core::lock as lock_manager;
pub use core::lock::locks;
pub use core::metastore;
pub use core::vfs_router;
// Kept under flat `semaphore::` so `m.add_class::<semaphore::VFSSemaphore>()`
// in #[pymodule] keeps the single-segment shape that
// scripts/codegen_kernel_abi.py's `add_class::<MOD::Name>` regex matches.
pub(crate) use core::lock::semaphore;
pub(crate) use core::metastore::remote as remote_metastore;
pub(crate) use core::pipe;
pub(crate) use core::pipe::manager as pipe_manager;
#[cfg(unix)]
pub(crate) use core::pipe::shm as shm_pipe;
#[cfg(unix)]
pub(crate) use core::pipe::stdio as stdio_pipe;
pub(crate) use core::service_registry;
pub(crate) use core::stream;
pub(crate) use core::stream::manager as stream_manager;
#[cfg(unix)]
pub(crate) use core::stream::shm as shm_stream;
pub(crate) use core::stream::wal as wal_stream;
// Note: core::lock::semaphore, core::pipe::remote, core::stream::observer,
// core::stream::remote, core::stream::stdio — not re-exported under
// flat names; their pre-Phase-C flat aliases were dead. Reach them
// through `crate::core::*` directly going forward.

// ── Modules that have not moved yet ──────────────────────────────────
// These stay flat through Phase C; later phases relocate them:
//   * Phase D — `_backend_impls`, anthropic / openai / s3 / gcs /
//     gdrive / gmail / slack / hn / x / cli / nostr_relay /
//     remote_backend / volume_engine / volume_index, plus the four
//     CAS pillar files (cas_engine / cas_chunking / cas_remote /
//     cas_transport).
//   * Phase E — audit_hook, permission_hook.
//   * Phase F — federation_client, peer_blob_client, blob_fetcher,
//     grpc_server, rpc_transport, ipc.
//   * Phase G — raft_metastore, replication.
//   * Phase H — bitmap, bloom, hash (delete duplicates with lib),
//     glob, io, path_utils, prefix, rebac, search, simd, trigram.
//   * Phase E — agent_status_resolver (services/agents).

mod agent_status_resolver;
#[cfg(feature = "connectors")]
mod anthropic_backend;
#[cfg(feature = "connectors")]
pub mod anthropic_streaming;
pub mod audit_hook;
mod blob_fetcher;
mod cas_chunking;
mod cas_engine;
mod cas_remote;
mod cas_transport;
#[cfg(feature = "connectors")]
mod cli_backend;
mod federation_client;
#[cfg(feature = "connectors")]
mod gcs_backend;
#[cfg(feature = "connectors")]
mod gdrive_backend;
#[cfg(feature = "connectors")]
mod gmail_backend;
#[cfg(feature = "connectors")]
mod hn_backend;
pub mod ipc;
mod kernel;
// `generated_kernel_abi_pyo3` (renamed from `generated_pyo3` in Phase C)
// kept public so other crates (e.g. `rust/raft`) can reference `PyKernel`
// via cross-crate PyO3 borrows — needed for
// `PyZoneHandle::attach_to_kernel_mount()` which wires a Raft-backed
// `Metastore` into `Kernel::mount_metastores` without surfacing a
// separate `KernelMetastore` Python class.
pub mod generated_kernel_abi_pyo3;
// Compat alias so any out-of-tree consumer pinned to the pre-Phase-C
// path keeps working through one release. Removable once downstream
// confirmed migrated.
pub use generated_kernel_abi_pyo3 as generated_pyo3;
// Rust-native gRPC server for NexusVFSService — replaces the Python
// `grpc.aio.server` so :2028 is owned by tonic. Read/Write/Delete/Ping
// are zero-PyO3 fast-paths; Call still uses a PyO3 callback into the
// Python `dispatch_method` pending the broader 195-service migration.
pub mod grpc_server;
#[cfg(feature = "nostr")]
pub mod nostr_relay;
#[cfg(feature = "connectors")]
mod openai_backend;
#[cfg(feature = "connectors")]
mod openai_inference;
#[cfg(feature = "connectors")]
pub mod openai_streaming;
mod peer_blob_client;
mod permission_hook;
mod raft_metastore;
mod remote_backend;
mod replication;
mod rpc_transport;
#[cfg(feature = "connectors")]
mod s3_backend;
#[cfg(feature = "connectors")]
mod slack_backend;
mod volume_engine;
mod volume_index;
#[cfg(feature = "connectors")]
mod x_backend;

use pyo3::prelude::*;

/// Python module definition.
#[pymodule]
fn nexus_kernel(m: &Bound<PyModule>) -> PyResult<()> {
    // Phase H: pure-Rust algorithm wrappers (rebac, search, glob, io,
    // prefix, simd, trigram, path_utils) all live in `lib::python` now.
    // Single delegation call replaces ~30 individual `add_function` /
    // `add_class` lines that used to live here.
    lib::python::register(m)?;
    // OpenAI inference (§10 D3) — GIL-free HTTP calls. Stays kernel-side
    // through Phase D-deferred connector migration; Phase D follow-up PR
    // moves it to `backends`.
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
    // bitmap / bloom / hash PyO3 wrappers all live in lib::python now
    // (Phase I — moved alongside the rest of the algorithm wrappers).
    // VFSLockManager deleted — I/O lock is now internal to LockManager,
    // accessed through Kernel syscalls (sys_read/sys_write/sys_copy).
    // MemoryPipeBackend/MemoryStreamBackend are kernel-internal only (no #[pyclass]).
    // Python accesses IPC buffers through kernel.create_pipe/create_stream.
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
    // CAS Volume Engine (Issue #3403)
    m.add_class::<volume_engine::VolumeEngine>()?;
    m.add_class::<grpc_server::PyVfsGrpcServerHandle>()?;
    m.add_function(pyo3::wrap_pyfunction!(
        grpc_server::start_vfs_grpc_server,
        m
    )?)?;
    // Kernel (Issue #1868 — PyKernel wraps pure Rust Kernel)
    m.add_class::<generated_kernel_abi_pyo3::PyOperationContext>()?;
    m.add_class::<generated_kernel_abi_pyo3::PyKernel>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysReadResult>()?;
    m.add_class::<generated_kernel_abi_pyo3::PySysWriteResult>()?;
    // path_utils PyO3 functions registered via lib::python::register above.

    // Federation peer gRPC client (R16.5b).
    m.add_class::<federation_client::PyFederationClient>()?;

    // Register raft's PyO3 classes (ZoneManager, ZoneHandle, …) so
    // Python sees them under ``nexus_kernel`` alongside ``Kernel``.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;

    Ok(())
}
