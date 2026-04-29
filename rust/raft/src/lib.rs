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

// F2 C8: mimalloc allocator moved to the final cdylib (nexus_runtime).
// An rlib cannot declare ``#[global_allocator]`` — only the final
// binary (cdylib/bin) can.

pub mod storage;

/// Raft consensus module for STRONG_HA zones.
///
/// Provides distributed consensus using tikv/raft-rs for linearizable
/// metadata and lock operations. Requires `consensus` feature for
/// full ZoneConsensus support (leader election, log replication).
pub mod raft;

/// Federation orchestration layer — cross-node zone share / join
/// flows and the TOFU peer-CA trust store. See [`federation`] for
/// the sub-module layout.
#[cfg(feature = "grpc")]
pub mod federation;

/// Pure-Rust zone handle — kernel-internal, not exposed to Python.
#[cfg(all(feature = "grpc", has_protos))]
pub mod zone_handle;

/// Pure-Rust zone manager — kernel-internal, not exposed to Python.
#[cfg(all(feature = "grpc", has_protos))]
pub mod zone_manager;

/// BlobFetcher trait — lets the raft gRPC server serve peer-facing
/// `ReadBlob` without depending on kernel types. The kernel crate
/// installs the impl at bootstrap. See [`blob_fetcher`] module.
#[cfg(all(feature = "grpc", has_protos))]
pub mod blob_fetcher;

#[cfg(all(feature = "grpc", has_protos))]
pub use zone_handle::{Consistency, ZoneHandle};

#[cfg(all(feature = "grpc", has_protos))]
pub use zone_manager::{ClusterStatus, TlsFiles, ZoneManager};

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
/// F2 C8 (Option A): raft is an rlib inside the ``nexus_runtime`` cdylib.
/// The PyO3 classes are registered by calling
/// ``_nexus_raft::register_python_classes(m)`` from kernel's
/// ``#[pymodule]`` entry point.
#[cfg(feature = "python")]
pub mod pyo3_bindings;

// Phase H of the rust-workspace restructure (Phase 5 federation DI):
// state-machine impls + the `FederationProvider` trait impl that
// previously lived in the kernel crate moved here when the kernel ↔
// raft Cargo edge was inverted.
//
//   federation_provider.rs    — `RaftFederationProvider` impl FederationProvider
//   zone_meta_store.rs        — Raft-backed `kernel::abc::MetaStore` impl
//   replication_scanner.rs    — EC replication background scanner
//   wal_stream_backend.rs     — `kernel::stream::StreamBackend` impl that
//                               persists each entry through Raft
#[cfg(all(feature = "grpc", has_protos))]
pub mod federation_provider;
#[cfg(all(feature = "grpc", has_protos))]
pub mod replication_scanner;
// WAL stream / pipe backends moved into the kernel crate
// (`kernel::core::stream::wal`, `kernel::core::pipe::wal`) — they
// are kernel primitives that compose whatever distributed `MetaStore`
// federation has DI'd (typically `ZoneMetaStore` below).  Raft no
// longer ships its own `WalConsensus` trait; the kernel-side
// `MetaStore::{append_stream_entry, get_stream_entry}` methods are
// the abstraction.
#[cfg(all(feature = "grpc", has_protos))]
pub mod zone_meta_store;

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
        Command, CommandResult, FullStateMachine, HolderInfo, LockAcquireResult, LockEntry,
        LockInfo, LockMode, LockState, RaftError, StateMachine, WitnessStateMachine,
    };

    #[cfg(feature = "consensus")]
    pub use crate::raft::{NodeRole, RaftConfig, RaftStorage, ZoneConsensus};

    #[cfg(all(feature = "grpc", has_protos))]
    pub use crate::transport::{
        ClientConfig, NodeAddress, RaftClient, RaftClientPool, RaftGrpcServer, ServerConfig,
        TransportError as GrpcError,
    };
}
