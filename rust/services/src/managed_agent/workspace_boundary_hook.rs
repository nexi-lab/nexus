//! WorkspaceBoundaryHook — INTERCEPT pre-write hook that enforces the
//! "you cannot write into another agent's workspace" convention.
//!
//! Path layout (sudowork/docs/tech/nexus-integration-architecture.md §3.4):
//!
//!     /proc/{owner_pid}/workspace/...   ← owned by the pid named in the path
//!
//! When a write target falls under that prefix, the hook compares the
//! workspace owner pid (extracted from the path) against the caller's
//! `agent_id`. On mismatch the hook returns `Err` with a structured
//! teaching payload pointing at the canonical mailbox path so the caller
//! (an LLM) learns the convention from the error itself rather than from
//! its system prompt or memory.
//!
//! Writes whose target is the workspace owner's *own* `chat-with-me`
//! stream are allowed regardless of identity — that path is the
//! advertised mailbox and exists precisely so other agents can write to
//! it without being inside the workspace.

use kernel::core::dispatch::{HookContext, HookOutcome, NativeInterceptHook};

/// Path prefix that scopes this hook. Anything under `/proc/{pid}/workspace/`
/// is governed by the workspace boundary check. Other paths short-circuit
/// in `is_workspace_path` so the hook is zero-cost outside its scope.
const WORKSPACE_PREFIX: &str = "/proc/";
const WORKSPACE_SEGMENT: &str = "/workspace/";

/// INTERCEPT pre-write hook scoped to `/proc/{pid}/workspace/`.
///
/// Stateless — the hook reads the workspace owner from the path and the
/// caller from the dispatch context, so a single instance covers every
/// workspace in the kernel.
pub(crate) struct WorkspaceBoundaryHook;

impl WorkspaceBoundaryHook {
    pub(crate) fn new() -> Self {
        Self
    }

    /// Extract the workspace owner pid from a path. Returns `Some(pid)`
    /// when the path is `/proc/{pid}/workspace/...`, `None` otherwise.
    fn owner_pid(path: &str) -> Option<&str> {
        let after_proc = path.strip_prefix(WORKSPACE_PREFIX)?;
        let slash = after_proc.find('/')?;
        let pid = &after_proc[..slash];
        let rest = &after_proc[slash..];
        if !rest.starts_with(WORKSPACE_SEGMENT) {
            return None;
        }
        if pid.is_empty() {
            return None;
        }
        Some(pid)
    }

    /// True when the path is the workspace owner's own `chat-with-me`
    /// (or its DT_LINK shortcut). These are the advertised entry points
    /// for outside agents and must remain writable to non-owners.
    fn is_chat_with_me(path: &str, owner_pid: &str) -> bool {
        let canonical = format!("{WORKSPACE_PREFIX}{owner_pid}/chat-with-me");
        let workspace = format!("{WORKSPACE_PREFIX}{owner_pid}{WORKSPACE_SEGMENT}chat-with-me");
        path == canonical || path == workspace
    }

    /// Build the structured teaching error the hook returns on cross-owner
    /// writes. The format mirrors the doc so reviewers can grep for it.
    fn teaching_error(path: &str, owner_pid: &str, caller_agent_id: &str) -> String {
        format!(
            "EPERM at {path}: This workspace is owned by pid '{owner_pid}'. \
             You are '{caller_agent_id}'. To send a message about this workspace, \
             write to: {WORKSPACE_PREFIX}{owner_pid}{WORKSPACE_SEGMENT}chat-with-me \
             (or address the owner directly at {WORKSPACE_PREFIX}{owner_pid}/chat-with-me).",
        )
    }
}

impl NativeInterceptHook for WorkspaceBoundaryHook {
    fn name(&self) -> &str {
        "workspace_boundary"
    }

    fn on_pre(&self, ctx: &HookContext) -> Result<HookOutcome, String> {
        // Only mutating-write contexts gate the boundary; reads, stat, and
        // other no-mutation ops walk through.
        let path = match ctx {
            HookContext::Write(_)
            | HookContext::Delete(_)
            | HookContext::Mkdir(_)
            | HookContext::Rmdir(_)
            | HookContext::Rename(_)
            | HookContext::Copy(_)
            | HookContext::WriteBatch(_) => ctx.path(),
            _ => return Ok(HookOutcome::Pass),
        };

        let owner_pid = match Self::owner_pid(path) {
            Some(p) => p,
            None => return Ok(HookOutcome::Pass),
        };

        if Self::is_chat_with_me(path, owner_pid) {
            return Ok(HookOutcome::Pass);
        }

        let caller = &ctx.identity().agent_id;
        if caller == owner_pid || caller.is_empty() {
            return Ok(HookOutcome::Pass);
        }

        Err(Self::teaching_error(path, owner_pid, caller))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::core::dispatch::{HookIdentity, ReadHookCtx, WriteHookCtx};

    fn ctx(path: &str, caller: &str) -> HookContext {
        HookContext::Write(WriteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                agent_id: caller.to_string(),
                user_id: caller.to_string(),
                zone_id: "root".to_string(),
                is_admin: false,
            },
            content: Vec::new(),
            is_new_file: true,
            content_id: None,
            new_version: 1,
            size_bytes: None,
        })
    }

    #[test]
    fn passes_when_caller_owns_workspace() {
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/proc/p1/workspace/notes.md", "p1");
        assert!(hook.on_pre(&c).is_ok());
    }

    #[test]
    fn passes_for_chat_with_me_link_target_in_workspace() {
        let hook = WorkspaceBoundaryHook::new();
        // chat-with-me inside the workspace is the DT_LINK shortcut and
        // is the advertised entry point — non-owners must be able to
        // write to it.
        let c = ctx("/proc/p1/workspace/chat-with-me", "stranger");
        assert!(hook.on_pre(&c).is_ok());
    }

    #[test]
    fn passes_for_canonical_chat_with_me() {
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/proc/p1/chat-with-me", "stranger");
        assert!(hook.on_pre(&c).is_ok());
    }

    #[test]
    fn passes_for_paths_outside_workspace_namespace() {
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/agents/scode-standard/config.toml", "stranger");
        assert!(hook.on_pre(&c).is_ok());
    }

    #[test]
    fn passes_for_pid_namespace_outside_workspace_segment() {
        // /proc/{pid}/agent and /proc/{pid}/sessions/ are runtime metadata
        // paths owned by their pid but not workspace files; the hook only
        // governs the workspace segment.
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/proc/p1/sessions/foo.jsonl", "stranger");
        assert!(hook.on_pre(&c).is_ok());
    }

    #[test]
    fn rejects_cross_owner_write_with_teaching_payload() {
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/proc/p1/workspace/projects/nexus/src/main.rs", "p_other");
        let err = hook.on_pre(&c).unwrap_err();
        assert!(err.contains("EPERM"));
        assert!(err.contains("p1"));
        assert!(err.contains("p_other"));
        assert!(err.contains("chat-with-me"));
    }

    #[test]
    fn read_path_does_not_trigger_boundary() {
        let hook = WorkspaceBoundaryHook::new();
        let read = HookContext::Read(ReadHookCtx {
            path: "/proc/p1/workspace/notes.md".to_string(),
            identity: HookIdentity {
                agent_id: "p_other".to_string(),
                user_id: "p_other".to_string(),
                zone_id: "root".to_string(),
                is_admin: false,
            },
            content: None,
            content_id: None,
        });
        assert!(hook.on_pre(&read).is_ok());
    }

    #[test]
    fn empty_caller_passes_through() {
        // Internal dispatchers without an authenticated caller must not
        // be blocked by the boundary check (kernel writes, recovery, …).
        let hook = WorkspaceBoundaryHook::new();
        let c = ctx("/proc/p1/workspace/notes.md", "");
        assert!(hook.on_pre(&c).is_ok());
    }
}
