//! Nexus Raft: Consensus and Embedded Storage for Nexus
//!
//! This crate provides:
//!
//! 1. **Embedded Storage** ([`storage`]): General-purpose embedded KV database
//!    based on sled, reusable for caching, queues, and more.
//!
//! 2. **Raft Consensus** ([`raft`]): Distributed consensus using tikv/raft-rs
//!    for STRONG_HA zones.
//!
//! 3. **Witness Node** ([`raft::WitnessStateMachine`]): Lightweight vote-only node
//!    for cost-effective high availability.
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
//! │  │  raft module                                             │   │
//! │  │                                                         │   │
//! │  │  RaftNode ──────┬──► Leader Election                    │   │
//! │  │                 ├──► Log Replication                    │   │
//! │  │                 └──► State Machine                      │   │
//! │  └─────────────────────────────────────────────────────────┘   │
//! │                                                                 │
//! │  ┌─────────────────────────────────────────────────────────┐   │
//! │  │  transport module                                        │   │
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
//! ```rust,ignore
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
//! - [`raft`]: Raft consensus state machine and node
//!
//! # Feature Flags
//!
//! - `async`: Enable async/await support with Tokio runtime
//!
//! # Issue Reference
//!
//! Part of Issue #1159: P2P Federation and Consensus Zones

pub mod storage;

/// Raft consensus module for STRONG_HA zones.
///
/// Provides distributed consensus using tikv/raft-rs for linearizable
/// metadata and lock operations. Requires `consensus` feature for
/// full RaftNode support (leader election, log replication).
pub mod raft;

/// gRPC transport layer (requires `grpc` feature).
///
/// This module provides network transport for Raft messages using gRPC.
/// It is also reusable for webhook streaming and real-time events.
///
/// Enable with:
/// ```toml
/// [dependencies]
/// nexus_raft = { version = "0.1", features = ["grpc"] }
/// ```
#[cfg(feature = "grpc")]
pub mod transport;

/// Python bindings via PyO3 (requires `python` feature).
///
/// This module provides direct FFI access to the Raft state machine
/// for same-box deployments, bypassing gRPC for better performance (~5μs vs ~200μs).
///
/// Enable with:
/// ```toml
/// [dependencies]
/// nexus_raft = { version = "0.1", features = ["python"] }
/// ```
#[cfg(feature = "python")]
mod pyo3_bindings;
#[cfg(feature = "python")]
pub use pyo3_bindings::*;

// Stub module when grpc feature is disabled
#[cfg(not(feature = "grpc"))]
pub mod transport {
    //! gRPC transport (requires `grpc` feature).
    //!
    //! Enable the `grpc` feature to use this module:
    //!
    //! ```toml
    //! [dependencies]
    //! nexus_raft = { version = "0.1", features = ["grpc"] }
    //! ```

    /// Transport error types.
    #[derive(Debug, thiserror::Error)]
    pub enum TransportError {
        /// gRPC feature not enabled.
        #[error("gRPC feature not enabled. Add `features = [\"grpc\"]` to Cargo.toml")]
        FeatureNotEnabled,
    }
}

/// Re-export commonly used types for convenience.
pub mod prelude {
    pub use crate::storage::{SledBatch, SledStore, SledTree, StorageError, TreeBatch};

    pub use crate::raft::{
        Command, CommandResult, FullStateMachine, HolderInfo, LockInfo, LockState, RaftError,
        StateMachine, WitnessStateMachine,
    };

    #[cfg(feature = "consensus")]
    pub use crate::raft::{NodeRole, RaftConfig, RaftNode, RaftStorage};

    #[cfg(all(feature = "grpc", has_protos))]
    pub use crate::transport::{
        ClientConfig, NodeAddress, RaftClient, RaftClientPool, RaftServer, RaftServerState,
        ServerConfig, TransportError as GrpcError,
    };
}
