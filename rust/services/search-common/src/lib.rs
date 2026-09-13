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

pub mod federated;
pub mod fusion;
pub mod results;

// Re-exports so downstream callers can `use nexus_search_common::{...}`
// without knowing the module split — the split is an internal
// organisation, not a public API contract.
pub use federated::{is_all_peers_failed, FederatedSearchResponse, ZoneFailure};
pub use fusion::{rrf_multi_fusion, FusionConfig, FusionMethod, RRF_TOP1_BONUS, RRF_TOP3_BONUS};
pub use results::{Hit, BACKEND_LEG_TIMING_KEYS};
