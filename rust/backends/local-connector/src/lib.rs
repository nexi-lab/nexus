//! `nexus-local-connector` — local-folder reference connector dylib.
//!
//! Loaded by `nexusd-cluster` at startup via `--plugin-dir`. Registers
//! as a driver named `"local-connector"` through the plugin ABI's
//! [`declare_driver_plugin!`] macro.
//!
//! ## Mounting is a syscall — boot OR runtime, not operator-boot-only
//!
//! A mount of this driver is a `sys_setattr(DT_MOUNT, backend_name =
//! "local-connector", …)` carrying the `local_root` config, so it can be
//! created — and torn down — at **runtime** (e.g. a session launcher
//! mounting a per-session workspace), not just at startup. The
//! `--mount-driver local-connector:<zone>:<vfs-path>:<config-json>` boot
//! flag is only a convenience that composes exactly that syscall at startup
//! for the operator; it is NOT the only path to a mount.
//!
//! ## Config schema
//!
//! ```json
//! {
//!   "local_root": "/host/tasks",
//!   "follow_symlinks": true
//! }
//! ```
//!
//! - `local_root` (required, string) — host filesystem path exposed
//!   into the VFS at the mount point.
//! - `follow_symlinks` (optional, bool, default `true`) — passes
//!   through to `LocalConnectorBackend`'s symlink-following policy.
//!   Symlink escape detection (resolved path must stay under
//!   `local_root`) is always enforced.

use std::path::Path;
use std::sync::Arc;

use kernel::abc::object_store::ObjectStore;
use kernel::kernel::OperationContext;
use nexus_plugin_abi::{declare_driver_plugin, KernelHandle};

use backends::storage::local_connector::LocalConnectorBackend;

/// Driver state: just the configured backend.  `OperationContext` is
/// rebuilt on every call because the backend's trait method takes one
/// — the driver itself runs as the kernel-system identity (no auth
/// needed; the kernel boundary already authorized the syscall before
/// dispatching to the driver).
struct LocalConnectorDriver {
    backend: Arc<LocalConnectorBackend>,
}

fn create_local_connector(
    _kernel: &KernelHandle,
    config_json: &str,
) -> Result<Box<LocalConnectorDriver>, i32> {
    let cfg: serde_json::Value = serde_json::from_str(config_json).map_err(|e| {
        tracing::error!(err = %e, config = config_json, "local-connector: config JSON parse failed");
        -2 // PluginResult::InvalidArgument
    })?;

    let local_root = cfg
        .get("local_root")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            tracing::error!(config = ?cfg, "local-connector: missing required 'local_root' field");
            -2_i32
        })?;
    let follow_symlinks = cfg
        .get("follow_symlinks")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);

    // fsync is intentionally off for v1 — the cc-tasks-share use case
    // is operator-driven sync (Claude Code rewrites tasks via plain
    // file writes) and fsync per write would dominate latency on a
    // busy editor.  Toggleable through config if a durability-bound
    // caller materializes.
    let backend = LocalConnectorBackend::new(Path::new(local_root), follow_symlinks, false)
        .map_err(|e| {
            tracing::error!(err = %e, local_root, "local-connector: backend init failed");
            -3 // PluginResult::Internal
        })?;

    tracing::info!(
        local_root,
        follow_symlinks,
        "local-connector driver instance created",
    );

    Ok(Box::new(LocalConnectorDriver {
        backend: Arc::new(backend),
    }))
}

fn read_local_connector(drv: &LocalConnectorDriver, path: &str) -> Result<Vec<u8>, i32> {
    let ctx = system_ctx();
    drv.backend
        .read_content(path, &ctx)
        .map_err(storage_error_to_plugin_code)
}

fn write_local_connector(drv: &LocalConnectorDriver, path: &str, data: &[u8]) -> Result<(), i32> {
    let ctx = system_ctx();
    drv.backend
        .write_content(data, path, &ctx, 0)
        .map(|_| ())
        .map_err(storage_error_to_plugin_code)
}

/// Enumerate immediate children at `path` (relative to `local_root`).
///
/// Wraps the existing `LocalConnectorBackend::list_dir` — the
/// `ObjectStore` impl already produced the right wire shape
/// (`Vec<String>` with trailing `/` for directories), so the bridge
/// is one function call.  Opting in via the `declare_driver_plugin!`
/// `readdir:` arm makes the kernel's `sys_readdir` surface
/// `local_root` contents under the mount path — what the
/// cc-tasks-share Mac↔Win flow needs to find Claude Code session
/// directories.
fn readdir_local_connector(drv: &LocalConnectorDriver, path: &str) -> Result<Vec<String>, i32> {
    drv.backend
        .list_dir(path)
        .map_err(storage_error_to_plugin_code)
}

/// Remove a backend file by path (sister of `write_local_connector`).
///
/// Delegates to `LocalConnectorBackend::delete_file`, which after
/// the standard path-escape check calls `fs::remove_file`.  Opting
/// into the `delete_file:` arm of `declare_driver_plugin!` closes
/// the FUSE-`rm`-leaves-ghost-file gap: pre-opt-in, `sys_unlink`
/// removed the metastore entry but the host fs file persisted, and
/// the now-working `list_dir` re-surfaced it on the next ls.
fn delete_file_local_connector(drv: &LocalConnectorDriver, path: &str) -> Result<(), i32> {
    drv.backend
        .delete_file(path)
        .map_err(storage_error_to_plugin_code)
}

/// Remove a backend directory by path (sister of `delete_file_local_connector`).
///
/// Plugin ABI v5 (nexus-vfs#70's C1) extends `nexus_driver_rmdir` with
/// the `recursive` flag the `ObjectStore::rmdir` trait already carries.
/// We thread it straight through so `sys_rmdir(path, recursive=true)`
/// reaches the host fs as a single `fs::remove_dir_all` call — one
/// syscall family instead of the kernel walking children and calling
/// us N+1 times.
fn rmdir_local_connector(
    drv: &LocalConnectorDriver,
    path: &str,
    recursive: bool,
) -> Result<(), i32> {
    drv.backend
        .rmdir(path, recursive)
        .map_err(storage_error_to_plugin_code)
}

/// Point-lookup metadata for `path` — returns `(size, is_dir)`.
///
/// Delegates to `LocalConnectorBackend::stat`, which uses
/// `fs::metadata` — O(1).  Opting into the `stat:` arm of
/// `declare_driver_plugin!` lets the kernel's `sys_stat` backend
/// fallback give callers the real size for backend-only files
/// (host fs entries Claude Code wrote directly), instead of the
/// trait-default `NotSupported` that would surface as ENOENT
/// against the FUSE layer.
fn stat_local_connector(drv: &LocalConnectorDriver, path: &str) -> Result<(u64, bool), i32> {
    drv.backend
        .stat(path)
        .map(|s| (s.size, s.is_dir))
        .map_err(storage_error_to_plugin_code)
}

fn system_ctx() -> OperationContext {
    // Driver runs inside the kernel's trust boundary; the syscall's
    // OperationContext already authorized the request upstream.  We
    // synthesize a `system` ctx here so the backend's permission
    // surface short-circuits — same shape kernel/plugins/mod.rs uses
    // for its KernelHandle callbacks.
    OperationContext::new("", kernel::ROOT_ZONE_ID, true, None, true)
}

fn storage_error_to_plugin_code(err: kernel::abc::object_store::StorageError) -> i32 {
    use kernel::abc::object_store::StorageError;
    match err {
        StorageError::NotFound(_) => -1,     // PluginResult::NotFound
        StorageError::NotSupported(_) => -2, // PluginResult::InvalidArgument
        StorageError::IOError(_) => -3,      // PluginResult::Internal
    }
}

declare_driver_plugin!("local-connector", LocalConnectorDriver, {
    create: create_local_connector,
    read: read_local_connector,
    write: write_local_connector,
    readdir: readdir_local_connector,
    delete_file: delete_file_local_connector,
    rmdir: rmdir_local_connector,
    stat: stat_local_connector,
});

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::raw::c_void;
    use tempfile::TempDir;

    // ── KernelHandle test stub ──────────────────────────────────────
    //
    // KernelHandle's callback fields are non-null `fn` pointers, so
    // `std::mem::zeroed()` is UB.  Our `create_local_connector` impl
    // never invokes the callbacks, but the struct still has to look
    // valid to the borrow checker — these no-op fns sit at known
    // addresses and never run.

    unsafe extern "C" fn stub_sys_read(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_write(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *const u8,
        _: usize,
    ) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_stat(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_readdir(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_path(_: *const c_void, _: *const std::ffi::c_char) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_rename(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *const std::ffi::c_char,
    ) -> i32 {
        -3
    }

    unsafe extern "C" fn stub_sys_stat_batch(
        _: *const c_void,
        _: *const std::ffi::c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -3
    }

    fn stub_handle() -> KernelHandle {
        KernelHandle {
            sys_read: stub_sys_read,
            sys_write: stub_sys_write,
            sys_stat: stub_sys_stat,
            sys_readdir: stub_sys_readdir,
            sys_unlink: stub_sys_path,
            sys_mkdir: stub_sys_path,
            sys_rmdir: stub_sys_path,
            sys_rename: stub_sys_rename,
            sys_stat_batch: stub_sys_stat_batch,
            free_buf: nexus_plugin_abi::nexus_free,
            kernel_ptr: std::ptr::null(),
        }
    }

    /// Bypasses dlopen — exercises the same `create_local_connector` +
    /// `read_local_connector` / `write_local_connector` callbacks the
    /// declare_driver_plugin! macro wires into the C ABI symbols.
    fn fresh_driver_with_root(tmp: &TempDir) -> Box<LocalConnectorDriver> {
        let root = tmp.path().to_str().expect("utf-8 tmp path");
        let config = format!(r#"{{"local_root":"{}"}}"#, root.replace('\\', "/"));
        let kernel_handle = stub_handle();
        create_local_connector(&kernel_handle, &config).expect("create_local_connector OK")
    }

    #[test]
    fn create_rejects_missing_local_root() {
        let kernel_handle = stub_handle();
        match create_local_connector(&kernel_handle, "{}") {
            Ok(_) => panic!("expected missing local_root to fail"),
            Err(code) => {
                assert_eq!(
                    code, -2,
                    "missing local_root must surface as InvalidArgument"
                )
            }
        }
    }

    #[test]
    fn create_rejects_bad_json() {
        let kernel_handle = stub_handle();
        match create_local_connector(&kernel_handle, "not-a-json") {
            Ok(_) => panic!("expected malformed JSON to fail"),
            Err(code) => assert_eq!(code, -2, "malformed JSON must surface as InvalidArgument"),
        }
    }

    #[test]
    fn write_then_read_roundtrip() {
        let tmp = TempDir::new().unwrap();
        let drv = fresh_driver_with_root(&tmp);

        write_local_connector(&drv, "/abc/1.json", b"{\"task\":\"hello\"}").expect("write OK");

        let bytes = read_local_connector(&drv, "/abc/1.json").expect("read OK");
        assert_eq!(bytes, b"{\"task\":\"hello\"}".to_vec());

        // Confirm bytes landed on the real filesystem at the expected
        // path — this is the connector's "reference mode" SSOT
        // property; the driver must not copy / shadow.
        let physical = tmp.path().join("abc").join("1.json");
        assert!(
            physical.exists(),
            "physical file at {} must exist",
            physical.display()
        );
        assert_eq!(fs::read(&physical).unwrap(), b"{\"task\":\"hello\"}");
    }

    #[test]
    fn read_missing_path_is_not_found() {
        let tmp = TempDir::new().unwrap();
        let drv = fresh_driver_with_root(&tmp);

        let err = read_local_connector(&drv, "/never/written.json").unwrap_err();
        assert_eq!(err, -1, "missing file must map to PluginResult::NotFound");
    }

    #[test]
    fn symlink_escape_is_rejected() {
        // Reproduces the security property documented at
        // backends/src/storage/local_connector.rs:71 — resolved path
        // must stay under mount root.  Path-traversal attempts
        // surface as IOError (PluginResult::Internal == -3).
        let tmp = TempDir::new().unwrap();
        let drv = fresh_driver_with_root(&tmp);

        let err = read_local_connector(&drv, "/../escape.json").unwrap_err();
        assert_eq!(err, -3, "path-traversal must be rejected, not pass through");
    }
}
