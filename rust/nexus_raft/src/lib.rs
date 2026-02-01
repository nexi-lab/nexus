//! Nexus Raft: Consensus and Embedded Storage for Nexus
//!
//! This crate provides:
//!
//! 1. **Embedded Storage** ([`storage`]): General-purpose embedded KV database
//!    based on sled, reusable for caching, queues, and more.
//!
//! 2. **Raft Consensus** (coming soon): Distributed consensus using tikv/raft-rs
//!    for STRONG_HA zones.
//!
//! 3. **Witness Node** (coming soon): Lightweight vote-only node for cost-effective
//!    high availability.
//!
//! # Architecture
//!
//! ```text
//! ┌─────────────────────────────────────────────────────────────────┐
//! │  nexus_raft crate                                               │
//! │                                                                 │
//! │  ┌─────────────────────────────────────────────────────────┐   │
//! │  │  storage module (general-purpose, reusable)             │   │
//! │  │                                                         │   │
//! │  │  SledStore ─────┬──► Raft Log Storage                   │   │
//! │  │                 ├──► Local Cache                        │   │
//! │  │                 ├──► Task Queues                        │   │
//! │  │                 └──► Session Storage                    │   │
//! │  └─────────────────────────────────────────────────────────┘   │
//! │                                                                 │
//! │  ┌─────────────────────────────────────────────────────────┐   │
//! │  │  raft module (coming in Commit 3)                       │   │
//! │  │                                                         │   │
//! │  │  RaftNode ──────┬──► Leader Election                    │   │
//! │  │                 ├──► Log Replication                    │   │
//! │  │                 └──► State Machine                      │   │
//! │  └─────────────────────────────────────────────────────────┘   │
//! │                                                                 │
//! │  ┌─────────────────────────────────────────────────────────┐   │
//! │  │  transport module (coming in Commit 2)                  │   │
//! │  │                                                         │   │
//! │  │  gRPC (tonic) ──┬──► Raft Messages                      │   │
//! │  │                 └──► Webhook Streaming (future)         │   │
//! │  └─────────────────────────────────────────────────────────┘   │
//! └─────────────────────────────────────────────────────────────────┘
//! ```
//!
//! # Quick Start
//!
//! ## Embedded Storage (Available Now)
//!
//! ```rust,no_run
//! use nexus_raft::storage::SledStore;
//!
//! // Open a persistent database
//! let store = SledStore::open("/var/lib/nexus/data").unwrap();
//!
//! // Use named trees for different data types
//! let cache = store.tree("cache").unwrap();
//! cache.set(b"key", b"value").unwrap();
//!
//! // Serialize complex data
//! use serde::{Serialize, Deserialize};
//!
//! #[derive(Serialize, Deserialize)]
//! struct MyData {
//!     id: u64,
//!     name: String,
//! }
//!
//! let data = MyData { id: 1, name: "test".into() };
//! cache.set_bincode(b"my_data", &data).unwrap();
//! ```
//!
//! # Modules
//!
//! - [`storage`]: Embedded key-value storage (sled-based)
//!
//! # Feature Flags
//!
//! - `async`: Enable async/await support with Tokio runtime
//!
//! # Issue Reference
//!
//! Part of Issue #1159: P2P Federation and Consensus Zones

pub mod storage;

// Future modules (placeholders for documentation)
// pub mod raft;      // Coming in Commit 3
// pub mod transport; // Coming in Commit 2

/// Re-export commonly used types for convenience.
pub mod prelude {
    pub use crate::storage::{SledBatch, SledStore, SledTree, StorageError};
}
