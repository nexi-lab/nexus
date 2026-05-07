//! File I/O syscalls -- `sys_read`, `sys_write`, `sys_stat`,
//! `sys_unlink`, `sys_rename`, `sys_copy`, `sys_mkdir`.
//!
//! `sys_rmdir` is kernel-internal (`pub(crate)`) -- only called from
//! `sys_unlink` DT_DIR branch. Removed from PyO3 surface in C21.

use std::sync::atomic::Ordering;

use crate::cache::{
    file_cache::FileCacheKey,
    index_cache::{ttl_for_backend, IndexCacheKey, IndexKind},
};
use crate::dispatch::ops_registry::{
    BackendKind, CatHandlerKind, FileType, FingerprintHandlerKind, OpHandler,
};
use crate::dispatch::{
    DeleteHookCtx, FileEventType, HookContext, HookIdentity, Permission, ReadHookCtx,
    RenameHookCtx, WriteHookCtx,
};
use crate::lock_manager::{LockManager, LockMode};
use crate::meta_store::{FileMetadata, DT_DIR, DT_MOUNT, DT_PIPE, DT_REG, DT_STREAM};

use super::{
    validate_path_fast,
    write_buffer::{DirtyWrite, DirtyWriteKey, DirtyWriteRoute, FlushSelection},
    FlushWriteBufferResult, Kernel, KernelError, OpMetadataResult, OperationContext, StatResult,
    SysCatResult, SysCopyResult, SysMkdirResult, SysReadResult, SysRenameResult, SysRmdirResult,
    SysUnlinkResult, SysWriteResult,
};

/// Per-request resolved state produced by Phase A of `sys_read` (batch path).
/// Kept file-private; callers only see the final `Vec<Result<…>>`.
/// `entry` is `None` when the metastore has no record yet; Phase B
/// falls back to `sys_read_one` which retries the backend directly.
///
/// Fields are consumed by Task 4 coalescing; allow dead_code until then.
#[allow(dead_code)]
struct ResolvedRead {
    route: crate::core::vfs_router::RouteResult,
    entry: Option<FileMetadata>,
}

/// Produce a cheap structural clone of a `KernelError` for fan-out in
/// `sys_read` batch path.  `KernelError` is `!Clone` (its `Route` variant holds a
/// non-Clone `RouteError`), so we collapse that variant to its `Debug` repr.
fn clone_kernel_err(e: &KernelError) -> KernelError {
    match e {
        KernelError::InvalidPath(s) => KernelError::InvalidPath(s.clone()),
        KernelError::FileNotFound(s) => KernelError::FileNotFound(s.clone()),
        KernelError::FileExists(s) => KernelError::FileExists(s.clone()),
        KernelError::IOError(s) => KernelError::IOError(s.clone()),
        KernelError::TrieError(s) => KernelError::TrieError(s.clone()),
        KernelError::PipeFull(s) => KernelError::PipeFull(s.clone()),
        KernelError::PipeEmpty(s) => KernelError::PipeEmpty(s.clone()),
        KernelError::PipeClosed(s) => KernelError::PipeClosed(s.clone()),
        KernelError::PipeExists(s) => KernelError::PipeExists(s.clone()),
        KernelError::PipeNotFound(s) => KernelError::PipeNotFound(s.clone()),
        KernelError::StreamFull(s) => KernelError::StreamFull(s.clone()),
        KernelError::StreamEmpty(s) => KernelError::StreamEmpty(s.clone()),
        KernelError::StreamClosed(s) => KernelError::StreamClosed(s.clone()),
        KernelError::StreamExists(s) => KernelError::StreamExists(s.clone()),
        KernelError::StreamNotFound(s) => KernelError::StreamNotFound(s.clone()),
        KernelError::WouldBlock(s) => KernelError::WouldBlock(s.clone()),
        KernelError::PermissionDenied(s) => KernelError::PermissionDenied(s.clone()),
        KernelError::BackendError(s) => KernelError::BackendError(s.clone()),
        KernelError::Federation(s) => KernelError::Federation(s.clone()),
        KernelError::Route(_) => {
            // RouteError isn't trivially cloneable. Collapse to a string form —
            // batch path only needs the per-item error visible downstream; the
            // structural detail is preserved in the Debug repr.
            KernelError::IOError(format!("{:?}", e))
        }
    }
}

/// Build a per-consumer `SysReadResult` from the lead request's shared result.
///
/// On success the caller's `offset` + `len` window is sliced out of the
/// lead's full-file bytes, so every consumer in a coalesced group gets
/// exactly the range it asked for without an extra backend round-trip.
///
/// `consumer_meta` is the consumer path's own `FileMetadata` snapshot.
/// We use it for the per-path `content_id`, `gen`, and `entry_type` so
/// CAS-deduplicated or metadata-only-copied paths sharing a content hash
/// still receive their *own* generation/content_id rather than the
/// lead's. The shared payload is the only thing borrowed from the lead.
fn clone_read_result(
    shared: &Result<SysReadResult, KernelError>,
    req: &crate::kernel::ReadRequest,
    consumer_meta: Option<&FileMetadata>,
) -> Result<SysReadResult, KernelError> {
    match shared {
        Err(e) => Err(clone_kernel_err(e)),
        Ok(src) => {
            let data = src.data.as_ref().map(|bytes| {
                let off = (req.offset as usize).min(bytes.len());
                let end = match req.len {
                    Some(l) => off.saturating_add(l as usize).min(bytes.len()),
                    None => bytes.len(),
                };
                bytes[off..end].to_vec()
            });
            // Per-consumer metadata when available; fall back to lead's
            // values only when the consumer's metadata is missing (cold
            // PAS-mount path read).
            let (content_id, gen, entry_type) = match consumer_meta {
                Some(m) => (
                    m.content_id.clone(),
                    m.gen,
                    if m.entry_type == 0 {
                        src.entry_type
                    } else {
                        m.entry_type
                    },
                ),
                None => (src.content_id.clone(), src.gen, src.entry_type),
            };
            Ok(SysReadResult {
                data,
                post_hook_needed: src.post_hook_needed,
                content_id,
                gen,
                entry_type,
                stream_next_offset: src.stream_next_offset,
            })
        }
    }
}

fn slice_read_result(
    r: Result<SysReadResult, KernelError>,
    req: &crate::kernel::ReadRequest,
) -> Result<SysReadResult, KernelError> {
    let mut r = r?;
    if let Some(bytes) = r.data.as_ref() {
        let off = (req.offset as usize).min(bytes.len());
        let end = match req.len {
            Some(l) => off.saturating_add(l as usize).min(bytes.len()),
            None => bytes.len(),
        };
        r.data = Some(bytes[off..end].to_vec());
    }
    Ok(r)
}

impl Kernel {
    /// Unified sys_read — accepts single or batch requests.
    ///
    /// Returns one `Result` per input request, preserving input order.
    /// `reqs.len() == 1` → fast path via `sys_read_single()`.
    /// `reqs.len() > 1` → Phase A (per-item auth + hooks) → Phase B (coalesce + rayon).
    pub fn sys_read(
        &self,
        reqs: &[crate::kernel::ReadRequest],
        ctx: &OperationContext,
    ) -> Vec<Result<SysReadResult, KernelError>> {
        if reqs.is_empty() {
            return Vec::new();
        }
        if reqs.len() == 1 {
            let req = &reqs[0];
            return vec![self.sys_read_single(&req.path, ctx, 1, req.timeout_ms, req.offset)];
        }
        self.sys_read_batch_impl(reqs, ctx)
    }

    /// Convenience wrapper for single-path reads.
    ///
    /// Kernel-internal callers (sys_cat, grep, etc.) and external callers
    /// (gRPC, PyO3, services) use this for the familiar single-path API.
    /// Will be removed once all callers migrate to the batch-capable `sys_read`.
    pub fn sys_read_one(
        &self,
        path: &str,
        ctx: &OperationContext,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError> {
        self.sys_read_single(path, ctx, 1, timeout_ms, offset)
    }

    pub fn op_metadata_for_path(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<OpMetadataResult, KernelError> {
        validate_path_fast(path)?;
        self.check_permission(path, Permission::Read, ctx)?;

        let route = self
            .vfs_router
            .route(path, &ctx.zone_id)
            .map_err(|_| KernelError::FileNotFound(path.to_string()))?;
        let stat = self.sys_stat(path, &ctx.zone_id);

        let mime_type = stat
            .as_ref()
            .and_then(|stat| (!stat.mime_type.is_empty()).then(|| stat.mime_type.clone()));
        let backend_name = route
            .backend
            .as_ref()
            .map(|backend| backend.name().to_string())
            .unwrap_or_default();
        let filetype = FileType::from_path_and_mime(path, mime_type.as_deref());
        let backend = BackendKind::from_backend_name(&backend_name);

        Ok(OpMetadataResult {
            filetype,
            backend,
            mime_type,
            backend_name,
        })
    }

    pub fn sys_cat(
        &self,
        path: &str,
        ctx: &OperationContext,
        strict_json: bool,
    ) -> Result<SysCatResult, KernelError> {
        let metadata = self.op_metadata_for_path(path, ctx)?;
        let read = self.sys_read_one(path, ctx, 5000, 0)?;
        let data = read
            .data
            .ok_or_else(|| KernelError::FileNotFound(path.to_string()))?;

        match self.resolve_op_handler("cat", &metadata.filetype, &metadata.backend) {
            Some(OpHandler::Cat(CatHandlerKind::JsonPretty)) => {
                match serde_json::from_slice::<serde_json::Value>(&data) {
                    Ok(value) => {
                        let mut pretty = serde_json::to_vec_pretty(&value)
                            .map_err(|e| KernelError::IOError(e.to_string()))?;
                        pretty.push(b'\n');
                        Ok(SysCatResult {
                            data: pretty,
                            handler: "cat/json_pretty".to_string(),
                            filetype: metadata.filetype,
                            backend: metadata.backend,
                        })
                    }
                    Err(e) if !strict_json => Ok(SysCatResult {
                        data,
                        handler: format!("cat/json_permissive_fallback:{e}"),
                        filetype: metadata.filetype,
                        backend: metadata.backend,
                    }),
                    Err(e) => Err(KernelError::IOError(format!(
                        "cat json parse failed for {path}: {e}"
                    ))),
                }
            }
            Some(OpHandler::Cat(CatHandlerKind::Default)) | None => Ok(SysCatResult {
                data,
                handler: "cat/default".to_string(),
                filetype: metadata.filetype,
                backend: metadata.backend,
            }),
            Some(_) => Ok(SysCatResult {
                data,
                handler: "cat/default".to_string(),
                filetype: metadata.filetype,
                backend: metadata.backend,
            }),
        }
    }

    pub fn backend_fingerprint(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<Option<String>, KernelError> {
        let metadata = self.op_metadata_for_path(path, ctx)?;
        match self.resolve_op_handler("fingerprint", &metadata.filetype, &metadata.backend) {
            Some(OpHandler::Fingerprint(FingerprintHandlerKind::S3)) => Ok(self
                .sys_stat(path, &ctx.zone_id)
                .and_then(|stat| stat.content_id)
                .filter(|content_id| !content_id.is_empty())),
            _ => Ok(None),
        }
    }

    /// Full single-read with auth + hooks + DT_LINK follow.
    fn sys_read_single(
        &self,
        path: &str,
        ctx: &OperationContext,
        max_link_hops: u8,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        // 1. Validate
        validate_path_fast(path)?;

        let _ = self.flush_due_write_buffer();

        // 1a. Xattr virtual path interception: /__xattr__/{key}/{path}
        if let Some(rest) = path.strip_prefix(contracts::XATTR_PATH_PREFIX) {
            return self.handle_xattr_read(rest);
        }

        // 1b. Trie-resolved virtual paths (§11 trie resolution)
        if self.trie.lookup(path).is_some() {
            return Err(not_found());
        }

        // 1c. Permission gate (§13) — BEFORE native hooks.
        self.check_permission(path, Permission::Read, ctx)?;

        // 1d. Native INTERCEPT PRE hooks (§11 native hooks) — audit etc.
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

        // Route → metastore → backend (shared logic)
        self.sys_read_after_auth(path, ctx, max_link_hops, timeout_ms, offset)
    }

    /// Phase B fan-out read — routing + metastore + backend, no auth/hooks.
    ///
    /// Auth was already done in Phase A. DT_LINK targets force re-auth
    /// via `sys_read_single`.
    fn sys_read_content_only(
        &self,
        path: &str,
        ctx: &OperationContext,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        validate_path_fast(path)?;

        if let Some(rest) = path.strip_prefix(contracts::XATTR_PATH_PREFIX) {
            return self.handle_xattr_read(rest);
        }

        if self.trie.lookup(path).is_some() {
            return Err(not_found());
        }

        // No permission check or hooks — Phase B fan-out, auth already done.
        self.sys_read_after_auth(path, ctx, 1, 5000, 0)
    }

    /// Shared read logic: route → metastore → DT_LINK follow → backend.
    ///
    /// Must be called after auth/hooks are resolved. DT_LINK targets
    /// re-enter via `sys_read_single` (forces auth on target).
    fn sys_read_after_auth(
        &self,
        path: &str,
        ctx: &OperationContext,
        max_link_hops: u8,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError> {
        let not_found = || KernelError::FileNotFound(path.to_string());

        // 2. Route (pure Rust LPM)
        let route = match self.vfs_router.route(path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return Err(not_found()),
        };

        // 3. MetaStore lookup. The metastore impl serves cache hits from
        // its own internal `DashMap` projection (see
        // `LocalMetaStore.cache` / `RemoteMetaStore.cache` /
        // `ZoneMetaStore.cache`), so this is the same hot-path cost as
        // the legacy `dcache.get_entry` lookup — relocated inside
        // `MetaStore::get` instead of a kernel-global side cache.
        let entry: FileMetadata = match self
            .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
            .flatten()
        {
            Some(meta) => meta,
            None => {
                if let Some(data) = self.write_buffer.get_dirty_bytes(path, &ctx.zone_id) {
                    return Ok(SysReadResult {
                        data: Some(data),
                        post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                        content_id: None,
                        gen: 1,
                        entry_type: DT_REG,
                        stream_next_offset: None,
                    });
                }
                // MetaStore miss → try backend directly (all backend types
                // uniformly).
                if let Some(data) = route
                    .backend
                    .as_ref()
                    .and_then(|b| b.read_content(&route.backend_path, ctx).ok())
                {
                    // §4057: emit prefetch hint after successful DT_REG read.
                    {
                        let size = data.len() as u32;
                        let sink = self.prefetch_sink.read().clone();
                        sink.on_read(path, offset, size);
                    }
                    return Ok(SysReadResult {
                        data: Some(data),
                        post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                        content_id: None,
                        gen: 0,
                        entry_type: DT_REG,
                        stream_next_offset: None,
                    });
                }
                return Err(not_found());
            }
        };

        // 3a. DT_LINK transparent follow (KERNEL-ARCHITECTURE.md §4.5).
        // DT_LINK target requires its own §13 permission gate +
        // §11 native PRE-read hook. Always enters via `sys_read_single`
        // so auth fires on the target path.
        if let Some(target) = Self::dt_link_target(path, &entry)? {
            if max_link_hops == 0 {
                return Err(KernelError::PermissionDenied(format!(
                    "DT_LINK chain rejected (ELOOP) at {path}"
                )));
            }
            return self.sys_read_single(target, ctx, max_link_hops - 1, timeout_ms, offset);
        }

        // DT_PIPE — Rust IPC registry: nowait pop, then optional blocking wait.
        if entry.entry_type == DT_PIPE {
            match self.pipe_read_nowait(path) {
                Ok(Some(data)) => {
                    return Ok(SysReadResult {
                        data: Some(data),
                        post_hook_needed: false,
                        content_id: None,
                        gen: 0,
                        entry_type: DT_PIPE,
                        stream_next_offset: None,
                    });
                }
                Ok(None) => {
                    if timeout_ms == 0 {
                        return Ok(SysReadResult {
                            data: None,
                            post_hook_needed: false,
                            content_id: None,
                            gen: 0,
                            entry_type: DT_PIPE,
                            stream_next_offset: None,
                        });
                    }
                    match self.pipe_read_blocking(path, timeout_ms) {
                        Ok(data) => {
                            return Ok(SysReadResult {
                                data: Some(data),
                                post_hook_needed: false,
                                content_id: None,
                                gen: 0,
                                entry_type: DT_PIPE,
                                stream_next_offset: None,
                            });
                        }
                        Err(KernelError::WouldBlock(_)) => {
                            return Ok(SysReadResult {
                                data: None,
                                post_hook_needed: false,
                                content_id: None,
                                gen: 0,
                                entry_type: DT_PIPE,
                                stream_next_offset: None,
                            });
                        }
                        Err(e) => return Err(e),
                    }
                }
                Err(e) => return Err(e),
            }
        }

        // DT_STREAM — Rust IPC registry: offset-based read with optional blocking.
        if entry.entry_type == DT_STREAM {
            match self.stream_read_at(path, offset as usize) {
                Ok(Some((data, next_offset))) => {
                    return Ok(SysReadResult {
                        data: Some(data),
                        post_hook_needed: false,
                        content_id: None,
                        gen: 0,
                        entry_type: DT_STREAM,
                        stream_next_offset: Some(next_offset),
                    });
                }
                Ok(None) => {
                    if timeout_ms == 0 {
                        return Ok(SysReadResult {
                            data: None,
                            post_hook_needed: false,
                            content_id: None,
                            gen: 0,
                            entry_type: DT_STREAM,
                            stream_next_offset: None,
                        });
                    }
                    match self.stream_read_at_blocking(path, offset as usize, timeout_ms) {
                        Ok((data, next_offset)) => {
                            return Ok(SysReadResult {
                                data: Some(data),
                                post_hook_needed: false,
                                content_id: None,
                                gen: 0,
                                entry_type: DT_STREAM,
                                stream_next_offset: Some(next_offset),
                            });
                        }
                        Err(KernelError::WouldBlock(_)) => {
                            return Ok(SysReadResult {
                                data: None,
                                post_hook_needed: false,
                                content_id: None,
                                gen: 0,
                                entry_type: DT_STREAM,
                                stream_next_offset: None,
                            });
                        }
                        Err(e) => return Err(e),
                    }
                }
                Err(e) => return Err(e),
            }
        }

        // Content identifier: CAS backends use content_id (hash). Path-addressed
        // backends derive their physical path from `path - mount_prefix`
        // inside the backend itself; the kernel always passes the content_id.
        if let Some(data) = self.write_buffer.get_dirty_bytes(path, &ctx.zone_id) {
            return Ok(SysReadResult {
                data: Some(data),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_id: entry.content_id.clone(),
                gen: entry.gen.saturating_add(1),
                entry_type: DT_REG,
                stream_next_offset: None,
            });
        }

        let content_id = match entry.content_id.as_deref().filter(|s| !s.is_empty()) {
            Some(id) => id,
            None => return Err(not_found()),
        };
        let expected_fingerprint = entry
            .content_id
            .as_ref()
            .filter(|id| !id.is_empty())
            .cloned()
            .or_else(|| (entry.version > 0).then(|| entry.version.to_string()));
        let file_key = FileCacheKey::new(&ctx.zone_id, path, "raw");

        // 4. VFS lock (blocking acquire — wrapper releases GIL before calling this)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Read, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return Err(KernelError::IOError(format!(
                "vfs read lock timeout: {path}"
            )));
        }

        if let Some(bytes) = self
            .file_cache
            .get(&file_key, expected_fingerprint.as_deref())
        {
            self.lock_manager.do_release(lock_handle);
            return Ok(SysReadResult {
                data: Some(bytes),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_id: expected_fingerprint.clone(),
                gen: entry.gen,
                entry_type: DT_REG,
                stream_next_offset: None,
            });
        }
        let _file_fill_guard = self.file_cache.lock(&file_key);
        if let Some(bytes) = self
            .file_cache
            .get(&file_key, expected_fingerprint.as_deref())
        {
            self.lock_manager.do_release(lock_handle);
            return Ok(SysReadResult {
                data: Some(bytes),
                post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                content_id: expected_fingerprint.clone(),
                gen: entry.gen,
                entry_type: DT_REG,
                stream_next_offset: None,
            });
        }

        // 5. Backend read (Rust-native ObjectStore)
        let content = route
            .backend
            .as_ref()
            .and_then(|b| b.read_content(content_id, ctx).ok());

        // 6. Release VFS lock (always, even on miss)
        self.lock_manager.do_release(lock_handle);

        // 7. Return result
        match content {
            Some(data) => {
                self.file_cache
                    .put(file_key, data.clone(), expected_fingerprint.clone(), None);
                // §4057: emit prefetch hint after successful DT_REG read.
                {
                    let size = data.len() as u32;
                    let sink = self.prefetch_sink.read().clone();
                    sink.on_read(path, offset, size);
                }
                Ok(SysReadResult {
                    data: Some(data),
                    post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                    content_id: entry.content_id.clone(),
                    gen: entry.gen,
                    entry_type: DT_REG,
                    stream_next_offset: None,
                })
            }
            // Local backend miss + metadata exists → federation path:
            // try the origin encoded in backend_name. Otherwise it's a
            // genuine miss.
            None => self.try_remote_fetch(path, &entry, &route, ctx),
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
        entry: &FileMetadata,
        route: &crate::vfs_router::RouteResult,
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
        // the pooled tonic Channel from ``peer_client``. Avoid one-shot
        // ``new_current_thread()`` per call so the runtime does not
        // linger when the future has not finished draining.
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
        // ``peer_client`` is ``RwLock<Arc<dyn PeerBlobClient>>``;
        // ``peer_client_arc()`` clones the Arc out from under the read
        // lock so the actual fetch happens lock-free.
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
            let _ = route
                .backend
                .as_ref()
                .map(|b| b.write_content(&data, cache_key, ctx, 0));
        }
        let expected_fingerprint = entry
            .content_id
            .as_ref()
            .filter(|id| !id.is_empty())
            .cloned()
            .or_else(|| (entry.version > 0).then(|| entry.version.to_string()));
        if expected_fingerprint.is_some() {
            self.file_cache.put(
                FileCacheKey::new(&ctx.zone_id, path, "raw"),
                data.clone(),
                expected_fingerprint.clone(),
                None,
            );
        }

        // §4057: emit prefetch hint after successful remote DT_REG fetch.
        // Remote fetch returns the entire blob, not an offset slice, so
        // we report offset 0.
        {
            let size = data.len() as u32;
            let sink = self.prefetch_sink.read().clone();
            sink.on_read(path, 0, size);
        }
        Ok(SysReadResult {
            data: Some(data),
            post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
            content_id: entry.content_id.clone(),
            gen: entry.gen,
            entry_type: DT_REG,
            stream_next_offset: None,
        })
    }
}

struct WriteCommitInput<'a> {
    path: &'a str,
    ctx: &'a OperationContext,
    content: &'a [u8],
    offset: u64,
    route: &'a crate::vfs_router::RouteResult,
    old_metadata: Option<Option<FileMetadata>>,
}

impl Kernel {
    // ── sys_write ──────────────────────────────────────────────────────

    /// Unified sys_write — accepts single or batch requests.
    ///
    /// Returns one `Result` per input request, preserving input order.
    /// `reqs.len() == 1` → fast path via `sys_write_single()`.
    /// `reqs.len() > 1` → batch write with sorted VFS lock acquisition.
    pub fn sys_write(
        &self,
        reqs: &[crate::kernel::WriteRequest],
        ctx: &OperationContext,
    ) -> Vec<Result<SysWriteResult, KernelError>> {
        if reqs.is_empty() {
            return Vec::new();
        }
        if reqs.len() == 1 {
            let req = &reqs[0];
            return vec![self.sys_write_single(&req.path, ctx, &req.content, req.offset, 1)];
        }
        self.sys_write_batch_impl(reqs, ctx)
    }

    /// Convenience wrapper for single-path writes.
    ///
    /// Kernel-internal callers (tests, services) and external callers
    /// (gRPC, PyO3) use this for the familiar single-path API.
    pub fn sys_write_one(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError> {
        self.sys_write_single(path, ctx, content, offset, 1)
    }

    fn sys_write_single(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
        max_link_hops: u8,
    ) -> Result<SysWriteResult, KernelError> {
        let miss = || {
            Ok(SysWriteResult {
                hit: false,
                content_id: None,
                post_hook_needed: false,
                version: 0,
                gen: 0,
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

        let _ = self.flush_due_write_buffer();

        // 1a. Xattr virtual path interception: /__xattr__/{key}/{path}
        // Short-circuits to metastore set_file_metadata without hooks/routing.
        if let Some(rest) = path.strip_prefix(contracts::XATTR_PATH_PREFIX) {
            return self.handle_xattr_write(rest, content);
        }

        // 1b. Trie-resolved virtual paths (§11 trie resolution)
        if self.trie.lookup(path).is_some() {
            return miss();
        }

        // 1c. Permission gate (§13) — BEFORE native hooks.
        self.check_permission(path, Permission::Write, ctx)?;

        // 1d. Native INTERCEPT PRE hooks (§11 native hooks).
        let needs_content_for_hook = self.has_mutating_hook_match(path);
        let hook_content = if needs_content_for_hook {
            content.to_vec()
        } else {
            Vec::new()
        };
        let replacement =
            self.dispatch_native_pre_with_replacement(&HookContext::Write(WriteHookCtx {
                path: path.to_string(),
                identity: HookIdentity {
                    user_id: ctx.user_id.clone(),
                    zone_id: ctx.zone_id.clone(),
                    agent_id: ctx.agent_id.clone().unwrap_or_default(),
                    is_admin: ctx.is_admin,
                },
                content: hook_content,
                is_new_file: false,
                content_id: None,
                new_version: 0,
                size_bytes: None,
            }))?;
        let effective_content: &[u8] = replacement.as_deref().unwrap_or(content);

        // 2. Route (check write access)
        let route = match self.vfs_router.route(path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };

        // 3. Load entry (dcache + metastore fallback) — needed both for
        //    DT_LINK transparent follow (cold-cache + cross-mount safe)
        //    and the existing DT_PIPE / DT_STREAM IPC registry dispatch.
        //    The metastore fallback is what fixes the cold-cache DT_LINK
        //    bug; for DT_PIPE / DT_STREAM it's a no-op in practice (those
        //    entries normally only land in dcache via the IPC registry
        //    setattr path) but is harmless on the rare cross-call cold
        //    path.
        let mut entry = self
            .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
            .flatten();

        // 3a. DT_LINK transparent follow (KERNEL-ARCHITECTURE.md §4.5).
        // Recursive call with `max_link_hops=0` rejects chained links via
        // this same branch.
        if let Some(e) = &entry {
            if let Some(target) = Self::dt_link_target(path, e)? {
                if max_link_hops == 0 {
                    return Err(KernelError::PermissionDenied(format!(
                        "DT_LINK chain rejected (ELOOP) at {path}"
                    )));
                }
                return self.sys_write_single(
                    target,
                    ctx,
                    effective_content,
                    offset,
                    max_link_hops - 1,
                );
            }
        }

        // 3b. Auto-create DT_REG on first write (offset==0).
        //     Mirrors POSIX open(O_CREAT|O_WRONLY) + write(2) semantics:
        //     the kernel ensures the inode exists before writing content.
        //     DT_PIPE / DT_STREAM are always pre-created via sys_setattr,
        //     so they're never None here. Partial writes (offset>0) to a
        //     non-existent file return Err(FileNotFound) in step 5.
        if entry.is_none() && offset == 0 {
            self.setattr_update(path, &ctx.zone_id, None, None, None, None, None, None)?;
            // Re-fetch the freshly created entry for step 4+.
            entry = self.metastore_get(path)?;
        }

        // 3c. DT_PIPE / DT_STREAM: try Rust IPC registry
        if let Some(entry) = entry {
            if entry.entry_type == DT_PIPE {
                if let Some(buf) = self.pipe_manager.get(path) {
                    match buf.push(effective_content) {
                        Ok(n) => {
                            // POST hooks fire on the IPC short-circuit
                            // path the same as for DT_REG. Hook
                            // self-exclusion (e.g. AuditHook on
                            // /__sys__/) prevents recursion when an
                            // observer's own sys_write would re-enter.
                            self.dispatch_native_post(&HookContext::Write(WriteHookCtx {
                                path: path.to_string(),
                                identity: HookIdentity {
                                    user_id: ctx.user_id.clone(),
                                    zone_id: ctx.zone_id.clone(),
                                    agent_id: ctx.agent_id.clone().unwrap_or_default(),
                                    is_admin: ctx.is_admin,
                                },
                                content: effective_content.to_vec(),
                                is_new_file: false,
                                content_id: None,
                                new_version: 0,
                                size_bytes: Some(n as u64),
                            }));
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0,
                                version: 0,
                                gen: 0,
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
                    match buf.push(effective_content) {
                        Ok(offset) => {
                            // POST hooks fire on the IPC short-circuit
                            // path the same as for DT_REG. Hook
                            // self-exclusion (e.g. AuditHook on
                            // /__sys__/) prevents recursion when an
                            // observer's own sys_write would re-enter.
                            self.dispatch_native_post(&HookContext::Write(WriteHookCtx {
                                path: path.to_string(),
                                identity: HookIdentity {
                                    user_id: ctx.user_id.clone(),
                                    zone_id: ctx.zone_id.clone(),
                                    agent_id: ctx.agent_id.clone().unwrap_or_default(),
                                    is_admin: ctx.is_admin,
                                },
                                content: effective_content.to_vec(),
                                is_new_file: false,
                                content_id: None,
                                new_version: 0,
                                size_bytes: Some(offset as u64),
                            }));
                            return Ok(SysWriteResult {
                                hit: true,
                                content_id: None,
                                post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0,
                                version: 0,
                                gen: 0,
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

        let policy = self.write_buffer.policy_for(path);
        if policy.enabled() && route.backend.is_some() {
            let lock_handle = self.lock_manager.blocking_acquire(
                path,
                LockMode::Write,
                self.vfs_lock_timeout_ms(),
            );
            if lock_handle == 0 {
                return miss();
            }

            let old_entry = entry.clone();
            let dirty_key = DirtyWriteKey::new(path, &ctx.zone_id);
            let base_content =
                match if offset == 0 || self.write_buffer.contains_dirty_key(&dirty_key) {
                    Ok(Vec::new())
                } else {
                    self.load_buffered_write_base(path, ctx, &route, old_entry.as_ref())
                } {
                    Ok(content) => content,
                    Err(err) => {
                        self.lock_manager.do_release(lock_handle);
                        return Err(err);
                    }
                };
            let now_ms = Self::now_ms_u64();
            let merge_result = self.write_buffer.merge_write_with_base_and_context(
                dirty_key,
                DirtyWriteRoute::from_route(path, &route),
                ctx.clone(),
                old_entry.clone(),
                base_content,
                effective_content,
                offset,
                policy.clone(),
                now_ms,
            );

            self.lock_manager.do_release(lock_handle);

            let size = merge_result.map_err(KernelError::IOError)?;
            if policy.byte_budget > 0 && size >= policy.byte_budget {
                self.flush_write_buffer(Some(path), Some(&ctx.zone_id))?;
            }

            return Ok(SysWriteResult {
                hit: true,
                content_id: old_entry.as_ref().and_then(|e| e.content_id.clone()),
                post_hook_needed: false,
                version: old_entry.as_ref().map(|e| e.version + 1).unwrap_or(1),
                gen: old_entry
                    .as_ref()
                    .map(|e| e.gen.saturating_add(1))
                    .unwrap_or(1),
                size: size as u64,
                is_new: old_entry.is_none(),
                old_content_id: old_entry.as_ref().and_then(|e| e.content_id.clone()),
                old_size: old_entry.as_ref().map(|e| e.size),
                old_version: old_entry.as_ref().map(|e| e.version),
                old_modified_at_ms: old_entry.as_ref().and_then(|e| e.modified_at_ms),
            });
        }

        // 4. VFS lock (blocking write lock)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return miss();
        }

        let result = self.commit_write_through(WriteCommitInput {
            path,
            ctx,
            content: effective_content,
            offset,
            route: &route,
            old_metadata: None,
        });

        self.lock_manager.do_release(lock_handle);

        result
    }

    fn load_buffered_write_base(
        &self,
        path: &str,
        ctx: &OperationContext,
        route: &crate::vfs_router::RouteResult,
        old_entry: Option<&FileMetadata>,
    ) -> Result<Vec<u8>, KernelError> {
        let backend = route
            .backend
            .as_ref()
            .ok_or_else(|| KernelError::FileNotFound(path.to_string()))?;
        if let Some(content_id) = old_entry
            .and_then(|e| e.content_id.as_deref())
            .filter(|content_id| !content_id.is_empty())
        {
            return backend.read_content(content_id, ctx).map_err(|err| {
                KernelError::BackendError(format!(
                    "buffered partial base read({path}, {content_id}): {err:?}"
                ))
            });
        }

        backend
            .read_content(&route.backend_path, ctx)
            .map_err(|err| match err {
                crate::abc::object_store::StorageError::NotFound(_) => {
                    KernelError::FileNotFound(path.to_string())
                }
                other => KernelError::BackendError(format!(
                    "buffered partial direct base read({path}, {}): {other:?}",
                    route.backend_path
                )),
            })
    }

    fn commit_old_metadata(&self, input: &WriteCommitInput<'_>) -> Option<FileMetadata> {
        match &input.old_metadata {
            Some(old_metadata) => old_metadata.clone(),
            None => self
                .with_metastore_route(input.route, |ms| ms.get(input.path).ok().flatten())
                .flatten(),
        }
    }

    fn commit_write_through(
        &self,
        input: WriteCommitInput<'_>,
    ) -> Result<SysWriteResult, KernelError> {
        let miss = || {
            Ok(SysWriteResult {
                hit: false,
                content_id: None,
                post_hook_needed: false,
                version: 0,
                gen: 0,
                size: 0,
                is_new: false,
                old_content_id: None,
                old_size: None,
                old_version: None,
                old_modified_at_ms: None,
            })
        };

        // 5. Backend write (Rust-native ObjectStore).
        //    Pass backend_path as content_id for PAS; for CAS at offset=0
        //    content_id is ignored, but for offset>0 we need the OLD
        //    content hash so CASEngine::write_partial can splice against
        //    it. Look up old entry (dcache → metastore fallback).
        let effective_content_id = if input.offset == 0 {
            input.route.backend_path.clone()
        } else {
            // Partial write path: use the CAS hash from the existing inode.
            // PathLocalBackend ignores content_id when offset>0 (uses the
            // on-disk file instead), so this value is only consulted by
            // CasLocalBackend.
            let old_entry = self.commit_old_metadata(&input);
            match old_entry {
                Some(e) => e.content_id.unwrap_or_default(),
                None => {
                    // Partial write to non-existent file (step 3b
                    // already rejects offset==0 miss; this handles
                    // the offset>0 race where the entry disappears
                    // between step 3 and step 5).
                    self.lock_manager.do_release(lock_handle);
                    return Err(KernelError::FileNotFound(path.to_string()));
                }
            }
        };
        let write_result = match input.route.backend.as_ref() {
            Some(backend) => {
                match backend.write_content(
                    input.content,
                    &effective_content_id,
                    input.ctx,
                    input.offset,
                ) {
                    Ok(wr) => Some(wr),
                    Err(storage_err) => {
                        // Storage/backend-level failure (connector wrapper raised a
                        // BackendError, disk full, permission denied, etc.). Surface
                        // the error to Python so callers can react (F2 C4 / Issue
                        // #3765 Cat-7 regression — previous code silently swallowed
                        // this via ``.ok()``).
                        return Err(KernelError::BackendError(format!("{storage_err:?}")));
                    }
                }
            }
            // Mount has no Rust backend (Python-side connector) — caller treats
            // as a hit=false miss.
            None => None,
        };

        // 6. After write -> build metadata + metastore.put + dcache update
        let result = match write_result {
            Some(wr) => {
                // Snapshot old state for OBSERVE event payload + Python
                // post-hook dispatch (is_new, old_content_id, old_size, etc.).
                // DCache → metastore fallback ensures accuracy even on cold
                // dcache (matches the authority that Python metadata.get()
                // had before this crossing elimination).
                let old_entry = self.commit_old_metadata(&input);
                let old_version = old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                let old_gen = old_entry.as_ref().map(|e| e.gen).unwrap_or(0);
                let old_content_id = old_entry.as_ref().and_then(|e| e.content_id.clone());
                let new_version = old_version + 1;
                let new_gen = old_gen.saturating_add(1);

                // Build FileMetadata and persist via metastore (per-mount or global)
                let now_ms = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0);
                let created_at_ms = old_entry
                    .as_ref()
                    .and_then(|e| e.created_at_ms)
                    .or(Some(now_ms));
                // Always pass the full global path. Per-mount
                // ZoneMetaStore translates at its boundary; the global
                // fallback stores full paths directly.
                let meta = self.build_metadata(
                    input.path,
                    &input.route.zone_id,
                    DT_REG,
                    wr.size,
                    Some(wr.content_id.clone()),
                    new_gen,
                    new_version,
                    None,
                    created_at_ms,
                    Some(now_ms),
                );
                // Atomic commit — metastore (raft) write. Hot path: bypass
                // the routing wrapper and dispatch through the trait via
                // route.metastore (already resolved above).
                let put_res = self
                    .with_metastore_route(input.route, |ms| ms.put(input.path, meta))
                    .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
                    .and_then(|r| {
                        r.map_err(|e| {
                            KernelError::IOError(format!("metastore_put({}): {e:?}", input.path))
                        })
                    });
                put_res?;

                // Snapshot old_entry fields for the result struct before
                // dispatch_mutation moves old_content_id into its closure.
                let result_is_new = old_entry.is_none();
                let result_old_etag = old_content_id.clone();
                let result_old_size = old_entry.as_ref().map(|e| e.size);
                let result_old_version = old_entry.as_ref().map(|e| e.version);
                let result_old_modified_at_ms = old_entry.as_ref().and_then(|e| e.modified_at_ms);

                // OBSERVE-phase dispatch (§11 OBSERVE): queue FileWrite to
                // the kernel observer ThreadPool. Returns immediately —
                // observer callbacks run off the syscall hot path.
                let content_id = wr.content_id.clone();
                let size = wr.size;
                self.dispatch_mutation(FileEventType::FileWrite, input.path, input.ctx, |ev| {
                    ev.size = Some(size);
                    ev.content_id = Some(content_id);
                    ev.version = Some(new_version);
                    ev.gen = Some(new_gen);
                    ev.is_new = old_version == 0;
                    ev.old_content_id = old_content_id;
                });

                // Native POST hooks (fire-and-forget — AuditHook sends to channel
                // in ~100 ns; no content clone on post path).
                self.dispatch_native_post(&HookContext::Write(WriteHookCtx {
                    path: input.path.to_string(),
                    identity: HookIdentity {
                        user_id: input.ctx.user_id.clone(),
                        zone_id: input.ctx.zone_id.clone(),
                        agent_id: input.ctx.agent_id.clone().unwrap_or_default(),
                        is_admin: input.ctx.is_admin,
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
                    gen: new_gen,
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
        if let Ok(result) = &result {
            if result.hit {
                self.file_cache
                    .invalidate_path(&input.ctx.zone_id, input.path, "raw");
                self.index_cache
                    .invalidate_parent_listing(&input.ctx.zone_id, input.path);
            }
        }

        result
    }

    pub fn flush_due_write_buffer(&self) -> Result<FlushWriteBufferResult, KernelError> {
        let now = Self::now_ms_u64();
        let due = self.write_buffer.due_dirty(now);
        let mut total = FlushWriteBufferResult::default();
        for dirty in due {
            Self::merge_flush_result(&mut total, self.flush_dirty_entry(dirty, false));
        }
        Self::finish_flush_result(total)
    }

    pub fn flush_write_buffer(
        &self,
        path: Option<&str>,
        zone_id: Option<&str>,
    ) -> Result<FlushWriteBufferResult, KernelError> {
        let selection = FlushSelection {
            path: path.map(str::to_string),
            zone_id: zone_id.map(str::to_string),
        };
        let mut result = FlushWriteBufferResult::default();
        for dirty in self.write_buffer.selected_dirty(&selection) {
            Self::merge_flush_result(&mut result, self.flush_dirty_entry(dirty, false));
        }
        Self::finish_flush_result(result)
    }

    pub(crate) fn flush_write_buffer_locked_path(
        &self,
        path: &str,
        zone_id: &str,
    ) -> Result<FlushWriteBufferResult, KernelError> {
        let mut result = FlushWriteBufferResult::default();
        let key = DirtyWriteKey::new(path, zone_id);
        if let Some(dirty) = self.write_buffer.get_dirty(&key) {
            Self::merge_flush_result(&mut result, self.flush_dirty_entry(dirty, true));
        }
        Self::finish_flush_result(result)
    }

    pub(crate) fn flush_write_buffer_locked_prefix(
        &self,
        path: &str,
        zone_id: &str,
    ) -> Result<FlushWriteBufferResult, KernelError> {
        // Caller must hold a write lock on `path`. LockManager write locks
        // conflict with descendants, so an ancestor directory lock fences
        // child writes while this prefix drain is in progress.
        let selection = FlushSelection {
            path: Some(path.to_string()),
            zone_id: Some(zone_id.to_string()),
        };
        let mut result = FlushWriteBufferResult::default();
        for dirty in self.write_buffer.selected_dirty(&selection) {
            Self::merge_flush_result(&mut result, self.flush_dirty_entry(dirty, true));
        }
        Self::finish_flush_result(result)
    }

    fn flush_dirty_entry(
        &self,
        dirty: DirtyWrite,
        lock_already_held: bool,
    ) -> FlushWriteBufferResult {
        let mut result = FlushWriteBufferResult::default();

        let mut lock_handle = 0;
        if !lock_already_held {
            lock_handle = self.lock_manager.blocking_acquire(
                &dirty.key.path,
                LockMode::Write,
                self.vfs_lock_timeout_ms(),
            );
            if lock_handle == 0 {
                result.failed += 1;
                result
                    .errors
                    .push(format!("vfs write lock timeout: {}", dirty.key.path));
                return result;
            }
        }

        if !self.write_buffer.claim_dirty_generation(&dirty) {
            if lock_handle > 0 {
                self.lock_manager.do_release(lock_handle);
            }
            return result;
        }

        let route = dirty.route.to_route_result();
        let commit = self.commit_write_through(WriteCommitInput {
            path: &dirty.key.path,
            ctx: &dirty.context,
            content: &dirty.content,
            offset: 0,
            route: &route,
            old_metadata: Some(dirty.old_metadata.clone()),
        });

        match commit {
            Ok(write) if write.hit => {
                if self.write_buffer.remove_if_generation_matches(&dirty) {
                    result.flushed += 1;
                }
            }
            Ok(_) => {
                result.failed += 1;
                result
                    .errors
                    .push(format!("{}: buffered write commit missed", dirty.key.path));
                self.write_buffer.unclaim_dirty_generation(&dirty);
            }
            Err(err) => {
                result.failed += 1;
                result.errors.push(format!("{}: {err:?}", dirty.key.path));
                self.write_buffer.unclaim_dirty_generation(&dirty);
            }
        }
        if lock_handle > 0 {
            self.lock_manager.do_release(lock_handle);
        }
        result
    }

    fn merge_flush_result(total: &mut FlushWriteBufferResult, one: FlushWriteBufferResult) {
        total.flushed += one.flushed;
        total.failed += one.failed;
        total.errors.extend(one.errors);
    }

    fn finish_flush_result(
        result: FlushWriteBufferResult,
    ) -> Result<FlushWriteBufferResult, KernelError> {
        if result.failed > 0 {
            return Err(KernelError::IOError(result.errors.join("; ")));
        }
        Ok(result)
    }

    fn flush_dirty_path_if_present(&self, path: &str, zone_id: &str) -> Result<(), KernelError> {
        let key = DirtyWriteKey::new(path, zone_id);
        if self.write_buffer.contains_dirty_key(&key) {
            self.flush_write_buffer(Some(path), Some(zone_id))?;
        }
        Ok(())
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
        if self.flush_dirty_path_if_present(path, zone_id).is_err() {
            return None;
        }

        // 3.5. Mount-point synthesis (federation cross-zone mount).
        // When ``backend_path`` is empty the routed path IS the mount
        // point itself. For federation mounts (where the parent zone's
        // canonical-key prefix differs from the routed ``zone_id`` — i.e.
        // ``target_zone_id`` was set on the entry), there is no "/" entry
        // in the target zone's metastore (the DT_MOUNT row lives in the
        // *parent* zone's metastore, written by ``dlc.mount``). The
        // VFSRouter is the SSOT for "this path is a mount point", so
        // synthesise the DT_MOUNT result directly from routing structure
        // — same pattern ``sys_mkdir`` uses ("the mount IS the
        // directory"). Avoids a metastore round-trip and removes the
        // need for federation to seed a dcache row at the mount root.
        let (parent_zone, _user_mp) =
            crate::vfs_router::extract_zone_from_canonical(&route.mount_point);
        if route.backend_path.is_empty() && parent_zone != route.zone_id {
            return Some(StatResult {
                path: path.to_string(),
                size: 4096,
                content_id: None,
                mime_type: "inode/directory".to_string(),
                is_directory: true,
                entry_type: DT_MOUNT,
                mode: 0o755,
                version: 1,
                gen: 0,
                zone_id: Some(route.zone_id.clone()),
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                lock: None,
                link_target: None,
                owner_id: None,
            });
        }

        // 4. MetaStore lookup. The metastore impl serves cache hits from
        //    its own internal cache (LocalMetaStore.cache /
        //    RemoteMetaStore.cache / ZoneMetaStore.cache), so this is the
        //    same hot-path cost as the legacy `dcache.get_entry` lookup
        //    — relocated inside MetaStore::get instead of a kernel-global
        //    side cache.
        //    On miss, check implicit directory (path has children in
        //    metastore but no explicit entry — e.g. /docs/ when
        //    /docs/readme.md exists). Returns synthetic DT_DIR.
        let entry: FileMetadata = match self
            .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
            .flatten()
        {
            Some(meta) => meta,
            None => {
                // Implicit directory: children exist under this prefix
                // but no explicit entry. Eliminates Python fallback to
                // _check_is_directory() (Crossing 3a).
                let is_implicit = self
                    .with_metastore_route(&route, |ms| {
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
                        gen: 0,
                        zone_id: Some(route.zone_id.clone()),
                        created_at_ms: None,
                        modified_at_ms: None,
                        last_writer_address: None,
                        lock: None,
                        link_target: None,
                        owner_id: None,
                    });
                }
                return None;
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
            gen: entry.gen,
            zone_id: entry.zone_id,
            created_at_ms: entry.created_at_ms,
            modified_at_ms: entry.modified_at_ms,
            last_writer_address: entry.last_writer_address,
            lock,
            link_target: entry.link_target,
            owner_id: entry.owner_id,
        })
    }

    // ── sys_unlink ────────────────────────────────────────────────────

    /// Unified sys_unlink — accepts single or batch requests.
    ///
    /// Returns one `Result` per input request, preserving input order.
    /// `reqs.len() == 1` → fast path via `sys_unlink_single()`.
    /// `reqs.len() > 1` → loops `sys_unlink_single` per item.
    pub fn sys_unlink(
        &self,
        reqs: &[crate::kernel::UnlinkRequest],
        ctx: &OperationContext,
    ) -> Vec<Result<SysUnlinkResult, KernelError>> {
        if reqs.is_empty() {
            return Vec::new();
        }
        if reqs.len() == 1 {
            let req = &reqs[0];
            return vec![self.sys_unlink_single(&req.path, ctx, req.recursive)];
        }
        reqs.iter()
            .map(
                |req| match self.sys_unlink_single(&req.path, ctx, req.recursive) {
                    Ok(r) => Ok(r),
                    Err(_) => Ok(SysUnlinkResult {
                        hit: false,
                        entry_type: 0,
                        post_hook_needed: false,
                        path: req.path.clone(),
                        content_id: None,
                        size: 0,
                    }),
                },
            )
            .collect()
    }

    /// Convenience wrapper for single-path unlinks.
    ///
    /// Kernel-internal callers (tests, services) and external callers
    /// (gRPC, PyO3) use this for the familiar single-path API.
    pub fn sys_unlink_one(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysUnlinkResult, KernelError> {
        self.sys_unlink_single(path, ctx, recursive)
    }

    /// Single-path unlink implementation.
    ///
    /// Returns `hit=true` when Rust completed the full operation. Python only
    /// dispatches event notify + POST hooks.
    /// Returns `hit=false` for DT_EXTERNAL_STORAGE (5) → Python handles connector teardown.
    /// DT_DIR is handled inline via sys_rmdir (§12e).
    fn sys_unlink_single(
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

        // 1b. Trie-resolved virtual paths (§11 trie resolution)
        if self.trie.lookup(path).is_some() {
            return miss(0);
        }

        // 1c. Permission gate (§13) — BEFORE native hooks.
        self.check_permission(path, Permission::Write, ctx)?;

        // 1d. Native INTERCEPT PRE hooks (§11 native hooks)
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

        // 2.5. Mount-point synthesis: ``sys_unlink`` on a federation mount
        // root runs the full unmount lifecycle (``dlc.unmount``). The
        // DT_MOUNT inode lives in the *parent* zone's metastore, which
        // routing skips — synthesize a DT_MOUNT entry directly from
        // routing structure when the path IS the federation mount point
        // (parent canonical zone differs from the routed target zone).
        // Mirrors the ``sys_stat`` synthesis above.
        let (parent_zone, _user_mp) =
            crate::vfs_router::extract_zone_from_canonical(&route.mount_point);
        let entry = if route.backend_path.is_empty() && parent_zone != route.zone_id {
            FileMetadata {
                path: path.to_string(),
                size: 0,
                content_id: None,
                gen: 0,
                version: 1,
                entry_type: DT_MOUNT,
                zone_id: Some(route.zone_id.clone()),
                mime_type: None,
                created_at_ms: None,
                modified_at_ms: None,
                last_writer_address: None,
                target_zone_id: Some(route.zone_id.clone()),
                link_target: None,
                owner_id: None,
            }
        } else {
            // 3. Get metadata via the routed metastore (per-mount first,
            //    global fallback — internal cache fast path).
            let meta: Option<FileMetadata> = self
                .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
                .flatten();

            match meta {
                Some(e) => e,
                None => {
                    let dirty_key = DirtyWriteKey::new(path, &ctx.zone_id);
                    if self.write_buffer.contains_dirty_key(&dirty_key) {
                        self.flush_write_buffer(Some(path), Some(&ctx.zone_id))?;
                    }
                    match self
                        .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
                        .flatten()
                    {
                        Some(e) => e,
                        None => return miss(0),
                    }
                }
            }
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
            // Metadata delete + dcache invalidation handled here (kernel concern).
            // Connector teardown (token revocation, etc.) stays in Python DLC
            // via the post_hook_needed signal.
            5 => {
                // Metadata-first ordering: delete metadata so the entry becomes
                // invisible immediately, then Python DLC handles connector teardown.
                // metastore.delete owns dcache invalidation internally.
                let _ = self.with_metastore_route(&route, |ms| ms.delete(path));
                return Ok(SysUnlinkResult {
                    hit: true,
                    entry_type: 5,          // DT_EXTERNAL_STORAGE
                    post_hook_needed: true, // always: Python DLC must unmount
                    path: path.to_string(),
                    content_id: entry.content_id,
                    size: entry.size,
                });
            }
            _ => {}
        }

        // 5. VFS write lock (DT_REG path)
        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return miss(entry.entry_type);
        }

        if entry.entry_type == DT_REG {
            if let Err(err) = self.flush_write_buffer_locked_path(path, &ctx.zone_id) {
                self.lock_manager.do_release(lock_handle);
                return Err(err);
            }
        }

        // 6. Atomic metastore delete + dcache evict (metadata-first ordering).
        // Metadata is deleted first so the file becomes invisible immediately;
        // on metastore failure the entry remains fully consistent for retry.
        // Backend bytes are cleaned up afterward (best-effort); orphaned objects
        // from a failed backend delete are collected by a background GC sweep.
        // The inverse ordering (backend-first) risks deleting bytes while leaving
        // live metadata if the metastore call fails — unrecoverable data loss.
        let del_res = self
            .with_metastore_route(&route, |ms| ms.delete(path))
            .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
            .and_then(|r| {
                r.map_err(|e| KernelError::IOError(format!("metastore_delete({path}): {e:?}")))
            });
        if let Err(e) = del_res {
            self.lock_manager.do_release(lock_handle);
            return Err(e);
        }

        // 7. Backend delete (PAS only, best-effort after metastore commit).
        // Errors are not surfaced to the caller — the namespace is already clean
        // and orphaned bytes are harmless pending GC. CAS backends do not track
        // content by path; their GC handles unreferenced blobs independently.
        let _ = route
            .backend
            .as_ref()
            .map(|b| b.delete_file(&route.backend_path));
        self.file_cache.invalidate_path(&ctx.zone_id, path, "raw");
        self.index_cache
            .invalidate_parent_listing(&ctx.zone_id, path);

        // 8. Release VFS lock
        self.lock_manager.do_release(lock_handle);

        // 10. OBSERVE-phase dispatch (§11 OBSERVE): queue FileDelete.
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

        // 1c. Permission gate (§13) — Write on both paths.
        self.check_permission(old_path, Permission::Write, ctx)?;
        self.check_permission(new_path, Permission::Write, ctx)?;

        // 1d. Native INTERCEPT PRE hooks (§11 native hooks)
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

        self.flush_write_buffer(Some(old_path), Some(&ctx.zone_id))?;

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
        if first != second && lock2 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        if let Err(err) = self.flush_write_buffer_locked_prefix(old_path, &ctx.zone_id) {
            release_locks(&self.lock_manager, lock1, lock2);
            return Err(err);
        }

        // 4. Existence check: get old metadata — use full VFS paths (R20.3 contract).
        // backend_path is used only for backend I/O and PAS content_id calculation.
        // The metastore impl serves cache hits from its own internal cache,
        // so no separate dcache fallback is needed.
        let old_meta = self
            .with_metastore_route(&old_route, |ms| ms.get(old_path).ok().flatten())
            .flatten();

        let (is_directory, entry_type) = match &old_meta {
            Some(m) => (m.entry_type == DT_DIR, m.entry_type),
            None => {
                // Check for implicit directory: no explicit entry, but has children
                let child_prefix = format!("{}/", old_path.trim_end_matches('/'));
                let has_children = self
                    .with_metastore_route(&old_route, |ms| {
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

        // 5. Destination conflict check — full VFS path (R20.3 contract)
        let new_exists = self
            .with_metastore(&new_route.mount_point, |ms| {
                ms.exists(new_path).unwrap_or(false)
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
                match old_route
                    .backend
                    .as_ref()
                    .map(|b| b.rename(&old_route.backend_path, &new_route.backend_path))
                {
                    Some(Err(e)) => {
                        release_locks(&self.lock_manager, lock1, lock2);
                        return Err(KernelError::IOError(format!(
                            "sys_rename: backend rename failed: {e:?}"
                        )));
                    }
                    Some(Ok(())) => true,
                    // None = no Rust backend (external connectors); fall through
                    // to metadata-only rename for those.
                    None => false,
                }
            } else {
                false
            };

            // Commit metadata after PAS bytes are moved (or immediately for CAS).
            // Use full VFS paths — metastore entries written by sys_write use full paths.
            let rename_result = self
                .with_metastore(&old_route.mount_point, |ms| {
                    ms.rename_path(old_path, new_path, !old_route.is_cas)
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
                    if let Some(Err(rollback_err)) = old_route
                        .backend
                        .as_ref()
                        .map(|b| b.rename(&new_route.backend_path, &old_route.backend_path))
                    {
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
                let _ = old_route
                    .backend
                    .as_ref()
                    .map(|b| b.rename(&old_route.backend_path, &new_route.backend_path));
            }
        }

        // 9. Each metastore impl owns its own internal cache and
        // already invalidated old_path / repopulated new_path during
        // ``rename_path`` above. The kernel side has nothing left to do
        // — there is no kernel-global metadata cache to keep in sync.
        self.file_cache
            .invalidate_path(&ctx.zone_id, old_path, "raw");
        self.file_cache
            .invalidate_path(&ctx.zone_id, new_path, "raw");
        self.index_cache
            .invalidate_parent_listing(&ctx.zone_id, old_path);
        self.index_cache
            .invalidate_parent_listing(&ctx.zone_id, new_path);

        // 10. Release sorted locks
        release_locks(&self.lock_manager, lock1, lock2);

        // 11. OBSERVE-phase dispatch (§11 OBSERVE): queue FileRename.
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
        // Metastore is the SSOT — its internal cache covers what the
        // legacy dcache used to mirror.
        let (rename_old_etag, rename_old_size, rename_old_version, rename_old_modified_at_ms) =
            match &old_meta {
                Some(m) => (
                    m.content_id.clone(),
                    Some(m.size),
                    Some(m.version),
                    m.modified_at_ms,
                ),
                None => (None, None, None, None),
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
        // Outer entry point — one DT_LINK follow allowed on `src_path`.
        // The recursive call below passes 0 so a chained link rejects.
        // `dst` is never a link follow target — copying INTO an existing
        // link is a write operation that goes through sys_write's link
        // follow path separately.
        self.sys_copy_with_link_depth(src_path, dst_path, ctx, 1)
    }

    fn sys_copy_with_link_depth(
        &self,
        src_path: &str,
        dst_path: &str,
        ctx: &OperationContext,
        max_link_hops: u8,
    ) -> Result<SysCopyResult, KernelError> {
        let miss = || {
            Ok(SysCopyResult {
                hit: false,
                post_hook_needed: false,
                dst_path: dst_path.to_string(),
                content_id: None,
                size: 0,
                version: 0,
                gen: 0,
            })
        };

        // 1. Validate both paths
        validate_path_fast(src_path)?;
        validate_path_fast(dst_path)?;

        // 1c. Permission gate (§13) — Read on src, Write on dst.
        self.check_permission(src_path, Permission::Read, ctx)?;
        self.check_permission(dst_path, Permission::Write, ctx)?;

        // 2. Route both (read access for src, write access for dst)
        let src_route = match self.vfs_router.route(src_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        let dst_route = match self.vfs_router.route(dst_path, &ctx.zone_id) {
            Ok(r) => r,
            Err(_) => return miss(),
        };
        self.flush_dirty_path_if_present(src_path, &ctx.zone_id)?;
        self.flush_dirty_path_if_present(dst_path, &ctx.zone_id)?;

        // 3. Get source metadata via the routed metastore (internal
        //    cache fast path) — full VFS paths (R20.3 contract).
        let src_meta: FileMetadata = match self
            .with_metastore_route(&src_route, |ms| ms.get(src_path).ok().flatten())
            .flatten()
        {
            Some(e) => e,
            None => return Err(KernelError::FileNotFound(src_path.to_string())),
        };

        // 3a. DT_LINK transparent follow on src — copy targets the
        // content the link points at, not the link's metadata entry.
        // (`cp -P` style "copy the link itself" is intentionally not
        // the default; sys_unlink and sys_rename keep operating on the
        // link entry directly.) Resolution runs AFTER routing + metadata
        // load so cold-cache and cross-mount links resolve against
        // authoritative metadata. Recursive call with `max_link_hops=0`
        // rejects chained links via this same branch.
        if let Some(target) = Self::dt_link_target(src_path, &src_meta)? {
            if max_link_hops == 0 {
                return Err(KernelError::PermissionDenied(format!(
                    "DT_LINK chain rejected (ELOOP) at {src_path}"
                )));
            }
            return self.sys_copy_with_link_depth(target, dst_path, ctx, max_link_hops - 1);
        }

        // 4. Reject non-regular files (§12e: explicit error, not miss)
        if src_meta.entry_type != DT_REG {
            return Err(KernelError::InvalidPath(format!(
                "sys_copy: source is not a regular file (entry_type={}): {}",
                src_meta.entry_type, src_path
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
        if first != second && lock2 == 0 {
            release_locks(&self.lock_manager, lock1, lock2);
            return miss();
        }

        // Snapshot destination state under the VFS locks. Copy-overwrite
        // is a content mutation, so it bumps the destination generation/version.
        let old_dst_meta: Option<FileMetadata> = self
            .with_metastore_route(&dst_route, |ms| ms.get(dst_path).ok().flatten())
            .flatten();
        if let Some(meta) = old_dst_meta.as_ref() {
            if meta.entry_type != DT_REG {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(KernelError::InvalidPath(format!(
                    "sys_copy: destination is not a regular file (entry_type={}): {}",
                    meta.entry_type, dst_path
                )));
            }
        }
        let new_version = old_dst_meta
            .as_ref()
            .map(|m| m.version)
            .unwrap_or(0)
            .saturating_add(1);
        let new_gen = old_dst_meta
            .as_ref()
            .map(|m| m.gen)
            .unwrap_or(0)
            .saturating_add(1);
        let old_dst_content: Option<Vec<u8>> = if !dst_route.is_cas && old_dst_meta.is_some() {
            let content_id = old_dst_meta
                .as_ref()
                .and_then(|m| m.content_id.as_deref())
                .filter(|id| !id.is_empty())
                .unwrap_or(&dst_route.backend_path);
            let backend = match dst_route.backend.as_ref() {
                Some(backend) => backend,
                None => {
                    release_locks(&self.lock_manager, lock1, lock2);
                    return Err(KernelError::IOError(format!(
                        "sys_copy: destination has tracked metadata but no backend: {dst_path}"
                    )));
                }
            };
            match backend.read_content(content_id, ctx) {
                Ok(content) => Some(content),
                Err(e) => {
                    release_locks(&self.lock_manager, lock1, lock2);
                    return Err(KernelError::BackendError(format!(
                        "sys_copy: failed to snapshot destination before overwrite: {e:?}"
                    )));
                }
            }
        } else {
            None
        };

        // For PAS backends, also check backend existence so rollback never
        // deletes a pre-existing untracked file at the destination path.
        // If the backend path already exists (untracked by metastore), reject
        // the copy rather than silently overwriting and potentially losing bytes
        // if the subsequent metastore commit fails.
        if !dst_route.is_cas {
            // Probe via the inline backend Arc — Some(true) means the file
            // exists, Some(false) means NotFound, None means no Rust backend
            // or the backend doesn't implement size probing (treat as
            // unknown — let the actual copy decide).
            let exists = dst_route.backend.as_ref().and_then(|b| {
                match b.get_content_size(&dst_route.backend_path) {
                    Ok(_) => Some(true),
                    Err(crate::abc::object_store::StorageError::NotFound(_)) => Some(false),
                    Err(_) => None,
                }
            });
            if old_dst_meta.is_none() && exists == Some(true) {
                release_locks(&self.lock_manager, lock1, lock2);
                return Err(KernelError::IOError(format!(
                    "sys_copy: destination backend path already exists (untracked): {dst_path}"
                )));
            }
        }

        // 7. Copy content (strategy depends on same-mount vs cross-mount)
        let same_mount = src_route.mount_point == dst_route.mount_point;

        // Track whether this operation created destination bytes so rollback only
        // deletes bytes we wrote (not pre-existing untracked backend objects).
        let mut wrote_dst_bytes = false;

        let copy_result: Result<(String, u64), KernelError> = if same_mount {
            // Try server-side copy first (PAS backends)
            match src_route
                .backend
                .as_ref()
                .map(|b| b.copy_file(&src_route.backend_path, &dst_route.backend_path))
            {
                Some(Ok(wr)) => {
                    wrote_dst_bytes = true;
                    Ok((wr.content_id, wr.size))
                }
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
                            let r =
                                self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx);
                            if r.is_ok() {
                                wrote_dst_bytes = true;
                            }
                            r
                        }
                    } else {
                        let r = self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx);
                        if r.is_ok() {
                            wrote_dst_bytes = true;
                        }
                        r
                    }
                }
                Some(Err(e)) => {
                    // Real backend error (disk full, permission denied, etc.) — propagate.
                    Err(KernelError::BackendError(format!("sys_copy: {e:?}")))
                }
            }
        } else {
            // Cross-mount: read from src backend, write to dst backend
            let r = self.copy_via_read_write(&src_route, &dst_route, &src_meta, ctx);
            if r.is_ok() {
                wrote_dst_bytes = true;
            }
            r
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
        let created_at_ms = old_dst_meta
            .as_ref()
            .and_then(|m| m.created_at_ms)
            .or(Some(now_ms));
        // Use full VFS dst_path for metastore key to match R20.3 convention.
        let meta = self.build_metadata(
            dst_path,
            &dst_route.zone_id,
            DT_REG,
            size,
            Some(content_id.clone()),
            new_gen,
            new_version,
            src_meta.mime_type.clone(),
            created_at_ms,
            Some(now_ms),
        );
        // 9. Atomic commit — metastore is the SSOT; its internal cache
        // is updated write-through by `put`.
        let put_result = self
            .with_metastore(&dst_route.mount_point, move |ms| ms.put(dst_path, meta))
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
            // Metastore failed after bytes were written to the destination backend.
            // Restore tracked PAS destinations. Only delete the destination if this
            // operation created previously untracked bytes.
            let rollback_err = if wrote_dst_bytes {
                if let Some(old_bytes) = old_dst_content.as_ref() {
                    dst_route.backend.as_ref().map(|b| {
                        b.write_content(old_bytes, &dst_route.backend_path, ctx, 0)
                            .map(|_| ())
                    })
                } else if old_dst_meta.is_none() {
                    dst_route
                        .backend
                        .as_ref()
                        .map(|b| b.delete_file(&dst_route.backend_path))
                } else {
                    None
                }
            } else {
                None
            };
            release_locks(&self.lock_manager, lock1, lock2);
            return Err(match rollback_err {
                Some(Err(rollback_err)) => KernelError::IOError(format!(
                    "sys_copy: metastore.put failed ({e:?}) and rollback \
                     also failed ({rollback_err:?}); destination bytes at {} may remain",
                    dst_route.backend_path
                )),
                _ => KernelError::IOError(format!("sys_copy: metastore.put: {e:?}")),
            });
        }

        // 10. Release VFS locks
        release_locks(&self.lock_manager, lock1, lock2);

        Ok(SysCopyResult {
            hit: true,
            post_hook_needed: self.copy_hook_count.load(Ordering::Relaxed) > 0,
            dst_path: dst_path.to_string(),
            content_id: Some(content_id),
            size,
            version: new_version,
            gen: new_gen,
        })
    }

    /// Internal: copy content via read_content + write_content (cross-mount or fallback).
    fn copy_via_read_write(
        &self,
        src_route: &crate::vfs_router::RouteResult,
        dst_route: &crate::vfs_router::RouteResult,
        src_meta: &FileMetadata,
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

        let content = src_route
            .backend
            .as_ref()
            .and_then(|b| b.read_content(content_id, ctx).ok())
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to read source content at {}",
                    src_route.backend_path
                ))
            })?;

        let wr = dst_route
            .backend
            .as_ref()
            .ok_or_else(|| {
                KernelError::IOError(format!(
                    "sys_copy: failed to write destination at {}",
                    dst_route.backend_path
                ))
            })?
            .write_content(&content, &dst_route.backend_path, ctx, 0)
            .map_err(|e| KernelError::BackendError(format!("sys_copy: {e:?}")))?;

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

        // 1c. Permission gate (§13) — Write permission for mkdir.
        self.check_permission(path, Permission::Write, ctx)?;

        // 2. Route (check write access)
        let route = self.vfs_router.route(path, &ctx.zone_id)?;

        // 2.5. mkdir on a mount point itself: the mount IS the
        // directory, by virtue of being a mount.  Materialising a
        // second metadata entry inside the target zone's metastore
        // (a) duplicates what the DT_MOUNT entry already represents
        // and (b) routes the put into the target zone's raft, which
        // races with the zone's leader election on a freshly-created
        // zone (voter ConfState seeded but no leader yet).  The race
        // surfaced as ``ZoneMetaStore.put: not leader, leader hint:
        // None`` and broke TestCrossNodeContentRead /
        // TestCrossZoneDailyWorkflow / TestLastWriterAttribution after
        // federation_zones migrated to combined sys_setattr DT_MOUNT.
        //
        // Honour parents=True first — the caller may rely on
        // ensure_parent_directories materialising the mount-point's
        // parent path (caught by tests/unit/core/test_mount_directory_creation).
        if route.backend_path.is_empty() {
            if parents {
                self.ensure_parent_directories(path, ctx, &route)?;
            }
            return Ok(SysMkdirResult {
                hit: true,
                post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
            });
        }

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
                    self.ensure_parent_directories(path, ctx, &route)?;
                }
                return Ok(SysMkdirResult {
                    hit: true,
                    post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
                });
            }
        }

        // 4. Backend mkdir (best-effort, PAS backends create physical dirs)
        let _ = route
            .backend
            .as_ref()
            .map(|b| b.mkdir(&route.backend_path, parents, true));

        // 5. Ensure parent directories
        if parents {
            self.ensure_parent_directories(path, ctx, &route)?;
        }

        // 6. Create directory metadata in metastore (per-mount or global) — full path
        let meta = self.build_metadata(
            path,
            &route.zone_id,
            DT_DIR,
            0,
            None,
            0,
            1,
            Some("inode/directory".to_string()),
            None,
            None,
        );
        // 7. Atomic commit via the per-mount metastore Arc on RouteResult.
        self.with_metastore_route(&route, |ms| ms.put(path, meta))
            .ok_or_else(|| KernelError::IOError("no metastore wired".into()))?
            .map_err(|e| KernelError::IOError(format!("metastore_put({path}): {e:?}")))?;

        // 8. OBSERVE-phase dispatch (§11 OBSERVE): queue DirCreate.
        // Only fires on the newly-created path — the early return at
        // step 3 (already-exists branch) does NOT dispatch because no
        // state actually changed. Parent directories created via
        // ensure_parent_directories don't get individual events; the
        // top-level mkdir event is enough for observers like
        // FileWatchRegistry to invalidate their dcache for the subtree.
        self.index_cache
            .invalidate_parent_listing(&ctx.zone_id, path);
        self.dispatch_mutation(FileEventType::DirCreate, path, ctx, |_ev| {});

        Ok(SysMkdirResult {
            hit: true,
            post_hook_needed: self.mkdir_hook_count.load(Ordering::Relaxed) > 0,
        })
    }

    /// Walk up `path` creating missing parent directory metadata.
    ///
    /// Metastore is keyed by full paths, so we walk the global path
    /// directly — no separate zone_path traversal needed.
    fn ensure_parent_directories(
        &self,
        path: &str,
        ctx: &OperationContext,
        route: &crate::vfs_router::RouteResult,
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
                        .with_metastore_route(route, |ms| ms.exists(cur).unwrap_or(true))
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
                0,
                1,
                Some("inode/directory".to_string()),
                None,
                None,
            );
            self.with_metastore_route(route, |ms| ms.put(dir_ref, meta))
                .ok_or_else(|| KernelError::IOError("no metastore wired".into()))?
                .map_err(|e| KernelError::IOError(format!("metastore_put({dir_ref}): {e:?}")))?;
        }
        Ok(())
    }

    // ── sys_rmdir ──────────────────────────────────────────────────────

    /// Kernel-internal rmdir: full rmdir (validate -> route -> children check -> delete -> dcache).
    ///
    /// Only called from `sys_unlink` DT_DIR branch internally.
    /// Not exposed to Python (removed from PyO3 surface in C21).
    ///
    /// Returns `hit=true` when Rust completed the full operation.
    /// Returns `hit=false` for DT_MOUNT/DT_EXTERNAL_STORAGE -> Python handles unmount.
    pub(crate) fn sys_rmdir(
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

        // 1c. Permission gate (§13) — Write permission for rmdir.
        self.check_permission(path, Permission::Write, ctx)?;

        // 2. Route (check write access)
        let route = self.vfs_router.route(path, &ctx.zone_id)?;

        self.flush_write_buffer(Some(path), Some(&ctx.zone_id))?;

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

        let lock_handle =
            self.lock_manager
                .blocking_acquire(path, LockMode::Write, self.vfs_lock_timeout_ms());
        if lock_handle == 0 {
            return miss();
        }

        if let Err(err) = self.flush_write_buffer_locked_prefix(path, &ctx.zone_id) {
            self.lock_manager.do_release(lock_handle);
            return Err(err);
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
            match result {
                Ok(deleted) => children_deleted = deleted,
                Err(err) => {
                    self.lock_manager.do_release(lock_handle);
                    return Err(err);
                }
            }
        }

        // 6. Backend rmdir (best-effort)
        let _ = route
            .backend
            .as_ref()
            .map(|b| b.rmdir(&route.backend_path, recursive));

        // 7. Atomic delete — metastore is the SSOT. Per-key cache
        // invalidation already happened: ``delete_batch`` invalidated
        // each child's cache row, and ``ms.delete`` invalidates the
        // parent's. No kernel-global cache to evict.
        let delete_result = self
            .with_metastore_route(&route, |ms| ms.delete(path))
            .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
            .and_then(|result| {
                result.map_err(|e| KernelError::IOError(format!("metastore_delete({path}): {e:?}")))
            });
        if let Err(err) = delete_result {
            self.lock_manager.do_release(lock_handle);
            return Err(err);
        }

        self.lock_manager.do_release(lock_handle);

        // 9. OBSERVE-phase dispatch (§11 OBSERVE): queue DirDelete.
        // Like sys_mkdir, only the top-level rmdir event fires —
        // recursively-deleted children don't generate individual events
        // (observers needing per-child notifications can list the
        // directory before unlink themselves; the top-level event is
        // the cache-invalidation signal).
        self.index_cache
            .invalidate_parent_listing(&ctx.zone_id, path);
        self.dispatch_mutation(FileEventType::DirDelete, path, ctx, |_ev| {});

        Ok(SysRmdirResult {
            hit: true,
            post_hook_needed: self.rmdir_hook_count.load(Ordering::Relaxed) > 0,
            children_deleted,
        })
    }

    // ── Tier 2 convenience methods ────────────────────────────────────

    /// Fast access check: validate + route + metastore existence.
    ///
    /// Returns true if a metadata entry exists for `path` and the
    /// path is routable. ``MetaStore::exists`` is a cache-fast check
    /// when the row is in the impl's internal cache, authoritative
    /// on a cache miss — no false negatives like the legacy
    /// dcache-only check produced.
    pub fn access(&self, path: &str, zone_id: &str) -> bool {
        if validate_path_fast(path).is_err() {
            return false;
        }
        let route = match self.vfs_router.route(path, zone_id) {
            Ok(r) => r,
            Err(_) => return false,
        };
        if self.flush_dirty_path_if_present(path, zone_id).is_err() {
            return false;
        }
        self.with_metastore_route(&route, |ms| ms.exists(path).unwrap_or(false))
            .unwrap_or(false)
    }

    // ── Internal batch functions (not Tier 1 syscalls) ────────────────

    /// Batch write implementation — sorted VFS lock acquisition,
    /// per-item backend write, grouped metastore commit.
    ///
    /// Called by `sys_write` when `reqs.len() > 1`.
    /// Returns `Vec<Result<SysWriteResult, KernelError>>` with per-item results.
    /// PRE-hooks are NOT dispatched here (caller handles batch pre-hooks).
    fn sys_write_batch_impl(
        &self,
        reqs: &[crate::kernel::WriteRequest],
        ctx: &OperationContext,
    ) -> Vec<Result<SysWriteResult, KernelError>> {
        let n = reqs.len();
        let mut results: Vec<Result<SysWriteResult, KernelError>> = Vec::with_capacity(n);

        let write_miss = || SysWriteResult {
            hit: false,
            content_id: None,
            post_hook_needed: false,
            version: 0,
            gen: 0,
            size: 0,
            is_new: false,
            old_content_id: None,
            old_size: None,
            old_version: None,
            old_modified_at_ms: None,
        };

        // 1. Validate all paths (fail-fast)
        for req in reqs {
            if validate_path_fast(&req.path).is_err() {
                return reqs
                    .iter()
                    .map(|r| Err(KernelError::InvalidPath(r.path.clone())))
                    .collect();
            }
        }

        // 2. Route all paths (single lock acquisition on mount table via read lock)
        let mut routes = Vec::with_capacity(n);
        for req in reqs {
            let route = self.vfs_router.route(&req.path, &ctx.zone_id).ok();
            routes.push(route);
        }

        // 3. Sorted VFS lock acquisition for all paths
        let mut lock_handles: Vec<u64> = vec![0; n];
        {
            // Sort indices by path to avoid deadlock
            let mut indices: Vec<usize> = (0..n).collect();
            indices.sort_by(|a, b| reqs[*a].path.cmp(&reqs[*b].path));

            for idx in indices {
                if routes[idx].is_some() {
                    lock_handles[idx] = self.lock_manager.blocking_acquire(
                        &reqs[idx].path,
                        LockMode::Write,
                        self.vfs_lock_timeout_ms(),
                    );
                }
            }
        }

        // 4. Write each item — collect metadata for batch put
        // Tuple: (mount_point, path, FileMetadata) for per-mount metastore support
        let mut batch_meta: Vec<(String, String, crate::meta_store::FileMetadata)> = Vec::new();

        for (i, (req, route_opt)) in reqs.iter().zip(routes.iter()).enumerate() {
            let route = match route_opt {
                Some(r) => r,
                None => {
                    results.push(Ok(write_miss()));
                    continue;
                }
            };

            // Lock timeout check
            if lock_handles[i] == 0 {
                results.push(Ok(write_miss()));
                continue;
            }

            // Contract guard: sys_write_batch does NOT create new files.
            // Same as sys_write step 3b — file must exist in metastore.
            // Batch writes are always at offset=0; callers that need to
            // create files must use sys_setattr(path, DT_REG) first.
            let pre_entry_exists = self
                .with_metastore_route(route, |ms| ms.get(path).ok().flatten())
                .flatten()
                .is_some();
            if !pre_entry_exists {
                results.push(SysWriteResult {
                    hit: false,
                    content_id: None,
                    post_hook_needed: false,
                    version: 0,
                    gen: 0,
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
            // whole batch.
            let write_result = route
                .backend
                .as_ref()
                .and_then(|b| b.write_content(content, &route.backend_path, ctx, 0).ok());

            match write_result {
                Some(wr) => {
                    let batch_old_entry: Option<FileMetadata> = self
                        .with_metastore_route(route, |ms| ms.get(&req.path).ok().flatten())
                        .flatten();
                    let old_version = batch_old_entry.as_ref().map(|e| e.version).unwrap_or(0);
                    let old_gen = batch_old_entry.as_ref().map(|e| e.gen).unwrap_or(0);
                    let new_version = old_version + 1;
                    let new_gen = old_gen.saturating_add(1);

                    // Collect metadata for batch put (instead of N individual puts)
                    let meta = self.build_metadata(
                        &req.path,
                        &route.zone_id,
                        DT_REG,
                        wr.size,
                        Some(wr.content_id.clone()),
                        new_gen,
                        new_version,
                        None,
                        None,
                        None,
                    );
                    // Defer dcache + metastore commit to step 4b so
                    // we can group raft proposes per mount and mark
                    // each result hit/miss based on the actual
                    // commit outcome rather than eagerly lying.
                    batch_meta.push((route.mount_point.clone(), req.path.to_string(), meta));

                    results.push(Ok(SysWriteResult {
                        hit: true,
                        content_id: Some(wr.content_id),
                        post_hook_needed: self.write_hook_count.load(Ordering::Relaxed) > 0
                            || self.write_batch_hook_count.load(Ordering::Relaxed) > 0,
                        version: new_version,
                        gen: new_gen,
                        size: wr.size,
                        is_new: batch_old_entry.is_none(),
                        old_content_id: batch_old_entry.as_ref().and_then(|e| e.content_id.clone()),
                        old_size: batch_old_entry.as_ref().map(|e| e.size),
                        old_version: batch_old_entry.as_ref().map(|e| e.version),
                        old_modified_at_ms: batch_old_entry.as_ref().and_then(|e| e.modified_at_ms),
                    }));
                }
                None => {
                    results.push(Ok(write_miss()));
                }
            }
        }

        // 4b. Atomic per-item commit. Per-mount items dispatch through the
        // mount's own metastore (raft propose for federation zones); global
        // items (no per-mount metastore) collect into a batch put since the
        // global LocalMetaStore can do that as one redb txn. Failures flip
        // the corresponding result entry from hit=true → hit=false so the
        // caller learns which items actually committed.
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
                    let put_res = self
                        .with_metastore(&mp, move |ms| ms.put(&path, meta))
                        .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
                        .and_then(|r| r.map_err(|e| KernelError::IOError(format!("{e:?}"))));
                    if let Err(_e) = put_res {
                        // Mark this batch entry as not-hit so the
                        // caller knows the propose didn't commit.
                        if let Some(Ok(r)) = results.get_mut(idx) {
                            r.hit = false;
                        }
                    }
                } else {
                    global_items.push((path, meta));
                    global_idx.push(idx);
                }
            }
            if !global_items.is_empty() {
                let put_ok = self
                    .metastore
                    .read()
                    .as_ref()
                    .map(|ms| ms.put_batch(&global_items).is_ok())
                    .unwrap_or(false);
                if !put_ok {
                    for idx in global_idx {
                        if let Some(Ok(r)) = results.get_mut(idx) {
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

        results
    }

    /// Batch read implementation — Phase A (per-item auth + hooks)
    /// → Phase B (coalesce + rayon parallel fetch).
    ///
    /// Called by `sys_read` when `reqs.len() > 1`. Uses
    /// `read_batch_max_aggregate_bytes` from kernel config as the DoS cap.
    fn sys_read_batch_impl(
        &self,
        reqs: &[crate::kernel::ReadRequest],
        ctx: &OperationContext,
    ) -> Vec<Result<SysReadResult, KernelError>> {
        let n = reqs.len();
        let mut results: Vec<Option<Result<SysReadResult, KernelError>>> =
            (0..n).map(|_| None).collect();
        let mut resolved: Vec<Option<ResolvedRead>> = (0..n).map(|_| None).collect();

        // Phase A — validate, permission, route, metadata lookup.
        for (i, req) in reqs.iter().enumerate() {
            // 1. Path validation
            if let Err(e) = validate_path_fast(&req.path) {
                results[i] = Some(Err(e));
                continue;
            }
            // 2. Permission gate
            if let Err(e) = self.check_permission(&req.path, Permission::Read, ctx) {
                results[i] = Some(Err(e));
                continue;
            }
            // 3. Routing
            let route = match self.vfs_router.route(&req.path, &ctx.zone_id) {
                Ok(r) => r,
                Err(_) => {
                    results[i] = Some(Err(KernelError::FileNotFound(req.path.clone())));
                    continue;
                }
            };
            // 4. Metadata lookup (best-effort; None means cold-cache /
            //    backend-only file — Phase B falls through to sys_read_one
            //    which has its own backend fallback path).
            let entry = self
                .with_metastore_route(&route, |ms| ms.get(&req.path).ok().flatten())
                .flatten();
            // Reject pipe/stream — they have blocking IPC semantics that
            // don't belong in batch reads.
            if let Some(m) = entry.as_ref() {
                if m.entry_type == crate::meta_store::DT_PIPE
                    || m.entry_type == crate::meta_store::DT_STREAM
                {
                    results[i] = Some(Err(KernelError::IOError(format!(
                        "batch read does not support pipes/streams: {}",
                        req.path
                    ))));
                    continue;
                }
            }
            // Native INTERCEPT PRE hooks (§11 native hooks) — must fire
            // PER PATH before coalescing so ReBAC permission_hook and
            // audit hooks see every request's identity, not just the
            // lead's.
            let hook_id = HookIdentity {
                user_id: ctx.user_id.clone(),
                zone_id: ctx.zone_id.clone(),
                agent_id: ctx.agent_id.clone().unwrap_or_default(),
                is_admin: ctx.is_admin,
            };
            if let Err(e) = self.dispatch_native_pre(&HookContext::Read(ReadHookCtx {
                path: req.path.clone(),
                identity: hook_id,
                content: None,
                content_id: None,
            })) {
                results[i] = Some(Err(e));
                continue;
            }
            // Zero-length range short-circuit — narrowed to require a
            // concrete metastore entry whose type is a readable regular
            // file. Phase A has already run §13 permission + §11 native
            // PRE-read hook for this path, so returning empty bytes for
            // a proven-existing DT_REG file is authoritative.
            if req.len == Some(0) {
                if let Some(m) = entry.as_ref() {
                    if m.entry_type != crate::meta_store::DT_LINK {
                        results[i] = Some(Ok(SysReadResult {
                            data: Some(Vec::new()),
                            post_hook_needed: self.read_hook_count.load(Ordering::Relaxed) > 0,
                            content_id: m.content_id.clone(),
                            gen: m.gen,
                            entry_type: m.entry_type,
                            stream_next_offset: None,
                        }));
                        continue;
                    }
                }
            }
            resolved[i] = Some(ResolvedRead { route, entry });
        }

        // Aggregate-bytes cap from kernel config.
        let cap = self.read_batch_max_aggregate_bytes();
        if cap < usize::MAX {
            use std::collections::HashMap;
            let mut sizes: HashMap<(String, String), usize> = HashMap::new();
            let mut declared: usize = 0;
            // Indices to clear from `resolved` after the scan.
            let mut reject: Vec<usize> = Vec::new();
            for i in 0..n {
                let r = match resolved[i].as_ref() {
                    Some(r) => r,
                    None => continue,
                };
                let meta = match r.entry.as_ref() {
                    Some(m) => m,
                    None => {
                        results[i] = Some(Err(KernelError::IOError(format!(
                            "sys_read cannot bound metadata-less path under cap (cap={} bytes)",
                            cap
                        ))));
                        reject.push(i);
                        continue;
                    }
                };
                if meta.entry_type == crate::meta_store::DT_LINK {
                    results[i] = Some(Err(KernelError::IOError(format!(
                        "sys_read cannot pre-bound DT_LINK target size (cap={} bytes)",
                        cap
                    ))));
                    reject.push(i);
                    continue;
                }
                let cid = meta.content_id.clone().unwrap_or_default();
                let blob_size = meta.size as usize;
                if cid.is_empty() {
                    declared = declared.saturating_add(blob_size);
                } else {
                    let key = (r.route.mount_point.clone(), cid);
                    let prev = sizes.entry(key).or_insert(0);
                    if blob_size > *prev {
                        declared = declared.saturating_add(blob_size - *prev);
                        *prev = blob_size;
                    }
                }
                if declared > cap {
                    for j in i..n {
                        if results[j].is_none() && resolved[j].is_some() {
                            results[j] = Some(Err(KernelError::IOError(format!(
                                "sys_read declared aggregate {} bytes exceeds {} bytes cap",
                                declared, cap
                            ))));
                            reject.push(j);
                        }
                    }
                    break;
                }
            }
            for idx in reject {
                resolved[idx] = None;
            }
        }

        // Phase B — group surviving requests by (mount_point, content_id).
        use std::collections::HashMap;
        let mut groups: HashMap<(String, String), Vec<usize>> = HashMap::new();
        let mut singletons: Vec<usize> = Vec::new();
        for (i, slot) in resolved.iter().enumerate() {
            if results[i].is_some() {
                continue;
            }
            let r = match slot {
                Some(r) => r,
                None => continue,
            };
            let cid = r
                .entry
                .as_ref()
                .and_then(|e| e.content_id.as_deref())
                .unwrap_or("");
            if cid.is_empty() {
                singletons.push(i);
            } else {
                groups
                    .entry((r.route.mount_point.clone(), cid.to_string()))
                    .or_default()
                    .push(i);
            }
        }

        // Phase B fan-out — bounded parallelism over distinct work units.
        use rayon::prelude::*;

        enum Unit {
            Group { indices: Vec<usize> },
            Singleton { idx: usize },
        }

        let group_vec: Vec<((String, String), Vec<usize>)> = groups.into_iter().collect();
        let mut units: Vec<Unit> = Vec::with_capacity(group_vec.len() + singletons.len());
        for (_key, indices) in group_vec {
            units.push(Unit::Group { indices });
        }
        for i in singletons {
            units.push(Unit::Singleton { idx: i });
        }

        let max_conc = self.read_batch_max_concurrency().max(1);
        let chunk_size = units.len().div_ceil(max_conc).max(1);

        let scattered: Vec<(usize, Result<SysReadResult, KernelError>)> = units
            .par_chunks(chunk_size)
            .flat_map_iter(|chunk| {
                let mut local: Vec<(usize, Result<SysReadResult, KernelError>)> =
                    Vec::with_capacity(chunk.len() * 2);
                for unit in chunk {
                    match unit {
                        Unit::Group { indices } => {
                            let lead = indices[0];
                            let req = &reqs[lead];
                            // Route stability check — if the lead's mount has
                            // shifted since Phase A, fall back to full
                            // sys_read_one so authz + hooks run against the
                            // *current* mount. Otherwise use
                            // sys_read_content_only since Phase A already
                            // authorized this exact route.
                            let phase_a_mount = resolved
                                .get(lead)
                                .and_then(|o| o.as_ref())
                                .map(|r| r.route.mount_point.as_str())
                                .unwrap_or("");
                            let route_stable = self
                                .vfs_router
                                .route(&req.path, &ctx.zone_id)
                                .map(|r| r.mount_point == phase_a_mount)
                                .unwrap_or(false);
                            let shared = if route_stable {
                                self.sys_read_content_only(&req.path, ctx)
                            } else {
                                self.sys_read_one(&req.path, ctx, 5000, 0)
                            };
                            let lead_cid = shared.as_ref().ok().and_then(|r| r.content_id.clone());
                            for &i in indices.iter() {
                                let consumer_route = resolved
                                    .get(i)
                                    .and_then(|o| o.as_ref())
                                    .map(|r| &r.route)
                                    .expect("resolved set in Phase A");
                                let consumer_route_stable = self
                                    .vfs_router
                                    .route(&reqs[i].path, &ctx.zone_id)
                                    .map(|r| r.mount_point == consumer_route.mount_point)
                                    .unwrap_or(false);
                                if !consumer_route_stable {
                                    let r = self.sys_read_one(&reqs[i].path, ctx, 5000, 0);
                                    local.push((i, slice_read_result(r, &reqs[i])));
                                    continue;
                                }
                                let fresh_meta = self
                                    .with_metastore_route(consumer_route, |ms| {
                                        ms.get(&reqs[i].path).ok().flatten()
                                    })
                                    .flatten();
                                let consumer_cid =
                                    fresh_meta.as_ref().and_then(|m| m.content_id.as_deref());
                                let bytes_match = match (&lead_cid, consumer_cid) {
                                    (Some(l), Some(c)) => l == c,
                                    _ => false,
                                };
                                if bytes_match {
                                    local.push((
                                        i,
                                        clone_read_result(&shared, &reqs[i], fresh_meta.as_ref()),
                                    ));
                                } else {
                                    let r = self.sys_read_content_only(&reqs[i].path, ctx);
                                    local.push((i, slice_read_result(r, &reqs[i])));
                                }
                            }
                        }
                        Unit::Singleton { idx } => {
                            let req = &reqs[*idx];
                            let phase_a_mount = resolved
                                .get(*idx)
                                .and_then(|o| o.as_ref())
                                .map(|r| r.route.mount_point.as_str())
                                .unwrap_or("");
                            let route_stable = self
                                .vfs_router
                                .route(&req.path, &ctx.zone_id)
                                .map(|r| r.mount_point == phase_a_mount)
                                .unwrap_or(false);
                            let r = if route_stable {
                                self.sys_read_content_only(&req.path, ctx)
                            } else {
                                self.sys_read_one(&req.path, ctx, 5000, 0)
                            };
                            local.push((*idx, slice_read_result(r, req)));
                        }
                    }
                }
                local.into_iter()
            })
            .collect();

        for (i, r) in scattered {
            results[i] = Some(r);
        }

        results.into_iter().map(|o| o.unwrap()).collect()
    }

    /// List immediate children of a directory path via the routed metastore.
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

        // Federation procfs: /__sys__/zones/ enumerates loaded zones
        // (read-only namespace, like Linux /proc).
        if let Some(zones) = self.zones_procfs_readdir(parent_path) {
            return zones
                .into_iter()
                .map(|z| {
                    let path = format!("/__sys__/zones/{z}");
                    (path, DT_DIR)
                })
                .collect();
        }

        // Callers pass either "/local" or "/local/" — normalize the trailing
        // slash off before routing so prefix comparisons below don't produce
        // double slashes (which silently return no children).
        let normalized = if parent_path != "/" && parent_path.ends_with('/') {
            parent_path.trim_end_matches('/')
        } else {
            parent_path
        };
        let cache_scope = if is_admin {
            format!("{zone_id}:admin")
        } else {
            zone_id.to_string()
        };
        let cache_key = IndexCacheKey::new(cache_scope, normalized, IndexKind::Listing);
        if let Some(entries) = self.index_cache.get_listing(&cache_key) {
            return entries;
        }
        let route = match self.vfs_router.route(normalized, zone_id) {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        if self
            .flush_write_buffer(Some(normalized), Some(zone_id))
            .is_err()
        {
            return Vec::new();
        }

        let global_prefix = if normalized == contracts::VFS_ROOT {
            contracts::VFS_ROOT.to_string()
        } else {
            format!("{}/", normalized)
        };

        let needs_zone_filter = !is_admin && zone_id != contracts::ROOT_ZONE_ID;

        // Track (entry_type, zone_id) so we can zone-filter at the end.
        let mut seen: std::collections::BTreeMap<String, (u8, Option<String>)> =
            std::collections::BTreeMap::new();
        let parent_for_join = if parent_path == contracts::VFS_ROOT {
            ""
        } else {
            parent_path.trim_end_matches('/')
        };

        if let Some(ms_children) =
            self.with_metastore_route(&route, |ms| ms.list(&global_prefix).ok())
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

        // Backend list_dir merge (all backend types uniformly).
        // CAS/S3/GCS return Err(NotSupported) → ignored.  Path-local
        // returns disk entries, external connectors return API results.
        // No ABC leak: kernel treats every backend the same.
        if let Some(Ok(backend_entries)) = route
            .backend
            .as_ref()
            .map(|b| b.list_dir(&route.backend_path))
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

        let entries: Vec<(String, u8)> = if needs_zone_filter {
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
        };
        self.index_cache
            .put_listing(cache_key, entries.clone(), ttl_for_backend("kernel"));
        entries
    }

    /// Paginated readdir: returns a page of children with cursor-based
    /// pagination. `limit=0` returns all (backward compat). Cursor is
    /// the last path from the previous page.
    ///
    /// Intercepts `/__sys__/locks` prefix → `lock_manager.list_locks`.
    pub fn readdir_paged(
        &self,
        parent_path: &str,
        zone_id: &str,
        is_admin: bool,
        limit: usize,
        cursor: Option<&str>,
    ) -> super::ReadDirResult {
        // /__sys__/locks intercept — admin-only lock enumeration.
        if parent_path == contracts::LOCKS_PATH_PREFIX
            || parent_path.starts_with(&format!("{}/", contracts::LOCKS_PATH_PREFIX))
        {
            if !is_admin {
                return super::ReadDirResult {
                    items: Vec::new(),
                    next_cursor: None,
                    has_more: false,
                };
            }
            let prefix = if parent_path == contracts::LOCKS_PATH_PREFIX {
                ""
            } else {
                parent_path
                    .strip_prefix(&format!("{}/", contracts::LOCKS_PATH_PREFIX))
                    .unwrap_or("")
            };
            let effective_limit = if limit == 0 { 10000 } else { limit };
            match self.lock_manager.list_locks(prefix, effective_limit + 1) {
                Ok(locks) => {
                    let has_more = locks.len() > effective_limit;
                    let items: Vec<(String, u8)> = locks
                        .into_iter()
                        .take(effective_limit)
                        .map(|l| (l.path.clone(), DT_REG))
                        .collect();
                    let next_cursor = if has_more {
                        items.last().map(|(p, _)| p.clone())
                    } else {
                        None
                    };
                    return super::ReadDirResult {
                        items,
                        next_cursor,
                        has_more,
                    };
                }
                Err(_) => {
                    return super::ReadDirResult {
                        items: Vec::new(),
                        next_cursor: None,
                        has_more: false,
                    };
                }
            }
        }

        // Normal readdir with optional pagination.
        let all = self.readdir(parent_path, zone_id, is_admin);

        if limit == 0 {
            return super::ReadDirResult {
                items: all,
                next_cursor: None,
                has_more: false,
            };
        }

        // Apply cursor: skip entries up to and including the cursor path.
        let start_idx = if let Some(c) = cursor {
            all.iter()
                .position(|(p, _)| p.as_str() > c)
                .unwrap_or(all.len())
        } else {
            0
        };

        let end_idx = (start_idx + limit).min(all.len());
        let items: Vec<(String, u8)> = all[start_idx..end_idx].to_vec();
        let has_more = end_idx < all.len();
        let next_cursor = if has_more {
            items.last().map(|(p, _)| p.clone())
        } else {
            None
        };

        super::ReadDirResult {
            items,
            next_cursor,
            has_more,
        }
    }

    // ── Xattr virtual path handlers ──────────────────────────────────

    /// Parse `rest` = `"{key}/{actual_path}"` from an `/__xattr__/` read,
    /// route to the correct per-mount metastore, and return the value.
    fn handle_xattr_read(&self, rest: &str) -> Result<SysReadResult, KernelError> {
        let (key, actual_path) = Self::parse_xattr_rest(rest)?;
        let route = self
            .vfs_router
            .route(actual_path, contracts::ROOT_ZONE_ID)
            .map_err(|_| KernelError::FileNotFound(actual_path.to_string()))?;
        let value = self
            .with_metastore_route(&route, |ms| ms.get_file_metadata(actual_path, key))
            .ok_or_else(|| KernelError::IOError("no metastore wired".into()))?
            .map_err(|e| {
                KernelError::IOError(format!("xattr read({actual_path}, {key}): {e:?}"))
            })?;
        match value {
            Some(v) => Ok(SysReadResult {
                data: Some(v.into_bytes()),
                post_hook_needed: false,
                content_id: None,
                gen: 0,
                entry_type: DT_REG,
                stream_next_offset: None,
            }),
            None => Err(KernelError::FileNotFound(format!(
                "xattr {key} not found on {actual_path}"
            ))),
        }
    }

    /// Parse `rest` = `"{key}/{actual_path}"` from an `/__xattr__/` write,
    /// route to the correct per-mount metastore, and set the value.
    fn handle_xattr_write(
        &self,
        rest: &str,
        content: &[u8],
    ) -> Result<SysWriteResult, KernelError> {
        let (key, actual_path) = Self::parse_xattr_rest(rest)?;
        let value = std::str::from_utf8(content)
            .map_err(|e| KernelError::IOError(format!("xattr value not utf-8: {e}")))?
            .to_string();
        let route = self
            .vfs_router
            .route(actual_path, contracts::ROOT_ZONE_ID)
            .map_err(|_| KernelError::FileNotFound(actual_path.to_string()))?;
        self.with_metastore_route(&route, |ms| ms.set_file_metadata(actual_path, key, value))
            .ok_or_else(|| KernelError::IOError("no metastore wired".into()))?
            .map_err(|e| {
                KernelError::IOError(format!("xattr write({actual_path}, {key}): {e:?}"))
            })?;
        Ok(SysWriteResult {
            hit: true,
            content_id: None,
            post_hook_needed: false,
            version: 0,
            gen: 0,
            size: content.len() as u64,
            is_new: false,
            old_content_id: None,
            old_size: None,
            old_version: None,
            old_modified_at_ms: None,
        })
    }

    /// Split `"key/rest/of/path"` into `("key", "/rest/of/path")`.
    fn parse_xattr_rest(rest: &str) -> Result<(&str, &str), KernelError> {
        let idx = rest.find('/').ok_or_else(|| {
            KernelError::InvalidPath("xattr path must be /__xattr__/{key}/{path}".into())
        })?;
        let key = &rest[..idx];
        let actual_path = &rest[idx..]; // includes leading '/'
        if key.is_empty() || actual_path.len() < 2 {
            return Err(KernelError::InvalidPath(
                "xattr path must be /__xattr__/{key}/{path}".into(),
            ));
        }
        Ok((key, actual_path))
    }
}

#[cfg(test)]
mod read_batch_tests {
    use crate::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use crate::kernel::{Kernel, KernelError};
    use contracts::OperationContext;
    use parking_lot::Mutex;
    use std::collections::HashMap;
    use std::sync::Arc;

    fn ctx() -> OperationContext {
        // Admin + system bypass — fine for unit tests.
        OperationContext::new("test", "root", true, None, true)
    }

    /// Minimal in-memory backend — enough for write + read round-trips.
    #[derive(Default)]
    struct MemBackend {
        blobs: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl ObjectStore for MemBackend {
        fn name(&self) -> &str {
            "mem"
        }

        fn write_content(
            &self,
            content: &[u8],
            content_id: &str,
            _ctx: &OperationContext,
            offset: u64,
        ) -> Result<WriteResult, StorageError> {
            let mut map = self.blobs.lock();
            let entry = map.entry(content_id.to_string()).or_default();
            let start = offset as usize;
            if start > entry.len() {
                entry.resize(start, 0);
            }
            let end = start + content.len();
            if end > entry.len() {
                entry.resize(end, 0);
            }
            entry[start..end].copy_from_slice(content);
            let size = entry.len() as u64;
            Ok(WriteResult {
                content_id: content_id.to_string(),
                version: content_id.to_string(),
                size,
            })
        }

        fn read_content(
            &self,
            content_id: &str,
            _ctx: &OperationContext,
        ) -> Result<Vec<u8>, StorageError> {
            self.blobs
                .lock()
                .get(content_id)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(content_id.into()))
        }

        fn delete_file(&self, path: &str) -> Result<(), StorageError> {
            self.blobs.lock().remove(path);
            Ok(())
        }

        fn get_content_size(&self, content_id: &str) -> Result<u64, StorageError> {
            self.blobs
                .lock()
                .get(content_id)
                .map(|d| d.len() as u64)
                .ok_or_else(|| StorageError::NotFound(content_id.into()))
        }

        fn copy_file(&self, src: &str, dst: &str) -> Result<WriteResult, StorageError> {
            let mut map = self.blobs.lock();
            let data = map
                .get(src)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(src.into()))?;
            let size = data.len() as u64;
            map.insert(dst.to_string(), data);
            Ok(WriteResult {
                content_id: dst.to_string(),
                version: dst.to_string(),
                size,
            })
        }
    }

    /// Kernel pre-wired with an in-memory backend at `/`.
    fn kernel_with_backend() -> Kernel {
        let k = Kernel::new();
        let backend: Arc<dyn ObjectStore> = Arc::new(MemBackend::default());
        k.add_mount(
            "/",
            contracts::ROOT_ZONE_ID,
            Some(backend),
            None,
            None,
            false,
        )
        .expect("add_mount");
        k
    }

    /// Helper to build a ReadRequest with default timeout.
    fn rreq(path: &str, offset: u64, len: Option<u64>) -> crate::kernel::ReadRequest {
        crate::kernel::ReadRequest {
            path: path.into(),
            offset,
            len,
            timeout_ms: 5000,
        }
    }

    #[test]
    fn read_batch_empty_input_returns_empty_vec() {
        let k = Kernel::new();
        let out = k.sys_read(&[], &ctx());
        assert_eq!(out.len(), 0);
    }

    #[test]
    fn read_batch_invalid_path_yields_per_item_err() {
        let k = Kernel::new();
        let reqs = vec![
            rreq("", 0, None),
            rreq("/definitely/does/not/exist", 0, None),
        ];
        let out = k.sys_read(&reqs, &ctx());
        assert_eq!(out.len(), 2);
        assert!(matches!(out[0], Err(KernelError::InvalidPath(_))));
        assert!(matches!(out[1], Err(KernelError::FileNotFound(_))));
    }

    #[test]
    fn read_batch_single_file_round_trip() {
        let k = kernel_with_backend();
        let c = ctx();
        k.sys_write_one("/r3.txt", &c, b"hi there", 0)
            .expect("write");
        let reqs = vec![rreq("/r3.txt", 0, None)];
        let out = k.sys_read(&reqs, &c);
        assert_eq!(out.len(), 1);
        let r = out[0].as_ref().expect("inner ok");
        assert_eq!(r.data.as_deref().unwrap(), b"hi there");
    }

    #[test]
    fn read_batch_coalesces_same_content_id() {
        let k = kernel_with_backend();
        let c = ctx();
        let payload = b"hello vectored world".to_vec();
        k.sys_write_one("/coalesce.txt", &c, &payload, 0)
            .expect("write");
        let reqs: Vec<_> = (0..5).map(|_| rreq("/coalesce.txt", 0, None)).collect();
        let out = k.sys_read(&reqs, &c);
        assert_eq!(out.len(), 5);
        for r in &out {
            let r = r.as_ref().expect("ok");
            assert_eq!(r.data.as_deref().unwrap(), payload.as_slice());
        }
    }

    #[test]
    fn read_batch_100_distinct_paths_parallel() {
        let k = kernel_with_backend();
        let c = ctx();
        let mut reqs = Vec::with_capacity(100);
        for i in 0..100u32 {
            let path = format!("/p{i:03}.txt");
            let payload = format!("payload-{i}").into_bytes();
            k.sys_write_one(&path, &c, &payload, 0).expect("write");
            reqs.push(rreq(&path, 0, None));
        }
        let out = k.sys_read(&reqs, &c);
        assert_eq!(out.len(), 100);
        for (i, r) in out.iter().enumerate() {
            let r = r.as_ref().expect("ok");
            let expected = format!("payload-{i}").into_bytes();
            assert_eq!(r.data.as_deref().unwrap(), expected.as_slice());
        }
    }

    #[test]
    fn read_batch_range_slicing() {
        let k = kernel_with_backend();
        let c = ctx();
        let payload = b"0123456789".to_vec(); // 10 bytes
        k.sys_write_one("/r.txt", &c, &payload, 0).unwrap();

        for letter in ["a", "b", "c", "d"] {
            k.sys_write_one(&format!("/r_{letter}.txt"), &c, &payload, 0)
                .unwrap();
        }

        let reqs = vec![
            rreq("/r_a.txt", 0, None),
            rreq("/r_b.txt", 3, Some(4)),
            rreq("/r_c.txt", 10, Some(5)),
            rreq("/r_d.txt", 8, Some(50)),
        ];
        let out = k.sys_read(&reqs, &c);
        assert_eq!(
            out[0].as_ref().unwrap().data.as_deref().unwrap(),
            b"0123456789"
        );
        assert_eq!(out[1].as_ref().unwrap().data.as_deref().unwrap(), b"3456");
        assert_eq!(out[2].as_ref().unwrap().data.as_deref().unwrap(), b"");
        assert_eq!(out[3].as_ref().unwrap().data.as_deref().unwrap(), b"89");
    }

    #[test]
    fn read_batch_rejects_pipe_entry() {
        use crate::meta_store::{FileMetadata, DT_PIPE};
        let k = kernel_with_backend();
        let c = ctx();
        let meta = FileMetadata {
            entry_type: DT_PIPE,
            ..FileMetadata::default()
        };
        k.metastore_put("/fake_pipe", meta).expect("put");
        // Need 2+ reqs to hit batch path (single req goes through sys_read_single)
        let reqs2 = vec![rreq("/fake_pipe", 0, None), rreq("/fake_pipe", 0, None)];
        let out = k.sys_read(&reqs2, &c);
        match &out[0] {
            Err(KernelError::IOError(m)) => {
                assert!(m.contains("pipe") || m.contains("stream"), "got: {m}");
            }
            Err(e) => panic!("expected IOError, got different KernelError: {e:?}"),
            Ok(_) => panic!("expected IOError, got Ok"),
        }
    }

    #[test]
    fn read_batch_respects_max_concurrency_one() {
        let k = kernel_with_backend();
        k.set_read_batch_max_concurrency(1);
        let c = ctx();
        for i in 0..10 {
            k.sys_write_one(&format!("/x{i}.txt"), &c, &format!("v{i}").into_bytes(), 0)
                .unwrap();
        }
        let reqs: Vec<_> = (0..10)
            .map(|i| rreq(&format!("/x{i}.txt"), 0, None))
            .collect();
        let out = k.sys_read(&reqs, &c);
        for (i, r) in out.iter().enumerate() {
            assert_eq!(
                r.as_ref().unwrap().data.as_deref().unwrap(),
                format!("v{i}").as_bytes()
            );
        }
    }
}
