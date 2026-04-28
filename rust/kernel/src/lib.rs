#![allow(clippy::useless_conversion)]

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Canonical root zone identifier — re-exported from the ``contracts``
/// crate (the Rust mirror of ``nexus.contracts.constants``) so kernel
/// users can reach it via ``nexus_runtime::ROOT_ZONE_ID`` without pulling
/// another workspace dep. Prefer this constant over hardcoded ``"root"``
/// literals.
pub use contracts::ROOT_ZONE_ID;

// ── §3 / §4 / HAL surface ────────────────────────────────────────────
// Three-way split inside the kernel crate (see
// `docs/architecture/KERNEL-ARCHITECTURE.md` §3 / §4 / §6.1):
//   * `crate::abc`  — §3 ABC pillars (ObjectStore / MetaStore /
//                     CacheStore). Trait declarations only.
//   * `crate::hal`  — kernel-defined extension interfaces alongside
//                     the §3 pillars (LlmStreamingBackend,
//                     PeerBlobClient, BackendFactory).
//   * `crate::core` — §4 kernel primitives (vfs_router, dlc, dcache,
//                     locks, dispatch, in-memory reference impls of
//                     the §3 pillars).
pub mod abc;
pub mod core;
pub mod hal;

// ── Flat re-exports of `core::*` ─────────────────────────────────────
// `pyclass` registrations in `python.rs` use `m.add_class::<MOD::Name>()`
// where the codegen `add_class::<MOD::Name>` regex captures exactly two
// `::`-separated segments, so each pyclass-bearing submodule is re-
// exported under a single-segment name here.  Visibility tracks the
// original module (`pub mod` stays `pub use`, private `mod` stays
// `pub(crate) use`).
pub(crate) use core::dcache;
pub(crate) use core::dispatch;
pub(crate) use core::dispatch::hook_registry;
pub(crate) use core::dlc;
pub(crate) use core::file_watch;
pub use core::lock as lock_manager;
pub use core::lock::locks;
pub use core::meta_store;
pub use core::vfs_router;
// Kept under flat `semaphore::` so `m.add_class::<semaphore::VFSSemaphore>()`
// in #[pymodule] keeps the single-segment shape that
// scripts/codegen_kernel_abi.py's `add_class::<MOD::Name>` regex matches.
pub(crate) use core::lock::semaphore;
pub(crate) use core::pipe;
pub(crate) use core::pipe::manager as pipe_manager;
#[cfg(unix)]
pub(crate) use core::pipe::shm as shm_pipe;
#[cfg(unix)]
pub(crate) use core::pipe::stdio as stdio_pipe;
pub(crate) use core::service_registry;
pub use core::stream;
pub use core::stream::manager as stream_manager;
#[cfg(unix)]
pub(crate) use core::stream::shm as shm_stream;

// ── Kernel-owned primitives ──────────────────────────────────────────
// CAS (content-addressed storage) — the kernel's storage primitive
// (Linux-VFS analogue).  `pub` so `backends::storage::cas_local` can
// wrap a `CASEngine` inside its `ObjectStore` impl; see
// `docs/architecture/KERNEL-ARCHITECTURE.md` §4 for the rationale.
pub mod cas_chunking;
pub mod cas_engine;
pub mod cas_remote;
pub mod cas_transport;

// Kernel struct + syscalls.  `pub` so peer crates (`services`,
// `transport`, `backends`) hold `&kernel::Kernel` and call the
// in-tree Rust API directly (`register_native_hook`,
// `prepare_audit_stream`, `sys_*`).  PyKernel mirrors the surface
// to Python through `generated_kernel_abi_pyo3`.
pub mod kernel;

// PyO3 surface generated from `kernel.rs` syscalls by
// `scripts/codegen_kernel_abi.py`.  Other rlibs (`raft`,
// `transport`) reference `PyKernel` here for cross-crate PyO3
// borrows used by install-hook pyfunctions.
pub mod generated_kernel_abi_pyo3;
pub use generated_kernel_abi_pyo3 as generated_pyo3;

// Phase H of the rust-workspace restructure inverted the kernel↔raft
// Cargo edge.  Raft state-machine impls (zone_meta_store,
// replication_scanner, wal_stream_backend) and the
// `RaftFederationProvider` trait impl live in the raft crate now.
// Kernel reaches them through the
// `kernel::hal::federation::FederationProvider` trait dispatch
// installed by the cdylib boot path.

// Client-side RPC transport for `RemoteBackend` (the
// `backends::storage::remote::RemoteBackend` ObjectStore impl that
// proxies all syscalls over gRPC to a remote `nexusd`).  `pub` so
// the `BackendFactory` impl in `backends/` can construct
// `RpcTransport` for the `"remote"` backend type.
pub mod rpc_transport;

// Phase 0 — `#[pymodule] fn nexus_runtime` lives in `rust/nexus-cdylib/`
// now (the dedicated cdylib build artifact). Kernel's pyclass /
// pyfunction surface is registered through `kernel::python::register`,
// called by the cdylib alongside `lib::python::register`,
// `nexus_raft::pyo3_bindings::register_python_classes`, and (post-
// Phase-2/3/4) the parallel-crate registers.
pub mod python;
