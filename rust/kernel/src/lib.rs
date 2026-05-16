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
//   * `crate::abc`  — §3.A Storage HAL pillars (ObjectStore / MetaStore
//                     / CacheStore). Trait declarations only.
//   * `crate::hal`  — §3.B Control-Plane HAL DI surfaces
//                     (DistributedCoordinator, ObjectStoreProvider).
//   * `crate::core` — §4 kernel primitives (vfs_router, dlc, locks,
//                     dispatch, in-memory reference impls of the §3.A
//                     pillars).
pub mod abc;
pub mod cache;
pub mod core;
pub mod hal;

// §3.A.2 ObjectStore extension hook — connector-backend SSE streaming.
// Lives at crate root (sibling to abc/, hal/, core/) because it
// extends a §3.A storage pillar through ObjectStore::as_llm_streaming
// without declaring a §3.B Control-Plane HAL DI surface. Concrete
// protocol-specific impls (`OpenAIBackend`, `AnthropicBackend`) live
// in `backends/src/transports/api/ai/*`.
pub mod llm_streaming;

// ── Flat re-exports of `core::*` ─────────────────────────────────────
pub(crate) use core::dispatch;
pub(crate) use core::dlc;
pub(crate) use core::file_watch;
pub use core::lock as lock_manager;
pub use core::lock::locks;
pub use core::meta_store;
pub use core::vfs_router;
// The pure Rust API lives at `core::lock::semaphore::VFSSemaphore`.
pub(crate) use core::pipe;
pub(crate) use core::pipe::manager as pipe_manager;

// `acp` and `managed_agent` modules used to live here; both moved to
// the `services` crate (`rust/services/src/{acp,managed_agent}/`) so
// the kernel↔services dep direction stays one-way (services depends
// on kernel, never the reverse).

pub(crate) use core::fdt;
#[cfg(unix)]
pub(crate) use core::pipe::shm as shm_pipe;
#[cfg(unix)]
pub(crate) use core::pipe::stdio as stdio_pipe;
pub use core::service_registry;
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
// `prepare_audit_stream`, `sys_*`).
pub mod kernel;

// `KernelAbi` trait — generic-over-K syscall surface that every
// Rust service uses to reach the kernel. `impl KernelAbi for Kernel`
// is a pure forwarder; production binaries monomorphise `K = Kernel`
// at link time so service code paths inline back to direct inherent
// calls (no vtable, no perf cost vs holding `Arc<Kernel>` directly).
pub mod abi;

// kernel↔raft Cargo edge direction: `raft → kernel`. Raft state-machine
// impls (zone_meta_store) and the
// `RaftDistributedCoordinator` trait impl live in the raft crate.
// Kernel reaches them through the
// `kernel::hal::distributed_coordinator::DistributedCoordinator`
// trait dispatch installed by the binary boot path.

// Client-side RPC transport for `RemoteBackend` (the
// `backends::storage::remote::RemoteBackend` ObjectStore impl that
// proxies all syscalls over gRPC to a remote `nexusd`). The driver-
// layer `rpc` crate re-exports this module as `rpc::vfs` so peer
// crates name a single canonical path; the file lives here in the
// kernel because the kernel-internal `RemoteMetaStore` /
// `RemotePipeBackend` / `RemoteStreamBackend` wrappers also wrap
// `RpcTransport` directly.
pub mod rpc_transport;
