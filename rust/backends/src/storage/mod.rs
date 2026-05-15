//! Storage tier — composed `ObjectStore` impls.
//!
//! Each module here is a complete `ObjectStore` implementation that
//! plugs into the kernel via the `ObjectStoreProvider`.  The split is
//! by addressing strategy + transport flavour:
//!
//! * `cas_local`        — CAS addressing + local fs transport
//! * `path_local`       — path addressing + local fs transport
//! * `local_connector`  — reference-mode local folder mount
//! * `remote`           — RPC proxy ObjectStore (`RemoteBackend`)
//! * `blob_pack/`       — `BlobPackEngine` (was `VolumeEngine`) +
//!   `BlobPackIndex` — append-only multi-blob
//!   format used by `cas_local` for higher
//!   write throughput.

// `blob_pack` is an append-only multi-blob format exposed to Python
// (`#[pyclass]` in `python::mod`).  Currently consumed only via the
// PyO3 wrapper; gated behind `python` so non-cdylib builds (cluster
// binary) skip the redb / pyo3 dependency surface it pulls in.
#[cfg(feature = "python")]
pub mod blob_pack;

#[cfg(feature = "driver-cas-local")]
pub mod cas_local;
#[cfg(feature = "driver-local-connector")]
pub mod local_connector;
#[cfg(feature = "driver-path-local")]
pub mod path_local;
#[cfg(feature = "driver-remote")]
pub mod remote;
