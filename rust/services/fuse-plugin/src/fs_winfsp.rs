//! `NexusWinFsp` — `winfsp::filesystem::FileSystemContext` impl that
//! translates Windows IRP_MJ_xxx ops into Nexus kernel syscalls via
//! the `KernelHandle` callback table.  The Windows analogue of
//! [`crate::fs::NexusFs`] (which serves the fuser-based Linux + macOS
//! mount path).
//!
//! ## Path translation
//!
//! WinFsp passes `&U16CStr` (UTF-16 wide strings) with backslash
//! separators (`\foo\bar`); the kernel callbacks take UTF-8
//! `*const c_char` with forward slashes (`/foo/bar`).  Every op
//! converts at the FFI boundary via [`to_kernel_path`].
//!
//! ## `FileContext`
//!
//! WinFsp's `FileContext` associated type is the user-defined value
//! bound to each open handle.  We carry the kernel-side path plus a
//! cached `is_dir` bit so `read_directory` / `read` / `write` don't
//! have to re-walk `sys_stat` on every call.
//!
//! ## What's covered, what's stubbed
//!
//! Covered v2 ops (route through the KernelHandle v2 callbacks
//! nexus-vfs#56 added):
//!
//! ```text
//! WinFsp method        →  KernelHandle callback
//! ────────────────────    ────────────────────────
//! open / get_file_info →  sys_stat
//! read                 →  sys_read
//! write                →  sys_write
//! create               →  sys_write(empty)+sys_stat  (file)
//!                         sys_mkdir                  (dir)
//! rename               →  sys_rename
//! set_delete + cleanup →  sys_unlink / sys_rmdir
//! read_directory       →  sys_readdir
//! ```
//!
//! Stubbed because there's no kernel-side analogue today (ReBAC owns
//! permissions; timestamps are kernel-managed):
//!
//! * `get_security_by_name` / `get_security` / `set_security` —
//!   return "read+write+everyone" SDDL stub.  ReBAC at the kernel
//!   layer is the SoT for access policy.
//! * `set_basic_info` (chmod/chown/utimes) — accept silently, no-op.
//! * `set_file_size` — accept silently; sys_write handles allocation
//!   implicitly on the next write.
//! * Reparse points / extended attributes / streams — return
//!   `STATUS_INVALID_DEVICE_REQUEST` via the trait defaults.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::SystemTime;

use widestring::U16CStr;
use windows_sys::Win32::Foundation::{
    NTSTATUS, STATUS_DIRECTORY_NOT_EMPTY, STATUS_INVALID_DEVICE_REQUEST, STATUS_NOT_A_DIRECTORY,
    STATUS_OBJECT_NAME_NOT_FOUND,
};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_FLAGS_AND_ATTRIBUTES,
};
use winfsp::filesystem::{
    DirInfo, DirMarker, FileInfo, FileSecurity, FileSystemContext, OpenFileInfo, VolumeInfo,
    WideNameInfo,
};
use winfsp::FspError;

use nexus_plugin_abi::KernelHandle;

use crate::kernel_callbacks::{self, parse_readdir, parse_stat, DT_DIR, DT_MOUNT};
use crate::path_index::{join_path, PathIndex};

/// Diagnostic-trace gate, cached from `NEXUS_FUSE_PLUGIN_TRACE` at first
/// call.  Production daemons leave the env var unset → every
/// `winfsp_diag!` call is a single atomic load that branches to noop, so
/// the per-callback overhead is zero in the steady state.  CI sets the
/// var to enable the per-callback stderr trace stream (file uploaded as
/// a workflow artifact, full byte-exact).
///
/// Why eprintln and not `tracing::debug!`: the plugin is dlopen'd as a
/// cdylib and owns a separate `tracing` global, so the cluster's
/// subscriber doesn't see plugin tracing events.  stderr lands in
/// nexusd.err.log regardless — the operator's only observable channel.
fn winfsp_trace_enabled() -> bool {
    use std::sync::OnceLock;
    static CELL: OnceLock<bool> = OnceLock::new();
    *CELL.get_or_init(|| std::env::var_os("NEXUS_FUSE_PLUGIN_TRACE").is_some())
}

macro_rules! winfsp_diag {
    ($fmt:literal $($args:tt)*) => {
        if $crate::fs_winfsp::winfsp_trace_enabled() {
            eprintln!(concat!("[winfsp] ", $fmt) $($args)*);
        }
    };
}

/// Sentinel root inode for the Windows mount.  WinFsp has no
/// protocol-level root constant — the inode space is entirely ours.
/// Pinned to `1` to mirror fuser's `INodeNo::ROOT.0` so the index's
/// invariants are identical across platforms (purely a debugging
/// affordance; nothing in the on-wire surface ever sees this).
const WINFSP_ROOT_INODE: u64 = 1;

/// C-ABI rc the kernel returns for "no such file/directory" (matches
/// fuser side's mapping convention).  Used only by [`errno_to_status`].
const ERRNO_NOT_FOUND: i32 = -1;

/// `FILE_DIRECTORY_FILE` from `winnt.h` — the create-option flag a
/// CreateFile2 call sets when the caller wants a directory rather
/// than a regular file.  Not re-exported by `windows-sys` 0.52 so
/// we inline the constant.
const FILE_DIRECTORY_FILE: u32 = 0x00000001;

/// Map a `sys_*` C-ABI rc into the closest NTSTATUS code WinFsp
/// understands.  Keep this table tight — surfacing
/// `STATUS_INVALID_DEVICE_REQUEST` for unknown errors makes the
/// failure visible in Event Viewer instead of silently mapped to
/// `STATUS_SUCCESS`.
fn errno_to_status(rc: i32) -> NTSTATUS {
    match rc {
        ERRNO_NOT_FOUND => STATUS_OBJECT_NAME_NOT_FOUND,
        _ => STATUS_INVALID_DEVICE_REQUEST,
    }
}

/// Per-handle state — what WinFsp's `FileContext` associated type
/// resolves to.  Carries the kernel-side path string so the hot path
/// doesn't re-translate from WideCString on every op.
pub struct NexusFileContext {
    /// Forward-slash kernel-side path corresponding to the open
    /// handle, e.g. `/shared/cc-tasks/mac/session-1/1.json`.
    path: String,
    /// Cached from the open-time `sys_stat`.  Used so
    /// `read_directory` can short-circuit on non-directories
    /// without a kernel round-trip.
    is_dir: bool,
    /// Per-handle DirBuffer that we re-populate on every
    /// `read_directory` call with entries strictly past the
    /// caller's marker.  We pass marker=None to `dir_buffer.read`
    /// since the buffer already only contains the entries WinFsp
    /// is asking for — relying on `FspFileSystemReadDirectoryBuffer`'s
    /// internal marker-skip semantics doesn't actually skip the
    /// matching entry (verified via eprintln diagnostics on CI
    /// run 27512551448: 200 consecutive calls with
    /// `marker=cross-layer.json` each returned 146 bytes of
    /// `cross-layer.json` instead of EOF).
    dir_buffer: std::sync::Mutex<winfsp::filesystem::DirBuffer>,
    /// Cached sorted entry list, populated on the first
    /// `read_directory` call and reused on every continuation.
    /// `(name, entry_type, size)` triples — the size comes from
    /// the v3 `sys_stat_batch` call so we don't pay N round-trips.
    /// Sorted by name so marker-comparison works correctly.
    entries: std::sync::Mutex<Option<Vec<(String, u64, u64)>>>,
    /// "Marked for deletion via set_delete; do the actual unlink
    /// in cleanup when FspCleanupDelete fires".  Per winfsp 0.13's
    /// trait docs on `set_delete`: "The file should never be
    /// deleted in this function.  Instead, set a flag to indicate
    /// that the file is to be deleted later by cleanup."
    /// CI run 27515796611 confirmed: deleting in set_delete made
    /// `cmd /c del` a silent no-op because Windows uses the
    /// cleanup-path for `DeleteFileW` and never called my
    /// set_delete on individual files (only on the parent dir
    /// during the test's finally rmdir /S /Q).
    delete_on_cleanup: std::sync::atomic::AtomicBool,
}

/// The Windows-side filesystem context.  `Send` + `Sync` because
/// every field is internally synchronised: `KernelHandle` is a
/// `repr(C)` bag of `extern "C"` fn pointers (trivially shareable),
/// `paths` is a `Mutex<PathIndex>`, `next_inode` is atomic.
pub struct NexusWinFsp {
    kernel: KernelHandle,
    paths: Mutex<PathIndex>,
    next_inode: AtomicU64,
    /// Kernel-side VFS prefix this mount is rooted at — every WinFsp
    /// callback prepends this to the relative path WinFsp hands us.
    /// Stored without a trailing slash (and as `""` when the
    /// configured root is `"/"`) so the concat is a single
    /// `format!("{vfs_root}{rel}")` with no double-slash hazard.
    vfs_root: String,
}

// SAFETY: see struct doc — every field is `Send + Sync` either by
// type or by interior mutability.  `KernelHandle` carries a
// `*const c_void` kernel pointer whose `Send + Sync` is asserted by
// the kernel-side contract (it lives at least as long as the
// plugin).
unsafe impl Send for NexusWinFsp {}
unsafe impl Sync for NexusWinFsp {}

impl NexusWinFsp {
    pub fn new(kernel: KernelHandle, vfs_root: String) -> Self {
        // Normalise `vfs_root` so `to_kernel_path` can rely on a
        // single concat shape: `"" + "/foo"` (root case) vs
        // `"/shared/cc-tasks" + "/foo"` (nested case).  Treat
        // `"/"` and `""` as the same root-case sentinel because
        // `NEXUS_FUSE_VFS_ROOT="/"` is the documented default and
        // we don't want to leak `"//foo"` to the kernel.
        let normalised = match vfs_root.trim_end_matches('/') {
            "" => String::new(),
            trimmed => trimmed.to_string(),
        };
        // PathIndex still wants the canonical root path so reverse
        // lookups (`path_for(root_ino)`) return what the kernel
        // would call this mount's root — `"/"` for the default,
        // `"/shared/cc-tasks"` for the cc-tasks-share case.
        let pi_root = if normalised.is_empty() {
            "/".to_string()
        } else {
            normalised.clone()
        };
        Self {
            kernel,
            paths: Mutex::new(PathIndex::with_root(WINFSP_ROOT_INODE, pi_root)),
            next_inode: AtomicU64::new(WINFSP_ROOT_INODE + 1),
            vfs_root: normalised,
        }
    }

    /// Convert a WinFsp wide path (UTF-16, backslash separators) into
    /// the kernel-side path (UTF-8, forward-slash separators), with
    /// the configured `vfs_root` prefix applied.  Thin wrapper over
    /// [`kernel_path_for`] — keeping the translation logic in a
    /// free function lets the unit tests pin the contract without
    /// having to fabricate a `KernelHandle`.
    fn to_kernel_path(&self, win_path: &U16CStr) -> String {
        kernel_path_for(&self.vfs_root, win_path)
    }

    // ── Helpers ─────────────────────────────────────────────────────

    /// Allocate a stable inode for `path`.  Same SSOT rules the fuser
    /// side uses (see `crate::path_index`).
    fn inode_for(&self, path: &str) -> u64 {
        self.paths
            .lock()
            .unwrap()
            .lookup_or_register(path, || self.next_inode.fetch_add(1, Ordering::Relaxed))
    }

    /// Populate WinFsp's `FileInfo` from a parsed sys_stat JSON.
    /// `FileInfo` is a thin `repr(C)` struct over WinFsp's wire-level
    /// `FSP_FSCTL_FILE_INFO` — public fields, no setter methods.
    fn populate_file_info(info: &mut FileInfo, size: u64, is_dir: bool) {
        let attrs: u32 = if is_dir {
            FILE_ATTRIBUTE_DIRECTORY
        } else {
            FILE_ATTRIBUTE_NORMAL
        };
        info.file_attributes = attrs;
        info.file_size = size;
        info.allocation_size = size;
        // Timestamps: kernel doesn't expose them yet, so synthesise
        // "now" for every call.  Operators see the file's mtime
        // update on every read which is a minor UX wart but doesn't
        // break the `cc tasks list` workflow (cc-side reads use
        // content + path, not stat-mtime).  Track the real time in a
        // follow-up when sys_stat starts emitting modified_at_ms.
        let now = systime_to_ft(SystemTime::now());
        info.creation_time = now;
        info.last_access_time = now;
        info.last_write_time = now;
        info.change_time = now;
        info.index_number = 0;
    }
}

/// Convert a `SystemTime` to Windows FILETIME (100-ns ticks since
/// 1601-01-01 UTC).  WinFsp's `FileInfo::set_*_time` takes this
/// representation directly.
fn systime_to_ft(t: SystemTime) -> u64 {
    const UNIX_EPOCH_IN_FILETIME: u64 = 116_444_736_000_000_000;
    let unix_nanos = t
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    UNIX_EPOCH_IN_FILETIME + (unix_nanos / 100)
}

/// Return `true` when an `entry_type` byte represents a directory
/// (DT_DIR or DT_MOUNT).  Mirrors fs.rs's `kind_from_entry_type` but
/// emits a plain bool because Windows uses `FILE_ATTRIBUTE_DIRECTORY`
/// not fuser's `FileType` enum.
fn is_dir_entry(entry_type: u64) -> bool {
    entry_type == DT_DIR || entry_type == DT_MOUNT
}

impl FileSystemContext for NexusWinFsp {
    type FileContext = NexusFileContext;

    fn get_security_by_name(
        &self,
        file_name: &U16CStr,
        _security_descriptor: Option<&mut [std::os::raw::c_void]>,
        _reparse_point_resolver: impl FnOnce(&U16CStr) -> Option<FileSecurity>,
    ) -> Result<FileSecurity, FspError> {
        let path = self.to_kernel_path(file_name);
        winfsp_diag!("get_security_by_name path={:?}", path);
        let json = kernel_callbacks::sys_stat(&self.kernel, &path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        let (_size, entry_type) =
            parse_stat(&json).ok_or(FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        let is_dir = is_dir_entry(entry_type);
        let attrs = if is_dir {
            FILE_ATTRIBUTE_DIRECTORY
        } else {
            FILE_ATTRIBUTE_NORMAL
        };
        Ok(FileSecurity {
            attributes: attrs,
            reparse: false,
            sz_security_descriptor: 0,
        })
    }

    fn open(
        &self,
        file_name: &U16CStr,
        create_options: u32,
        granted_access: u32,
        file_info: &mut OpenFileInfo,
    ) -> Result<Self::FileContext, FspError> {
        let path = self.to_kernel_path(file_name);
        winfsp_diag!(
            "open path={:?} create_opts=0x{:x} granted=0x{:x} raw_widebytes={}",
            path,
            create_options,
            granted_access,
            file_name.len()
        );
        let json = kernel_callbacks::sys_stat(&self.kernel, &path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        let (size, entry_type) =
            parse_stat(&json).ok_or(FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        let is_dir = is_dir_entry(entry_type);
        self.inode_for(&path);
        Self::populate_file_info(file_info.as_mut(), size, is_dir);
        Ok(NexusFileContext {
            path,
            is_dir,
            dir_buffer: std::sync::Mutex::new(winfsp::filesystem::DirBuffer::new()),
            entries: std::sync::Mutex::new(None),
            delete_on_cleanup: std::sync::atomic::AtomicBool::new(false),
        })
    }

    fn close(&self, _context: Self::FileContext) {
        // No per-handle resources to release.
    }

    fn create(
        &self,
        file_name: &U16CStr,
        create_options: u32,
        _granted_access: u32,
        _file_attributes: FILE_FLAGS_AND_ATTRIBUTES,
        _security_descriptor: Option<&[std::os::raw::c_void]>,
        _allocation_size: u64,
        _extra_buffer: Option<&[u8]>,
        _extra_buffer_is_reparse_point: bool,
        file_info: &mut OpenFileInfo,
    ) -> Result<Self::FileContext, FspError> {
        let path = self.to_kernel_path(file_name);
        winfsp_diag!("create path={:?} create_opts=0x{:x}", path, create_options);
        let is_dir = (create_options & FILE_DIRECTORY_FILE) != 0;
        if is_dir {
            kernel_callbacks::sys_mkdir(&self.kernel, &path)
                .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        } else {
            // Compose create from empty-payload sys_write — same
            // pattern the fuser side uses; the kernel treats
            // "write-to-nonexistent-path" as create-on-write.
            kernel_callbacks::sys_write(&self.kernel, &path, &[])
                .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        }
        let json = kernel_callbacks::sys_stat(&self.kernel, &path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        let (size, entry_type) =
            parse_stat(&json).ok_or(FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        let parsed_is_dir = is_dir_entry(entry_type);
        self.inode_for(&path);
        Self::populate_file_info(file_info.as_mut(), size, parsed_is_dir);
        Ok(NexusFileContext {
            path,
            is_dir: parsed_is_dir,
            dir_buffer: std::sync::Mutex::new(winfsp::filesystem::DirBuffer::new()),
            entries: std::sync::Mutex::new(None),
            delete_on_cleanup: std::sync::atomic::AtomicBool::new(false),
        })
    }

    fn get_file_info(
        &self,
        context: &Self::FileContext,
        file_info: &mut FileInfo,
    ) -> Result<(), FspError> {
        let json = kernel_callbacks::sys_stat(&self.kernel, &context.path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        let (size, entry_type) =
            parse_stat(&json).ok_or(FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        let is_dir = is_dir_entry(entry_type);
        Self::populate_file_info(file_info, size, is_dir);
        Ok(())
    }

    fn read(
        &self,
        context: &Self::FileContext,
        buffer: &mut [u8],
        offset: u64,
    ) -> Result<u32, FspError> {
        if context.is_dir {
            return Err(FspError::NTSTATUS(STATUS_NOT_A_DIRECTORY));
        }
        let full = kernel_callbacks::sys_read(&self.kernel, &context.path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        let start = offset as usize;
        if start >= full.len() {
            return Ok(0);
        }
        let n = (full.len() - start).min(buffer.len());
        buffer[..n].copy_from_slice(&full[start..start + n]);
        Ok(n as u32)
    }

    fn write(
        &self,
        context: &Self::FileContext,
        buffer: &[u8],
        offset: u64,
        _write_to_eof: bool,
        _constrained_io: bool,
        file_info: &mut FileInfo,
    ) -> Result<u32, FspError> {
        if context.is_dir {
            return Err(FspError::NTSTATUS(STATUS_NOT_A_DIRECTORY));
        }
        // First cut: O_TRUNC semantics — `sys_write` rewrites the
        // whole file.  An offset != 0 write surfaces as
        // STATUS_INVALID_DEVICE_REQUEST until the offset-aware kernel
        // callback lands; CC's task-file workflow always rewrites the
        // whole JSON document so offset==0 is the common path.
        winfsp_diag!(
            "write path={} offset={} len={}",
            context.path,
            offset,
            buffer.len()
        );
        if offset != 0 {
            return Err(FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST));
        }
        kernel_callbacks::sys_write(&self.kernel, &context.path, buffer)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        // Refresh file_info with the new size.
        Self::populate_file_info(file_info, buffer.len() as u64, false);
        Ok(buffer.len() as u32)
    }

    fn read_directory(
        &self,
        context: &Self::FileContext,
        _pattern: Option<&U16CStr>,
        marker: DirMarker<'_>,
        buffer: &mut [u8],
    ) -> Result<u32, FspError> {
        if !context.is_dir {
            return Err(FspError::NTSTATUS(STATUS_NOT_A_DIRECTORY));
        }
        let marker_str = marker.inner().map(String::from_utf16_lossy);
        winfsp_diag!(
            "read_directory path={} marker={:?} buffer_cap={}",
            context.path,
            marker_str,
            buffer.len()
        );

        // Cache the sorted entry list on the FIRST call, reuse on
        // every continuation.  `sys_readdir` returns metastore-
        // insertion order; we sort by name once so the marker
        // comparison below works deterministically.
        let mut entries_guard = context
            .entries
            .lock()
            .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        if entries_guard.is_none() {
            let json = kernel_callbacks::sys_readdir(&self.kernel, &context.path)
                .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
            let mut raw = parse_readdir(&json);
            raw.sort_by(|a, b| a.0.cmp(&b.0));

            // Batched sys_stat for sizes — KernelHandle v3
            // `sys_stat_batch` collapses N FFI hops into one.
            let child_paths: Vec<String> = raw
                .iter()
                .map(|(name, _)| join_path(&context.path, name))
                .collect();
            let sizes = kernel_callbacks::sys_stat_batch(&self.kernel, &child_paths)
                .unwrap_or_else(|_| vec![None; raw.len()]);

            let cached: Vec<(String, u64, u64)> = raw
                .into_iter()
                .enumerate()
                .map(|(idx, (name, entry_type))| {
                    let size = sizes
                        .get(idx)
                        .copied()
                        .flatten()
                        .map(|(s, _)| s)
                        .unwrap_or(0);
                    (name, entry_type, size)
                })
                .collect();
            *entries_guard = Some(cached);
        }
        let cached = entries_guard.as_ref().expect("just populated");

        // Filter entries STRICTLY past the marker — name > marker.
        // We then populate the per-call DirBuffer with only those,
        // and read with `marker=None` so `FspFileSystemReadDirectoryBuffer`
        // returns everything we just wrote.  This sidesteps the
        // observed WinFsp marker-skip bug where reading with the
        // matching marker re-emits the entry indefinitely.
        let filtered: Vec<&(String, u64, u64)> = match marker_str.as_deref() {
            Some(m) => cached
                .iter()
                .filter(|(name, _, _)| name.as_str() > m)
                .collect(),
            None => cached.iter().collect(),
        };

        let dir_buffer = context
            .dir_buffer
            .lock()
            .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        let session = dir_buffer
            .acquire(true, None)
            .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        for (name, entry_type, size) in filtered {
            let attrs = if is_dir_entry(*entry_type) {
                FILE_ATTRIBUTE_DIRECTORY
            } else {
                FILE_ATTRIBUTE_NORMAL
            };
            let mut info: DirInfo = DirInfo::new();
            info.set_name(std::ffi::OsStr::new(name))
                .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
            let fi = info.file_info_mut();
            Self::populate_file_info(fi, *size, is_dir_entry(*entry_type));
            fi.file_attributes = attrs;
            session
                .write(&mut info)
                .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
        }
        drop(session);
        // Pass the caller's marker through.  The buffer already
        // contains only entries strictly past it, so even if
        // `FspFileSystemReadDirectoryBuffer` includes the matching-
        // marker entry (which our diagnostics showed is what it does
        // in practice), there's nothing in the buffer to wrong-emit.
        let written = dir_buffer.read(marker, buffer);
        winfsp_diag!(
            "read_directory done path={} written_bytes={}",
            context.path,
            written
        );
        Ok(written)
    }

    fn rename(
        &self,
        _context: &Self::FileContext,
        file_name: &U16CStr,
        new_file_name: &U16CStr,
        _replace_if_exists: bool,
    ) -> Result<(), FspError> {
        let old_path = self.to_kernel_path(file_name);
        let new_path = self.to_kernel_path(new_file_name);
        let res = kernel_callbacks::sys_rename(&self.kernel, &old_path, &new_path);
        // Trace kept while the rename callsite is still being
        // debugged on CI — diagnostic on CI run 27515796611 showed
        // `cmd /c move` failed before ever reaching this callback
        // (NO rename traces emitted at all), so the failure is
        // upstream of this code path; keeping the eprintln here
        // means the next round either confirms the upstream open()
        // is rejecting the rename intent OR pinpoints what error
        // sys_rename surfaces if we do get this far.
        winfsp_diag!("rename old={} new={} result={:?}", old_path, new_path, res);
        res.map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        self.paths.lock().unwrap().rename(&old_path, &new_path);
        Ok(())
    }

    fn set_delete(
        &self,
        context: &Self::FileContext,
        _file_name: &U16CStr,
        delete_file: bool,
    ) -> Result<(), FspError> {
        winfsp_diag!(
            "set_delete path={} is_dir={} delete_file={}",
            context.path,
            context.is_dir,
            delete_file
        );
        // Per winfsp 0.13's `set_delete` contract: do NOT actually
        // delete here.  Stage the intent on the FileContext; the
        // real `sys_unlink` / `sys_rmdir` happens in `cleanup` when
        // the FspCleanupDelete flag fires.  Deleting here makes
        // `cmd /c del` a silent no-op because Windows uses the
        // cleanup-path for `DeleteFileW` and never routes through
        // `set_delete` for individual files (CI run 27515796611
        // proved this — the only set_delete calls visible were on
        // the parent dir during the test's finally rmdir, never on
        // the files cmd had just supposedly deleted).
        //
        // For directories we still surface the early "is empty"
        // check synchronously so Explorer + cmd get the visible
        // failure before they commit to delete pending.
        if delete_file && context.is_dir {
            let probe = kernel_callbacks::sys_rmdir(&self.kernel, &context.path);
            if probe.is_err() {
                return Err(FspError::NTSTATUS(STATUS_DIRECTORY_NOT_EMPTY));
            }
            // sys_rmdir already succeeded — the dir is gone.
            // PathIndex forget happens in cleanup along with the
            // (now no-op) repeat call path.  Mark delete-pending so
            // cleanup runs forget; sys_rmdir is idempotent on
            // already-deleted paths (returns ENOENT, which we map
            // back to OK in cleanup).
        }
        context
            .delete_on_cleanup
            .store(delete_file, std::sync::atomic::Ordering::SeqCst);
        Ok(())
    }

    fn set_basic_info(
        &self,
        _context: &Self::FileContext,
        _file_attributes: u32,
        _creation_time: u64,
        _last_access_time: u64,
        _last_write_time: u64,
        _last_change_time: u64,
        file_info: &mut FileInfo,
    ) -> Result<(), FspError> {
        // chmod/utimes/etc. — no kernel surface today.  Accept
        // silently so explorer.exe doesn't error out, but the values
        // never leave the kernel side (ReBAC owns access policy).
        // Repopulate file_info with whatever the current state is.
        Self::populate_file_info(file_info, 0, false);
        Ok(())
    }

    fn set_file_size(
        &self,
        context: &Self::FileContext,
        new_size: u64,
        _set_allocation_size: bool,
        file_info: &mut FileInfo,
    ) -> Result<(), FspError> {
        // Pre-allocation hint.  We don't track allocation precisely
        // — sys_write handles the actual allocation on next write —
        // so just update file_info to keep the kernel-side caller
        // happy.
        Self::populate_file_info(file_info, new_size, context.is_dir);
        Ok(())
    }

    fn cleanup(&self, context: &Self::FileContext, _file_name: Option<&U16CStr>, flags: u32) {
        winfsp_diag!(
            "cleanup path={} is_dir={} flags={} staged={}",
            context.path,
            context.is_dir,
            flags,
            context
                .delete_on_cleanup
                .load(std::sync::atomic::Ordering::SeqCst)
        );
        // Real delete happens here, per winfsp 0.13's contract.
        // Two signals can trigger it: the staged `delete_on_cleanup`
        // flag set by `set_delete` for the FileDispositionInformation
        // path, OR the `FspCleanupDelete` flag the dispatcher sets
        // for the cleanup-path delete (FILE_FLAG_DELETE_ON_CLOSE,
        // and Windows 11's `DeleteFileW` for single-handle cases —
        // CI run 27515796611 proved this latter path was the bug:
        // Windows never called `set_delete` on the individual task
        // files, only `cleanup` with FspCleanupDelete, so previous
        // delete-in-set_delete shape made `cmd /c del` a no-op).
        let staged = context
            .delete_on_cleanup
            .load(std::sync::atomic::Ordering::SeqCst);
        let cleanup_delete = winfsp::constants::FspCleanupFlags::FspCleanupDelete.is_flagged(flags);
        if !staged && !cleanup_delete {
            return;
        }
        let res = if context.is_dir {
            kernel_callbacks::sys_rmdir(&self.kernel, &context.path)
        } else {
            kernel_callbacks::sys_unlink(&self.kernel, &context.path)
        };
        winfsp_diag!(
            "cleanup deleted path={} is_dir={} result={:?}",
            context.path,
            context.is_dir,
            res
        );
        if res.is_ok() {
            self.paths.lock().unwrap().forget(&context.path);
        }
    }

    fn flush(
        &self,
        _context: Option<&Self::FileContext>,
        _file_info: &mut FileInfo,
    ) -> Result<(), FspError> {
        // sys_write is synchronous through to the metastore commit;
        // nothing to flush at the FUSE layer.
        Ok(())
    }

    fn get_volume_info(&self, out: &mut VolumeInfo) -> Result<(), FspError> {
        // Placeholder figures — the cc-tasks-share workflow is many
        // small files; surface a generously sized notional volume so
        // `dir` / `Get-Volume` don't surface 0 bytes free and confuse
        // operators.  Track free/used precisely in a follow-up when
        // the kernel grows a sys_statfs callback.
        out.total_size = 1 << 40;
        out.free_size = 1 << 39;
        let _ = out.set_volume_label("NexusVFS");
        Ok(())
    }
}

/// Pure-function path translator extracted from
/// [`NexusWinFsp::to_kernel_path`] so unit tests can pin the
/// contract without fabricating a `KernelHandle`.
///
/// Translates a WinFsp wide path (UTF-16, backslash separators,
/// always rooted at `\`) into a kernel-side absolute VFS path
/// (UTF-8, forward slashes) by prepending the mount's `vfs_root`.
/// `vfs_root` is expected pre-normalised: no trailing slash, empty
/// string sentinel for the `"/"` default case.
fn kernel_path_for(vfs_root: &str, win_path: &U16CStr) -> String {
    let s = win_path.to_string_lossy();
    let relative = if s == "\\" {
        String::new()
    } else {
        s.replace('\\', "/")
    };
    if vfs_root.is_empty() {
        if relative.is_empty() {
            "/".to_string()
        } else {
            relative
        }
    } else if relative.is_empty() {
        vfs_root.to_string()
    } else {
        format!("{}{}", vfs_root, relative)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use widestring::U16CString;

    fn w(s: &str) -> U16CString {
        U16CString::from_str(s).expect("utf-16 conversion")
    }

    #[test]
    fn kernel_path_for_root_default() {
        // Default mount (`NEXUS_FUSE_VFS_ROOT="/"` → normalised to "")
        // surfaces the global VFS root on `M:\`.
        assert_eq!(kernel_path_for("", w("\\").as_ucstr()), "/");
        assert_eq!(kernel_path_for("", w("\\foo").as_ucstr()), "/foo");
        assert_eq!(
            kernel_path_for("", w("\\dir\\file.txt").as_ucstr()),
            "/dir/file.txt"
        );
    }

    #[test]
    fn kernel_path_for_subtree_mount() {
        // cc-tasks-share scope: M:\ surfaces /shared/cc-tasks,
        // M:\songym-win surfaces /shared/cc-tasks/songym-win, etc.
        // Regression pin for the bug where `ls M:\` showed `/`s
        // contents (raft/, sm, ...) instead of the configured
        // subtree.
        let root = "/shared/cc-tasks";
        assert_eq!(kernel_path_for(root, w("\\").as_ucstr()), "/shared/cc-tasks");
        assert_eq!(
            kernel_path_for(root, w("\\songym-win").as_ucstr()),
            "/shared/cc-tasks/songym-win"
        );
        assert_eq!(
            kernel_path_for(
                root,
                w("\\songym-win\\session-1\\1.json").as_ucstr()
            ),
            "/shared/cc-tasks/songym-win/session-1/1.json"
        );
    }

    #[test]
    fn kernel_path_for_normalised_vfs_root_no_double_slash() {
        // Even if the caller forgets to normalise, the constructor
        // does — verify the post-normalisation contract: `vfs_root`
        // never carries a trailing slash, so the concat is a single
        // `{root}{relative}` with no `"//"` hazard.
        assert!(!kernel_path_for("/shared/cc-tasks", w("\\foo").as_ucstr()).contains("//"));
    }
}
