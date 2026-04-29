//! Session bookkeeping for managed-agent gRPC handlers.
//!
//! `Session` is the row carried by `ManagedAgentService` for every
//! active managed-agent invocation: it carries the surface sudowork
//! addresses (session_id, agent name, workspace_path) and the
//! AgentRegistry pid so cancel / get_session can reach AgentTable
//! without sudowork having to track pids.
//!
//! sudowork sees `session_id`; nexus tracks both. The map lives on the
//! service struct (`DashMap<session_id, Session>`); this module only
//! defines the row shape and the `pid` / `session_id` allocators.

use uuid::Uuid;

#[derive(Clone, Debug)]
pub(crate) struct Session {
    pub session_id: String,
    pub pid: String,
    pub agent: String,
    pub model: String,
    pub workspace_path: String,
}

pub(crate) fn alloc_pid() -> String {
    format!("pid-{}", short_uuid())
}

pub(crate) fn alloc_session_id() -> String {
    format!("sess-{}", short_uuid())
}

/// 12-char hex prefix of a v4 uuid. Plenty of entropy for kernel-local
/// session / pid scope, and short enough to fit in log lines + path
/// segments (`/proc/{pid}/workspace/`) without being noisy.
fn short_uuid() -> String {
    let s = Uuid::new_v4().simple().to_string();
    s[..12].to_string()
}

pub(crate) fn now_ms() -> u64 {
    use std::time::SystemTime;
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alloc_pid_has_pid_prefix() {
        let p = alloc_pid();
        assert!(p.starts_with("pid-"));
        assert_eq!(p.len(), 4 + 12);
    }

    #[test]
    fn alloc_session_id_has_sess_prefix() {
        let s = alloc_session_id();
        assert!(s.starts_with("sess-"));
        assert_eq!(s.len(), 5 + 12);
    }

    #[test]
    fn alloc_pid_collisions_are_unlikely() {
        let mut seen = std::collections::HashSet::new();
        for _ in 0..1024 {
            assert!(seen.insert(alloc_pid()), "pid collision in 1024 draws");
        }
    }
}
