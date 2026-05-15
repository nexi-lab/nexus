//! External API transports — connectors for third-party services.
//!
//! Per the architecture clarification, "connectors" are transport-tier
//! (different transport mechanism than blob storage), not a separate
//! architectural pillar.

#[cfg(any(feature = "driver-openai", feature = "driver-anthropic"))]
pub mod ai;
#[cfg(feature = "driver-cli")]
pub mod cli;
#[cfg(any(feature = "driver-gdrive", feature = "driver-gmail"))]
pub mod google;
#[cfg(any(feature = "driver-slack", feature = "driver-x", feature = "driver-hn"))]
pub mod social;
