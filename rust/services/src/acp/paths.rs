//! VFS path constructors for the ACP service.
//!
//! Mirror of the subset of `nexus.contracts.vfs_paths` that AcpService
//! touches: agent config files under `/{zone}/agents/{id}/...` and
//! per-process pipe / result files under `/{zone}/proc/{pid}/...`.
//!
//! Kept as standalone functions (not a trait) so they're callable from
//! both the service and its tests without an instance handle.

/// Agent config JSON: `/{zone}/agents/{id}/agent.json`.
pub(crate) fn agent_config(zone_id: &str, agent_id: &str) -> String {
    format!("/{zone_id}/agents/{agent_id}/agent.json")
}

/// System prompt override: `/{zone}/agents/{id}/SYSTEM.md`.
pub(crate) fn system_prompt(zone_id: &str, agent_id: &str) -> String {
    format!("/{zone_id}/agents/{agent_id}/SYSTEM.md")
}

/// Enabled-skills config: `/{zone}/agents/{id}/config`.
pub(crate) fn skills(zone_id: &str, agent_id: &str) -> String {
    format!("/{zone_id}/agents/{agent_id}/config")
}

/// DT_PIPE file descriptor: `/{zone}/proc/{pid}/fd/{0,1,2}`.
pub(crate) fn proc_fd(zone_id: &str, pid: &str, fd_num: u8) -> String {
    format!("/{zone_id}/proc/{pid}/fd/{fd_num}")
}

/// Agent turn result (JSON): `/{zone}/proc/{pid}/result`.
pub(crate) fn proc_result(zone_id: &str, pid: &str) -> String {
    format!("/{zone_id}/proc/{pid}/result")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_match_python_conventions() {
        assert_eq!(
            agent_config("root", "claude"),
            "/root/agents/claude/agent.json"
        );
        assert_eq!(
            system_prompt("root", "claude"),
            "/root/agents/claude/SYSTEM.md"
        );
        assert_eq!(skills("root", "claude"), "/root/agents/claude/config");
        assert_eq!(proc_fd("root", "pid-1", 0), "/root/proc/pid-1/fd/0");
        assert_eq!(proc_fd("root", "pid-1", 2), "/root/proc/pid-1/fd/2");
        assert_eq!(proc_result("root", "pid-1"), "/root/proc/pid-1/result");
    }

    #[test]
    fn zone_id_with_dashes_is_preserved() {
        assert_eq!(
            agent_config("east-coast", "codex"),
            "/east-coast/agents/codex/agent.json"
        );
    }
}
