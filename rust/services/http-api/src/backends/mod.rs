//! Backend impls consumed by the federated dispatcher's
//! [`nexus_federated_search::LocalSearchBackend`] trait.
//!
//! # What lives here
//!
//! * [`plugin_local`] — the daemon's OWN plugin: dispatches per-zone
//!   `search_zone` calls through the shared [`crate::SearchBackend`]
//!   tonic-channel cache to the local plugin's `SearchService.Query`
//!   RPC.
//!
//! # What deliberately does NOT live here
//!
//! * The cross-daemon tonic remote impl.  Cross-daemon search rides
//!   on a federation transport that does not exist yet (search-side
//!   inter-daemon dial is unimplemented on both the Python and Rust
//!   sides today).  When it lands, it slots in as a sibling module.

#[cfg(feature = "rebac")]
pub mod plugin_local;
