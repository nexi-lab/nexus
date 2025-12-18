//! FUSE filesystem implementation for Nexus.

use crate::cache::{CacheLookup, FileCache};
use crate::client::{FileEntry, NexusClient, ReadResponse};
use fuser::{
    FileAttr, FileType, Filesystem, ReplyAttr, ReplyData, ReplyDirectory, ReplyEntry, ReplyWrite,
    Request, FUSE_ROOT_ID,
};
use libc::{ENOENT, ENOTDIR, ENOTEMPTY, EISDIR, EIO};
use log::{debug, error};
use lru::LruCache;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::num::NonZeroUsize;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// TTL for attribute caching (30s for better read performance).
const ATTR_TTL: Duration = Duration::from_secs(30);

/// Default block size.
const BLOCK_SIZE: u32 = 512;

/// Nexus FUSE filesystem.
pub struct NexusFs {
    client: Arc<NexusClient>,
    /// Inode to path mapping.
    inode_to_path: Mutex<HashMap<u64, String>>,
    /// Path to inode mapping.
    path_to_inode: Mutex<HashMap<String, u64>>,
    /// Next available inode number.
    next_inode: Mutex<u64>,
    /// Attribute cache (in-memory, short TTL).
    attr_cache: Mutex<LruCache<u64, (FileAttr, SystemTime)>>,
    /// Directory listing cache (in-memory).
    dir_cache: Mutex<LruCache<u64, (Vec<FileEntry>, SystemTime)>>,
    /// Persistent SQLite cache for file content (optional).
    file_cache: Option<FileCache>,
}

impl NexusFs {
    /// Create a new NexusFs instance.
    pub fn new(client: NexusClient, file_cache: Option<FileCache>) -> Self {
        let mut inode_to_path = HashMap::new();
        let mut path_to_inode = HashMap::new();

        // Root inode
        inode_to_path.insert(FUSE_ROOT_ID, "/".to_string());
        path_to_inode.insert("/".to_string(), FUSE_ROOT_ID);

        Self {
            client: Arc::new(client),
            inode_to_path: Mutex::new(inode_to_path),
            path_to_inode: Mutex::new(path_to_inode),
            next_inode: Mutex::new(FUSE_ROOT_ID + 1),
            attr_cache: Mutex::new(LruCache::new(NonZeroUsize::new(10000).unwrap())),
            dir_cache: Mutex::new(LruCache::new(NonZeroUsize::new(1000).unwrap())),
            file_cache,
        }
    }

    /// Get or create inode for a path.
    fn get_or_create_inode(&self, path: &str) -> u64 {
        let mut path_to_inode = self.path_to_inode.lock().unwrap();

        if let Some(&inode) = path_to_inode.get(path) {
            return inode;
        }

        let mut next_inode = self.next_inode.lock().unwrap();
        let inode = *next_inode;
        *next_inode += 1;

        path_to_inode.insert(path.to_string(), inode);
        drop(path_to_inode);

        let mut inode_to_path = self.inode_to_path.lock().unwrap();
        inode_to_path.insert(inode, path.to_string());

        inode
    }

    /// Get path for an inode.
    fn get_path(&self, inode: u64) -> Option<String> {
        let inode_to_path = self.inode_to_path.lock().unwrap();
        inode_to_path.get(&inode).cloned()
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
    fn make_attr(&self, inode: u64, entry_type: &str, size: u64, created: Option<&String>, updated: Option<&String>) -> FileAttr {
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
        let perm = if kind == FileType::Directory { 0o777 } else { 0o666 };

        FileAttr {
            ino: inode,
            size,
            blocks: (size + BLOCK_SIZE as u64 - 1) / BLOCK_SIZE as u64,
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

    /// Check if a path is a directory by listing the parent.
    fn is_directory(&self, path: &str) -> bool {
        // Root is always a directory
        if path == "/" {
            return true;
        }

        // Get parent path and filename
        let path_obj = std::path::Path::new(path);
        let parent = path_obj.parent().map(|p| p.to_string_lossy().to_string()).unwrap_or("/".to_string());
        let parent = if parent.is_empty() { "/".to_string() } else { parent };
        let name = path_obj.file_name().map(|n| n.to_string_lossy().to_string());

        let name = match name {
            Some(n) => n,
            None => return false,
        };

        // List parent directory and find this entry
        match self.client.list(&parent) {
            Ok(entries) => {
                entries.iter().any(|e| e.name == name && e.entry_type == "directory")
            }
            Err(_) => false,
        }
    }

    /// Get attributes for a path, using cache.
    fn get_attr(&self, inode: u64, path: &str) -> Result<FileAttr, i32> {
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

        // Check parent listing to determine if file or directory
        // This is the source of truth since directories can also be "read" with 0 bytes
        if self.is_directory(path) {
            let attr = self.make_attr(inode, "directory", 0, None, None);
            let mut cache = self.attr_cache.lock().unwrap();
            cache.put(inode, (attr, SystemTime::now()));
            return Ok(attr);
        }

        // Not a directory - try to read as a file
        match self.read_cached(path) {
            Ok((data, _etag)) => {
                let attr = self.make_attr(inode, "file", data.len() as u64, None, None);
                let mut cache = self.attr_cache.lock().unwrap();
                cache.put(inode, (attr, SystemTime::now()));
                Ok(attr)
            }
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("not found") {
                    Err(ENOENT)
                } else {
                    error!("get_attr error for {}: {}", path, e);
                    Err(EIO)
                }
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

    /// Invalidate caches for a path.
    fn invalidate_path(&self, path: &str) {
        if let Some(inode) = self.path_to_inode.lock().unwrap().get(path).copied() {
            self.attr_cache.lock().unwrap().pop(&inode);
            self.dir_cache.lock().unwrap().pop(&inode);
        }

        // Also invalidate parent directory cache
        if let Some(parent) = std::path::Path::new(path).parent() {
            let parent_path = parent.to_string_lossy().to_string();
            let parent_path = if parent_path.is_empty() { "/".to_string() } else { parent_path };
            if let Some(inode) = self.path_to_inode.lock().unwrap().get(&parent_path).copied() {
                self.dir_cache.lock().unwrap().pop(&inode);
            }
        }

        // Invalidate persistent cache
        if let Some(ref cache) = self.file_cache {
            cache.invalidate(path);
        }
    }

    /// Read file with SQLite cache and ETag support.
    ///
    /// Cache flow:
    /// 1. Check SQLite cache
    /// 2. If hit and fresh -> return cached content
    /// 3. If hit but stale with etag -> send If-None-Match request
    /// 4. If server returns 304 -> touch cache, return cached content
    /// 5. If server returns 200 -> update cache, return new content
    /// 6. If miss -> fetch from server, store in cache
    fn read_cached(&self, path: &str) -> anyhow::Result<(Vec<u8>, Option<String>)> {
        // Check persistent cache first
        if let Some(ref cache) = self.file_cache {
            match cache.get(path) {
                CacheLookup::Hit(entry) => {
                    debug!("SQLite cache hit for {}", path);
                    return Ok((entry.content, entry.etag));
                }
                CacheLookup::NeedsRevalidation { etag } => {
                    // Send conditional request
                    debug!("Revalidating cache for {} with etag {}", path, etag);
                    match self.client.read_with_etag(path, Some(&etag)) {
                        Ok(ReadResponse::NotModified) => {
                            // Touch cache to refresh timestamp
                            cache.touch(path);
                            // Return cached content - must re-fetch from cache
                            // Use a direct query since touch() updated the timestamp
                            match cache.get(path) {
                                CacheLookup::Hit(entry) => {
                                    return Ok((entry.content, entry.etag));
                                }
                                _ => {
                                    // Cache inconsistency - should not happen
                                    // Fall through to fetch fresh content
                                    error!("Cache inconsistency after 304 for {}", path);
                                }
                            }
                        }
                        Ok(ReadResponse::Content { content, etag }) => {
                            // Update cache with new content
                            cache.put(path, &content, etag.as_deref());
                            return Ok((content, etag));
                        }
                        Err(e) => {
                            // On error, try to use stale cache as fallback
                            debug!("Revalidation failed for {}: {}, using stale cache", path, e);
                            if let CacheLookup::Hit(entry) = cache.get(path) {
                                return Ok((entry.content, entry.etag));
                            }
                            return Err(e);
                        }
                    }
                }
                CacheLookup::Miss => {
                    // Fall through to fetch
                }
            }
        }

        // Fetch from server
        match self.client.read_with_etag(path, None) {
            Ok(ReadResponse::Content { content, etag }) => {
                // Store in cache
                if let Some(ref cache) = self.file_cache {
                    cache.put(path, &content, etag.as_deref());
                }
                Ok((content, etag))
            }
            Ok(ReadResponse::NotModified) => {
                // Shouldn't happen without etag, but handle gracefully
                Err(anyhow::anyhow!("Unexpected 304 response"))
            }
            Err(e) => Err(e),
        }
    }
}

impl Filesystem for NexusFs {
    fn lookup(&mut self, _req: &Request, parent: u64, name: &OsStr, reply: ReplyEntry) {
        let name = name.to_string_lossy();
        debug!("lookup: parent={}, name={}", parent, name);

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let path = Self::join_path(&parent_path, &name);
        let inode = self.get_or_create_inode(&path);

        match self.get_attr(inode, &path) {
            Ok(attr) => reply.entry(&ATTR_TTL, &attr, 0),
            Err(e) => reply.error(e),
        }
    }

    fn getattr(&mut self, _req: &Request, ino: u64, reply: ReplyAttr) {
        debug!("getattr: ino={}", ino);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        match self.get_attr(ino, &path) {
            Ok(attr) => reply.attr(&ATTR_TTL, &attr),
            Err(e) => reply.error(e),
        }
    }

    fn readdir(
        &mut self,
        _req: &Request,
        ino: u64,
        _fh: u64,
        offset: i64,
        mut reply: ReplyDirectory,
    ) {
        debug!("readdir: ino={}, offset={}", ino, offset);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // Check directory cache
        let entries = {
            let mut cache = self.dir_cache.lock().unwrap();
            if let Some((entries, cached_at)) = cache.get(&ino) {
                if cached_at.elapsed().unwrap_or(Duration::MAX) < ATTR_TTL {
                    entries.clone()
                } else {
                    cache.pop(&ino);
                    Vec::new()
                }
            } else {
                Vec::new()
            }
        };

        let entries = if entries.is_empty() {
            match self.client.list(&path) {
                Ok(entries) => {
                    // Cache the result
                    let mut cache = self.dir_cache.lock().unwrap();
                    cache.put(ino, (entries.clone(), SystemTime::now()));
                    entries
                }
                Err(e) => {
                    let msg = e.to_string();
                    if msg.contains("not found") {
                        reply.error(ENOENT);
                    } else {
                        error!("readdir error for {}: {}", path, e);
                        reply.error(EIO);
                    }
                    return;
                }
            }
        } else {
            entries
        };

        // Build entries with . and ..
        let mut all_entries: Vec<(u64, FileType, String)> = vec![
            (ino, FileType::Directory, ".".to_string()),
            (ino, FileType::Directory, "..".to_string()),
        ];

        for entry in &entries {
            let child_path = Self::join_path(&path, &entry.name);
            let child_inode = self.get_or_create_inode(&child_path);
            let kind = if entry.entry_type == "directory" {
                FileType::Directory
            } else {
                FileType::RegularFile
            };
            all_entries.push((child_inode, kind, entry.name.clone()));
        }

        // Return entries starting from offset
        for (i, (inode, kind, name)) in all_entries.iter().enumerate().skip(offset as usize) {
            if reply.add(*inode, (i + 1) as i64, *kind, name) {
                break;
            }
        }

        reply.ok();
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
        debug!("read: ino={}, offset={}, size={}", ino, offset, size);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // Read using SQLite cache with ETag support
        let content = match self.read_cached(&path) {
            Ok((data, _etag)) => data,
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("not found") {
                    reply.error(ENOENT);
                } else {
                    error!("read error for {}: {}", path, e);
                    reply.error(EIO);
                }
                return;
            }
        };

        // Return requested slice
        let offset = offset as usize;
        if offset >= content.len() {
            reply.data(&[]);
        } else {
            let end = std::cmp::min(offset + size as usize, content.len());
            reply.data(&content[offset..end]);
        }
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
        debug!("write: ino={}, offset={}, size={}", ino, offset, data.len());

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // For simplicity, we only support full file writes (offset 0)
        // For partial writes, we'd need to read-modify-write
        if offset != 0 {
            // Read existing content first (use cache if available)
            let existing = match self.read_cached(&path) {
                Ok((data, _)) => data,
                Err(_) => Vec::new(),
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
                    self.invalidate_path(&path);
                    reply.written(data.len() as u32);
                }
                Err(e) => {
                    error!("write error for {}: {}", path, e);
                    reply.error(EIO);
                }
            }
        } else {
            match self.client.write(&path, data) {
                Ok(_) => {
                    self.invalidate_path(&path);
                    reply.written(data.len() as u32);
                }
                Err(e) => {
                    error!("write error for {}: {}", path, e);
                    reply.error(EIO);
                }
            }
        }
    }

    fn create(
        &mut self,
        _req: &Request,
        parent: u64,
        name: &OsStr,
        _mode: u32,
        _umask: u32,
        _flags: i32,
        reply: fuser::ReplyCreate,
    ) {
        let name = name.to_string_lossy();
        debug!("create: parent={}, name={}", parent, name);

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let path = Self::join_path(&parent_path, &name);

        // Create empty file
        match self.client.write(&path, &[]) {
            Ok(_) => {
                let inode = self.get_or_create_inode(&path);
                self.invalidate_path(&path);

                let attr = self.make_attr(inode, "file", 0, None, None);
                reply.created(&ATTR_TTL, &attr, 0, 0, 0);
            }
            Err(e) => {
                error!("create error for {}: {}", path, e);
                reply.error(EIO);
            }
        }
    }

    fn mkdir(
        &mut self,
        _req: &Request,
        parent: u64,
        name: &OsStr,
        _mode: u32,
        _umask: u32,
        reply: ReplyEntry,
    ) {
        let name = name.to_string_lossy();
        debug!("mkdir: parent={}, name={}", parent, name);

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let path = Self::join_path(&parent_path, &name);

        match self.client.mkdir(&path) {
            Ok(_) => {
                let inode = self.get_or_create_inode(&path);
                self.invalidate_path(&path);

                let attr = self.make_attr(inode, "directory", 0, None, None);
                reply.entry(&ATTR_TTL, &attr, 0);
            }
            Err(e) => {
                error!("mkdir error for {}: {}", path, e);
                reply.error(EIO);
            }
        }
    }

    fn unlink(&mut self, _req: &Request, parent: u64, name: &OsStr, reply: fuser::ReplyEmpty) {
        let name = name.to_string_lossy();
        debug!("unlink: parent={}, name={}", parent, name);

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let path = Self::join_path(&parent_path, &name);

        match self.client.delete(&path) {
            Ok(_) => {
                self.invalidate_path(&path);
                reply.ok();
            }
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("not found") {
                    reply.error(ENOENT);
                } else {
                    error!("unlink error for {}: {}", path, e);
                    reply.error(EIO);
                }
            }
        }
    }

    fn rmdir(&mut self, _req: &Request, parent: u64, name: &OsStr, reply: fuser::ReplyEmpty) {
        let name = name.to_string_lossy();
        debug!("rmdir: parent={}, name={}", parent, name);

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let path = Self::join_path(&parent_path, &name);

        // Check if directory is empty
        match self.client.list(&path) {
            Ok(entries) if !entries.is_empty() => {
                reply.error(ENOTEMPTY);
                return;
            }
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("not found") {
                    reply.error(ENOENT);
                } else {
                    error!("rmdir list error for {}: {}", path, e);
                    reply.error(EIO);
                }
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
                error!("rmdir error for {}: {}", path, e);
                reply.error(EIO);
            }
        }
    }

    fn rename(
        &mut self,
        _req: &Request,
        parent: u64,
        name: &OsStr,
        newparent: u64,
        newname: &OsStr,
        _flags: u32,
        reply: fuser::ReplyEmpty,
    ) {
        let name = name.to_string_lossy();
        let newname = newname.to_string_lossy();
        debug!(
            "rename: parent={}, name={}, newparent={}, newname={}",
            parent, name, newparent, newname
        );

        let parent_path = match self.get_path(parent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let new_parent_path = match self.get_path(newparent) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        let old_path = Self::join_path(&parent_path, &name);
        let new_path = Self::join_path(&new_parent_path, &newname);

        // For POSIX compliance, delete destination if it exists (unless RENAME_NOREPLACE flag set)
        // flags: RENAME_NOREPLACE = 1, RENAME_EXCHANGE = 2
        if _flags & 1 == 0 && self.client.exists(&new_path) {
            let _ = self.client.delete(&new_path);
        }

        match self.client.rename(&old_path, &new_path) {
            Ok(_) => {
                // Update inode mappings
                // Note: Extract to variable first to avoid deadlock from extended lifetime
                // of MutexGuard in if-let conditions
                let inode_opt = self.path_to_inode.lock().unwrap().remove(&old_path);
                if let Some(inode) = inode_opt {
                    self.path_to_inode.lock().unwrap().insert(new_path.clone(), inode);
                    self.inode_to_path.lock().unwrap().insert(inode, new_path.clone());
                }
                self.invalidate_path(&old_path);
                self.invalidate_path(&new_path);
                reply.ok();
            }
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("not found") {
                    reply.error(ENOENT);
                } else {
                    error!("rename error: {} -> {}: {}", old_path, new_path, e);
                    reply.error(EIO);
                }
            }
        }
    }

    fn setattr(
        &mut self,
        _req: &Request,
        ino: u64,
        _mode: Option<u32>,
        _uid: Option<u32>,
        _gid: Option<u32>,
        size: Option<u64>,
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
        debug!("setattr: ino={}, size={:?}", ino, size);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // Handle truncate
        if let Some(new_size) = size {
            if new_size == 0 {
                // Truncate to empty
                match self.client.write(&path, &[]) {
                    Ok(_) => {
                        self.invalidate_path(&path);
                    }
                    Err(e) => {
                        error!("truncate error for {}: {}", path, e);
                        reply.error(EIO);
                        return;
                    }
                }
            } else {
                // Truncate to specific size - read and rewrite (use cache if available)
                match self.read_cached(&path) {
                    Ok((mut data, _)) => {
                        data.resize(new_size as usize, 0);
                        match self.client.write(&path, &data) {
                            Ok(_) => {
                                self.invalidate_path(&path);
                            }
                            Err(e) => {
                                error!("truncate write error for {}: {}", path, e);
                                reply.error(EIO);
                                return;
                            }
                        }
                    }
                    Err(e) => {
                        error!("truncate read error for {}: {}", path, e);
                        reply.error(EIO);
                        return;
                    }
                }
            }
        }

        // Return updated attributes
        match self.get_attr(ino, &path) {
            Ok(attr) => reply.attr(&ATTR_TTL, &attr),
            Err(e) => reply.error(e),
        }
    }

    fn open(&mut self, _req: &Request, ino: u64, _flags: i32, reply: fuser::ReplyOpen) {
        debug!("open: ino={}", ino);

        // Check if file exists
        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // Check if path exists
        if !self.client.exists(&path) {
            reply.error(ENOENT);
            return;
        }

        // Check if it's a directory
        if self.is_directory(&path) {
            reply.error(EISDIR);
        } else {
            reply.opened(0, 0);
        }
    }

    fn opendir(&mut self, _req: &Request, ino: u64, _flags: i32, reply: fuser::ReplyOpen) {
        debug!("opendir: ino={}", ino);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        // Root always exists and is a directory
        if path == "/" {
            reply.opened(0, 0);
            return;
        }

        // Check if path exists
        if !self.client.exists(&path) {
            reply.error(ENOENT);
            return;
        }

        // Check if it's a directory
        if !self.is_directory(&path) {
            reply.error(ENOTDIR);
        } else {
            reply.opened(0, 0);
        }
    }

    fn flush(&mut self, _req: &Request, _ino: u64, _fh: u64, _lock_owner: u64, reply: fuser::ReplyEmpty) {
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
        reply: fuser::ReplyEmpty,
    ) {
        reply.ok();
    }

    fn releasedir(&mut self, _req: &Request, _ino: u64, _fh: u64, _flags: i32, reply: fuser::ReplyEmpty) {
        reply.ok();
    }

    fn access(&mut self, _req: &Request, ino: u64, _mask: i32, reply: fuser::ReplyEmpty) {
        debug!("access: ino={}", ino);

        let path = match self.get_path(ino) {
            Some(p) => p,
            None => {
                reply.error(ENOENT);
                return;
            }
        };

        if self.client.exists(&path) {
            reply.ok();
        } else {
            reply.error(ENOENT);
        }
    }
}
