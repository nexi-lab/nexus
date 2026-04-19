//! Distributed advisory-lock backend (R20.7).
//!
//! Implements the ``contracts::Locks`` trait on top of a
//! ``ZoneConsensus<FullStateMachine>``. Write operations propose a
//! ``Command::{Acquire,Release,Force,Extend}Lock`` through raft; the
//! apply path mutates the shared ``Arc<Mutex<LockState>>`` on every
//! peer. Reads hit that same shared map directly (no round-trip) —
//! same post-R14 consistency model as the previous in-kernel
//! implementation.
//!
//! The kernel installs this backend via
//! ``Kernel::install_locks(Arc<DistributedLocks>, shared_state)``
//! exactly once per process; federation's ``setup_zone`` is the
//! caller and pre-migrates existing local holders into the state
//! machine's map before the swap.

use std::sync::Arc;

use parking_lot::Mutex;

use contracts::lock_state::{LockInfo, LockMode, LockState, Locks};

use crate::raft::{Command, CommandResult, FullStateMachine, ZoneConsensus};

fn now_secs() -> u64 {
    FullStateMachine::now()
}

fn local_now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Distributed ``Locks`` backend — raft-replicated advisory locks.
///
/// Wraps a ``ZoneConsensus<FullStateMachine>`` + its tokio runtime +
/// the state machine's shared advisory ``Arc<Mutex<LockState>>``.
///
/// ``shared_state`` is the same Arc the state machine's apply path
/// mutates — owning a handle here lets ``get_lock`` / ``list_locks``
/// read without a raft round-trip.
pub struct DistributedLocks {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
    shared_state: Arc<Mutex<LockState>>,
}

impl DistributedLocks {
    /// Construct a distributed backend + migrate existing local holders
    /// into the state machine's advisory map.
    ///
    /// ``kernel_local_state`` is the kernel's current advisory Arc
    /// (``LockManager::advisory_state_arc()``) — any holders already
    /// in it are merged into the state machine's map, using
    /// state-machine state as authoritative (raft may have replayed
    /// committed entries the moment the state machine was
    /// constructed; never overwrite raft-owned paths with stale
    /// local data).
    ///
    /// Returns ``(backend, shared_state_arc)`` — callers pass the
    /// second tuple element to ``LockManager::install_locks`` so the
    /// kernel swaps its read-side Arc alongside the backend.
    pub fn new(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        kernel_local_state: Arc<Mutex<LockState>>,
    ) -> (Self, Arc<Mutex<LockState>>) {
        // Adopt the state machine's shared advisory Arc. Using
        // ``runtime.block_on`` because ``install_locks`` is invoked
        // from a sync context (kernel::sys_setattr DT_MOUNT).
        let shared_state: Arc<Mutex<LockState>> = runtime.block_on(async {
            node.with_state_machine(|sm: &FullStateMachine| sm.advisory_state())
                .await
        });

        // Merge kernel-local holders that have no corresponding
        // raft-apply row. Raft may already have replayed entries; we
        // treat raft's state as authoritative and only fill gaps.
        {
            let mut dst = shared_state.lock();
            let src = kernel_local_state.lock();
            let mut migrated = 0usize;
            let mut skipped = 0usize;
            for (path, entry) in &src.locks {
                if entry.holders.is_empty() {
                    continue;
                }
                if dst.locks.contains_key(path) {
                    skipped += 1;
                    continue;
                }
                dst.locks.insert(path.clone(), entry.clone());
                migrated += 1;
            }
            if migrated > 0 || skipped > 0 {
                tracing::info!(
                    migrated = migrated,
                    skipped_because_raft_owns = skipped,
                    "DistributedLocks::new: migrated local advisory holders into state-machine map",
                );
            }
        }

        let backend = Self {
            node,
            runtime,
            shared_state: shared_state.clone(),
        };
        (backend, shared_state)
    }
}

impl Locks for DistributedLocks {
    fn acquire(
        &self,
        path: &str,
        lock_id: &str,
        mode: LockMode,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> Result<bool, String> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
            mode,
            now_secs: now_secs(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("DistributedLocks.acquire({path}): {e}"))?;
        match result {
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::Error(e) => {
                Err(format!("DistributedLocks.acquire({path}) rejected: {e}"))
            }
            _ => Err("DistributedLocks.acquire: unexpected result type".into()),
        }
    }

    fn release(&self, path: &str, lock_id: &str) -> Result<bool, String> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("DistributedLocks.release({path}): {e}"))?;
        Ok(matches!(result, CommandResult::Success))
    }

    fn force_release(&self, path: &str) -> Result<bool, String> {
        let cmd = Command::ForceReleaseLock {
            path: path.to_string(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("DistributedLocks.force_release({path}): {e}"))?;
        Ok(matches!(result, CommandResult::Success))
    }

    fn extend(&self, path: &str, lock_id: &str, ttl_secs: u32) -> Result<bool, String> {
        let cmd = Command::ExtendLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            new_ttl_secs: ttl_secs,
            now_secs: now_secs(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("DistributedLocks.extend({path}): {e}"))?;
        Ok(matches!(result, CommandResult::Success))
    }

    fn get_lock(&self, path: &str) -> Option<LockInfo> {
        // GC expired holders in-line so callers never observe a stale
        // record that the raft apply path hasn't had reason to touch
        // (committed writes prune on apply; reads don't).
        let mut guard = self.shared_state.lock();
        guard.gc_expired(local_now_secs());
        guard.get_lock(path)
    }

    fn list_locks(&self, prefix: &str, limit: usize) -> Vec<LockInfo> {
        let mut guard = self.shared_state.lock();
        guard.gc_expired(local_now_secs());
        guard.list_locks(prefix, limit)
    }
}
