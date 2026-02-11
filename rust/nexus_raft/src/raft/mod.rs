//! Raft consensus module for STRONG_HA zones.
//!
//! This module provides distributed consensus using tikv/raft-rs for
//! linearizable operations on metadata and locks.
//!
//! # Architecture
//!
//! ```text
//! ┌────────────────────────────────────────────────────────────────────────┐
//! │  Raft Consensus Group (Consensus Zone)                                  │
//! │                                                                         │
//! │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐              │
//! │  │   Leader     │    │   Follower   │    │   Witness    │              │
//! │  │              │    │              │    │              │              │
//! │  │ StateMachine │    │ StateMachine │    │ (No SM)      │              │
//! │  │   ├─ meta    │    │   ├─ meta    │    │              │              │
//! │  │   └─ locks   │    │   └─ locks   │    │              │              │
//! │  │              │    │              │    │              │              │
//! │  │ RaftStorage  │    │ RaftStorage  │    │ RaftStorage  │              │
//! │  │   (sled)     │    │   (sled)     │    │   (sled)     │              │
//! │  └──────────────┘    └──────────────┘    └──────────────┘              │
//! │                                                                         │
//! └────────────────────────────────────────────────────────────────────────┘
//! ```
//!
//! # Lock Types
//!
//! Supports both mutex and semaphore:
//! - `max_holders = 1`: Exclusive lock (mutex)
//! - `max_holders > 1`: Shared lock (semaphore with owner tracking)
//!
//! Each holder has a unique `lock_id` (UUID) for identification.
//!
//! # Key Components
//!
//! - [`RaftNode`]: Main entry point for Raft operations
//! - [`StateMachine`]: Trait for state machine implementations
//! - [`FullStateMachine`]: Full state machine with metadata and locks
//! - [`WitnessStateMachine`]: Minimal state machine for witness nodes
//! - [`RaftStorage`]: Persistent Raft log storage using sled

mod error;
mod state_machine;

#[cfg(feature = "consensus")]
mod node;
#[cfg(feature = "consensus")]
mod storage;
#[cfg(all(feature = "grpc", has_protos))]
mod zone_registry;

pub use error::{RaftError, Result};
pub use state_machine::{
    Command, CommandResult, FullStateMachine, HolderInfo, LockInfo, LockState, StateMachine,
    WitnessStateMachine, WitnessStateMachineInMemory,
};

#[cfg(feature = "consensus")]
pub use node::{NodeRole, RaftConfig, RaftMsg, RaftNode, RaftNodeDriver};
#[cfg(feature = "consensus")]
pub use storage::RaftStorage;
#[cfg(all(feature = "grpc", has_protos))]
pub use zone_registry::ZoneRaftRegistry;

/// A proposal to be replicated through Raft.
#[derive(Debug)]
pub struct Proposal {
    /// The command to propose.
    pub command: Command,
}
