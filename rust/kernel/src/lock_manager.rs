//! Unified lock manager — kernel primitive §4.4.
//!
//! Single `LockManager` struct replaces both `VFSLockManagerInner` (I/O locks)
//! and `LocalLockManager` / `DistributedLockManager` (advisory locks).
//!
//! Two acquire modes on the same struct:
//!   - **I/O lock** (kernel-internal): blocking, hierarchy-aware, no TTL, auto
//!     handle via `blocking_acquire` / `do_release`.
//!   - **Advisory lock** (user-facing): try-once, hierarchy-aware, TTL-based,
//!     explicit lock_id via `acquire_lock` / `release_lock` / `extend_lock`.
//!
//! Both lock types stored in the same `BTreeMap<String, LockEntry>`, enabling
//! unified hierarchy conflict detection. I/O and advisory locks are orthogonal —
//! they do not conflict with each other.
//!
//! Optional Raft backend: when `raft` is `Some`, advisory lock writes go through
//! `propose()`, reads through `read_linearizable()`. I/O locks always stay local.

use parking_lot::{Condvar, Mutex};
use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use nexus_raft::prelude::{
    Command, CommandResult, FullStateMachine, LockInfo as RaftLockInfo, LockMode as RaftLockMode,
    ZoneConsensus,
};

// ── Lock types (advisory) ───────────────────────────────────────────

/// Per-holder conflict mode (advisory locks).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default)]
pub enum KernelLockMode {
    /// Sole-holder lock. Blocks any concurrent acquire.
    #[default]
    Exclusive,
    /// Read-like lock. Coexists with other Shared holders up to
    /// `KernelLockInfo::max_holders`; blocked by any Exclusive holder.
    Shared,
}

/// Information about a single advisory lock holder.
#[derive(Clone, Debug, Default)]
pub struct KernelHolderInfo {
    pub lock_id: String,
    pub holder_info: String,
    pub mode: KernelLockMode,
    pub acquired_at_secs: u64,
    pub expires_at_secs: u64,
}

/// Advisory lock entry returned by `get_lock_info` / `list_locks`.
#[derive(Clone, Debug, Default)]
pub struct KernelLockInfo {
    pub path: String,
    pub max_holders: u32,
    pub holders: Vec<KernelHolderInfo>,
}

// ── I/O lock types ──────────────────────────────────────────────────

/// I/O lock mode (kernel-internal read/write serialization).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockMode {
    Read,
    Write,
}

/// Reverse-map entry: maps an I/O handle back to its path and mode.
#[derive(Debug, Clone)]
struct HandleInfo {
    path: String,
    mode: LockMode,
}

// ── Unified lock entry ──────────────────────────────────────────────

/// Per-path lock state — holds both I/O lock and advisory lock state.
///
/// I/O and advisory locks are orthogonal: an I/O write lock on `/a` does
/// NOT conflict with an advisory Exclusive lock on `/a`. They serve
/// different layers (kernel I/O serialization vs. user-facing coordination).
#[derive(Debug, Clone)]
struct LockEntry {
    // ── I/O lock state (anonymous, blocking) ──
    io_readers: u32,
    io_writer: Option<u64>, // handle of current writer
    // ── Advisory lock state (named, TTL-based) ──
    max_holders: u32,
    holders: Vec<KernelHolderInfo>,
}

impl LockEntry {
    fn new() -> Self {
        Self {
            io_readers: 0,
            io_writer: None,
            max_holders: 0,
            holders: Vec::new(),
        }
    }

    /// True when both I/O and advisory state are empty (can be GC'd).
    fn is_idle(&self) -> bool {
        self.io_readers == 0 && self.io_writer.is_none() && self.holders.is_empty()
    }

    /// True when I/O state is idle (for `is_locked` check).
    fn io_idle(&self) -> bool {
        self.io_readers == 0 && self.io_writer.is_none()
    }
}

/// Protected state: all lock mutations go through this Mutex.
#[derive(Debug)]
struct LockState {
    locks: BTreeMap<String, LockEntry>,
    handles: HashMap<u64, HandleInfo>,
}

// ── Path helpers ────────────────────────────────────────────────────

/// Normalize a path: collapse repeated slashes, remove trailing slash (except root).
pub(crate) fn normalize_path(path: &str) -> String {
    if path.is_empty() {
        return "/".to_string();
    }

    let mut result = String::with_capacity(path.len());
    let mut prev_slash = false;

    for ch in path.chars() {
        if ch == '/' {
            if !prev_slash {
                result.push('/');
            }
            prev_slash = true;
        } else {
            result.push(ch);
            prev_slash = false;
        }
    }

    if result.len() > 1 && result.ends_with('/') {
        result.pop();
    }

    result
}

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

// ── Advisory lock helpers ───────────────────────────────────────────

pub(crate) fn lock_now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn prune_expired_holders(holders: &mut Vec<KernelHolderInfo>, now: u64) {
    holders.retain(|h| h.expires_at_secs > now);
}

fn accepts_new_holder(
    holders: &[KernelHolderInfo],
    max_holders: u32,
    mode: KernelLockMode,
) -> bool {
    match mode {
        KernelLockMode::Exclusive => holders.is_empty(),
        KernelLockMode::Shared => {
            let has_exclusive = holders.iter().any(|h| h.mode == KernelLockMode::Exclusive);
            !has_exclusive && (holders.len() as u32) < max_holders
        }
    }
}

// ── Raft conversion helpers ─────────────────────────────────────────

fn kernel_mode_to_raft(m: KernelLockMode) -> RaftLockMode {
    match m {
        KernelLockMode::Exclusive => RaftLockMode::Exclusive,
        KernelLockMode::Shared => RaftLockMode::Shared,
    }
}

fn raft_mode_to_kernel(m: RaftLockMode) -> KernelLockMode {
    match m {
        RaftLockMode::Exclusive => KernelLockMode::Exclusive,
        RaftLockMode::Shared => KernelLockMode::Shared,
    }
}

fn raft_lock_to_kernel(lock: RaftLockInfo) -> KernelLockInfo {
    KernelLockInfo {
        path: lock.path,
        max_holders: lock.max_holders,
        holders: lock
            .holders
            .into_iter()
            .map(|h| KernelHolderInfo {
                lock_id: h.lock_id,
                holder_info: h.holder_info,
                mode: raft_mode_to_kernel(h.mode),
                acquired_at_secs: h.acquired_at,
                expires_at_secs: h.expires_at,
            })
            .collect(),
    }
}

// ── LockError ───────────────────────────────────────────────────────

/// Error type for lock operations.
#[derive(Debug)]
pub enum LockError {
    IOError(String),
}

impl std::fmt::Display for LockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LockError::IOError(msg) => write!(f, "{}", msg),
        }
    }
}

// ═══════════════════════════════════════════════════════════════════
// LockManager — unified I/O + advisory lock primitive
// ═══════════════════════════════════════════════════════════════════

/// Unified lock manager: I/O lock + advisory lock + optional Raft.
///
/// Replaces the previous `VFSLockManagerInner` (I/O), `LocalLockManager`
/// (advisory, standalone), `DistributedLockManager` (advisory, federation),
/// and `LockManagerKind` enum dispatch.
///
/// Shared via `Arc` between Kernel and VFSLockManager PyO3 wrapper.
pub struct LockManager {
    state: Mutex<LockState>,
    notify: Condvar,        // for blocking I/O acquire
    next_handle: AtomicU64, // for auto-generated I/O lock handles
    raft: Mutex<Option<(ZoneConsensus<FullStateMachine>, tokio::runtime::Handle)>>,

    // Metrics (relaxed atomics — approximate counters)
    acquire_count: AtomicU64,
    release_count: AtomicU64,
    contention_count: AtomicU64,
    total_acquire_ns: AtomicU64,
    timeout_count: AtomicU64,
}

impl LockManager {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(LockState {
                locks: BTreeMap::new(),
                handles: HashMap::new(),
            }),
            notify: Condvar::new(),
            next_handle: AtomicU64::new(0),
            raft: Mutex::new(None),
            acquire_count: AtomicU64::new(0),
            release_count: AtomicU64::new(0),
            contention_count: AtomicU64::new(0),
            total_acquire_ns: AtomicU64::new(0),
            timeout_count: AtomicU64::new(0),
        }
    }

    /// Upgrade to distributed mode (federation DI). Sets Raft backend
    /// for advisory lock operations. I/O locks remain local always.
    pub fn upgrade_to_distributed(
        &self,
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) {
        *self.raft.lock() = Some((node, runtime));
    }

    // ── I/O lock: blocking acquire ──────────────────────────────────

    /// Blocking acquire with timeout (for Rust-internal I/O callers).
    /// Returns non-zero handle on success, 0 on timeout.
    /// Does NOT require GIL — safe to call from within py.allow_threads().
    pub fn blocking_acquire(&self, path: &str, mode: LockMode, timeout_ms: u64) -> u64 {
        let norm_path = normalize_path(path);
        let start = Instant::now();

        // Fast path: non-blocking try under mutex.
        {
            let mut state = self.state.lock();
            if let Some(handle) =
                Self::try_acquire_io_locked(&mut state, &self.next_handle, &norm_path, mode)
            {
                let elapsed = start.elapsed().as_nanos() as u64;
                self.total_acquire_ns.fetch_add(elapsed, Ordering::Relaxed);
                self.acquire_count.fetch_add(1, Ordering::Relaxed);
                return handle;
            }
        }

        // If timeout == 0 (try-acquire), return immediately.
        if timeout_ms == 0 {
            self.contention_count.fetch_add(1, Ordering::Relaxed);
            self.timeout_count.fetch_add(1, Ordering::Relaxed);
            return 0;
        }

        // Blocking wait with Condvar — woken on every release().
        let deadline = start + Duration::from_millis(timeout_ms);

        loop {
            self.contention_count.fetch_add(1, Ordering::Relaxed);

            let mut state = self.state.lock();
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.timeout_count.fetch_add(1, Ordering::Relaxed);
                return 0;
            }

            let wait_result = self.notify.wait_for(&mut state, remaining);

            if let Some(handle) =
                Self::try_acquire_io_locked(&mut state, &self.next_handle, &norm_path, mode)
            {
                let elapsed = start.elapsed().as_nanos() as u64;
                self.total_acquire_ns.fetch_add(elapsed, Ordering::Relaxed);
                self.acquire_count.fetch_add(1, Ordering::Relaxed);
                return handle;
            }

            if wait_result.timed_out() {
                self.timeout_count.fetch_add(1, Ordering::Relaxed);
                return 0;
            }
        }
    }

    /// Release a previously acquired I/O lock by handle.
    pub fn do_release(&self, handle: u64) -> bool {
        let released = {
            let mut state = self.state.lock();

            let info = match state.handles.remove(&handle) {
                Some(info) => info,
                None => return false,
            };

            if let Some(entry) = state.locks.get_mut(&info.path) {
                match info.mode {
                    LockMode::Read => {
                        entry.io_readers = entry.io_readers.saturating_sub(1);
                    }
                    LockMode::Write => {
                        if entry.io_writer == Some(handle) {
                            entry.io_writer = None;
                        }
                    }
                }

                if entry.is_idle() {
                    state.locks.remove(&info.path);
                }
            }

            true
        };

        if released {
            self.notify.notify_all();
            self.release_count.fetch_add(1, Ordering::Relaxed);
        }

        released
    }

    // ── I/O lock: conflict detection ────────────────────────────────

    /// Check whether `path` in I/O `mode` conflicts with any *ancestor* I/O locks.
    fn ancestor_io_conflict(
        locks: &BTreeMap<String, LockEntry>,
        path: &str,
        mode: LockMode,
    ) -> bool {
        for anc in ancestors(path) {
            if let Some(entry) = locks.get(anc) {
                match mode {
                    LockMode::Read => {
                        if entry.io_writer.is_some() {
                            return true;
                        }
                    }
                    LockMode::Write => {
                        if entry.io_writer.is_some() || entry.io_readers > 0 {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }

    /// Check whether any *descendant* path has a conflicting I/O lock.
    fn descendant_io_conflict(
        locks: &BTreeMap<String, LockEntry>,
        path: &str,
        mode: LockMode,
    ) -> bool {
        let prefix = if path.ends_with('/') {
            path.to_string()
        } else {
            format!("{}/", path)
        };

        let mut upper = prefix.clone();
        upper.pop();
        upper.push('0'); // '0' > '/' in ASCII

        for (_key, entry) in locks.range(prefix..upper) {
            match mode {
                LockMode::Read => {
                    if entry.io_writer.is_some() {
                        return true;
                    }
                }
                LockMode::Write => {
                    if entry.io_writer.is_some() || entry.io_readers > 0 {
                        return true;
                    }
                }
            }
        }
        false
    }

    /// Attempt a single non-blocking I/O acquire under the lock.
    fn try_acquire_io_locked(
        state: &mut LockState,
        next_handle: &AtomicU64,
        path: &str,
        mode: LockMode,
    ) -> Option<u64> {
        if Self::ancestor_io_conflict(&state.locks, path, mode) {
            return None;
        }
        if Self::descendant_io_conflict(&state.locks, path, mode) {
            return None;
        }

        let entry = state
            .locks
            .entry(path.to_string())
            .or_insert_with(LockEntry::new);

        match mode {
            LockMode::Read => {
                if entry.io_writer.is_some() {
                    return None;
                }
                let handle = next_handle.fetch_add(1, Ordering::Relaxed) + 1;
                entry.io_readers += 1;
                state.handles.insert(
                    handle,
                    HandleInfo {
                        path: path.to_string(),
                        mode,
                    },
                );
                Some(handle)
            }
            LockMode::Write => {
                if entry.io_writer.is_some() || entry.io_readers > 0 {
                    return None;
                }
                let handle = next_handle.fetch_add(1, Ordering::Relaxed) + 1;
                entry.io_writer = Some(handle);
                state.handles.insert(
                    handle,
                    HandleInfo {
                        path: path.to_string(),
                        mode,
                    },
                );
                Some(handle)
            }
        }
    }

    // ── I/O lock: query helpers (PyO3 VFSLockManager) ───────────────

    /// Check whether `path` currently has any active I/O lock.
    pub fn is_locked(&self, path: &str) -> bool {
        let norm = normalize_path(path);
        let state = self.state.lock();
        state.locks.get(&norm).is_some_and(|entry| !entry.io_idle())
    }

    /// Return I/O lock-holder information for `path`: (readers, writer_handle).
    /// Returns `None` if unlocked.
    pub fn io_holders(&self, path: &str) -> Option<(u32, u64)> {
        let norm = normalize_path(path);
        let state = self.state.lock();
        match state.locks.get(&norm) {
            Some(entry) if !entry.io_idle() => {
                Some((entry.io_readers, entry.io_writer.unwrap_or(0)))
            }
            _ => None,
        }
    }

    /// Number of actively locked paths (I/O locks only — for VFSLockManager.active_locks).
    pub fn io_active_locks(&self) -> usize {
        let state = self.state.lock();
        state.locks.values().filter(|e| !e.io_idle()).count()
    }

    /// Number of active I/O handles.
    pub fn io_active_handles(&self) -> usize {
        self.state.lock().handles.len()
    }

    /// Metrics accessors.
    pub fn acquire_count(&self) -> u64 {
        self.acquire_count.load(Ordering::Relaxed)
    }
    pub fn release_count(&self) -> u64 {
        self.release_count.load(Ordering::Relaxed)
    }
    pub fn contention_count(&self) -> u64 {
        self.contention_count.load(Ordering::Relaxed)
    }
    pub fn timeout_count(&self) -> u64 {
        self.timeout_count.load(Ordering::Relaxed)
    }
    pub fn total_acquire_ns(&self) -> u64 {
        self.total_acquire_ns.load(Ordering::Relaxed)
    }

    // ── Advisory lock: hierarchy conflict detection ─────────────────

    /// Check whether any *ancestor* has an advisory lock that conflicts with `mode`.
    fn ancestor_advisory_conflict(
        locks: &BTreeMap<String, LockEntry>,
        path: &str,
        mode: KernelLockMode,
    ) -> bool {
        for anc in ancestors(path) {
            if let Some(entry) = locks.get(anc) {
                if !entry.holders.is_empty() {
                    match mode {
                        KernelLockMode::Exclusive => return true,
                        KernelLockMode::Shared => {
                            if entry
                                .holders
                                .iter()
                                .any(|h| h.mode == KernelLockMode::Exclusive)
                            {
                                return true;
                            }
                        }
                    }
                }
            }
        }
        false
    }

    /// Check whether any *descendant* has a conflicting advisory lock.
    fn descendant_advisory_conflict(
        locks: &BTreeMap<String, LockEntry>,
        path: &str,
        mode: KernelLockMode,
    ) -> bool {
        let prefix = if path.ends_with('/') {
            path.to_string()
        } else {
            format!("{}/", path)
        };

        let mut upper = prefix.clone();
        upper.pop();
        upper.push('0');

        for (_key, entry) in locks.range(prefix..upper) {
            if !entry.holders.is_empty() {
                match mode {
                    KernelLockMode::Exclusive => return true,
                    KernelLockMode::Shared => {
                        if entry
                            .holders
                            .iter()
                            .any(|h| h.mode == KernelLockMode::Exclusive)
                        {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }

    // ── Advisory lock: public API ───────────────────────────────────

    /// Try to acquire an advisory lock. Returns `Ok(true)` when the caller
    /// became (or already was) a holder, `Ok(false)` on conflict.
    ///
    /// When Raft backend is active, the write goes through `propose()`.
    /// I/O locks always stay local regardless.
    pub fn acquire_lock(
        &self,
        path: &str,
        lock_id: &str,
        mode: KernelLockMode,
        max_holders: u32,
        ttl_secs: u64,
        holder_info: &str,
    ) -> Result<bool, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            // Distributed path: propose through Raft
            let cmd = Command::AcquireLock {
                path: path.to_string(),
                lock_id: lock_id.to_string(),
                max_holders,
                ttl_secs: ttl_secs.min(u32::MAX as u64) as u32,
                holder_info: holder_info.to_string(),
                mode: kernel_mode_to_raft(mode),
                now_secs: FullStateMachine::now(),
            };
            let result = runtime.block_on(node.propose(cmd)).map_err(|e| {
                LockError::IOError(format!("LockManager.acquire_lock({path}): {e}"))
            })?;
            match result {
                CommandResult::LockResult(state) => Ok(state.acquired),
                CommandResult::Error(e) => Err(LockError::IOError(format!(
                    "LockManager.acquire_lock({path}) rejected: {e}"
                ))),
                _ => Err(LockError::IOError(
                    "LockManager.acquire_lock: unexpected result type".into(),
                )),
            }
        } else {
            // Local path: in-process BTreeMap
            drop(raft_guard);
            self.acquire_lock_local(path, lock_id, mode, max_holders, ttl_secs, holder_info)
        }
    }

    /// Local advisory lock acquire (standalone mode).
    fn acquire_lock_local(
        &self,
        path: &str,
        lock_id: &str,
        mode: KernelLockMode,
        max_holders: u32,
        ttl_secs: u64,
        holder_info: &str,
    ) -> Result<bool, LockError> {
        let now = lock_now_secs();
        let expires_at = now.saturating_add(ttl_secs);
        let mut state = self.state.lock();

        // Hierarchy conflict check (advisory)
        if Self::ancestor_advisory_conflict(&state.locks, path, mode) {
            return Ok(false);
        }
        if Self::descendant_advisory_conflict(&state.locks, path, mode) {
            return Ok(false);
        }

        let entry = state
            .locks
            .entry(path.to_string())
            .or_insert_with(LockEntry::new);

        // Prune expired holders
        prune_expired_holders(&mut entry.holders, now);

        // Idempotent re-acquire: same lock_id already present
        if let Some(h) = entry.holders.iter_mut().find(|h| h.lock_id == lock_id) {
            h.expires_at_secs = expires_at;
            return Ok(true);
        }

        // First holder sets max_holders; subsequent holders must match
        if entry.holders.is_empty() {
            entry.max_holders = max_holders;
        } else if entry.max_holders != max_holders {
            return Ok(false);
        }

        if accepts_new_holder(&entry.holders, entry.max_holders, mode) {
            entry.holders.push(KernelHolderInfo {
                lock_id: lock_id.to_string(),
                holder_info: holder_info.to_string(),
                mode,
                acquired_at_secs: now,
                expires_at_secs: expires_at,
            });
            Ok(true)
        } else {
            Ok(false)
        }
    }

    /// Release a specific advisory lock holder. Returns `Ok(true)` if found.
    pub fn release_lock(&self, path: &str, lock_id: &str) -> Result<bool, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            let cmd = Command::ReleaseLock {
                path: path.to_string(),
                lock_id: lock_id.to_string(),
            };
            let result = runtime.block_on(node.propose(cmd)).map_err(|e| {
                LockError::IOError(format!("LockManager.release_lock({path}): {e}"))
            })?;
            Ok(matches!(result, CommandResult::Success))
        } else {
            drop(raft_guard);
            self.release_lock_local(path, lock_id)
        }
    }

    fn release_lock_local(&self, path: &str, lock_id: &str) -> Result<bool, LockError> {
        let mut state = self.state.lock();
        if let Some(entry) = state.locks.get_mut(path) {
            let before = entry.holders.len();
            entry.holders.retain(|h| h.lock_id != lock_id);
            let removed = entry.holders.len() < before;
            if entry.is_idle() {
                state.locks.remove(path);
            }
            Ok(removed)
        } else {
            Ok(false)
        }
    }

    /// Force-release ALL advisory holders on `path` (admin override).
    pub fn force_release_lock(&self, path: &str) -> Result<bool, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            let cmd = Command::ForceReleaseLock {
                path: path.to_string(),
            };
            let result = runtime.block_on(node.propose(cmd)).map_err(|e| {
                LockError::IOError(format!("LockManager.force_release_lock({path}): {e}"))
            })?;
            Ok(matches!(result, CommandResult::Success))
        } else {
            drop(raft_guard);
            let mut state = self.state.lock();
            if let Some(entry) = state.locks.get_mut(path) {
                let had_holders = !entry.holders.is_empty();
                entry.holders.clear();
                entry.max_holders = 0;
                if entry.is_idle() {
                    state.locks.remove(path);
                }
                Ok(had_holders)
            } else {
                Ok(false)
            }
        }
    }

    /// Extend a holder's TTL. Returns `Ok(true)` if extended.
    pub fn extend_lock(&self, path: &str, lock_id: &str, ttl_secs: u64) -> Result<bool, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            let cmd = Command::ExtendLock {
                path: path.to_string(),
                lock_id: lock_id.to_string(),
                new_ttl_secs: ttl_secs.min(u32::MAX as u64) as u32,
                now_secs: FullStateMachine::now(),
            };
            let result = runtime
                .block_on(node.propose(cmd))
                .map_err(|e| LockError::IOError(format!("LockManager.extend_lock({path}): {e}")))?;
            Ok(matches!(result, CommandResult::Success))
        } else {
            drop(raft_guard);
            let now = lock_now_secs();
            let new_expires = now.saturating_add(ttl_secs);
            let mut state = self.state.lock();
            if let Some(entry) = state.locks.get_mut(path) {
                prune_expired_holders(&mut entry.holders, now);
                if let Some(h) = entry.holders.iter_mut().find(|h| h.lock_id == lock_id) {
                    h.expires_at_secs = new_expires;
                    Ok(true)
                } else {
                    Ok(false)
                }
            } else {
                Ok(false)
            }
        }
    }

    /// Read the full advisory lock record for a path (or `None` if unlocked).
    pub fn get_lock_info(&self, path: &str) -> Result<Option<KernelLockInfo>, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            let key = path.to_string();
            let fut = node.read_linearizable(move |sm: &FullStateMachine| sm.get_lock(&key));
            let lock_opt = runtime
                .block_on(fut)
                .map_err(|e| {
                    LockError::IOError(format!("LockManager.get_lock_info({path}) read_index: {e}"))
                })?
                .map_err(|e| {
                    LockError::IOError(format!("LockManager.get_lock_info({path}): {e:?}"))
                })?;
            Ok(lock_opt.map(raft_lock_to_kernel))
        } else {
            drop(raft_guard);
            let state = self.state.lock();
            Ok(state.locks.get(path).and_then(|entry| {
                if entry.holders.is_empty() {
                    None
                } else {
                    Some(KernelLockInfo {
                        path: path.to_string(),
                        max_holders: entry.max_holders,
                        holders: entry.holders.clone(),
                    })
                }
            }))
        }
    }

    /// Enumerate advisory locks with a given path prefix, capped at `limit`.
    pub fn list_locks(&self, prefix: &str, limit: usize) -> Result<Vec<KernelLockInfo>, LockError> {
        let raft_guard = self.raft.lock();
        if let Some((ref node, ref runtime)) = *raft_guard {
            let key = prefix.to_string();
            let fut =
                node.read_linearizable(move |sm: &FullStateMachine| sm.list_locks(&key, limit));
            let locks = runtime
                .block_on(fut)
                .map_err(|e| {
                    LockError::IOError(format!("LockManager.list_locks({prefix}) read_index: {e}"))
                })?
                .map_err(|e| {
                    LockError::IOError(format!("LockManager.list_locks({prefix}): {e:?}"))
                })?;
            Ok(locks.into_iter().map(raft_lock_to_kernel).collect())
        } else {
            drop(raft_guard);
            let state = self.state.lock();
            let mut out = Vec::new();
            for (key, entry) in state.locks.iter() {
                if out.len() >= limit {
                    break;
                }
                if key.starts_with(prefix) && !entry.holders.is_empty() {
                    out.push(KernelLockInfo {
                        path: key.clone(),
                        max_holders: entry.max_holders,
                        holders: entry.holders.clone(),
                    });
                }
            }
            Ok(out)
        }
    }
}

impl Default for LockManager {
    fn default() -> Self {
        Self::new()
    }
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── I/O lock tests (migrated from lock.rs) ──────────────────────

    fn io_acquire(lm: &LockManager, path: &str, mode: LockMode) -> Option<u64> {
        let handle = lm.blocking_acquire(path, mode, 0);
        if handle > 0 {
            Some(handle)
        } else {
            None
        }
    }

    // -- path normalization ------------------------------------------------

    #[test]
    fn test_normalize_trailing_slash() {
        assert_eq!(normalize_path("/a/b/"), "/a/b");
    }

    #[test]
    fn test_normalize_double_slash() {
        assert_eq!(normalize_path("/a//b"), "/a/b");
    }

    #[test]
    fn test_normalize_root() {
        assert_eq!(normalize_path("/"), "/");
    }

    #[test]
    fn test_normalize_empty() {
        assert_eq!(normalize_path(""), "/");
    }

    // -- ancestors helper --------------------------------------------------

    #[test]
    fn test_ancestors_root() {
        assert_eq!(ancestors("/"), Vec::<&str>::new());
    }

    #[test]
    fn test_ancestors_one_level() {
        assert_eq!(ancestors("/a"), vec!["/"]);
    }

    #[test]
    fn test_ancestors_deep() {
        assert_eq!(ancestors("/a/b/c"), vec!["/a/b", "/a", "/"]);
    }

    // -- basic I/O acquire / release -------------------------------------------

    #[test]
    fn test_basic_read_acquire_release() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/foo", LockMode::Read).unwrap();
        assert!(h > 0);
        assert!(lm.is_locked("/foo"));
        assert!(lm.do_release(h));
        assert!(!lm.is_locked("/foo"));
    }

    #[test]
    fn test_basic_write_acquire_release() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/foo", LockMode::Write).unwrap();
        assert!(h > 0);
        assert!(lm.is_locked("/foo"));
        assert!(lm.do_release(h));
        assert!(!lm.is_locked("/foo"));
    }

    // -- read-read coexistence ─────────────────────────────────────────

    #[test]
    fn test_read_read_coexist() {
        let lm = LockManager::new();
        let h1 = io_acquire(&lm, "/foo", LockMode::Read).unwrap();
        let h2 = io_acquire(&lm, "/foo", LockMode::Read).unwrap();
        assert!(h1 != h2);
        assert!(lm.is_locked("/foo"));
        lm.do_release(h1);
        assert!(lm.is_locked("/foo"));
        lm.do_release(h2);
        assert!(!lm.is_locked("/foo"));
    }

    // -- read-write conflict ──────────────────────────────────────────

    #[test]
    fn test_write_blocks_read() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/foo", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/foo", LockMode::Read).is_none());
    }

    #[test]
    fn test_read_blocks_write() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/foo", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/foo", LockMode::Write).is_none());
    }

    // -- write-write conflict ─────────────────────────────────────────

    #[test]
    fn test_write_write_conflict() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/foo", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/foo", LockMode::Write).is_none());
    }

    // -- ancestor I/O conflict ────────────────────────────────────────

    #[test]
    fn test_ancestor_write_blocks_child_read() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Read).is_none());
    }

    #[test]
    fn test_ancestor_write_blocks_child_write() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b/c", LockMode::Write).is_none());
    }

    #[test]
    fn test_ancestor_read_allows_child_read() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/a", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Read).is_some());
    }

    #[test]
    fn test_ancestor_read_blocks_child_write() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/a", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_none());
    }

    // -- descendant I/O conflict ──────────────────────────────────────

    #[test]
    fn test_descendant_write_blocks_parent_write() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/b/c", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_read_blocks_parent_write() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/a/b", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/a", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_write_blocks_parent_read() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/b", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a", LockMode::Read).is_none());
    }

    // -- root path edge cases ─────────────────────────────────────────

    #[test]
    fn test_root_write_blocks_all_descendants() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a", LockMode::Read).is_none());
        assert!(io_acquire(&lm, "/a/b/c", LockMode::Write).is_none());
    }

    #[test]
    fn test_descendant_blocks_root_write() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/a", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/", LockMode::Write).is_none());
    }

    // -- path normalization in I/O locking ────────────────────────────

    #[test]
    fn test_trailing_slash_same_as_without() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/a/b/", LockMode::Write).unwrap();
        assert!(lm.is_locked("/a/b"));
        lm.do_release(h);
    }

    #[test]
    fn test_double_slash_same_as_single() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/a//b", LockMode::Write).unwrap();
        assert!(lm.is_locked("/a/b"));
        lm.do_release(h);
    }

    // -- release wrong handle ─────────────────────────────────────────

    #[test]
    fn test_release_wrong_handle() {
        let lm = LockManager::new();
        assert!(!lm.do_release(999));
    }

    // -- stats accuracy ───────────────────────────────────────────────

    #[test]
    fn test_stats_counters() {
        let lm = LockManager::new();
        let h1 = io_acquire(&lm, "/x", LockMode::Read).unwrap();
        let h2 = io_acquire(&lm, "/y", LockMode::Write).unwrap();
        lm.do_release(h1);
        lm.do_release(h2);
        assert_eq!(lm.release_count(), 2);
    }

    // -- unicode path ─────────────────────────────────────────────────

    #[test]
    fn test_unicode_path() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/data/file", LockMode::Write).unwrap();
        assert!(lm.is_locked("/data/file"));
        lm.do_release(h);
        assert!(!lm.is_locked("/data/file"));
    }

    // -- concurrent multi-thread test (rayon) ─────────────────────────

    #[test]
    fn test_concurrent_reads() {
        use rayon::prelude::*;

        let lm = LockManager::new();
        let handles: Vec<u64> = (0..100)
            .into_par_iter()
            .map(|_| io_acquire(&lm, "/shared", LockMode::Read).unwrap())
            .collect();

        assert_eq!(handles.len(), 100);
        let mut sorted = handles.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), 100);

        for h in handles {
            assert!(lm.do_release(h));
        }
        assert!(!lm.is_locked("/shared"));
    }

    #[test]
    fn test_concurrent_write_exclusion() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        let lm = LockManager::new();
        let success_count = AtomicU32::new(0);

        (0..100).into_par_iter().for_each(|_| {
            if io_acquire(&lm, "/exclusive", LockMode::Write).is_some() {
                success_count.fetch_add(1, Ordering::Relaxed);
            }
        });

        assert_eq!(success_count.load(Ordering::Relaxed), 1);
    }

    // -- TOCTOU regression test ───────────────────────────────────────

    #[test]
    fn test_no_toctou_parent_child_write() {
        use rayon::prelude::*;
        use std::sync::atomic::AtomicU32;

        let lm = LockManager::new();
        let success_count = AtomicU32::new(0);

        (0..1000).into_par_iter().for_each(|i| {
            let path = if i % 2 == 0 { "/a" } else { "/a/b" };
            if let Some(h) = io_acquire(&lm, path, LockMode::Write) {
                success_count.fetch_add(1, Ordering::Relaxed);
                std::thread::sleep(Duration::from_micros(1));
                lm.do_release(h);
            }
        });

        assert!(success_count.load(Ordering::Relaxed) > 0);
    }

    // -- cleanup: idle entries removed ────────────────────────────────

    #[test]
    fn test_idle_entry_cleaned_up() {
        let lm = LockManager::new();
        let h = io_acquire(&lm, "/temp", LockMode::Read).unwrap();
        assert_eq!(lm.io_active_locks(), 1);
        lm.do_release(h);
        assert_eq!(lm.io_active_locks(), 0);
    }

    // -- BTreeMap range boundary tests (Issue #2941) ──────────────────

    #[test]
    fn test_sibling_path_no_conflict() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/bc", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_path_with_dash_no_conflict() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/b-special", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_path_with_dot_no_conflict() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/b.txt", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_true_descendant_still_conflicts() {
        let lm = LockManager::new();
        let _w = io_acquire(&lm, "/a/b/c", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_none());
    }

    #[test]
    fn test_deep_descendant_conflicts() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/a/b/c/d/e/f", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_none());
    }

    #[test]
    fn test_root_descendant_range() {
        let lm = LockManager::new();
        let _r = io_acquire(&lm, "/x/y/z", LockMode::Read).unwrap();
        assert!(io_acquire(&lm, "/", LockMode::Write).is_none());
    }

    #[test]
    fn test_many_siblings_only_descendant_conflicts() {
        let lm = LockManager::new();
        let _w1 = io_acquire(&lm, "/a/ba", LockMode::Write).unwrap();
        let _w2 = io_acquire(&lm, "/a/bb", LockMode::Write).unwrap();
        let _w3 = io_acquire(&lm, "/a/b-x", LockMode::Write).unwrap();
        let _w4 = io_acquire(&lm, "/a/b.y", LockMode::Write).unwrap();
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_some());
    }

    #[test]
    fn test_sibling_and_descendant_mixed() {
        let lm = LockManager::new();
        let _w1 = io_acquire(&lm, "/a/bc", LockMode::Write).unwrap(); // sibling
        let _w2 = io_acquire(&lm, "/a/b/child", LockMode::Write).unwrap(); // descendant
        assert!(io_acquire(&lm, "/a/b", LockMode::Write).is_none());
    }

    // ── Advisory lock tests (migrated from old LocalLockManager) ────

    #[test]
    fn advisory_exclusive_blocks_exclusive() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/lk/a", "h1", KernelLockMode::Exclusive, 1, 60, "agent:1")
            .unwrap());
        assert!(!lm
            .acquire_lock("/lk/a", "h2", KernelLockMode::Exclusive, 1, 60, "agent:2")
            .unwrap());
    }

    #[test]
    fn advisory_shared_coexists_up_to_max() {
        let lm = LockManager::new();
        for id in ["r1", "r2", "r3"] {
            assert!(lm
                .acquire_lock("/lk/b", id, KernelLockMode::Shared, 3, 60, "agent")
                .unwrap());
        }
        assert!(!lm
            .acquire_lock("/lk/b", "r4", KernelLockMode::Shared, 3, 60, "agent")
            .unwrap());
    }

    #[test]
    fn advisory_shared_blocked_by_exclusive() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/lk/c", "w1", KernelLockMode::Exclusive, 3, 60, "agent:w")
            .unwrap());
        assert!(!lm
            .acquire_lock("/lk/c", "r1", KernelLockMode::Shared, 3, 60, "agent:r")
            .unwrap());
    }

    #[test]
    fn advisory_exclusive_blocked_by_shared() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/lk/d", "r1", KernelLockMode::Shared, 3, 60, "agent:r")
            .unwrap());
        assert!(!lm
            .acquire_lock("/lk/d", "w1", KernelLockMode::Exclusive, 3, 60, "agent:w")
            .unwrap());
    }

    #[test]
    fn advisory_idempotent_reacquire_and_release() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/lk/e", "h1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap());
        assert!(lm
            .acquire_lock("/lk/e", "h1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap());
        let info = lm.get_lock_info("/lk/e").unwrap().unwrap();
        assert_eq!(info.holders.len(), 1);

        assert!(lm.release_lock("/lk/e", "h1").unwrap());
        assert!(lm.get_lock_info("/lk/e").unwrap().is_none());
    }

    #[test]
    fn advisory_list_filters_by_prefix() {
        let lm = LockManager::new();
        lm.acquire_lock("/lk/ns/a", "h1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap();
        lm.acquire_lock("/lk/ns/b", "h2", KernelLockMode::Shared, 2, 60, "agent")
            .unwrap();
        lm.acquire_lock("/lk/other", "h3", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap();

        let under_ns = lm.list_locks("/lk/ns/", 10).unwrap();
        assert_eq!(under_ns.len(), 2);

        let all_lk = lm.list_locks("/lk/", 10).unwrap();
        assert_eq!(all_lk.len(), 3);
    }

    #[test]
    fn advisory_extend_refreshes_ttl() {
        let lm = LockManager::new();
        lm.acquire_lock("/lk/x", "h1", KernelLockMode::Exclusive, 1, 1, "agent")
            .unwrap();
        let before = lm.get_lock_info("/lk/x").unwrap().unwrap().holders[0].expires_at_secs;
        assert!(lm.extend_lock("/lk/x", "h1", 3600).unwrap());
        let after = lm.get_lock_info("/lk/x").unwrap().unwrap().holders[0].expires_at_secs;
        assert!(after >= before);
    }

    #[test]
    fn advisory_capacity_mismatch_rejects() {
        let lm = LockManager::new();
        lm.acquire_lock("/lk/y", "r1", KernelLockMode::Shared, 3, 60, "agent")
            .unwrap();
        assert!(!lm
            .acquire_lock("/lk/y", "w1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap());
    }

    #[test]
    fn advisory_force_release() {
        let lm = LockManager::new();
        lm.acquire_lock("/lk/f", "h1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap();
        assert!(lm.force_release_lock("/lk/f").unwrap());
        assert!(lm.get_lock_info("/lk/f").unwrap().is_none());
    }

    // ── Advisory hierarchy tests (NEW — not in old LocalLockManager) ─

    #[test]
    fn advisory_hierarchy_parent_exclusive_blocks_child() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/folder", "h1", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap());
        // Locking /folder should block /folder/file
        assert!(!lm
            .acquire_lock(
                "/folder/file",
                "h2",
                KernelLockMode::Exclusive,
                1,
                60,
                "agent"
            )
            .unwrap());
        assert!(!lm
            .acquire_lock("/folder/file", "h3", KernelLockMode::Shared, 2, 60, "agent")
            .unwrap());
    }

    #[test]
    fn advisory_hierarchy_child_blocks_parent_exclusive() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock(
                "/folder/file",
                "h1",
                KernelLockMode::Exclusive,
                1,
                60,
                "agent"
            )
            .unwrap());
        assert!(!lm
            .acquire_lock("/folder", "h2", KernelLockMode::Exclusive, 1, 60, "agent")
            .unwrap());
    }

    #[test]
    fn advisory_hierarchy_shared_parent_allows_shared_child() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/folder", "h1", KernelLockMode::Shared, 5, 60, "agent")
            .unwrap());
        // Shared parent should allow shared child
        assert!(lm
            .acquire_lock("/folder/file", "h2", KernelLockMode::Shared, 5, 60, "agent")
            .unwrap());
    }

    #[test]
    fn advisory_hierarchy_shared_parent_blocks_exclusive_child() {
        let lm = LockManager::new();
        assert!(lm
            .acquire_lock("/folder", "h1", KernelLockMode::Shared, 5, 60, "agent")
            .unwrap());
        // Shared parent should block exclusive child
        assert!(!lm
            .acquire_lock(
                "/folder/file",
                "h2",
                KernelLockMode::Exclusive,
                1,
                60,
                "agent"
            )
            .unwrap());
    }

    // ── I/O + advisory orthogonality test ────────────────────────────

    #[test]
    fn io_and_advisory_do_not_conflict() {
        let lm = LockManager::new();
        // I/O write lock on /data/file
        let h = io_acquire(&lm, "/data/file", LockMode::Write).unwrap();
        // Advisory exclusive lock on same path should succeed (orthogonal)
        assert!(lm
            .acquire_lock(
                "/data/file",
                "adv1",
                KernelLockMode::Exclusive,
                1,
                60,
                "agent"
            )
            .unwrap());
        // Release I/O lock
        lm.do_release(h);
        // Advisory still held
        assert!(lm.get_lock_info("/data/file").unwrap().is_some());
        // Release advisory
        assert!(lm.release_lock("/data/file", "adv1").unwrap());
    }
}
