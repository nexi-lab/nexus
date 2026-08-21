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
//! Linux (`libfuse3`) and macOS (`FUSE-T` via `libfuse3`) share one
//! cfg-gated body — `fuser`'s `Filesystem` trait + `spawn_mount2`
//! work identically on both.  On macOS, fuser links against FUSE-T's
//! native `libfuse3` (not the legacy libfuse2 compat shim) so both
//! `.pkg` and `brew` FUSE-T installations work out of the box.
//! Operators install the platform-native FUSE userspace out-of-band
//! (apt `libfuse3-3` / `FUSE-T.pkg` / `brew install fuse-t`) before
//! launching `nexusd-cluster --plugin-dir`.  Windows uses a separate
//! WinFsp adapter — different binding crate (winfsp-rs), different
//! mount API surface.  On Windows today the plugin compiles to a
//! no-op cdylib so the workspace builds cleanly across the matrix.

// `kernel_callbacks` and `path_index` are platform-agnostic — both
// the fuser-based fs (Linux/macOS) and the WinFsp-based fs_winfsp
// (Windows) consume them.  No cfg gate.
mod kernel_callbacks;
mod path_index;

#[cfg(any(target_os = "linux", target_os = "macos"))]
mod fs;

#[cfg(target_os = "macos")]
pub mod fs_nfs;

#[cfg(target_os = "macos")]
mod fuse_t_detect;

#[cfg(target_os = "windows")]
mod fs_winfsp;

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
use std::sync::Mutex;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};

/// Plugin state held between `create` and `destroy`.
///
/// * Linux / macOS — owns the `fuser::BackgroundSession` driving the
///   FUSE event loop.  Dropping sends an unmount + joins the worker
///   thread.
/// * Windows — owns the `winfsp::host::FileSystemHost` driving the
///   WinFsp dispatcher.  Dropping sends `stop()` + un-mounts.
struct FusePlugin {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    session: Mutex<Option<fuser::BackgroundSession>>,
    /// Names a host-side prerequisite the platform-native FUSE userspace
    /// driver needs that we couldn't satisfy at plugin-create time. Only
    /// populated on macOS today, where FUSE-T is a user-installed `.pkg`
    /// the supervisor (sudowork on the desktop) provisions via
    /// `osascript` after this plugin reports the gap. `Some("fuse-t")`
    /// means "the FUSE event loop was skipped because FUSE-T isn't
    /// installed" — surfaced over `dispatch("status")` as
    /// `"fuse-t-missing"` so the supervisor can match without parsing
    /// a free-form message.
    #[cfg(target_os = "macos")]
    prereq_missing: Mutex<Option<&'static str>>,
    /// NFS localhost mount handle — populated when the fuser mount fails
    /// on macOS and the NFS fallback succeeds.  Dropping the handle
    /// unmounts and shuts down the NFS server.
    #[cfg(target_os = "macos")]
    nfs_handle: Mutex<Option<fs_nfs::NfsMountHandle>>,
    #[cfg(target_os = "windows")]
    host: Mutex<
        Option<winfsp::host::FileSystemHost<fs_winfsp::NexusWinFsp, winfsp::host::CoarseGuard>>,
    >,
}

impl FusePlugin {
    fn new() -> Self {
        Self {
            #[cfg(any(target_os = "linux", target_os = "macos"))]
            session: Mutex::new(None),
            #[cfg(target_os = "macos")]
            prereq_missing: Mutex::new(None),
            #[cfg(target_os = "macos")]
            nfs_handle: Mutex::new(None),
            #[cfg(target_os = "windows")]
            host: Mutex::new(None),
        }
    }
}

/// Try NFS localhost fallback — single implementation shared by all
/// macOS fallback paths (FUSE-T missing, fuser error, fuser silent fail).
#[cfg(target_os = "macos")]
fn try_nfs_fallback(
    plugin: &FusePlugin,
    kernel: &KernelHandle,
    mount_point: &str,
    vfs_root: &str,
    reason: &str,
) {
    eprintln!("[nexus-fuse-plugin] {reason}, falling back to NFS localhost");
    tracing::warn!(
        target: "nexus::fuse",
        mount_point = %mount_point,
        reason = %reason,
        "attempting NFS localhost fallback"
    );
    let kernel_nfs = unsafe { kernel_handle_clone(kernel) };
    let nfs_fs = fs_nfs::NexusNfs::new(kernel_nfs, vfs_root.to_string());
    match fs_nfs::spawn_nfs_mount(nfs_fs, mount_point) {
        Ok(handle) => {
            eprintln!(
                "[nexus-fuse-plugin] NFS mount OK at {mount_point} (port {})",
                handle.port()
            );
            tracing::info!(
                target: "nexus::fuse",
                mount_point = %mount_point,
                port = handle.port(),
                "NFS localhost fallback mounted"
            );
            *plugin.nfs_handle.lock().unwrap() = Some(handle);
        }
        Err(nfs_err) => {
            eprintln!("[nexus-fuse-plugin] NFS fallback also failed: {nfs_err}");
            tracing::error!(
                target: "nexus::fuse",
                mount_point = %mount_point,
                nfs_error = %nfs_err,
                "NFS fallback failed"
            );
        }
    }
}

/// Check if a mount point has a live filesystem attached (macOS).
/// Returns false if the mount point doesn't appear in `mount` output,
/// which means FUSE-T returned Ok but silently disconnected.
#[cfg(target_os = "macos")]
fn is_mount_live(mount_point: &str) -> bool {
    std::process::Command::new("/sbin/mount")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains(mount_point))
        .unwrap_or(false)
}

/// Plugin-lifecycle `create`: read operator config from env, build the
/// FUSE filesystem instance, and spawn the event loop in a background
/// thread.  Returns the boxed `FusePlugin` even when the mount fails —
/// the dispatch surface exposes a `"status"` method that operators can
/// poll to see whether the event loop is actually running.  The
/// alternative (returning null from `create`) would block the
/// nexusd-cluster boot path on a recoverable mount failure, which
/// over-couples plugin lifecycle to operator-environment correctness.
#[allow(unused_variables)] // _kernel unused on unsupported targets
fn create_fuse_plugin(_kernel: &KernelHandle) -> Box<FusePlugin> {
    let plugin = FusePlugin::new();

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        let mount_point = std::env::var("NEXUS_FUSE_MOUNT_POINT").ok();
        if let Some(mount_point) = mount_point {
            // macOS-only preflight: FUSE-T is a user-installed `.pkg`
            // shipping the kernel-side FUSE userspace driver. Without
            // it `fuser::spawn_mount2` fails with a low-level
            // "device not found" that's confusing to surface up. Probe
            // for it first, and if missing, register a clean
            // `prereq_missing = Some("fuse-t")` state the supervisor
            // (sudowork on the desktop) can poll via
            // `dispatch("status")`. The supervisor installs FUSE-T via
            // `osascript` and re-creates this plugin to retry the mount.
            // No supervisor-visible spawn failure, no half-mounted state.
            #[cfg(target_os = "macos")]
            {
                use fuse_t_detect::{is_fuse_t_installed, DetectionResult};
                if matches!(is_fuse_t_installed(), DetectionResult::NotFound) {
                    let vfs_root =
                        std::env::var("NEXUS_FUSE_VFS_ROOT").unwrap_or_else(|_| "/".to_string());
                    try_nfs_fallback(
                        &plugin,
                        _kernel,
                        &mount_point,
                        &vfs_root,
                        "FUSE-T not installed",
                    );
                    if plugin.nfs_handle.lock().unwrap().is_none() {
                        *plugin.prereq_missing.lock().unwrap() = Some("fuse-t");
                    }
                    return Box::new(plugin);
                }
            }

            let vfs_root = std::env::var("NEXUS_FUSE_VFS_ROOT").unwrap_or_else(|_| "/".to_string());

            // SAFETY: KernelHandle's function pointers and `kernel_ptr`
            // are documented to remain valid for the plugin's lifetime.
            // The fuser background session keeps `NexusFs` alive only
            // while the FUSE event loop runs, which can't outlive the
            // plugin instance (destroy joins the worker thread before
            // dropping the box).
            let kernel_clone = unsafe { kernel_handle_clone(_kernel) };
            let fs = fs::NexusFs::new(kernel_clone, vfs_root.clone());

            // Default fuser::Config is sufficient for cc-tasks-share's
            // flat-file read/write workflow.  Operators who need uid/gid
            // mapping or read-only mounts set them via the OS-level FUSE
            // mount wrapper, not the plugin.
            let mount_config = fuser::Config::default();
            match fuser::spawn_mount2(fs, &mount_point, &mount_config) {
                Ok(session) => {
                    // Post-mount validation: FUSE-T on macOS 26 Tahoe
                    // returns Ok from spawn_mount2 but the mount silently
                    // disconnects immediately (go-nfsv4 backend crash).
                    #[cfg(target_os = "macos")]
                    {
                        std::thread::sleep(std::time::Duration::from_millis(500));
                        if !is_mount_live(&mount_point) {
                            drop(session);
                            try_nfs_fallback(
                                &plugin,
                                _kernel,
                                &mount_point,
                                &vfs_root,
                                "fuser returned Ok but mount is not live (FUSE-T silent failure)",
                            );
                            return Box::new(plugin);
                        }
                    }
                    eprintln!("[nexus-fuse-plugin] mount OK at {mount_point}");
                    tracing::info!(
                        target: "nexus::fuse",
                        mount_point = %mount_point,
                        "FUSE event loop spawned"
                    );
                    *plugin.session.lock().unwrap() = Some(session);
                }
                Err(e) => {
                    #[cfg(target_os = "macos")]
                    {
                        try_nfs_fallback(
                            &plugin,
                            _kernel,
                            &mount_point,
                            &vfs_root,
                            &format!("fuser mount failed ({e})"),
                        );
                    }
                    #[cfg(not(target_os = "macos"))]
                    {
                        eprintln!("[nexus-fuse-plugin] mount FAILED at {mount_point}: {e}");
                        tracing::error!(
                            target: "nexus::fuse",
                            mount_point = %mount_point,
                            error = %e,
                            "FUSE mount failed; plugin will report status=unmounted"
                        );
                    }
                }
            }
        } else {
            eprintln!("[nexus-fuse-plugin] NEXUS_FUSE_MOUNT_POINT not set — no mount performed");
            tracing::warn!(
                target: "nexus::fuse",
                "NEXUS_FUSE_MOUNT_POINT not set — plugin loaded but no mount performed"
            );
        }
    }

    #[cfg(target_os = "windows")]
    {
        // winfsp_init_or_die loads winfsp-x64.dll and binds the
        // function-pointer table. Idempotent — calling it twice on
        // the same process is a no-op. Required before any
        // FileSystemHost::new call. Panics if WinFsp isn't installed
        // on the operator's machine, which is the only mount-time
        // failure mode we can't recover from.
        if let Err(e) = winfsp::winfsp_init() {
            eprintln!("[nexus-fuse-plugin] WinFsp init FAILED: {e:?} — is WinFsp installed?");
            tracing::error!(
                target: "nexus::fuse",
                error = ?e,
                "WinFsp init failed; plugin will report status=unmounted"
            );
            return Box::new(plugin);
        }

        let mount_point = std::env::var("NEXUS_FUSE_MOUNT_POINT").ok();
        if let Some(mount_point) = mount_point {
            let vfs_root = std::env::var("NEXUS_FUSE_VFS_ROOT").unwrap_or_else(|_| "/".to_string());

            // SAFETY: same lifetime contract as the Linux branch —
            // KernelHandle's pointers remain valid for the plugin's
            // lifetime, and the FileSystemHost holds NexusWinFsp only
            // while the dispatcher runs (destroy stops it first).
            let kernel_clone = unsafe { kernel_handle_clone(_kernel) };
            let fs = fs_winfsp::NexusWinFsp::new(kernel_clone, vfs_root);

            // Reasonable defaults for the cc-tasks-share workflow.
            // Operators who need different VolumeParams (sector size,
            // case-sensitivity, etc.) override via the OS-level mount
            // wrapper or a follow-up env var.
            let mut volume_params = winfsp::host::VolumeParams::new();
            volume_params
                .filesystem_name("NexusVFS")
                .prefix("")
                .case_preserved_names(true)
                .case_sensitive_search(true)
                .unicode_on_disk(true)
                .persistent_acls(false)
                .reparse_points(false)
                .named_streams(false);

            match winfsp::host::FileSystemHost::new(volume_params, fs) {
                Ok(mut host) => match host.mount(&mount_point) {
                    // CoarseGuard is the default guard strategy on
                    // FileSystemHost<T> but the impl trees overlap so
                    // `.start()` is ambiguous; reach the
                    // CoarseGuard-specific start via UFCS.
                    Ok(()) => match winfsp::host::FileSystemHost::<
                        fs_winfsp::NexusWinFsp,
                        winfsp::host::CoarseGuard,
                    >::start(&mut host)
                    {
                        Ok(()) => {
                            eprintln!("[nexus-fuse-plugin] mount OK at {mount_point}");
                            tracing::info!(
                                target: "nexus::fuse",
                                mount_point = %mount_point,
                                "WinFsp dispatcher started"
                            );
                            *plugin.host.lock().unwrap() = Some(host);
                        }
                        Err(e) => {
                            eprintln!(
                                "[nexus-fuse-plugin] WinFsp start FAILED at {mount_point}: {e:?}"
                            );
                            tracing::error!(
                                target: "nexus::fuse",
                                mount_point = %mount_point,
                                error = ?e,
                                "WinFsp dispatcher start failed"
                            );
                        }
                    },
                    Err(e) => {
                        eprintln!(
                            "[nexus-fuse-plugin] WinFsp mount FAILED at {mount_point}: {e:?}"
                        );
                        tracing::error!(
                            target: "nexus::fuse",
                            mount_point = %mount_point,
                            error = ?e,
                            "WinFsp mount() failed"
                        );
                    }
                },
                Err(e) => {
                    eprintln!("[nexus-fuse-plugin] WinFsp FileSystemHost::new FAILED: {e:?}");
                    tracing::error!(
                        target: "nexus::fuse",
                        error = ?e,
                        "WinFsp FileSystemHost::new failed"
                    );
                }
            }
        } else {
            eprintln!("[nexus-fuse-plugin] NEXUS_FUSE_MOUNT_POINT not set — no mount performed");
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
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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
        sys_stat_batch: src.sys_stat_batch,
        free_buf: src.free_buf,
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
            #[cfg(target_os = "macos")]
            {
                // FUSE-T (or whatever future macOS preflight names
                // itself) takes precedence over the session check —
                // the session can't possibly be `Some(_)` when the
                // preflight bailed, but `<prereq>-missing` is the
                // signal the supervisor needs to know which action
                // to take. The format `"<prereq>-missing"` is the
                // contract sudowork's nexusd-cluster supervisor matches
                // on; adding a new prereq is a single-line change here.
                if let Some(prereq) = *svc.prereq_missing.lock().unwrap() {
                    return Ok(format!("{prereq}-missing").into_bytes());
                }
            }
            #[cfg(any(target_os = "linux", target_os = "macos"))]
            {
                let fuse_mounted = svc.session.lock().unwrap().is_some();
                if fuse_mounted {
                    return Ok(b"mounted".to_vec());
                }
                #[cfg(target_os = "macos")]
                {
                    let nfs_mounted = svc.nfs_handle.lock().unwrap().is_some();
                    if nfs_mounted {
                        return Ok(b"mounted-nfs".to_vec());
                    }
                }
                Ok(b"unmounted".to_vec())
            }
            #[cfg(not(any(target_os = "linux", target_os = "macos")))]
            {
                let _ = svc;
                Ok(b"unsupported-platform".to_vec())
            }
        }
        "unmount" => {
            #[cfg(any(target_os = "linux", target_os = "macos"))]
            {
                let session = svc.session.lock().unwrap().take();
                if session.is_some() {
                    // Dropping the BackgroundSession sends the unmount
                    // ioctl and joins the worker thread.
                    drop(session);
                    return Ok(b"ok".to_vec());
                }
                #[cfg(target_os = "macos")]
                {
                    let nfs = svc.nfs_handle.lock().unwrap().take();
                    if nfs.is_some() {
                        // Dropping NfsMountHandle runs `umount` and
                        // stops the tokio runtime.
                        drop(nfs);
                        return Ok(b"ok".to_vec());
                    }
                }
                Ok(b"not-mounted".to_vec())
            }
            #[cfg(not(any(target_os = "linux", target_os = "macos")))]
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
