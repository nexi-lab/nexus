//! Procfs-style entry registration for managed-agent pids.
//!
//! Materialises only the dirent + stat for `/proc/{pid}/` and
//! `/proc/{pid}/workspace/`.  Content of those subtrees (chat-with-me
//! link, per-repo links) is composed by `ProcWorkspaceResolver` at
//! read time from `AgentDescriptor.repos` and the fixed mailbox
//! convention — never persisted in the metastore.
//!
//! Mirrors how Linux procfs works: kernel owns the dirent + stat layer
//! (visible to `readdir` / `stat`), procfs handlers compose file
//! contents on demand from in-kernel state.

use std::sync::Arc;

use kernel::kernel::Kernel;

const DT_DIR: i32 = 1;

/// Stamp the dirent + stat for `/proc/`, `/proc/{pid}/`, and
/// `/proc/{pid}/workspace/`. Idempotent — `sys_setattr(DT_DIR)` is a
/// no-op on already-present directories so duplicate registrations from
/// re-spawn / restart paths are safe.
pub(crate) fn register_proc_entry(kernel: &Arc<Kernel>, pid: &str) -> Result<(), String> {
    let pid_root = format!("/proc/{pid}");
    let workspace_root = format!("/proc/{pid}/workspace");
    for dir in ["/proc", pid_root.as_str(), workspace_root.as_str()] {
        create_dt_dir(kernel, dir)?;
    }
    Ok(())
}

/// Reverse of [`register_proc_entry`]. Best-effort: missing entries
/// (e.g. partial registration that failed) are not an error.  Drops
/// `/proc/{pid}/workspace/` first, then `/proc/{pid}/` so directory
/// removal sees an empty parent.
pub(crate) fn unregister_proc_entry(kernel: &Arc<Kernel>, pid: &str) {
    let workspace_root = format!("/proc/{pid}/workspace");
    let pid_root = format!("/proc/{pid}");
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

#[cfg(test)]
mod tests {
    use super::*;
    use kernel::kernel::Kernel;

    fn dir_exists(kernel: &Kernel, path: &str) -> bool {
        kernel
            .metastore_get(path)
            .ok()
            .flatten()
            .is_some_and(|e| e.entry_type == 1)
    }

    fn link_exists(kernel: &Kernel, path: &str) -> bool {
        kernel
            .metastore_get(path)
            .ok()
            .flatten()
            .is_some_and(|e| e.entry_type == 6)
    }

    #[test]
    fn register_proc_entry_creates_pid_and_workspace_dirents() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, "pid-test").expect("register_proc_entry");
        assert!(dir_exists(&kernel, "/proc"));
        assert!(dir_exists(&kernel, "/proc/pid-test"));
        assert!(dir_exists(&kernel, "/proc/pid-test/workspace"));
    }

    #[test]
    fn register_proc_entry_does_not_materialise_chat_with_me() {
        // The whole point of the procfs view: workspace/chat-with-me is
        // derived by ProcWorkspaceResolver, not stamped into metastore.
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, "pid-test").expect("register_proc_entry");
        assert!(!link_exists(
            &kernel,
            "/proc/pid-test/workspace/chat-with-me"
        ));
        assert!(kernel
            .metastore_get("/proc/pid-test/workspace/chat-with-me")
            .ok()
            .flatten()
            .is_none());
    }

    #[test]
    fn register_proc_entry_is_idempotent() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, "pid-x").expect("first");
        register_proc_entry(&kernel, "pid-x").expect("second call should not error");
        assert!(dir_exists(&kernel, "/proc/pid-x/workspace"));
    }

    #[test]
    fn unregister_proc_entry_removes_dirents() {
        let kernel = Arc::new(Kernel::new());
        register_proc_entry(&kernel, "pid-y").unwrap();
        assert!(dir_exists(&kernel, "/proc/pid-y/workspace"));
        unregister_proc_entry(&kernel, "pid-y");
        assert!(!dir_exists(&kernel, "/proc/pid-y/workspace"));
        assert!(!dir_exists(&kernel, "/proc/pid-y"));
    }

    #[test]
    fn unregister_proc_entry_is_idempotent_on_missing_pid() {
        let kernel = Arc::new(Kernel::new());
        // No prior register call — must not panic / surface an error.
        unregister_proc_entry(&kernel, "pid-ghost");
    }
}
