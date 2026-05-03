//! Pid allocator + epoch-ms helper for `ManagedAgentService`.
//!
//! There is no separate `session_id` for managed-agent sessions: the
//! AgentRegistry pid IS the session identifier sudowork sends back
//! over `cancel_v1` / `get_session_v1`.  Everything else
//! (workspace_path, model, agent name, state) is derived from the
//! descriptor on demand.

use uuid::Uuid;

pub(crate) fn alloc_pid() -> String {
    format!("pid-{}", short_uuid())
}

/// 12-char hex prefix of a v4 uuid. Plenty of entropy for kernel-local
/// pid scope, and short enough to fit in log lines + path segments
/// (`/proc/{pid}/workspace/`) without being noisy.
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
    fn alloc_pid_collisions_are_unlikely() {
        let mut seen = std::collections::HashSet::new();
        for _ in 0..1024 {
            assert!(seen.insert(alloc_pid()), "pid collision in 1024 draws");
        }
    }
}
