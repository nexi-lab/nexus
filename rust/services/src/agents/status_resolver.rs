#![allow(dead_code)]
//! AgentStatusResolver — procfs view over the AgentTable SSOT.
//!
//! Implements the kernel `PathResolver` trait for `/{zone}/proc/{pid}/status`.
//! Reads from the [`kernel::core::agents::table::AgentTable`] SSOT; ownership is
//! shared via `Arc`, so the resolver remains valid for as long as any caller
//! holds it, independent of the Kernel's lifetime or field layout.
//!
//! The resolver is service-tier (it serves a virtual procfs view); the
//! trait it impls (`PathResolver`) is the kernel's in-tree Rust API for
//! that virtual-path mechanism, exposed via
//! `kernel::core::dispatch::PathResolver`.

use kernel::core::agents::table::AgentTable;
use kernel::core::dispatch::PathResolver;
use std::sync::Arc;

pub struct AgentStatusResolver {
    table: Arc<AgentTable>,
}

impl AgentStatusResolver {
    pub fn new(table: Arc<AgentTable>) -> Self {
        Self { table }
    }

    fn table(&self) -> &AgentTable {
        &self.table
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
        let desc = self.table().get(pid)?;

        // serde_json escapes user-controlled fields (pid, name, owner_id,
        // zone_id) so a path containing a quote / backslash produces valid
        // JSON instead of malformed output.
        let value = serde_json::json!({
            "pid": desc.pid,
            "name": desc.name,
            "kind": desc.kind.as_str(),
            "state": desc.state.as_str(),
            "owner_id": desc.owner_id,
            "zone_id": desc.zone_id,
            "created_at_ms": desc.created_at_ms,
            "exit_code": desc.exit_code,
        });
        Some(
            serde_json::to_vec(&value)
                .unwrap_or_else(|_| b"{\"error\":\"serialization failed\"}".to_vec()),
        )
    }

    fn try_write(&self, _path: &str, _content: &[u8]) -> Option<()> {
        None
    }

    fn try_delete(&self, _path: &str) -> Option<()> {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::core::agents::table::{AgentDescriptor, AgentKind, AgentState};

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
    fn test_agent_status_resolver() {
        let table = Arc::new(AgentTable::new());
        table.register(make_desc("abc123", "test-agent"));
        let resolver = AgentStatusResolver::new(Arc::clone(&table));
        let data = resolver.try_read("/zone1/proc/abc123/status").unwrap();
        let json = String::from_utf8(data).unwrap();
        assert!(json.contains("\"pid\":\"abc123\""));
        assert!(json.contains("\"state\":\"REGISTERED\""));
        // Non-matching paths
        assert!(resolver.try_read("/zone1/proc/abc123/other").is_none());
        assert!(resolver.try_read("/zone1/notproc/abc123/status").is_none());
    }
}
