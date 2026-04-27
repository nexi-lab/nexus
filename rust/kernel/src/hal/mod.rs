//! Kernel HAL — kernel-defined extension interfaces.
//!
//! Linux analogue: `security_operations` (LSM hooks) and similar
//! extension surfaces. These traits are NOT §3 ABC pillars (those live
//! in `crate::abc::*`); they're additional contracts the kernel
//! exposes for parallel-crate impls to plug into.
//!
//! Current members:
//!
//! * [`backend_factory`] — Phase 2 cycle break.  `BackendFactory`
//!   trait + `BackendArgs` struct + `OnceLock` slot.  Concrete impl
//!   lives in `backends::python::factory`; cdylib boot installs it
//!   before any `sys_setattr` call fires.  Required because the 17
//!   connector backends (anthropic / openai / s3 / gcs / …) live in
//!   the `backends` crate after Phase 2; kernel can't `use
//!   backends::*`.
//! * [`llm_streaming`] — extension over `ObjectStore` for connector
//!   backends that want a chunked LLM response stream materialised
//!   into the CAS pillar (the AI connector path).
//! * [`peer`] — abstract peer-blob fetch trait. Kernel code holds an
//!   `Arc<dyn PeerBlobClient>` so the concrete `transport::blob::
//!   peer_client::PeerBlobClient` impl can move into the `transport`
//!   crate (Phase 4) without dragging the kernel ↔ transport edge
//!   across the workspace twice.
//!
//! ## What's intentionally **not** here
//!
//! The CAS primitives — `cas_engine`, `cas_chunking`, `cas_remote`
//! (incl. `RemoteChunkFetcher` + `GrpcChunkFetcher`), `cas_transport`
//! (`LocalCASTransport`) — stay in the kernel crate.  Linux precedent:
//! the kernel-VFS-equivalent storage primitive (CAS engine for our
//! content-addressed pillar) belongs in the kernel; backends consume
//! it through `Arc<CASEngine>` to compose `ObjectStore` impls
//! (`CasLocalBackend` etc.).  Moving the CAS primitives out would
//! require either a runtime-dispatched `CasOps` trait (perf hit on
//! the hot CAS read path) or an ABI-breaking move of the entire
//! `PyKernel::cas_*` family — neither pays its way given the CAS
//! engine is conceptually a kernel primitive.
//!
//! Phase 1 introduced this directory alongside `abc/`. The two are
//! intentionally separate: `abc/` is the §3 invariant set (3 pillars,
//! period), `hal/` is the open-ended extension namespace.

pub mod backend_factory;
pub mod llm_streaming;
pub mod peer;
