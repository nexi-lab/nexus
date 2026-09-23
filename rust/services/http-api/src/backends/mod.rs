//! Backend impls consumed by the federated dispatcher's
//! [`nexus_federated_search::LocalSearchBackend`] and
//! [`nexus_federated_search::RemoteSearchBackend`] traits.
//!
//! # What lives here
//!
//! * [`plugin_local`] — the daemon's OWN plugin: dispatches per-zone
//!   `search_zone` calls through the shared [`crate::SearchBackend`]
//!   tonic-channel cache to the local plugin's `SearchService.Query`
//!   RPC.
//! * [`tonic_remote`] — cross-daemon dial: implements the federated
//!   dispatcher's [`nexus_federated_search::RemoteSearchBackend`]
//!   trait, uses the shared [`nexus_search_common::transport::PeerChannelCache`]
//!   for one-Channel-per-peer caching, stamps a
//!   [`nexus_search_common::SearchDelegation`] onto tonic metadata so
//!   the receiving daemon's servicer can validate it.
//! * [`proto_bridge`] — one place for the `ProtoQueryResult → Hit`
//!   mapping.  Both `plugin_local` and `tonic_remote` receive the
//!   same [`crate::search_proto::QueryResponse`] shape from a
//!   `SearchService.Query`; converting attribution onto
//!   [`nexus_search_common::Hit::extras`] happens in ONE place so a
//!   new field lands as one branch here + one entry in
//!   [`crate::handlers::search_bridge::EXTRAS_KEYS`].

#[cfg(feature = "rebac")]
pub mod plugin_local;
#[cfg(feature = "rebac")]
pub mod proto_bridge;
/// Env-driven [`nexus_search_common::InMemoryZoneSearchRegistry`]
/// builder — `NEXUS_SEARCH_REMOTE_ZONE_TARGETS=zone1=url1,zone2=url2`
/// unlocks the cross-daemon path.  Empty env → single-daemon
/// (default).  See the module docstring for the wire format +
/// fail-loud rules.
#[cfg(feature = "rebac")]
pub mod registry_config;
#[cfg(feature = "rebac")]
pub mod tonic_remote;
