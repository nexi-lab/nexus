//! Federation subsystem — optional DI for multi-node zone sharing.
//!
//! Sits above the raft ``ZoneManager`` as an orchestration layer:
//!
//! - [`distributed_locks::DistributedLocks`] — Raft-replicated
//!   distributed-lock backend installed through
//!   `DistributedCoordinator::locks_for_zone`.
//! - [`topology`] — env-var parsers for static Day-1 cluster topology
//!   (consumed by the cluster binary's bootstrap_static / apply_topology
//!   loop).
//!
//! TOFU trust store lives in `transport-primitives` (shared across
//! raft + rpc) — re-exported from the workspace crate, not here.

pub mod distributed_locks;
pub mod topology;

pub use distributed_locks::DistributedLocks;
pub use topology::{
    parse_federation_env, parse_mounts_env, parse_zones_env, ENV_FEDERATION_MOUNTS,
    ENV_FEDERATION_ZONES,
};

// TOFU trust store re-exports from the shared `transport-primitives`
// crate so existing `nexus_raft::federation::TofuTrustStore` callers
// keep working through the move.
#[cfg(feature = "grpc")]
pub use transport_primitives::{TofuError, TofuResult, TofuTrustStore, TrustedZone};
