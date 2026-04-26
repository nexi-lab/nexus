//! Advisory-lock backends for the kernel's ``LockManager`` (R20.7).
//!
//! Two impls live in the tree today:
//!   - ``LocalLocks`` (this file) — direct mutation of the shared
//!     ``Arc<Mutex<LockState>>``, no replication.
//!   - ``nexus_raft::federation::DistributedLocks`` — proposes a
//!     ``Command::{Acquire,Release,Force,Extend}Lock`` through a raft
//!     ``ZoneConsensus``; apply mutates the same shared map on every
//!     peer.
//!
//! ``LockManager`` stores the backend as ``Arc<dyn contracts::Locks>``
//! so the kernel has no dependency on any raft concrete type. The
//! default backend is ``LocalLocks``; federation DI swaps in
//! ``DistributedLocks`` via ``Kernel::install_locks`` at nexus init
//! time (first-wins; idempotent).

use std::sync::Arc;

use parking_lot::Mutex;

use contracts::lock_state::{LockInfo, LockMode, LockState, Locks};

use crate::lock_manager::lock_now_secs;

/// Local-mode advisory lock backend: one Arc-wrapped ``LockState``
/// mutated directly on every call. Never proposes anything — used by
/// standalone deployments and as the kernel's default before
/// federation DI fires.
pub struct LocalLocks {
    state: Arc<Mutex<LockState>>,
}

impl LocalLocks {
    /// Construct a LocalLocks that shares ``state`` with whoever owns
    /// the Arc (typically the kernel's own ``LockManager``). When the
    /// backend is later swapped for ``DistributedLocks`` (R20.7
    /// install), the kernel passes the current state Arc along so
    /// existing holders are not lost — the federation-side impl
    /// merges them into the state machine's map under the same mutex
    /// discipline.
    pub fn new(state: Arc<Mutex<LockState>>) -> Self {
        Self { state }
    }

    /// Snapshot the shared state Arc — used by federation setup to
    /// migrate local holders into the raft state machine's map.
    pub fn state(&self) -> Arc<Mutex<LockState>> {
        self.state.clone()
    }
}

impl Locks for LocalLocks {
    fn acquire(
        &self,
        path: &str,
        lock_id: &str,
        mode: LockMode,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> Result<bool, String> {
        let now = lock_now_secs();
        let mut guard = self.state.lock();
        let result =
            guard.apply_acquire(path, lock_id, max_holders, ttl_secs, holder_info, mode, now);
        Ok(result.acquired)
    }

    fn release(&self, path: &str, lock_id: &str) -> Result<bool, String> {
        Ok(self.state.lock().apply_release(path, lock_id))
    }

    fn force_release(&self, path: &str) -> Result<bool, String> {
        Ok(self.state.lock().apply_force_release(path))
    }

    fn extend(&self, path: &str, lock_id: &str, ttl_secs: u32) -> Result<bool, String> {
        let now = lock_now_secs();
        Ok(self.state.lock().apply_extend(path, lock_id, ttl_secs, now))
    }

    fn get_lock(&self, path: &str) -> Option<LockInfo> {
        self.state.lock().get_lock(path)
    }

    fn list_locks(&self, prefix: &str, limit: usize) -> Vec<LockInfo> {
        self.state.lock().list_locks(prefix, limit)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backend() -> LocalLocks {
        LocalLocks::new(Arc::new(Mutex::new(LockState::new())))
    }

    #[test]
    fn acquire_release_roundtrip() {
        let b = backend();
        assert!(b
            .acquire("/a", "h1", LockMode::Exclusive, 1, 60, "agent")
            .unwrap());
        assert!(b.release("/a", "h1").unwrap());
        assert!(b.get_lock("/a").is_none());
    }

    #[test]
    fn exclusive_blocks_second_acquire() {
        let b = backend();
        assert!(b
            .acquire("/a", "h1", LockMode::Exclusive, 1, 60, "agent")
            .unwrap());
        assert!(!b
            .acquire("/a", "h2", LockMode::Exclusive, 1, 60, "agent")
            .unwrap());
    }

    #[test]
    fn shared_coexists_up_to_max() {
        let b = backend();
        assert!(b
            .acquire("/a", "h1", LockMode::Shared, 2, 60, "agent")
            .unwrap());
        assert!(b
            .acquire("/a", "h2", LockMode::Shared, 2, 60, "agent")
            .unwrap());
        // Third reader exceeds max_holders=2
        assert!(!b
            .acquire("/a", "h3", LockMode::Shared, 2, 60, "agent")
            .unwrap());
    }

    #[test]
    fn force_release_drops_all() {
        let b = backend();
        b.acquire("/a", "h1", LockMode::Shared, 3, 60, "agent")
            .unwrap();
        b.acquire("/a", "h2", LockMode::Shared, 3, 60, "agent")
            .unwrap();
        assert!(b.force_release("/a").unwrap());
        assert!(b.get_lock("/a").is_none());
    }

    #[test]
    fn extend_refreshes_ttl() {
        let b = backend();
        b.acquire("/a", "h1", LockMode::Exclusive, 1, 1, "agent")
            .unwrap();
        let before = b.get_lock("/a").unwrap().holders[0].expires_at;
        assert!(b.extend("/a", "h1", 3600).unwrap());
        let after = b.get_lock("/a").unwrap().holders[0].expires_at;
        assert!(after > before);
    }

    #[test]
    fn list_locks_filters_by_prefix() {
        let b = backend();
        b.acquire("/a/one", "h1", LockMode::Exclusive, 1, 60, "agent")
            .unwrap();
        b.acquire("/a/two", "h2", LockMode::Exclusive, 1, 60, "agent")
            .unwrap();
        b.acquire("/b/three", "h3", LockMode::Exclusive, 1, 60, "agent")
            .unwrap();
        let under_a = b.list_locks("/a/", 100);
        assert_eq!(under_a.len(), 2);
    }
}
