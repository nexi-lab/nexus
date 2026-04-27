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

/// Enumerate every active pid's `/proc/{pid}/chat-with-me` for an
/// agent name. The kernel uses this for the multi-instance broadcast
/// path: when more than one pid is active, the write fans out to every
/// returned target.
///
/// Returns `Ok(None)` when `path` is not an agent-name chat path, so
/// callers borrow with no allocation in the common case. `Ok(Some(...))`
/// always carries a non-empty Vec — `0` active pids surfaces as
/// `Err(NoInstance)` to keep the "agent does not exist" diagnostic
/// distinct from "multi-pid result with one entry".
pub fn list_active_pid_chat_paths(
    table: &AgentTable,
    path: &str,
) -> Result<Option<Vec<String>>, AgentChatError> {
    let Some(agent_name) = parse_agent_chat_path(path) else {
        return Ok(None);
    };

    let mut active_pids: Vec<String> = table
        .list(None, None, None)
        .into_iter()
        .filter(|d| d.name == agent_name && is_active(&d.state))
        .map(|d| d.pid)
        .collect();

    if active_pids.is_empty() {
        return Err(AgentChatError::NoInstance(agent_name.to_string()));
    }
    active_pids.sort();
    Ok(Some(
        active_pids
            .into_iter()
            .map(|pid| format!("/proc/{pid}/chat-with-me"))
            .collect(),
    ))
}

/// One envelope read off a single pid's `/proc/{pid}/chat-with-me`.
/// `bytes` is the raw stream entry as written by the sender; `pid` is
/// the pid the entry was read from (carried so reads tagged with their
/// source can show "agent-name (instance pid)" in UIs).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatStreamEntry {
    pub pid: String,
    pub bytes: Vec<u8>,
}

/// Merge entries from multiple pids' `chat-with-me` streams into a
/// single chronological sequence. Entries are sorted by their JSON
/// envelope's `ts` field (ISO-8601 string — same shape mailbox stamping
/// emits and AuditRecord uses, which sorts lexicographically the same
/// way it sorts chronologically). Entries with no `ts` field — and any
/// payload that fails to parse as JSON — fall to the end in source
/// order, so receivers always see well-formed timestamped envelopes
/// before any malformed ones rather than the merge silently dropping
/// content.
///
/// Stable sort: entries with equal timestamps keep their pid-order
/// from the input, which keeps readers reading multiple instances
/// from getting flicker on simultaneous writes.
pub fn merge_chat_streams(streams: Vec<Vec<ChatStreamEntry>>) -> Vec<ChatStreamEntry> {
    let mut all: Vec<ChatStreamEntry> = streams.into_iter().flatten().collect();
    all.sort_by(|a, b| {
        let a_ts = extract_ts(&a.bytes);
        let b_ts = extract_ts(&b.bytes);
        match (a_ts, b_ts) {
            (Some(at), Some(bt)) => at.cmp(&bt),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        }
    });
    all
}

fn extract_ts(bytes: &[u8]) -> Option<String> {
    let value: serde_json::Value = serde_json::from_slice(bytes).ok()?;
    let ts = value.as_object()?.get("ts")?;
    ts.as_str().map(|s| s.to_string())
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

    // ── list_active_pid_chat_paths ─────────────────────────────────────

    #[test]
    fn list_active_returns_none_for_non_agent_path() {
        let t = make_table();
        assert_eq!(
            list_active_pid_chat_paths(&t, "/proc/p1/chat-with-me").unwrap(),
            None
        );
        assert_eq!(
            list_active_pid_chat_paths(&t, "/agents/scode-standard/config.toml").unwrap(),
            None
        );
    }

    #[test]
    fn list_active_returns_no_instance_when_empty() {
        let t = make_table();
        let err =
            list_active_pid_chat_paths(&t, "/agents/scode-standard/chat-with-me").unwrap_err();
        assert_eq!(
            err,
            AgentChatError::NoInstance("scode-standard".to_string())
        );
    }

    #[test]
    fn list_active_returns_single_entry_for_one_pid() {
        let t = make_table();
        t.register(descriptor("p1", "scode-standard", AgentState::Ready));
        let r = list_active_pid_chat_paths(&t, "/agents/scode-standard/chat-with-me").unwrap();
        assert_eq!(r, Some(vec!["/proc/p1/chat-with-me".to_string()]));
    }

    // ── merge_chat_streams ─────────────────────────────────────────────

    fn entry(pid: &str, bytes: &[u8]) -> ChatStreamEntry {
        ChatStreamEntry {
            pid: pid.to_string(),
            bytes: bytes.to_vec(),
        }
    }

    #[test]
    fn merge_empty_returns_empty() {
        let merged = merge_chat_streams(Vec::new());
        assert!(merged.is_empty());
    }

    #[test]
    fn merge_single_stream_passthrough() {
        let stream_a = vec![
            entry("p1", br#"{"ts":"2026-04-27T10:00:00.000Z","body":"a1"}"#),
            entry("p1", br#"{"ts":"2026-04-27T10:00:01.000Z","body":"a2"}"#),
        ];
        let merged = merge_chat_streams(vec![stream_a]);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].pid, "p1");
        assert_eq!(merged[1].pid, "p1");
    }

    #[test]
    fn merge_interleaves_by_iso_timestamp() {
        let stream_a = vec![
            entry("p1", br#"{"ts":"2026-04-27T10:00:00.000Z","body":"a1"}"#),
            entry("p1", br#"{"ts":"2026-04-27T10:00:02.000Z","body":"a2"}"#),
        ];
        let stream_b = vec![
            entry("p2", br#"{"ts":"2026-04-27T10:00:01.000Z","body":"b1"}"#),
            entry("p2", br#"{"ts":"2026-04-27T10:00:03.000Z","body":"b2"}"#),
        ];
        let merged = merge_chat_streams(vec![stream_a, stream_b]);
        let pids: Vec<&str> = merged.iter().map(|e| e.pid.as_str()).collect();
        assert_eq!(pids, vec!["p1", "p2", "p1", "p2"]);
    }

    #[test]
    fn merge_is_stable_for_equal_timestamps() {
        let same_ts = br#"{"ts":"2026-04-27T10:00:00.000Z","body":"x"}"#;
        let stream_a = vec![entry("p_a", same_ts)];
        let stream_b = vec![entry("p_b", same_ts)];
        let merged = merge_chat_streams(vec![stream_a, stream_b]);
        // Stable sort: input order (a before b) preserved on tie.
        assert_eq!(merged[0].pid, "p_a");
        assert_eq!(merged[1].pid, "p_b");
    }

    #[test]
    fn merge_pushes_missing_ts_to_end() {
        let stream_a = vec![entry(
            "p1",
            br#"{"ts":"2026-04-27T10:00:00.000Z","body":"timed"}"#,
        )];
        let stream_b = vec![entry("p2", br#"{"body":"no ts"}"#)];
        let merged = merge_chat_streams(vec![stream_b, stream_a]);
        // Even though stream_b is first in input, the timed entry comes first.
        assert_eq!(merged[0].pid, "p1");
        assert_eq!(merged[1].pid, "p2");
    }

    #[test]
    fn merge_handles_non_json_payloads_at_end() {
        let stream_a = vec![entry(
            "p1",
            br#"{"ts":"2026-04-27T10:00:00.000Z","body":"timed"}"#,
        )];
        let stream_b = vec![entry("p_bogus", b"plain text, not json")];
        let merged = merge_chat_streams(vec![stream_b, stream_a]);
        assert_eq!(merged[0].pid, "p1");
        assert_eq!(merged[1].pid, "p_bogus");
    }

    #[test]
    fn list_active_returns_all_active_pids_sorted() {
        let t = make_table();
        // Mix in different states to exercise the active-state filter.
        t.register(descriptor("p_b", "scode-standard", AgentState::Ready));
        t.register(descriptor("p_a", "scode-standard", AgentState::Busy));
        t.register(descriptor("p_c", "scode-standard", AgentState::WarmingUp));
        t.register(descriptor(
            "p_dead",
            "scode-standard",
            AgentState::Terminated,
        ));
        // Different agent name — must be excluded.
        t.register(descriptor("p_other", "scode-fast", AgentState::Ready));

        let r = list_active_pid_chat_paths(&t, "/agents/scode-standard/chat-with-me")
            .unwrap()
            .unwrap();
        assert_eq!(
            r,
            vec![
                "/proc/p_a/chat-with-me".to_string(),
                "/proc/p_b/chat-with-me".to_string(),
                "/proc/p_c/chat-with-me".to_string(),
            ]
        );
    }
}
