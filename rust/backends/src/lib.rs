//! `backends` — ObjectStore driver impls (Phase D parallel-layers crate).
//!
//! Per `docs/architecture/KERNEL-ARCHITECTURE.md` §1 / §3 the driver
//! layer sits parallel to the kernel: it implements
//! `kernel::abc::object_store::ObjectStore` (and where applicable
//! `kernel::hal::llm_streaming::LlmStreamingBackend`) without
//! adding new kernel surface. Concrete backends compose an *addressing*
//! strategy (CAS, path) with a *transport* (local fs, S3, GCS, HTTP API).
//!
//! Module layout:
//!
//! ```text
//! backends/
//!   addressing/cas/   — CAS engine, chunking, remote fetcher, local transport
//!   transports/
//!     blob_pack_local/  — Volume engine renamed to BlobPack (Phase L peer)
//!     ai/{anthropic,openai}  — LLM connectors (SSE → DT_STREAM → CAS)
//!     google/{gdrive,gmail}  — Google API connectors
//!     social/{slack,x,hn,nostr} — social/feed connectors
//!     {s3,gcs,cli,remote}.rs  — single-file connectors
//!   python/           — `#[cfg(feature = "python")]` PyO3 sub-module
//! ```
//!
//! The `python` feature aggregates every PyO3 wrapper into a single
//! `register(m)` function the kernel cdylib calls from its
//! `#[pymodule]` so `import nexus_kernel` exposes the same surface as
//! before the split.

#[cfg(feature = "python")]
pub mod python;
