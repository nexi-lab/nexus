//! Distributed advisory-lock backend.
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

use contracts::lock_state::{LockInfo, LockState, Locks};

use crate::raft::{Command, CommandResult, FullStateMachine, ZoneConsensus};

fn now_secs() -> u64 {
    FullStateMachine::now()
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
    /// The returned backend exposes its shared state via the
    /// ``Locks::shared_state_arc`` trait method, so callers don't pass
    /// a separate state Arc to ``LockManager::install_locks`` — the
    /// kernel pulls it back through the trait, eliminating the misuse
    /// risk of mismatching backend/state Arcs at install time.
    pub fn new(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        kernel_local_state: Arc<Mutex<LockState>>,
    ) -> Self {
        // Adopt the state machine's shared advisory Arc.  Two callers
        // reach this path:
        //
        //   1. ``kernel::sys_setattr DT_MOUNT`` — sync gRPC handler
        //      thread, no tokio runtime context.
        //   2. ``mount_apply_cb`` on followers — the closure fires
        //      inside the raft apply loop, which IS a tokio task on
        //      the zone manager's multi-thread runtime.
        //
        // Calling ``runtime.block_on`` from path (2) panics with
        // "Cannot start a runtime from within a runtime", and
        // ``block_in_place`` only papers over the panic without fixing
        // the worker-exhaustion deadlock that follows under
        // concurrent DT_MOUNT load.
        //
        // ``advisory_state_blocking`` uses ``RwLock::blocking_read``
        // which suspends the OS thread without reentering the runtime
        // — works identically from both paths (sync gRPC thread or
        // raft apply task) and never deadlocks because the read-side
        // is read-mostly + the apply path holds the write lock only
        // briefly per entry.
        let shared_state: Arc<Mutex<LockState>> = node.advisory_state_blocking();
        let _ = runtime;

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

        Self {
            node,
            runtime,
            shared_state,
        }
    }
}

impl Locks for DistributedLocks {
    fn acquire(
        &self,
        path: &str,
        lock_id: &str,
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
            now_secs: now_secs(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| format!("DistributedLocks.acquire({path}): {e}"))?;
        match result {
            CommandResult::LockResult(state) => {
                if state.acquired {
                    // Read-your-writes on follower: poll local state
                    // machine until the exact lock_id we just wrote
                    // appears. SSOT = `shared_state` (the same
                    // `Arc<Mutex<LockState>>` the apply path mutates).
                    // Using a raft-index barrier instead would fail on
                    // followers — `commit_index()` immediately after
                    // propose is stale until AppendEntries arrives.
                    let shared = Arc::clone(&self.shared_state);
                    let lock_id_owned = lock_id.to_string();
                    let path_owned = path.to_string();
                    let _ = self.node.wait_until(
                        || {
                            shared
                                .lock()
                                .get_lock(&path_owned)
                                .map(|info| info.holders.iter().any(|h| h.lock_id == lock_id_owned))
                                .unwrap_or(false)
                        },
                        500,
                    );
                }
                Ok(state.acquired)
            }
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
        // Reads share semantics with ``LocalLocks`` post-fix: no
        // read-side GC. ``apply_*`` paths prune expired holders on
        // every write, so unmutated rows that survive are by
        // definition cold and never block live decisions.
        self.shared_state.lock().get_lock(path)
    }

    fn list_locks(&self, prefix: &str, limit: usize) -> Vec<LockInfo> {
        self.shared_state.lock().list_locks(prefix, limit)
    }

    fn shared_state_arc(&self) -> Arc<Mutex<LockState>> {
        self.shared_state.clone()
    }
}
