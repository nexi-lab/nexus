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
    /// Per-handle DirBuffer for directory enumeration.  WinFsp
    /// calls `read_directory` repeatedly with `marker` for
    /// continuation; the buffer must persist across calls so
    /// subsequent reads resume from the marker instead of
    /// re-populating from scratch (which would loop the first
    /// entry forever — observed locally as `ls Y:\` repeating
    /// `probe.json` 65 times before the test runner gave up).
    dir_buffer: std::sync::Mutex<winfsp::filesystem::DirBuffer>,
}

/// The Windows-side filesystem context.  `Send` + `Sync` because
/// every field is internally synchronised: `KernelHandle` is a
/// `repr(C)` bag of `extern "C"` fn pointers (trivially shareable),
/// `paths` is a `Mutex<PathIndex>`, `next_inode` is atomic.
pub struct NexusWinFsp {
    kernel: KernelHandle,
    paths: Mutex<PathIndex>,
    next_inode: AtomicU64,
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
        Self {
            kernel,
            paths: Mutex::new(PathIndex::with_root(WINFSP_ROOT_INODE, vfs_root)),
            next_inode: AtomicU64::new(WINFSP_ROOT_INODE + 1),
        }
    }

    /// Convert a WinFsp wide path (UTF-16, backslash separators) into
    /// the kernel-side path (UTF-8, forward-slash separators).
    /// `\` → `/` is the only transformation; WinFsp already strips
    /// the drive letter / mount point prefix before handing the path
    /// to the filesystem.
    fn to_kernel_path(win_path: &U16CStr) -> String {
        let s = win_path.to_string_lossy();
        if s == "\\" {
            "/".to_string()
        } else {
            s.replace('\\', "/")
        }
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
        let path = Self::to_kernel_path(file_name);
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
        _create_options: u32,
        _granted_access: u32,
        file_info: &mut OpenFileInfo,
    ) -> Result<Self::FileContext, FspError> {
        let path = Self::to_kernel_path(file_name);
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
        let path = Self::to_kernel_path(file_name);
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
        let dir_buffer = context
            .dir_buffer
            .lock()
            .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;

        // `marker.is_none()` is WinFsp's signal that this is the
        // first read_directory call for this open handle — populate
        // the per-handle DirBuffer once.  Subsequent calls (marker
        // present) skip straight to the `read` step below, which
        // resumes from where the prior call left off.  Filling on
        // every call would feed WinFsp the first entry forever and
        // turn `ls` into an infinite loop (verified locally before
        // this fix).
        if marker.is_none() {
            let json = kernel_callbacks::sys_readdir(&self.kernel, &context.path)
                .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
            let mut entries = parse_readdir(&json);
            // WinFsp's `FspFileSystemReadDirectoryBuffer` continues
            // an enumeration by name-comparison against the caller's
            // `marker`: it returns entries whose name lexicographically
            // follows the marker, and signals EOF (0 bytes written)
            // when the marker is past the last entry.  That contract
            // assumes the buffer's entries are SORTED — without that
            // invariant the continuation walks the buffer's insertion
            // order and can re-emit the trailing entry indefinitely
            // (observed on CI: `rmdir /S /Q` recursive enumeration
            // hung 10 s+ because readdir kept handing back the last
            // probe.json instead of EOF).  Kernel `sys_readdir`
            // returns metastore insertion order, not sorted, so the
            // sort happens here at the WinFsp ABI boundary.
            entries.sort_by(|a, b| a.0.cmp(&b.0));

            // Single batched sys_stat for sizes — KernelHandle v3's
            // `sys_stat_batch` (nexus-vfs#60) does one FFI hop + one
            // kernel pass instead of N round-trips.  Aligned 1:1 with
            // `entries` order so the index walk below matches up.
            let child_paths: Vec<String> = entries
                .iter()
                .map(|(name, _)| join_path(&context.path, name))
                .collect();
            let sizes = kernel_callbacks::sys_stat_batch(&self.kernel, &child_paths)
                .unwrap_or_else(|_| vec![None; entries.len()]);

            let session = dir_buffer
                .acquire(true, None)
                .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
            for (idx, (name, entry_type)) in entries.into_iter().enumerate() {
                let attrs = if is_dir_entry(entry_type) {
                    FILE_ATTRIBUTE_DIRECTORY
                } else {
                    FILE_ATTRIBUTE_NORMAL
                };
                // sys_stat_batch returns aligned `Option<(size, entry_type)>`;
                // unwrap to 0 for stat-misses so the entry still surfaces.
                let size = sizes
                    .get(idx)
                    .copied()
                    .flatten()
                    .map(|(s, _)| s)
                    .unwrap_or(0);

                let mut info: DirInfo = DirInfo::new();
                info.set_name(std::ffi::OsStr::new(&name))
                    .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
                let fi = info.file_info_mut();
                Self::populate_file_info(fi, size, is_dir_entry(entry_type));
                fi.file_attributes = attrs;
                session
                    .write(&mut info)
                    .map_err(|_| FspError::NTSTATUS(STATUS_INVALID_DEVICE_REQUEST))?;
            }
            drop(session);
        }
        let written = dir_buffer.read(marker, buffer);
        Ok(written)
    }

    fn rename(
        &self,
        _context: &Self::FileContext,
        file_name: &U16CStr,
        new_file_name: &U16CStr,
        _replace_if_exists: bool,
    ) -> Result<(), FspError> {
        let old_path = Self::to_kernel_path(file_name);
        let new_path = Self::to_kernel_path(new_file_name);
        kernel_callbacks::sys_rename(&self.kernel, &old_path, &new_path)
            .map_err(|e| FspError::NTSTATUS(errno_to_status(e)))?;
        self.paths.lock().unwrap().rename(&old_path, &new_path);
        Ok(())
    }

    fn set_delete(
        &self,
        context: &Self::FileContext,
        _file_name: &U16CStr,
        delete_file: bool,
    ) -> Result<(), FspError> {
        if !delete_file {
            return Ok(());
        }
        let res = if context.is_dir {
            kernel_callbacks::sys_rmdir(&self.kernel, &context.path)
        } else {
            kernel_callbacks::sys_unlink(&self.kernel, &context.path)
        };
        match res {
            Ok(()) => {
                self.paths.lock().unwrap().forget(&context.path);
                Ok(())
            }
            // Kernel surfaces ENOTEMPTY for non-empty dirs as -3
            // (Internal); without a dedicated errno we map any rmdir
            // failure to STATUS_DIRECTORY_NOT_EMPTY which is the
            // operator-visible expectation.
            Err(_) if context.is_dir => Err(FspError::NTSTATUS(STATUS_DIRECTORY_NOT_EMPTY)),
            Err(rc) => Err(FspError::NTSTATUS(errno_to_status(rc))),
        }
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

    fn cleanup(&self, _context: &Self::FileContext, _file_name: Option<&U16CStr>, _flags: u32) {
        // No per-handle state to release on cleanup.  The actual
        // delete (when `set_delete(true)` was called earlier) ran
        // synchronously in `set_delete`; WinFsp's `cleanup` is just
        // the hook to finalise deferred work, of which we have none.
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
