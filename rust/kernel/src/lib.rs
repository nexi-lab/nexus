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

// ── §3 / §4 / HAL surface (Phase 1) ───────────────────────────────────
// Strict 3-way split inside the kernel crate:
//   * `crate::abc`  — §3 ABC pillars (ObjectStore / MetaStore / CacheStore).
//                     Three trait files, period.
//   * `crate::hal`  — kernel-defined extension interfaces that aren't
//                     §3 pillars (LlmStreamingBackend, PeerBlobClient).
//   * `crate::core` — §4 kernel primitives (vfs_router, dlc, dcache,
//                     locks, dispatch, plus in-memory reference impls of
//                     the §3 pillars).  No traits, no extension ifaces.
//
// Driver / service / transport impls move out into parallel crates in
// Phases 2–5.
pub mod abc;
pub mod core;
pub mod hal;

// Phase B holding pen for ObjectStore impls (CasLocalBackend,
// PathLocalBackend, LocalConnectorBackend) — Phase 2 lifts them into
// `backends/`.  `backend` is a flat re-export shim over the trait
// (in `crate::abc::object_store` after Phase 1) plus the impls so the
// 17 callers using `use crate::backend::{ObjectStore, ...}` keep
// working through the transition.
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
pub use core::meta_store;
pub use core::vfs_router;
// Kept under flat `semaphore::` so `m.add_class::<semaphore::VFSSemaphore>()`
// in #[pymodule] keeps the single-segment shape that
// scripts/codegen_kernel_abi.py's `add_class::<MOD::Name>` regex matches.
pub(crate) use core::lock::semaphore;
pub(crate) use core::meta_store::remote as remote_meta_store;
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
// `core::stream::stdio` only ships its pyclass on Unix (the
// `StdioStreamBackend` impl is `#[cfg(unix)]`); the cdylib's
// `m.add_class::<stdio_stream::StdioStreamBackend>` line below is
// likewise cfg-gated, so the shim must match. Without the shim the
// Linux build trips `unresolved module \`stdio_stream\`` even though
// the Windows build (where neither the shim nor the add_class line
// are emitted) compiles fine.
#[cfg(unix)]
pub(crate) use core::stream::stdio as stdio_stream;
pub(crate) use core::stream::wal as wal_stream;
// Note: core::lock::semaphore, core::pipe::remote, core::stream::observer,
// core::stream::remote — not re-exported under flat names; their
// pre-Phase-C flat aliases were dead. Reach them through
// `crate::core::*` directly going forward.

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
//   * Phase G — raft_meta_store, replication.
//   * Phase H — bitmap, bloom, hash (delete duplicates with lib),
//     glob, io, path_utils, prefix, rebac, search, simd, trigram.
//   * Phase E — agent_status_resolver (services/agents).

// Phase 3: `agent_status_resolver` moved to `services::agents::status_resolver`.
// Phase 3: `audit_hook` moved to `services::audit`.
// Phase 3: `permission_hook` moved to `services::permission::hook`.
// All three were `mod *_hook;` / `mod agent_status_resolver;` declarations
// here; their concrete impls now live in the services peer crate, and
// kernel reaches them only through the in-tree Rust API surface
// (`Kernel::register_native_hook`, `PathResolver` impls).  The cdylib
// composes both crates via `services::python::register(m)`.
#[cfg(feature = "connectors")]
mod anthropic_backend;
#[cfg(feature = "connectors")]
pub mod anthropic_streaming;
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
// `kernel` itself is `pub` (Phase 3 onward) so peer crates
// (`services::audit`, etc.) can hold `&kernel::Kernel` references and
// call the kernel's in-tree Rust API (`register_native_hook`,
// `prepare_audit_stream`, `sys_*` direct).  PyKernel surfaces those
// methods to Python through `generated_kernel_abi_pyo3`; peer crates
// bypass PyO3 and call the Rust methods directly.
pub mod kernel;
// `generated_kernel_abi_pyo3` (renamed from `generated_pyo3` in Phase C)
// kept public so other crates (e.g. `rust/raft`) can reference `PyKernel`
// via cross-crate PyO3 borrows — needed for
// `PyZoneHandle::attach_to_kernel_mount()` which wires a Raft-backed
// `MetaStore` into `Kernel::mount_metastores` without surfacing a
// separate `KernelMetaStore` Python class.
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
// `permission_hook` moved to `services::permission::hook` (Phase 3).
mod raft_meta_store;
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

// Phase 0 — `#[pymodule] fn nexus_kernel` lives in `rust/nexus-cdylib/`
// now (the dedicated cdylib build artifact). Kernel's pyclass /
// pyfunction surface is registered through `kernel::python::register`,
// called by the cdylib alongside `lib::python::register`,
// `nexus_raft::pyo3_bindings::register_python_classes`, and (post-
// Phase-2/3/4) the parallel-crate registers.
pub mod python;
