//! Procfs-style entry registration for managed-agent pids.
//!
//! Stamps every per-pid metastore entry the integration doc §2.2 lists:
//!
//!   * `/proc/`, `/proc/{pid}/`, `/proc/{pid}/workspace/`,
//!     `/proc/{pid}/sessions/`, `/proc/{pid}/tasks/` — DT_DIR dirents
//!   * `/proc/{pid}/agent` — DT_LINK to `/agents/{desc.name}`
//!     (Linux `/proc/{pid}/exe` analogue; readlink returns the static
//!     profile dir).  May dangle until `/agents/{name}/` is materialised
//!     by upstream profile-management code — kernel does not validate
//!     link target existence.
//!   * `/proc/{pid}/chat-with-me` — DT_STREAM (capacity 65_536), the
//!     canonical mailbox.  io_profile is `wal` when federation is up
//!     (writes raft-replicate across voters per integration doc §6) and
//!     `memory` otherwise (test mode).
//!   * `/proc/{pid}/workspace/chat-with-me` — DT_LINK to
//!     `/proc/{pid}/chat-with-me` (workspace shortcut so agents can
//!     address `chat-with-me` relative to their cwd)
//!   * `/proc/{pid}/workspace/{alias}` — one DT_LINK per
//!     `RepoMount` in the descriptor, target is `mount_path`
//!
//! VFSRouter follows DT_LINK rows transparently on `sys_read` / `sys_write`
//! (single-hop, ELOOP-detected), so the existing kernel hooks
//! (`MailboxStampingHook`, `WorkspaceBoundaryHook`, `AuditHook`) match
//! on the link path's suffix and behave identically whether the caller
//! writes to the canonical pid-level path or through the workspace
//! shortcut.

use contracts::OperationContext;

use kernel::abi::KernelAbi;
use kernel::core::agents::registry::AgentDescriptor;

const DT_DIR: i32 = 1;
const DT_STREAM: i32 = 4;
const DT_LINK: i32 = 6;

/// chat-with-me DT_STREAM capacity — sized for the per-pid conversation
/// flow described in integration doc §3.
const CHAT_STREAM_CAPACITY: usize = 65_536;

/// `OperationContext` proc_entry uses for its teardown `sys_unlink`
/// calls. `is_system = true` + `is_admin = true` so the unlink
/// bypasses ReBAC checks — proc_entry teardown is infrastructure,
/// not a user-issued op. `agent_id = None` so the empty-caller
/// branch in `WorkspaceBoundaryHook::on_pre` lets the system unlink
/// past the per-pid boundary check (the hook only constrains
/// agent-attributed writes).
fn sys_ctx() -> OperationContext {
    let mut ctx = OperationContext::new(
        /* user_id */ "managed_agent",
        /* zone_id */ "root",
        /* is_admin */ true,
        /* agent_id */ None,
        /* is_system */ true,
    );
    ctx.subject_type = "service".to_string();
    ctx
}

/// Stamp the per-pid metastore subtree at start_session time. Idempotent
/// against re-spawn / restart paths — every `sys_setattr` call accepts
/// a matching existing entry as a successful no-op.
pub(crate) fn register_proc_entry<K: KernelAbi>(
    kernel: &K,
    desc: &AgentDescriptor,
) -> Result<(), String> {
    let pid = desc.pid.as_str();
    let pid_root = format!("/proc/{pid}");
    let workspace_root = format!("/proc/{pid}/workspace");
    let sessions_root = format!("/proc/{pid}/sessions");
    let tasks_root = format!("/proc/{pid}/tasks");

    // Dirent layer first — children attach below.
    for dir in [
        "/proc",
        pid_root.as_str(),
        workspace_root.as_str(),
        sessions_root.as_str(),
        tasks_root.as_str(),
    ] {
        create_dt_dir(kernel, dir)?;
    }

    // Canonical chat-with-me stream — wal-backed when federation is up
    // so writes raft-replicate, in-memory otherwise (test mode).
    let cwm_canonical = format!("/proc/{pid}/chat-with-me");
    let profile = if kernel.is_federation_initialized() {
        "wal"
    } else {
        "memory"
    };
    create_dt_stream(kernel, &cwm_canonical, CHAT_STREAM_CAPACITY, profile)?;

    // /proc/{pid}/agent → /agents/{desc.name} (Linux /proc/{pid}/exe
    // analogue). Target may not exist yet; DT_LINK rows are not
    // validated against entry presence.
    let agent_link = format!("{pid_root}/agent");
    let agent_target = format!("/agents/{}", desc.name);
    create_dt_link(kernel, &agent_link, &agent_target)?;

    // Workspace `chat-with-me` shortcut → canonical pid-level stream.
    let cwm_shortcut = format!("{workspace_root}/chat-with-me");
    create_dt_link(kernel, &cwm_shortcut, &cwm_canonical)?;

    // One DT_LINK per repo mount carried in the descriptor.
    for repo in &desc.repos {
        let alias_link = format!("{workspace_root}/{}", repo.alias);
        create_dt_link(kernel, &alias_link, &repo.mount_path)?;
    }

    Ok(())
}

/// Reverse of [`register_proc_entry`]. Best-effort: missing entries
/// (e.g. partial registration that failed) are not an error. Children
/// drop before parents so directory removal sees an empty parent. The
/// canonical chat-with-me DT_STREAM also goes here — its lifetime is
/// the pid's; any persistent inbox lives at `/agents/{name}/chat-with-me`
/// instead.
pub(crate) fn unregister_proc_entry<K: KernelAbi>(kernel: &K, desc: &AgentDescriptor) {
    let pid = desc.pid.as_str();
    let pid_root = format!("/proc/{pid}");
    let workspace_root = format!("/proc/{pid}/workspace");
    let sessions_root = format!("/proc/{pid}/sessions");
    let tasks_root = format!("/proc/{pid}/tasks");
    let ctx = sys_ctx();

    // Workspace shortcut + alias links first, then the workspace dir.
    let _ = kernel.sys_unlink(&format!("{workspace_root}/chat-with-me"), &ctx, false);
    for repo in &desc.repos {
        let _ = kernel.sys_unlink(&format!("{workspace_root}/{}", repo.alias), &ctx, false);
    }
    let _ = kernel.sys_unlink(&workspace_root, &ctx, false);

    // Sessions / tasks are leaf dirents from this layer's perspective —
    // any sub-entries are sudo-code's bookkeeping, dropped here as a
    // bulk-delete because the pid is going away.
    let _ = kernel.sys_unlink(&sessions_root, &ctx, false);
    let _ = kernel.sys_unlink(&tasks_root, &ctx, false);

    // Canonical chat-with-me stream + agent link, then pid root itself.
    let _ = kernel.sys_unlink(&format!("{pid_root}/chat-with-me"), &ctx, false);
    let _ = kernel.sys_unlink(&format!("{pid_root}/agent"), &ctx, false);
    let _ = kernel.sys_unlink(&pid_root, &ctx, false);
}

fn create_dt_dir<K: KernelAbi>(kernel: &K, path: &str) -> Result<(), String> {
    kernel
        .sys_setattr(
            path,
            DT_DIR,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            /* io_profile */ "memory",
            /* zone_id */ "root",
            /* is_external */ false,
            /* capacity */ 0,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* link_target */ None,
            /* source */ None,
            /* remote_metastore */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_DIR at {path:?}): {e:?}"))
}

fn create_dt_link<K: KernelAbi>(kernel: &K, path: &str, target: &str) -> Result<(), String> {
    kernel
        .sys_setattr(
            path,
            DT_LINK,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            /* io_profile */ "memory",
            /* zone_id */ "root",
            /* is_external */ false,
            /* capacity */ 0,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* link_target */ Some(target),
            /* source */ None,
            /* remote_metastore */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_LINK at {path:?} → {target:?}): {e:?}"))
}

fn create_dt_stream<K: KernelAbi>(
    kernel: &K,
    path: &str,
    capacity: usize,
    io_profile: &str,
) -> Result<(), String> {
    kernel
        .sys_setattr(
            path,
            DT_STREAM,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            io_profile,
            /* zone_id */ "root",
            /* is_external */ false,
            capacity,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* link_target */ None,
            /* source */ None,
            /* remote_metastore */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_STREAM at {path:?} io_profile={io_profile:?}): {e:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::core::agents::registry::{AgentKind, AgentState, RepoMount};
    use kernel::kernel::Kernel;

    fn dir_exists(kernel: &Kernel, path: &str) -> bool {
        kernel
            .metastore_get(path)
            .ok()
            .flatten()
            .is_some_and(|e| e.entry_type == DT_DIR as u8)
    }

    fn link_target(kernel: &Kernel, path: &str) -> Option<String> {
        kernel
            .metastore_get(path)
            .ok()
            .flatten()
            .filter(|e| e.entry_type == DT_LINK as u8)
            .and_then(|e| e.link_target)
    }

    fn stream_exists(kernel: &Kernel, path: &str) -> bool {
        kernel
            .metastore_get(path)
            .ok()
            .flatten()
            .is_some_and(|e| e.entry_type == DT_STREAM as u8)
    }

    fn entry_present(kernel: &Kernel, path: &str) -> bool {
        kernel.metastore_get(path).ok().flatten().is_some()
    }

    #[test]
    fn register_proc_entry_creates_full_per_pid_dirent_layer() {
        let kernel = Kernel::new();
        let desc = AgentDescriptor {
            pid: "p1".to_string(),
            name: "managed-claude".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id: "system".to_string(),
            zone_id: "root".to_string(),
            ..Default::default()
        };
        register_proc_entry(&kernel, &desc).unwrap();

        for dir in [
            "/proc",
            "/proc/p1",
            "/proc/p1/workspace",
            "/proc/p1/sessions",
            "/proc/p1/tasks",
        ] {
            assert!(dir_exists(&kernel, dir), "dirent missing: {dir}");
        }
        assert!(stream_exists(&kernel, "/proc/p1/chat-with-me"));
        assert_eq!(
            link_target(&kernel, "/proc/p1/agent").as_deref(),
            Some("/agents/managed-claude"),
        );
        assert_eq!(
            link_target(&kernel, "/proc/p1/workspace/chat-with-me").as_deref(),
            Some("/proc/p1/chat-with-me"),
        );
    }

    #[test]
    fn register_proc_entry_stamps_one_dt_link_per_repo() {
        let kernel = Kernel::new();
        let desc = AgentDescriptor {
            pid: "p2".to_string(),
            name: "managed-claude".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id: "system".to_string(),
            zone_id: "root".to_string(),
            repos: vec![
                RepoMount {
                    alias: "alpha".to_string(),
                    mount_path: "/repos/alpha".to_string(),
                },
                RepoMount {
                    alias: "beta".to_string(),
                    mount_path: "/repos/beta".to_string(),
                },
            ],
            ..Default::default()
        };
        register_proc_entry(&kernel, &desc).unwrap();

        assert_eq!(
            link_target(&kernel, "/proc/p2/workspace/alpha").as_deref(),
            Some("/repos/alpha"),
        );
        assert_eq!(
            link_target(&kernel, "/proc/p2/workspace/beta").as_deref(),
            Some("/repos/beta"),
        );
    }

    #[test]
    fn register_proc_entry_is_idempotent() {
        let kernel = Kernel::new();
        let desc = AgentDescriptor {
            pid: "p3".to_string(),
            name: "managed-claude".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id: "system".to_string(),
            zone_id: "root".to_string(),
            ..Default::default()
        };
        register_proc_entry(&kernel, &desc).unwrap();
        register_proc_entry(&kernel, &desc).unwrap();
    }

    #[test]
    fn unregister_proc_entry_removes_full_per_pid_subtree() {
        let kernel = Kernel::new();
        // sys_unlink routes through VFSRouter; the production
        // /proc namespace is mounted by `install_returning` in
        // managed_agent/mod.rs. This unit test calls register +
        // unregister directly without going through install, so it
        // mounts /proc itself to mirror the production setup.
        kernel
            .vfs_router_arc()
            .add_mount("/proc", "root", None, false);
        let desc = AgentDescriptor {
            pid: "p4".to_string(),
            name: "managed-claude".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id: "system".to_string(),
            zone_id: "root".to_string(),
            repos: vec![RepoMount {
                alias: "main".to_string(),
                mount_path: "/repos/main".to_string(),
            }],
            ..Default::default()
        };
        register_proc_entry(&kernel, &desc).unwrap();
        unregister_proc_entry(&kernel, &desc);

        for path in [
            "/proc/p4",
            "/proc/p4/workspace",
            "/proc/p4/workspace/main",
            "/proc/p4/workspace/chat-with-me",
            "/proc/p4/sessions",
            "/proc/p4/tasks",
            "/proc/p4/agent",
            "/proc/p4/chat-with-me",
        ] {
            assert!(
                !entry_present(&kernel, path),
                "{path} still present after unregister"
            );
        }
    }
}
