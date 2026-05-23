//! Rust-native gRPC server for `NexusVFSService`.
//!
//! Owns the :2028 socket via tonic. Auth is handled by
//! `services::auth::AuthProvider` (pure Rust).
//!
//! Per-RPC architecture:
//!
//! | RPC                              | Path                                     |
//! | -------------------------------- | ---------------------------------------- |
//! | `Read`/`Write`/`Delete`/`Ping`   | Pure Rust → `Kernel::sys_*`              |
//! | `BatchRead`                      | Pure Rust → `Kernel::sys_read` (batch)   |
//! | `Call`                           | Stubbed (`Unimplemented`)                |

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use services::auth::AuthProvider;
use tokio::sync::oneshot;
use tonic::{transport::Server, Request, Response, Status};

use crate::TlsConfig;
use kernel::abi::KernelAbi;
use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    BatchReadItemResponse, BatchReadRequest, BatchReadResponse, BatchStatItem, BatchStatRequest,
    BatchStatResponse, BatchWriteItemResponse, BatchWriteRequest, BatchWriteResponse, CallRequest,
    CallResponse, CopyRequest, CopyResponse, DeleteRequest, DeleteResponse, LockRequest,
    LockResponse, PingRequest, PingResponse, ReaddirEntry, ReaddirRequest, ReaddirResponse,
    ReadRequest, ReadResponse, RenameRequest, RenameResponse, SetattrRequest, SetattrResponse,
    StatRequest, StatResponse, UnlockRequest, UnlockResponse, WatchRequest, WatchResponse,
    WriteRequest, WriteResponse,
};
use kernel::kernel::{Kernel, KernelError, OperationContext};

/// Configuration for the VFS gRPC server.
#[derive(Clone)]
pub struct VfsGrpcConfig {
    pub bind_addr: SocketAddr,
    /// Optional mTLS config (PEM bytes). `None` = plaintext HTTP/2.
    pub tls: Option<TlsConfig>,
    /// Max gRPC message size in bytes (default 64 MiB to match
    /// `contracts::constants::MAX_GRPC_MESSAGE_BYTES`).
    pub max_message_bytes: usize,
    /// Server `version` advertised in `Ping` responses.
    pub server_version: String,
}

/// Handle returned at startup. Dropping it (or calling `shutdown()`)
/// triggers graceful shutdown of the tonic server. The dedicated tokio
/// runtime is dropped with the handle, so the server task is
/// guaranteed to stop.
pub struct VfsGrpcHandle {
    pub(crate) shutdown_tx: Option<oneshot::Sender<()>>,
    pub(crate) runtime: Option<tokio::runtime::Runtime>,
}

impl VfsGrpcHandle {
    pub fn shutdown_blocking(mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        if let Some(rt) = self.runtime.take() {
            rt.shutdown_timeout(std::time::Duration::from_secs(5));
        }
    }
}

impl Drop for VfsGrpcHandle {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        if let Some(rt) = self.runtime.take() {
            rt.shutdown_timeout(std::time::Duration::from_secs(5));
        }
    }
}

/// Core VFS service implementation — pure Rust.
///
/// Auth is delegated to `Arc<dyn AuthProvider>`.  `Call` RPC returns
/// `Unimplemented`.
#[derive(Clone)]
pub(crate) struct VfsServiceImpl {
    pub(crate) kernel: Arc<Kernel>,
    pub(crate) auth: Arc<dyn AuthProvider>,
    pub(crate) server_started_at: Instant,
    pub(crate) server_version: Arc<str>,
    pub(crate) started_secs: Arc<AtomicU64>,
}

impl VfsServiceImpl {
    /// Validate the bearer token via the configured `AuthProvider`.
    pub(crate) fn resolve_context(&self, token: &str) -> Result<OperationContext, Status> {
        self.auth.resolve(token)
    }

    pub(crate) fn map_kernel_err(&self, err: KernelError) -> (RpcErrorCode, String) {
        match err {
            KernelError::FileNotFound(p) => (RpcErrorCode::FileNotFound, p),
            KernelError::PermissionDenied(m) => (RpcErrorCode::PermissionError, m),
            KernelError::InvalidPath(m) => (RpcErrorCode::InvalidPath, m),
            KernelError::BackendError(m) => {
                let lower = m.to_ascii_lowercase();
                if lower.contains("permission")
                    || lower.contains("denied")
                    || lower.contains("read-only")
                {
                    (RpcErrorCode::PermissionError, m)
                } else {
                    (RpcErrorCode::InternalError, m)
                }
            }
            KernelError::PipeClosed(m) | KernelError::StreamClosed(m) => {
                (RpcErrorCode::InternalError, m)
            }
            other => (RpcErrorCode::InternalError, format!("{:?}", other)),
        }
    }

    /// Test-only constructor.
    #[cfg(test)]
    pub(crate) fn for_test(kernel: Arc<Kernel>) -> Self {
        Self {
            kernel,
            auth: Arc::new(services::auth::ApiKeyAuth::new("test-key")),
            server_started_at: Instant::now(),
            server_version: Arc::from("test"),
            started_secs: Arc::new(AtomicU64::new(0)),
        }
    }
}

#[tonic::async_trait]
impl NexusVfsService for VfsServiceImpl {
    async fn read(&self, req: Request<ReadRequest>) -> Result<Response<ReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_read(s))),
        };
        if !ctx.zone_perms.is_empty() {
            return Ok(Response::new(error_read(Status::permission_denied(
                "federation token: use Call dispatch (sys_read RPC) — typed Read bypasses zone authorization",
            ))));
        }
        match KernelAbi::sys_read(&*self.kernel, &req.path, &ctx, 5000, 0) {
            Ok(result) => {
                let bytes = result.data.unwrap_or_default();
                Ok(Response::new(ReadResponse {
                    size: bytes.len() as i64,
                    content: bytes,
                    content_id: result.content_id.unwrap_or_default(),
                    gen: result.gen,
                    is_error: false,
                    error_payload: Vec::new(),
                }))
            }
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(ReadResponse {
                    content: Vec::new(),
                    content_id: String::new(),
                    size: 0,
                    gen: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn write(&self, req: Request<WriteRequest>) -> Result<Response<WriteResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_write(s))),
        };
        if !ctx.zone_perms.is_empty() {
            return Ok(Response::new(error_write(Status::permission_denied(
                "federation token: use Call dispatch (sys_write RPC) — typed Write bypasses zone authorization",
            ))));
        }
        match KernelAbi::sys_write(&*self.kernel, &req.path, &ctx, &req.content, 0) {
            Ok(result) => Ok(Response::new(WriteResponse {
                content_id: result.content_id.unwrap_or_default(),
                size: result.size as i64,
                gen: result.gen,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(WriteResponse {
                    content_id: String::new(),
                    size: 0,
                    gen: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn delete(
        &self,
        req: Request<DeleteRequest>,
    ) -> Result<Response<DeleteResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_delete(s))),
        };
        if !ctx.zone_perms.is_empty() {
            return Ok(Response::new(error_delete(Status::permission_denied(
                "federation token: use Call dispatch (sys_unlink RPC) — typed Delete bypasses zone authorization",
            ))));
        }
        match KernelAbi::sys_unlink(&*self.kernel, &req.path, &ctx, req.recursive) {
            Ok(result) => Ok(Response::new(DeleteResponse {
                success: result.hit,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(DeleteResponse {
                    success: false,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn stat(&self, req: Request<StatRequest>) -> Result<Response<StatResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_stat(s))),
        };
        let zone_id = if req.zone_id.is_empty() {
            ctx.zone_id.as_str()
        } else {
            req.zone_id.as_str()
        };
        // `sys_stat` returns `Option` — `None` is "no such path", a
        // normal result surfaced as `found = false` (not an error).
        match self.kernel.sys_stat(&req.path, zone_id) {
            Some(s) => Ok(Response::new(StatResponse {
                found: true,
                path: s.path,
                size: s.size as i64,
                content_id: s.content_id.unwrap_or_default(),
                mime_type: s.mime_type,
                is_directory: s.is_directory,
                entry_type: s.entry_type as i32,
                mode: s.mode,
                version: s.version,
                gen: s.gen,
                zone_id: s.zone_id.unwrap_or_default(),
                created_at_ms: s.created_at_ms,
                modified_at_ms: s.modified_at_ms,
                last_writer_address: s.last_writer_address.unwrap_or_default(),
                link_target: s.link_target.unwrap_or_default(),
                owner_id: s.owner_id.unwrap_or_default(),
                is_error: false,
                error_payload: Vec::new(),
            })),
            None => Ok(Response::new(StatResponse {
                found: false,
                ..Default::default()
            })),
        }
    }

    async fn readdir(
        &self,
        req: Request<ReaddirRequest>,
    ) -> Result<Response<ReaddirResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_readdir(s))),
        };
        let zone_id = if req.zone_id.is_empty() {
            ctx.zone_id.as_str()
        } else {
            req.zone_id.as_str()
        };
        // `is_admin` comes from the auth-resolved context, never the
        // request — clients can't spoof admin reads of `/__sys__/zones/`.
        let entries = self.kernel.sys_readdir(&req.path, zone_id, ctx.is_admin);
        let mapped: Vec<ReaddirEntry> = entries
            .into_iter()
            .map(|(name, dt)| ReaddirEntry {
                name,
                entry_type: dt as u32,
            })
            .collect();
        Ok(Response::new(ReaddirResponse {
            entries: mapped,
            is_error: false,
            error_payload: Vec::new(),
        }))
    }

    async fn setattr(
        &self,
        req: Request<SetattrRequest>,
    ) -> Result<Response<SetattrResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_setattr(s))),
        };
        let _ = ctx; // resolve auth for permissions / future use

        let zone_id_str = req.zone_id;
        let zone_id = if zone_id_str.is_empty() {
            kernel::ROOT_ZONE_ID
        } else {
            &zone_id_str
        };

        // DT_MOUNT special case — the subprocess kernel already owns its
        // mount table (auto-created from NEXUS_DATA_DIR at startup) and
        // Python can't pass a Rust ObjectStore Arc through the wire. The
        // Python factory still emits sys_setattr(DT_MOUNT) during boot,
        // so we ack synthetically rather than overwrite the live mount
        // with backend=None (which would break all I/O).
        if req.entry_type == 2 {
            return Ok(Response::new(SetattrResponse {
                path: req.path,
                created: false,
                entry_type: req.entry_type,
                is_error: false,
                error_payload: Vec::new(),
            }));
        }

        match self.kernel.sys_setattr(
            &req.path,
            req.entry_type,
            &req.backend_name,
            None, // backend (non-mount entry types don't need one)
            None, // metastore
            None, // raft_backend
            &req.io_profile,
            zone_id,
            req.is_external,
            req.capacity as usize,
            None, // read_fd  — DT_PIPE stdio uses the in-process AcpSubprocess path
            None, // write_fd
            req.mime_type.as_deref(),
            req.modified_at_ms,
            req.content_id.as_deref(),
            req.size,
            req.version,
            req.created_at_ms,
            None, // link_target — DT_LINK creation isn't on the JSON-wire today
            None, // source
            None, // remote_metastore
        ) {
            Ok(r) => Ok(Response::new(SetattrResponse {
                path: r.path,
                created: r.created,
                entry_type: r.entry_type,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(SetattrResponse {
                    path: String::new(),
                    created: false,
                    entry_type: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn rename(
        &self,
        req: Request<RenameRequest>,
    ) -> Result<Response<RenameResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_rename(s))),
        };
        match KernelAbi::sys_rename(&*self.kernel, &req.path, &req.new_path, &ctx) {
            Ok(r) => Ok(Response::new(RenameResponse {
                hit: r.hit,
                success: r.success,
                is_directory: r.is_directory,
                old_content_id: r.old_content_id,
                old_size: r.old_size,
                old_version: r.old_version,
                old_modified_at_ms: r.old_modified_at_ms,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(RenameResponse {
                    hit: false,
                    success: false,
                    is_directory: false,
                    old_content_id: None,
                    old_size: None,
                    old_version: None,
                    old_modified_at_ms: None,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn copy(&self, req: Request<CopyRequest>) -> Result<Response<CopyResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_copy(s))),
        };
        match KernelAbi::sys_copy(&*self.kernel, &req.src, &req.dst, &ctx) {
            Ok(r) => Ok(Response::new(CopyResponse {
                hit: r.hit,
                dst_path: r.dst_path,
                content_id: r.content_id,
                size: r.size,
                version: r.version,
                gen: r.gen,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(CopyResponse {
                    hit: false,
                    dst_path: String::new(),
                    content_id: None,
                    size: 0,
                    version: 0,
                    gen: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn lock(&self, req: Request<LockRequest>) -> Result<Response<LockResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_lock(s))),
        };
        // Match the Call wire surface (mode / max_holders / ttl_secs are
        // hardcoded on the JSON path; expose only when there's a caller
        // that needs to vary them).
        let ttl_secs = req.timeout_ms / 1000 + 1;
        match self.kernel.sys_lock(
            &req.path,
            &req.lock_id,
            kernel::lock_manager::KernelLockMode::Exclusive,
            1,
            ttl_secs,
            "",
        ) {
            Ok(Some(id)) => Ok(Response::new(LockResponse {
                acquired: true,
                lock_id: id,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Ok(None) => Ok(Response::new(LockResponse {
                acquired: false,
                lock_id: String::new(),
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(LockResponse {
                    acquired: false,
                    lock_id: String::new(),
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn unlock(
        &self,
        req: Request<UnlockRequest>,
    ) -> Result<Response<UnlockResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_unlock(s))),
        };
        match self.kernel.sys_unlock(&req.path, &req.lock_id, req.force) {
            Ok(released) => Ok(Response::new(UnlockResponse {
                released,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(UnlockResponse {
                    released: false,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn watch(&self, req: Request<WatchRequest>) -> Result<Response<WatchResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_watch(s))),
        };
        match self.kernel.sys_watch(&req.path, req.timeout_ms) {
            Some(evt) => Ok(Response::new(WatchResponse {
                matched: true,
                path: evt.path().to_string(),
                event_type: format!("{:?}", evt.event_type),
                is_error: false,
                error_payload: Vec::new(),
            })),
            None => Ok(Response::new(WatchResponse {
                matched: false,
                path: String::new(),
                event_type: String::new(),
                is_error: false,
                error_payload: Vec::new(),
            })),
        }
    }

    async fn ping(&self, req: Request<PingRequest>) -> Result<Response<PingResponse>, Status> {
        let ctx = self.resolve_context(&req.into_inner().auth_token)?;
        let uptime = self.server_started_at.elapsed().as_secs() as i64;
        self.started_secs.store(uptime as u64, Ordering::Relaxed);
        Ok(Response::new(PingResponse {
            version: self.server_version.to_string(),
            zone_id: ctx.zone_id,
            uptime_seconds: uptime,
        }))
    }

    async fn batch_read(
        &self,
        req: Request<BatchReadRequest>,
    ) -> Result<Response<BatchReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Err(s),
        };
        if !ctx.zone_perms.is_empty() {
            return Err(Status::permission_denied(
                "federation token: use Call dispatch (BatchRead RPC) — typed BatchRead bypasses zone authorization",
            ));
        }

        let rust_reqs: Vec<kernel::kernel::ReadRequest> = req
            .items
            .into_iter()
            .map(|it| kernel::kernel::ReadRequest {
                path: it.path,
                offset: it.offset,
                len: it.length,
                timeout_ms: 5000,
            })
            .collect();

        let results = self.kernel.sys_read(&rust_reqs, &ctx);

        let max_agg = self.kernel.read_batch_max_aggregate_bytes();
        let mut total = 0usize;
        for r in results.iter().filter_map(|r| r.as_ref().ok()) {
            total = total.saturating_add(r.data.as_ref().map(|b| b.len()).unwrap_or(0));
            if total > max_agg {
                return Err(Status::resource_exhausted(format!(
                    "batch read response {} bytes exceeds {} MB",
                    total,
                    max_agg / (1024 * 1024)
                )));
            }
        }

        let mapped: Vec<BatchReadItemResponse> = results
            .into_iter()
            .map(|r| match r {
                Ok(r) => BatchReadItemResponse {
                    content: r.data.unwrap_or_default(),
                    is_error: false,
                    error_payload: Vec::new(),
                    content_id: r.content_id.unwrap_or_default(),
                    gen: r.gen,
                },
                Err(e) => {
                    let (code, msg) = self.map_kernel_err(e);
                    BatchReadItemResponse {
                        content: Vec::new(),
                        is_error: true,
                        error_payload: encode_rpc_error(code, &msg),
                        content_id: String::new(),
                        gen: 0,
                    }
                }
            })
            .collect();

        Ok(Response::new(BatchReadResponse { results: mapped }))
    }

    async fn batch_stat(
        &self,
        req: Request<BatchStatRequest>,
    ) -> Result<Response<BatchStatResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Err(s),
        };
        if !ctx.zone_perms.is_empty() {
            return Err(Status::permission_denied(
                "federation token: use Call dispatch — typed BatchStat bypasses zone authorization",
            ));
        }
        let zone_id = if req.zone_id.is_empty() {
            ctx.zone_id.as_str()
        } else {
            req.zone_id.as_str()
        };

        // KernelConvenience::stat_batch picks the optimized path on
        // Kernel — single redb read txn via `with_metastore::get_batch`,
        // falling back to per-path sys_stat for implicit dirs / procfs.
        let results = KernelConvenience::stat_batch(&*self.kernel, &req.paths, zone_id);
        let mapped: Vec<BatchStatItem> = results
            .into_iter()
            .map(|opt| match opt {
                Some(s) => BatchStatItem {
                    found: true,
                    path: s.path,
                    size: s.size as i64,
                    content_id: s.content_id.unwrap_or_default(),
                    mime_type: s.mime_type,
                    is_directory: s.is_directory,
                    entry_type: s.entry_type as i32,
                    mode: s.mode,
                    version: s.version,
                    gen: s.gen,
                    zone_id: s.zone_id.unwrap_or_default(),
                    created_at_ms: s.created_at_ms,
                    modified_at_ms: s.modified_at_ms,
                    last_writer_address: s.last_writer_address.unwrap_or_default(),
                    link_target: s.link_target.unwrap_or_default(),
                    owner_id: s.owner_id.unwrap_or_default(),
                },
                None => BatchStatItem {
                    found: false,
                    ..Default::default()
                },
            })
            .collect();
        Ok(Response::new(BatchStatResponse { results: mapped }))
    }

    async fn batch_write(
        &self,
        req: Request<BatchWriteRequest>,
    ) -> Result<Response<BatchWriteResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Err(s),
        };
        if !ctx.zone_perms.is_empty() {
            return Err(Status::permission_denied(
                "federation token: use Call dispatch — typed BatchWrite bypasses zone authorization",
            ));
        }

        // Tier 2 `write_batch`: create-or-overwrite per item, each item
        // independent. One bad path no longer aborts the batch the way
        // the generic `write_batch` Call did (it looped Tier 1 sys_write
        // and `return Err`d on the first failure — and never created
        // missing files). The positional per-item result vector matches
        // the input order.
        let items: Vec<(String, Vec<u8>)> = req
            .items
            .into_iter()
            .map(|it| (it.path, it.content))
            .collect();

        let results = KernelConvenience::write_batch(&*self.kernel, &items, &ctx);

        let mapped: Vec<BatchWriteItemResponse> = results
            .into_iter()
            .map(|r| match r {
                Ok(r) => BatchWriteItemResponse {
                    content_id: r.content_id.unwrap_or_default(),
                    size: r.size as i64,
                    gen: r.gen,
                    version: r.version,
                    is_error: false,
                    error_payload: Vec::new(),
                },
                Err(e) => {
                    let (code, msg) = self.map_kernel_err(e);
                    BatchWriteItemResponse {
                        content_id: String::new(),
                        size: 0,
                        gen: 0,
                        version: 0,
                        is_error: true,
                        error_payload: encode_rpc_error(code, &msg),
                    }
                }
            })
            .collect();

        Ok(Response::new(BatchWriteResponse { results: mapped }))
    }

    async fn call(&self, req: Request<CallRequest>) -> Result<Response<CallResponse>, Status> {
        let req = req.into_inner();
        let ctx = self.resolve_context(&req.auth_token)?;
        crate::call_dispatch::dispatch(&self.kernel, &ctx, &req.method, &req.payload)
    }
}

/// Spawn the VFS gRPC server on a dedicated tokio runtime and return a
/// shutdown handle.
pub fn spawn(
    kernel: Arc<Kernel>,
    cfg: VfsGrpcConfig,
    auth: Arc<dyn AuthProvider>,
) -> Result<VfsGrpcHandle, String> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("nexus-vfs-grpc")
        .enable_all()
        .build()
        .map_err(|e| format!("vfs-grpc runtime: {e}"))?;

    let routes = build_vfs_routes(kernel, auth, cfg.max_message_bytes, &cfg.server_version);

    let mut server_builder = Server::builder()
        .max_concurrent_streams(Some(1024))
        .timeout(std::time::Duration::from_secs(60));

    if let Some(tls) = cfg.tls {
        let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
        let ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
        let tls_cfg = tonic::transport::ServerTlsConfig::new()
            .identity(identity)
            .client_ca_root(ca);
        server_builder = server_builder
            .tls_config(tls_cfg)
            .map_err(|e| format!("TLS config: {e}"))?;
    }

    let router = server_builder.add_routes(routes);

    let (tx, rx) = oneshot::channel::<()>();
    let bind = cfg.bind_addr;
    runtime.spawn(async move {
        let result = router
            .serve_with_shutdown(bind, async move {
                let _ = rx.await;
            })
            .await;
        if let Err(e) = result {
            tracing::error!("VFS gRPC server stopped: {e}");
        }
    });

    Ok(VfsGrpcHandle {
        shutdown_tx: Some(tx),
        runtime: Some(runtime),
    })
}

/// Build the VFS gRPC service as type-erased `tonic::service::Routes`.
///
/// Returns `Routes` (not a concrete generic type) so callers don't
/// need to name `VfsServiceImpl`, keeping it `pub(crate)`.
///
/// Used by:
/// - `nexusd-cluster`: passes the Routes to `ZoneManager` for shared-port co-hosting.
/// - `spawn()`: wraps the Routes in a standalone tonic server.
pub fn build_vfs_routes(
    kernel: Arc<Kernel>,
    auth: Arc<dyn AuthProvider>,
    max_message_bytes: usize,
    server_version: &str,
) -> tonic::service::Routes {
    let svc = VfsServiceImpl {
        kernel,
        auth,
        server_started_at: Instant::now(),
        server_version: Arc::from(server_version),
        started_secs: Arc::new(AtomicU64::new(0)),
    };
    let server = NexusVfsServiceServer::new(svc)
        .max_decoding_message_size(max_message_bytes)
        .max_encoding_message_size(max_message_bytes);
    tonic::service::Routes::new(server)
}

// ── Helpers ──────────────────────────────────────────────────────────

/// Subset of `RPCErrorCode` from `nexus.contracts.rpc_types`.
#[derive(Copy, Clone)]
pub(crate) enum RpcErrorCode {
    InvalidPath = -32004,
    PermissionError = -32003,
    AccessDenied = -32018,
    FileNotFound = -32007,
    ValidationError = -32005,
    Conflict = -32006,
    InternalError = -32603,
}

pub(crate) fn encode_rpc_error(code: RpcErrorCode, message: &str) -> Vec<u8> {
    encode_rpc_error_bytes(code, message)
}

pub(crate) fn encode_rpc_error_bytes(code: RpcErrorCode, message: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "code": code as i64,
        "message": message,
    }))
    .unwrap_or_else(|_| b"{}".to_vec())
}

fn error_read(status: Status) -> ReadResponse {
    ReadResponse {
        content: Vec::new(),
        content_id: String::new(),
        size: 0,
        gen: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_write(status: Status) -> WriteResponse {
    WriteResponse {
        content_id: String::new(),
        size: 0,
        gen: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_delete(status: Status) -> DeleteResponse {
    DeleteResponse {
        success: false,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_readdir(status: Status) -> ReaddirResponse {
    ReaddirResponse {
        entries: Vec::new(),
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_lock(status: Status) -> LockResponse {
    LockResponse {
        acquired: false,
        lock_id: String::new(),
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_unlock(status: Status) -> UnlockResponse {
    UnlockResponse {
        released: false,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_watch(status: Status) -> WatchResponse {
    WatchResponse {
        matched: false,
        path: String::new(),
        event_type: String::new(),
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_rename(status: Status) -> RenameResponse {
    RenameResponse {
        hit: false,
        success: false,
        is_directory: false,
        old_content_id: None,
        old_size: None,
        old_version: None,
        old_modified_at_ms: None,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_copy(status: Status) -> CopyResponse {
    CopyResponse {
        hit: false,
        dst_path: String::new(),
        content_id: None,
        size: 0,
        version: 0,
        gen: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_setattr(status: Status) -> SetattrResponse {
    SetattrResponse {
        path: String::new(),
        created: false,
        entry_type: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_stat(status: Status) -> StatResponse {
    StatResponse {
        found: false,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
        ..Default::default()
    }
}

fn status_to_code(s: &Status) -> RpcErrorCode {
    use tonic::Code;
    match s.code() {
        Code::Unauthenticated => RpcErrorCode::AccessDenied,
        Code::PermissionDenied => RpcErrorCode::PermissionError,
        Code::NotFound => RpcErrorCode::FileNotFound,
        Code::InvalidArgument => RpcErrorCode::InvalidPath,
        _ => RpcErrorCode::InternalError,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::collections::HashMap;
    use std::sync::Mutex as StdMutex;

    use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use kernel::kernel::vfs_proto::{
        nexus_vfs_service_server::NexusVfsService, BatchReadItemRequest, BatchReadRequest,
        BatchWriteItemRequest, BatchWriteRequest, StatRequest,
    };
    use kernel::kernel::Kernel;

    #[derive(Default)]
    struct MemBackend {
        blobs: StdMutex<HashMap<String, Vec<u8>>>,
    }

    impl ObjectStore for MemBackend {
        fn name(&self) -> &str {
            "mem"
        }

        fn write_content(
            &self,
            content: &[u8],
            content_id: &str,
            _ctx: &kernel::kernel::OperationContext,
            offset: u64,
        ) -> Result<WriteResult, StorageError> {
            let mut map = self.blobs.lock().unwrap();
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
            _ctx: &kernel::kernel::OperationContext,
        ) -> Result<Vec<u8>, StorageError> {
            self.blobs
                .lock()
                .unwrap()
                .get(content_id)
                .cloned()
                .ok_or_else(|| StorageError::NotFound(content_id.into()))
        }
    }

    fn kernel_with_mem_backend() -> Kernel {
        let k = Kernel::new();
        let backend: std::sync::Arc<dyn ObjectStore> = std::sync::Arc::new(MemBackend::default());
        k.sys_setattr(
            "/",
            2,
            "mem",
            Some(backend),
            None,
            None,
            "",
            kernel::ROOT_ZONE_ID,
            false,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        .expect("kernel_with_mem_backend: sys_setattr DT_MOUNT");
        k
    }

    #[tokio::test]
    async fn batch_read_returns_per_item_results_in_order() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let ctx = OperationContext::new("test", "root", true, None, true);
        KernelAbi::sys_write(&*kernel, "/x.txt", &ctx, b"hello", 0).expect("write");

        let svc = VfsServiceImpl::for_test(kernel.clone());

        let req = tonic::Request::new(BatchReadRequest {
            auth_token: "test-key".into(),
            items: vec![
                BatchReadItemRequest {
                    path: "/x.txt".into(),
                    offset: 0,
                    length: None,
                },
                BatchReadItemRequest {
                    path: "/missing.txt".into(),
                    offset: 0,
                    length: None,
                },
                BatchReadItemRequest {
                    path: "/x.txt".into(),
                    offset: 1,
                    length: Some(3),
                },
            ],
        });

        let resp = svc.batch_read(req).await.expect("rpc ok").into_inner();
        assert_eq!(resp.results.len(), 3);
        assert!(!resp.results[0].is_error);
        assert_eq!(resp.results[0].content, b"hello");
        assert!(resp.results[1].is_error);
        assert!(!resp.results[2].is_error);
        assert_eq!(resp.results[2].content, b"ell");
    }

    #[tokio::test]
    async fn batch_read_empty_items_returns_empty_results() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let svc = VfsServiceImpl::for_test(kernel);

        let req = tonic::Request::new(BatchReadRequest {
            auth_token: "test-key".into(),
            items: vec![],
        });

        let resp = svc.batch_read(req).await.expect("rpc ok").into_inner();
        assert_eq!(resp.results.len(), 0);
    }

    #[tokio::test]
    async fn batch_write_creates_all_items_and_reports_per_item() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let svc = VfsServiceImpl::for_test(kernel.clone());

        let req = tonic::Request::new(BatchWriteRequest {
            auth_token: "test-key".into(),
            items: vec![
                BatchWriteItemRequest {
                    path: "/a.txt".into(),
                    content: b"alpha".to_vec(),
                },
                BatchWriteItemRequest {
                    path: "/b.txt".into(),
                    content: b"bravo!".to_vec(),
                },
            ],
        });

        let resp = svc.batch_write(req).await.expect("rpc ok").into_inner();
        assert_eq!(resp.results.len(), 2);
        assert!(!resp.results[0].is_error);
        assert_eq!(resp.results[0].size, 5);
        assert!(!resp.results[1].is_error);
        assert_eq!(resp.results[1].size, 6);

        // Tier 2 create-or-overwrite landed the bytes — read /a.txt back.
        let ctx = OperationContext::new("test", "root", true, None, true);
        let read = KernelAbi::sys_read(&*kernel, "/a.txt", &ctx, 5000, 0).expect("read");
        assert_eq!(read.data.unwrap_or_default(), b"alpha");
    }

    #[tokio::test]
    async fn stat_reports_metadata_and_not_found() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let ctx = OperationContext::new("test", "root", true, None, true);
        // Create-or-overwrite so a metastore entry exists for stat.
        let _ = KernelConvenience::write_batch(
            &*kernel,
            &[("/s.txt".to_string(), b"stat-me".to_vec())],
            &ctx,
        );

        let svc = VfsServiceImpl::for_test(kernel);

        let found = svc
            .stat(tonic::Request::new(StatRequest {
                path: "/s.txt".into(),
                auth_token: "test-key".into(),
                zone_id: String::new(),
            }))
            .await
            .expect("rpc ok")
            .into_inner();
        assert!(found.found);
        assert!(!found.is_error);
        assert_eq!(found.path, "/s.txt");
        assert_eq!(found.size, 7);
        assert!(!found.is_directory);

        let missing = svc
            .stat(tonic::Request::new(StatRequest {
                path: "/nope.txt".into(),
                auth_token: "test-key".into(),
                zone_id: String::new(),
            }))
            .await
            .expect("rpc ok")
            .into_inner();
        assert!(!missing.found);
        assert!(!missing.is_error);
    }

    #[tokio::test]
    async fn batch_write_empty_items_returns_empty_results() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let svc = VfsServiceImpl::for_test(kernel);

        let req = tonic::Request::new(BatchWriteRequest {
            auth_token: "test-key".into(),
            items: vec![],
        });

        let resp = svc.batch_write(req).await.expect("rpc ok").into_inner();
        assert_eq!(resp.results.len(), 0);
    }
}
