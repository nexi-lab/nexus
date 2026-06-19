//! FUSE filesystem implementation for Nexus.

use crate::cache::FileCache;
use crate::cached_read::{read_with_cache, CachedReadResult};
use crate::client::{FileEntry, NexusClient};
use crate::error::NexusClientError;
use crate::metrics;
use crate::passthrough::{ActivePassthrough, OpenAccess, PassthroughDecision, PassthroughManager};
use fuser::{
    AccessFlags, BsdFileFlags, Errno, FileAttr, FileHandle, FileType, Filesystem, FopenFlags,
    Generation, INodeNo, InitFlags, KernelConfig, LockOwner, OpenFlags, RenameFlags, ReplyAttr,
    ReplyData, ReplyDirectory, ReplyEntry, ReplyWrite, ReplyXattr, Request, WriteFlags,
};
use log::{debug, error};
use lru::LruCache;
use std::ffi::OsStr;
use std::io;
use std::num::NonZeroUsize;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// TTL for attribute caching (30s for better read performance).
const ATTR_TTL: Duration = Duration::from_secs(30);

/// Default block size.
const BLOCK_SIZE: u32 = 512;

const FUSE_ROOT_ID: u64 = INodeNo::ROOT.0;

/// Maximum number of inode entries to keep in the LRU maps.
/// Prevents unbounded memory growth (Issue #1569 / 1A).
/// At ~200 bytes per entry, 100K entries ≈ 20MB.
const MAX_INODE_ENTRIES: usize = 100_000;

const XATTR_GEN: &str = "user.nexus.gen";

/// Maximum number of open file contents to keep for range reads.
const MAX_OPEN_FILE_HANDLES: usize = 128;

/// Maximum total bytes retained by open file contents.
const MAX_OPEN_FILE_CACHE_BYTES: usize = 128 * 1024 * 1024;

/// Maximum stack depth requested for kernel passthrough.
const PASSTHROUGH_MAX_STACK_DEPTH: u32 = 2;

struct OpenFileCacheEntry {
    path: String,
    content: Vec<u8>,
}

struct OpenFileCache {
    entries: LruCache<u64, OpenFileCacheEntry>,
    total_bytes: usize,
    max_bytes: usize,
}

impl OpenFileCache {
    fn new(capacity: NonZeroUsize, max_bytes: usize) -> Self {
        Self {
            entries: LruCache::new(capacity),
            total_bytes: 0,
            max_bytes,
        }
    }

    fn get(&mut self, fh: &u64) -> Option<&OpenFileCacheEntry> {
        self.entries.get(fh)
    }

    fn put(&mut self, fh: u64, path: String, content: Vec<u8>) {
        if content.len() > self.max_bytes {
            self.remove(fh);
            return;
        }

        let content_len = content.len();
        if let Some((_, old_entry)) = self.entries.push(fh, OpenFileCacheEntry { path, content }) {
            self.total_bytes = self.total_bytes.saturating_sub(old_entry.content.len());
        }
        self.total_bytes = self.total_bytes.saturating_add(content_len);
        self.evict_to_budget();
    }

    fn remove(&mut self, fh: u64) {
        if let Some(entry) = self.entries.pop(&fh) {
            self.total_bytes = self.total_bytes.saturating_sub(entry.content.len());
        }
    }

    fn invalidate_path(&mut self, path: &str) {
        let stale_handles: Vec<u64> = self
            .entries
            .iter()
            .filter_map(|(fh, entry)| if entry.path == path { Some(*fh) } else { None })
            .collect();
        for fh in stale_handles {
            self.remove(fh);
        }
    }

    fn evict_to_budget(&mut self) {
        while self.total_bytes > self.max_bytes {
            if let Some((_, entry)) = self.entries.pop_lru() {
                self.total_bytes = self.total_bytes.saturating_sub(entry.content.len());
            } else {
                self.total_bytes = 0;
                break;
            }
        }
    }
}

/// Unified inode table combining bidirectional maps and counter under a single
/// lock. Eliminates the race condition in `get_or_create_inode()` (Issue 7A)
/// where releasing one lock before acquiring another could allow duplicate
/// allocations. LRU bounds prevent unbounded memory growth (Issue 1A).
struct InodeTable {
    inode_to_path: LruCache<u64, String>,
    path_to_inode: LruCache<String, u64>,
    next_inode: u64,
}

impl InodeTable {
    fn new() -> Self {
        let cap = NonZeroUsize::new(MAX_INODE_ENTRIES).unwrap();
        let mut inode_to_path = LruCache::new(cap);
        let mut path_to_inode = LruCache::new(cap);

        // Root inode — always present, re-pinned after eviction-prone operations.
        inode_to_path.put(FUSE_ROOT_ID, "/".to_string());
        path_to_inode.put("/".to_string(), FUSE_ROOT_ID);

        Self {
            inode_to_path,
            path_to_inode,
            next_inode: FUSE_ROOT_ID + 1,
        }
    }

    /// Ensure root inode is present in both maps (Issue #3029 / Bug 4).
    /// Called after operations that could trigger LRU eviction.
    fn ensure_root_pinned(&mut self) {
        if self.inode_to_path.peek(&FUSE_ROOT_ID).is_none() {
            self.inode_to_path.put(FUSE_ROOT_ID, "/".to_string());
        }
        if self.path_to_inode.peek("/").is_none() {
            self.path_to_inode.put("/".to_string(), FUSE_ROOT_ID);
        }
    }

    /// Get or create inode for a path. Race-free because both maps and the
    /// counter are behind the same Mutex.
    fn get_or_create(&mut self, path: &str) -> u64 {
        if let Some(&inode) = self.path_to_inode.get(path) {
            return inode;
        }

        let inode = self.next_inode;
        self.next_inode += 1;

        // Insert into both maps, synchronizing evictions (Issue #3029 / Bug 4).
        // `push` returns the evicted (key, value) if the cache was at capacity.
        if let Some((_evicted_path, evicted_inode)) =
            self.path_to_inode.push(path.to_string(), inode)
        {
            // path_to_inode evicted an entry; remove the stale reverse mapping.
            self.inode_to_path.pop(&evicted_inode);
        }
        if let Some((_evicted_inode, evicted_path)) =
            self.inode_to_path.push(inode, path.to_string())
        {
            // inode_to_path evicted an entry; remove the stale reverse mapping.
            self.path_to_inode.pop(&evicted_path);
        }

        // Re-pin root after potential evictions
        self.ensure_root_pinned();

        inode
    }

    /// Get path for an inode.
    fn get_path(&mut self, inode: u64) -> Option<String> {
        self.inode_to_path.get(&inode).cloned()
    }

    /// Remove a path mapping (e.g., after rename/delete).
    #[allow(dead_code)]
    fn remove_path(&mut self, path: &str) -> Option<u64> {
        if let Some(inode) = self.path_to_inode.pop(path) {
            self.inode_to_path.pop(&inode);
            Some(inode)
        } else {
            None
        }
    }

    /// Update path for an existing inode (rename).
    /// Uses `push` for synchronized eviction and re-pins root (Issue #3029 / Bug 4).
    fn rename_path(&mut self, old_path: &str, new_path: &str) {
        if let Some(inode) = self.path_to_inode.pop(old_path) {
            self.inode_to_path.pop(&inode);

            // Re-insert with synchronized eviction (same pattern as get_or_create)
            if let Some((_evicted_path, evicted_inode)) =
                self.path_to_inode.push(new_path.to_string(), inode)
            {
                self.inode_to_path.pop(&evicted_inode);
            }
            if let Some((_evicted_inode, evicted_path)) =
                self.inode_to_path.push(inode, new_path.to_string())
            {
                self.path_to_inode.pop(&evicted_path);
            }

            self.ensure_root_pinned();
        }
    }

    /// Get inode for a path (promotes in LRU).
    #[cfg(test)]
    fn get_inode(&mut self, path: &str) -> Option<u64> {
        self.path_to_inode.get(path).copied()
    }

    /// Peek inode for a path without promoting in LRU (Issue #3029 / Issue 8).
    /// Used for speculative lookups where promotion is undesirable.
    fn peek_inode(&self, path: &str) -> Option<u64> {
        self.path_to_inode.peek(path).copied()
    }
}

/// Resolve inode to path, returning ENOENT on the reply if not found.
/// Eliminates the 12x repeated "get path or ENOENT" pattern (Issue 6A).
macro_rules! resolve_path {
    ($self:expr, $inode:expr, $reply:expr) => {
        match $self.inodes.lock().unwrap().get_path($inode) {
            Some(p) => p,
            None => {
                $reply.error(Errno::ENOENT);
                return;
            }
        }
    };
}

/// Map a NexusClientError to a fuser Errno via the typed `to_errno()`
/// helper, so HTTP 401/403, RPC -32003/-32004, -32001, -32002,
/// -32005, -32006 each surface as their proper errno
/// (EACCES/EPERM/EEXIST/EINVAL/EAGAIN) instead of getting flattened
/// to EIO at the FUSE boundary (#4056 R4).
///
/// Callers should still log the error when it isn't NotFound or a
/// permission error — those are routine, everything else means the
/// backend is misbehaving and the operator wants to see it.
fn errno_for(e: &crate::error::NexusClientError) -> Errno {
    Errno::from_i32(e.to_errno())
}

/// Nexus FUSE filesystem.
pub struct NexusFs {
    client: Arc<NexusClient>,
    /// Unified inode table (Issue 1A/7A: single lock, LRU-bounded).
    inodes: Mutex<InodeTable>,
    /// Attribute cache (in-memory, short TTL).
    attr_cache: Mutex<LruCache<u64, (FileAttr, SystemTime)>>,
    /// Directory listing cache (in-memory).
    dir_cache: Mutex<LruCache<u64, (Vec<FileEntry>, SystemTime)>>,
    /// Persistent foyer cache for file content (optional).
    file_cache: Option<Arc<FileCache>>,
    /// Optional kernel passthrough manager for large read-only file opens.
    passthrough: Option<Arc<PassthroughManager>>,
    /// Per-open file content cache for range reads that bypass persistent cache size limits.
    open_file_cache: Mutex<OpenFileCache>,
    next_file_handle: Mutex<u64>,
}

impl NexusFs {
    /// Create a new NexusFs instance.
    pub fn new(
        client: NexusClient,
        file_cache: Option<Arc<FileCache>>,
        passthrough: Option<Arc<PassthroughManager>>,
    ) -> Self {
        Self {
            client: Arc::new(client),
            inodes: Mutex::new(InodeTable::new()),
            attr_cache: Mutex::new(LruCache::new(NonZeroUsize::new(10000).unwrap())),
            dir_cache: Mutex::new(LruCache::new(NonZeroUsize::new(1000).unwrap())),
            file_cache,
            passthrough,
            open_file_cache: Mutex::new(OpenFileCache::new(
                NonZeroUsize::new(MAX_OPEN_FILE_HANDLES).unwrap(),
                MAX_OPEN_FILE_CACHE_BYTES,
            )),
            next_file_handle: Mutex::new(1),
        }
    }

    #[cfg(test)]
    fn passthrough_enabled_for_tests(&self) -> bool {
        self.passthrough.is_some()
    }

    #[cfg(test)]
    fn passthrough_runtime_failure_is_fatal_for_tests(&self) -> bool {
        self.passthrough_runtime_failure_is_fatal()
    }

    #[cfg(test)]
    fn passthrough_max_stack_depth_for_tests() -> u32 {
        PASSTHROUGH_MAX_STACK_DEPTH
    }

    /// Parse timestamp string to SystemTime.
    fn parse_time(time_str: Option<&String>) -> SystemTime {
        time_str
            .and_then(|s| {
                // Try parsing ISO 8601 format
                chrono::DateTime::parse_from_rfc3339(s)
                    .ok()
                    .map(|dt| UNIX_EPOCH + Duration::from_secs(dt.timestamp() as u64))
            })
            .unwrap_or(UNIX_EPOCH)
    }

    /// Create FileAttr from metadata.
    fn make_attr(
        &self,
        inode: u64,
        entry_type: &str,
        size: u64,
        created: Option<&String>,
        updated: Option<&String>,
    ) -> FileAttr {
        let kind = if entry_type == "directory" {
            FileType::Directory
        } else {
            FileType::RegularFile
        };

        let ctime = Self::parse_time(created);
        let mtime = Self::parse_time(updated);
        let atime = mtime;

        let nlink = if kind == FileType::Directory { 2 } else { 1 };
        // Use permissive permissions - access control is done by Nexus API key
        let perm = if kind == FileType::Directory {
            0o777
        } else {
            0o666
        };

        FileAttr {
            ino: INodeNo(inode),
            size,
            blocks: size.div_ceil(BLOCK_SIZE as u64),
            atime,
            mtime,
            ctime,
            crtime: ctime,
            kind,
            perm,
            nlink,
            uid: unsafe { libc::getuid() },
            gid: unsafe { libc::getgid() },
            rdev: 0,
            blksize: BLOCK_SIZE,
            flags: 0,
        }
    }

    /// Check if a path is a directory using attr_cache first, falling
    /// back to stat() RPC. Replaces the old is_directory() which made a
    /// full parent list() RPC every call — 50-200ms waste (Issue 13A).
    ///
    /// #4056 R5: returns the typed NexusClientError on RPC failure so
    /// callers can map it to the correct errno (EACCES/EPERM/etc.)
    /// instead of flattening every error to ENOENT.
    fn check_is_directory(&self, path: &str) -> Result<bool, crate::error::NexusClientError> {
        if path == "/" {
            return Ok(true);
        }

        // Fast path: check attr_cache for existing info
        {
            let inodes = self.inodes.lock().unwrap();
            if let Some(inode) = inodes.peek_inode(path) {
                let mut cache = self.attr_cache.lock().unwrap();
                if let Some((attr, cached_at)) = cache.get(&inode) {
                    if cached_at.elapsed().unwrap_or(Duration::MAX) < ATTR_TTL {
                        return Ok(attr.kind == FileType::Directory);
                    }
                }
            }
        }

        // Slow path: single stat() RPC.
        self.client.stat(path).map(|meta| meta.is_directory)
    }

    /// Get attributes for a path, using cache.
    fn get_attr(&self, inode: u64, path: &str) -> Result<FileAttr, Errno> {
        // Check cache first
        {
            let mut cache = self.attr_cache.lock().unwrap();
            if let Some((attr, cached_at)) = cache.get(&inode) {
                if cached_at.elapsed().unwrap_or(Duration::MAX) < ATTR_TTL {
                    return Ok(*attr);
                }
            }
        }

        // Root always exists as a directory
        if path == "/" {
            let attr = self.make_attr(inode, "directory", 0, None, None);
            let mut cache = self.attr_cache.lock().unwrap();
            cache.put(inode, (attr, SystemTime::now()));
            return Ok(attr);
        }

        // Use stat() for single API call
        match self.client.stat(path) {
            Ok(meta) => {
                let entry_type = if meta.is_directory {
                    "directory"
                } else {
                    "file"
                };
                let attr = self.make_attr(
                    inode,
                    entry_type,
                    meta.size,
                    None,
                    meta.modified_at.as_ref(),
                );
                let mut cache = self.attr_cache.lock().unwrap();
                cache.put(inode, (attr, SystemTime::now()));
                Ok(attr)
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("get_attr error for {}: {}", path, e);
                }
                Err(errno_for(&e))
            }
        }
    }

    /// Join parent path with child name.
    fn join_path(parent: &str, name: &str) -> String {
        if parent == "/" {
            format!("/{}", name)
        } else {
            format!("{}/{}", parent, name)
        }
    }

    fn generation_xattr_value(gen: u64) -> Vec<u8> {
        gen.to_string().into_bytes()
    }

    fn generation_xattr_name_list() -> Vec<u8> {
        let mut names = XATTR_GEN.as_bytes().to_vec();
        names.push(0);
        names
    }

    fn reply_xattr(reply: ReplyXattr, value: &[u8], size: u32) {
        if size == 0 {
            reply.size(value.len() as u32);
        } else if value.len() > size as usize {
            reply.error(Errno::ERANGE);
        } else {
            reply.data(value);
        }
    }

    fn stat_gen(&self, path: &str) -> Result<u64, Errno> {
        self.client.stat(path).map(|meta| meta.gen).map_err(|e| {
            if !e.is_not_found() && !e.is_permission_denied() {
                error!("stat error for {}: {}", path, e);
            }
            errno_for(&e)
        })
    }

    /// Invalidate caches for a path.
    ///
    /// H22 fix: Release inodes lock before acquiring attr_cache/dir_cache
    /// to prevent deadlock from inconsistent lock ordering.
    fn invalidate_path(&self, path: &str) {
        // Extract inode info while holding inodes lock, then release
        let (inode, parent_inode) = {
            let inodes = self.inodes.lock().unwrap();
            let inode = inodes.peek_inode(path);
            let parent_inode = if let Some(parent) = std::path::Path::new(path).parent() {
                let parent_path = parent.to_string_lossy().to_string();
                let parent_path = if parent_path.is_empty() {
                    "/".to_string()
                } else {
                    parent_path
                };
                inodes.peek_inode(&parent_path)
            } else {
                None
            };
            (inode, parent_inode)
        }; // inodes lock released here

        // Now acquire secondary locks without holding inodes
        if let Some(ino) = inode {
            self.attr_cache.lock().unwrap().pop(&ino);
            self.dir_cache.lock().unwrap().pop(&ino);
        }
        if let Some(ino) = parent_inode {
            self.dir_cache.lock().unwrap().pop(&ino);
        }

        // Invalidate persistent cache
        if let Some(ref cache) = self.file_cache {
            cache.invalidate(path);
        }

        if let Some(ref passthrough) = self.passthrough {
            passthrough.invalidate_path(path);
        }

        // Invalidate open-handle content caches for this path.
        self.open_file_cache.lock().unwrap().invalidate_path(path);
    }

    fn allocate_file_handle(&self) -> anyhow::Result<u64> {
        if let Some(ref passthrough) = self.passthrough {
            return passthrough.next_file_handle();
        }

        let mut next = self.next_file_handle.lock().unwrap();
        let fh = *next;
        *next = next.wrapping_add(1);
        if *next == 0 {
            *next = 1;
        }
        Ok(fh)
    }

    fn reply_data_slice(content: &[u8], offset: u64, size: u32, reply: ReplyData) {
        let offset = offset as usize;
        if offset >= content.len() {
            reply.data(&[]);
        } else {
            let end = std::cmp::min(offset + size as usize, content.len());
            reply.data(&content[offset..end]);
        }
    }

    fn slice_len(content: &[u8], offset: u64, size: u32) -> usize {
        let offset = offset as usize;
        if offset >= content.len() {
            0
        } else {
            std::cmp::min(offset + size as usize, content.len()) - offset
        }
    }

    /// Read file with foyer cache and ETag support.
    ///
    /// Cache flow:
    /// 1. Check foyer cache
    /// 2. If hit and fresh -> return cached content
    /// 3. If hit but stale with etag -> send If-None-Match request
    /// 4. If server returns 304 -> touch cache, return cached content
    /// 5. If server returns 200 -> update cache, return new content
    /// 6. If miss -> fetch from server, store in cache
    fn read_cached(&self, path: &str, gen: u64) -> anyhow::Result<CachedReadResult> {
        read_with_cache(&self.client, self.file_cache.as_deref(), path, gen).map_err(Into::into)
    }

    /// Read the authoritative current bytes for an RMW source.
    ///
    /// Used by partial-write (offset != 0) and non-zero truncate.
    /// Deliberately bypasses `read_cached` / the FileCache so a stale
    /// cache entry can't end up as the source for a full-file rewrite
    /// (#4056 R6). The lost-update window between this read and the
    /// follow-up `client.write` is still open — the server's `write`
    /// RPC doesn't accept an `if_match` / generation guard yet; a
    /// follow-up that adds conditional writes will close it.
    ///
    /// Extracted as a method so a regression test can exercise the
    /// "no cache involvement" property: even with a primed FileCache,
    /// this call must hit the backend `/api/nfs/read` endpoint.
    fn rmw_read(&self, path: &str) -> Result<Vec<u8>, crate::error::NexusClientError> {
        self.client.read(path)
    }

    fn passthrough_runtime_failure_is_fatal(&self) -> bool {
        self.passthrough
            .as_ref()
            .is_some_and(|passthrough| passthrough.require())
    }

    fn open_userspace(&self, reply: fuser::ReplyOpen) {
        match self.allocate_file_handle() {
            Ok(fh) => reply.opened(FileHandle(fh), FopenFlags::empty()),
            Err(err) => {
                error!("file handle allocation failed: {}", err);
                reply.error(Errno::EIO);
            }
        }
    }

    fn reply_passthrough_open_failure<E: std::fmt::Display>(
        &self,
        path: &str,
        operation: &str,
        err: E,
        reply: fuser::ReplyOpen,
    ) {
        if self.passthrough_runtime_failure_is_fatal() {
            error!("passthrough {} failed for {}: {}", operation, path, err);
            reply.error(Errno::EIO);
        } else {
            debug!(
                "passthrough {} failed for {}; falling back to userspace open: {}",
                operation, path, err
            );
            self.open_userspace(reply);
        }
    }
}

impl Filesystem for NexusFs {
    fn init(&mut self, _req: &Request, config: &mut KernelConfig) -> io::Result<()> {
        let Some(ref passthrough) = self.passthrough else {
            return Ok(());
        };

        let capability_ok = match config.add_capabilities(InitFlags::FUSE_PASSTHROUGH) {
            Ok(()) => true,
            Err(unsupported) => {
                error!(
                    "kernel passthrough capability not available: {:?}",
                    unsupported
                );
                false
            }
        };
        let stack_depth_ok = match config.set_max_stack_depth(PASSTHROUGH_MAX_STACK_DEPTH) {
            Ok(_) => true,
            Err(maximum) => {
                error!(
                    "kernel passthrough stack depth negotiation failed; maximum accepted depth={}",
                    maximum
                );
                false
            }
        };

        let negotiated = capability_ok && stack_depth_ok;
        passthrough.set_negotiated(negotiated);

        if negotiated {
            debug!("kernel passthrough negotiated");
            Ok(())
        } else if passthrough.require() {
            Err(io::Error::new(
                io::ErrorKind::Other,
                "kernel passthrough negotiation failed",
            ))
        } else {
            Ok(())
        }
    }

    fn lookup(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEntry) {
        let name = name.to_string_lossy();
        debug!("lookup: parent={}, name={}", parent, name);

        let parent_path = resolve_path!(self, parent.0, reply);

        let path = Self::join_path(&parent_path, &name);
        let inode = self.inodes.lock().unwrap().get_or_create(&path);

        match self.get_attr(inode, &path) {
            Ok(attr) => reply.entry(&ATTR_TTL, &attr, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn getattr(&self, _req: &Request, ino: INodeNo, _fh: Option<FileHandle>, reply: ReplyAttr) {
        debug!("getattr: ino={}", ino);

        let path = resolve_path!(self, ino.0, reply);

        match self.get_attr(ino.0, &path) {
            Ok(attr) => reply.attr(&ATTR_TTL, &attr),
            Err(e) => reply.error(e),
        }
    }

    fn readdir(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        mut reply: ReplyDirectory,
    ) {
        debug!("readdir: ino={}, offset={}", ino, offset);

        let path = resolve_path!(self, ino.0, reply);

        // Check directory cache - use Option to distinguish cache miss from empty directory
        let cached_entries: Option<Vec<FileEntry>> = {
            let mut cache = self.dir_cache.lock().unwrap();
            if let Some((entries, cached_at)) = cache.get(&ino.0) {
                if cached_at.elapsed().unwrap_or(Duration::MAX) < ATTR_TTL {
                    Some(entries.clone()) // Cache hit (may be empty dir)
                } else {
                    cache.pop(&ino.0);
                    None // Cache expired
                }
            } else {
                None // Cache miss
            }
        };

        let entries = match cached_entries {
            Some(entries) => entries, // Cache hit - use cached (even if empty)
            None => {
                // Cache miss - fetch from server
                match self.client.list(&path) {
                    Ok(entries) => {
                        // Cache the result
                        let mut cache = self.dir_cache.lock().unwrap();
                        cache.put(ino.0, (entries.clone(), SystemTime::now()));
                        entries
                    }
                    Err(e) => {
                        if !e.is_not_found() && !e.is_permission_denied() {
                            error!("readdir error for {}: {}", path, e);
                        }
                        reply.error(errno_for(&e));
                        return;
                    }
                }
            }
        };

        // Build entries with . and ..
        let mut all_entries: Vec<(u64, FileType, String)> = vec![
            (ino.0, FileType::Directory, ".".to_string()),
            (ino.0, FileType::Directory, "..".to_string()),
        ];

        // Phase 1: Acquire inodes lock once, resolve all child inodes, release.
        // This maintains lock ordering (inodes before attr_cache) and avoids
        // re-acquiring per entry (Issue 15A).
        let child_info: Vec<(u64, FileType, String)> = {
            let mut inodes = self.inodes.lock().unwrap();
            entries
                .iter()
                .map(|entry| {
                    let child_path = Self::join_path(&path, &entry.name);
                    let child_inode = inodes.get_or_create(&child_path);
                    let kind = if entry.entry_type == "directory" {
                        FileType::Directory
                    } else {
                        FileType::RegularFile
                    };
                    (child_inode, kind, entry.name.clone())
                })
                .collect()
        }; // inodes lock released here

        // Build attrs outside any lock (pure computation)
        let attrs: Vec<(u64, FileAttr)> = entries
            .iter()
            .zip(child_info.iter())
            .map(|(entry, (child_inode, _, _))| {
                let entry_type = if entry.entry_type == "directory" {
                    "directory"
                } else {
                    "file"
                };
                let attr = self.make_attr(
                    *child_inode,
                    entry_type,
                    entry.size,
                    entry.created_at.as_ref(),
                    entry.updated_at.as_ref(),
                );
                (*child_inode, attr)
            })
            .collect();

        // Phase 2: Acquire attr_cache lock once, populate all entries, release.
        {
            let now = SystemTime::now();
            let mut cache = self.attr_cache.lock().unwrap();
            for (child_inode, attr) in &attrs {
                cache.put(*child_inode, (*attr, now));
            }
        } // attr_cache lock released here

        for (child_inode, kind, name) in child_info {
            all_entries.push((child_inode, kind, name));
        }

        // Return entries starting from offset
        for (i, (inode, kind, name)) in all_entries.iter().enumerate().skip(offset as usize) {
            if reply.add(INodeNo(*inode), (i + 1) as u64, *kind, name) {
                break;
            }
        }

        reply.ok();
    }

    fn read(
        &self,
        _req: &Request,
        ino: INodeNo,
        fh: FileHandle,
        offset: u64,
        size: u32,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        reply: ReplyData,
    ) {
        debug!("read: ino={}, offset={}, size={}", ino, offset, size);

        let path = resolve_path!(self, ino.0, reply);

        let started_at = std::time::Instant::now();

        // #4055 R10: when a foyer cache is present, fail closed on stat
        // errors instead of substituting gen=0 and probing the cache.
        // A cached gen=0 entry could otherwise be served past current
        // authorization (403/404). With no cache the gen value never
        // gates a cache hit, so we keep the lightweight unwrap_or path.
        let gen = if self.file_cache.is_some() {
            match self.client.stat(&path) {
                Ok(meta) => meta.gen,
                Err(e) => {
                    metrics::record_read("error", 0, started_at.elapsed());
                    if !e.is_not_found() && !e.is_permission_denied() {
                        error!("read stat error for {}: {}", path, e);
                    }
                    reply.error(errno_for(&e));
                    return;
                }
            }
        } else {
            self.client.stat(&path).map(|m| m.gen).unwrap_or(0)
        };

        if fh.0 != 0 {
            let mut open_cache = self.open_file_cache.lock().unwrap();
            if let Some(entry) = open_cache.get(&fh.0) {
                if entry.path == path {
                    let slice_len = Self::slice_len(&entry.content, offset, size);
                    metrics::record_read("cache", slice_len, started_at.elapsed());
                    Self::reply_data_slice(&entry.content, offset, size, reply);
                    return;
                }
            }
        }

        // Read using foyer cache with ETag support
        let read_result = match self.read_cached(&path, gen) {
            Ok(result) => result,
            Err(e) => {
                metrics::record_read("error", 0, started_at.elapsed());
                // Recover the typed NexusClientError if present so the
                // full to_errno() table applies (auth, conflict, etc.);
                // otherwise default to EIO. (#4056 R4)
                let errno = e
                    .downcast_ref::<crate::error::NexusClientError>()
                    .map(errno_for)
                    .unwrap_or(Errno::EIO);
                let is_noisy = e
                    .downcast_ref::<crate::error::NexusClientError>()
                    .map(|ne| !ne.is_not_found() && !ne.is_permission_denied())
                    .unwrap_or(true);
                if is_noisy {
                    error!("read error for {}: {}", path, e);
                }
                reply.error(errno);
                return;
            }
        };

        let CachedReadResult {
            content,
            etag: _etag,
            tier,
        } = read_result;

        if fh.0 != 0 && content.len() <= MAX_OPEN_FILE_CACHE_BYTES {
            let mut open_cache = self.open_file_cache.lock().unwrap();
            open_cache.put(fh.0, path, content);
            if let Some(entry) = open_cache.get(&fh.0) {
                let slice_len = Self::slice_len(&entry.content, offset, size);
                metrics::record_read(tier, slice_len, started_at.elapsed());
                Self::reply_data_slice(&entry.content, offset, size, reply);
                return;
            }
            metrics::record_read("error", 0, started_at.elapsed());
            reply.error(Errno::EIO);
            return;
        }

        let slice_len = Self::slice_len(&content, offset, size);
        metrics::record_read(tier, slice_len, started_at.elapsed());
        Self::reply_data_slice(&content, offset, size, reply);
    }

    fn write(
        &self,
        _req: &Request,
        ino: INodeNo,
        fh: FileHandle,
        offset: u64,
        data: &[u8],
        _write_flags: WriteFlags,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        reply: ReplyWrite,
    ) {
        debug!("write: ino={}, offset={}, size={}", ino, offset, data.len());

        let path = resolve_path!(self, ino.0, reply);

        // For simplicity, we only support full file writes (offset 0).
        // Partial writes are implemented as read-modify-write.
        //
        // #4056 R8 — Pre-existing lost-update window, not addressed here.
        //   `git blame` traces this RMW pattern to the initial commit
        //   (fd152335e, 2025-12-19); the server's `write` RPC has never
        //   accepted an `if_match` / generation guard. Closing the
        //   window requires a server-protocol change (conditional
        //   writes that map server-side conflicts to RPC -32006 —
        //   which we already map to `Conflict`/`EAGAIN` on this side).
        //   That is a separate, server-tier scope item. Adding it
        //   under the banner of "migrate to async hyper" would be
        //   well outside #4056's stated charter.
        //
        // #4056 R6 — In-scope mitigation that *did* land:
        //   The source read used to go through `read_cached`, which
        //   on a transient revalidation failure could serve stale
        //   FileCache bytes that then got blind-written back. The
        //   `rmw_read` helper goes straight to `client.read` so the
        //   source is always the backend's current view (no cache
        //   staleness in the read). `tests::rmw_read_bypasses_file_cache_even_when_warm`
        //   locks that property in.
        if offset != 0 {
            let existing = match self.rmw_read(&path) {
                Ok(bytes) => bytes,
                Err(e) => {
                    if !e.is_not_found() && !e.is_permission_denied() {
                        error!("partial write: read failed for {}: {}", path, e);
                    }
                    reply.error(errno_for(&e));
                    return;
                }
            };

            let mut new_content = existing;
            let offset = offset as usize;

            // Extend if needed
            if offset > new_content.len() {
                new_content.resize(offset, 0);
            }

            // Overwrite or append
            if offset + data.len() > new_content.len() {
                new_content.resize(offset + data.len(), 0);
            }
            new_content[offset..offset + data.len()].copy_from_slice(data);

            match self.client.write(&path, &new_content) {
                Ok(_) => {
                    metrics::record_write_backend_rpc();
                    if fh.0 != 0 {
                        self.open_file_cache.lock().unwrap().remove(fh.0);
                    }
                    self.invalidate_path(&path);
                    reply.written(data.len() as u32);
                }
                Err(e) => {
                    if !e.is_not_found() && !e.is_permission_denied() {
                        error!("write error for {}: {}", path, e);
                    }
                    reply.error(errno_for(&e));
                }
            }
        } else {
            match self.client.write(&path, data) {
                Ok(_) => {
                    metrics::record_write_backend_rpc();
                    if fh.0 != 0 {
                        self.open_file_cache.lock().unwrap().remove(fh.0);
                    }
                    self.invalidate_path(&path);
                    reply.written(data.len() as u32);
                }
                Err(e) => {
                    if !e.is_not_found() && !e.is_permission_denied() {
                        error!("write error for {}: {}", path, e);
                    }
                    reply.error(errno_for(&e));
                }
            }
        }
    }

    fn create(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        _mode: u32,
        _umask: u32,
        _flags: i32,
        reply: fuser::ReplyCreate,
    ) {
        let name = name.to_string_lossy();
        debug!("create: parent={}, name={}", parent, name);

        let parent_path = resolve_path!(self, parent.0, reply);

        let path = Self::join_path(&parent_path, &name);

        // Create empty file
        match self.client.write(&path, &[]) {
            Ok(_) => {
                metrics::record_write_backend_rpc();
                let inode = self.inodes.lock().unwrap().get_or_create(&path);
                self.invalidate_path(&path);

                let attr = self.make_attr(inode, "file", 0, None, None);
                reply.created(
                    &ATTR_TTL,
                    &attr,
                    Generation(0),
                    FileHandle(0),
                    FopenFlags::empty(),
                );
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("create error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn mkdir(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        _mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        let name = name.to_string_lossy();
        debug!("mkdir: parent={}, name={}", parent, name);

        let parent_path = resolve_path!(self, parent.0, reply);

        let path = Self::join_path(&parent_path, &name);

        match self.client.mkdir(&path) {
            Ok(_) => {
                let inode = self.inodes.lock().unwrap().get_or_create(&path);
                self.invalidate_path(&path);

                let attr = self.make_attr(inode, "directory", 0, None, None);
                reply.entry(&ATTR_TTL, &attr, Generation(0));
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("mkdir error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn unlink(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: fuser::ReplyEmpty) {
        let name = name.to_string_lossy();
        debug!("unlink: parent={}, name={}", parent, name);

        let parent_path = resolve_path!(self, parent.0, reply);

        let path = Self::join_path(&parent_path, &name);

        match self.client.delete(&path) {
            Ok(_) => {
                self.invalidate_path(&path);
                reply.ok();
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("unlink error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn rmdir(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: fuser::ReplyEmpty) {
        let name = name.to_string_lossy();
        debug!("rmdir: parent={}, name={}", parent, name);

        let parent_path = resolve_path!(self, parent.0, reply);

        let path = Self::join_path(&parent_path, &name);

        // Check if directory is empty
        match self.client.list(&path) {
            Ok(entries) if !entries.is_empty() => {
                reply.error(Errno::ENOTEMPTY);
                return;
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("rmdir list error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
                return;
            }
            _ => {}
        }

        match self.client.delete(&path) {
            Ok(_) => {
                self.invalidate_path(&path);
                reply.ok();
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("rmdir error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn rename(
        &self,
        _req: &Request,
        parent: INodeNo,
        name: &OsStr,
        newparent: INodeNo,
        newname: &OsStr,
        flags: RenameFlags,
        reply: fuser::ReplyEmpty,
    ) {
        let name = name.to_string_lossy();
        let newname = newname.to_string_lossy();
        debug!(
            "rename: parent={}, name={}, newparent={}, newname={}, flags={}",
            parent, name, newparent, newname, flags
        );

        let parent_path = resolve_path!(self, parent.0, reply);
        let new_parent_path = resolve_path!(self, newparent.0, reply);

        let old_path = Self::join_path(&parent_path, &name);
        let new_path = Self::join_path(&new_parent_path, &newname);

        // Issue 16A: Let server handle POSIX replace semantics instead of
        // making client-side exists() + delete() calls (2-3 extra HTTP RPCs).
        // The server's rename() implements atomic replace when destination exists.
        // Only log if RENAME_NOREPLACE (flag bit 0) is set — the server should
        // handle this, but we note it for debugging.
        if flags.bits() & 1 != 0 {
            debug!(
                "rename: RENAME_NOREPLACE flag set for {} -> {}",
                old_path, new_path
            );
        }

        match self.client.rename(&old_path, &new_path) {
            Ok(_) => {
                // Update inode mappings atomically (single lock)
                self.inodes
                    .lock()
                    .unwrap()
                    .rename_path(&old_path, &new_path);
                self.invalidate_path(&old_path);
                self.invalidate_path(&new_path);
                reply.ok();
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("rename error: {} -> {}: {}", old_path, new_path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn setattr(
        &self,
        _req: &Request,
        ino: INodeNo,
        _mode: Option<u32>,
        _uid: Option<u32>,
        _gid: Option<u32>,
        size: Option<u64>,
        _atime: Option<fuser::TimeOrNow>,
        _mtime: Option<fuser::TimeOrNow>,
        _ctime: Option<SystemTime>,
        fh: Option<FileHandle>,
        _crtime: Option<SystemTime>,
        _chgtime: Option<SystemTime>,
        _bkuptime: Option<SystemTime>,
        _flags: Option<BsdFileFlags>,
        reply: ReplyAttr,
    ) {
        debug!("setattr: ino={}, size={:?}", ino, size);

        let path = resolve_path!(self, ino.0, reply);

        // Handle truncate
        if let Some(new_size) = size {
            if new_size == 0 {
                // Truncate to empty
                match self.client.write(&path, &[]) {
                    Ok(_) => {
                        metrics::record_write_backend_rpc();
                        if let Some(fh) = fh {
                            self.open_file_cache.lock().unwrap().remove(fh.0);
                        }
                        self.invalidate_path(&path);
                    }
                    Err(e) => {
                        if !e.is_not_found() && !e.is_permission_denied() {
                            error!("truncate error for {}: {}", path, e);
                        }
                        reply.error(errno_for(&e));
                        return;
                    }
                }
            } else {
                // Truncate to specific size - read and rewrite.
                //
                // #4056 R6 mitigation: bypass the read-through cache
                // for the source read (rmw_read goes straight to the
                // backend) so a stale-cache revalidation cannot feed
                // a blind full-file rewrite.
                //
                // #4056 R8: same pre-existing lost-update window as
                // the partial-write path above — between rmw_read and
                // the follow-up `client.write` a concurrent writer can
                // commit and be silently overwritten. Closing it
                // requires a conditional-write RPC on the server.
                // Out of scope for this PR (transport migration).
                match self.rmw_read(&path) {
                    Ok(bytes) => {
                        let mut data = bytes;
                        data.resize(new_size as usize, 0);
                        match self.client.write(&path, &data) {
                            Ok(_) => {
                                metrics::record_write_backend_rpc();
                                if let Some(fh) = fh {
                                    self.open_file_cache.lock().unwrap().remove(fh.0);
                                }
                                self.invalidate_path(&path);
                            }
                            Err(e) => {
                                if !e.is_not_found() && !e.is_permission_denied() {
                                    error!("truncate write error for {}: {}", path, e);
                                }
                                reply.error(errno_for(&e));
                                return;
                            }
                        }
                    }
                    Err(e) => {
                        // Direct backend read (no cache) — typed error
                        // already in scope, just map it. (#4056 R6)
                        if !e.is_not_found() && !e.is_permission_denied() {
                            error!("truncate read error for {}: {}", path, e);
                        }
                        reply.error(errno_for(&e));
                        return;
                    }
                }
            }
        }

        // Return updated attributes
        match self.get_attr(ino.0, &path) {
            Ok(attr) => reply.attr(&ATTR_TTL, &attr),
            Err(e) => reply.error(e),
        }
    }

    fn getxattr(&self, _req: &Request, ino: INodeNo, name: &OsStr, size: u32, reply: ReplyXattr) {
        debug!("getxattr: ino={}, name={:?}, size={}", ino, name, size);

        let path = resolve_path!(self, ino.0, reply);
        if name != OsStr::new(XATTR_GEN) {
            reply.error(Errno::NO_XATTR);
            return;
        }

        match self.stat_gen(&path) {
            Ok(gen) => {
                let value = Self::generation_xattr_value(gen);
                Self::reply_xattr(reply, &value, size);
            }
            Err(errno) => reply.error(errno),
        }
    }

    fn listxattr(&self, _req: &Request, ino: INodeNo, size: u32, reply: ReplyXattr) {
        debug!("listxattr: ino={}, size={}", ino, size);

        let path = resolve_path!(self, ino.0, reply);
        match self.client.stat(&path) {
            Ok(_) => {
                let names = Self::generation_xattr_name_list();
                Self::reply_xattr(reply, &names, size);
            }
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("listxattr stat error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn setxattr(
        &self,
        _req: &Request,
        ino: INodeNo,
        name: &OsStr,
        _value: &[u8],
        _flags: i32,
        _position: u32,
        reply: fuser::ReplyEmpty,
    ) {
        debug!("setxattr: ino={}, name={:?}", ino, name);

        let path = resolve_path!(self, ino.0, reply);
        match self.client.stat(&path) {
            Ok(_) if name == OsStr::new(XATTR_GEN) => reply.error(Errno::EROFS),
            Ok(_) => reply.error(Errno::NO_XATTR),
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("setxattr stat error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn open(&self, _req: &Request, ino: INodeNo, flags: OpenFlags, reply: fuser::ReplyOpen) {
        debug!("open: ino={}", ino);

        let path = resolve_path!(self, ino.0, reply);

        if self.passthrough.is_none() {
            match self.check_is_directory(&path) {
                Ok(true) => reply.error(Errno::EISDIR),
                Ok(false) => self.open_userspace(reply),
                Err(e) => {
                    if !e.is_not_found() && !e.is_permission_denied() {
                        error!("open stat error for {}: {}", path, e);
                    }
                    reply.error(errno_for(&e));
                }
            }
            return;
        }

        let metadata = match self.client.stat(&path) {
            Ok(metadata) => metadata,
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("open stat error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
                return;
            }
        };

        if metadata.is_directory {
            reply.error(Errno::EISDIR);
            return;
        }

        if let Some(ref passthrough) = self.passthrough {
            let access = OpenAccess::from_open_flags(flags);
            match passthrough.decide(&path, &metadata, access) {
                PassthroughDecision::Allow => {
                    let fh = match self.allocate_file_handle() {
                        Ok(fh) => fh,
                        Err(err) => {
                            self.reply_passthrough_open_failure(
                                &path,
                                "file handle allocation",
                                err,
                                reply,
                            );
                            return;
                        }
                    };
                    let backing = match passthrough.materialize(&path, &self.client, &metadata) {
                        Ok(backing) => backing,
                        Err(err) => {
                            self.reply_passthrough_open_failure(
                                &path,
                                "materialization",
                                err,
                                reply,
                            );
                            return;
                        }
                    };
                    let backing_id = match reply.open_backing(backing.file()) {
                        Ok(backing_id) => Arc::new(backing_id),
                        Err(err) => {
                            self.reply_passthrough_open_failure(
                                &path,
                                "backing registration",
                                err,
                                reply,
                            );
                            return;
                        }
                    };
                    let active = ActivePassthrough {
                        backing,
                        backing_id: Arc::clone(&backing_id),
                    };
                    if let Err(err) = passthrough.insert_active(fh, active) {
                        self.reply_passthrough_open_failure(
                            &path,
                            &format!("active handle insert for fh={}", fh),
                            err,
                            reply,
                        );
                        return;
                    }
                    reply.opened_passthrough(
                        FileHandle(fh),
                        FopenFlags::empty(),
                        backing_id.as_ref(),
                    );
                    return;
                }
                PassthroughDecision::Deny(reason) => {
                    debug!("passthrough denied for {}: {:?}", path, reason);
                }
            }
        }

        self.open_userspace(reply);
    }

    fn opendir(&self, _req: &Request, ino: INodeNo, _flags: OpenFlags, reply: fuser::ReplyOpen) {
        debug!("opendir: ino={}", ino);

        let path = resolve_path!(self, ino.0, reply);

        // Root always exists and is a directory
        if path == "/" {
            reply.opened(FileHandle(0), FopenFlags::empty());
            return;
        }

        match self.check_is_directory(&path) {
            Ok(true) => reply.opened(FileHandle(0), FopenFlags::empty()),
            Ok(false) => reply.error(Errno::ENOTDIR),
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("opendir stat error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }

    fn flush(
        &self,
        _req: &Request,
        _ino: INodeNo,
        _fh: FileHandle,
        _lock_owner: LockOwner,
        reply: fuser::ReplyEmpty,
    ) {
        reply.ok();
    }

    fn release(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        _flush: bool,
        reply: fuser::ReplyEmpty,
    ) {
        if fh.0 != 0 {
            self.open_file_cache.lock().unwrap().remove(fh.0);
        }
        if let Some(ref passthrough) = self.passthrough {
            if let Err(err) = passthrough.remove_active(fh.0) {
                error!(
                    "passthrough active handle cleanup failed for fh={}: {}",
                    fh.0, err
                );
            }
        }
        reply.ok();
    }

    fn releasedir(
        &self,
        _req: &Request,
        _ino: INodeNo,
        _fh: FileHandle,
        _flags: OpenFlags,
        reply: fuser::ReplyEmpty,
    ) {
        reply.ok();
    }

    fn access(&self, _req: &Request, ino: INodeNo, _mask: AccessFlags, reply: fuser::ReplyEmpty) {
        debug!("access: ino={}", ino);

        let path = resolve_path!(self, ino.0, reply);

        // #4056 R5: surface auth failures as EACCES/EPERM instead of
        // collapsing them to ENOENT through best-effort `exists`.
        match self.client.exists_result(&path) {
            Ok(true) => reply.ok(),
            Ok(false) => reply.error(Errno::ENOENT),
            Err(e) => {
                if !e.is_not_found() && !e.is_permission_denied() {
                    error!("access exists error for {}: {}", path, e);
                }
                reply.error(errno_for(&e));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::{CacheConfig, MAX_FILE_SIZE};
    use mockito::{Matcher, Server};

    fn metric_value(rendered: &str, metric: &str) -> Option<u64> {
        rendered.lines().find_map(|line| {
            line.strip_prefix(metric)
                .and_then(|value| value.trim().parse::<u64>().ok())
        })
    }

    fn test_file_cache(label: &str) -> Arc<FileCache> {
        let dir = tempfile::tempdir().unwrap().keep();
        let config = CacheConfig::new(
            dir.join(label),
            4 * 1024 * 1024,
            64 * 1024 * 1024,
            MAX_FILE_SIZE,
        )
        .unwrap();
        Arc::new(
            FileCache::new_with_config(&format!("http://{label}.test"), "test", config).unwrap(),
        )
    }

    #[test]
    fn nexus_fs_can_be_constructed_with_passthrough_manager() {
        let fs = nexus_fs_with_passthrough_required(false);

        assert!(fs.passthrough_enabled_for_tests());
    }

    #[test]
    fn optional_passthrough_runtime_failure_is_not_fatal() {
        let fs = nexus_fs_with_passthrough_required(false);

        assert!(!fs.passthrough_runtime_failure_is_fatal_for_tests());
    }

    #[test]
    fn required_passthrough_runtime_failure_is_fatal() {
        let fs = nexus_fs_with_passthrough_required(true);

        assert!(fs.passthrough_runtime_failure_is_fatal_for_tests());
    }

    #[test]
    fn passthrough_requests_maximum_supported_stack_depth() {
        assert_eq!(NexusFs::passthrough_max_stack_depth_for_tests(), 2);
    }

    fn nexus_fs_with_passthrough_required(require: bool) -> NexusFs {
        use crate::passthrough::{PassthroughConfig, PassthroughManager};

        let mut server = Server::new();
        let url = server.url();
        let client = NexusClient::new(&url, "test-key", None).expect("client");
        let manager = PassthroughManager::new(
            url,
            PassthroughConfig {
                enabled: true,
                allow_patterns: vec!["/data/**".to_string()],
                deny_patterns: vec![],
                threshold_bytes: 128 * 1024,
                require,
                backing_dir: None,
            },
        )
        .expect("manager");

        NexusFs::new(client, None, Some(Arc::new(manager)))
    }

    #[test]
    fn test_get_or_create_idempotent() {
        let mut table = InodeTable::new();
        let inode1 = table.get_or_create("/foo/bar");
        let inode2 = table.get_or_create("/foo/bar");
        assert_eq!(inode1, inode2, "Same path must return same inode");
    }

    #[test]
    fn test_get_or_create_unique() {
        let mut table = InodeTable::new();
        let a = table.get_or_create("/a");
        let b = table.get_or_create("/b");
        assert_ne!(a, b, "Different paths must get different inodes");
    }

    #[test]
    fn test_get_path_roundtrip() {
        let mut table = InodeTable::new();
        let inode = table.get_or_create("/foo");
        let path = table.get_path(inode);
        assert_eq!(path.as_deref(), Some("/foo"));
    }

    #[test]
    fn test_root_inode_accessible() {
        let mut table = InodeTable::new();
        assert_eq!(table.get_path(FUSE_ROOT_ID), Some("/".to_string()));
        assert_eq!(table.get_inode("/"), Some(FUSE_ROOT_ID));
        assert_eq!(table.peek_inode("/"), Some(FUSE_ROOT_ID));
    }

    #[test]
    fn read_cached_returns_stale_cache_when_revalidation_errors() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = Server::new();
        let read_mock = server
            .mock("POST", "/api/nfs/read")
            .match_header("if-none-match", Matcher::Exact("\"etag-1\"".to_string()))
            .with_status(500)
            .with_body("backend unavailable")
            .create();

        let cache = test_file_cache("fs-stale-fallback");
        cache.put("/stale.txt", b"stale-data", Some("etag-1"), 0);
        cache.backdate_for_test("/stale.txt", 3601);

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let fs = NexusFs::new(client, Some(cache), None);

        let result = fs.read_cached("/stale.txt", 0).unwrap();

        assert_eq!(result.content, b"stale-data");
        assert_eq!(result.etag, Some("etag-1".to_string()));
        assert_eq!(result.tier, "cache");
        read_mock.assert();

        let rendered = crate::metrics::render();
        assert!(rendered.contains("nexus_cache_etag_revalidate_total{result=\"fallback\"} 1"));
        assert!(rendered.contains("nexus_etag_check_total{result=\"fallback\"} 1"));
        assert!(!rendered.contains("nexus_cache_etag_revalidate_total{result=\"error\"}"));
        assert!(!rendered.contains("nexus_etag_check_total{result=\"error\"}"));
    }

    #[test]
    fn read_cached_not_modified_does_not_record_extra_cache_hit() {
        let _guard = crate::metrics::test_guard();
        crate::metrics::reset_for_tests();

        let mut server = Server::new();
        let read_mock = server
            .mock("POST", "/api/nfs/read")
            .match_header("if-none-match", Matcher::Exact("\"etag-1\"".to_string()))
            .with_status(304)
            .create();

        let cache = test_file_cache("fs-not-modified");
        cache.put("/not-modified.txt", b"cached-data", Some("etag-1"), 0);
        cache.backdate_for_test("/not-modified.txt", 3601);

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let fs = NexusFs::new(client, Some(cache), None);

        let result = fs.read_cached("/not-modified.txt", 0).unwrap();

        assert_eq!(result.content, b"cached-data");
        assert_eq!(result.etag, Some("etag-1".to_string()));
        assert_eq!(result.tier, "cache");
        read_mock.assert();

        let rendered = crate::metrics::render();
        assert_eq!(
            metric_value(
                &rendered,
                "nexus_cache_requests_total{tier=\"dram\",result=\"stale\"} "
            ),
            Some(1)
        );
        assert_eq!(
            metric_value(
                &rendered,
                "nexus_cache_requests_total{tier=\"dram\",result=\"hit\"} "
            ),
            None
        );
        assert!(rendered.contains("nexus_cache_etag_revalidate_total{result=\"304\"} 1"));
        assert!(rendered.contains("nexus_etag_check_total{result=\"304\"} 1"));
    }

    /// Root inode must survive even when MAX_INODE_ENTRIES are inserted
    /// (Issue #3029 / Bug 4 regression test).
    #[test]
    fn test_root_inode_survives_eviction() {
        // Use a small capacity to make the test fast
        let cap = NonZeroUsize::new(100).unwrap();
        let mut table = InodeTable {
            inode_to_path: LruCache::new(cap),
            path_to_inode: LruCache::new(cap),
            next_inode: FUSE_ROOT_ID + 1,
        };
        // Manually insert root
        table.inode_to_path.put(FUSE_ROOT_ID, "/".to_string());
        table.path_to_inode.put("/".to_string(), FUSE_ROOT_ID);

        // Insert more entries than capacity — should evict old entries but not root
        for i in 0..200 {
            table.get_or_create(&format!("/deep/path/entry-{}", i));
        }

        // Root must still be accessible
        assert_eq!(
            table.get_path(FUSE_ROOT_ID),
            Some("/".to_string()),
            "Root inode was evicted from inode_to_path"
        );
        assert_eq!(
            table.peek_inode("/"),
            Some(FUSE_ROOT_ID),
            "Root inode was evicted from path_to_inode"
        );
    }

    /// After eviction, both maps must agree — no stale reverse mappings
    /// (Issue #3029 / Bug 4 regression test).
    #[test]
    fn test_eviction_sync() {
        let cap = NonZeroUsize::new(10).unwrap();
        let mut table = InodeTable {
            inode_to_path: LruCache::new(cap),
            path_to_inode: LruCache::new(cap),
            next_inode: FUSE_ROOT_ID + 1,
        };
        table.inode_to_path.put(FUSE_ROOT_ID, "/".to_string());
        table.path_to_inode.put("/".to_string(), FUSE_ROOT_ID);

        // Record first batch of inodes
        let mut first_inodes = Vec::new();
        for i in 0..9 {
            let inode = table.get_or_create(&format!("/file-{}", i));
            first_inodes.push((inode, format!("/file-{}", i)));
        }

        // Now insert enough to trigger evictions of the first batch
        for i in 0..20 {
            table.get_or_create(&format!("/new-{}", i));
        }

        // For any inode in inode_to_path, path_to_inode must agree (and vice versa)
        // Check: if get_path returns a path, peek_inode on that path must return the same inode
        for inode_val in 1..table.next_inode {
            if let Some(path) = table.inode_to_path.peek(&inode_val).cloned() {
                let reverse = table.path_to_inode.peek(&path).copied();
                assert_eq!(
                    reverse,
                    Some(inode_val),
                    "Desync: inode {} -> path {:?} but path -> inode {:?}",
                    inode_val,
                    path,
                    reverse,
                );
            }
        }
    }

    #[test]
    fn test_rename_path() {
        let mut table = InodeTable::new();
        let inode = table.get_or_create("/old/name");
        table.rename_path("/old/name", "/new/name");

        // Old path should not resolve
        assert_eq!(table.peek_inode("/old/name"), None);
        // New path should resolve to same inode
        assert_eq!(table.peek_inode("/new/name"), Some(inode));
        // Inode should map to new path
        assert_eq!(table.get_path(inode), Some("/new/name".to_string()));
    }

    #[test]
    fn test_remove_path() {
        let mut table = InodeTable::new();
        let inode = table.get_or_create("/to-delete");
        let removed = table.remove_path("/to-delete");
        assert_eq!(removed, Some(inode));
        assert_eq!(table.peek_inode("/to-delete"), None);
        assert_eq!(table.get_path(inode), None);
    }

    #[test]
    fn test_generation_xattr_name_list_is_nul_terminated() {
        assert_eq!(
            NexusFs::generation_xattr_name_list(),
            b"user.nexus.gen\0".to_vec()
        );
    }

    #[test]
    fn test_generation_xattr_value_is_decimal_bytes() {
        assert_eq!(NexusFs::generation_xattr_value(42), b"42".to_vec());
    }

    #[test]
    fn test_open_file_cache_rejects_file_over_byte_budget() {
        let mut cache = OpenFileCache::new(NonZeroUsize::new(4).unwrap(), 8);
        cache.put(1, "/big.bin".to_string(), vec![0; 9]);

        assert!(cache.get(&1).is_none());
        assert_eq!(cache.total_bytes, 0);
    }

    #[test]
    fn test_open_file_cache_evicts_to_byte_budget() {
        let mut cache = OpenFileCache::new(NonZeroUsize::new(4).unwrap(), 8);
        cache.put(1, "/a.bin".to_string(), vec![0; 4]);
        cache.put(2, "/b.bin".to_string(), vec![0; 4]);
        cache.put(3, "/c.bin".to_string(), vec![0; 4]);

        assert!(cache.get(&1).is_none());
        assert!(cache.get(&2).is_some());
        assert!(cache.get(&3).is_some());
        assert_eq!(cache.total_bytes, 8);
    }

    #[test]
    fn test_open_file_cache_remove_updates_byte_count() {
        let mut cache = OpenFileCache::new(NonZeroUsize::new(4).unwrap(), 16);
        cache.put(1, "/a.bin".to_string(), vec![0; 4]);
        cache.put(2, "/b.bin".to_string(), vec![0; 6]);

        cache.remove(1);

        assert!(cache.get(&1).is_none());
        assert!(cache.get(&2).is_some());
        assert_eq!(cache.total_bytes, 6);
    }

    /// #4056 R6/R7: `rmw_read` (the source-read for partial writes
    /// and non-zero truncate) must always hit the backend, regardless
    /// of FileCache state. A regression where someone routes RMW
    /// reads back through `read_cached` / `read_with_cache` would
    /// reopen the stale-cache → blind-write clobber that R6 fixed.
    ///
    /// We prime the FileCache with a `STALE-CACHED` body, then stand
    /// up a mockito mock for `/api/nfs/read` that returns
    /// `FRESH-BACKEND` and asserts it gets hit (so we know the
    /// helper didn't short-circuit via the cache). `expect_at_least(1)`
    /// is enforced when the `Mock` is dropped — if the cache short-
    /// circuit returned and the backend mock was never called, the
    /// drop-time assertion fails the test.
    #[test]
    fn rmw_read_bypasses_file_cache_even_when_warm() {
        use base64::engine::general_purpose::STANDARD;
        use base64::Engine;

        let mut server = Server::new();
        // Backend response: encoded "FRESH-BACKEND" so the JSON-RPC
        // base64 envelope round-trips cleanly.
        let fresh = b"FRESH-BACKEND".to_vec();
        let payload = STANDARD.encode(&fresh);
        let read_body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            payload
        );
        // Require the backend read to be hit AT LEAST ONCE — proves
        // the helper did not take a cache short-circuit. Mockito's
        // `expect_at_least` is verified when the `Mock` value drops
        // at the end of the test scope.
        let _backend_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(read_body)
            .expect_at_least(1)
            .create();

        let client = NexusClient::new(&server.url(), "k", None).unwrap();
        let cache = test_file_cache("rmw-bypasses-cache");
        // Prime cache with stale, fully-warm bytes (NOT past TTL — so
        // the cache lookup would normally return Hit if anyone routed
        // through it).
        cache.put("/rmw.txt", b"STALE-CACHED", Some("stale-etag"), 0);

        let fs = NexusFs::new(client, Some(cache.clone()), None);
        let bytes = fs.rmw_read("/rmw.txt").expect("rmw_read");
        assert_eq!(
            bytes, fresh,
            "rmw_read must return fresh backend bytes, not stale FileCache content"
        );
    }
}
