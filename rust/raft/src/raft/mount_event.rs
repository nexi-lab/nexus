//! DT_MOUNT apply-side events (R16.2).
//!
//! Replaces the Python ``start_mount_reconciler`` polling thread with
//! event-driven notifications fired from ``FullStateMachine::apply``
//! after a ``Command::SetMetadata`` for a DT_MOUNT entry commits to
//! disk. The event stream is consumed by ``PyZoneManager``'s tokio
//! task, which invokes a registered Python callback under the GIL.
//!
//! # Raft-contract guarantees
//!
//! - Events fire only for entries that have been persisted to
//!   redb inside the atomic apply transaction — so consumers see a
//!   prefix of the committed log, never an uncommitted or rolled-back
//!   mutation.
//! - The send site uses ``UnboundedSender::send`` which is O(1) and
//!   non-blocking; it cannot stall the apply loop.
//! - A failed send (channel closed, hook not registered, decode
//!   failure) is logged at ``error!``/``warn!`` — never silently
//!   dropped — but the error never propagates out of ``apply`` because
//!   returning ``Err`` there would poison the state machine per raft's
//!   "apply must not fail" invariant.

#[cfg(feature = "grpc")]
use tokio::sync::mpsc::UnboundedSender;

/// A single DT_MOUNT commit event, fired from ``apply_set_metadata``
/// after the write transaction commits.
///
/// Consumer is expected to wire the mount point into the node's local
/// ``DriverLifecycleCoordinator`` / kernel mount table so sys_* cold
/// paths resolve into the target zone.
#[cfg(feature = "grpc")]
#[derive(Debug, Clone)]
pub struct MountEvent {
    /// The zone whose state machine applied the DT_MOUNT entry (the
    /// *parent* zone — the one containing the mount point).
    pub parent_zone_id: String,
    /// The zone-relative path where the DT_MOUNT entry lives (the key
    /// that was written via ``Command::SetMetadata``).
    pub mount_path: String,
    /// The zone the mount points to (``FileMetadata.target_zone_id``).
    /// Always non-empty for a well-formed DT_MOUNT — entries with an
    /// empty target zone are skipped by the send site so consumers
    /// never see them.
    pub target_zone_id: String,
}

/// Sender half of the event channel. Cloned into every
/// ``FullStateMachine`` by ``ZoneRaftRegistry::setup_zone`` after the
/// per-zone SM is constructed but before it's moved into
/// ``ZoneConsensus``.
#[cfg(feature = "grpc")]
pub type MountEventTx = UnboundedSender<MountEvent>;
