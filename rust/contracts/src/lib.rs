//! Shared contracts (traits + types) for Nexus Rust crates.
//! Aligned with Python ``src/nexus/contracts/``.
//!
//! Cross-tier constants live here so both ``nexus_kernel`` (kernel +
//! raft merged cdylib) and auxiliary crates share one source of truth,
//! mirroring the ``nexus.contracts.constants`` module on the Python
//! side. Add new primitives sparingly; the bar is "used by two or
//! more tiers / crates".

/// Canonical root zone identifier.
///
/// Every path routed by the kernel carries an implicit zone; the
/// default is this value. Mirrors
/// ``nexus.contracts.constants.ROOT_ZONE_ID``.
pub const ROOT_ZONE_ID: &str = "root";
