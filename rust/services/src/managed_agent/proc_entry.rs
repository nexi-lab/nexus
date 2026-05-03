//! Procfs-style entry registration for managed-agent pids.
//!
//! Stamps every per-pid metastore entry the integration doc §2.2 lists:
//!
//!   * `/proc/`, `/proc/{pid}/`, `/proc/{pid}/workspace/` — DT_DIR dirents
//!   * `/proc/{pid}/workspace/chat-with-me` — DT_LINK to
//!     `/proc/{pid}/chat-with-me` (canonical mailbox stream lives at the
//!     pid-level path; the workspace shortcut lets agents address
//!     `chat-with-me` relative to their cwd)
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
const DT_LINK: i32 = 6;

/// Stamp the dirent + DT_LINK rows for a freshly-spawned pid. Idempotent
/// against re-spawn / restart paths — `sys_setattr(DT_DIR)` is a no-op
/// on already-present directories, and `sys_setattr(DT_LINK)` accepts a
/// matching existing link as a successful no-op.
pub(crate) fn register_proc_entry(
    kernel: &Arc<Kernel>,
    desc: &AgentDescriptor,
) -> Result<(), String> {
    let pid = desc.pid.as_str();
    let pid_root = format!("/proc/{pid}");
    let workspace_root = format!("/proc/{pid}/workspace");
    for dir in ["/proc", pid_root.as_str(), workspace_root.as_str()] {
        create_dt_dir(kernel, dir)?;
    }

    // Workspace `chat-with-me` shortcut → canonical pid-level stream.
    let cwm_link = format!("{workspace_root}/chat-with-me");
    let cwm_target = format!("/proc/{pid}/chat-with-me");
    create_dt_link(kernel, &cwm_link, &cwm_target)?;

    // One DT_LINK per repo mount carried in the descriptor.
    for repo in &desc.repos {
        let alias_link = format!("{workspace_root}/{}", repo.alias);
        create_dt_link(kernel, &alias_link, &repo.mount_path)?;
    }

    Ok(())
}

/// Reverse of [`register_proc_entry`]. Best-effort: missing entries
/// (e.g. partial registration that failed) are not an error. Children
/// drop before the parent dirent so directory removal sees an empty
/// parent.
pub(crate) fn unregister_proc_entry(kernel: &Arc<Kernel>, desc: &AgentDescriptor) {
    let pid = desc.pid.as_str();
    let workspace_root = format!("/proc/{pid}/workspace");
    let pid_root = format!("/proc/{pid}");

    // Workspace links first, then the workspace dir, then pid root.
    let _ = kernel.metastore_delete(&format!("{workspace_root}/chat-with-me"));
    for repo in &desc.repos {
        let _ = kernel.metastore_delete(&format!("{workspace_root}/{}", repo.alias));
    }
    let _ = kernel.metastore_delete(&workspace_root);
    let _ = kernel.metastore_delete(&pid_root);
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
    fn register_proc_entry_creates_pid_and_workspace_dirents() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, &make_desc("pid-test", Vec::new()))
            .expect("register_proc_entry");
        assert!(dir_exists(&kernel, "/proc"));
        assert!(dir_exists(&kernel, "/proc/pid-test"));
        assert!(dir_exists(&kernel, "/proc/pid-test/workspace"));
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
    fn unregister_proc_entry_removes_dirents_and_links() {
        let kernel = Arc::new(Kernel::new());
        let desc = make_desc(
            "pid-y",
            vec![RepoMount {
                alias: "core".into(),
                mount_path: "/host/core".into(),
            }],
        );
        register_proc_entry(&kernel, &desc).unwrap();
        assert!(entry_present(&kernel, "/proc/pid-y/workspace/chat-with-me"));
        assert!(entry_present(&kernel, "/proc/pid-y/workspace/core"));

        unregister_proc_entry(&kernel, &desc);
        assert!(!entry_present(&kernel, "/proc/pid-y/workspace/chat-with-me"));
        assert!(!entry_present(&kernel, "/proc/pid-y/workspace/core"));
        assert!(!dir_exists(&kernel, "/proc/pid-y/workspace"));
        assert!(!dir_exists(&kernel, "/proc/pid-y"));
    }

    #[test]
    fn unregister_proc_entry_is_idempotent_on_missing_pid() {
        let kernel = Arc::new(Kernel::new());
        unregister_proc_entry(&kernel, &make_desc("pid-ghost", Vec::new()));
    }
}
