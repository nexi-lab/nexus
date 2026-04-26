//! Federation subsystem — optional DI for multi-node zone sharing.
//!
//! Sits above the raft ``ZoneManager`` as an orchestration layer:
//!
//! - [`tofu::TofuTrustStore`] — SSH-style TOFU trust store for peer
//!   zone CA fingerprints.
//! - [`topology`] — env-var parsers for static Day-1 cluster topology
//!   (consumed by the cluster binary's bootstrap_static / apply_topology
//!   loop).
//! - (R16.5b — pending) gRPC client helpers for peer discovery
//!   (VFS sys_stat) and membership requests (ZoneApiService.JoinZone).
//! - (R16.5c — pending) ``NexusFederation`` orchestrator composing the
//!   above with the ``ZoneManager`` for share() / join() flows.

pub mod distributed_locks;
pub mod topology;

#[cfg(feature = "grpc")]
pub mod tofu;

pub use distributed_locks::DistributedLocks;
pub use topology::{
    parse_federation_env, parse_mounts_env, parse_zones_env, ENV_FEDERATION_MOUNTS,
    ENV_FEDERATION_ZONES,
};

#[cfg(feature = "grpc")]
pub use tofu::{TofuError, TofuResult, TofuTrustStore, TrustedZone};
