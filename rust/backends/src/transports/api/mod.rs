//! External API transports — connectors for third-party services.
//!
//! Per the architecture clarification, "connectors" are transport-tier
//! (different transport mechanism than blob storage), not a separate
//! architectural pillar.

pub mod ai;
pub mod cli;
pub mod google;
pub mod social;
