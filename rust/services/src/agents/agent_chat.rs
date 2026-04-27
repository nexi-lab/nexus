//! Agent-name `chat-with-me` resolver — routes a write or read on
//! `/agents/{name}/chat-with-me` to the matching `/proc/{pid}/chat-with-me`
//! when there is exactly one active pid for the agent.
//!
//! Lives in the services rlib next to mailbox_stamping and the
//! AgentTable SSOT it consults. The kernel calls into this from
//! sys_write / sys_read after DT_LINK follow, so the steady-state cost
//! on non-aggregator paths is one `str::starts_with` test inside the
//! resolver.
//!
//! Resolution policy (single-instance MVP — multi-instance merging is
//! deferred):
//!
//!   * 0 active pids for the agent  → Err(NoInstance)
//!   * 1 active pid                  → Ok(Some("/proc/{pid}/chat-with-me"))
//!   * 2+ active pids                → Err(Ambiguous(pids))
//!
//! "Active" = AgentState ∈ { WarmingUp, Ready, Busy, Suspended }. Terminated
//! and Registered (not yet warm) pids are skipped so a stale agent
//! record doesn't intercept the path.

use kernel::core::agents::table::{AgentState, AgentTable};

const AGENTS_PREFIX: &str = "/agents/";
const CHAT_WITH_ME_SUFFIX: &str = "/chat-with-me";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentChatError {
    /// No active pid is registered for the agent — caller should treat
    /// the path as "not found" so the normal pipeline returns the usual
    /// not-found error.
    NoInstance(String),
    /// Multiple active pids share the agent name. The resolver refuses
    /// to pick one silently; callers should address
    /// `/proc/{pid}/chat-with-me` directly. The list of candidate pids
    /// is returned so the kernel can include it in the structured error
    /// message that reaches the LLM.
    Ambiguous {
        agent_name: String,
        pids: Vec<String>,
    },
}

/// Returns `Ok(Some(/proc/{pid}/chat-with-me))` when `path` is the
/// agent-name chat path AND exactly one pid is active for the agent.
/// Returns `Ok(None)` for paths that are not agent-name chat paths so
/// kernel callers short-circuit with one borrow check.
pub fn resolve_agent_chat(
    table: &AgentTable,
    path: &str,
) -> Result<Option<String>, AgentChatError> {
    let Some(agent_name) = parse_agent_chat_path(path) else {
        return Ok(None);
    };

    // Filter the agent list by name and active state. AgentTable has no
    // by-name index; the linear scan is fine because the table is
    // bounded (a few dozen pids in practice) and this only runs on
    // matching paths.
    let mut active_pids: Vec<String> = table
        .list(None, None, None)
        .into_iter()
        .filter(|d| d.name == agent_name && is_active(&d.state))
        .map(|d| d.pid)
        .collect();

    match active_pids.len() {
        0 => Err(AgentChatError::NoInstance(agent_name.to_string())),
        1 => {
            let pid = active_pids.pop().expect("len checked above");
            Ok(Some(format!("/proc/{pid}/chat-with-me")))
        }
        _ => {
            active_pids.sort();
            Err(AgentChatError::Ambiguous {
                agent_name: agent_name.to_string(),
                pids: active_pids,
            })
        }
    }
}

fn parse_agent_chat_path(path: &str) -> Option<&str> {
    let rest = path.strip_prefix(AGENTS_PREFIX)?;
    let agent_name = rest.strip_suffix(CHAT_WITH_ME_SUFFIX)?;
    if agent_name.is_empty() || agent_name.contains('/') {
        return None;
    }
    Some(agent_name)
}

fn is_active(state: &AgentState) -> bool {
    matches!(
        state,
        AgentState::WarmingUp | AgentState::Ready | AgentState::Busy | AgentState::Suspended
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_table::{AgentDescriptor, AgentKind};

    fn make_table() -> AgentTable {
        AgentTable::new()
    }

    fn descriptor(pid: &str, name: &str, state: AgentState) -> AgentDescriptor {
        AgentDescriptor {
            pid: pid.to_string(),
            name: name.to_string(),
            kind: AgentKind::Managed,
            state,
            owner_id: "user".to_string(),
            zone_id: "root".to_string(),
            created_at_ms: 1,
            exit_code: None,
            parent_pid: None,
            connection_id: None,
            last_heartbeat_ms: None,
        }
    }

    #[test]
    fn passes_through_non_agent_chat_paths() {
        let t = make_table();
        assert_eq!(
            resolve_agent_chat(&t, "/proc/p1/chat-with-me").unwrap(),
            None
        );
        assert_eq!(
            resolve_agent_chat(&t, "/agents/scode-standard/config.toml").unwrap(),
            None,
        );
        assert_eq!(
            resolve_agent_chat(&t, "/agents/scode-standard/sub/chat-with-me").unwrap(),
            None,
            "nested paths under /agents/{{name}}/ must not be intercepted",
        );
    }

    #[test]
    fn no_instance_returns_dedicated_error() {
        let t = make_table();
        let err = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap_err();
        assert_eq!(
            err,
            AgentChatError::NoInstance("scode-standard".to_string())
        );
    }

    #[test]
    fn single_active_pid_resolves_to_proc_path() {
        let t = make_table();
        t.register(descriptor("p1", "scode-standard", AgentState::Ready));
        let r = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap();
        assert_eq!(r.as_deref(), Some("/proc/p1/chat-with-me"));
    }

    #[test]
    fn ignores_terminated_and_unregistered_pids() {
        let t = make_table();
        t.register(descriptor(
            "p_dead",
            "scode-standard",
            AgentState::Terminated,
        ));
        t.register(descriptor(
            "p_pending",
            "scode-standard",
            AgentState::Registered,
        ));
        // No active pid even though two records exist for the agent name.
        let err = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap_err();
        assert!(matches!(err, AgentChatError::NoInstance(_)));
    }

    #[test]
    fn warming_up_busy_suspended_all_count_as_active() {
        for state in [
            AgentState::WarmingUp,
            AgentState::Busy,
            AgentState::Suspended,
        ] {
            let t = make_table();
            t.register(descriptor("p1", "scode-standard", state));
            let r = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap();
            assert_eq!(r.as_deref(), Some("/proc/p1/chat-with-me"));
        }
    }

    #[test]
    fn multiple_active_pids_return_ambiguous_with_sorted_pids() {
        let t = make_table();
        t.register(descriptor("p_b", "scode-standard", AgentState::Ready));
        t.register(descriptor("p_a", "scode-standard", AgentState::Busy));
        // Different agent name — ignored when resolving scode-standard.
        t.register(descriptor("p_c", "scode-fast", AgentState::Ready));
        let err = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap_err();
        match err {
            AgentChatError::Ambiguous { agent_name, pids } => {
                assert_eq!(agent_name, "scode-standard");
                assert_eq!(pids, vec!["p_a".to_string(), "p_b".to_string()]);
            }
            _ => panic!("expected Ambiguous, got {err:?}"),
        }
    }

    #[test]
    fn ignores_pids_with_other_agent_names() {
        let t = make_table();
        t.register(descriptor("p1", "scode-standard", AgentState::Ready));
        t.register(descriptor("p2", "scode-fast", AgentState::Ready));
        let r = resolve_agent_chat(&t, "/agents/scode-standard/chat-with-me").unwrap();
        assert_eq!(r.as_deref(), Some("/proc/p1/chat-with-me"));
    }
}
