//! File I/O syscalls — `sys_read`, `sys_write`, `sys_stat`,
//! `sys_unlink`, `sys_rename`, `sys_copy`, `sys_mkdir`, `sys_rmdir`.
//!
//! Phase G of Phase 3 restructure plan extracted these methods from
//! the monolithic `kernel.rs`.  The split is a file-organization
//! change — every method stays a member of [`Kernel`] via the
//! submodule's `impl Kernel { ... }` block.

use std::sync::atomic::Ordering;

use crate::dcache::{CachedEntry, DT_DIR, DT_MOUNT, DT_PIPE, DT_REG, DT_STREAM};
use crate::dispatch::{
    DeleteHookCtx, FileEventType, HookContext, HookIdentity, ReadHookCtx, RenameHookCtx,
    WriteHookCtx,
};
use crate::lock_manager::{LockManager, LockMode};

use super::{
    validate_path_fast, Kernel, KernelError, OperationContext, StatResult, SysCopyResult,
    SysMkdirResult, SysReadResult, SysRenameResult, SysRmdirResult, SysUnlinkResult,
    SysWriteResult,
};

impl Kernel {
    pub fn sys_read(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        // 1. Validate
        validate_path_fast(path)?;

        // 1a. DT_LINK transparent follow (KERNEL-ARCHITECTURE.md §4.5).
        // Non-link paths borrow the input; link paths produce an owned
        // String for the resolved target. The rest of the syscall sees
        // the resolved path and is otherwise indistinguishable from a
        // direct read at the target.
        let resolved = self.resolve_path_through_link(path)?;
        let path = resolved.as_ref();

        // 1b. Trie-resolved virtual paths (§11 Phase 21) — Python's resolve_read
        // should have handled these before reaching us; treat as missing.
        if self.trie.lookup(path).is_some() {
            return Err(not_found());
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14) — permission check etc.
        let hook_id = HookIdentity {
            user_id: ctx.user_id.clone(),
            zone_id: ctx.zone_id.clone(),
            agent_id: ctx.agent_id.clone().unwrap_or_default(),
            is_admin: ctx.is_admin,
        };
        self.dispatch_native_pre(&HookContext::Read(ReadHookCtx {
            path: path.to_string(),
            identity: hook_id,
            content: None,
            content_id: None,
        }))?;

        // 2. Route (pure Rust LPM)
        let route = match self.vfs_router.route(path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return Err(not_found()),
        };

        // External mounts now fall through to the normal backend read path
        // — Rust-registered ObjectStore handles all connectors natively.

        // 3. DCache lookup — on miss, fallback to metastore (cold path)
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                // MetaStore fallback (per-mount first, then global) — full path
                match self.with_metastore(&route.mount_point, |ms| ms.get(path)) {
                    Some(Ok(Some(meta))) => {
                        // Populate dcache from metastore result
                        self.dcache.put(path, (&meta).into());
                        // Re-fetch from dcache (now populated)
                        self.dcache.get_entry(path).unwrap()
                    }
                    Some(Ok(None)) | Some(Err(_)) | None => {
                        // MetaStore miss → try backend directly (all backend types
                        // uniformly).  CAS backends return Err for path-based reads
                        // (hash-addressed).  Path-local/external backends serve the
                        // file if it exists on disk / via API.  No ABC leak: kernel
                        // treats every backend the same through ObjectStore trait.
                        if let Some(data) = self.vfs_router.read_content(
                            &route.mount_point,
                            &route.backend_path, // PAS uses as path; CAS rejects (not a hash)
                            ctx,
                        ) {
                            return Ok(SysReadResult {
                                data: Some(data),
                                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                                content_id: None,
                                entry_type: DT_REG,
                            });
                        }
                        return Err(not_found());
                    }
                }
            }
        };

        // DT_PIPE — try Rust IPC registry (nowait pop)
        if entry.entry_type == DT_PIPE {
            if let Some(buf) = self.pipe_manager.get(path) {
                match buf.pop() {
                    Ok(data) => {
                        return Ok(SysReadResult {
                            data: Some(data),
                            post_hook_needed: false,
                            content_id: None,
                            entry_type: DT_PIPE,
                        });
                    }
                    Err(crate::pipe::PipeError::Empty) => {
                        // Empty — surface DT_PIPE so Python async shell retries.
                        return Ok(SysReadResult {
                            data: None,
                            post_hook_needed: false,
                            content_id: None,
                            entry_type: DT_PIPE,
                        });
                    }
                    Err(crate::pipe::PipeError::ClosedEmpty) => {
                        return Err(KernelError::PipeClosed(path.to_string()));
                    }
                    Err(_) => {}
                }
            }
            // Not in Rust registry — fall through to Python fallback.
            return Ok(SysReadResult {
                data: None,
                post_hook_needed: false,
                content_id: None,
                entry_type: DT_PIPE,
            });
        }

        // DT_STREAM — surface to wrapper so Python stream_read_at handles offset.
        if entry.entry_type == DT_STREAM {
            return Ok(SysReadResult {
                data: None,
                post_hook_needed: false,
                content_id: None,
                entry_type: DT_STREAM,
            });
        }

        // Content identifier: CAS backends use content_id (hash). Path-addressed
        // backends derive their physical path from `path - mount_prefix`
        // inside the backend itself; the kernel always passes the content_id.
        let content_id = match entry.content_id.as_deref().filter(|s| !s.is_empty()) {
            Some(id) => id,
            None => return Err(not_found()),
        };

        // 4. VFS lock (blocking acquire — wrapper releases GIL before calling this)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Read, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return Err(KernelError::IOError(format!(
                "vfs read lock timeout: {path}"
            )));
        }

        // 5. Backend read (Rust-native ObjectStore)
        let content = self
            .vfs_router
            .read_content(&route.mount_point, content_id, ctx);

        // 6. Release VFS lock (always, even on miss)
        self.lock_manager.do_release(lock_handle);

        // 7. Return result
        match content {
            Some(data) => Ok(SysReadResult {
                data: Some(data),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_id: entry.content_id.clone(),
                entry_type: DT_REG,
            }),
            // Local backend miss + metadata exists → federation path:
            // try the origin encoded in backend_name. Otherwise it's a
            // genuine miss.
            None => self.try_remote_fetch(path, &entry, &route.mount_point, ctx),
        }
    }

    /// Federation on-demand content fetch (store-and-forward).
    ///
    /// When local read of a Raft-replicated entry misses,
    /// ``last_writer_address`` names the node that wrote it. We send
    /// the *virtual path* over to that peer's ``ReadBlob`` RPC; the
    /// peer's ``BlobFetcher::read`` self-routes through its own
    /// ``VFSRouter`` exactly like a local ``sys_read`` and lets each
    /// backend interpret the locally-stored ``content_id`` (CAS hash
    /// or PAS backend_path) however it likes. The kernel performs no
    /// CAS-vs-PAS dispatch — the peer's mount table answers that.
    ///
    /// Returns ``Err(FileNotFound)`` when ``last_writer_address`` is
    /// unset, equals ``self_address``, or the remote call fails.
    fn try_remote_fetch(
        &self,
        path: &str,
        entry: &CachedEntry,
        mount_point: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        let origin = match entry.last_writer_address.as_deref() {
            Some(s) if !s.is_empty() => s,
            _ => return Err(not_found()),
        };

        // Don't loop back to self — we're the writer, blob is truly missing.
        if let Some(addr) = self.self_address.read().as_deref() {
            if origin == addr {
                return Err(not_found());
            }
        }

        // Drive the RPC on the kernel-owned shared runtime — reusing
        // the pooled tonic Channel from ``peer_client``. No more one-
        // shot ``new_current_thread()`` per call (that pattern left
        // the runtime lingering if the future hadn't finished
        // draining; see R11 hypothesis #2).
        //
        // Pass the file's **content_id** to the peer when we have one
        // (CAS hash for content-addressed storage, backend_path for
        // path-addressed storage). The peer's ``BlobFetcher::read``
        // then either fans out by hash across CAS backends or routes
        // the path to its own mount table. Falls back to the
        // user-facing global ``path`` when content_id is unset (cold
        // dcache or unwritten metadata) — ``BlobFetcher::read`` will
        // path-route it through the peer's VFSRouter.
        //
        // Caching the fetched blob locally is intentionally NOT done
        // here: that would require kernel-side knowledge of the local
        // mount's addressing scheme (CAS hash → write_content; PAS →
        // which backend_path slot), exactly the thing this refactor
        // moved out. If a follow-up wants opportunistic local caching
        // it belongs in the local backend's ``write_content`` callable
        // from the BlobFetcher impl, not here.
        //
        // Phase 4 (full): peer_client is now
        // ``RwLock<Arc<dyn PeerBlobClient>>``. ``peer_client_arc()``
        // clones the Arc out from under the read lock so the actual
        // fetch happens lock-free.
        let fetch_key = entry
            .content_id
            .as_deref()
            .filter(|s| !s.is_empty())
            .unwrap_or(path);
        let client = self.peer_client_arc();
        let data = client
            .fetch(origin, fetch_key)
            .map_err(KernelError::IOError)?;

        // Cache the fetched blob locally so subsequent reads don't need to
        // hit the writer node again. Critical for failover: once the
        // origin goes down, re-fetch would fail (see
        // `TestLeaderFailover::test_failover_and_recovery`) but the blob
        // must still be readable from local storage.
        //
        // ``write_content`` is idempotent on the addressing key: CAS
        // backends compute the same hash for the same bytes; PAS
        // backends overwrite the file at the same backend_path. We pass
        // through the writer's ``content_id`` (CAS hash or PAS backend_
        // path — kernel-opaque) so the local backend stores the bytes
        // under the same key the metastore points at. Failure is
        // swallowed: the read still returns the bytes, the next read
        // will simply remote-fetch again.
        let cache_key = entry.content_id.as_deref().unwrap_or("");
        if !cache_key.is_empty() {
            let _ = self
                .vfs_router
                .write_content(mount_point, &data, cache_key, ctx, 0);
        }

        Ok(SysReadResult {
            data: Some(data),
            post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
            content_id: entry.content_id.clone(),
            entry_type: DT_REG,
        })
    }

    // ── sys_write ──────────────────────────────────────────────────────

    /// Rust syscall: write file content (pure Rust, no GIL).
    ///
    /// validate -> route -> VFS lock -> CAS write -> metadata build -> metastore.put
    /// -> dcache update -> return.
    ///
    /// Hooks are NOT dispatched here — wrapper handles PRE-INTERCEPT.
    pub fn sys_write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError> {
        let miss = || {
            Ok(SysWriteResult {
                hit: false,
                content_id: None,
                post_hook_needed: false,
                version: 0,
                size: 0,
                is_new: false,
                old_content_id: None,
                old_size: None,
                old_version: None,
                old_modified_at_ms: None,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 1a. DT_LINK transparent follow (KERNEL-ARCHITECTURE.md §4.5).
        // Same one-hop semantics as sys_read; sys_setattr's link branch
        // rejects self-loops at write time so the resolver only handles
        // chain rejection here.
        let resolved = self.resolve_path_through_link(path)?;
        let path = resolved.as_ref();

        // 1b. Trie-resolved virtual paths (§11 Phase 21)
        if self.trie.lookup(path).is_some() {
            return miss();
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Write(WriteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
            content: vec![], // no clone — no current hook inspects content
            is_new_file: false,
            content_id: None,
            new_version: 0,
            size_bytes: None,
        }))?;

        // 2. Route (check write access)
        let route = match self.vfs_router.route(path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. DCache check — DT_PIPE/DT_STREAM: try Rust IPC registry
        if let Some(entry) = self.dcache.get_entry(path) {
            if entry.entry_type == DT_PIPE {
                if let Some(buf) = self.pipe_manager.get(path) {
                    match buf.push(content) {
                        Ok(n) => {
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: false,
                                version: 0,
                                size: n as u64,
                                is_new: false,
                                old_content_id: None,
                                old_size: None,
                                old_version: None,
                                old_modified_at_ms: None,
                            });
                        }
                        Err(crate::pipe::PipeError::Full(_, _)) => {
                            // Full — return miss so Python async shell retries
                            return miss();
                        }
                        Err(crate::pipe::PipeError::Closed(msg)) => {
                            return Err(KernelError::PipeClosed(msg.to_string()));
                        }
                        Err(_) => {}
                    }
                }
                return miss();
            }
            if entry.entry_type == DT_STREAM {
                if let Some(buf) = self.stream_manager.get(path) {
                    match buf.push(content) {
                        Ok(offset) => {
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: false,
                                version: 0,
                                size: offset as u64,
                                is_new: false,
                                old_content_id: None,
                                old_size: None,
                                old_version: None,
                                old_modified_at_ms: None,
                            });
                        }
                        Err(crate::stream::StreamError::Full(_, _)) => return miss(),
                        Err(crate::stream::StreamError::Closed(msg)) => {
                            return Err(KernelError::StreamClosed(msg.to_string()));
                        }
                        Err(_) => {}
                    }
                }
                return miss();
            }
        }

        // 4. VFS lock (blocking write lock)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return miss();
        }

        // 5. Backend write (Rust-native ObjectStore).
        //    Pass backend_path as content_id for PAS; for CAS at offset=0
        //    content_id is ignored, but for offset>0 we need the OLD
        //    content hash so CASEngine::write_partial can splice against
        //    it. Look up old entry (dcache → metastore fallback).
        let effective_content_id = if offset == 0 {
            route.backend_path.clone()
        } else {
            // Partial write path: use the CAS hash from the existing inode.
            // PathLocalBackend ignores content_id when offset>0 (uses the
            // on-disk file instead), so this value is only consulted by
            // CasLocalBackend.
            let old_entry = self.dcache.get_entry(path).or_else(|| {
                self.with_metastore(&route.mount_point, |ms| {
                    ms.get(path).ok().flatten().map(|m| (&m).into())
                })
                .flatten()
            });
            match old_entry {
                Some(e) => e.content_id.unwrap_or_default(),
                None => {
                    // Partial write requires an existing file — but
                    // `sys_write` contract says "file must exist" anyway,
                    // so just surface that.
                    self.lock_manager.do_release(lock_handle);
                    return Err(KernelError::FileNotFound(path.to_string()));
                }
            }
        };
        let write_result = match self.vfs_router.write_content(
            &route.mount_point,
            content,
            &effective_content_id,
            ctx,
            offset,
        ) {
            Ok(opt) => opt,
            Err(storage_err) => {
                // Storage/backend-level failure (connector wrapper raised a
                // BackendError, disk full, permission denied, etc.). Release
                // the VFS lock and surface the error to Python so callers
                // can react (F2 C4 / Issue #3765 Cat-7 regression — previous
                // code silently swallowed this via ``.ok()``).
                self.lock_manager.do_release(lock_handle);
                return Err(KernelError::BackendError(format!("{storage_err:?}")));
            }
        };

        // 6. After write -> build metadata + metastore.put + dcache update
        let result = match write_result {
            Some(wr) => {
                // Snapshot old state for OBSERVE event payload + Python
                // post-hook dispatch (is_new, old_content_id, old_size, etc.).
                // DCache → metastore fallback ensures accuracy even on cold
                // dcache (matches the authority that Python metadata.get()
                // had before this crossing elimination).
                let old_entry = self.dcache.get_entry(path).or_else(|| {
                    self.with_metastore(&route.mount_point, |ms| {
                        ms.get(path).ok().flatten().map(|m| (&m).into())
                    })
                    .flatten()
                });
                let old_version = old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                let old_content_id = old_entry.as_ref().and_then(|e| e.content_id.clone());
                let new_version = old_version + 1;

                // Build FileMetadata and persist via metastore (per-mount or global)
                let now_ms = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0);
                let created_at_ms = old_entry
                    .as_ref()
                    .and_then(|e| e.created_at_ms)
                    .or(Some(now_ms));
                // R20.3: always pass the full global path. Per-mount
                // ZoneMetaStore translates at its boundary; the global
                // fallback stores full paths directly.
                let meta = self.build_metadata(
                    path,
                    &route.zone_id,
                    DT_REG,
                    wr.size,
                    Some(wr.content_id.clone()),
                    new_version,
                    None,
                    created_at_ms,
                    Some(now_ms),
                );
                // Atomic commit — metastore (raft) first, dcache on
                // success. Releases the VFS lock before propagating
                // so the next caller doesn't block on stale state if
                // raft propose fails.
                if let Err(e) = self.commit_metadata(path, &route.mount_point, meta) {
                    self.lock_manager.do_release(lock_handle);
                    return Err(e);
                }

                // Snapshot old_entry fields for the result struct before
                // dispatch_mutation moves old_content_id into its closure.
                let result_is_new = old_entry.is_none();
                let result_old_etag = old_content_id.clone();
                let result_old_size = old_entry.as_ref().map(|e| e.size);
                let result_old_version = old_entry.as_ref().map(|e| e.version);
                let result_old_modified_at_ms = old_entry.as_ref().and_then(|e| e.modified_at_ms);

                // OBSERVE-phase dispatch (§11 Phase 5): queue FileWrite to
                // the kernel observer ThreadPool. Returns immediately —
                // observer callbacks run off the syscall hot path.
                let content_id = wr.content_id.clone();
                let size = wr.size;
                self.dispatch_mutation(FileEventType::FileWrite, path, ctx, |ev| {
                    ev.size = Some(size);
                    ev.content_id = Some(content_id);
                    ev.version = Some(new_version);
                    ev.is_new = old_version == 0;
                    ev.old_content_id = old_content_id;
                });

                // Native POST hooks (fire-and-forget — AuditHook sends to channel
                // in ~100 ns; no content clone on post path).
                self.dispatch_native_post(&HookContext::Write(WriteHookCtx {
                    path: path.to_string(),
                    identity: HookIdentity {
                        user_id: ctx.user_id.clone(),
                        zone_id: ctx.zone_id.clone(),
                        agent_id: ctx.agent_id.clone().unwrap_or_default(),
                        is_admin: ctx.is_admin,
                    },
                    content: vec![],
                    is_new_file: result_is_new,
                    content_id: None,
                    new_version: new_version.into(),
                    size_bytes: Some(wr.size),
                }));

                Ok(SysWriteResult {
                    hit: true,
                    content_id: Some(wr.content_id),
                    post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0,
                    version: new_version,
                    size: wr.size,
                    is_new: result_is_new,
                    old_content_id: result_old_etag,
                    old_size: result_old_size,
                    old_version: result_old_version,
                    old_modified_at_ms: result_old_modified_at_ms,
                })
            }
            None => miss(),
        };

        // 7. Release VFS lock (always, even on miss)
        self.lock_manager.do_release(lock_handle);

        result
    }

    // ── sys_stat ───────────────────────────────────────────────────────

    /// Rust syscall: get file metadata (pure Rust, no GIL).
    ///
    /// validate -> route -> dcache lookup -> return StatResult.
    /// Returns None on dcache miss or trie-resolved paths (wrapper handles).
    pub fn sys_stat(&self, path: &str, zone_id: &str) -> Option<StatResult> {
        // 1. Validate
        if validate_path_fast(path).is_err() {
            return None;
        }

        // 2. Trie-resolved paths -> wrapper handles
        if self.trie.lookup(path).is_some() {
            return None;
        }

        // 2.5 Federation procfs: /__sys__/zones/<id> exposes raft cluster
        // status as a synthesised file entry; /__sys__/zones/ exposes the
        // zone-id directory.  This is the read side of the kernel's
        // virtual federation namespace — service-tier callers read zone
        // state through `sys_stat` instead of a direct PyKernel surface.
        if let Some(stat) = self.zones_procfs_stat(path) {
            return Some(stat);
        }

        // 3. Route
        let route = self.vfs_router.route(path, zone_id).ok()?;

        // 4. DCache lookup. On miss, fall back to the per-mount metastore
        //    so federation zones see inodes that haven't been cached yet
        //    (F2 C5 — matches sys_read's cold path). Full path.
        //    On double miss, check implicit directory (path has children
        //    in metastore but no explicit entry — e.g. /docs/ when
        //    /docs/readme.md exists). Returns synthetic DT_DIR.
        let entry = match self.dcache.get_entry(path) {
            Some(e) => e,
            None => {
                match self
                    .with_metastore(&route.mount_point, |ms| ms.get(path).ok().flatten())
                    .flatten()
                {
                    Some(meta) => {
                        let cached: CachedEntry = (&meta).into();
                        self.dcache.put(path, cached.clone());
                        cached
                    }
                    None => {
                        // Implicit directory: children exist under this prefix
                        // but no explicit entry. Eliminates Python fallback to
                        // _check_is_directory() (Crossing 3a).
                        let is_implicit = self
                            .with_metastore(&route.mount_point, |ms| {
                                ms.is_implicit_directory(path).unwrap_or(false)
                            })
                            .unwrap_or(false);
                        if is_implicit {
                            return Some(StatResult {
                                path: path.to_string(),
                                size: 4096,
                                content_id: None,
                                mime_type: "inode/directory".to_string(),
                                is_directory: true,
                                entry_type: DT_DIR,
                                mode: 0o755,
                                version: 0,
                                zone_id: Some(route.zone_id.clone()),
                                created_at_ms: None,
                                modified_at_ms: None,
                                last_writer_address: None,
                                lock: None,
                                link_target: None,
                            });
                        }
                        return None;
                    }
                }
            }
        };

        // Treat DT_MOUNT like a directory for VFS callers — a mount point is
        // the zone-root inode, analogous to a DT_DIR from the user's view.
        let is_dir = entry.entry_type == DT_DIR || entry.entry_type == DT_MOUNT;
        let mime = entry
            .mime_type
            .as_deref()
            .unwrap_or(if is_dir {
                "inode/directory"
            } else {
                "application/octet-stream"
            })
            .to_string();

        let lock = self.lock_manager.get_lock_info(path).ok().flatten();

        Some(StatResult {
            path: path.to_string(),
            size: if is_dir && entry.size == 0 {
                4096
            } else {
                entry.size
            },
            content_id: entry.content_id,
            mime_type: mime,
            is_directory: is_dir,
            entry_type: entry.entry_type,
            mode: if is_dir { 0o755 } else { 0o644 },
            version: entry.version,
            zone_id: entry.zone_id,
            created_at_ms: entry.created_at_ms,
            modified_at_ms: entry.modified_at_ms,
            last_writer_address: entry.last_writer_address,
            lock,
            link_target: entry.link_target,
        })
    }

    // ── sys_unlink ────────────────────────────────────────────────────

    /// Rust syscall: full unlink (validate → route → metastore → backend → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation. Python only
    /// dispatches event notify + POST hooks.
    /// Returns `hit=false` for DT_EXTERNAL_STORAGE (5) → Python handles connector teardown.
    /// DT_DIR is handled inline via sys_rmdir (§12e).
    pub fn sys_unlink(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysUnlinkResult, KernelError> {
        let miss = |et: u8| {
            Ok(SysUnlinkResult {
                hit: false,
                entry_type: et,
                post_hook_needed: false,
                path: path.to_string(),
                content_id: None,
                size: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 1b. Trie-resolved virtual paths (§11 Phase 21)
        if self.trie.lookup(path).is_some() {
            return miss(0);
        }

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Delete(DeleteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
        }))?;

        // 2. Route (check write access)
        let route = match self.vfs_router.route(path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(0),
        };

        // 3. Get metadata (dcache or metastore — per-mount first, then global)
        let meta = match self.dcache.get_entry(path) {
            Some(e) => Some(e),
            None => self
                .with_metastore(&route.mount_point, |ms| {
                    ms.get(path).ok().flatten().map(|m| (&m).into())
                })
                .flatten(),
        };

        let entry = match meta {
            Some(e) => e,
            None => return miss(0),
        };

        // 4. Entry-type dispatch
        match entry.entry_type {
            DT_PIPE => {
                // Destroy pipe buffer + metastore/dcache cleanup (Rust-native)
                let _ = self.destroy_pipe(path);
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: DT_PIPE,
                    post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
                    path: path.to_string(),
                    content_id: entry.content_id,
                    size: entry.size,
                });
            }
            DT_STREAM => {
                // Destroy stream buffer + metastore/dcache cleanup (Rust-native)
                let _ = self.destroy_stream(path);
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: DT_STREAM,
                    post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
                    path: path.to_string(),
                    content_id: entry.content_id,
                    size: entry.size,
                });
            }
            DT_DIR => {
                // §12e: handle DT_DIR inline instead of returning miss.
                // Delegates to sys_rmdir which handles recursive delete,
                // backend rmdir, dcache evict, and observer dispatch.
                let rmdir_result = self.sys_rmdir(path, ctx, recursive)?;
                return Ok(SysUnlinkResult {
                    hit: rmdir_result.hit,
                    entry_type: DT_DIR,
                    post_hook_needed: rmdir_result.post_hook_needed,
                    path: path.to_string(),
                    content_id: entry.content_id,
                    size: entry.size,
                });
            }
            // DT_MOUNT (2) → full unmount lifecycle (metastore + dcache + routing
            // table). Returns hit=true so callers don't need a separate
            // Python-side `unmount()` shim — `sys_unlink(mount_path)` is the
            // single entry point.
            DT_MOUNT => {
                let zone_id = entry.zone_id.clone().unwrap_or_else(|| ctx.zone_id.clone());
                self.dlc.unmount(self, path, &zone_id);
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: DT_MOUNT,
                    post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
                    path: path.to_string(),
                    content_id: entry.content_id,
                    size: entry.size,
                });
            }
            // DT_EXTERNAL_STORAGE (5) — connector-backed mounts (oauth/api).
            // Their lifecycle (token revocation, connector teardown) lives
            // in Python; keep as a miss so the Python layer dispatches.
            5 => return miss(entry.entry_type),
            _ => {}
        }

        // 5. VFS write lock (DT_REG path)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return miss(entry.entry_type);
        }

        // 6. Atomic delete — metastore (raft) first, dcache evict on
        // success. If raft propose fails (quorum unreachable), the
        // entry stays in BOTH the state machine and the dcache so a
        // retry sees a consistent view rather than a phantom miss.
        if let Err(e) = self.commit_delete(path, &route.mount_point) {
            self.lock_manager.do_release(lock_handle);
            return Err(e);
        }

        // 7. Backend delete (best-effort, PAS only) — only after
        // metastore commit succeeded; otherwise we'd orphan the file
        // on the filesystem with no metadata pointing at it.
        let _ = self
            .vfs_router
            .delete_file(&route.mount_point, &route.backend_path);

        // 8. Release VFS lock
        self.lock_manager.do_release(lock_handle);

        // 10. OBSERVE-phase dispatch (§11 Phase 5): queue FileDelete.
        // Cloned out of `entry` because the SysUnlinkResult below also
        // moves them.
        let etag_for_event = entry.content_id.clone();
        let size_for_event = entry.size;
        self.dispatch_mutation(FileEventType::FileDelete, path, ctx, |ev| {
            ev.size = Some(size_for_event);
            ev.content_id = etag_for_event;
        });

        // 11. Return hit=true with metadata for event payload
        self.dispatch_native_post(&HookContext::Delete(DeleteHookCtx {
            path: path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
        }));
        Ok(SysUnlinkResult {
            hit: true,
            entry_type: entry.entry_type,
            post_hook_needed: self.delete_hook_count.load(Ordering::Relaxed) > 0,
            path: path.to_string(),
            content_id: entry.content_id,
            size: entry.size,
        })
    }

    // ── sys_rename ────────────────────────────────────────────────────

    /// Rust syscall: full rename (validate → route → VFS lock → metastore → backend → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Returns `hit=false` for DT_MOUNT/DT_PIPE/DT_STREAM → Python fallback.
    pub fn sys_rename(
        &self,
        old_path: &str,
        new_path: &str,
        ctx: &OperationContext,
    ) -> Result<SysRenameResult, KernelError> {
        let miss = || {
            Ok(SysRenameResult {
                hit: false,
                success: false,
                post_hook_needed: false,
                is_directory: false,
                old_content_id: None,
                old_size: None,
                old_version: None,
                old_modified_at_ms: None,
            })
        };

        // 1. Validate both
        validate_path_fast(old_path)?;
        validate_path_fast(new_path)?;

        // 1c. Native INTERCEPT PRE hooks (§11 Phase 14)
        self.dispatch_native_pre(&HookContext::Rename(RenameHookCtx {
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
            is_directory: false,
        }))?;

        // 2. Route both
        let old_route = match self.vfs_router.route(old_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let new_route = match self.vfs_router.route(new_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. Sorted VFS lock acquire (deadlock-free: min(old,new) first)
        let (first, second) = if old_path <= new_path {
            (old_path, new_path)
        } else {
            (new_path, old_path)
        };

        let lock1 =
            self.lock_manager
                .blocking_acquire(first, LockMode::Write, self.vfs_lock_timeout_ms());
        let lock2 = if first != second {
            self.lock_manager
                .blocking_acquire(second, LockMode::Write, self.vfs_lock_timeout_ms())
        } else {
            0
        };

        let release_locks = |lm: &LockManager, h1: u64, h2: u64| {
            if h2 > 0 {
                lm.do_release(h2);
            }
            if h1 > 0 {
                lm.do_release(h1);
            }
        };

        // Lock timeout check
        if lock1 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // 4. Existence check: get old metadata (per-mount or global) — zone-relative keys
        let old_zone_path = Self::zone_key(&old_route.backend_path);
        let new_zone_path = Self::zone_key(&new_route.backend_path);
        let old_meta = self
            .with_metastore(&old_route.mount_point, |ms| {
                ms.get(&old_zone_path).ok().flatten()
            })
            .flatten();

        // Also check dcache
        let old_entry = self.dcache.get_entry(old_path);

        let (is_directory, entry_type) = match (&old_meta, &old_entry) {
            (Some(m), _) => (m.entry_type == DT_DIR, m.entry_type),
            (None, Some(e)) => (e.entry_type == DT_DIR, e.entry_type),
            (None, None) => {
                // Check for implicit directory: no explicit entry, but has children
                let child_prefix = format!("{}/", old_zone_path.trim_end_matches('/'));
                let has_children = self
                    .with_metastore(&old_route.mount_point, |ms| {
                        ms.list(&child_prefix)
                            .map(|v| !v.is_empty())
                            .unwrap_or(false)
                    })
                    .unwrap_or(false);
                if has_children {
                    (true, DT_DIR)
                } else {
                    // Source truly does not exist — raise FileNotFound
                    release_locks(&self.lock_manager, lock1, lock2);
                    return Err(KernelError::FileNotFound(old_path.to_string()));
                }
            }
        };

        // DT_PIPE/DT_STREAM: rename not supported (IPC endpoints are identity-bound)
        // DT_MOUNT (2) / DT_EXTERNAL_STORAGE (5): single metastore entries —
        // normal rename logic handles them (backend.rename() is a no-op for mounts).
        match entry_type {
            DT_PIPE | DT_STREAM => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(KernelError::IOError(format!(
                    "rename not supported for entry type {} at {}",
                    entry_type, old_path
                )));
            }
            _ => {}
        }

        // 5. Destination conflict check — use new_route's metastore for cross-mount
        let new_exists = self
            .with_metastore(&new_route.mount_point, |ms| {
                ms.exists(&new_zone_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if new_exists {
            release_locks(&self.lock_manager, lock1, lock2);
            return Err(KernelError::FileExists(format!(
                "Destination path already exists: {}",
                new_path
            )));
        }

        // 6. Rename — cross-mount vs same-mount
        let is_cross_mount = old_route.mount_point != new_route.mount_point;

        if is_cross_mount {
            // Cross-mount rename is always rejected regardless of addressing mode.
            //
            // For PAS: physically moving bytes requires a distributed 2PC that is
            // not atomic and cannot be compensated without a WAL.
            // For CAS-to-PAS or CAS-to-different-CAS: cloning metadata across
            // content-addressed namespaces leaves the destination pointing at a
            // content_id the destination backend cannot resolve, making the file
            // inaccessible after the source metastore entry is deleted.
            //
            // Callers must use sys_copy + sys_unlink for cross-mount moves.
            release_locks(&self.lock_manager, lock1, lock2);
            return Err(KernelError::IOError(
                "sys_rename: cross-mount rename not supported; use copy + delete instead"
                    .to_string(),
            ));
        } else {
            // Same-mount rename.
            //
            // For PAS (path-addressed) backends, rename bytes on storage BEFORE
            // committing the metastore update. If the backend rename fails the
            // metastore is untouched and the caller sees the error; no orphaned
            // metadata or aliased content_id is created. CAS backends return
            // None/NotSupported from rename_file (bytes are hash-addressed and
            // never moved), so the ordering does not matter for them.
            //
            // Errors from rename_file are propagated for PAS; for CAS/unsupported
            // backends the None result is silently accepted and only the metastore
            // rewrite happens (metadata-only rename, which is correct for CAS).
            // For PAS backends: rename bytes first so a storage failure never
            // leaves metadata committed to a path where no bytes were moved.
            // CAS backends do not move bytes on rename; drive them after metadata.
            let backend_renamed = if !old_route.is_cas {
                match self.vfs_router.rename_file(
                    &old_route.mount_point,
                    &old_route.backend_path,
                    &new_route.backend_path,
                ) {
                    Some(Err(e)) => {
                        release_locks(&self.lock_manager, lock1, lock2);
                        return Err(KernelError::IOError(format!(
                            "sys_rename: backend rename failed: {e:?}"
                        )));
                    }
                    Some(Ok(())) => true,
                    // None = backend does not implement rename (external connectors);
                    // fall through to metadata-only rename for those.
                    None => false,
                }
            } else {
                false
            };

            // Commit metadata after PAS bytes are moved (or immediately for CAS).
            let rename_result = self
                .with_metastore(&old_route.mount_point, |ms| {
                    ms.rename_path(&old_zone_path, &new_zone_path, !old_route.is_cas)
                })
                .ok_or_else(|| {
                    KernelError::IOError(format!(
                        "sys_rename: no metastore for {}",
                        old_route.mount_point
                    ))
                })?;
            if let Err(meta_err) = rename_result {
                // PAS: bytes already moved to new path — try to roll back so the
                // file is accessible again. If rollback also fails, report both
                // errors; data is at new backend path but metadata is at old path.
                if backend_renamed {
                    if let Some(Err(rollback_err)) = self.vfs_router.rename_file(
                        &old_route.mount_point,
                        &new_route.backend_path,
                        &old_route.backend_path,
                    ) {
                        release_locks(&self.lock_manager, lock1, lock2);
                        return Err(KernelError::IOError(format!(
                            "sys_rename: metastore failed and storage rollback also failed \
                             (data at {new_path} is inaccessible): meta={meta_err:?} \
                             rollback={rollback_err:?}"
                        )));
                    }
                }
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(KernelError::IOError(format!(
                    "sys_rename: metastore.rename_path: {meta_err:?}"
                )));
            }

            // CAS: drive backend rename (no-op for hash-addressed content) after metadata.
            if old_route.is_cas {
                let _ = self.vfs_router.rename_file(
                    &old_route.mount_point,
                    &old_route.backend_path,
                    &new_route.backend_path,
                );
            }
        }

        // 9. DCache: evict old + put new; evict children prefix for directories.
        // For PAS backends, content_id is the backend-relative path. After a rename
        // the disk file is at the new backend path, so we must update content_id in
        // the cached entry before inserting it at new_path — otherwise sys_read
        // fetches the stale old-path backend file (which no longer exists).
        if let Some(mut entry) = self.dcache.get_entry(old_path) {
            self.dcache.evict(old_path);
            // PAS only: update content_id to new backend path.
            // Use the route's authoritative is_cas flag rather than a
            // string-shape heuristic — a PAS file named like a BLAKE3 hex
            // digest would otherwise be incorrectly treated as CAS.
            if !old_route.is_cas {
                if let Some(ref cid) = entry.content_id.clone() {
                    if *cid == old_route.backend_path {
                        entry.content_id = Some(new_route.backend_path.clone());
                    }
                }
            }
            self.dcache.put(new_path, entry);
        }
        if is_directory {
            let prefix = format!("{}/", old_path.trim_end_matches('/'));
            self.dcache.evict_prefix(&prefix);
        }

        // 10. Release sorted locks
        release_locks(&self.lock_manager, lock1, lock2);

        // 11. OBSERVE-phase dispatch (§11 Phase 5): queue FileRename.
        // Convention (mirrors Python FileEvent for renames): primary
        // `path` is the source, `new_path` is the destination.
        let new_path_owned = new_path.to_string();
        self.dispatch_mutation(FileEventType::FileRename, old_path, ctx, |ev| {
            ev.new_path = Some(new_path_owned);
        });

        // Native POST hooks
        self.dispatch_native_post(&HookContext::Rename(RenameHookCtx {
            old_path: old_path.to_string(),
            new_path: new_path.to_string(),
            identity: HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            },
            is_directory,
        }));

        // Extract old metadata fields for Python post-hook dispatch.
        // Prefer metastore (old_meta) over dcache (old_entry) for accuracy.
        let (rename_old_etag, rename_old_size, rename_old_version, rename_old_modified_at_ms) =
            match (&old_meta, &old_entry) {
                (Some(m), _) => (
                    m.content_id.clone(),
                    Some(m.size),
                    Some(m.version),
                    m.modified_at_ms,
                ),
                (None, Some(e)) => (
                    e.content_id.clone(),
                    Some(e.size),
                    Some(e.version),
                    e.modified_at_ms,
                ),
                (None, None) => (None, None, None, None),
            };

        Ok(SysRenameResult {
            hit: true,
            success: true,
            post_hook_needed: self.rename_hook_count.load(Ordering::Relaxed) > 0,
            is_directory,
            old_content_id: rename_old_etag,
            old_size: rename_old_size,
            old_version: rename_old_version,
            old_modified_at_ms: rename_old_modified_at_ms,
        })
    }

    // ── sys_copy ───────────────────────────────────────────────────────

    /// Rust syscall: copy file (validate → route → VFS lock → backend copy → metastore → dcache).
    ///
    /// Three strategies:
    ///   1. Same mount, CAS backend → metadata-only copy (content deduplicated by hash).
    ///   2. Same mount, PAS backend → `backend.copy_file()`, fallback to read+write.
    ///   3. Cross mount → `read_content()` from src + `write_content()` to dst.
    ///
    /// Returns `hit=false` for directories, DT_PIPE/DT_STREAM, or when src not found.
    pub fn sys_copy(
        &self,
        src_path: &str,
        dst_path: &str,
        ctx: &OperationContext,
    ) -> Result<SysCopyResult, KernelError> {
        let miss = || {
            Ok(SysCopyResult {
                hit: false,
                post_hook_needed: false,
                dst_path: dst_path.to_string(),
                content_id: None,
                size: 0,
                version: 0,
            })
        };

        // 1. Validate both paths
        validate_path_fast(src_path)?;
        validate_path_fast(dst_path)?;

        // 1a. DT_LINK transparent follow on src — copy targets the
        // content the link points at, not the link's metadata entry.
        // (`cp -P` style "copy the link itself" is intentionally not
        // the default; sys_unlink and sys_rename keep operating on the
        // link entry directly.) dst is never a link follow target —
        // copying INTO an existing link is a write operation that goes
        // through sys_write's link follow path separately.
        let src_resolved = self.resolve_path_through_link(src_path)?;
        let src_path = src_resolved.as_ref();

        // 2. Route both (read access for src, write access for dst)
        let src_route = match self.vfs_router.route(src_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let dst_route = match self.vfs_router.route(dst_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. Get source metadata (dcache or metastore) — zone-relative keys
        let src_zone_path = Self::zone_key(&src_route.backend_path);
        let dst_zone_path = Self::zone_key(&dst_route.backend_path);
        let src_meta = match self.dcache.get_entry(src_path) {
            Some(e) => e,
            None => {
                match self
                    .with_metastore(&src_route.mount_point, |ms| {
                        ms.get(&src_zone_path).ok().flatten().map(|m| (&m).into())
                    })
                    .flatten()
                {
                    Some(e) => e,
                    None => return Err(KernelError::FileNotFound(src_path.to_string())),
                }
            }
        };

        // 4. Reject non-regular files (§12e: explicit error, not miss)
        if src_meta.entry_type != DT_REG {
            return Err(KernelError::InvalidPath(format!(
                "sys_copy: source is not a regular file (entry_type={}): {}",
                src_meta.entry_type, src_path
            )));
        }

        // 5. Check destination doesn't already exist (zone-relative key)
        let dst_exists = self
            .with_metastore(&dst_route.mount_point, |ms| {
                ms.exists(&dst_zone_path).unwrap_or(false)
            })
            .unwrap_or(false);
        if dst_exists {
            return Err(KernelError::IOError(format!(
                "sys_copy: destination already exists: {dst_path}"
            )));
        }

        // 6. VFS lock both paths (sorted, deadlock-free)
        let (first, second) = if src_path <= dst_path {
            (src_path, dst_path)
        } else {
            (dst_path, src_path)
        };
        let lock1 =
            self.lock_manager
                .blocking_acquire(first, LockMode::Write, self.vfs_lock_timeout_ms());
        let lock2 = if first != second {
            self.lock_manager
                .blocking_acquire(second, LockMode::Write, self.vfs_lock_timeout_ms())
        } else {
            0
        };

        let release_locks = |lm: &LockManager, h1: u64, h2: u64| {
            if h2 > 0 {
                lm.do_release(h2);
            }
            if h1 > 0 {
                lm.do_release(h1);
            }
        };

        if lock1 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // 7. Copy content (strategy depends on same-mount vs cross-mount)
        let same_mount = src_route.mount_point == dst_route.mount_point;

        let copy_result: Result<(String, u64), KernelError> = if same_mount {
            // Try server-side copy first (PAS backends)
            match self.vfs_router.copy_file(
                &src_route.mount_point,
                &src_route.backend_path,
                &dst_route.backend_path,
            ) {
                Some(Ok(wr)) => Ok((wr.content_id, wr.size)),
                Some(Err(crate::abc::object_store::StorageError::NotSupported(_))) | None => {
                    // No backend / operation not supported: fall back per addressing mode.
                    // For CAS: metadata-only copy is correct — same content_id, different path.
                    // For PAS: read+write to avoid creating a metadata alias pointing at
                    // source bytes that haven't been physically duplicated.
                    if src_route.is_cas {
                        let content_id = src_meta.content_id.clone().unwrap_or_default();
                        if !content_id.is_empty() {
                            Ok((content_id, src_meta.size))
                        } else {
                            self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx)
                        }
                    } else {
                        self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx)
                    }
                }
                Some(Err(e)) => {
                    // Real backend error (disk full, permission denied, etc.) — propagate.
                    Err(KernelError::BackendError(format!("sys_copy: {e:?}")))
                }
            }
        } else {
            // Cross-mount: read from src backend, write to dst backend
            self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx)
        };

        let (content_id, size) = match copy_result {
            Ok(r) => r,
            Err(e) => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(e);
            }
        };

        // 8. Build destination metadata and persist
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        let new_version = 1u32;
        let meta = self.build_metadata(
            &dst_zone_path,
            &dst_route.zone_id,
            DT_REG,
            size,
            Some(content_id.clone()),
            new_version,
            src_meta.mime_type.clone(),
            Some(now_ms),
            Some(now_ms),
        );
        // 9. Atomic commit — metastore (raft) first, dcache on success.
        // dcache uses the caller-visible dst_path; metastore uses the
        // zone-relative key from meta.path.
        let cache_entry: CachedEntry = (&meta).into();
        let put_result = self
            .with_metastore(&dst_route.mount_point, move |ms| {
                ms.put(&dst_zone_path, meta)
            })
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: no metastore for {}",
                    dst_route.mount_point
                ))
            });
        let put_result = match put_result {
            Ok(r) => r,
            Err(e) => {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(e);
            }
        };
        if let Err(e) = put_result {
            release_locks(&self.lock_manager, lock1, lock2);
            return Err(KernelError::IOError(format!(
                "sys_copy: metastore.put: {e:?}"
            )));
        }
        self.dcache.put(dst_path, cache_entry);

        // 10. Release VFS locks
        release_locks(&self.lock_manager, lock1, lock2);

        Ok(SysCopyResult {
            hit: true,
            post_hook_needed: self.copy_hook_count.load(Ordering::Relaxed) > 0,
            dst_path: dst_path.to_string(),
            content_id: Some(content_id),
            size,
            version: new_version,
        })
    }

    /// Internal: copy content via read_content + write_content (cross-mount or fallback).
    fn copy_via_read_write(
        &self,
        src_route: &crate::vfs_router::RustRouteResult,
        dst_route: &crate::vfs_router::RustRouteResult,
        src_meta: &CachedEntry,
        ctx: &OperationContext,
    ) -> Result<(String, u64), KernelError> {
        let content_id = match src_meta.content_id.as_deref().filter(|s| !s.is_empty()) {
            Some(id) => id,
            None => {
                return Err(KernelError::IOError(
                    "sys_copy: source has no content_id".into(),
                ))
            }
        };

        let content = self
            .vfs_router
            .read_content(&src_route.mount_point, content_id, ctx)
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to read source content at {}",
                    src_route.backend_path
                ))
            })?;

        let wr = self
            .vfs_router
            .write_content(
                &dst_route.mount_point,
                &content,
                &dst_route.backend_path,
                ctx,
                0,
            )
            .map_err(|e| KernelError::BackendError(format!("sys_copy: {e:?}")))?
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to write destination at {}",
                    dst_route.backend_path
                ))
            })?;

        Ok((wr.content_id, wr.size))
    }

    // ── sys_mkdir ──────────────────────────────────────────────────────

    /// Rust syscall: full mkdir (validate → route → backend → metastore → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Python only dispatches event notify + POST hooks when hit=true.
    /// `parents=true` creates parent directories. `exist_ok=true` ignores existing.
    pub fn sys_mkdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        parents: bool,
        exist_ok: bool,
    ) -> Result<SysMkdirResult, KernelError> {
        // 1. Validate
        validate_path_fast(path)?;

        // 2. Route (check write access)
        let route = self.vfs_router.route(path, &ctx.zone_id)?;

        // 3. Existence check: explicit entry OR implicit directory (children
        //    exist under this prefix). Eliminates Python's router.route() +
        //    metastore.get() + is_implicit_directory() pre-check (Crossing 3a).
        let explicit_exists = self
            .with_metastore(&route.mount_point, |ms| ms.exists(path).unwrap_or(false))
            .unwrap_or(false);
        let implicit_exists = !explicit_exists
            && self
                .with_metastore(&route.mount_point, |ms| {
                    ms.is_implicit_directory(path).unwrap_or(false)
                })
                .unwrap_or(false);
        if explicit_exists || implicit_exists {
            if !exist_ok && !parents {
                return Err(KernelError::IOError(format!(
                    "Directory already exists: {path}"
                )));
            }
            // Explicit entry: ensure parents and return (already materialized).
            // Implicit dir: fall through to create explicit DT_DIR entry.
            if explicit_exists {
                if parents {
                    self.ensure_parent_directories(path, ctx, &route.mount_point)?;
                }
                return Ok(SysMkdirResult {
                    hit: true,
                    post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
                });
            }
        }

        // 4. Backend mkdir (best-effort, PAS backends create physical dirs)
        let _ = self
            .vfs_router
            .mkdir(&route.mount_point, &route.backend_path, parents, true);

        // 5. Ensure parent directories
        if parents {
            self.ensure_parent_directories(path, ctx, &route.mount_point)?;
        }

        // 6. Create directory metadata in metastore (per-mount or global) — full path
        let meta = self.build_metadata(
            path,
            &route.zone_id,
            DT_DIR,
            0,
            None,
            1,
            Some("inode/directory".to_string()),
            None,
            None,
        );
        // 7. Atomic commit — metastore (raft) first, dcache on success.
        self.commit_metadata(path, &route.mount_point, meta)?;

        // 8. OBSERVE-phase dispatch (§11 Phase 5): queue DirCreate.
        // Only fires on the newly-created path — the early return at
        // step 3 (already-exists branch) does NOT dispatch because no
        // state actually changed. Parent directories created via
        // ensure_parent_directories don't get individual events; the
        // top-level mkdir event is enough for observers like
        // FileWatchRegistry to invalidate their dcache for the subtree.
        self.dispatch_mutation(FileEventType::DirCreate, path, ctx, |_ev| {});

        Ok(SysMkdirResult {
            hit: true,
            post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
        })
    }

    /// Walk up `path` creating missing parent directory metadata.
    ///
    /// R20.3: metastore now keyed by full paths, so we walk the global
    /// path directly — no separate zone_path traversal needed.
    fn ensure_parent_directories(
        &self,
        path: &str,
        ctx: &OperationContext,
        mount_point: &str,
    ) -> Result<(), KernelError> {
        // Walk up path from parent to root, collecting missing dirs.
        let mut cur = path;
        let mut to_create: Vec<String> = Vec::new();
        loop {
            match cur.rfind('/') {
                Some(0) | None => break,
                Some(pos) => {
                    cur = &path[..pos];
                    if cur.is_empty() || cur == contracts::VFS_ROOT {
                        break;
                    }
                    let exists = self
                        .with_metastore(mount_point, |ms| ms.exists(cur).unwrap_or(true))
                        .unwrap_or(true);
                    if !exists {
                        to_create.push(cur.to_string());
                    } else {
                        break; // Existing parent found, stop
                    }
                }
            }
        }

        // Create from shallowest to deepest
        for dir in to_create.into_iter().rev() {
            let dir_ref = dir.as_str();
            let meta = self.build_metadata(
                dir_ref,
                &ctx.zone_id,
                DT_DIR,
                0,
                None,
                1,
                Some("inode/directory".to_string()),
                None,
                None,
            );
            self.commit_metadata(dir_ref, mount_point, meta)?;
        }
        Ok(())
    }

    // ── sys_rmdir ──────────────────────────────────────────────────────

    /// Rust syscall: full rmdir (validate → route → children check → delete → dcache).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Returns `hit=false` for DT_MOUNT/DT_EXTERNAL_STORAGE → Python handles unmount.
    pub fn sys_rmdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysRmdirResult, KernelError> {
        let miss = || {
            Ok(SysRmdirResult {
                hit: false,
                post_hook_needed: false,
                children_deleted: 0,
            })
        };

        // 1. Validate
        validate_path_fast(path)?;

        // 2. Route (check write access)
        let route = self.vfs_router.route(path, &ctx.zone_id)?;

        // 3. Get metadata (per-mount or global) — full path
        let entry_type = self
            .with_metastore(&route.mount_point, |ms| {
                ms.get(path)
                    .ok()
                    .flatten()
                    .map(|m| m.entry_type)
                    .unwrap_or(DT_DIR)
            })
            .unwrap_or(DT_DIR);

        // DT_MOUNT(2) / DT_EXTERNAL_STORAGE(5) → Python handles unmount
        if entry_type == 2 || entry_type == 5 {
            return miss();
        }

        // 4. Check children (per-mount or global) — full-path prefix
        let mut children_deleted = 0;
        if let Some(result) = self.with_metastore(&route.mount_point, |ms| {
            let prefix = format!("{}/", path.trim_end_matches('/'));
            let children = ms.list(&prefix).unwrap_or_default();

            if !children.is_empty() {
                if !recursive {
                    return Err(KernelError::IOError(format!("Directory not empty: {path}")));
                }

                // 5. Recursive: batch delete all children
                let child_paths: Vec<String> = children.iter().map(|c| c.path.clone()).collect();
                Ok(ms.delete_batch(&child_paths).unwrap_or(0))
            } else {
                Ok(0)
            }
        }) {
            children_deleted = result?;
        }

        // 6. Backend rmdir (best-effort)
        let _ = self
            .vfs_router
            .rmdir(&route.mount_point, &route.backend_path, recursive);

        // 7. Atomic delete — metastore (raft) first, dcache evict on
        // success. The prefix evict for child entries follows the
        // delete because the children share fate with the directory's
        // metadata commit.
        self.commit_delete(path, &route.mount_point)?;
        let prefix = format!("{}/", path.trim_end_matches('/'));
        self.dcache.evict_prefix(&prefix);

        // 9. OBSERVE-phase dispatch (§11 Phase 5): queue DirDelete.
        // Like sys_mkdir, only the top-level rmdir event fires —
        // recursively-deleted children don't generate individual events
        // (observers needing per-child notifications can list the
        // directory before unlink themselves; the top-level event is
        // the cache-invalidation signal).
        self.dispatch_mutation(FileEventType::DirDelete, path, ctx, |_ev| {});

        Ok(SysRmdirResult {
            hit: true,
            post_hook_needed: self.rmdir_hook_count.load(Ordering::Relaxed) > 0,
            children_deleted,
        })
    }

    // ── Tier 2 convenience methods ────────────────────────────────────

    /// Fast access check: validate + route + dcache existence (~100ns).
    ///
    /// Returns true if file exists in dcache and path is routable.
    /// Does NOT check metastore (dcache authoritative for hot-path).
    pub fn access(&self, path: &str, zone_id: &str) -> bool {
        if validate_path_fast(path).is_err() {
            return false;
        }
        if self.vfs_router.route(path, zone_id).is_err() {
            return false;
        }
        self.dcache.contains(path)
    }

    // ── Internal batch functions (not Tier 1 syscalls) ────────────────

    /// Internal: batch write — loops sys_write logic for each item.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python `write_batch` method.
    /// Each item is (path, content). Returns Vec<SysWriteResult> with per-item results.
    /// Sorted VFS lock acquisition to avoid deadlocks.
    /// PRE-hooks are NOT dispatched here (caller handles batch pre-hooks).
    pub fn _write_batch(
        &self,
        items: &[(String, Vec<u8>)],
        ctx: &OperationContext,
    ) -> Result<Vec<SysWriteResult>, KernelError> {
        let mut results = Vec::with_capacity(items.len());

        // 1. Validate all paths (fail-fast)
        for (path, _) in items {
            validate_path_fast(path)?;
        }

        // 2. Route all paths (single lock acquisition on mount table via read lock)
        let mut routes = Vec::with_capacity(items.len());
        for (path, _) in items {
            let route = self.vfs_router.route(path, &ctx.zone_id).ok();
            routes.push(route);
        }

        // 3. Sorted VFS lock acquisition for all paths
        let mut lock_handles: Vec<u64> = vec![0; items.len()];
        {
            // Sort indices by path to avoid deadlock
            let mut indices: Vec<usize> = (0..items.len()).collect();
            indices.sort_by(|a, b| items[*a].0.cmp(&items[*b].0));

            for idx in indices {
                if routes[idx].is_some() {
                    lock_handles[idx] = self.lock_manager.blocking_acquire(
                        &items[idx].0,
                        LockMode::Write,
                        self.vfs_lock_timeout_ms(),
                    );
                }
            }
        }

        // 4. Write each item — collect metadata for batch put
        // Tuple: (mount_point, path, FileMetadata) for per-mount metastore support
        let mut batch_meta: Vec<(String, String, crate::meta_store::FileMetadata)> = Vec::new();

        for (i, ((path, content), route_opt)) in items.iter().zip(routes.iter()).enumerate() {
            let route = match route_opt {
                Some(r) => r,
                None => {
                    results.push(SysWriteResult {
                        hit: false,
                        content_id: None,
                        post_hook_needed: false,
                        version: 0,
                        size: 0,
                        is_new: false,
                        old_content_id: None,
                        old_size: None,
                        old_version: None,
                        old_modified_at_ms: None,
                    });
                    continue;
                }
            };

            // Lock timeout check
            if lock_handles[i] == 0 {
                results.push(SysWriteResult {
                    hit: false,
                    content_id: None,
                    post_hook_needed: false,
                    version: 0,
                    size: 0,
                    is_new: false,
                    old_content_id: None,
                    old_size: None,
                    old_version: None,
                    old_modified_at_ms: None,
                });
                continue;
            }

            // Backend write. ``sys_write_batch`` keeps per-item error
            // semantics: a failure only taints that item's result, not the
            // whole batch. We still surface the full error to the caller by
            // synthesising a backend-error result via ``hit=false`` so the
            // observer/post-hook path doesn't fire. The per-item error is
            // logged for observability but not hoisted to ``Result<..>``.
            // Backend write error (batch variant): collapse to None so the
            // per-item result surfaces as hit=false (observer + post-hook
            // path skipped). Caller inspects ``SysWriteResult.hit`` + retries.
            let write_result = self
                .vfs_router
                .write_content(&route.mount_point, content, &route.backend_path, ctx, 0)
                .unwrap_or_default();

            match write_result {
                Some(wr) => {
                    let batch_old_entry = self.dcache.get_entry(path);
                    let old_version = batch_old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                    let new_version = old_version + 1;

                    // Collect metadata for batch put (instead of N individual puts)
                    let meta = self.build_metadata(
                        path,
                        &route.zone_id,
                        DT_REG,
                        wr.size,
                        Some(wr.content_id.clone()),
                        new_version,
                        None,
                        None,
                        None,
                    );
                    // Defer dcache + metastore commit to step 4b so
                    // we can group raft proposes per mount and mark
                    // each result hit/miss based on the actual
                    // commit outcome rather than eagerly lying.
                    batch_meta.push((route.mount_point.clone(), path.to_string(), meta));

                    results.push(SysWriteResult {
                        hit: true,
                        content_id: Some(wr.content_id),
                        post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0
                            || self.write_batch_hook_count.load(Ordering::Relaxed) > 0,
                        version: new_version,
                        size: wr.size,
                        is_new: batch_old_entry.is_none(),
                        old_content_id: batch_old_entry.as_ref().and_then(|e| e.content_id.clone()),
                        old_size: batch_old_entry.as_ref().map(|e| e.size),
                        old_version: batch_old_entry.as_ref().map(|e| e.version),
                        old_modified_at_ms: batch_old_entry.as_ref().and_then(|e| e.modified_at_ms),
                    });
                }
                None => {
                    results.push(SysWriteResult {
                        hit: false,
                        content_id: None,
                        post_hook_needed: false,
                        version: 0,
                        size: 0,
                        is_new: false,
                        old_content_id: None,
                        old_size: None,
                        old_version: None,
                        old_modified_at_ms: None,
                    });
                }
            }
        }

        // 4b. Atomic per-item commit. Per-mount items go through
        // commit_metadata (raft propose, ms.put then dcache). Global
        // items (no per-mount metastore) collect into a batch put
        // since the global LocalMetaStore can do that as one redb
        // txn — but we still update dcache only after the txn lands.
        // Failures flip the corresponding result entry from
        // hit=true → hit=false so the caller learns which items
        // actually committed.
        if !batch_meta.is_empty() {
            let mut global_items: Vec<(String, crate::meta_store::FileMetadata)> = Vec::new();
            let mut global_idx: Vec<usize> = Vec::new();
            for (idx, (mp, path, meta)) in batch_meta.into_iter().enumerate() {
                let has_per_mount = self
                    .vfs_router
                    .get_canonical(&mp)
                    .map(|e| e.metastore.is_some())
                    .unwrap_or(false);
                if has_per_mount {
                    if let Err(_e) = self.commit_metadata(&path, &mp, meta) {
                        // Mark this batch entry as not-hit so the
                        // caller knows the propose didn't commit.
                        if let Some(r) = results.get_mut(idx) {
                            r.hit = false;
                        }
                    }
                } else {
                    global_items.push((path, meta));
                    global_idx.push(idx);
                }
            }
            if !global_items.is_empty() {
                let dcache_updates: Vec<(String, CachedEntry)> = global_items
                    .iter()
                    .map(|(p, m)| (p.clone(), m.into()))
                    .collect();
                let put_ok = self
                    .metastore
                    .read()
                    .as_ref()
                    .map(|ms| ms.put_batch(&global_items).is_ok())
                    .unwrap_or(false);
                if put_ok {
                    for (p, e) in dcache_updates {
                        self.dcache.put(&p, e);
                    }
                } else {
                    for idx in global_idx {
                        if let Some(r) = results.get_mut(idx) {
                            r.hit = false;
                        }
                    }
                }
            }
        }

        // 5. Release all VFS locks
        for handle in &lock_handles {
            if *handle > 0 {
                self.lock_manager.do_release(*handle);
            }
        }

        Ok(results)
    }

    /// Internal: batch read — parallel reads using rayon.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python `read_bulk` method.
    /// Returns Vec<SysReadResult> with per-path results.
    /// Safe because Kernel is Sync (DashMap + parking_lot).
    pub fn _read_batch(
        &self,
        paths: &[String],
        ctx: &OperationContext,
    ) -> Result<Vec<SysReadResult>, KernelError> {
        use rayon::prelude::*;

        let results: Vec<SysReadResult> = paths
            .par_iter()
            .map(|path| {
                self.sys_read(path, ctx).unwrap_or(SysReadResult {
                    data: None,
                    post_hook_needed: false,
                    content_id: None,
                    entry_type: 0,
                })
            })
            .collect();

        Ok(results)
    }

    /// Internal: batch delete — full Rust + batch metastore.
    ///
    /// NOT a syscall — prefixed with `_`. Called by Python batch delete.
    /// Returns Vec<SysUnlinkResult> with per-path results.
    /// Collects hit=true paths for a single metastore.delete_batch() call.
    pub fn _delete_batch(
        &self,
        paths: &[String],
        ctx: &OperationContext,
    ) -> Result<Vec<SysUnlinkResult>, KernelError> {
        let mut results = Vec::with_capacity(paths.len());

        for path in paths {
            match self.sys_unlink(path, ctx, false) {
                Ok(r) => results.push(r),
                Err(_) => results.push(SysUnlinkResult {
                    hit: false,
                    entry_type: 0,
                    post_hook_needed: false,
                    path: path.clone(),
                    content_id: None,
                    size: 0,
                }),
            }
        }

        Ok(results)
    }

    /// List immediate children of a directory path from dcache + metastore.
    ///
    /// When `is_admin` is false and `zone_id` is not ROOT_ZONE_ID, entries
    /// are filtered to only include those belonging to the caller's zone or
    /// the root zone (global namespace).
    ///
    /// Returns Vec of (child_path, entry_type) tuples.
    pub fn readdir(&self, parent_path: &str, zone_id: &str, is_admin: bool) -> Vec<(String, u8)> {
        if validate_path_fast(parent_path).is_err() {
            return Vec::new();
        }
        // Callers pass either "/local" or "/local/" — normalize the trailing
        // slash off before routing so prefix comparisons below don't produce
        // double slashes (which silently return no children).
        let normalized = if parent_path != "/" && parent_path.ends_with('/') {
            parent_path.trim_end_matches('/')
        } else {
            parent_path
        };
        let route = match self.vfs_router.route(normalized, zone_id) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };

        let global_prefix = if normalized == contracts::VFS_ROOT {
            contracts::VFS_ROOT.to_string()
        } else {
            format!("{}/", normalized)
        };

        let needs_zone_filter = !is_admin && zone_id != contracts::ROOT_ZONE_ID;

        // Merge dcache children with per-mount metastore list.
        // Track (entry_type, zone_id) so we can zone-filter at the end.
        let mut seen: std::collections::BTreeMap<String, (u8, Option<String>)> =
            std::collections::BTreeMap::new();
        let parent_for_join = if parent_path == contracts::VFS_ROOT {
            ""
        } else {
            parent_path.trim_end_matches('/')
        };
        for (child, etype, entry_zone) in self.dcache.list_children(&global_prefix) {
            let global = format!("{}/{}", parent_for_join, child);
            seen.insert(global, (etype, entry_zone));
        }

        if let Some(ms_children) =
            self.with_metastore(&route.mount_point, |ms| ms.list(&global_prefix).ok())
        {
            let parent_depth = global_prefix.matches('/').count();
            for meta in ms_children.into_iter().flatten() {
                // Direct children only: same depth as prefix + 1 segment.
                if meta.path.matches('/').count() != parent_depth {
                    continue;
                }
                if !meta.path.starts_with(&global_prefix) {
                    continue;
                }
                seen.entry(meta.path)
                    .or_insert((meta.entry_type, meta.zone_id));
            }
        }

        // Phase 3: Backend list_dir merge (all backend types uniformly).
        // CAS/S3/GCS return Err(NotSupported) → ignored.  Path-local
        // returns disk entries, external connectors return API results.
        // No ABC leak: kernel treats every backend the same.
        if let Ok(backend_entries) = self
            .vfs_router
            .list_dir(&route.mount_point, &route.backend_path)
        {
            for name in backend_entries {
                let is_dir = name.ends_with('/');
                let clean = name.trim_end_matches('/');
                if clean.is_empty() {
                    continue;
                }
                let etype = if is_dir { DT_DIR } else { DT_REG };
                let child_path = format!("{}/{}", parent_for_join, clean);
                seen.entry(child_path)
                    .or_insert((etype, Some(route.zone_id.clone())));
            }
        }

        if needs_zone_filter {
            seen.into_iter()
                .filter(|(_, (_, entry_zone))| {
                    let ez = entry_zone.as_deref().unwrap_or(contracts::ROOT_ZONE_ID);
                    ez == contracts::ROOT_ZONE_ID || ez == zone_id
                })
                .map(|(path, (etype, _))| (path, etype))
                .collect()
        } else {
            seen.into_iter()
                .map(|(path, (etype, _))| (path, etype))
                .collect()
        }
    }
}
