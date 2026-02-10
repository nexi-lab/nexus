//! Storage module for Nexus Raft.
//!
//! Embedded key-value storage using **redb 2.x** (replaced sled 0.34).
//! See `docs/rfcs/adr-raft-sled-strategy.md` for migration rationale.
//!
//! # Usage
//!
//! - **Metadata cache**: Fast local reads (~5μs) for file metadata
//! - **Lock store**: Distributed lock state with TTL support
//! - **Raft log**: Persistent storage for consensus log entries
//!
//! # Storage Backend
//!
//! - [`SledStore`]: Pure Rust embedded KV database (redb under the hood).
//!   Type name kept for backward compatibility.
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

mod redb_store;

pub use redb_store::{Result, SledBatch, SledStore, SledTree, StorageError, TreeBatch};
