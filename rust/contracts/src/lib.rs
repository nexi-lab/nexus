//! Shared contracts (traits + types) for Nexus Rust crates.
//! Aligned with Python ``src/nexus/contracts/``.
//!
//! Submodules mirror Python's file layout so a reader jumping between
//! the two trees sees the same names in the same places. Re-exports at
//! the crate root keep consumers' ``use contracts::X`` paths stable.

pub mod constants;
pub mod lock_state;
pub mod operation_context;
pub mod rust_service;
pub mod write_coalescing;

pub use constants::{
    env, is_system_path, BLAKE3_EMPTY, LOCKS_PATH_PREFIX, MAX_GRPC_MESSAGE_BYTES, ROOT_ZONE_ID,
    SHARE_REGISTRY_PREFIX, SYSTEM_PATH_PREFIX, VFS_ROOT,
};
pub use lock_state::{
    HolderInfo, LockAcquireResult, LockEntry, LockInfo, LockMode, LockState, Locks,
};
pub use operation_context::OperationContext;
pub use rust_service::{RustCallError, RustService};
pub use write_coalescing::{WriteCoalescingMode, WriteCoalescingPolicy};
