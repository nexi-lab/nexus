//! ProcWorkspaceResolver — procfs view over `/{zone}/proc/{pid}/workspace/`.
//!
//! Mirrors `services::agents::status_resolver::AgentStatusResolver` (which
//! serves `/{zone}/proc/{pid}/status`).  The two resolvers cover the same
//! `/proc/{pid}/` prefix and partition by trailing segment: `status` ↔
//! `AgentStatusResolver`, `workspace/...` ↔ `ProcWorkspaceResolver`.
//!
//! Read semantics match readlink: `try_read` returns the link target as
//! UTF-8 bytes — the canonical address the workspace shortcut points
//! at.  Two link kinds are served:
//!
//!   * `workspace/chat-with-me` — fixed mailbox convention, target is
//!     `/proc/{pid}/chat-with-me` (DT_STREAM owned by this pid).
//!   * `workspace/{alias}` — looked up in `AgentDescriptor.repos`;
//!     target is the `mount_path` recorded at start_session time.
//!
//! Reads succeed for any pid present in `AgentRegistry` without any
//! metastore write.  The resolver is the SSOT for workspace link
//! topology; the metastore only carries the dirent + stat for
//! `/proc/{pid}/workspace/` (planted by
//! [`super::proc_entry::register_proc_entry`]).

use std::sync::Arc;

use kernel::core::agents::registry::AgentRegistry;
use kernel::core::dispatch::PathResolver;

const WORKSPACE_SEGMENT: &str = "workspace";
const CHAT_WITH_ME: &str = "chat-with-me";

pub struct ProcWorkspaceResolver {
    table: Arc<AgentRegistry>,
}

impl ProcWorkspaceResolver {
    pub fn new(table: Arc<AgentRegistry>) -> Self {
        Self { table }
    }

    /// Resolve a `/{zone}/proc/{pid}/workspace/{tail}` path to its link
    /// target.  Returns `None` for paths outside the workspace subtree
    /// or when the pid / alias is unknown — the dispatcher then falls
    /// through to the next resolver / the default metastore lookup.
    fn link_target(&self, path: &str) -> Option<String> {
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        // /{zone}/proc/{pid}/workspace/{tail} = 5 segments
        if segments.len() != 5 {
            return None;
        }
        if segments[1] != "proc" || segments[3] != WORKSPACE_SEGMENT {
            return None;
        }
        let pid = segments[2];
        let tail = segments[4];

        if tail == CHAT_WITH_ME {
            // Fixed convention; doesn't depend on the descriptor.  Still
            // gate on the pid being known so reads on a reaped agent
            // return None instead of a dangling target.
            self.table.get(pid)?;
            return Some(format!("/proc/{pid}/chat-with-me"));
        }

        let desc = self.table.get(pid)?;
        desc.repos
            .iter()
            .find(|r| r.alias == tail)
            .map(|r| r.mount_path.clone())
    }
}

impl PathResolver for ProcWorkspaceResolver {
    fn try_read(&self, path: &str) -> Option<Vec<u8>> {
        self.link_target(path).map(|t| t.into_bytes())
    }

    fn try_write(&self, _path: &str, _content: &[u8]) -> Option<()> {
        // Workspace links are read-only descriptors; mutations belong
        // to ManagedAgentService.start_session (descriptor.repos) or
        // the canonical chat-with-me stream (separate kernel path).
        None
    }

    fn try_delete(&self, _path: &str) -> Option<()> {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::core::agents::registry::{
        AgentDescriptor, AgentKind, AgentState, RepoMount,
    };

    fn make_desc(pid: &str, repos: Vec<RepoMount>) -> AgentDescriptor {
        AgentDescriptor {
            pid: pid.to_string(),
            name: "scode-standard".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Ready,
            owner_id: "ethan".to_string(),
            zone_id: "root".to_string(),
            created_at_ms: 1,
            updated_at_ms: 1,
            repos,
            ..Default::default()
        }
    }

    fn fixture(pid: &str, repos: Vec<RepoMount>) -> ProcWorkspaceResolver {
        let table = Arc::new(AgentRegistry::new());
        table.register(make_desc(pid, repos));
        ProcWorkspaceResolver::new(table)
    }

    #[test]
    fn chat_with_me_resolves_to_canonical_pid_path() {
        let resolver = fixture("pid-1", Vec::new());
        let bytes = resolver
            .try_read("/root/proc/pid-1/workspace/chat-with-me")
            .expect("chat-with-me should resolve");
        assert_eq!(String::from_utf8(bytes).unwrap(), "/proc/pid-1/chat-with-me");
    }

    #[test]
    fn repo_alias_resolves_to_descriptor_mount_path() {
        let resolver = fixture(
            "pid-2",
            vec![
                RepoMount {
                    alias: "myrepo".into(),
                    mount_path: "/host/repos/myrepo".into(),
                },
                RepoMount {
                    alias: "another".into(),
                    mount_path: "/host/repos/another".into(),
                },
            ],
        );
        let bytes = resolver
            .try_read("/root/proc/pid-2/workspace/myrepo")
            .expect("repo alias should resolve");
        assert_eq!(String::from_utf8(bytes).unwrap(), "/host/repos/myrepo");

        let bytes = resolver
            .try_read("/root/proc/pid-2/workspace/another")
            .expect("second repo alias should resolve");
        assert_eq!(String::from_utf8(bytes).unwrap(), "/host/repos/another");
    }

    #[test]
    fn unknown_alias_returns_none() {
        let resolver = fixture(
            "pid-3",
            vec![RepoMount {
                alias: "myrepo".into(),
                mount_path: "/host/repos/myrepo".into(),
            }],
        );
        assert!(resolver
            .try_read("/root/proc/pid-3/workspace/ghost")
            .is_none());
    }

    #[test]
    fn unknown_pid_returns_none() {
        let resolver = fixture("pid-4", Vec::new());
        assert!(resolver
            .try_read("/root/proc/pid-other/workspace/chat-with-me")
            .is_none());
    }

    #[test]
    fn paths_outside_workspace_subtree_pass_through() {
        let resolver = fixture("pid-5", Vec::new());
        // /proc/{pid}/status is AgentStatusResolver's territory.
        assert!(resolver.try_read("/root/proc/pid-5/status").is_none());
        // Non-proc paths.
        assert!(resolver.try_read("/root/agents/scode-standard/config").is_none());
        // Wrong segment count (workspace as the leaf — no tail).
        assert!(resolver.try_read("/root/proc/pid-5/workspace").is_none());
        // Nested paths under workspace are not single-link names; the
        // resolver only owns one-hop link entries today.
        assert!(resolver
            .try_read("/root/proc/pid-5/workspace/myrepo/sub/file")
            .is_none());
    }

    #[test]
    fn read_succeeds_without_any_metastore_write() {
        // The whole point of the procfs view: no sys_setattr / no
        // metastore_create_dir before this read.  Resolver only needs
        // the AgentRegistry record planted by start_session.
        let resolver = fixture(
            "pid-6",
            vec![RepoMount {
                alias: "core".into(),
                mount_path: "/host/core".into(),
            }],
        );
        // chat-with-me + repo both resolve straight from in-memory state.
        assert!(resolver
            .try_read("/root/proc/pid-6/workspace/chat-with-me")
            .is_some());
        assert!(resolver
            .try_read("/root/proc/pid-6/workspace/core")
            .is_some());
    }

    #[test]
    fn try_write_and_try_delete_are_inert() {
        let resolver = fixture("pid-7", Vec::new());
        assert!(resolver
            .try_write("/root/proc/pid-7/workspace/chat-with-me", b"x")
            .is_none());
        assert!(resolver
            .try_delete("/root/proc/pid-7/workspace/chat-with-me")
            .is_none());
    }
}
