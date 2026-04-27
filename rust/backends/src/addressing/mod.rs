//! Addressing strategies — how a backend names blobs.
//!
//! Two orthogonal axes per `backend-architecture.md`:
//!   * **Addressing**: CAS (content hash), PAS (path).
//!   * **Transport**: local fs, S3, GCS, HTTP API.
//!
//! Phase 2 only ships a CAS-side stub here because the actual CAS
//! engine (`kernel::cas_engine::CASEngine`) lives in the kernel
//! crate as a primitive (Linux-VFS-equivalent).  PAS is currently a
//! placeholder; per-backend path handling is folded into each
//! `transports::*` impl directly.

pub mod cas;
