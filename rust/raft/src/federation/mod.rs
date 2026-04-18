//! Federation subsystem — optional DI for multi-node zone sharing.
//!
//! Sits above the raft ``ZoneManager`` as an orchestration layer:
//!
//! - [`tofu::TofuTrustStore`] — SSH-style TOFU trust store for peer
//!   zone CA fingerprints.
//! - (R16.5b — pending) gRPC client helpers for peer discovery
//!   (VFS sys_stat) and membership requests (ZoneApiService.JoinZone).
//! - (R16.5c — pending) ``NexusFederation`` orchestrator composing the
//!   above with the ``ZoneManager`` for share() / join() flows.

#[cfg(feature = "grpc")]
pub mod tofu;

#[cfg(feature = "grpc")]
pub use tofu::{TofuError, TofuResult, TofuTrustStore, TrustedZone};
