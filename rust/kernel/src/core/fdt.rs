//! FileDescriptorTable — kernel primitive for pre-opened file descriptors.
//!
//! Mirrors the PipeManager / StreamManager pattern: a `DashMap` registry
//! of runtime state keyed by VFS path. `sys_write` registers an fd after
//! a successful backend write (PAS backends only — CAS/remote return
//! `None` from `resolve_physical_path`). `sys_read` hits the FDT before
//! falling through to the full backend read path.
//!
//! Uses `libc::pread` / `libc::pwrite` — atomic, no seek state, thread-safe.
//! Each `FileHandle` owns its `RawFd` and closes it on Drop.

use std::path::Path;

// ── Non-Unix stub ─────────────────────────────────────────────────────
// FDT is Unix-only (libc::pread/pwrite). On Windows the struct is a
// no-op: all reads miss, writes are no-ops. Production targets Linux/macOS.

#[cfg(not(unix))]
pub(crate) struct FileDescriptorTable;

#[cfg(not(unix))]
impl FileDescriptorTable {
    pub(crate) fn new() -> Self {
        Self
    }
    pub(crate) fn register(&self, _vfs_path: &str, _physical_path: &Path) -> bool {
        false
    }
    pub(crate) fn pread(&self, _vfs_path: &str) -> Option<Vec<u8>> {
        None
    }
    pub(crate) fn remove(&self, _vfs_path: &str) {}
    pub(crate) fn rename(&self, _old_path: &str, _new_path: &str) {}
    #[allow(dead_code)]
    pub(crate) fn close_all(&self) {}
}

// ── Unix implementation ───────────────────────────────────────────────

#[cfg(unix)]
use dashmap::DashMap;
#[cfg(unix)]
use std::os::unix::io::RawFd;
#[cfg(unix)]
use std::sync::Arc;

#[cfg(unix)]
/// Owns a raw file descriptor; closes on drop.
struct FileHandle {
    fd: RawFd,
}

#[cfg(unix)]
impl FileHandle {
    /// Open `path` with O_RDWR. Returns `None` if open fails.
    fn open(path: &Path) -> Option<Self> {
        use std::ffi::CString;
        let c_path = CString::new(path.as_os_str().as_encoded_bytes()).ok()?;
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR) };
        if fd < 0 {
            None
        } else {
            Some(Self { fd })
        }
    }

    /// Atomic positioned read — no seek state mutation.
    fn pread(&self, buf: &mut [u8], offset: i64) -> isize {
        unsafe { libc::pread(self.fd, buf.as_mut_ptr().cast(), buf.len(), offset) }
    }

    /// Atomic positioned write — no seek state mutation.
    #[allow(dead_code)]
    fn pwrite(&self, buf: &[u8], offset: i64) -> isize {
        unsafe { libc::pwrite(self.fd, buf.as_ptr().cast(), buf.len(), offset) }
    }

    /// File size via fstat.
    fn size(&self) -> Option<u64> {
        unsafe {
            let mut stat: libc::stat = std::mem::zeroed();
            if libc::fstat(self.fd, &mut stat) == 0 {
                Some(stat.st_size as u64)
            } else {
                None
            }
        }
    }
}

#[cfg(unix)]
impl Drop for FileHandle {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.fd);
        }
    }
}

// SAFETY: RawFd is an integer; pread/pwrite are thread-safe (no shared seek cursor).
#[cfg(unix)]
unsafe impl Send for FileHandle {}
#[cfg(unix)]
unsafe impl Sync for FileHandle {}

#[cfg(unix)]
/// Kernel-internal file descriptor table — pre-opened fds for PAS backends.
///
/// Lifecycle:
/// - `register`: called by `sys_write` after a successful PAS write.
/// - `pread`: called by `sys_read` as a fast path (skip VFS lock + backend I/O).
/// - `remove`: called by `sys_unlink`.
/// - `rename`: called by `sys_rename`.
/// - `close_all`: called on kernel shutdown / metastore release.
pub(crate) struct FileDescriptorTable {
    fds: DashMap<String, Arc<FileHandle>>,
}

#[cfg(unix)]
impl FileDescriptorTable {
    pub(crate) fn new() -> Self {
        Self {
            fds: DashMap::new(),
        }
    }

    /// Register (or replace) an fd for `vfs_path`. Returns true if opened successfully.
    pub(crate) fn register(&self, vfs_path: &str, physical_path: &Path) -> bool {
        if let Some(handle) = FileHandle::open(physical_path) {
            self.fds.insert(vfs_path.to_string(), Arc::new(handle));
            true
        } else {
            false
        }
    }

    /// Read the entire file content via the pre-opened fd. Returns `None` on miss.
    pub(crate) fn pread(&self, vfs_path: &str) -> Option<Vec<u8>> {
        let handle = self.fds.get(vfs_path)?;
        let size = handle.size()? as usize;
        if size == 0 {
            return Some(Vec::new());
        }
        let mut buf = vec![0u8; size];
        let n = handle.pread(&mut buf, 0);
        if n < 0 {
            return None;
        }
        buf.truncate(n as usize);
        Some(buf)
    }

    /// Remove and close the fd for `vfs_path`.
    pub(crate) fn remove(&self, vfs_path: &str) {
        self.fds.remove(vfs_path);
    }

    /// Re-key an fd from `old_path` to `new_path` (Unix rename keeps fd valid).
    pub(crate) fn rename(&self, old_path: &str, new_path: &str) {
        if let Some((_, handle)) = self.fds.remove(old_path) {
            self.fds.insert(new_path.to_string(), handle);
        }
    }

    /// Close all fds (kernel shutdown).
    #[allow(dead_code)]
    pub(crate) fn close_all(&self) {
        self.fds.clear();
    }
}

// ── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn register_and_pread() {
        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("hello.txt");
        std::fs::write(&file_path, b"hello world").unwrap();

        let fdt = FileDescriptorTable::new();
        assert!(fdt.pread("/test/hello.txt").is_none());

        assert!(fdt.register("/test/hello.txt", &file_path));
        let data = fdt.pread("/test/hello.txt").unwrap();
        assert_eq!(data, b"hello world");
    }

    #[test]
    fn miss_returns_none() {
        let fdt = FileDescriptorTable::new();
        assert!(fdt.pread("/nonexistent").is_none());
    }

    #[test]
    fn remove_closes_fd() {
        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("rm.txt");
        std::fs::write(&file_path, b"data").unwrap();

        let fdt = FileDescriptorTable::new();
        fdt.register("/rm", &file_path);
        assert!(fdt.pread("/rm").is_some());

        fdt.remove("/rm");
        assert!(fdt.pread("/rm").is_none());
    }

    #[test]
    fn rename_preserves_fd() {
        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("orig.txt");
        std::fs::write(&file_path, b"content").unwrap();

        let fdt = FileDescriptorTable::new();
        fdt.register("/old", &file_path);

        fdt.rename("/old", "/new");
        assert!(fdt.pread("/old").is_none());
        assert_eq!(fdt.pread("/new").unwrap(), b"content");
    }

    #[test]
    fn close_all_clears_table() {
        let dir = tempfile::tempdir().unwrap();
        let f1 = dir.path().join("a.txt");
        let f2 = dir.path().join("b.txt");
        std::fs::write(&f1, b"a").unwrap();
        std::fs::write(&f2, b"b").unwrap();

        let fdt = FileDescriptorTable::new();
        fdt.register("/a", &f1);
        fdt.register("/b", &f2);

        fdt.close_all();
        assert!(fdt.pread("/a").is_none());
        assert!(fdt.pread("/b").is_none());
    }

    #[test]
    fn pread_reflects_external_write() {
        let dir = tempfile::tempdir().unwrap();
        let file_path = dir.path().join("mut.txt");
        std::fs::write(&file_path, b"v1").unwrap();

        let fdt = FileDescriptorTable::new();
        fdt.register("/mut", &file_path);
        assert_eq!(fdt.pread("/mut").unwrap(), b"v1");

        // External write (simulates a subsequent sys_write that wrote new content)
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&file_path)
            .unwrap();
        f.write_all(b"v2-longer").unwrap();
        f.flush().unwrap();
        drop(f);

        // pread reflects the new content (fd stays valid after external truncate+write)
        let data = fdt.pread("/mut").unwrap();
        assert_eq!(data, b"v2-longer");
    }
}
