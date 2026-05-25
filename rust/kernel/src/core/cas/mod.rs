//! Content-addressable storage primitive (§4 kernel primitive).
//!
//! CAS is the kernel's content-addressed storage subsystem — Rust-native
//! BLAKE3 hashing, CDC chunking, and local blob I/O. Composed by
//! `CasLocalBackend` in the `backends` crate (which wraps a
//! `CASEngine` inside its `ObjectStore` impl) and reached via the
//! `Kernel::cas_*` syscall family.
//!
//! Module layout:
//!
//! * [`engine`]    — `CASEngine`: hash + dedup + read/write driver.
//! * [`chunking`]  — CDC chunking + chunked-write / chunked-read.
//! * [`remote`]    — `RemoteChunkFetcher`: scatter-gather across peer nodes.
//! * [`transport`] — `LocalCASTransport`: on-disk blob fetch/store/exists.

pub mod chunking;
pub mod engine;
pub mod remote;
pub mod transport;
