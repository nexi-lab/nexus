//! Shared contracts (traits + types) for Nexus Rust crates.
//! Aligned with Python ``src/nexus/contracts/``.
//!
//! Submodules mirror Python's file layout so a reader jumping between
//! the two trees sees the same names in the same places. Re-exports at
//! the crate root keep consumers' ``use contracts::X`` paths stable.

pub mod constants;

pub use constants::{BLAKE3_EMPTY, MAX_GRPC_MESSAGE_BYTES, ROOT_ZONE_ID};
