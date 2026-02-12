//! Nexus Raft: Embedded Storage and Consensus for Nexus
//!
//! This crate provides:
//!
//! 1. **Embedded Storage** ([`storage`]): General-purpose embedded KV database
//!    using redb (stable, pure Rust). Reusable for caching, queues, and more.
//!
//! 2. **State Machine** ([`raft`]): Metadata and lock operations with
//!    snapshot/restore support.
//!
//! 3. **Raft Consensus** (behind `consensus` flag): Distributed consensus using
//!    tikv/raft-rs. EXPERIMENTAL — not used in production.
//!
//! 4. **gRPC Transport** (behind `grpc` flag): Network transport for Raft
//!    messages. EXPERIMENTAL — not used in production.
//!
//! # Feature Flags
//!
//! | Flag | Default | Description |
//! |------|---------|-------------|
//! | `python` | off | PyO3 bindings for ~5μs metadata/lock ops (production path) |
//! | `consensus` | off | Raft consensus via tikv/raft-rs (experimental) |
//! | `grpc` | off | gRPC transport for Raft messages (experimental) |
//! | `async` | off | Tokio async runtime |
//! | `full` | off | All features |
//!
//! # Quick Start
//!
//! ## Embedded Storage
//!
//! ```rust,ignore
//! use nexus_raft::storage::RedbStore;
//!
//! let store = RedbStore::open("/var/lib/nexus/data").unwrap();
//! let cache = store.tree("cache").unwrap();
//! cache.set(b"key", b"value").unwrap();
//! ```
//!
//! # Storage Backend
//!
//! Uses **redb 2.x** — stable, pure Rust embedded KV database.
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
    pub use crate::storage::{RedbBatch, RedbStore, RedbTree, RedbTreeBatch, StorageError};

    pub use crate::raft::{
        Command, CommandResult, FullStateMachine, HolderInfo, LockInfo, LockState, RaftError,
        StateMachine, WitnessStateMachine,
    };

    #[cfg(feature = "consensus")]
    pub use crate::raft::{NodeRole, RaftConfig, RaftNode, RaftStorage};

    #[cfg(all(feature = "grpc", has_protos))]
    pub use crate::transport::{
        ClientConfig, NodeAddress, RaftClient, RaftClientPool, RaftGrpcServer, ServerConfig,
        TransportError as GrpcError,
    };
}
