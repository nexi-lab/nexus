//! Storage module for Nexus Raft.
//!
//! This module provides embedded storage capabilities that can be reused
//! across Nexus for various purposes:
//!
//! - **Raft Log**: Persistent storage for Raft consensus log entries
//! - **Local Cache**: Fast local cache that survives restarts
//! - **Task Queues**: Persistent task/event queues
//! - **Session Storage**: Persistent session state
//!
//! # Storage Backends
//!
//! Currently, we provide:
//!
//! - [`SledStore`]: Pure Rust embedded key-value database (like SQLite for KV)
//!
//! # Example
//!
//! ```rust,ignore
//! use nexus_raft::storage::{SledStore, SledTree, SledBatch};
//!
//! // Open a database
//! let store = SledStore::open("/var/lib/nexus/data").unwrap();
//!
//! // Use named trees for different data
//! let raft_log = store.tree("raft_log").unwrap();
//! let cache = store.tree("cache").unwrap();
//!
//! // Basic operations
//! raft_log.set(b"entry:1", b"data").unwrap();
//! cache.set(b"item:1", b"cached_value").unwrap();
//! ```

mod sled_store;

pub use sled_store::{Result, SledBatch, SledStore, SledTree, StorageError, TreeBatch};
