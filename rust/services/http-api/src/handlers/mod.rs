//! Route handler modules — one file per `/v2/*` route domain.
//!
//! Each submodule exports a `pub fn router() -> axum::Router<AppState>`
//! that `crate::router` merges into the aggregate.  A handler NEVER
//! wires state directly; it takes `axum::extract::State<AppState>`
//! and pulls whichever backend it needs off the state struct.  This
//! keeps `AppState` growth O(1) per new domain — a fresh domain
//! adds a field, no handler rewire.

pub mod auth;
pub mod documents;
#[cfg(feature = "rebac")]
pub mod rebac;
pub mod search;
/// `Hit` → `QueryHit` bridge — one place that keeps the algorithm
/// working type ([`nexus_search_common::Hit`]) and the JSON wire
/// shape ([`search::QueryHit`]) in lockstep.  Rebac-gated because the
/// only consumer is the federated dispatcher (which needs ReBAC to
/// discover the caller's readable zones); a slim non-rebac build
/// never reaches the bridge.
#[cfg(feature = "rebac")]
pub mod search_bridge;
pub mod status;
