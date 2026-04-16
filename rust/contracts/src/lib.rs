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

/// BLAKE3 hash of the empty byte string — used as the canonical ETag
/// for zero-content inodes (DT_DIR, empty files). Mirrors the Python
/// ``nexus.core.hash_utils.BLAKE3_EMPTY`` constant.
pub const BLAKE3_EMPTY: &str = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262";
