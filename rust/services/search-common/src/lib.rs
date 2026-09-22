//! Reusable search primitives that outlive any one HTTP or gRPC
//! surface: a lightweight [`Hit`] type, RRF fusion, the federated
//! response envelope + its degraded-signal helpers, and the shared
//! backend-timing key set.
//!
//! # Why a separate crate
//!
//! Both `nexus-http-api` (HTTP boundary) and the future federated-
//! dispatcher crate need identical fusion + envelope shapes.  Putting
//! them in either owner would drag one crate into the other's build
//! graph and force one to import the other's HTTP / gRPC types just
//! to reuse an algorithm.  A pure-algorithm sibling crate keeps both
//! callers thin.
//!
//! # Modules
//!
//! * [`results`] — [`Hit`] + [`BACKEND_LEG_TIMING_KEYS`] (the phase-
//!   timing keys every backend surfaces on its response envelope).
//! * [`fusion`] — [`rrf_multi_fusion`] (N-way Reciprocal Rank Fusion)
//!   and the shared [`FusionMethod`] / [`FusionConfig`] shape.
//! * [`federated`] — [`FederatedSearchResponse`], [`ZoneFailure`],
//!   [`is_all_peers_failed`]: the cross-zone response envelope and
//!   the degrade-guard predicate.
//! * [`delegation`] — [`SearchDelegation`] short-lived read-only
//!   credential the dispatcher hands to a remote zone so the remote
//!   can run a search on behalf of the original caller.
//! * [`registry`] — [`ZoneSearchRegistry`] trait + in-memory impl
//!   the dispatcher calls to find each zone's daemon target.
//!
//! # Two-shape layering: [`Hit`] vs `nexus_http_api::handlers::search::QueryHit`
//!
//! The HTTP crate carries its own typed hit — `QueryHit` — with 12
//! optional attribution fields (`title_score`, `tier_boost`,
//! `recency_boost`, per-arm scores, …).  It is the **wire contract**
//! a JSON caller deserialises into.
//!
//! [`Hit`] in this crate is the **algorithm working type** — narrow,
//! carries an opaque `extras` map so fusion / dedup / envelope
//! building do not have to know which attribution fields exist.
//!
//! Both must stay: collapsing them would either pull HTTP-wire
//! knowledge into this pure-algorithm crate (SRP violation) or push
//! the algorithm surface into the HTTP crate (blocks the future
//! federated-dispatcher from reusing it).  **PR 3** of the federated
//! axum port MUST land an `impl From<Hit> for QueryHit` in the HTTP
//! crate so both shapes remain but their conversion is a single named
//! function — one place to keep the two in lockstep.

pub mod delegation;
pub mod federated;
pub mod fusion;
pub mod registry;
pub mod results;
/// Shared tonic `Channel`-per-peer cache — the ONE dial+cache impl
/// every cross-daemon caller uses.  Feature-gated because tonic +
/// dashmap are network deps a pure-algorithm consumer (fusion, RRF)
/// has no reason to pull.  See the module docstring for the two
/// callers this exists to unify.
#[cfg(feature = "transport")]
pub mod transport;

// Re-exports so downstream callers can `use nexus_search_common::{...}`
// without knowing the module split — the split is an internal
// organisation, not a public API contract.
pub use delegation::{
    DelegationError, SearchDelegation, DEFAULT_TTL_SECONDS, DELEGATION_METADATA_KEY,
    SEARCH_DELEGATION_METHODS,
};
pub use federated::{is_all_peers_failed, FederatedSearchResponse, ZoneFailure};
pub use fusion::{rrf_multi_fusion, FusionConfig, FusionMethod, RRF_TOP1_BONUS, RRF_TOP3_BONUS};
pub use registry::{InMemoryZoneSearchRegistry, SearchDaemonTarget, ZoneSearchRegistry};
pub use results::{Hit, BACKEND_LEG_TIMING_KEYS};
