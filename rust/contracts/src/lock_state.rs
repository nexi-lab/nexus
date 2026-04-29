//! Advisory lock state — shared SSOT between kernel and raft.
//!
//! After R14, advisory-lock state lives in exactly one place: a
//! `BTreeMap<String, LockEntry>` wrapped in `Arc<parking_lot::Mutex<LockState>>`
//! and shared between the kernel's `LockManager` and the raft crate's
//! `FullStateMachine`. The `apply_*` methods are the state-transition
//! primitives; raft apply and local-mode acquires both call them under
//! the same mutex, so there is no divergence window between writers and
//! readers. This matches the raft invariant that apply is an atomic
//! commit point, not a tuple of writes that could partially observe
//! each other.
//!
//! I/O locks (blocking read/write serialization) are kept local in the
//! kernel's `LockManager` — they never replicate and are orthogonal to
//! advisory locks.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

// ── Types ───────────────────────────────────────────────────────────

/// Per-holder conflict mode.
///
/// `Shared` holders may coexist with other `Shared` holders up to
/// `LockEntry::max_holders`. `Exclusive` holders must be the sole
/// holder — they block both other `Exclusive` acquirers and any
/// `Shared` acquirers. This is the standard reader-writer rule.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash, Default)]
pub enum LockMode {
    #[default]
    Exclusive,
    Shared,
}

/// Information about a single advisory lock holder.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HolderInfo {
    pub lock_id: String,
    pub holder_info: String,
    pub mode: LockMode,
    /// Unix seconds.
    pub acquired_at: u64,
    /// Unix seconds.
    pub expires_at: u64,
}

/// Persistent lock record — transport type for `get_lock` / `list_locks`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LockInfo {
    pub path: String,
    pub max_holders: u32,
    pub holders: Vec<HolderInfo>,
}

/// Result of a `apply_acquire` call — mirrors what is returned to clients
/// via `CommandResult::LockResult`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockAcquireResult {
    pub acquired: bool,
    pub current_holders: u32,
    pub max_holders: u32,
    pub holders: Vec<HolderInfo>,
}

/// Per-path entry in the SSOT map. Advisory-lock only — I/O lock state
/// stays in the kernel and never replicates.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct LockEntry {
    pub max_holders: u32,
    pub holders: Vec<HolderInfo>,
}

impl LockEntry {
    pub fn is_empty(&self) -> bool {
        self.holders.is_empty()
    }
}

/// Single source of truth for advisory locks.
///
/// Apply-path mutations (`apply_acquire` / `apply_release` /
/// `apply_force_release` / `apply_extend`) and reads (`get_lock` /
/// `list_locks`) all operate on the same `BTreeMap`. Callers own the
/// surrounding `Arc<parking_lot::Mutex<LockState>>` — this struct
/// doesn't ship its own locking so kernel and raft can share a single
/// lock discipline.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LockState {
    pub locks: BTreeMap<String, LockEntry>,
}

// ── Hierarchy helpers ───────────────────────────────────────────────

/// Collect the *strict* ancestors of `path` (must be normalized).
///
/// Example: `"/a/b/c"` → `["/a/b", "/a", "/"]`
fn ancestors(path: &str) -> Vec<&str> {
    if path == "/" || path.is_empty() {
        return Vec::new();
    }
    let mut result = Vec::new();
    let mut end = path.len();
    while let Some(pos) = path[..end].rfind('/') {
        if pos == 0 {
            result.push("/");
            break;
        }
        result.push(&path[..pos]);
        end = pos;
    }
    result
}

fn descendant_range(path: &str) -> (String, String) {
    let prefix = if path.ends_with('/') {
        path.to_string()
    } else {
        format!("{}/", path)
    };
    let mut upper = prefix.clone();
    upper.pop();
    upper.push('0'); // '0' > '/' in ASCII
    (prefix, upper)
}

// ── State-transition primitives ─────────────────────────────────────

impl LockState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Drop expired holders across the whole map and prune now-empty entries.
    /// Used by apply paths to keep the map compact.
    fn prune_expired(&mut self, now_secs: u64) {
        self.locks.retain(|_, entry| {
            entry.holders.retain(|h| h.expires_at > now_secs);
            !entry.is_empty()
        });
    }

    fn ancestor_conflict(&self, path: &str, mode: LockMode) -> bool {
        for anc in ancestors(path) {
            if let Some(entry) = self.locks.get(anc) {
                if entry.is_empty() {
                    continue;
                }
                match mode {
                    LockMode::Exclusive => return true,
                    LockMode::Shared => {
                        if entry.holders.iter().any(|h| h.mode == LockMode::Exclusive) {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }

    fn descendant_conflict(&self, path: &str, mode: LockMode) -> bool {
        let (lo, hi) = descendant_range(path);
        for (_, entry) in self.locks.range(lo..hi) {
            if entry.is_empty() {
                continue;
            }
            match mode {
                LockMode::Exclusive => return true,
                LockMode::Shared => {
                    if entry.holders.iter().any(|h| h.mode == LockMode::Exclusive) {
                        return true;
                    }
                }
            }
        }
        false
    }

    fn accepts_new_holder(entry: &LockEntry, mode: LockMode) -> bool {
        match mode {
            LockMode::Exclusive => entry.holders.is_empty(),
            LockMode::Shared => {
                let has_exclusive = entry.holders.iter().any(|h| h.mode == LockMode::Exclusive);
                !has_exclusive && (entry.holders.len() as u32) < entry.max_holders
            }
        }
    }

    fn to_result(entry: &LockEntry, acquired: bool) -> LockAcquireResult {
        LockAcquireResult {
            acquired,
            current_holders: entry.holders.len() as u32,
            max_holders: entry.max_holders,
            holders: entry.holders.clone(),
        }
    }

    /// Empty response for "lock did not exist and we rejected the attempt".
    /// Used when a hierarchy conflict prevents even creating the entry.
    fn empty_result(max_holders: u32) -> LockAcquireResult {
        LockAcquireResult {
            acquired: false,
            current_holders: 0,
            max_holders,
            holders: Vec::new(),
        }
    }

    /// Acquire (or re-acquire) an advisory lock holder.
    ///
    /// Does hierarchy conflict detection, idempotent re-acquire, and
    /// capacity matching. Behaviour matches the pre-R14 kernel local
    /// path — raft apply now calls this via `FullStateMachine`.
    #[allow(clippy::too_many_arguments)]
    pub fn apply_acquire(
        &mut self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
        mode: LockMode,
        now_secs: u64,
    ) -> LockAcquireResult {
        // Hierarchy conflict: reject without creating a dangling entry.
        if self.ancestor_conflict(path, mode) || self.descendant_conflict(path, mode) {
            return Self::empty_result(max_holders);
        }

        let expires_at = now_secs.saturating_add(ttl_secs as u64);
        let entry = self.locks.entry(path.to_string()).or_default();

        // Prune expired holders before checking capacity / mode.
        entry.holders.retain(|h| h.expires_at > now_secs);

        // Idempotent re-acquire by same lock_id — bump TTL.
        if let Some(h) = entry.holders.iter_mut().find(|h| h.lock_id == lock_id) {
            h.expires_at = expires_at;
            return Self::to_result(entry, true);
        }

        // First holder seeds max_holders; subsequent holders must match.
        if entry.holders.is_empty() {
            entry.max_holders = max_holders;
        } else if entry.max_holders != max_holders {
            return Self::to_result(entry, false);
        }

        if Self::accepts_new_holder(entry, mode) {
            entry.holders.push(HolderInfo {
                lock_id: lock_id.to_string(),
                holder_info: holder_info.to_string(),
                mode,
                acquired_at: now_secs,
                expires_at,
            });
            Self::to_result(entry, true)
        } else {
            Self::to_result(entry, false)
        }
    }

    /// Release one holder. Returns `true` if found.
    pub fn apply_release(&mut self, path: &str, lock_id: &str) -> bool {
        if let Some(entry) = self.locks.get_mut(path) {
            let before = entry.holders.len();
            entry.holders.retain(|h| h.lock_id != lock_id);
            let removed = entry.holders.len() < before;
            if entry.is_empty() {
                self.locks.remove(path);
            }
            removed
        } else {
            false
        }
    }

    /// Force-release all holders (admin override). Returns `true` if
    /// the lock existed.
    pub fn apply_force_release(&mut self, path: &str) -> bool {
        self.locks.remove(path).is_some()
    }

    /// Extend a specific holder's TTL. Returns `true` if extended.
    pub fn apply_extend(
        &mut self,
        path: &str,
        lock_id: &str,
        new_ttl_secs: u32,
        now_secs: u64,
    ) -> bool {
        let new_expires = now_secs.saturating_add(new_ttl_secs as u64);
        if let Some(entry) = self.locks.get_mut(path) {
            entry.holders.retain(|h| h.expires_at > now_secs);
            if let Some(h) = entry.holders.iter_mut().find(|h| h.lock_id == lock_id) {
                h.expires_at = new_expires;
                return true;
            }
            if entry.is_empty() {
                self.locks.remove(path);
            }
        }
        false
    }

    // ── Reads ────────────────────────────────────────────────────────

    pub fn get_lock(&self, path: &str) -> Option<LockInfo> {
        self.locks.get(path).and_then(|entry| {
            if entry.holders.is_empty() {
                None
            } else {
                Some(LockInfo {
                    path: path.to_string(),
                    max_holders: entry.max_holders,
                    holders: entry.holders.clone(),
                })
            }
        })
    }

    pub fn list_locks(&self, prefix: &str, limit: usize) -> Vec<LockInfo> {
        let mut out = Vec::new();
        for (key, entry) in self.locks.iter() {
            if out.len() >= limit {
                break;
            }
            if key.starts_with(prefix) && !entry.holders.is_empty() {
                out.push(LockInfo {
                    path: key.clone(),
                    max_holders: entry.max_holders,
                    holders: entry.holders.clone(),
                });
            }
        }
        out
    }

    /// Public read-side helper for expiry pruning (used by tests and
    /// future background reapers). apply_* already inline this on their
    /// own paths.
    pub fn gc_expired(&mut self, now_secs: u64) {
        self.prune_expired(now_secs);
    }
}

// ── Locks backend trait (R20.7) ─────────────────────────────────────
//
// The kernel's ``LockManager`` dispatches advisory-lock operations
// through this trait instead of holding a concrete
// ``ZoneConsensus<FullStateMachine>``. Two concrete impls live in the
// tree:
//
//   - ``nexus_runtime::locks::LocalLocks`` — mutates the shared
//     ``Arc<Mutex<LockState>>`` directly (pre-federation default,
//     single-node deployments).
//   - ``nexus_raft::federation::DistributedLocks`` — proposes a
//     ``Command::{Acquire,Release,Force,Extend}Lock`` through a raft
//     consensus node; apply writes into the shared ``LockState``.
//
// The trait lives in ``contracts`` because both kernel and raft crates
// depend on ``contracts`` already, and the trait avoids a cyclic
// dependency that would arise if kernel owned a raft type or raft
// owned a kernel trait.
//
// Errors use ``String`` rather than a crate-specific enum — both impls
// already translate their internal errors (``RaftError``, locked map
// corruption) into string form at the API boundary, and keeping the
// trait error type simple means contracts stays tier-neutral.
pub trait Locks: Send + Sync {
    /// Try to acquire a lock. Returns ``Ok(true)`` if the caller
    /// became (or already was) a holder, ``Ok(false)`` on conflict.
    #[allow(clippy::too_many_arguments)]
    fn acquire(
        &self,
        path: &str,
        lock_id: &str,
        mode: LockMode,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
    ) -> Result<bool, String>;

    /// Release a specific holder. Returns ``Ok(true)`` if found.
    fn release(&self, path: &str, lock_id: &str) -> Result<bool, String>;

    /// Force-release ALL holders on ``path`` (admin override).
    fn force_release(&self, path: &str) -> Result<bool, String>;

    /// Extend a holder's TTL. Returns ``Ok(true)`` if extended.
    fn extend(&self, path: &str, lock_id: &str, ttl_secs: u32) -> Result<bool, String>;

    /// Read the full advisory lock record (or ``None`` if unlocked).
    fn get_lock(&self, path: &str) -> Option<LockInfo>;

    /// Enumerate locks under ``prefix``, capped at ``limit``.
    fn list_locks(&self, prefix: &str, limit: usize) -> Vec<LockInfo>;
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn acq(
        state: &mut LockState,
        path: &str,
        id: &str,
        mode: LockMode,
        max: u32,
        ttl: u32,
    ) -> LockAcquireResult {
        state.apply_acquire(path, id, max, ttl, "agent", mode, 1000)
    }

    #[test]
    fn exclusive_blocks_exclusive() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "h1", LockMode::Exclusive, 1, 60).acquired);
        assert!(!acq(&mut s, "/a", "h2", LockMode::Exclusive, 1, 60).acquired);
    }

    #[test]
    fn shared_coexists_up_to_max() {
        let mut s = LockState::new();
        for id in ["r1", "r2", "r3"] {
            assert!(acq(&mut s, "/a", id, LockMode::Shared, 3, 60).acquired);
        }
        assert!(!acq(&mut s, "/a", "r4", LockMode::Shared, 3, 60).acquired);
    }

    #[test]
    fn exclusive_blocked_by_shared() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "r1", LockMode::Shared, 3, 60).acquired);
        assert!(!acq(&mut s, "/a", "w1", LockMode::Exclusive, 3, 60).acquired);
    }

    #[test]
    fn shared_blocked_by_exclusive() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "w1", LockMode::Exclusive, 3, 60).acquired);
        assert!(!acq(&mut s, "/a", "r1", LockMode::Shared, 3, 60).acquired);
    }

    #[test]
    fn idempotent_reacquire_same_holder() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "h1", LockMode::Exclusive, 1, 60).acquired);
        let second = acq(&mut s, "/a", "h1", LockMode::Exclusive, 1, 60);
        assert!(second.acquired);
        assert_eq!(second.current_holders, 1);
    }

    #[test]
    fn capacity_mismatch_rejects() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "r1", LockMode::Shared, 3, 60).acquired);
        assert!(!acq(&mut s, "/a", "w1", LockMode::Exclusive, 1, 60).acquired);
    }

    #[test]
    fn hierarchy_parent_exclusive_blocks_child() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/folder", "h1", LockMode::Exclusive, 1, 60).acquired);
        assert!(!acq(&mut s, "/folder/file", "h2", LockMode::Exclusive, 1, 60).acquired);
        assert!(!acq(&mut s, "/folder/file", "h3", LockMode::Shared, 2, 60).acquired);
    }

    #[test]
    fn hierarchy_child_exclusive_blocks_parent() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/folder/file", "h1", LockMode::Exclusive, 1, 60).acquired);
        assert!(!acq(&mut s, "/folder", "h2", LockMode::Exclusive, 1, 60).acquired);
    }

    #[test]
    fn hierarchy_sibling_no_conflict() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a/bc", "h1", LockMode::Exclusive, 1, 60).acquired);
        // /a/b is a sibling (not ancestor or descendant) of /a/bc
        assert!(acq(&mut s, "/a/b", "h2", LockMode::Exclusive, 1, 60).acquired);
    }

    #[test]
    fn release_removes_holder() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "h1", LockMode::Exclusive, 1, 60).acquired);
        assert!(s.apply_release("/a", "h1"));
        assert!(s.get_lock("/a").is_none());
    }

    #[test]
    fn force_release_drops_all_holders() {
        let mut s = LockState::new();
        assert!(acq(&mut s, "/a", "r1", LockMode::Shared, 3, 60).acquired);
        assert!(acq(&mut s, "/a", "r2", LockMode::Shared, 3, 60).acquired);
        assert!(s.apply_force_release("/a"));
        assert!(s.get_lock("/a").is_none());
    }

    #[test]
    fn extend_refreshes_ttl() {
        let mut s = LockState::new();
        s.apply_acquire("/a", "h1", 1, 1, "agent", LockMode::Exclusive, 1000);
        let before = s.get_lock("/a").unwrap().holders[0].expires_at;
        assert!(s.apply_extend("/a", "h1", 3600, 1000));
        let after = s.get_lock("/a").unwrap().holders[0].expires_at;
        assert!(after > before);
    }

    #[test]
    fn expired_holder_auto_evicted_on_reacquire() {
        let mut s = LockState::new();
        // TTL=1 at t=1000 → expires at 1001
        s.apply_acquire("/a", "h1", 1, 1, "agent", LockMode::Exclusive, 1000);
        // Another holder at t=1002 — the original should be pruned.
        let res = s.apply_acquire("/a", "h2", 1, 60, "agent", LockMode::Exclusive, 1002);
        assert!(res.acquired);
        let lock = s.get_lock("/a").unwrap();
        assert_eq!(lock.holders.len(), 1);
        assert_eq!(lock.holders[0].lock_id, "h2");
    }

    #[test]
    fn list_locks_filters_by_prefix() {
        let mut s = LockState::new();
        acq(&mut s, "/ns/a", "h1", LockMode::Exclusive, 1, 60);
        acq(&mut s, "/ns/b", "h2", LockMode::Shared, 2, 60);
        acq(&mut s, "/other", "h3", LockMode::Exclusive, 1, 60);
        assert_eq!(s.list_locks("/ns/", 10).len(), 2);
        assert_eq!(s.list_locks("/", 10).len(), 3);
    }

    #[test]
    fn clone_preserves_state() {
        let mut s = LockState::new();
        acq(&mut s, "/a", "h1", LockMode::Exclusive, 1, 60);
        acq(&mut s, "/b", "r1", LockMode::Shared, 3, 60);
        let copy = s.clone();
        assert_eq!(copy.get_lock("/a"), s.get_lock("/a"));
        assert_eq!(copy.get_lock("/b"), s.get_lock("/b"));
    }

    #[test]
    fn gc_prunes_expired_entries() {
        let mut s = LockState::new();
        s.apply_acquire("/a", "h1", 1, 1, "agent", LockMode::Exclusive, 1000);
        s.gc_expired(2000);
        assert!(s.get_lock("/a").is_none());
    }
}
