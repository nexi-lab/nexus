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
/// Negative-entry TTL.  Must stay at zero — see `lookup`'s negative
/// branch for the full rationale: `mv <src> <new>` followed by a sub-
/// second `cat <new>` would otherwise hit the cached negative entry
/// from `mv`'s pre-rename LOOKUP(new) probe and surface stale ENOENT
/// despite the rename having succeeded kernel-side.  A `Notifier`-
/// based invalidation deadlocks fuser's single-threaded event loop
/// (the notify message can't dispatch while the only worker is
/// inside the rename handler), so zero-TTL negatives are the only
/// race-free option without a dedicated invalidation worker.
const NEG_ENTRY_TTL: Duration = Duration::ZERO;

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
    /// Bidirectional inode↔path index.  The forward map (`ino_to_path`)
    /// translates a FUSE inode back to the VFS path the kernel
    /// callbacks expect; the reverse map (`path_to_ino`) lets
    /// `lookup` return a **stable** inode for a path it has seen
    /// before — without it every LOOKUP minted a fresh inode and the
    /// kernel-side FUSE driver couldn't tie its cached dentry to the
    /// one the next op walked through (rename's `d_move` is the
    /// canonical case: source dentry got inode A on create, dest
    /// lookup returned inode B; the rename op's userspace handler ran
    /// fine but the kernel-side `d_move(A → B)` linked the wrong
    /// inode and downstream `cat <dest>` surfaced ENOENT despite the
    /// rename having succeeded kernel-side).  Root inode
    /// (FUSE_ROOT_RAW) is seeded with the operator-supplied
    /// `vfs_root` prefix; child paths register on first lookup.  We
    /// never evict — replacing the counter with a path-cache
    /// eviction policy is a follow-up.
    paths: Mutex<PathIndex>,
    next_inode: AtomicU64,
}

/// Bidirectional inode↔path index — the SSOT for "what does each FUSE
/// inode refer to right now".  Single mutex so both maps stay in
/// lockstep; every mutator updates both sides in one critical section.
struct PathIndex {
    ino_to_path: HashMap<u64, String>,
    path_to_ino: HashMap<String, u64>,
}

impl PathIndex {
    fn with_root(vfs_root: String) -> Self {
        let mut ino_to_path = HashMap::new();
        let mut path_to_ino = HashMap::new();
        ino_to_path.insert(FUSE_ROOT_RAW, vfs_root.clone());
        path_to_ino.insert(vfs_root, FUSE_ROOT_RAW);
        Self {
            ino_to_path,
            path_to_ino,
        }
    }

    fn lookup_or_register(&mut self, path: &str, mint: impl FnOnce() -> u64) -> u64 {
        if let Some(&ino) = self.path_to_ino.get(path) {
            return ino;
        }
        let ino = mint();
        self.ino_to_path.insert(ino, path.to_string());
        self.path_to_ino.insert(path.to_string(), ino);
        ino
    }

    /// Move the inode currently bound to `old_path` over to `new_path`.
    /// No-op when `old_path` isn't tracked (e.g. the kernel-side
    /// rename succeeded against a path the FUSE side never looked up,
    /// which can happen with bulk operators).  Used by the `rename`
    /// op so subsequent `read` / `getattr` on the d_moved inode walks
    /// the new path through `sys_*` — otherwise the inode would still
    /// resolve to `old_path` and every read would surface ENOENT.
    fn rename(&mut self, old_path: &str, new_path: &str) {
        let Some(ino) = self.path_to_ino.remove(old_path) else {
            return;
        };
        self.ino_to_path.insert(ino, new_path.to_string());
        self.path_to_ino.insert(new_path.to_string(), ino);
    }

    /// Drop the bidirectional mapping for `path`.  Used by `unlink`
    /// and `rmdir` to release the inode after the kernel-side entry
    /// is gone — a subsequent `create` at the same path then mints a
    /// fresh inode instead of reusing one whose previous lifetime
    /// the kernel may have already forgotten.
    fn forget(&mut self, path: &str) {
        if let Some(ino) = self.path_to_ino.remove(path) {
            self.ino_to_path.remove(&ino);
        }
    }
}

impl NexusFs {
    pub fn new(kernel: KernelHandle, vfs_root: String) -> Self {
        Self {
            kernel,
            paths: Mutex::new(PathIndex::with_root(vfs_root)),
            // First allocated inode = root + 1; the root inode itself
            // is reserved (fuser INodeNo::ROOT == 1).
            next_inode: AtomicU64::new(FUSE_ROOT_RAW + 1),
        }
    }

    fn path_for(&self, ino: INodeNo) -> Option<String> {
        self.paths.lock().unwrap().ino_to_path.get(&ino.0).cloned()
    }

    /// Return the existing inode for `path` if we've seen it before,
    /// otherwise mint a fresh one and register both directions.  See
    /// the `paths` field docstring for why a stable inode-per-path
    /// matters for FUSE's dentry/inode tracking.
    fn inode_for(&self, path: &str) -> u64 {
        self.paths
            .lock()
            .unwrap()
            .lookup_or_register(path, || self.next_inode.fetch_add(1, Ordering::Relaxed))
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
        // Kernel exports `entry_type` (the SSOT for "what kind of
        // inode this is"); `kind_from_entry_type` is the single rule
        // that maps it to a FUSE FileType.
        let size = json_u64(json, "\"size\":")?;
        let entry_type = json_u64(json, "\"entry_type\":")?;
        let kind = kind_from_entry_type(entry_type);
        let is_dir = kind == FileType::Directory;
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

/// FUSE-protocol negative-entry attr — `attr.ino == 0` signals
/// "no entry at this name" to the kernel.  Every other field is a
/// don't-care for negative entries, so we initialise to sane zeros
/// instead of carrying a separate `Option<FileAttr>` plumbing path.
/// Paired with `NEG_ENTRY_TTL = Duration::ZERO` in `lookup`'s
/// Err branch — see the rationale on `NEG_ENTRY_TTL`.
fn negative_entry_attr() -> FileAttr {
    let now = SystemTime::now();
    FileAttr {
        ino: INodeNo(0),
        size: 0,
        blocks: 0,
        atime: now,
        mtime: now,
        ctime: now,
        crtime: now,
        kind: FileType::RegularFile,
        perm: 0,
        nlink: 0,
        uid: 0,
        gid: 0,
        rdev: 0,
        blksize: 0,
        flags: 0,
    }
}

/// Single SSOT rule mapping kernel `entry_type` values to FUSE
/// `FileType`.  Both DT_DIR (1) and DT_MOUNT (2) surface as
/// directories — the kernel's `sys_stat` mount-point synthesis path
/// (kernel/src/kernel/io.rs §3.5) explicitly emits DT_MOUNT with
/// `is_directory: true` + `mode: 0o755`, so FUSE must treat them the
/// same or `mountpoint -q` / `cd <mount>` / `readdir` all break at
/// the mount root.  Every other `entry_type` (DT_REG / DT_LINK /
/// DT_PIPE / DT_STREAM / DT_EXTERNAL_STORAGE) maps to RegularFile —
/// the most conservative choice for kinds the FUSE layer doesn't
/// (yet) have first-class handling for.
fn kind_from_entry_type(entry_type: u64) -> FileType {
    const DT_DIR: u64 = 1;
    const DT_MOUNT: u64 = 2;
    if entry_type == DT_DIR || entry_type == DT_MOUNT {
        FileType::Directory
    } else {
        FileType::RegularFile
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
        // Stat first, allocate the inode only on a positive hit so
        // negative-lookup probes (`mv`'s pre-rename LOOKUP, every
        // ENOENT path resolution) don't leak path↔inode entries the
        // FUSE side then has to clean up.
        match self.sys_stat(&path) {
            Ok(json) => {
                let ino = INodeNo(self.inode_for(&path));
                match self.parse_stat(ino, &json) {
                    Some(attr) => reply.entry(&ENTRY_TTL, &attr, Generation(0)),
                    None => reply.error(errno_io()),
                }
            }
            // FUSE protocol: a reply.entry with `attr.ino == 0` is a
            // *negative* entry — the kernel records "this name does
            // not exist" for `entry_valid` seconds.  Setting TTL = 0
            // means the kernel doesn't cache the negative at all,
            // which is the SoT we need: every subsequent LOOKUP for
            // this name walks back through to `sys_stat` instead of
            // serving the stale "doesn't exist" answer that `mv`'s
            // pre-rename probe left behind.  See NEG_ENTRY_TTL.
            Err(_) => reply.entry(&NEG_ENTRY_TTL, &negative_entry_attr(), Generation(0)),
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
        for (idx, (name, entry_type)) in entries.into_iter().enumerate().skip(offset as usize) {
            let child_path = join_path(&parent_path, &name);
            let child_ino = self.inode_for(&child_path);
            let kind = kind_from_entry_type(entry_type);
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
        let ino_raw = self.inode_for(&path);
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
            Ok(()) => {
                self.paths.lock().unwrap().forget(&path);
                reply.ok();
            }
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
            Ok(()) => {
                self.paths.lock().unwrap().forget(&path);
                reply.ok();
            }
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
            Ok(()) => {
                // Move the inode's path mapping so the d_moved FUSE
                // dentry (which still points at the old inode) walks
                // through `sys_read` against the new path instead of
                // the now-vanished old one — without this, `cat <new>`
                // after `mv old new` hits ENOENT despite the kernel-
                // side rename having succeeded.
                self.paths.lock().unwrap().rename(&old_path, &new_path);
                reply.ok();
            }
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
        let ino_raw = self.inode_for(&path);
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
            vec![("foo.txt".to_string(), 0), ("sub".to_string(), 1),]
        );
    }

    #[test]
    fn kind_from_entry_type_mount_and_dir_are_directories() {
        // DT_DIR (1) and DT_MOUNT (2) — same FUSE surface.  Kernel
        // io.rs §3.5 synthesizes mount roots as DT_MOUNT with
        // `is_directory: true`, so this mapping is the SSOT for
        // "FUSE root is a directory" — without it `mountpoint -q`
        // fails the moment fuser hands off to the plugin.
        assert_eq!(kind_from_entry_type(1), FileType::Directory);
        assert_eq!(kind_from_entry_type(2), FileType::Directory);
    }

    #[test]
    fn kind_from_entry_type_regular_for_everything_else() {
        // DT_REG (0), DT_LINK (3), DT_PIPE (5+), DT_STREAM,
        // DT_EXTERNAL_STORAGE — all map to RegularFile until the
        // FUSE layer grows first-class handling.
        assert_eq!(kind_from_entry_type(0), FileType::RegularFile);
        assert_eq!(kind_from_entry_type(3), FileType::RegularFile);
        assert_eq!(kind_from_entry_type(99), FileType::RegularFile);
    }

    #[test]
    fn parse_readdir_entries_handles_escapes() {
        // Kernel-side serializer escapes `"` → `\"` and `\` → `\\`.
        // The parser must un-escape them, otherwise lookups against
        // the resulting path will miss.
        let json =
            r#"[{"name":"with \"quote\"","entry_type":0},{"name":"with\\back","entry_type":0}]"#;
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
