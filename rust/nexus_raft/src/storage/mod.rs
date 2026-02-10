//! Storage module for Nexus Raft.
//!
//! Embedded key-value storage with pluggable backends:
//! - **sled 0.34** (default): Battle-tested in this codebase
//! - **redb 2.x** (opt-in via `storage-redb` feature): 1.0 stable since June 2023
//!
//! Both backends export identical types (`SledStore`, `SledTree`, etc.)
//! for seamless switching. See `docs/rfcs/adr-raft-sled-strategy.md`.
//!
//! # Usage
//!
//! - **Metadata cache**: Fast local reads (~5μs) for file metadata
//! - **Lock store**: Distributed lock state with TTL support
//! - **Raft log**: Persistent storage for consensus log entries
//!
//! # Storage Backend
//!
//! - [`SledStore`]: Pure Rust embedded KV database.
//!   Default uses sled; enable `storage-redb` feature for redb.
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::storage::{SledStore, SledTree, SledBatch};
//!
//! let store = SledStore::open("/var/lib/nexus/data").unwrap();
//! let cache = store.tree("cache").unwrap();
//! cache.set(b"key", b"value").unwrap();
//! ```

#[cfg(feature = "storage-redb")]
mod redb_store;
#[cfg(feature = "storage-redb")]
pub use redb_store::{Result, SledBatch, SledStore, SledTree, StorageError, TreeBatch};

#[cfg(not(feature = "storage-redb"))]
mod sled_store;
#[cfg(not(feature = "storage-redb"))]
pub use sled_store::{Result, SledBatch, SledStore, SledTree, StorageError, TreeBatch};
