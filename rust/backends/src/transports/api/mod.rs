//! External API transports — connectors for third-party services.
//!
//! Phase 2 lifted Python `nexus.backends.connectors/` into Rust
//! here.  Per the architecture clarification, "connectors" are
//! transport-tier (different transport mechanism than blob storage),
//! not a separate architectural pillar.

pub mod ai;
pub mod cli;
pub mod google;
pub mod social;
