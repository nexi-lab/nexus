//! `nexus-fuse-plugin` — FUSE service-plugin dylib.
//!
//! Loaded by `nexusd-cluster` at startup via `--plugin-dir`.  Registers
//! as a service plugin named `"fuse"` through the plugin ABI's
//! `declare_service_plugin!` macro.  On `create`, spawns a fuser-based
//! FUSE event loop in a background thread that mounts the Nexus VFS at
//! an operator-named host path.  POSIX filesystem ops landing on that
//! mount point translate **in-process** to Nexus kernel syscalls via
//! the same `KernelHandle` callback table the loader handed to
//! `nexus_service_create` — no gRPC, no client process, no socket.
//!
//! ## Why a service plugin (and not a separate daemon)
//!
//! The legacy `nexus-fuse` crate is a standalone process that talks to
//! a Python FastAPI server over HTTPS REST.  Every filesystem syscall
//! crosses two process boundaries plus a serialization round-trip;
//! latency on the hot path is dominated by transport, not by the
//! filesystem itself.  The service-plugin model puts the FUSE event
//! loop in the same process as the kernel.  Each filesystem op is one
//! C-ABI function call into kernel-resident code — same cost as a
//! direct `kernel.sys_read(path)`.  The architecture matches what
//! `nexus-vault` does for password ops: bridge a service-tier surface
//! (gRPC / FUSE / whatever) into kernel syscalls without leaving the
//! process.
//!
//! ## Operator surface
//!
//! Mount point + VFS path prefix come from environment variables read
//! at `nexus_service_create` time:
//!
//!   * `NEXUS_FUSE_MOUNT_POINT` — host path where Nexus VFS will be
//!     mounted (e.g. `/mnt/nexus`).  Must exist and be empty.
//!   * `NEXUS_FUSE_VFS_ROOT` — VFS path prefix to expose at the mount
//!     point's root (defaults to `/`).
//!
//! Both default to placeholder values when the env vars are absent, so
//! `--plugin-dir` scans don't fail on hosts that load the dylib for
//! ABI introspection only.  The plugin no-ops in that case (event loop
//! exits immediately, no mount happens).  Production deployments
//! always set these env vars in the daemon's launch environment.
//!
//! ## Platform support
//!
//! Linux first (`libfuse3` via the `fuser` crate).  macOS support
//! piggybacks on `fuser`'s macFUSE backend the moment we add the
//! `target_os = "macos"` cfg gate.  Windows requires a separate
//! WinFsp adapter — out of scope for this first cut.

#[cfg(target_os = "linux")]
mod fs;

#[cfg(target_os = "linux")]
use std::sync::Mutex;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};

/// Plugin state held between `create` and `destroy`.
///
/// On Linux, owns the `fuser::BackgroundSession` driving the FUSE
/// event loop — dropping it sends an unmount + joins the worker
/// thread.  On non-Linux targets, holds nothing; the plugin is
/// effectively a no-op.
struct FusePlugin {
    #[cfg(target_os = "linux")]
    session: Mutex<Option<fuser::BackgroundSession>>,
    #[cfg(not(target_os = "linux"))]
    _phantom: (),
}

impl FusePlugin {
    fn new() -> Self {
        Self {
            #[cfg(target_os = "linux")]
            session: Mutex::new(None),
            #[cfg(not(target_os = "linux"))]
            _phantom: (),
        }
    }
}

/// Plugin-lifecycle `create`: read operator config from env, build the
/// FUSE filesystem instance, and spawn the event loop in a background
/// thread.  Returns the boxed `FusePlugin` even when the mount fails —
/// the dispatch surface exposes a `"status"` method that operators can
/// poll to see whether the event loop is actually running.  The
/// alternative (returning null from `create`) would block the
/// nexusd-cluster boot path on a recoverable mount failure, which
/// over-couples plugin lifecycle to operator-environment correctness.
#[allow(unused_variables)] // _kernel unused on non-Linux until those targets land
fn create_fuse_plugin(_kernel: &KernelHandle) -> Box<FusePlugin> {
    let plugin = FusePlugin::new();

    #[cfg(target_os = "linux")]
    {
        let mount_point = std::env::var("NEXUS_FUSE_MOUNT_POINT").ok();
        if let Some(mount_point) = mount_point {
            let vfs_root = std::env::var("NEXUS_FUSE_VFS_ROOT").unwrap_or_else(|_| "/".to_string());

            // SAFETY: KernelHandle's function pointers and `kernel_ptr`
            // are documented to remain valid for the plugin's lifetime.
            // The fuser background session keeps `NexusFs` alive only
            // while the FUSE event loop runs, which can't outlive the
            // plugin instance (destroy joins the worker thread before
            // dropping the box).
            let kernel_clone = unsafe { kernel_handle_clone(_kernel) };
            let fs = fs::NexusFs::new(kernel_clone, vfs_root);

            // Default fuser::Config is sufficient for cc-tasks-share's
            // flat-file read/write workflow.  Operators who need uid/gid
            // mapping or read-only mounts set them via the OS-level FUSE
            // mount wrapper, not the plugin.
            let mount_config = fuser::Config::default();
            match fuser::spawn_mount2(fs, &mount_point, &mount_config) {
                Ok(session) => {
                    tracing::info!(
                        target: "nexus::fuse",
                        mount_point = %mount_point,
                        "FUSE event loop spawned"
                    );
                    *plugin.session.lock().unwrap() = Some(session);
                }
                Err(e) => {
                    tracing::error!(
                        target: "nexus::fuse",
                        mount_point = %mount_point,
                        error = %e,
                        "FUSE mount failed; plugin will report status=unmounted"
                    );
                }
            }
        } else {
            tracing::warn!(
                target: "nexus::fuse",
                "NEXUS_FUSE_MOUNT_POINT not set — plugin loaded but no mount performed"
            );
        }
    }

    Box::new(plugin)
}

/// Clone the kernel handle so the FUSE thread owns its own copy.
/// `KernelHandle` is a `#[repr(C)]` bag of function pointers + an
/// opaque kernel pointer; cloning is just a field-by-field copy.  The
/// kernel guarantees the underlying state lives at least as long as
/// any plugin holding a clone (loader holds the `Arc<Kernel>` via the
/// `DylibRustService` it stores).
#[cfg(target_os = "linux")]
unsafe fn kernel_handle_clone(src: &KernelHandle) -> KernelHandle {
    KernelHandle {
        sys_read: src.sys_read,
        sys_write: src.sys_write,
        sys_stat: src.sys_stat,
        sys_readdir: src.sys_readdir,
        sys_unlink: src.sys_unlink,
        sys_mkdir: src.sys_mkdir,
        sys_rmdir: src.sys_rmdir,
        sys_rename: src.sys_rename,
        kernel_ptr: src.kernel_ptr,
    }
}

/// Plugin-lifecycle `dispatch` — admin-method surface invoked by the
/// service registry.  This is the **gRPC-style** path; the FUSE event
/// loop itself doesn't ride dispatch (it runs continuously in a
/// background thread and serves FUSE ops via the kernel handle
/// directly).  Methods here are operator pokes: `status` for a health
/// probe, `unmount` for shutdown without dropping the whole plugin.
///
/// Payload encoding: plain UTF-8 strings on input and output for the
/// first-cut admin surface.  When more structured methods land
/// (`stat-one-path`, `walk-tree`), graduate to prost-encoded protobuf
/// to match vault's payload shape.
fn dispatch_fuse(svc: &FusePlugin, method: &str, _payload: &[u8]) -> Result<Vec<u8>, i32> {
    match method {
        "status" => {
            #[cfg(target_os = "linux")]
            {
                let mounted = svc.session.lock().unwrap().is_some();
                Ok(if mounted {
                    b"mounted".to_vec()
                } else {
                    b"unmounted".to_vec()
                })
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = svc;
                Ok(b"unsupported-platform".to_vec())
            }
        }
        "unmount" => {
            #[cfg(target_os = "linux")]
            {
                let session = svc.session.lock().unwrap().take();
                if session.is_some() {
                    // Dropping the BackgroundSession sends the unmount
                    // ioctl and joins the worker thread.
                    drop(session);
                    Ok(b"ok".to_vec())
                } else {
                    Ok(b"not-mounted".to_vec())
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = svc;
                Ok(b"unsupported-platform".to_vec())
            }
        }
        _ => {
            // -1 maps to PluginResult::NotFound on the loader side.
            Err(-1)
        }
    }
}

declare_service_plugin!("fuse", FusePlugin, {
    create: create_fuse_plugin,
    dispatch: dispatch_fuse,
});
