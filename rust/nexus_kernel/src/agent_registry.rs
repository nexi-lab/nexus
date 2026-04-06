#![allow(dead_code)]
//! AgentRegistry — Rust backing store for agent lifecycle (§10 B1-B3).
//!
//! Kernel-knows pattern: Python AgentRegistry delegates to Rust backing store.
//! DashMap<pid, AgentDescriptor> for O(1) lookup.
//!
//! Also contains:
//!   - AgentStatusResolver: impl PathResolver for /{zone}/proc/{pid}/status (B2)
//!   - AgentObserver: text chunk accumulator + usage metrics (B3)

use crate::dispatch::PathResolver;
use dashmap::DashMap;
use std::sync::atomic::{AtomicU64, Ordering};

// ── AgentDescriptor ────────────────────────────────────────────────────

/// Agent process descriptor — analogous to Linux task_struct.
#[derive(Clone, Debug)]
pub(crate) struct AgentDescriptor {
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
pub(crate) enum AgentKind {
    Worker,
    Daemon,
    Unmanaged,
}

/// Agent process state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AgentState {
    Registered,
    Running,
    Stopped,
    Failed,
    Terminated,
}

impl AgentState {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            AgentState::Registered => "REGISTERED",
            AgentState::Running => "RUNNING",
            AgentState::Stopped => "STOPPED",
            AgentState::Failed => "FAILED",
            AgentState::Terminated => "TERMINATED",
        }
    }

    pub(crate) fn from_str(s: &str) -> Option<Self> {
        match s {
            "REGISTERED" => Some(AgentState::Registered),
            "RUNNING" => Some(AgentState::Running),
            "STOPPED" => Some(AgentState::Stopped),
            "FAILED" => Some(AgentState::Failed),
            "TERMINATED" => Some(AgentState::Terminated),
            _ => None,
        }
    }
}

impl AgentKind {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            AgentKind::Worker => "WORKER",
            AgentKind::Daemon => "DAEMON",
            AgentKind::Unmanaged => "UNMANAGED",
        }
    }

    pub(crate) fn from_str(s: &str) -> Option<Self> {
        match s {
            "WORKER" => Some(AgentKind::Worker),
            "DAEMON" => Some(AgentKind::Daemon),
            "UNMANAGED" => Some(AgentKind::Unmanaged),
            _ => None,
        }
    }
}

// ── AgentRegistry ──────────────────────────────────────────────────────

/// Rust backing store for agent lifecycle.
/// DashMap<pid, AgentDescriptor> for lock-free concurrent access.
pub(crate) struct AgentRegistry {
    agents: DashMap<String, AgentDescriptor>,
}

impl AgentRegistry {
    pub(crate) fn new() -> Self {
        Self {
            agents: DashMap::new(),
        }
    }

    /// Register a new agent. Returns true if inserted (pid was new).
    pub(crate) fn register(&self, desc: AgentDescriptor) -> bool {
        use dashmap::mapref::entry::Entry;
        match self.agents.entry(desc.pid.clone()) {
            Entry::Occupied(_) => false,
            Entry::Vacant(v) => {
                v.insert(desc);
                true
            }
        }
    }

    /// Unregister (remove) an agent by pid. Returns the descriptor if found.
    pub(crate) fn unregister(&self, pid: &str) -> Option<AgentDescriptor> {
        self.agents.remove(pid).map(|(_, v)| v)
    }

    /// Get agent descriptor by pid.
    pub(crate) fn get(&self, pid: &str) -> Option<AgentDescriptor> {
        self.agents.get(pid).map(|r| r.clone())
    }

    /// Update agent state. Returns true if agent found and updated.
    pub(crate) fn update_state(&self, pid: &str, new_state: AgentState) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.state = new_state;
            true
        } else {
            false
        }
    }

    /// Update agent state with exit code.
    pub(crate) fn update_state_with_exit(
        &self,
        pid: &str,
        new_state: AgentState,
        exit_code: i32,
    ) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.state = new_state;
            entry.exit_code = Some(exit_code);
            true
        } else {
            false
        }
    }

    /// Update heartbeat timestamp for external agents.
    pub(crate) fn heartbeat(&self, pid: &str, timestamp_ms: u64) -> bool {
        if let Some(mut entry) = self.agents.get_mut(pid) {
            entry.last_heartbeat_ms = Some(timestamp_ms);
            true
        } else {
            false
        }
    }

    /// List all agents, optionally filtered by zone_id and/or state.
    #[allow(clippy::option_map_or_none)]
    pub(crate) fn list(
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
    pub(crate) fn count(&self) -> usize {
        self.agents.len()
    }
}

// ── AgentStatusResolver (§10 B2) ────────────────────────────────────────

/// PathResolver for /{zone}/proc/{pid}/status — reads from kernel AgentRegistry.
///
/// Registered in Kernel's Trie at boot time.
/// try_read returns JSON-serialized agent status.
pub(crate) struct AgentStatusResolver {
    registry: *const AgentRegistry,
}

// Safety: AgentRegistry is behind DashMap (Send+Sync). The pointer is stable
// because AgentRegistry lives inside Kernel which is heap-pinned by PyKernel.
unsafe impl Send for AgentStatusResolver {}
unsafe impl Sync for AgentStatusResolver {}

impl AgentStatusResolver {
    /// Create resolver pointing to kernel's agent registry.
    ///
    /// # Safety
    /// The registry pointer must remain valid for the lifetime of this resolver.
    pub(crate) unsafe fn new(registry: *const AgentRegistry) -> Self {
        Self { registry }
    }

    fn registry(&self) -> &AgentRegistry {
        unsafe { &*self.registry }
    }
}

impl PathResolver for AgentStatusResolver {
    fn try_read(&self, path: &str) -> Option<Vec<u8>> {
        // Parse: /{zone}/proc/{pid}/status
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        if segments.len() != 4 || segments[1] != "proc" || segments[3] != "status" {
            return None;
        }
        let pid = segments[2];
        let desc = self.registry().get(pid)?;
        // Serialize to JSON
        let json = format!(
            r#"{{"pid":"{}","name":"{}","kind":"{}","state":"{}","owner_id":"{}","zone_id":"{}","created_at_ms":{},"exit_code":{}}}"#,
            desc.pid,
            desc.name,
            desc.kind.as_str(),
            desc.state.as_str(),
            desc.owner_id,
            desc.zone_id,
            desc.created_at_ms,
            desc.exit_code.map_or("null".to_string(), |c| c.to_string()),
        );
        Some(json.into_bytes())
    }

    fn try_write(&self, _path: &str, _content: &[u8]) -> Option<()> {
        None // Read-only
    }

    fn try_delete(&self, _path: &str) -> Option<()> {
        None // Read-only
    }
}

// ── AgentObserver (§10 B3) ──────────────────────────────────────────────

/// Text chunk accumulator + usage metrics for ManagedAgentLoop.
///
/// Tracks token usage and text output for agent turns.
/// AtomicU64 counters for lock-free metric accumulation.
pub(crate) struct AgentObserver {
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
    pub(crate) fn new() -> Self {
        Self {
            chunks: parking_lot::Mutex::new(Vec::new()),
            input_tokens: AtomicU64::new(0),
            output_tokens: AtomicU64::new(0),
            turn_count: AtomicU64::new(0),
        }
    }

    /// Append a text chunk to the current turn accumulator.
    pub(crate) fn observe_chunk(&self, text: &str) {
        self.chunks.lock().push(text.to_string());
    }

    /// Record token usage for a turn.
    pub(crate) fn observe_usage(&self, input_tokens: u64, output_tokens: u64) {
        self.input_tokens.fetch_add(input_tokens, Ordering::Relaxed);
        self.output_tokens
            .fetch_add(output_tokens, Ordering::Relaxed);
    }

    /// Finish the current turn: drain accumulated chunks, increment turn counter.
    /// Returns the accumulated text for this turn.
    pub(crate) fn finish_turn(&self) -> String {
        self.turn_count.fetch_add(1, Ordering::Relaxed);
        let mut chunks = self.chunks.lock();
        let text = chunks.join("");
        chunks.clear();
        text
    }

    /// Get current accumulated text without finishing the turn.
    pub(crate) fn peek_chunks(&self) -> String {
        self.chunks.lock().join("")
    }

    /// Get usage stats: (input_tokens, output_tokens, turn_count).
    pub(crate) fn stats(&self) -> (u64, u64, u64) {
        (
            self.input_tokens.load(Ordering::Relaxed),
            self.output_tokens.load(Ordering::Relaxed),
            self.turn_count.load(Ordering::Relaxed),
        )
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
    fn test_update_state() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "agent1"));
        assert!(reg.update_state("p1", AgentState::Running));
        assert_eq!(reg.get("p1").unwrap().state, AgentState::Running);
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
    fn test_list_with_filters() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("p1", "a1"));
        reg.register(make_desc("p2", "a2"));
        reg.update_state("p2", AgentState::Running);
        let running = reg.list(None, Some(&AgentState::Running), None);
        assert_eq!(running.len(), 1);
        assert_eq!(running[0].pid, "p2");
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
    fn test_agent_status_resolver() {
        let reg = AgentRegistry::new();
        reg.register(make_desc("abc123", "test-agent"));
        let resolver = unsafe { AgentStatusResolver::new(&reg as *const AgentRegistry) };
        let data = resolver.try_read("/zone1/proc/abc123/status").unwrap();
        let json = String::from_utf8(data).unwrap();
        assert!(json.contains("\"pid\":\"abc123\""));
        assert!(json.contains("\"state\":\"REGISTERED\""));
        // Non-matching paths
        assert!(resolver.try_read("/zone1/proc/abc123/other").is_none());
        assert!(resolver.try_read("/zone1/notproc/abc123/status").is_none());
    }
}
