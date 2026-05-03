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

use std::sync::Arc;

use kernel::core::agents::registry::AgentDescriptor;
use kernel::kernel::Kernel;

const DT_DIR: i32 = 1;
const DT_STREAM: i32 = 4;
const DT_LINK: i32 = 6;

/// chat-with-me DT_STREAM capacity — sized for the per-pid conversation
/// flow described in integration doc §3.
const CHAT_STREAM_CAPACITY: usize = 65_536;

/// Stamp the per-pid metastore subtree at start_session time. Idempotent
/// against re-spawn / restart paths — every `sys_setattr` call accepts
/// a matching existing entry as a successful no-op.
pub(crate) fn register_proc_entry(
    kernel: &Arc<Kernel>,
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
    create_dt_stream(
        kernel,
        &cwm_canonical,
        CHAT_STREAM_CAPACITY,
        chat_stream_profile(kernel),
    )?;

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
pub(crate) fn unregister_proc_entry(kernel: &Arc<Kernel>, desc: &AgentDescriptor) {
    let pid = desc.pid.as_str();
    let pid_root = format!("/proc/{pid}");
    let workspace_root = format!("/proc/{pid}/workspace");
    let sessions_root = format!("/proc/{pid}/sessions");
    let tasks_root = format!("/proc/{pid}/tasks");

    // Workspace shortcut + alias links first, then the workspace dir.
    let _ = kernel.metastore_delete(&format!("{workspace_root}/chat-with-me"));
    for repo in &desc.repos {
        let _ = kernel.metastore_delete(&format!("{workspace_root}/{}", repo.alias));
    }
    let _ = kernel.metastore_delete(&workspace_root);

    // Sessions / tasks are leaf dirents from this layer's perspective —
    // any sub-entries are sudo-code's bookkeeping, dropped here as a
    // bulk-delete because the pid is going away.
    let _ = kernel.metastore_delete(&sessions_root);
    let _ = kernel.metastore_delete(&tasks_root);

    // Canonical chat-with-me stream + agent link, then pid root itself.
    let _ = kernel.metastore_delete(&format!("{pid_root}/chat-with-me"));
    let _ = kernel.metastore_delete(&format!("{pid_root}/agent"));
    let _ = kernel.metastore_delete(&pid_root);
}

/// Pick `wal` when federation is initialised, `memory` otherwise.
/// `is_initialized` is the same readiness probe used by `setattr_mount`
/// — true once `init_from_env` completes, regardless of whether any
/// zones are loaded yet.
fn chat_stream_profile(kernel: &Kernel) -> &'static str {
    if kernel.distributed_coordinator().is_initialized(kernel) {
        "wal"
    } else {
        "memory"
    }
}

fn create_dt_dir(kernel: &Kernel, path: &str) -> Result<(), String> {
    kernel
        .sys_setattr(
            path, DT_DIR, /* backend_name */ "", /* backend */ None,
            /* metastore */ None, /* raft_backend */ None,
            /* io_profile */ "memory", /* zone_id */ "root",
            /* is_external */ false, /* capacity */ 0, /* read_fd */ None,
            /* write_fd */ None, /* mime_type */ None,
            /* modified_at_ms */ None, /* link_target */ None,
            /* source */ None, /* remote_metastore */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_DIR at {path:?}): {e:?}"))
}

fn create_dt_link(kernel: &Kernel, path: &str, target: &str) -> Result<(), String> {
    kernel
        .sys_setattr(
            path, DT_LINK, /* backend_name */ "", /* backend */ None,
            /* metastore */ None, /* raft_backend */ None,
            /* io_profile */ "memory", /* zone_id */ "root",
            /* is_external */ false, /* capacity */ 0, /* read_fd */ None,
            /* write_fd */ None, /* mime_type */ None,
            /* modified_at_ms */ None, /* link_target */ Some(target),
            /* source */ None, /* remote_metastore */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_LINK at {path:?} → {target:?}): {e:?}"))
}

fn create_dt_stream(
    kernel: &Kernel,
    path: &str,
    capacity: usize,
    io_profile: &str,
) -> Result<(), String> {
    kernel
        .sys_setattr(
            path, DT_STREAM, /* backend_name */ "", /* backend */ None,
            /* metastore */ None, /* raft_backend */ None, io_profile,
            /* zone_id */ "root", /* is_external */ false, capacity,
            /* read_fd */ None, /* write_fd */ None, /* mime_type */ None,
            /* modified_at_ms */ None, /* link_target */ None,
            /* source */ None, /* remote_metastore */ None,
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

    fn make_desc(pid: &str, repos: Vec<RepoMount>) -> AgentDescriptor {
        AgentDescriptor {
            pid: pid.to_string(),
            name: "scode-standard".to_string(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id: "ethan".to_string(),
            zone_id: "root".to_string(),
            created_at_ms: 1,
            updated_at_ms: 1,
            repos,
            ..Default::default()
        }
    }

    #[test]
    fn register_proc_entry_creates_full_per_pid_dirent_layer() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-test", Vec::new()))
            .expect("register_proc_entry");
        assert!(dir_exists(&kernel, "/proc"));
        assert!(dir_exists(&kernel, "/proc/pid-test"));
        assert!(dir_exists(&kernel, "/proc/pid-test/workspace"));
        assert!(dir_exists(&kernel, "/proc/pid-test/sessions"));
        assert!(dir_exists(&kernel, "/proc/pid-test/tasks"));
    }

    #[test]
    fn register_proc_entry_stamps_canonical_chat_with_me_dt_stream() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-cwm", Vec::new())).unwrap();
        assert!(stream_exists(&kernel, "/proc/pid-cwm/chat-with-me"));
    }

    #[test]
    fn register_proc_entry_stamps_agent_dt_link_to_profile() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-agent", Vec::new())).unwrap();
        // Target may dangle until /agents/{name} is materialised by
        // upstream profile-management code — the link is stamped
        // unconditionally.
        assert_eq!(
            link_target(&kernel, "/proc/pid-agent/agent").as_deref(),
            Some("/agents/scode-standard"),
        );
    }

    #[test]
    fn register_proc_entry_stamps_workspace_chat_with_me_dt_link() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-1", Vec::new())).unwrap();
        assert_eq!(
            link_target(&kernel, "/proc/pid-1/workspace/chat-with-me").as_deref(),
            Some("/proc/pid-1/chat-with-me"),
        );
    }

    #[test]
    fn register_proc_entry_stamps_one_dt_link_per_repo() {
        let kernel = Arc::new(Kernel::new());
        let repos = vec![
            RepoMount {
                alias: "myrepo".into(),
                mount_path: "/host/repos/myrepo".into(),
            },
            RepoMount {
                alias: "another".into(),
                mount_path: "/host/repos/another".into(),
            },
        ];
        register_proc_entry(&kernel, &make_desc("pid-2", repos)).unwrap();
        assert_eq!(
            link_target(&kernel, "/proc/pid-2/workspace/myrepo").as_deref(),
            Some("/host/repos/myrepo"),
        );
        assert_eq!(
            link_target(&kernel, "/proc/pid-2/workspace/another").as_deref(),
            Some("/host/repos/another"),
        );
    }

    #[test]
    fn register_proc_entry_is_idempotent() {
        let kernel = Arc::new(Kernel::new());
        let desc = make_desc(
            "pid-x",
            vec![RepoMount {
                alias: "core".into(),
                mount_path: "/host/core".into(),
            }],
        );
        register_proc_entry(&kernel, &desc).expect("first");
        register_proc_entry(&kernel, &desc).expect("second call should not error");
        assert!(dir_exists(&kernel, "/proc/pid-x/workspace"));
        assert_eq!(
            link_target(&kernel, "/proc/pid-x/workspace/core").as_deref(),
            Some("/host/core"),
        );
    }

    #[test]
    fn unregister_proc_entry_removes_full_per_pid_subtree() {
        let kernel = Arc::new(Kernel::new());
        let desc = make_desc(
            "pid-y",
            vec![RepoMount {
                alias: "core".into(),
                mount_path: "/host/core".into(),
            }],
        );
        register_proc_entry(&kernel, &desc).unwrap();
        // All entries planted.
        assert!(entry_present(&kernel, "/proc/pid-y/agent"));
        assert!(entry_present(&kernel, "/proc/pid-y/chat-with-me"));
        assert!(entry_present(&kernel, "/proc/pid-y/sessions"));
        assert!(entry_present(&kernel, "/proc/pid-y/tasks"));
        assert!(entry_present(&kernel, "/proc/pid-y/workspace/chat-with-me"));
        assert!(entry_present(&kernel, "/proc/pid-y/workspace/core"));

        unregister_proc_entry(&kernel, &desc);

        // Every per-pid entry gone.
        assert!(!entry_present(&kernel, "/proc/pid-y/agent"));
        assert!(!entry_present(&kernel, "/proc/pid-y/chat-with-me"));
        assert!(!entry_present(&kernel, "/proc/pid-y/sessions"));
        assert!(!entry_present(&kernel, "/proc/pid-y/tasks"));
        assert!(!entry_present(&kernel, "/proc/pid-y/workspace/chat-with-me"));
        assert!(!entry_present(&kernel, "/proc/pid-y/workspace/core"));
        assert!(!dir_exists(&kernel, "/proc/pid-y/workspace"));
        assert!(!dir_exists(&kernel, "/proc/pid-y"));
    }

    #[test]
    fn chat_stream_falls_back_to_memory_without_federation() {
        // Default `Kernel::new()` has no federation initialised, so the
        // canonical chat-with-me stream is created with io_profile=memory
        // and the call succeeds.  Production use installs a real
        // distributed coordinator (set_distributed_coordinator) so the
        // probe selects io_profile=wal — covered by federation e2e.
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-mem", Vec::new()))
            .expect("memory profile must succeed without federation");
        assert!(stream_exists(&kernel, "/proc/pid-mem/chat-with-me"));
    }

    #[test]
    fn unregister_proc_entry_is_idempotent_on_missing_pid() {
        let kernel = Arc::new(Kernel::new());
        unregister_proc_entry(&kernel, &make_desc("pid-ghost", Vec::new()));
    }
}
