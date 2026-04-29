//! `backends` — ObjectStore driver impls (Phase 2 parallel-layers crate).
//!
//! Per `docs/architecture/KERNEL-ARCHITECTURE.md` §1 / §3 the driver
//! layer sits parallel to the kernel: it implements
//! `kernel::abc::object_store::ObjectStore` (and where applicable
//! `kernel::hal::llm_streaming::LlmStreamingBackend`) without
//! adding new kernel surface. Concrete backends compose an *addressing*
//! strategy (CAS, path) with a *transport* (local fs, S3, GCS, HTTP API).
//!
//! Module layout (mirrors Python `nexus.backends/`):
//!
//! ```text
//! backends/
//!   addressing/
//!     cas/                — placeholder (CAS primitive lives in kernel)
//!   transports/
//!     blob/               — Nexus-managed blob storage (gcs, s3)
//!     api/                — External API transport (formerly Python connectors/)
//!       ai/{anthropic,openai}/  — LLM connectors (SSE → DT_STREAM → CAS)
//!       google/{gdrive,gmail}/  — Google API connectors
//!       social/{slack,x,hn,nostr}/ — social/feed connectors
//!       cli.rs            — CLI command-output backend
//!   storage/              — Composed ObjectStore impls
//!     cas_local.rs        — CasLocalBackend (was _backend_impls)
//!     path_local.rs       — PathLocalBackend (was _backend_impls)
//!     local_connector.rs  — LocalConnectorBackend (was _backend_impls)
//!     remote.rs           — RemoteBackend (was kernel::remote_backend)
//!     blob_pack/          — BlobPackEngine + BlobPackIndex
//!                           (Volume rename from kernel::volume_*)
//!   python/               — `#[cfg(feature = "python")]` PyO3 sub-module
//!     factory.rs          — `DefaultObjectStoreProvider` impl (the
//!                           17-way backend-type dispatch that
//!                           `PyKernel.sys_setattr` used to do inline)
//! ```
//!
//! Direction: `backends -> kernel`. Backends impls `kernel::abc::*`
//! traits and consumes `Kernel`'s in-tree Rust API surface; kernel
//! reaches concrete backends through the §3.B.2
//! `kernel::hal::object_store_provider::ObjectStoreProvider` trait.
//! Kernel holds an `Arc<dyn ObjectStoreProvider>` set at cdylib boot,
//! and `sys_setattr`'s 17-way construction switch lives in
//! `backends::python::factory`.

pub mod addressing;
pub mod storage;
pub mod transports;

#[cfg(feature = "python")]
pub mod python;
