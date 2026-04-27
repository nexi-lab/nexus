//! Storage tier — composed `ObjectStore` impls.
//!
//! Each module here is a complete `ObjectStore` implementation that
//! plugs into the kernel via the `BackendFactory`.  The split is
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

pub mod blob_pack;
pub mod cas_local;
pub mod local_connector;
pub mod path_local;
pub mod remote;
