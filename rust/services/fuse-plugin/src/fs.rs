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
//! ## What now works via KernelHandle v2 (nexus-vfs#56)
//!
//! `readdir`, `mkdir`, `unlink`, `rmdir`, `rename`, `create` — every
//! directory-mutating op routes through real kernel callbacks added
//! in `PLUGIN_API_VERSION = 2`.  `create` is composed from `sys_write`
//! (empty payload) followed by `sys_stat`, which matches the kernel's
//! own "touch a file" semantics without adding a dedicated callback.
//!
//! ## What still surfaces as `ENOSYS`
//!
//! `setattr` — chmod/chown/truncate/utimes have no kernel equivalent
//! yet; permissions are owned by ReBAC and timestamps are
//! kernel-managed.  Surfacing `ENOSYS` keeps the operator-visible
//! contract honest until the kernel grows a setattr surface.

use std::collections::HashMap;
use std::ffi::CString;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use fuser::{
    Errno, FileAttr, FileHandle, FileType, Filesystem, Generation, INodeNo, ReplyAttr, ReplyData,
    ReplyEmpty, ReplyEntry, ReplyOpen, ReplyWrite, Request,
};

use nexus_plugin_abi::{nexus_free, KernelHandle};

/// Cache TTL for kernel-attributes returned to FUSE.  Set to 1 second
/// to mirror typical NFS defaults; the kernel-side metastore is the
/// SSOT, so stale entries self-correct within a tick.
const ATTR_TTL: Duration = Duration::from_secs(1);
const ENTRY_TTL: Duration = Duration::from_secs(1);

/// Raw inode value for the FUSE root.  fuser 0.17 exposes a typed
/// `INodeNo::ROOT` constant (the `u64` 1), which we unwrap here so
/// the inode map can use plain `u64` keys.
const FUSE_ROOT_RAW: u64 = INodeNo::ROOT.0;

/// Raw i32 EIO used by the internal sys_*() C-ABI wrappers (which
/// return `Result<_, i32>` mirroring the C-side rc).  Trait-level
/// methods wrap it into the typed `Errno` via `errno_io()`.
const ERRNO_IO_RAW: i32 = libc::EIO;

/// `EIO` errno wrapper used as the catch-all for "kernel callback
/// returned a non-zero status."  Refined later if we want to map
/// specific kernel errors to ENOENT / EACCES / etc.
fn errno_io() -> Errno {
    Errno::from_i32(libc::EIO)
}
fn errno_enoent() -> Errno {
    Errno::from_i32(libc::ENOENT)
}
fn errno_einval() -> Errno {
    Errno::from_i32(libc::EINVAL)
}
fn errno_nosys() -> Errno {
    Errno::from_i32(libc::ENOSYS)
}

pub struct NexusFs {
    kernel: KernelHandle,
    /// `inode -> VFS path` registry.  Root inode (FUSE_ROOT_RAW) is
    /// seeded with the operator-supplied `vfs_root` prefix; child
    /// lookups join from there.  We never evict.
    inodes: Mutex<HashMap<u64, String>>,
    next_inode: AtomicU64,
}

impl NexusFs {
    pub fn new(kernel: KernelHandle, vfs_root: String) -> Self {
        let mut inodes = HashMap::new();
        inodes.insert(FUSE_ROOT_RAW, vfs_root);
        Self {
            kernel,
            inodes: Mutex::new(inodes),
            // First allocated inode = root + 1; the root inode itself
            // is reserved (fuser INodeNo::ROOT == 1).
            next_inode: AtomicU64::new(FUSE_ROOT_RAW + 1),
        }
    }

    fn path_for(&self, ino: INodeNo) -> Option<String> {
        self.inodes.lock().unwrap().get(&ino.0).cloned()
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
            Err(_) => return Err(ERRNO_IO_RAW),
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
                .map_err(|_| ERRNO_IO_RAW);
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
            Err(_) => return Err(ERRNO_IO_RAW),
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

    /// Wrapper around the C `sys_readdir` callback.  Returns the JSON
    /// array `[{"name":..,"entry_type":..}, ...]` as a `String`.
    fn sys_readdir(&self, parent_path: &str) -> Result<String, i32> {
        let c_path = match CString::new(parent_path) {
            Ok(s) => s,
            Err(_) => return Err(ERRNO_IO_RAW),
        };
        let mut out_buf: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let rc = unsafe {
            (self.kernel.sys_readdir)(
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
                .map_err(|_| ERRNO_IO_RAW);
            nexus_free(out_buf, out_len);
            s?
        };
        Ok(json)
    }

    /// Wrapper around the C `sys_unlink` callback.
    fn sys_unlink(&self, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let rc = unsafe { (self.kernel.sys_unlink)(self.kernel.kernel_ptr, c_path.as_ptr()) };
        if rc != 0 {
            return Err(rc);
        }
        Ok(())
    }

    /// Wrapper around the C `sys_mkdir` callback.
    fn sys_mkdir(&self, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let rc = unsafe { (self.kernel.sys_mkdir)(self.kernel.kernel_ptr, c_path.as_ptr()) };
        if rc != 0 {
            return Err(rc);
        }
        Ok(())
    }

    /// Wrapper around the C `sys_rmdir` callback.
    fn sys_rmdir(&self, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let rc = unsafe { (self.kernel.sys_rmdir)(self.kernel.kernel_ptr, c_path.as_ptr()) };
        if rc != 0 {
            return Err(rc);
        }
        Ok(())
    }

    /// Wrapper around the C `sys_rename` callback.
    fn sys_rename(&self, old_path: &str, new_path: &str) -> Result<(), i32> {
        let c_old = CString::new(old_path).map_err(|_| ERRNO_IO_RAW)?;
        let c_new = CString::new(new_path).map_err(|_| ERRNO_IO_RAW)?;
        let rc = unsafe {
            (self.kernel.sys_rename)(self.kernel.kernel_ptr, c_old.as_ptr(), c_new.as_ptr())
        };
        if rc != 0 {
            return Err(rc);
        }
        Ok(())
    }

    /// Wrapper around the C `sys_write` callback.
    fn sys_write(&self, path: &str, data: &[u8]) -> Result<(), i32> {
        let c_path = match CString::new(path) {
            Ok(s) => s,
            Err(_) => return Err(ERRNO_IO_RAW),
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
    /// `kernel/src/kernel/mod.rs StatResult` and the JSON serializer
    /// in `kernel/src/kernel/plugins/mod.rs kernel_cb_sys_stat`);
    /// we hand-roll a tiny parser to avoid pulling `serde_json` into
    /// the cdylib's dependency closure for two fields.
    fn parse_stat(&self, ino: INodeNo, json: &str) -> Option<FileAttr> {
        // Kernel callback exports `entry_type` (the SSOT for "what
        // kind of inode this is") rather than a precomputed
        // `is_directory` flag, so dispatch by entry_type instead.
        // DT_DIR (1) is the only directory variant; everything else
        // (DT_REG / DT_MOUNT / DT_PIPE / DT_STREAM /
        // DT_EXTERNAL_STORAGE / DT_LINK) maps to a regular-file
        // surface for FUSE callers.  DT_MOUNT would deserve a
        // directory surface here once the FUSE layer learns how to
        // traverse mount points; for now treating it as a regular
        // file is consistent with the kernel returning miss on
        // readdir against a mount root.
        let size = json_u64(json, "\"size\":")?;
        let entry_type = json_u64(json, "\"entry_type\":")?;
        const DT_DIR: u64 = 1;
        let kind = if entry_type == DT_DIR {
            FileType::Directory
        } else {
            FileType::RegularFile
        };
        let is_dir = entry_type == DT_DIR;
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

    /// Same sys_stat wrapper but returning a populated FileAttr on
    /// success.  Centralises the path → callback → parse pipeline so
    /// lookup / getattr / readdir share one error mapping.
    fn stat_attr(&self, ino: INodeNo, path: &str) -> Result<FileAttr, Errno> {
        let json = self.sys_stat(path).map_err(|_| errno_enoent())?;
        self.parse_stat(ino, &json).ok_or_else(errno_io)
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

/// Parse the `sys_readdir` JSON array into `(name, entry_type)` pairs.
/// The kernel-side serializer (see `kernel_cb_sys_readdir` in
/// `kernel/src/kernel/plugins/mod.rs`) hand-rolls a minimal JSON of
/// shape `[{"name":<escaped>,"entry_type":<u8>}, ...]`, so we parse
/// the same shape without pulling `serde_json` into the cdylib.  Only
/// the `"` and `\` escapes the kernel emits are handled.
fn parse_readdir_entries(json: &str) -> Vec<(String, u64)> {
    let mut out = Vec::new();
    let mut rest = json;
    while let Some((_, after_name)) = rest.split_once("\"name\":\"") {
        let mut name = String::new();
        let mut bytes = after_name.chars();
        loop {
            match bytes.next() {
                Some('\\') => match bytes.next() {
                    Some(c) => name.push(c),
                    None => return out,
                },
                Some('"') => break,
                Some(c) => name.push(c),
                None => return out,
            }
        }
        let after_close = bytes.as_str();
        let entry_type = match json_u64(after_close, "\"entry_type\":") {
            Some(t) => t,
            None => return out,
        };
        out.push((name, entry_type));
        rest = after_close;
    }
    out
}

impl Filesystem for NexusFs {
    fn lookup(&self, _req: &Request, parent: INodeNo, name: &std::ffi::OsStr, reply: ReplyEntry) {
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        let ino_raw = self.alloc_inode(path.clone());
        let ino = INodeNo(ino_raw);
        match self.stat_attr(ino, &path) {
            Ok(attr) => reply.entry(&ENTRY_TTL, &attr, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn getattr(&self, _req: &Request, ino: INodeNo, _fh: Option<FileHandle>, reply: ReplyAttr) {
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        match self.stat_attr(ino, &path) {
            Ok(attr) => reply.attr(&ATTR_TTL, &attr),
            Err(e) => reply.error(e),
        }
    }

    fn open(&self, _req: &Request, _ino: INodeNo, _flags: fuser::OpenFlags, reply: ReplyOpen) {
        // Stateless: no per-fd context.  fuser accepts FileHandle(0)
        // as the "no handle" sentinel; FopenFlags::empty() opts out
        // of fuser's caching hints.
        reply.opened(FileHandle(0), fuser::FopenFlags::empty());
    }

    fn read(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        size: u32,
        _flags: fuser::OpenFlags,
        _lock_owner: Option<fuser::LockOwner>,
        reply: ReplyData,
    ) {
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let full = match self.sys_read(&path) {
            Ok(d) => d,
            Err(_) => {
                reply.error(errno_enoent());
                return;
            }
        };
        // Slice by offset/size.  Once the offset-aware `sys_read`
        // extension lands in KernelHandle, this becomes a direct
        // callback pass-through and stops reading the whole file for
        // every page-sized FUSE read.
        let start = offset as usize;
        if start >= full.len() {
            reply.data(&[]);
            return;
        }
        let end = (start + size as usize).min(full.len());
        reply.data(&full[start..end]);
    }

    fn write(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        data: &[u8],
        _write_flags: fuser::WriteFlags,
        _flags: fuser::OpenFlags,
        _lock_owner: Option<fuser::LockOwner>,
        reply: ReplyWrite,
    ) {
        // First cut: O_TRUNC semantics only.  An offset != 0 write
        // surfaces as EIO until the offset-aware kernel callback
        // lands.  This is honest about the gap rather than silently
        // dropping bytes; CC's task-file workflow always rewrites
        // the whole JSON document so offset==0 is the common path.
        if offset != 0 {
            reply.error(errno_io());
            return;
        }
        let path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        match self.sys_write(&path, data) {
            Ok(()) => reply.written(data.len() as u32),
            Err(_) => reply.error(errno_io()),
        }
    }

    fn flush(
        &self,
        _req: &Request,
        _ino: INodeNo,
        _fh: FileHandle,
        _lock_owner: fuser::LockOwner,
        reply: ReplyEmpty,
    ) {
        reply.ok();
    }

    fn release(
        &self,
        _req: &Request,
        _ino: INodeNo,
        _fh: FileHandle,
        _flags: fuser::OpenFlags,
        _lock_owner: Option<fuser::LockOwner>,
        _flush: bool,
        reply: ReplyEmpty,
    ) {
        reply.ok();
    }

    // ── KernelHandle v2 ops (PLUGIN_API_VERSION ≥ 2) ────────────────

    fn readdir(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        mut reply: fuser::ReplyDirectory,
    ) {
        let parent_path = match self.path_for(ino) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let json = match self.sys_readdir(&parent_path) {
            Ok(j) => j,
            Err(_) => {
                reply.error(errno_io());
                return;
            }
        };
        let entries = parse_readdir_entries(&json);
        // FUSE readdir is a streaming op: each `add` returns true once
        // the kernel buffer is full, after which we stop and let the
        // VFS resume from the offset we last reported.  Skipping
        // `offset` entries handles continuation correctly without
        // needing a per-fh cursor.
        const DT_DIR: u64 = 1;
        for (idx, (name, entry_type)) in entries.into_iter().enumerate().skip(offset as usize) {
            let child_path = join_path(&parent_path, &name);
            let child_ino = self.alloc_inode(child_path);
            let kind = if entry_type == DT_DIR {
                FileType::Directory
            } else {
                FileType::RegularFile
            };
            // FUSE's `next_offset` semantics: the next readdir call
            // resumes at this value, so we return `idx + 1`.
            if reply.add(INodeNo(child_ino), (idx + 1) as u64, kind, &name) {
                break;
            }
        }
        reply.ok();
    }

    fn mkdir(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &std::ffi::OsStr,
        _mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        if self.sys_mkdir(&path).is_err() {
            reply.error(errno_io());
            return;
        }
        let ino_raw = self.alloc_inode(path.clone());
        let ino = INodeNo(ino_raw);
        match self.stat_attr(ino, &path) {
            Ok(attr) => reply.entry(&ENTRY_TTL, &attr, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn unlink(&self, _req: &Request, parent: INodeNo, name: &std::ffi::OsStr, reply: ReplyEmpty) {
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        match self.sys_unlink(&path) {
            Ok(()) => reply.ok(),
            Err(_) => reply.error(errno_io()),
        }
    }

    fn rmdir(&self, _req: &Request, parent: INodeNo, name: &std::ffi::OsStr, reply: ReplyEmpty) {
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        match self.sys_rmdir(&path) {
            Ok(()) => reply.ok(),
            Err(_) => reply.error(errno_io()),
        }
    }

    fn rename(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &std::ffi::OsStr,
        newparent: INodeNo,
        newname: &std::ffi::OsStr,
        _flags: fuser::RenameFlags,
        reply: ReplyEmpty,
    ) {
        let old_parent = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let new_parent = match self.path_for(newparent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let old_name = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let new_name = match newname.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let old_path = join_path(&old_parent, old_name);
        let new_path = join_path(&new_parent, new_name);
        match self.sys_rename(&old_path, &new_path) {
            Ok(()) => reply.ok(),
            Err(_) => reply.error(errno_io()),
        }
    }

    fn create(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &std::ffi::OsStr,
        _mode: u32,
        _umask: u32,
        _flags: i32,
        reply: fuser::ReplyCreate,
    ) {
        // Compose create from an empty-payload `sys_write` (touches
        // the file into existence) + `sys_stat` (returns the populated
        // FileAttr).  Avoids adding a dedicated `sys_create` callback
        // — the kernel already treats "write to a non-existent path"
        // as create-on-write.
        let parent_path = match self.path_for(parent) {
            Some(p) => p,
            None => {
                reply.error(errno_enoent());
                return;
            }
        };
        let name_str = match name.to_str() {
            Some(s) => s,
            None => {
                reply.error(errno_einval());
                return;
            }
        };
        let path = join_path(&parent_path, name_str);
        if self.sys_write(&path, &[]).is_err() {
            reply.error(errno_io());
            return;
        }
        let ino_raw = self.alloc_inode(path.clone());
        let ino = INodeNo(ino_raw);
        match self.stat_attr(ino, &path) {
            Ok(attr) => reply.created(
                &ENTRY_TTL,
                &attr,
                Generation(0),
                FileHandle(0),
                fuser::FopenFlags::empty(),
            ),
            Err(e) => reply.error(e),
        }
    }

    fn setattr(
        &self,
        _req: &Request,
        _ino: INodeNo,
        _mode: Option<u32>,
        _uid: Option<u32>,
        _gid: Option<u32>,
        _size: Option<u64>,
        _atime: Option<fuser::TimeOrNow>,
        _mtime: Option<fuser::TimeOrNow>,
        _ctime: Option<SystemTime>,
        _fh: Option<FileHandle>,
        _crtime: Option<SystemTime>,
        _chgtime: Option<SystemTime>,
        _bkuptime: Option<SystemTime>,
        _flags: Option<fuser::BsdFileFlags>,
        reply: ReplyAttr,
    ) {
        reply.error(errno_nosys());
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
    fn json_u64_extracts_size() {
        assert_eq!(
            json_u64(r#"{"size":12345,"entry_type":0}"#, "\"size\":"),
            Some(12345)
        );
    }

    #[test]
    fn json_u64_extracts_entry_type() {
        // Directory entry — kernel reports entry_type=1 (DT_DIR).
        assert_eq!(
            json_u64(r#"{"size":4096,"entry_type":1}"#, "\"entry_type\":"),
            Some(1)
        );
        // Regular file — entry_type=0 (DT_REG).
        assert_eq!(
            json_u64(r#"{"size":12,"entry_type":0}"#, "\"entry_type\":"),
            Some(0)
        );
    }

    #[test]
    fn parse_readdir_entries_empty() {
        assert_eq!(parse_readdir_entries("[]"), Vec::<(String, u64)>::new());
    }

    #[test]
    fn parse_readdir_entries_mixed_kinds() {
        // Same shape `kernel_cb_sys_readdir` emits.
        let json = r#"[{"name":"foo.txt","entry_type":0},{"name":"sub","entry_type":1}]"#;
        let entries = parse_readdir_entries(json);
        assert_eq!(
            entries,
            vec![
                ("foo.txt".to_string(), 0),
                ("sub".to_string(), 1),
            ]
        );
    }

    #[test]
    fn parse_readdir_entries_handles_escapes() {
        // Kernel-side serializer escapes `"` → `\"` and `\` → `\\`.
        // The parser must un-escape them, otherwise lookups against
        // the resulting path will miss.
        let json = r#"[{"name":"with \"quote\"","entry_type":0},{"name":"with\\back","entry_type":0}]"#;
        let entries = parse_readdir_entries(json);
        assert_eq!(
            entries,
            vec![
                ("with \"quote\"".to_string(), 0),
                ("with\\back".to_string(), 0),
            ]
        );
    }
}
