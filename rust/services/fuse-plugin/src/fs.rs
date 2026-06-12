//! `NexusFs` — `fuser::Filesystem` impl that translates FUSE ops
//! into Nexus kernel syscalls via the `KernelHandle` callback table.
//!
//! ## Inode model
//!
//! FUSE addresses files by `u64` inode numbers; Nexus VFS addresses
//! them by path strings.  We maintain a `HashMap<u64, String>` —
//! built incrementally on every `lookup` — that lets the FUSE ops
//! translate an inode back into the path the kernel-side callbacks
//! expect.  The root inode (`fuser::FUSE_ROOT_ID == 1`) maps to the
//! configured VFS root prefix (`/` by default).  New inodes are
//! allocated monotonically from a counter; we never recycle, which
//! keeps the implementation lock-free in the common read path at the
//! cost of unbounded growth on long-running mounts.  Replacing the
//! counter with a path-cache eviction policy is a follow-up.
//!
//! ## What works in the first cut
//!
//! * `lookup` / `getattr` — both route through `sys_stat` on the path
//!   resolved from inode mapping.  `getattr` returns a `FileAttr`
//!   synthesised from the stat result.
//! * `read` — reads the **entire** file via `sys_read` and slices the
//!   returned buffer by offset / size.  This is correct but wasteful
//!   for large files; an offset-aware `sys_read` is one of the
//!   `KernelHandle` extensions tracked for a follow-up nexus-vfs PR.
//! * `write` — writes the full payload at offset 0 only (effectively
//!   `O_TRUNC` semantics).  Real offset-aware writes need the same
//!   `KernelHandle` extension.
//! * `open` / `release` / `flush` — no-ops; the kernel doesn't carry
//!   per-fd state.
//!
//! ## What surfaces as `ENOSYS` until KernelHandle grows
//!
//! `readdir`, `create`, `mkdir`, `unlink`, `rmdir`, `rename`,
//! `setattr` — every directory-mutating op and any read that depends
//! on enumeration.  These are tracked in the parent crate's docstring
//! as the upcoming `nexus-vfs` PR.  Reporting `ENOSYS` rather than a
//! best-effort fake keeps the operator-visible surface honest: `ls`
//! over the mount fails loudly until the extension lands, instead of
//! silently returning an empty directory.

use std::collections::HashMap;
use std::ffi::CString;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use fuser::{
    FileAttr, FileType, Filesystem, ReplyAttr, ReplyData, ReplyEmpty, ReplyEntry, ReplyOpen,
    ReplyWrite, Request, FUSE_ROOT_ID,
};

use nexus_plugin_abi::{nexus_free, KernelHandle};

/// Cache TTL for kernel-attributes returned to FUSE.  Set to 1 second
/// to mirror typical NFS defaults; the kernel-side metastore is the
/// SSOT, so stale entries self-correct within a tick.
const ATTR_TTL: Duration = Duration::from_secs(1);
const ENTRY_TTL: Duration = Duration::from_secs(1);

/// `EIO` — best-fit POSIX errno for "kernel callback returned a
/// non-zero status."  Refined later if we want to map specific kernel
/// errors to ENOENT / EACCES / etc.
const ERRNO_IO: i32 = libc::EIO;
/// `ENOSYS` — surfaced for ops that depend on `KernelHandle` syscalls
/// not yet exposed (readdir / unlink / mkdir / rmdir / rename).
const ERRNO_NOSYS: i32 = libc::ENOSYS;

pub struct NexusFs {
    kernel: KernelHandle,
    /// VFS-path prefix that maps to the FUSE root inode.  Joined with
    /// child names to produce kernel-side paths.
    vfs_root: String,
    /// `inode -> VFS path` registry.  Path lookups populate this; we
    /// never evict.
    inodes: Mutex<HashMap<u64, String>>,
    next_inode: AtomicU64,
}

impl NexusFs {
    pub fn new(kernel: KernelHandle, vfs_root: String) -> Self {
        let mut inodes = HashMap::new();
        inodes.insert(FUSE_ROOT_ID, vfs_root.clone());
        Self {
            kernel,
            vfs_root,
            inodes: Mutex::new(inodes),
            // First allocated inode = root + 1; FUSE_ROOT_ID itself is
            // reserved.
            next_inode: AtomicU64::new(FUSE_ROOT_ID + 1),
        }
    }

    fn path_for(&self, ino: u64) -> Option<String> {
        self.inodes.lock().unwrap().get(&ino).cloned()
    }

    fn alloc_inode(&self, path: String) -> u64 {
        let ino = self.next_inode.fetch_add(1, Ordering::Relaxed);
        self.inodes.lock().unwrap().insert(ino, path);
        ino
    }

    /// Wrapper around the C `sys_stat` callback.  Returns the JSON
    /// payload as a `String` on success.
    fn sys_stat(&self, path: &str) -> Result<String, i32> {
        let c_path = match CString::new(path) {
            Ok(s) => s,
            Err(_) => return Err(ERRNO_IO),
        };
        let mut out_buf: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let rc = unsafe {
            (self.kernel.sys_stat)(
                self.kernel.kernel_ptr,
                c_path.as_ptr(),
                &mut out_buf,
                &mut out_len,
            )
        };
        if rc != 0 {
            return Err(rc);
        }
        let json = unsafe {
            let slice = std::slice::from_raw_parts(out_buf, out_len);
            let s = std::str::from_utf8(slice)
                .map(|s| s.to_string())
                .map_err(|_| ERRNO_IO);
            nexus_free(out_buf, out_len);
            s?
        };
        Ok(json)
    }

    /// Wrapper around the C `sys_read` callback.  Returns the full
    /// file content as a `Vec<u8>`; callers slice for offset/size.
    fn sys_read(&self, path: &str) -> Result<Vec<u8>, i32> {
        let c_path = match CString::new(path) {
            Ok(s) => s,
            Err(_) => return Err(ERRNO_IO),
        };
        let mut out_buf: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let rc = unsafe {
            (self.kernel.sys_read)(
                self.kernel.kernel_ptr,
                c_path.as_ptr(),
                &mut out_buf,
                &mut out_len,
            )
        };
        if rc != 0 {
            return Err(rc);
        }
        let data = unsafe {
            let slice = std::slice::from_raw_parts(out_buf, out_len);
            let v = slice.to_vec();
            nexus_free(out_buf, out_len);
            v
        };
        Ok(data)
    }

    /// Wrapper around the C `sys_write` callback.
    fn sys_write(&self, path: &str, data: &[u8]) -> Result<(), i32> {
        let c_path = match CString::new(path) {
            Ok(s) => s,
            Err(_) => return Err(ERRNO_IO),
        };
        let rc = unsafe {
            (self.kernel.sys_write)(
                self.kernel.kernel_ptr,
                c_path.as_ptr(),
                data.as_ptr(),
                data.len(),
            )
        };
        if rc != 0 {
            return Err(rc);
        }
        Ok(())
    }

    /// Parse the JSON payload `sys_stat` returns into a `FileAttr`.
    /// The kernel-side shape is `StatResult` (see
    /// `kernel/src/kernel/mod.rs StatResult`); we hand-roll a tiny
    /// parser to avoid pulling `serde_json` into the cdylib's
    /// dependency closure for one path.
    fn parse_stat(&self, ino: u64, json: &str) -> Option<FileAttr> {
        // Crude key-value scan — kernel-side JSON is well-formed by
        // construction, and we only need three fields.  When stat
        // grows more shape (gen, mode bits), promote to serde_json.
        let size = json_u64(json, "\"size\":")?;
        let is_dir = json_bool(json, "\"is_directory\":")?;
        let kind = if is_dir {
            FileType::Directory
        } else {
            FileType::RegularFile
        };
        // FUSE wants a complete FileAttr; fields we don't pull from
        // sys_stat default to zero / now / 0644.
        let now = SystemTime::now();
        Some(FileAttr {
            ino,
            size,
            blocks: size.div_ceil(512),
            atime: now,
            mtime: now,
            ctime: now,
            crtime: now,
            kind,
            perm: if is_dir { 0o755 } else { 0o644 },
            nlink: if is_dir { 2 } else { 1 },
            uid: unsafe { libc::getuid() },
            gid: unsafe { libc::getgid() },
            rdev: 0,
            blksize: 4096,
            flags: 0,
        })
    }
}

/// Extract a `u64` from `json` by scanning for `key` and parsing the
/// digits that follow.  Returns `None` if the key isn't present or
/// the value isn't a non-negative integer.
fn json_u64(json: &str, key: &str) -> Option<u64> {
    let after = json.split_once(key)?.1.trim_start();
    let end = after
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(after.len());
    after[..end].parse().ok()
}

/// Extract a JSON `true` / `false` from `json` after `key`.
fn json_bool(json: &str, key: &str) -> Option<bool> {
    let after = json.split_once(key)?.1.trim_start();
    if after.starts_with("true") {
        Some(true)
    } else if after.starts_with("false") {
        Some(false)
    } else {
        None
    }
}

impl Filesystem for NexusFs {
    fn lookup(&mut self, _req: &Request, parent: u64, name: &std::ffi::OsStr, reply: ReplyEntry) {
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(libc::EINVAL);
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        let json = match self.sys_stat(&path) {
            Ok(j) => j,
            Err(_) => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        let ino = self.alloc_inode(path.clone());
        let attr = match self.parse_stat(ino, &json) {
            Some(a) => a,
            None => {
                reply.error(ERRNO_IO);
                return;
            }
        };
        reply.entry(&ENTRY_TTL, &attr, /* generation */ 0);
    }

    fn getattr(&mut self, _req: &Request, ino: u64, _fh: Option<u64>, reply: ReplyAttr) {
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        let json = match self.sys_stat(&path) {
            Ok(j) => j,
            Err(_) => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        let attr = match self.parse_stat(ino, &json) {
            Some(a) => a,
            None => {
                reply.error(ERRNO_IO);
                return;
            }
        };
        reply.attr(&ATTR_TTL, &attr);
    }

    fn open(&mut self, _req: &Request, _ino: u64, _flags: i32, reply: ReplyOpen) {
        // No per-fd state.  fh=0, flags=0 keeps it stateless.
        reply.opened(0, 0);
    }

    fn read(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        size: u32,
        _flags: i32,
        _lock_owner: Option<u64>,
        reply: ReplyData,
    ) {
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        let full = match self.sys_read(&path) {
            Ok(d) => d,
            Err(_) => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        // Slice by offset/size.  Once the offset-aware `sys_read`
        // extension lands in KernelHandle, this becomes a direct
        // callback pass-through and stops reading the whole file
        // for every page-sized FUSE read.
        let start = offset as usize;
        if start >= full.len() {
            reply.data(&[]);
            return;
        }
        let end = (start + size as usize).min(full.len());
        reply.data(&full[start..end]);
    }

    fn write(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        data: &[u8],
        _write_flags: u32,
        _flags: i32,
        _lock_owner: Option<u64>,
        reply: ReplyWrite,
    ) {
        // First cut: O_TRUNC semantics only.  An offset != 0 write
        // surfaces as EIO until the offset-aware kernel callback
        // lands.  This is honest about the gap rather than silently
        // dropping bytes; CC's task-file workflow always rewrites
        // the whole JSON document so offset==0 is the common path.
        if offset != 0 {
            reply.error(libc::EIO);
            return;
        }
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(libc::ENOENT);
                return;
            }
        };
        match self.sys_write(&path, data) {
            Ok(()) => reply.written(data.len() as u32),
            Err(_) => reply.error(ERRNO_IO),
        }
    }

    fn flush(&mut self, _req: &Request, _ino: u64, _fh: u64, _lock_owner: u64, reply: ReplyEmpty) {
        reply.ok();
    }

    fn release(
        &mut self,
        _req: &Request,
        _ino: u64,
        _fh: u64,
        _flags: i32,
        _lock_owner: Option<u64>,
        _flush: bool,
        reply: ReplyEmpty,
    ) {
        reply.ok();
    }

    // ── Ops that need KernelHandle to grow ───────────────────────────
    //
    // Surface as ENOSYS until the matching syscall is added to the
    // plugin ABI's KernelHandle struct.  Tracked in the parent
    // crate's docstring as the follow-up nexus-vfs PR.

    fn readdir(
        &mut self,
        _req: &Request,
        _ino: u64,
        _fh: u64,
        _offset: i64,
        reply: fuser::ReplyDirectory,
    ) {
        reply.error(ERRNO_NOSYS);
    }

    fn mkdir(
        &mut self,
        _req: &Request,
        _parent: u64,
        _name: &std::ffi::OsStr,
        _mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        reply.error(ERRNO_NOSYS);
    }

    fn unlink(&mut self, _req: &Request, _parent: u64, _name: &std::ffi::OsStr, reply: ReplyEmpty) {
        reply.error(ERRNO_NOSYS);
    }

    fn rmdir(&mut self, _req: &Request, _parent: u64, _name: &std::ffi::OsStr, reply: ReplyEmpty) {
        reply.error(ERRNO_NOSYS);
    }

    fn rename(
        &mut self,
        _req: &Request,
        _parent: u64,
        _name: &std::ffi::OsStr,
        _newparent: u64,
        _newname: &std::ffi::OsStr,
        _flags: u32,
        reply: ReplyEmpty,
    ) {
        reply.error(ERRNO_NOSYS);
    }

    fn create(
        &mut self,
        _req: &Request,
        _parent: u64,
        _name: &std::ffi::OsStr,
        _mode: u32,
        _umask: u32,
        _flags: i32,
        reply: fuser::ReplyCreate,
    ) {
        reply.error(ERRNO_NOSYS);
    }

    fn setattr(
        &mut self,
        _req: &Request,
        _ino: u64,
        _mode: Option<u32>,
        _uid: Option<u32>,
        _gid: Option<u32>,
        _size: Option<u64>,
        _atime: Option<fuser::TimeOrNow>,
        _mtime: Option<fuser::TimeOrNow>,
        _ctime: Option<SystemTime>,
        _fh: Option<u64>,
        _crtime: Option<SystemTime>,
        _chgtime: Option<SystemTime>,
        _bkuptime: Option<SystemTime>,
        _flags: Option<u32>,
        reply: ReplyAttr,
    ) {
        reply.error(ERRNO_NOSYS);
    }
}

/// Join a parent VFS path and a child file name into the canonical
/// `parent/child` form the kernel expects.  Strips trailing slashes
/// from `parent` so `/` + `foo` == `/foo` (not `//foo`).
fn join_path(parent: &str, name: &str) -> String {
    if parent == "/" {
        format!("/{name}")
    } else {
        let p = parent.trim_end_matches('/');
        format!("{p}/{name}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_path_root() {
        assert_eq!(join_path("/", "foo"), "/foo");
    }

    #[test]
    fn join_path_nested() {
        assert_eq!(join_path("/a/b", "c"), "/a/b/c");
    }

    #[test]
    fn join_path_strips_trailing_slash() {
        assert_eq!(join_path("/a/b/", "c"), "/a/b/c");
    }

    #[test]
    fn json_u64_extracts() {
        assert_eq!(
            json_u64(r#"{"size":12345,"is_directory":false}"#, "\"size\":"),
            Some(12345)
        );
    }

    #[test]
    fn json_bool_extracts() {
        assert_eq!(
            json_bool(r#"{"is_directory":true}"#, "\"is_directory\":"),
            Some(true)
        );
        assert_eq!(
            json_bool(r#"{"is_directory":false}"#, "\"is_directory\":"),
            Some(false)
        );
    }
}
