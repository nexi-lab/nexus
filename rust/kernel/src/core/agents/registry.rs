#![allow(dead_code)]
//! AgentRegistry — Rust SSOT for agent lifecycle state.
//!
//! Linux task_struct array analogue. Lifecycle state, PCB metadata, parent/
//! child links, signal semantics, and condvar wake-ups all live here. The
//! Python service layer is a thin pass-through; in-process callers reach the
//! registry directly through ``PyKernel.agent_registry``.
//!
//! AgentState mirrors `contracts/process_types.py` exactly:
//!   REGISTERED → WARMING_UP → READY ↔ BUSY → TERMINATED
//!   READY/BUSY → SUSPENDED → READY

use dashmap::DashMap;
use parking_lot::{Condvar, Mutex, RwLock};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use uuid::Uuid;

// ── ExternalProcessInfo ────────────────────────────────────────────────

/// Connection metadata for external (gRPC/MCP) processes.
#[derive(Clone, Debug, Default)]
pub struct ExternalProcessInfo {
    pub connection_id: String,
    pub host_pid: Option<i64>,
    pub remote_addr: Option<String>,
    pub protocol: String, // "grpc" | "mcp" | "stdio"
    pub last_heartbeat_ms: Option<u64>,
}

// ── RepoMount ──────────────────────────────────────────────────────────

/// One workspace repo mount carried in the per-pid descriptor.
/// Drives the per-alias DT_LINK rows stamped under
/// `/proc/{pid}/workspace/{alias}` at start_session time
/// (services::managed_agent::proc_entry::register_proc_entry).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RepoMount {
    pub alias: String,
    pub mount_path: String,
}

// ── AgentDescriptor ────────────────────────────────────────────────────

/// Agent process descriptor — analogous to Linux task_struct.
#[derive(Clone, Debug)]
pub struct AgentDescriptor {
    // Identity
    pub pid: String,
    pub name: String,
    pub kind: AgentKind,
    pub owner_id: String,
    pub zone_id: String,
    pub parent_pid: Option<String>,

    // Lifecycle
    pub state: AgentState,
    pub exit_code: Option<i32>,
    pub generation: u32,

    // Filesystem
    pub cwd: String,
    pub root: String,

    // Sub-processes
    pub children: Vec<String>,

    // Timestamps (epoch ms)
    pub created_at_ms: u64,
    pub updated_at_ms: u64,

    // Heartbeat (epoch ms) — UNMANAGED agents only
    pub last_heartbeat_ms: Option<u64>,

    // Connection metadata for UNMANAGED agents
    pub connection_id: Option<String>,
    pub external_info: Option<ExternalProcessInfo>,

    // Opaque labels — Kubernetes-style.
    pub labels: HashMap<String, String>,

    // Workspace repo mounts — one entry per `/proc/{pid}/workspace/{alias}`
    // DT_LINK row stamped at start_session time. Useful PCB metadata for
    // inspection; the metastore DT_LINK rows are the routing SSOT.
    pub repos: Vec<RepoMount>,
}

impl Default for AgentDescriptor {
    fn default() -> Self {
        Self {
            pid: String::new(),
            name: String::new(),
            kind: AgentKind::Managed,
            owner_id: String::new(),
            zone_id: String::new(),
            parent_pid: None,
            state: AgentState::Registered,
            exit_code: None,
            generation: 1,
            cwd: "/".to_string(),
            root: "/".to_string(),
            children: Vec::new(),
            created_at_ms: 0,
            updated_at_ms: 0,
            last_heartbeat_ms: None,
            connection_id: None,
            external_info: None,
            labels: HashMap::new(),
            repos: Vec::new(),
        }
    }
}

/// Agent process kind.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AgentKind {
    Worker,
    Daemon,
    Unmanaged,
    Managed,
}

/// Agent process state — mirrors contracts/process_types.py AgentState (SSOT).
///
/// Lifecycle:
///   REGISTERED → WARMING_UP → READY ↔ BUSY → TERMINATED
///   READY/BUSY → SUSPENDED → READY
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AgentState {
    Registered,
    WarmingUp,
    Ready,
    Busy,
    Suspended,
    Terminated,
}

impl AgentState {
    pub fn as_str(&self) -> &'static str {
        match self {
            AgentState::Registered => "REGISTERED",
            AgentState::WarmingUp => "WARMING_UP",
            AgentState::Ready => "READY",
            AgentState::Busy => "BUSY",
            AgentState::Suspended => "SUSPENDED",
            AgentState::Terminated => "TERMINATED",
        }
    }

    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "REGISTERED" | "registered" => Some(AgentState::Registered),
            "WARMING_UP" | "warming_up" => Some(AgentState::WarmingUp),
            "READY" | "ready" => Some(AgentState::Ready),
            "BUSY" | "busy" => Some(AgentState::Busy),
            "SUSPENDED" | "suspended" => Some(AgentState::Suspended),
            "TERMINATED" | "terminated" => Some(AgentState::Terminated),
            _ => None,
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, AgentState::Terminated)
    }

    /// Return true if the transition `self -> next` is permitted.
    /// Mirrors VALID_AGENT_TRANSITIONS in contracts/process_types.py.
    pub fn can_transition_to(&self, next: AgentState) -> bool {
        match self {
            AgentState::Registered => {
                matches!(next, AgentState::WarmingUp | AgentState::Terminated)
            }
            AgentState::WarmingUp => {
                matches!(next, AgentState::Ready | AgentState::Terminated)
            }
            AgentState::Ready => matches!(
                next,
                AgentState::Busy | AgentState::Suspended | AgentState::Terminated
            ),
            AgentState::Busy => matches!(
                next,
                AgentState::Ready | AgentState::Suspended | AgentState::Terminated
            ),
            AgentState::Suspended => {
                matches!(next, AgentState::Ready | AgentState::Terminated)
            }
            AgentState::Terminated => false,
        }
    }
}

impl AgentKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            AgentKind::Worker => "WORKER",
            AgentKind::Daemon => "DAEMON",
            AgentKind::Unmanaged => "UNMANAGED",
            AgentKind::Managed => "MANAGED",
        }
    }

    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "WORKER" | "worker" => Some(AgentKind::Worker),
            "DAEMON" | "daemon" => Some(AgentKind::Daemon),
            "UNMANAGED" | "unmanaged" => Some(AgentKind::Unmanaged),
            "MANAGED" | "managed" => Some(AgentKind::Managed),
            _ => None,
        }
    }
}

/// POSIX-like agent signals. Mirrors contracts/process_types.py AgentSignal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AgentSignal {
    /// Graceful shutdown → TERMINATED.
    Sigterm,
    /// Suspend → SUSPENDED.
    Sigstop,
    /// Resume/connect → READY (bumps generation).
    Sigcont,
    /// Immediate kill + reap (exit_code=-9).
    Sigkill,
    /// User-defined steering (label merge — no state change).
    Sigusr1,
}

impl AgentSignal {
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "SIGTERM" => Some(AgentSignal::Sigterm),
            "SIGSTOP" => Some(AgentSignal::Sigstop),
            "SIGCONT" => Some(AgentSignal::Sigcont),
            "SIGKILL" => Some(AgentSignal::Sigkill),
            "SIGUSR1" => Some(AgentSignal::Sigusr1),
            _ => None,
        }
    }
}

// ── Errors ──────────────────────────────────────────────────────────────

/// Failure modes for AgentRegistry mutations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentError {
    NotFound(String),
    AlreadyExists(String),
    InvalidTransition {
        pid: String,
        from: AgentState,
        to: AgentState,
    },
    /// Operation not valid for this agent kind (e.g. heartbeat on a
    /// MANAGED agent). Carries a human-readable message.
    InvalidKind(String),
    /// PID allocation exhausted — the registry could not produce a new
    /// unique pid in a reasonable number of attempts.
    PidExhausted,
    /// Generic protocol violation (e.g. external_info missing on heartbeat).
    Protocol(String),
}

impl std::fmt::Display for AgentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AgentError::NotFound(pid) => write!(f, "process not found: {pid}"),
            AgentError::AlreadyExists(pid) => write!(f, "process already exists: {pid}"),
            AgentError::InvalidTransition { pid, from, to } => write!(
                f,
                "cannot transition {pid} from {} to {}",
                from.as_str(),
                to.as_str()
            ),
            AgentError::InvalidKind(msg) => write!(f, "invalid kind: {msg}"),
            AgentError::PidExhausted => write!(f, "failed to allocate a unique process id"),
            AgentError::Protocol(msg) => write!(f, "protocol error: {msg}"),
        }
    }
}

impl std::error::Error for AgentError {}

// ── Per-agent notification ──────────────────────────────────────────────

struct AgentNotify {
    mutex: Mutex<()>,
    state_changed: Condvar,
}

impl AgentNotify {
    fn new() -> Self {
        Self {
            mutex: Mutex::new(()),
            state_changed: Condvar::new(),
        }
    }
}

// ── AgentRegistry ─────────────────────────────────────────────────────────

/// Termination callback fired when an agent transitions to
/// `Terminated` or is reaped out-of-band.
///
/// The callback receives the pid as a string slice.  Callbacks run
/// inline on the thread that triggered the transition so they should
/// be cheap; callers needing async work should hand off to a task.
/// Panics propagate to the caller — keep callbacks panic-free.
pub type OnTerminateCallback = Arc<dyn Fn(&str) + Send + Sync>;

/// Rust SSOT for agent lifecycle state.
/// DashMap<pid, AgentDescriptor> for lock-free concurrent access; per-pid
/// condvar wakes blocking waiters on state transitions.
pub struct AgentRegistry {
    agents: DashMap<String, AgentDescriptor>,
    notify: DashMap<String, Arc<AgentNotify>>,
    /// Termination observers — fired on `Terminated` transitions and on
    /// `reap`.  Keyed by `id` so callers can deregister; the order in
    /// which they fire is the order they were registered.  Used by
    /// `ManagedAgentService` to reap `/proc/{pid}/workspace/` when an
    /// agent is killed out-of-band (e.g. SIGKILL or orphan auto-reap)
    /// without going through `cancel_session`.
    on_terminate: RwLock<Vec<(String, OnTerminateCallback)>>,
}

fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

impl AgentRegistry {
    pub fn new() -> Self {
        Self {
            agents: DashMap::new(),
            notify: DashMap::new(),
            on_terminate: RwLock::new(Vec::new()),
        }
    }

    /// Register a termination callback. Idempotent on `id`: a second
    /// call with the same `id` replaces the prior callback. Used by
    /// `ManagedAgentService` to chain workspace cleanup onto every
    /// agent termination, including SIGKILL and orphan auto-reap.
    pub fn register_on_terminate(&self, id: &str, callback: OnTerminateCallback) {
        let mut guard = self.on_terminate.write();
        if let Some(slot) = guard.iter_mut().find(|(k, _)| k == id) {
            slot.1 = callback;
        } else {
            guard.push((id.to_string(), callback));
        }
    }

    /// Drop a termination callback by `id`. Returns true if a callback
    /// was registered under that name.
    pub fn unregister_on_terminate(&self, id: &str) -> bool {
        let mut guard = self.on_terminate.write();
        let prev = guard.len();
        guard.retain(|(k, _)| k != id);
        guard.len() != prev
    }

    /// Fire every registered `on_terminate` callback for `pid`.
    /// Snapshots the callback list under a read lock and drops the lock
    /// before calling so callbacks can re-enter the registry without
    /// deadlocking.
    fn fire_on_terminate(&self, pid: &str) {
        let snapshot: Vec<OnTerminateCallback> = self
            .on_terminate
            .read()
            .iter()
            .map(|(_, cb)| Arc::clone(cb))
            .collect();
        for cb in snapshot {
            cb(pid);
        }
    }

    /// Allocate a unique pid (uuid4 hex prefix). Bounded retry loop.
    fn alloc_pid(&self) -> Result<String, AgentError> {
        for _ in 0..100 {
            let pid = Uuid::new_v4().simple().to_string();
            let pid = pid[..12].to_string();
            if !self.agents.contains_key(&pid) {
                return Ok(pid);
            }
        }
        Err(AgentError::PidExhausted)
    }

    fn wake(&self, pid: &str) {
        if let Some(notify) = self.notify.get(pid) {
            let _guard = notify.mutex.lock();
            notify.state_changed.notify_all();
        }
    }

    /// Register a pre-built descriptor. Returns true if inserted (pid was
    /// new). Used by Rust callers that already constructed a descriptor;
    /// most call sites should prefer ``spawn``.
    pub fn register(&self, desc: AgentDescriptor) -> bool {
        use dashmap::mapref::entry::Entry;
        match self.agents.entry(desc.pid.clone()) {
            Entry::Occupied(_) => false,
            Entry::Vacant(v) => {
                let pid = desc.pid.clone();
                v.insert(desc);
                self.notify
                    .entry(pid.clone())
                    .or_insert_with(|| Arc::new(AgentNotify::new()));
                true
            }
        }
    }

    /// Allocate a pid, register a fresh descriptor in REGISTERED state,
    /// and append to the parent's children list. Returns the new
    /// descriptor.
    ///
    /// `pid` may be supplied to force a specific id (used for external
    /// agents where the connection_id IS the pid). When `pid` is
    /// `Some(existing)` and already present, returns
    /// `AgentError::AlreadyExists`.
    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        &self,
        name: String,
        owner_id: String,
        zone_id: String,
        kind: AgentKind,
        parent_pid: Option<String>,
        pid: Option<String>,
        cwd: String,
        external_info: Option<ExternalProcessInfo>,
        labels: HashMap<String, String>,
    ) -> Result<AgentDescriptor, AgentError> {
        if let Some(ppid) = parent_pid.as_ref() {
            if !self.agents.contains_key(ppid) {
                return Err(AgentError::NotFound(format!("parent not found: {ppid}")));
            }
        }

        let pid = match pid {
            Some(p) => {
                if self.agents.contains_key(&p) {
                    return Err(AgentError::AlreadyExists(p));
                }
                p
            }
            None => self.alloc_pid()?,
        };

        let now = now_ms();
        let connection_id = external_info.as_ref().map(|e| e.connection_id.clone());
        let desc = AgentDescriptor {
            pid: pid.clone(),
            name,
            kind,
            owner_id,
            zone_id,
            parent_pid: parent_pid.clone(),
            state: AgentState::Registered,
            exit_code: None,
            generation: 1,
            cwd,
            root: "/".to_string(),
            children: Vec::new(),
            created_at_ms: now,
            updated_at_ms: now,
            last_heartbeat_ms: None,
            connection_id,
            external_info,
            labels,
            repos: Vec::new(),
        };

        self.agents.insert(pid.clone(), desc.clone());
        self.notify
            .entry(pid.clone())
            .or_insert_with(|| Arc::new(AgentNotify::new()));

        if let Some(ppid) = parent_pid.as_ref() {
            if let Some(mut parent) = self.agents.get_mut(ppid) {
                parent.children.push(pid.clone());
                parent.updated_at_ms = now;
            }
        }

        Ok(desc)
    }

    /// Unregister (remove) an agent by pid. Returns the descriptor if
    /// found. Does NOT touch the parent's children list — callers wanting
    /// reap semantics should use ``reap`` (or ``signal SIGKILL`` /
    /// ``unregister_external``).
    pub fn unregister(&self, pid: &str) -> Option<AgentDescriptor> {
        let result = self.agents.remove(pid).map(|(_, v)| v);
        if result.is_some() {
            if let Some((_, notify)) = self.notify.remove(pid) {
                let _guard = notify.mutex.lock();
                notify.state_changed.notify_all();
            }
        }
        result
    }

    /// Reap a process: remove from table + clean up parent.children.
    /// Mirrors Python `_reap`. Idempotent — returns false if not found.
    ///
    /// Termination observers are NOT fired here — they fire on the
    /// `update_state` transition to `Terminated` so cleanup happens at
    /// the moment the agent is logically dead, even if reaping is
    /// deferred (e.g. a child waiting for its parent).  By the time
    /// `reap` runs the agent is already in `Terminated` and observers
    /// have already done their work.
    pub fn reap(&self, pid: &str) -> bool {
        let desc = match self.unregister(pid) {
            Some(d) => d,
            None => return false,
        };
        if let Some(ppid) = desc.parent_pid.as_ref() {
            if let Some(mut parent) = self.agents.get_mut(ppid) {
                parent.children.retain(|c| c != pid);
                parent.updated_at_ms = now_ms();
            }
        }
        true
    }

    /// Get agent descriptor by pid.
    pub fn get(&self, pid: &str) -> Option<AgentDescriptor> {
        self.agents.get(pid).map(|r| r.clone())
    }

    /// Update state with VALID_AGENT_TRANSITIONS validation.
    /// Returns Ok(true) on success, Ok(false) if pid not found.
    /// Returns Err(InvalidTransition) when the transition is rejected.
    pub fn update_state(&self, pid: &str, new_state: AgentState) -> Result<bool, AgentError> {
        let mut entry = match self.agents.get_mut(pid) {
            Some(e) => e,
            None => return Ok(false),
        };
        let from = entry.state;
        if from == new_state {
            return Ok(true);
        }
        if !from.can_transition_to(new_state) {
            return Err(AgentError::InvalidTransition {
                pid: pid.to_string(),
                from,
                to: new_state,
            });
        }
        entry.state = new_state;
        entry.updated_at_ms = now_ms();
        drop(entry);
        self.wake(pid);
        if new_state == AgentState::Terminated && from != AgentState::Terminated {
            self.fire_on_terminate(pid);
        }
        Ok(true)
    }

    /// Update state + exit code with validation.
    pub fn update_state_with_exit(
        &self,
        pid: &str,
        new_state: AgentState,
        exit_code: i32,
    ) -> Result<bool, AgentError> {
        let mut entry = match self.agents.get_mut(pid) {
            Some(e) => e,
            None => return Ok(false),
        };
        let from = entry.state;
        if from != new_state && !from.can_transition_to(new_state) {
            return Err(AgentError::InvalidTransition {
                pid: pid.to_string(),
                from,
                to: new_state,
            });
        }
        entry.state = new_state;
        entry.exit_code = Some(exit_code);
        entry.updated_at_ms = now_ms();
        drop(entry);
        self.wake(pid);
        if new_state == AgentState::Terminated && from != AgentState::Terminated {
            self.fire_on_terminate(pid);
        }
        Ok(true)
    }

    /// Raw, unvalidated state set. Reserved for boot-time / test hooks
    /// that intentionally bypass the FSM. Do not use from production
    /// code paths.
    pub fn set_state_unchecked(&self, pid: &str, new_state: AgentState) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.state = new_state;
            entry.updated_at_ms = now_ms();
            drop(entry);
            self.wake(pid);
            true
        } else {
            false
        }
    }

    /// Update heartbeat timestamp. Returns Err(NotFound) / Err(InvalidKind)
    /// for protocol violations to mirror the Python contract; otherwise
    /// Ok(true) on success.
    pub fn heartbeat(&self, pid: &str) -> Result<bool, AgentError> {
        let mut entry = match self.agents.get_mut(pid) {
            Some(e) => e,
            None => return Err(AgentError::NotFound(pid.to_string())),
        };
        if entry.kind != AgentKind::Unmanaged {
            return Err(AgentError::InvalidKind(format!(
                "heartbeat only for unmanaged processes: {pid}"
            )));
        }
        if entry.external_info.is_none() {
            return Err(AgentError::Protocol(format!(
                "missing external_info: {pid}"
            )));
        }
        let now = now_ms();
        entry.last_heartbeat_ms = Some(now);
        if let Some(info) = entry.external_info.as_mut() {
            info.last_heartbeat_ms = Some(now);
        }
        entry.updated_at_ms = now;
        Ok(true)
    }

    /// Variant accepted by callers that already have an authoritative
    /// timestamp (e.g. dual-write paths from the wider system clock).
    /// No kind/info validation — simply stamps the heartbeat field.
    pub fn heartbeat_at(&self, pid: &str, timestamp_ms: u64) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.last_heartbeat_ms = Some(timestamp_ms);
            if let Some(info) = entry.external_info.as_mut() {
                info.last_heartbeat_ms = Some(timestamp_ms);
            }
            entry.updated_at_ms = now_ms();
            true
        } else {
            false
        }
    }

    /// List all agents, optionally filtered by zone_id, owner_id, kind,
    /// state.
    pub fn list(
        &self,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
        kind: Option<&AgentKind>,
        state: Option<&AgentState>,
    ) -> Vec<AgentDescriptor> {
        self.agents
            .iter()
            .filter(|entry| {
                let desc = entry.value();
                zone_id.is_none_or(|z| desc.zone_id == z)
                    && owner_id.is_none_or(|o| desc.owner_id == o)
                    && kind.is_none_or(|k| &desc.kind == k)
                    && state.is_none_or(|s| &desc.state == s)
            })
            .map(|entry| entry.value().clone())
            .collect()
    }

    /// Count processes in a given state, optionally scoped to a zone.
    pub fn count_by_state(&self, state: AgentState, zone_id: Option<&str>) -> usize {
        self.agents
            .iter()
            .filter(|entry| {
                let desc = entry.value();
                desc.state == state && zone_id.is_none_or(|z| desc.zone_id == z)
            })
            .count()
    }

    /// List BUSY processes ordered by eviction priority (lowest first), then
    /// by `updated_at_ms` (LRU). Mirrors Python `list_by_priority`.
    pub fn list_by_priority(
        &self,
        zone_id: Option<&str>,
        batch_size: usize,
    ) -> Vec<AgentDescriptor> {
        let mut procs: Vec<AgentDescriptor> = self
            .agents
            .iter()
            .filter(|entry| {
                let desc = entry.value();
                desc.state == AgentState::Busy && zone_id.is_none_or(|z| desc.zone_id == z)
            })
            .map(|entry| entry.value().clone())
            .collect();

        procs.sort_by(|a, b| {
            let pri_a: i64 = a
                .labels
                .get("eviction_priority")
                .and_then(|v| v.parse().ok())
                .unwrap_or(50);
            let pri_b: i64 = b
                .labels
                .get("eviction_priority")
                .and_then(|v| v.parse().ok())
                .unwrap_or(50);
            pri_a
                .cmp(&pri_b)
                .then_with(|| a.updated_at_ms.cmp(&b.updated_at_ms))
        });

        procs.truncate(batch_size);
        procs
    }

    /// Number of registered agents.
    pub fn count(&self) -> usize {
        self.agents.len()
    }

    /// Block until agent `pid` reaches `target_state` or timeout.
    ///
    /// Returns the state string when the target is reached, or
    /// `Err("timeout")` / `Err("not_found")` on failure.
    ///
    /// Callers must hold no DashMap refs across this call (no deadlock).
    pub fn wait_for_state(
        &self,
        pid: &str,
        target_state: &AgentState,
        timeout_ms: u64,
    ) -> Result<String, String> {
        let notify = match self.notify.get(pid) {
            Some(n) => Arc::clone(n.value()),
            None => return Err("not_found".to_string()),
        };

        // Fast path
        if let Some(desc) = self.agents.get(pid) {
            if &desc.state == target_state || desc.state.is_terminal() {
                return Ok(desc.state.as_str().to_string());
            }
        } else {
            return Err("not_found".to_string());
        }

        // Slow path: park on condvar
        let timeout = Duration::from_millis(timeout_ms);
        let deadline = std::time::Instant::now() + timeout;
        let mut guard = notify.mutex.lock();

        loop {
            match self.agents.get(pid) {
                Some(desc) if &desc.state == target_state || desc.state.is_terminal() => {
                    return Ok(desc.state.as_str().to_string());
                }
                None => return Err("not_found".to_string()),
                _ => {}
            }

            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err("timeout".to_string());
            }
            if notify
                .state_changed
                .wait_for(&mut guard, remaining)
                .timed_out()
            {
                match self.agents.get(pid) {
                    Some(desc) if &desc.state == target_state || desc.state.is_terminal() => {
                        return Ok(desc.state.as_str().to_string());
                    }
                    None => return Err("not_found".to_string()),
                    _ => return Err("timeout".to_string()),
                }
            }
        }
    }

    // ── Signal semantics (mirrors Python AgentRegistry.signal) ───────────

    /// Send a signal. Returns the post-signal descriptor, or an
    /// `AgentError`. Reaping happens for SIGKILL (and SIGTERM via kill).
    pub fn signal(
        &self,
        pid: &str,
        sig: AgentSignal,
        payload: Option<HashMap<String, String>>,
    ) -> Result<AgentDescriptor, AgentError> {
        match sig {
            AgentSignal::Sigstop => {
                self.update_state(pid, AgentState::Suspended)?;
                self.get(pid)
                    .ok_or_else(|| AgentError::NotFound(pid.to_string()))
            }
            AgentSignal::Sigcont => {
                let mut entry = self
                    .agents
                    .get_mut(pid)
                    .ok_or_else(|| AgentError::NotFound(pid.to_string()))?;
                let from = entry.state;
                if !from.can_transition_to(AgentState::Ready) {
                    return Err(AgentError::InvalidTransition {
                        pid: pid.to_string(),
                        from,
                        to: AgentState::Ready,
                    });
                }
                entry.state = AgentState::Ready;
                entry.generation += 1;
                entry.updated_at_ms = now_ms();
                drop(entry);
                self.wake(pid);
                Ok(self.get(pid).unwrap())
            }
            AgentSignal::Sigterm => self.kill(pid, 0),
            AgentSignal::Sigkill => {
                let desc = self
                    .get(pid)
                    .ok_or_else(|| AgentError::NotFound(pid.to_string()))?;
                if desc.state != AgentState::Terminated {
                    self.update_state_with_exit(pid, AgentState::Terminated, -9)?;
                }
                let final_desc = self.get(pid).unwrap_or(desc);
                self.reap(pid);
                Ok(final_desc)
            }
            AgentSignal::Sigusr1 => {
                let mut entry = self
                    .agents
                    .get_mut(pid)
                    .ok_or_else(|| AgentError::NotFound(pid.to_string()))?;
                if let Some(p) = payload {
                    for (k, v) in p {
                        entry.labels.insert(k, v);
                    }
                    entry.updated_at_ms = now_ms();
                }
                let snapshot = entry.clone();
                drop(entry);
                self.wake(pid);
                Ok(snapshot)
            }
        }
    }

    /// Kill — TERMINATED + auto-reap if orphan. Mirrors Python ``kill``.
    pub fn kill(&self, pid: &str, exit_code: i32) -> Result<AgentDescriptor, AgentError> {
        let desc = self
            .get(pid)
            .ok_or_else(|| AgentError::NotFound(pid.to_string()))?;
        if desc.state == AgentState::Terminated {
            return Ok(desc);
        }
        self.update_state_with_exit(pid, AgentState::Terminated, exit_code)?;
        let updated = self.get(pid).unwrap_or(desc);
        if updated.parent_pid.is_none() {
            self.reap(pid);
        }
        Ok(updated)
    }

    /// Register an external (gRPC/MCP) process — UNMANAGED kind, the
    /// connection_id IS the pid. Returns the new descriptor.
    #[allow(clippy::too_many_arguments)]
    pub fn register_external(
        &self,
        name: String,
        owner_id: String,
        zone_id: String,
        connection_id: String,
        host_pid: Option<i64>,
        remote_addr: Option<String>,
        protocol: String,
        parent_pid: Option<String>,
        labels: HashMap<String, String>,
    ) -> Result<AgentDescriptor, AgentError> {
        let now = now_ms();
        let info = ExternalProcessInfo {
            connection_id: connection_id.clone(),
            host_pid,
            remote_addr,
            protocol,
            last_heartbeat_ms: Some(now),
        };
        self.spawn(
            name,
            owner_id,
            zone_id,
            AgentKind::Unmanaged,
            parent_pid,
            Some(connection_id),
            "/".to_string(),
            Some(info),
            labels,
        )
    }

    /// Unregister an external process — TERMINATED + reap. Mirrors Python
    /// ``unregister_external``.
    pub fn unregister_external(&self, pid: &str) -> Result<(), AgentError> {
        let desc = self
            .get(pid)
            .ok_or_else(|| AgentError::NotFound(pid.to_string()))?;
        if desc.kind != AgentKind::Unmanaged {
            return Err(AgentError::InvalidKind(format!(
                "unregister_external only for unmanaged processes: {pid}"
            )));
        }
        if desc.state != AgentState::Terminated {
            self.update_state(pid, AgentState::Terminated)?;
        }
        self.reap(pid);
        Ok(())
    }

    /// Drain: kill+reap every process. Used at shutdown.
    pub fn close_all(&self) {
        let pids: Vec<String> = self.agents.iter().map(|e| e.key().clone()).collect();
        for pid in pids {
            // Best-effort — already-terminated agents skip the transition.
            let _ = self.kill(&pid, 0);
            self.reap(&pid);
        }
    }
}

impl Default for AgentRegistry {
    fn default() -> Self {
        Self::new()
    }
}

// ── Tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_desc(pid: &str, name: &str) -> AgentDescriptor {
        AgentDescriptor {
            pid: pid.to_string(),
            name: name.to_string(),
            kind: AgentKind::Worker,
            owner_id: "user1".to_string(),
            zone_id: "zone1".to_string(),
            created_at_ms: 1000,
            updated_at_ms: 1000,
            ..Default::default()
        }
    }

    #[test]
    fn test_register_and_get() {
        let reg = AgentRegistry::new();
        assert!(reg.register(make_desc("p1", "agent1")));
        let desc = reg.get("p1").unwrap();
        assert_eq!(desc.name, "agent1");
        assert_eq!(desc.state, AgentState::Registered);
    }

    #[test]
    fn test_duplicate_register() {
        let reg = AgentRegistry::new();
        assert!(reg.register(make_desc("p1", "agent1")));
        assert!(!reg.register(make_desc("p1", "agent2")));
    }

    #[test]
    fn test_update_state_validates() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "agent1"));
        assert_eq!(reg.update_state("p1", AgentState::WarmingUp), Ok(true));
        assert_eq!(reg.get("p1").unwrap().state, AgentState::WarmingUp);
        assert_eq!(reg.update_state("p1", AgentState::Ready), Ok(true));
        assert_eq!(reg.update_state("p1", AgentState::Busy), Ok(true));
        // BUSY -> WARMING_UP is not allowed
        let err = reg.update_state("p1", AgentState::WarmingUp).unwrap_err();
        assert!(matches!(err, AgentError::InvalidTransition { .. }));
    }

    #[test]
    fn test_update_state_missing_returns_false() {
        let reg = AgentRegistry::new();
        assert_eq!(reg.update_state("nope", AgentState::Ready), Ok(false));
    }

    #[test]
    fn test_unregister() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "agent1"));
        let removed = reg.unregister("p1");
        assert!(removed.is_some());
        assert!(reg.get("p1").is_none());
    }

    #[test]
    fn test_on_terminate_fires_on_state_transition() {
        use std::sync::Mutex;
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        let captured: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let captured2 = Arc::clone(&captured);
        reg.register_on_terminate(
            "test",
            Arc::new(move |pid| captured2.lock().unwrap().push(pid.to_string())),
        );
        // Non-terminal transition does not fire.
        reg.update_state("p1", AgentState::WarmingUp).unwrap();
        assert!(captured.lock().unwrap().is_empty());
        // Terminal transition fires once.
        reg.update_state("p1", AgentState::Terminated).unwrap();
        assert_eq!(captured.lock().unwrap().as_slice(), &["p1".to_string()]);
        // Idempotent re-transition does not double-fire.
        reg.update_state("p1", AgentState::Terminated).unwrap();
        assert_eq!(captured.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_on_terminate_register_replace_and_unregister() {
        use std::sync::atomic::{AtomicU32, Ordering};
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        let calls_a: Arc<AtomicU32> = Arc::new(AtomicU32::new(0));
        let calls_b: Arc<AtomicU32> = Arc::new(AtomicU32::new(0));
        let calls_a2 = Arc::clone(&calls_a);
        let calls_b2 = Arc::clone(&calls_b);
        reg.register_on_terminate(
            "obs",
            Arc::new(move |_| {
                calls_a2.fetch_add(1, Ordering::SeqCst);
            }),
        );
        // Replace under the same id.
        reg.register_on_terminate(
            "obs",
            Arc::new(move |_| {
                calls_b2.fetch_add(1, Ordering::SeqCst);
            }),
        );
        reg.update_state("p1", AgentState::Terminated).unwrap();
        assert_eq!(calls_a.load(Ordering::SeqCst), 0);
        assert_eq!(calls_b.load(Ordering::SeqCst), 1);
        // Unregister + re-fire should produce no extra calls.
        let p2 = reg
            .spawn(
                "n2".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        assert!(reg.unregister_on_terminate("obs"));
        reg.update_state(&p2.pid, AgentState::Terminated).unwrap();
        assert_eq!(calls_b.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn test_list_with_filters() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        reg.register(make_desc("p2", "a2"));
        reg.update_state("p2", AgentState::WarmingUp).unwrap();
        reg.update_state("p2", AgentState::Ready).unwrap();
        let ready = reg.list(None, None, None, Some(&AgentState::Ready));
        assert_eq!(ready.len(), 1);
        assert_eq!(ready[0].pid, "p2");
    }

    #[test]
    fn test_spawn_allocates_pid_and_links_parent() {
        let reg = AgentRegistry::new();
        let parent = reg
            .spawn(
                "p".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let child = reg
            .spawn(
                "c".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                Some(parent.pid.clone()),
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let parent_after = reg.get(&parent.pid).unwrap();
        assert_eq!(parent_after.children, vec![child.pid.clone()]);
    }

    #[test]
    fn test_spawn_unknown_parent() {
        let reg = AgentRegistry::new();
        let err = reg
            .spawn(
                "c".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                Some("ghost".to_string()),
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap_err();
        assert!(matches!(err, AgentError::NotFound(_)));
    }

    #[test]
    fn test_signal_sigstop_sigcont() {
        let reg = AgentRegistry::new();
        let desc = reg
            .spawn(
                "a".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        reg.update_state(&desc.pid, AgentState::WarmingUp).unwrap();
        reg.update_state(&desc.pid, AgentState::Ready).unwrap();
        let after_stop = reg.signal(&desc.pid, AgentSignal::Sigstop, None).unwrap();
        assert_eq!(after_stop.state, AgentState::Suspended);
        let after_cont = reg.signal(&desc.pid, AgentSignal::Sigcont, None).unwrap();
        assert_eq!(after_cont.state, AgentState::Ready);
        assert_eq!(after_cont.generation, desc.generation + 1);
    }

    #[test]
    fn test_signal_sigterm_kills_and_reaps_orphan() {
        let reg = AgentRegistry::new();
        let desc = reg
            .spawn(
                "a".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let killed = reg.signal(&desc.pid, AgentSignal::Sigterm, None).unwrap();
        assert_eq!(killed.state, AgentState::Terminated);
        // Orphan auto-reaped.
        assert!(reg.get(&desc.pid).is_none());
    }

    #[test]
    fn test_signal_sigkill_force_terminate_and_reap() {
        let reg = AgentRegistry::new();
        let parent = reg
            .spawn(
                "p".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let child = reg
            .spawn(
                "c".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                Some(parent.pid.clone()),
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        // Even though the child has a parent (would not auto-reap on
        // SIGTERM), SIGKILL force-reaps and updates parent.children.
        reg.signal(&child.pid, AgentSignal::Sigkill, None).unwrap();
        assert!(reg.get(&child.pid).is_none());
        let parent_after = reg.get(&parent.pid).unwrap();
        assert!(parent_after.children.is_empty());
    }

    #[test]
    fn test_signal_sigusr1_merges_labels() {
        let reg = AgentRegistry::new();
        let desc = reg
            .spawn(
                "a".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let mut payload = HashMap::new();
        payload.insert("k1".to_string(), "v1".to_string());
        let after = reg
            .signal(&desc.pid, AgentSignal::Sigusr1, Some(payload))
            .unwrap();
        assert_eq!(after.labels.get("k1").map(|s| s.as_str()), Some("v1"));
        assert_eq!(after.state, AgentState::Registered);
    }

    #[test]
    fn test_register_external_and_heartbeat() {
        let reg = AgentRegistry::new();
        let desc = reg
            .register_external(
                "ext".to_string(),
                "u".to_string(),
                "z".to_string(),
                "conn-123".to_string(),
                Some(42),
                Some("1.2.3.4:5".to_string()),
                "grpc".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        assert_eq!(desc.pid, "conn-123");
        assert_eq!(desc.kind, AgentKind::Unmanaged);
        assert!(desc.external_info.is_some());
        // heartbeat ok
        assert_eq!(reg.heartbeat("conn-123"), Ok(true));
    }

    #[test]
    fn test_heartbeat_rejects_managed() {
        let reg = AgentRegistry::new();
        let desc = reg
            .spawn(
                "a".to_string(),
                "u".to_string(),
                "z".to_string(),
                AgentKind::Managed,
                None,
                None,
                "/".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        let err = reg.heartbeat(&desc.pid).unwrap_err();
        assert!(matches!(err, AgentError::InvalidKind(_)));
    }

    #[test]
    fn test_unregister_external_path() {
        let reg = AgentRegistry::new();
        let desc = reg
            .register_external(
                "ext".to_string(),
                "u".to_string(),
                "z".to_string(),
                "conn-1".to_string(),
                None,
                None,
                "grpc".to_string(),
                None,
                HashMap::new(),
            )
            .unwrap();
        reg.unregister_external(&desc.pid).unwrap();
        assert!(reg.get(&desc.pid).is_none());
    }

    #[test]
    fn test_count_by_state_and_list_by_priority() {
        let reg = AgentRegistry::new();
        for (i, prio) in ["10", "30", "20"].iter().enumerate() {
            let mut labels = HashMap::new();
            labels.insert("eviction_priority".to_string(), prio.to_string());
            let d = reg
                .spawn(
                    format!("a{i}"),
                    "u".to_string(),
                    "z".to_string(),
                    AgentKind::Managed,
                    None,
                    None,
                    "/".to_string(),
                    None,
                    labels,
                )
                .unwrap();
            reg.update_state(&d.pid, AgentState::WarmingUp).unwrap();
            reg.update_state(&d.pid, AgentState::Ready).unwrap();
            reg.update_state(&d.pid, AgentState::Busy).unwrap();
        }
        assert_eq!(reg.count_by_state(AgentState::Busy, Some("z")), 3);
        let batch = reg.list_by_priority(Some("z"), 2);
        assert_eq!(batch.len(), 2);
        assert_eq!(
            batch[0].labels.get("eviction_priority").map(String::as_str),
            Some("10")
        );
        assert_eq!(
            batch[1].labels.get("eviction_priority").map(String::as_str),
            Some("20")
        );
    }

    #[test]
    fn test_wait_for_state_fast_path() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        reg.update_state("p1", AgentState::WarmingUp).unwrap();
        reg.update_state("p1", AgentState::Ready).unwrap();
        let result = reg.wait_for_state("p1", &AgentState::Ready, 100);
        assert_eq!(result.unwrap(), "READY");
    }

    #[test]
    fn test_wait_for_state_blocking() {
        use std::sync::Arc;
        use std::thread;

        let reg = Arc::new(AgentRegistry::new());
        reg.register(make_desc("p1", "a1"));
        reg.update_state("p1", AgentState::WarmingUp).unwrap();

        let reg2 = Arc::clone(&reg);
        let writer = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            reg2.update_state("p1", AgentState::Ready).unwrap();
        });

        let result = reg.wait_for_state("p1", &AgentState::Ready, 500);
        writer.join().unwrap();
        assert_eq!(result.unwrap(), "READY");
    }

    #[test]
    fn test_wait_for_state_timeout() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        let result = reg.wait_for_state("p1", &AgentState::Ready, 50);
        assert_eq!(result.unwrap_err(), "timeout");
    }

    #[test]
    fn test_state_from_str_roundtrip() {
        for (s, expected) in [
            ("REGISTERED", AgentState::Registered),
            ("WARMING_UP", AgentState::WarmingUp),
            ("READY", AgentState::Ready),
            ("BUSY", AgentState::Busy),
            ("SUSPENDED", AgentState::Suspended),
            ("TERMINATED", AgentState::Terminated),
        ] {
            assert_eq!(AgentState::from_str(s).unwrap(), expected);
            assert_eq!(expected.as_str(), s);
        }
    }
}
