//! `NexusNfs` — `nfsserve::vfs::NFSFileSystem` impl that translates
//! NFSv3 ops into Nexus kernel syscalls via the `KernelHandle` callback
//! table.  macOS fallback path when FUSE-T is broken or missing.
//!
//! ## Why NFS localhost
//!
//! FUSE-T 1.2.7 is broken on macOS 26 (Tahoe): the go-nfsv4 backend
//! panics on every mount, SMB backend EOF's, and the FSKit backend is
//! blocked by a missing Apple entitlement.  FUSE-T is closed-source so
//! we can't fix it.  Instead we run an in-process NFSv3 server on
//! localhost and let macOS's native NFS client (`mount_nfs`) do the
//! mount — zero third-party runtime dependency.
//!
//! ## Architecture
//!
//! Same `KernelHandle` + `PathIndex` as the fuser path.  The NFS
//! server (nfsserve crate, MIT, HuggingFace-proven) runs in a tokio
//! runtime spawned by `spawn_nfs_mount`.  All NFS ops translate to the
//! same `kernel_callbacks::sys_*` wrappers that fuser uses.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::SystemTime;

use async_trait::async_trait;
use nfsserve::nfs::{
    fattr3, fileid3, filename3, ftype3, nfspath3, nfsstat3, nfstime3, sattr3, specdata3,
};
use nfsserve::tcp::{NFSTcp, NFSTcpListener};
use nfsserve::vfs::{DirEntry, NFSFileSystem, ReadDirResult, VFSCapabilities};

use nexus_plugin_abi::KernelHandle;

use crate::kernel_callbacks::{self, parse_readdir, parse_stat, DT_DIR, DT_MOUNT};
use crate::path_index::{join_path, PathIndex};

/// Root inode — matches fuser's `INodeNo::ROOT.0` so PathIndex seeds
/// identically regardless of which adapter creates it.
const NFS_ROOT_ID: fileid3 = 1;

/// NFS filesystem backed by Nexus kernel syscalls.
pub struct NexusNfs {
    kernel: KernelHandle,
    paths: Mutex<PathIndex>,
    next_inode: AtomicU64,
}

// SAFETY: KernelHandle is a bag of function pointers + an opaque ptr
// documented to be valid for the plugin's lifetime.  The NFS server's
// Arc<NexusNfs> is dropped before the plugin (spawn_nfs_mount's handle
// is stored in FusePlugin, which the loader drops before reclaiming
// the kernel).  All mutable state is behind Mutex/Atomic.
unsafe impl Send for NexusNfs {}
unsafe impl Sync for NexusNfs {}

impl NexusNfs {
    pub fn new(kernel: KernelHandle, vfs_root: String) -> Self {
        Self {
            kernel,
            paths: Mutex::new(PathIndex::with_root(NFS_ROOT_ID, vfs_root)),
            next_inode: AtomicU64::new(NFS_ROOT_ID + 1),
        }
    }

    fn path_for(&self, id: fileid3) -> Option<String> {
        self.paths.lock().unwrap().path_for(id)
    }

    fn inode_for(&self, path: &str) -> fileid3 {
        self.paths
            .lock()
            .unwrap()
            .lookup_or_register(path, || self.next_inode.fetch_add(1, Ordering::Relaxed))
    }

    /// Build an `fattr3` from a kernel `sys_stat` JSON result.
    fn make_fattr(&self, id: fileid3, stat_json: &str) -> Result<fattr3, nfsstat3> {
        let (size, entry_type) = parse_stat(stat_json).ok_or(nfsstat3::NFS3ERR_IO)?;
        let is_dir = entry_type == DT_DIR || entry_type == DT_MOUNT;
        let now = nfs_now();
        Ok(fattr3 {
            ftype: if is_dir {
                ftype3::NF3DIR
            } else {
                ftype3::NF3REG
            },
            mode: if is_dir { 0o755 } else { 0o644 },
            nlink: if is_dir { 2 } else { 1 },
            uid: unsafe { libc::getuid() },
            gid: unsafe { libc::getgid() },
            size,
            used: size,
            rdev: specdata3 {
                specdata1: 0,
                specdata2: 0,
            },
            fsid: 0,
            fileid: id,
            atime: now,
            mtime: now,
            ctime: now,
        })
    }

    /// Stat a path and return its fattr3.
    fn stat_fattr(&self, id: fileid3, path: &str) -> Result<fattr3, nfsstat3> {
        let json =
            kernel_callbacks::sys_stat(&self.kernel, path).map_err(|_| nfsstat3::NFS3ERR_NOENT)?;
        self.make_fattr(id, &json)
    }
}

fn nfs_now() -> nfstime3 {
    let dur = SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    nfstime3 {
        seconds: dur.as_secs() as u32,
        nseconds: dur.subsec_nanos(),
    }
}

#[async_trait]
impl NFSFileSystem for NexusNfs {
    fn root_dir(&self) -> fileid3 {
        NFS_ROOT_ID
    }

    fn capabilities(&self) -> VFSCapabilities {
        VFSCapabilities::ReadWrite
    }

    async fn lookup(&self, dirid: fileid3, filename: &filename3) -> Result<fileid3, nfsstat3> {
        let parent = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let name = std::str::from_utf8(filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let path = join_path(&parent, name);
        // Stat first — only allocate inode on positive hit.
        kernel_callbacks::sys_stat(&self.kernel, &path).map_err(|_| nfsstat3::NFS3ERR_NOENT)?;
        Ok(self.inode_for(&path))
    }

    async fn getattr(&self, id: fileid3) -> Result<fattr3, nfsstat3> {
        let path = self.path_for(id).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        self.stat_fattr(id, &path)
    }

    async fn setattr(&self, _id: fileid3, _setattr: sattr3) -> Result<fattr3, nfsstat3> {
        Err(nfsstat3::NFS3ERR_NOENT)
    }

    async fn read(
        &self,
        id: fileid3,
        offset: u64,
        count: u32,
    ) -> Result<(Vec<u8>, bool), nfsstat3> {
        let path = self.path_for(id).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let full =
            kernel_callbacks::sys_read(&self.kernel, &path).map_err(|_| nfsstat3::NFS3ERR_NOENT)?;
        let start = offset as usize;
        if start >= full.len() {
            return Ok((vec![], true));
        }
        let end = (start + count as usize).min(full.len());
        let eof = end >= full.len();
        Ok((full[start..end].to_vec(), eof))
    }

    async fn write(&self, id: fileid3, offset: u64, data: &[u8]) -> Result<fattr3, nfsstat3> {
        if offset != 0 {
            return Err(nfsstat3::NFS3ERR_IO);
        }
        let path = self.path_for(id).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        kernel_callbacks::sys_write(&self.kernel, &path, data).map_err(|_| nfsstat3::NFS3ERR_IO)?;
        self.stat_fattr(id, &path)
    }

    async fn create(
        &self,
        dirid: fileid3,
        filename: &filename3,
        _attr: sattr3,
    ) -> Result<(fileid3, fattr3), nfsstat3> {
        let parent = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let name = std::str::from_utf8(filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let path = join_path(&parent, name);
        kernel_callbacks::sys_write(&self.kernel, &path, &[]).map_err(|_| nfsstat3::NFS3ERR_IO)?;
        let id = self.inode_for(&path);
        let attr = self.stat_fattr(id, &path)?;
        Ok((id, attr))
    }

    async fn create_exclusive(
        &self,
        dirid: fileid3,
        filename: &filename3,
    ) -> Result<fileid3, nfsstat3> {
        let parent = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let name = std::str::from_utf8(filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let path = join_path(&parent, name);
        // Check existence first.
        if kernel_callbacks::sys_stat(&self.kernel, &path).is_ok() {
            return Err(nfsstat3::NFS3ERR_EXIST);
        }
        kernel_callbacks::sys_write(&self.kernel, &path, &[]).map_err(|_| nfsstat3::NFS3ERR_IO)?;
        Ok(self.inode_for(&path))
    }

    async fn mkdir(
        &self,
        dirid: fileid3,
        dirname: &filename3,
    ) -> Result<(fileid3, fattr3), nfsstat3> {
        let parent = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let name = std::str::from_utf8(dirname).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let path = join_path(&parent, name);
        kernel_callbacks::sys_mkdir(&self.kernel, &path).map_err(|_| nfsstat3::NFS3ERR_IO)?;
        let id = self.inode_for(&path);
        let attr = self.stat_fattr(id, &path)?;
        Ok((id, attr))
    }

    async fn remove(&self, dirid: fileid3, filename: &filename3) -> Result<(), nfsstat3> {
        let parent = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let name = std::str::from_utf8(filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let path = join_path(&parent, name);
        // Try unlink first (file), fall back to rmdir (directory).
        if kernel_callbacks::sys_unlink(&self.kernel, &path).is_err() {
            kernel_callbacks::sys_rmdir(&self.kernel, &path).map_err(|_| nfsstat3::NFS3ERR_IO)?;
        }
        self.paths.lock().unwrap().forget(&path);
        Ok(())
    }

    async fn rename(
        &self,
        from_dirid: fileid3,
        from_filename: &filename3,
        to_dirid: fileid3,
        to_filename: &filename3,
    ) -> Result<(), nfsstat3> {
        let from_parent = self.path_for(from_dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let to_parent = self.path_for(to_dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let from_name = std::str::from_utf8(from_filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let to_name = std::str::from_utf8(to_filename).map_err(|_| nfsstat3::NFS3ERR_INVAL)?;
        let old_path = join_path(&from_parent, from_name);
        let new_path = join_path(&to_parent, to_name);
        kernel_callbacks::sys_rename(&self.kernel, &old_path, &new_path)
            .map_err(|_| nfsstat3::NFS3ERR_IO)?;
        self.paths.lock().unwrap().rename(&old_path, &new_path);
        Ok(())
    }

    async fn readdir(
        &self,
        dirid: fileid3,
        start_after: fileid3,
        max_entries: usize,
    ) -> Result<ReadDirResult, nfsstat3> {
        let parent_path = self.path_for(dirid).ok_or(nfsstat3::NFS3ERR_NOENT)?;
        let json = kernel_callbacks::sys_readdir(&self.kernel, &parent_path)
            .map_err(|_| nfsstat3::NFS3ERR_IO)?;
        let entries = parse_readdir(&json);

        let mut result_entries = Vec::new();
        let mut past_start = start_after == 0;

        for (name, entry_type) in &entries {
            let child_path = join_path(&parent_path, name);
            let child_id = self.inode_for(&child_path);

            if !past_start {
                if child_id == start_after {
                    past_start = true;
                }
                continue;
            }

            let is_dir = *entry_type == DT_DIR || *entry_type == DT_MOUNT;
            let now = nfs_now();
            result_entries.push(DirEntry {
                fileid: child_id,
                name: name.as_bytes().into(),
                attr: fattr3 {
                    ftype: if is_dir {
                        ftype3::NF3DIR
                    } else {
                        ftype3::NF3REG
                    },
                    mode: if is_dir { 0o755 } else { 0o644 },
                    nlink: if is_dir { 2 } else { 1 },
                    uid: unsafe { libc::getuid() },
                    gid: unsafe { libc::getgid() },
                    size: 0,
                    used: 0,
                    rdev: specdata3 {
                        specdata1: 0,
                        specdata2: 0,
                    },
                    fsid: 0,
                    fileid: child_id,
                    atime: now,
                    mtime: now,
                    ctime: now,
                },
            });

            if result_entries.len() >= max_entries {
                break;
            }
        }

        let end = result_entries.len() < max_entries;
        Ok(ReadDirResult {
            entries: result_entries,
            end,
        })
    }

    async fn symlink(
        &self,
        _dirid: fileid3,
        _linkname: &filename3,
        _symlink: &nfspath3,
        _attr: &sattr3,
    ) -> Result<(fileid3, fattr3), nfsstat3> {
        Err(nfsstat3::NFS3ERR_NOENT)
    }

    async fn readlink(&self, _id: fileid3) -> Result<nfspath3, nfsstat3> {
        Err(nfsstat3::NFS3ERR_NOENT)
    }
}

// ── NFS mount lifecycle ────────────────────────────────────────────

/// Handle returned by `spawn_nfs_mount` — dropping it shuts down the
/// NFS server and unmounts the mount point.
pub struct NfsMountHandle {
    /// Port the NFS server is listening on.
    port: u16,
    /// Mount point path on the host filesystem.
    mount_point: String,
    /// Tokio runtime driving the NFS server.  Dropping it stops all
    /// spawned tasks (the `handle_forever` loop + connections).
    _runtime: tokio::runtime::Runtime,
}

impl Drop for NfsMountHandle {
    fn drop(&mut self) {
        // Best-effort force-unmount — `-f` is essential on macOS
        // because a normal unmount can hang if any process has an
        // open fd on the mount.  May fail if the mount was already
        // torn down (e.g. `diskutil unmount` from Finder).
        let _ = std::process::Command::new("/sbin/umount")
            .args(["-f", &self.mount_point])
            .output();
    }
}

impl NfsMountHandle {
    pub fn port(&self) -> u16 {
        self.port
    }
}

/// Force-unmount any stale NFS mount at `mount_point`.
///
/// macOS allows only **one** `localhost:/` NFS mount at a time.  When
/// the daemon is killed with SIGKILL, `Drop` doesn't run → the stale
/// mount blocks all subsequent `mount_nfs` calls with "No such file or
/// directory".  This function is idempotent: if there is no stale mount
/// the `umount -f` simply fails and we ignore the error.
fn cleanup_stale_nfs_mount(mount_point: &str) {
    // 1. Force-unmount the target mount point (may be stale).
    let _ = std::process::Command::new("/sbin/umount")
        .args(["-f", mount_point])
        .output();

    // 2. Scan for any other stale nexus NFS mounts (e.g. /tmp/nexus-nfs-*)
    //    from previously crashed daemon instances.
    if let Ok(output) = std::process::Command::new("/sbin/mount").output() {
        let mount_table = String::from_utf8_lossy(&output.stdout);
        for line in mount_table.lines() {
            // NFS mounts from us look like:
            //   localhost:/ on /tmp/nexus-nfs-XXXX (nfs, ...)
            //   localhost:/ on /Users/.../nexus-project (nfs, ...)
            if line.contains("localhost:/") && line.contains("nfs") {
                // Extract the mount path: "... on <path> (..."
                if let Some(on_idx) = line.find(" on ") {
                    let after_on = &line[on_idx + 4..];
                    if let Some(paren_idx) = after_on.find(" (") {
                        let stale_path = &after_on[..paren_idx];
                        // Don't re-unmount what we already did above
                        if stale_path != mount_point {
                            eprintln!("[nexus-fuse-plugin] cleaning stale NFS mount: {stale_path}");
                            let _ = std::process::Command::new("/sbin/umount")
                                .args(["-f", stale_path])
                                .output();
                        }
                    }
                }
            }
        }
    }
}

/// Bind an NFS server on localhost, mount it at `mount_point`, and
/// return the handle.  The NFS server runs in a dedicated tokio
/// runtime so it doesn't interfere with fuser's thread model.
pub fn spawn_nfs_mount(
    nfs_fs: NexusNfs,
    mount_point: &str,
) -> Result<NfsMountHandle, Box<dyn std::error::Error>> {
    // Clean up stale NFS mounts from crashed daemon instances before
    // we bind a new NFS server.  Without this, macOS rejects the new
    // mount_nfs with "No such file or directory".
    cleanup_stale_nfs_mount(mount_point);

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .thread_name("nexus-nfs")
        .build()?;

    let (port, listener) = rt.block_on(async {
        let listener = NFSTcpListener::bind("127.0.0.1:0", nfs_fs).await?;
        let port = listener.get_listen_port();
        eprintln!("[nexus-fuse-plugin] NFS server bound to 127.0.0.1:{port}");
        Ok::<_, std::io::Error>((port, listener))
    })?;

    // Spawn the NFS server loop — runs forever until the runtime is
    // dropped.
    rt.spawn(async move {
        if let Err(e) = listener.handle_forever().await {
            eprintln!("[nexus-fuse-plugin] NFS server error: {e}");
        }
    });

    // Ensure mount point directory exists.
    std::fs::create_dir_all(mount_point)?;

    // Wait for the NFS server to start accepting connections.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        if std::net::TcpStream::connect(format!("127.0.0.1:{port}")).is_ok() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }

    // Mount via macOS native `mount_nfs`.  Options:
    //   nolocks  — no NLM (kernel doesn't support locking)
    //   vers=3   — NFSv3 (nfsserve speaks v3)
    //   tcp      — TCP transport
    //   port=P   — NFS server port
    //   mountport=P — mount daemon port (same, nfsserve handles both)
    let mount_output = std::process::Command::new("/sbin/mount_nfs")
        .args([
            "-o",
            &format!("nolocks,noresvport,vers=3,tcp,port={port},mountport={port}"),
            "localhost:/",
            mount_point,
        ])
        .output()?;

    if !mount_output.status.success() {
        let stderr = String::from_utf8_lossy(&mount_output.stderr);
        return Err(format!("mount_nfs failed: {stderr}").into());
    }

    Ok(NfsMountHandle {
        port,
        mount_point: mount_point.to_string(),
        _runtime: rt,
    })
}
