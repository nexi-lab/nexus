//! Addressing strategies — how a backend names blobs.
//!
//! Two orthogonal axes per `backend-architecture.md`:
//!   * **Addressing**: CAS (content hash), PAS (path).
//!   * **Transport**: local fs, S3, GCS, HTTP API.
//!
//! The CAS side is a stub here because the actual CAS engine
//! (`kernel::cas_engine::CASEngine`) lives in the kernel crate as a
//! primitive (Linux-VFS-equivalent).
//!
//! `addressing::path::PathAddressingEngine` — the Rust mirror of Python
//! `path_addressing_engine.py` — sits on top of `ObjectStore` and adds
//! streaming / batch / path-keyed metadata operations that
//! path-addressed cloud backends share.

pub mod cas;
pub mod path;
