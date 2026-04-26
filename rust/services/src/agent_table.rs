#![allow(dead_code)]
//! AgentTable — Rust SSOT for agent lifecycle state.
//!
//! Linux task_struct array analogue. State (state field + condvar wakeup)
//! lives here; richer PCB metadata (cwd/labels/children/generation) is
//! cached on the Python side. The Python `AgentRegistry` service in
//! `nexus.services.agents` is a thin shim that delegates state ops here
//! and owns OS-level behavior (spawn/kill/signal/transition validation).
//!
//! AgentState mirrors `contracts/process_types.py` exactly:
//!   REGISTERED → WARMING_UP → READY ↔ BUSY → TERMINATED
//!   READY/BUSY → SUSPENDED → READY
//!
//! Companion: AgentObserver — text-chunk accumulator + usage metrics for
//! the managed-agent loop. Lock-free atomic counters; the chunk buffer
//! uses parking_lot::Mutex.

use dashmap::DashMap;
use parking_lot::{Condvar, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

// ── AgentDescriptor ────────────────────────────────────────────────────

/// Agent process descriptor — analogous to Linux task_struct.
#[derive(Clone, Debug)]
pub struct AgentDescriptor {
    pub pid: String,
    pub name: String,
    pub kind: AgentKind,
    pub state: AgentState,
    pub owner_id: String,
    pub zone_id: String,
    pub created_at_ms: u64,
    pub exit_code: Option<i32>,
    pub parent_pid: Option<String>,
    pub connection_id: Option<String>,
    pub last_heartbeat_ms: Option<u64>,
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
#[derive(Clone, Debug, PartialEq, Eq)]
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
            "WORKER" => Some(AgentKind::Worker),
            "DAEMON" => Some(AgentKind::Daemon),
            "UNMANAGED" | "unmanaged" => Some(AgentKind::Unmanaged),
            "MANAGED" | "managed" => Some(AgentKind::Managed),
            _ => None,
        }
    }
}

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

// ── AgentTable ─────────────────────────────────────────────────────────

/// Rust SSOT for agent lifecycle state.
/// DashMap<pid, AgentDescriptor> for lock-free concurrent access; per-pid
/// condvar wakes blocking waiters on state transitions.
pub struct AgentTable {
    agents: DashMap<String, AgentDescriptor>,
    notify: DashMap<String, Arc<AgentNotify>>,
}

impl AgentTable {
    pub fn new() -> Self {
        Self {
            agents: DashMap::new(),
            notify: DashMap::new(),
        }
    }

    /// Register a new agent. Returns true if inserted (pid was new).
    pub fn register(&self, desc: AgentDescriptor) -> bool {
        use dashmap::mapref::entry::Entry;
        match self.agents.entry(desc.pid.clone()) {
            Entry::Occupied(_) => false,
            Entry::Vacant(v) => {
                let pid = desc.pid.clone();
                v.insert(desc);
                self.notify
                    .entry(pid)
                    .or_insert_with(|| Arc::new(AgentNotify::new()));
                true
            }
        }
    }

    /// Unregister (remove) an agent by pid. Returns the descriptor if found.
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

    /// Get agent descriptor by pid.
    pub fn get(&self, pid: &str) -> Option<AgentDescriptor> {
        self.agents.get(pid).map(|r| r.clone())
    }

    /// Update agent state. Returns true if agent found and updated.
    pub fn update_state(&self, pid: &str, new_state: AgentState) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.state = new_state;
            drop(entry);
            if let Some(notify) = self.notify.get(pid) {
                let _guard = notify.mutex.lock();
                notify.state_changed.notify_all();
            }
            true
        } else {
            false
        }
    }

    /// Update agent state with exit code.
    pub fn update_state_with_exit(
        &self,
        pid: &str,
        new_state: AgentState,
        exit_code: i32,
    ) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.state = new_state;
            entry.exit_code = Some(exit_code);
            drop(entry);
            if let Some(notify) = self.notify.get(pid) {
                let _guard = notify.mutex.lock();
                notify.state_changed.notify_all();
            }
            true
        } else {
            false
        }
    }

    /// Update heartbeat timestamp for external agents.
    pub fn heartbeat(&self, pid: &str, timestamp_ms: u64) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.last_heartbeat_ms = Some(timestamp_ms);
            true
        } else {
            false
        }
    }

    /// List all agents, optionally filtered by zone_id and/or state.
    #[allow(clippy::option_map_or_none)]
    pub fn list(
        &self,
        zone_id: Option<&str>,
        state: Option<&AgentState>,
        kind: Option<&AgentKind>,
    ) -> Vec<AgentDescriptor> {
        self.agents
            .iter()
            .filter(|entry| {
                let desc = entry.value();
                zone_id.is_none_or(|z| desc.zone_id == z)
                    && state.is_none_or(|s| &desc.state == s)
                    && kind.is_none_or(|k| &desc.kind == k)
            })
            .map(|entry| entry.value().clone())
            .collect()
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
}

impl Default for AgentTable {
    fn default() -> Self {
        Self::new()
    }
}

// ── AgentObserver ───────────────────────────────────────────────────────

/// Text chunk accumulator + usage metrics for the managed-agent loop.
///
/// Tracks token usage and text output for agent turns. AtomicU64 counters
/// for lock-free metric accumulation.
pub struct AgentObserver {
    /// Accumulated text chunks for current turn.
    chunks: parking_lot::Mutex<Vec<String>>,
    /// Total input tokens across all turns.
    pub input_tokens: AtomicU64,
    /// Total output tokens across all turns.
    pub output_tokens: AtomicU64,
    /// Number of completed turns.
    pub turn_count: AtomicU64,
}

impl AgentObserver {
    pub fn new() -> Self {
        Self {
            chunks: parking_lot::Mutex::new(Vec::new()),
            input_tokens: AtomicU64::new(0),
            output_tokens: AtomicU64::new(0),
            turn_count: AtomicU64::new(0),
        }
    }

    /// Append a text chunk to the current turn accumulator.
    pub fn observe_chunk(&self, text: &str) {
        self.chunks.lock().push(text.to_string());
    }

    /// Record token usage for a turn.
    pub fn observe_usage(&self, input_tokens: u64, output_tokens: u64) {
        self.input_tokens.fetch_add(input_tokens, Ordering::Relaxed);
        self.output_tokens
            .fetch_add(output_tokens, Ordering::Relaxed);
    }

    /// Finish the current turn: drain accumulated chunks, increment turn counter.
    /// Returns the accumulated text for this turn.
    pub fn finish_turn(&self) -> String {
        self.turn_count.fetch_add(1, Ordering::Relaxed);
        let mut chunks = self.chunks.lock();
        let text = chunks.join("");
        chunks.clear();
        text
    }

    /// Get current accumulated text without finishing the turn.
    pub fn peek_chunks(&self) -> String {
        self.chunks.lock().join("")
    }

    /// Get usage stats: (input_tokens, output_tokens, turn_count).
    pub fn stats(&self) -> (u64, u64, u64) {
        (
            self.input_tokens.load(Ordering::Relaxed),
            self.output_tokens.load(Ordering::Relaxed),
            self.turn_count.load(Ordering::Relaxed),
        )
    }
}

impl Default for AgentObserver {
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
            state: AgentState::Registered,
            owner_id: "user1".to_string(),
            zone_id: "zone1".to_string(),
            created_at_ms: 1000,
            exit_code: None,
            parent_pid: None,
            connection_id: None,
            last_heartbeat_ms: None,
        }
    }

    #[test]
    fn test_register_and_get() {
        let reg = AgentTable::new();
        assert!(reg.register(make_desc("p1", "agent1")));
        let desc = reg.get("p1").unwrap();
        assert_eq!(desc.name, "agent1");
        assert_eq!(desc.state, AgentState::Registered);
    }

    #[test]
    fn test_duplicate_register() {
        let reg = AgentTable::new();
        assert!(reg.register(make_desc("p1", "agent1")));
        assert!(!reg.register(make_desc("p1", "agent2")));
    }

    #[test]
    fn test_update_state() {
        let reg = AgentTable::new();
        reg.register(make_desc("p1", "agent1"));
        assert!(reg.update_state("p1", AgentState::WarmingUp));
        assert_eq!(reg.get("p1").unwrap().state, AgentState::WarmingUp);
        assert!(reg.update_state("p1", AgentState::Ready));
        assert_eq!(reg.get("p1").unwrap().state, AgentState::Ready);
        assert!(reg.update_state("p1", AgentState::Busy));
        assert_eq!(reg.get("p1").unwrap().state, AgentState::Busy);
    }

    #[test]
    fn test_unregister() {
        let reg = AgentTable::new();
        reg.register(make_desc("p1", "agent1"));
        let removed = reg.unregister("p1");
        assert!(removed.is_some());
        assert!(reg.get("p1").is_none());
    }

    #[test]
    fn test_list_with_filters() {
        let reg = AgentTable::new();
        reg.register(make_desc("p1", "a1"));
        reg.register(make_desc("p2", "a2"));
        reg.update_state("p2", AgentState::Ready);
        let ready = reg.list(None, Some(&AgentState::Ready), None);
        assert_eq!(ready.len(), 1);
        assert_eq!(ready[0].pid, "p2");
    }

    #[test]
    fn test_agent_observer() {
        let obs = AgentObserver::new();
        obs.observe_chunk("Hello ");
        obs.observe_chunk("world");
        obs.observe_usage(100, 50);
        let text = obs.finish_turn();
        assert_eq!(text, "Hello world");
        let (inp, out, turns) = obs.stats();
        assert_eq!(inp, 100);
        assert_eq!(out, 50);
        assert_eq!(turns, 1);
        // After finish_turn, chunks are cleared
        assert_eq!(obs.peek_chunks(), "");
    }

    #[test]
    fn test_wait_for_state_fast_path() {
        let reg = AgentTable::new();
        reg.register(make_desc("p1", "a1"));
        reg.update_state("p1", AgentState::Ready);
        let result = reg.wait_for_state("p1", &AgentState::Ready, 100);
        assert_eq!(result.unwrap(), "READY");
    }

    #[test]
    fn test_wait_for_state_blocking() {
        use std::sync::Arc;
        use std::thread;

        let reg = Arc::new(AgentTable::new());
        reg.register(make_desc("p1", "a1"));

        let reg2 = Arc::clone(&reg);
        let writer = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            reg2.update_state("p1", AgentState::Ready);
        });

        let result = reg.wait_for_state("p1", &AgentState::Ready, 500);
        writer.join().unwrap();
        assert_eq!(result.unwrap(), "READY");
    }

    #[test]
    fn test_wait_for_state_timeout() {
        let reg = AgentTable::new();
        reg.register(make_desc("p1", "a1"));
        // Never transition — should timeout
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
