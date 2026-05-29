//! Rust-native gRPC server for `NexusVFSService`.
//!
//! Owns the :2028 socket via tonic. Auth is handled by
//! `transport::auth::AuthProvider` (pure Rust).
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

use crate::auth::AuthProvider;
use tokio::sync::oneshot;
use tonic::{transport::Server, Request, Response, Status};

use crate::TlsConfig;
use kernel::abi::KernelAbi;
use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    BatchReadItemResponse, BatchReadRequest, BatchReadResponse, BatchStatItem, BatchStatRequest,
    BatchStatResponse, BatchWriteItemResponse, BatchWriteRequest, BatchWriteResponse, CallRequest,
    CallResponse, CopyRequest, CopyResponse, DeleteRequest, DeleteResponse, GetXattrBulkItem,
    GetXattrBulkRequest, GetXattrBulkResponse, GetXattrRequest, GetXattrResponse, IpcAck,
    IpcEmpty, IpcHasResponse, IpcPathRequest, LockRequest, LockResponse, MkdirRequest,
    MkdirResponse, PingRequest, PingResponse,
    ReaddirEntry, ReaddirRequest, ReaddirResponse, ReadRequest, ReadResponse, RenameRequest,
    RenameResponse, SetXattrRequest, SetXattrResponse, SetattrRequest, SetattrResponse,
    StatRequest, StatResponse, StreamCollectAllResponse, StreamReadAtRequest, StreamReadAtResponse,
    StreamWriteRequest, StreamWriteResponse, UnlockRequest, UnlockResponse, WatchRequest,
    WatchResponse, WriteRequest, WriteResponse,
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
            auth: Arc::new(crate::auth::NoAuth),
            server_started_at: Instant::now(),
            server_version: Arc::from("test"),
            started_secs: Arc::new(AtomicU64::new(0)),
        }
    }
}

/// Run a blocking kernel operation off the async runtime.
///
/// These VFS handlers are co-hosted (via `with_extra_services`) on the
/// `ZoneManager` tokio runtime that also drives raft consensus, and that
/// runtime is intentionally small (`recommended_worker_threads(
/// MIN_SERVER_RUNTIME_WORKERS)`). Several kernel syscalls block for a long
/// time — `sys_read` on a DT_PIPE/DT_STREAM blocks up to the caller timeout,
/// `sys_write` waits on the VFS write lock up to `vfs_lock_timeout_ms`, and
/// `sys_watch` blocks up to its (default 30s) timeout. Running those inline
/// on a worker parks it, and enough concurrent blocking calls starve the
/// runtime shared by raft + every VFS RPC. Offloading to the blocking pool
/// keeps the async workers free to poll raft, accept connections, and serve
/// other requests.
async fn run_blocking<F, T>(f: F) -> Result<T, Status>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| Status::internal(format!("kernel blocking task join error: {e}")))
}

#[tonic::async_trait]
impl NexusVfsService for VfsServiceImpl {
    async fn read(&self, req: Request<ReadRequest>) -> Result<Response<ReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_read(s))),
        };
        // No federation guard: KernelAbi::sys_read consults ctx.zone_perms via
        // the permission gate (kernel::dispatch.rs:101). The same SSOT runs
        // whether the call entered via typed Read or generic Call.
        //
        // Honor the kernel's read-timeout contract verbatim: `timeout_ms == 0`
        // is O_NONBLOCK (resolve entry / return immediately; empty pipe yields
        // b""), and a non-zero value blocks DT_PIPE/DT_STREAM reads up to N ms.
        // Regular-file reads ignore this value entirely — the VFS read lock uses
        // `vfs_lock_timeout_ms()` (kernel/io.rs), not this parameter.
        //
        // The previous `if req.timeout_ms == 0 { 5000 }` override silently turned
        // every consumer's intended non-blocking pipe poll (all internal pipe/
        // stream consumers pass timeout_ms=0 and pace themselves with their own
        // asyncio.sleep) into a 5s BLOCKING read. That read runs synchronously on
        // a zone-mgr tokio worker, so a handful of polling consumers parked all
        // workers for ~5s at a time, starving the runtime shared by raft + every
        // VFS RPC and stalling unrelated requests ~5s. Pass the value through.
        let timeout_ms = req.timeout_ms;
        // Offload to the blocking pool: a DT_PIPE/DT_STREAM read can block up to
        // `timeout_ms`, and a contended regular-file read waits on the VFS lock —
        // neither must run on the raft-shared async worker.
        let kernel = self.kernel.clone();
        let path = req.path;
        let offset = req.offset;
        let read_res =
            run_blocking(move || KernelAbi::sys_read(&*kernel, &path, &ctx, timeout_ms, offset))
                .await?;
        match read_res {
            Ok(result) => {
                let bytes = result.data.unwrap_or_default();
                Ok(Response::new(ReadResponse {
                    size: bytes.len() as i64,
                    content: bytes,
                    content_id: result.content_id.unwrap_or_default(),
                    gen: result.gen,
                    is_error: false,
                    error_payload: Vec::new(),
                    entry_type: result.entry_type as i32,
                    stream_next_offset: result.stream_next_offset.map(|v| v as u64),
                    post_hook_needed: result.post_hook_needed,
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
                    entry_type: 0,
                    stream_next_offset: None,
                    post_hook_needed: false,
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
        // No federation guard: ctx.zone_perms is enforced inside sys_write's
        // permission gate (kernel::dispatch.rs:101) — same SSOT as Call.
        // Offload: sys_write waits on the VFS write lock (up to
        // vfs_lock_timeout_ms), which must not block a raft-shared worker.
        let kernel = self.kernel.clone();
        let path = req.path;
        let content = req.content;
        let write_res =
            run_blocking(move || KernelAbi::sys_write(&*kernel, &path, &ctx, &content, 0)).await?;
        match write_res {
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
        // No federation guard: ctx.zone_perms is enforced inside sys_unlink's
        // permission gate (kernel::dispatch.rs:101) — same SSOT as Call.
        // Offload: sys_unlink takes the VFS write lock (raft-shared runtime).
        let kernel = self.kernel.clone();
        let path = req.path;
        let recursive = req.recursive;
        let del_res =
            run_blocking(move || KernelAbi::sys_unlink(&*kernel, &path, &ctx, recursive)).await?;
        match del_res {
            Ok(result) => Ok(Response::new(DeleteResponse {
                success: result.hit,
                is_error: false,
                error_payload: Vec::new(),
                entry_type: result.entry_type as u32,
                path: result.path,
                content_id: result.content_id.unwrap_or_default(),
                size: result.size,
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(DeleteResponse {
                    success: false,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                    entry_type: 0,
                    path: String::new(),
                    content_id: String::new(),
                    size: 0,
                }))
            }
        }
    }

    async fn mkdir(
        &self,
        req: Request<MkdirRequest>,
    ) -> Result<Response<MkdirResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_mkdir(s))),
        };
        match KernelConvenience::mkdir(&*self.kernel, &req.path, &ctx, req.parents, req.exist_ok) {
            Ok(r) => Ok(Response::new(MkdirResponse {
                hit: r.hit,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(MkdirResponse {
                    hit: false,
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
        // Offload: sys_rename takes VFS write locks on both paths.
        let kernel = self.kernel.clone();
        let path = req.path;
        let new_path = req.new_path;
        let rename_res =
            run_blocking(move || KernelAbi::sys_rename(&*kernel, &path, &new_path, &ctx)).await?;
        match rename_res {
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
        // Offload: sys_copy takes the VFS write lock on the destination.
        let kernel = self.kernel.clone();
        let src = req.src;
        let dst = req.dst;
        let copy_res = run_blocking(move || KernelAbi::sys_copy(&*kernel, &src, &dst, &ctx)).await?;
        match copy_res {
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
        // Offload: sys_lock blocks acquiring the advisory lock (raft-shared runtime).
        let kernel = self.kernel.clone();
        let path = req.path;
        let lock_id_req = req.lock_id;
        let lock_res =
            run_blocking(move || kernel.sys_lock(&path, &lock_id_req, 1, ttl_secs, "")).await?;
        match lock_res {
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
        // sys_watch blocks up to `timeout_ms` (default 30s) waiting for a file
        // event — never run that on a raft-shared async worker. Offload and
        // project the event into owned Strings inside the blocking closure.
        let kernel = self.kernel.clone();
        let path = req.path;
        let timeout_ms = req.timeout_ms;
        let matched = run_blocking(move || {
            kernel
                .sys_watch(&path, timeout_ms)
                .map(|evt| (evt.path().to_string(), format!("{:?}", evt.event_type)))
        })
        .await?;
        match matched {
            Some((path, event_type)) => Ok(Response::new(WatchResponse {
                matched: true,
                path,
                event_type,
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

    async fn get_xattr(
        &self,
        req: Request<GetXattrRequest>,
    ) -> Result<Response<GetXattrResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_get_xattr(s))),
        };
        match KernelConvenience::get_xattr(&*self.kernel, &req.path, &req.key, kernel::ROOT_ZONE_ID)
        {
            Ok(Some(value)) => Ok(Response::new(GetXattrResponse {
                found: true,
                value,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Ok(None) => Ok(Response::new(GetXattrResponse {
                found: false,
                value: String::new(),
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(GetXattrResponse {
                    found: false,
                    value: String::new(),
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn set_xattr(
        &self,
        req: Request<SetXattrRequest>,
    ) -> Result<Response<SetXattrResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_set_xattr(s))),
        };
        match KernelConvenience::set_xattr(
            &*self.kernel,
            &req.path,
            &req.key,
            req.value,
            kernel::ROOT_ZONE_ID,
        ) {
            Ok(()) => Ok(Response::new(SetXattrResponse {
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(SetXattrResponse {
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn get_xattr_bulk(
        &self,
        req: Request<GetXattrBulkRequest>,
    ) -> Result<Response<GetXattrBulkResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_get_xattr_bulk(s))),
        };
        match KernelConvenience::get_xattr_bulk(
            &*self.kernel,
            &req.paths,
            &req.key,
            kernel::ROOT_ZONE_ID,
        ) {
            Ok(rows) => Ok(Response::new(GetXattrBulkResponse {
                items: rows
                    .into_iter()
                    .map(|(p, v)| GetXattrBulkItem {
                        path: p,
                        found: v.is_some(),
                        value: v.unwrap_or_default(),
                    })
                    .collect(),
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(GetXattrBulkResponse {
                    items: Vec::new(),
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn close_pipe(
        &self,
        req: Request<IpcPathRequest>,
    ) -> Result<Response<IpcAck>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_ipc_ack(s))),
        };
        match self.kernel.close_pipe(&req.path) {
            Ok(()) => Ok(Response::new(IpcAck {
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(IpcAck {
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn has_pipe(
        &self,
        req: Request<IpcPathRequest>,
    ) -> Result<Response<IpcHasResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_ipc_has(s))),
        };
        Ok(Response::new(IpcHasResponse {
            present: self.kernel.has_pipe(&req.path),
            is_error: false,
            error_payload: Vec::new(),
        }))
    }

    async fn close_all_pipes(
        &self,
        req: Request<IpcEmpty>,
    ) -> Result<Response<IpcAck>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_ipc_ack(s))),
        };
        self.kernel.close_all_pipes();
        Ok(Response::new(IpcAck {
            is_error: false,
            error_payload: Vec::new(),
        }))
    }

    async fn close_stream(
        &self,
        req: Request<IpcPathRequest>,
    ) -> Result<Response<IpcAck>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_ipc_ack(s))),
        };
        match self.kernel.close_stream(&req.path) {
            Ok(()) => Ok(Response::new(IpcAck {
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(IpcAck {
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn has_stream(
        &self,
        req: Request<IpcPathRequest>,
    ) -> Result<Response<IpcHasResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_ipc_has(s))),
        };
        Ok(Response::new(IpcHasResponse {
            present: self.kernel.has_stream(&req.path),
            is_error: false,
            error_payload: Vec::new(),
        }))
    }

    async fn stream_write_nowait(
        &self,
        req: Request<StreamWriteRequest>,
    ) -> Result<Response<StreamWriteResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_stream_write(s))),
        };
        match self.kernel.stream_write_nowait(&req.path, &req.data) {
            Ok(offset) => Ok(Response::new(StreamWriteResponse {
                offset: offset as u64,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(StreamWriteResponse {
                    offset: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn stream_read_at(
        &self,
        req: Request<StreamReadAtRequest>,
    ) -> Result<Response<StreamReadAtResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_stream_read(s))),
        };
        if req.blocking {
            match self.kernel.stream_read_at_blocking(
                &req.path,
                req.offset as usize,
                req.timeout_ms,
            ) {
                Ok((data, next)) => Ok(Response::new(StreamReadAtResponse {
                    data,
                    next_offset: next as u64,
                    eof: false,
                    is_error: false,
                    error_payload: Vec::new(),
                })),
                Err(err) => {
                    let (code, msg) = self.map_kernel_err(err);
                    Ok(Response::new(StreamReadAtResponse {
                        data: Vec::new(),
                        next_offset: 0,
                        eof: false,
                        is_error: true,
                        error_payload: encode_rpc_error(code, &msg),
                    }))
                }
            }
        } else {
            match self.kernel.stream_read_at(&req.path, req.offset as usize) {
                Ok(Some((data, next))) => Ok(Response::new(StreamReadAtResponse {
                    data,
                    next_offset: next as u64,
                    eof: false,
                    is_error: false,
                    error_payload: Vec::new(),
                })),
                Ok(None) => Ok(Response::new(StreamReadAtResponse {
                    data: Vec::new(),
                    next_offset: req.offset,
                    eof: true,
                    is_error: false,
                    error_payload: Vec::new(),
                })),
                Err(err) => {
                    let (code, msg) = self.map_kernel_err(err);
                    Ok(Response::new(StreamReadAtResponse {
                        data: Vec::new(),
                        next_offset: 0,
                        eof: false,
                        is_error: true,
                        error_payload: encode_rpc_error(code, &msg),
                    }))
                }
            }
        }
    }

    async fn stream_collect_all(
        &self,
        req: Request<IpcPathRequest>,
    ) -> Result<Response<StreamCollectAllResponse>, Status> {
        let req = req.into_inner();
        let _ctx = match self.resolve_context(&req.auth_token) {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_stream_collect(s))),
        };
        match self.kernel.stream_collect_all(&req.path) {
            Ok(data) => Ok(Response::new(StreamCollectAllResponse {
                data,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(StreamCollectAllResponse {
                    data: Vec::new(),
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
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
        // No federation guard: read_batch composes per-item sys_read on the
        // KernelConvenience SSOT, which honors ctx.zone_perms in the
        // permission gate (kernel::dispatch.rs:101).

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

        // Offload: batched sys_read composes per-item blocking reads.
        let kernel = self.kernel.clone();
        let results = run_blocking(move || kernel.sys_read(&rust_reqs, &ctx)).await?;

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
        // No federation guard: stat_batch goes through metastore-direct path
        // and the per-path sys_stat fallback, both of which inherit the
        // permission gate's zone_perms enforcement.
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
        // No federation guard: write_batch composes per-item KernelConvenience
        // ::write on the SSOT, which honors ctx.zone_perms in the permission
        // gate (kernel::dispatch.rs:101).

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

        // Offload: write_batch composes per-item blocking writes (VFS write lock).
        let kernel = self.kernel.clone();
        let results = run_blocking(move || KernelConvenience::write_batch(&*kernel, &items, &ctx)).await?;

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
        // Generic dispatch runs arbitrary (possibly blocking) kernel work
        // synchronously — offload it off the raft-shared async runtime.
        let kernel = self.kernel.clone();
        let method = req.method;
        let payload = req.payload;
        run_blocking(move || crate::call_dispatch::dispatch(&kernel, &ctx, &method, &payload)).await?
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

    let mut server_builder = lib::transport_primitives::apply_server_limits(Server::builder())
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
        entry_type: 0,
        stream_next_offset: None,
        post_hook_needed: false,
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
        entry_type: 0,
        path: String::new(),
        content_id: String::new(),
        size: 0,
    }
}

fn error_mkdir(status: Status) -> MkdirResponse {
    MkdirResponse {
        hit: false,
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

fn error_ipc_ack(status: Status) -> IpcAck {
    IpcAck {
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_ipc_has(status: Status) -> IpcHasResponse {
    IpcHasResponse {
        present: false,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_stream_write(status: Status) -> StreamWriteResponse {
    StreamWriteResponse {
        offset: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_stream_read(status: Status) -> StreamReadAtResponse {
    StreamReadAtResponse {
        data: Vec::new(),
        next_offset: 0,
        eof: false,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_stream_collect(status: Status) -> StreamCollectAllResponse {
    StreamCollectAllResponse {
        data: Vec::new(),
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_get_xattr(status: Status) -> GetXattrResponse {
    GetXattrResponse {
        found: false,
        value: String::new(),
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_set_xattr(status: Status) -> SetXattrResponse {
    SetXattrResponse {
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_get_xattr_bulk(status: Status) -> GetXattrBulkResponse {
    GetXattrBulkResponse {
        items: Vec::new(),
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
    use kernel::kernel::convenience::{KernelConvenience, MountOptions};
    use kernel::kernel::vfs_proto::{
        nexus_vfs_service_server::NexusVfsService, BatchReadItemRequest, BatchReadRequest,
        BatchWriteItemRequest, BatchWriteRequest, ReadRequest, SetattrRequest, StatRequest,
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
        k.mount(
            "/",
            MountOptions::new("mem")
                .with_backend(backend)
                .with_io_profile(""),
        )
        .expect("kernel_with_mem_backend: mount DT_MOUNT");
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

    /// Regression: a pipe read with `timeout_ms == 0` must be O_NONBLOCK —
    /// return immediately with empty content, never block. A prior bug in this
    /// handler overrode `timeout_ms == 0` to 5000ms, so every internal pipe
    /// consumer's non-blocking poll (skeleton/zoekt/task-dispatch/audit all
    /// pass timeout_ms=0) became a 5s BLOCKING read running synchronously on a
    /// zone-mgr tokio worker. A handful of concurrent pollers then parked all
    /// workers ~5s at a time, starving the runtime shared by raft + every VFS
    /// RPC and stalling unrelated requests ~5s.
    #[tokio::test]
    async fn read_empty_pipe_with_zero_timeout_is_nonblocking() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let svc = VfsServiceImpl::for_test(kernel);

        // Create an empty DT_PIPE (entry_type 3) through the service API.
        let created = svc
            .setattr(tonic::Request::new(SetattrRequest {
                path: "/nexus/pipes/regression-test".into(),
                auth_token: "test-key".into(),
                entry_type: 3,
                capacity: 65_536,
                ..Default::default()
            }))
            .await
            .expect("setattr rpc ok")
            .into_inner();
        assert!(!created.is_error, "pipe create failed: {created:?}");

        // Non-blocking (timeout_ms == 0) read of the empty pipe must return
        // promptly. The starvation bug made this block ~5s.
        let start = std::time::Instant::now();
        let resp = svc
            .read(tonic::Request::new(ReadRequest {
                path: "/nexus/pipes/regression-test".into(),
                auth_token: "test-key".into(),
                timeout_ms: 0,
                ..Default::default()
            }))
            .await
            .expect("read rpc ok")
            .into_inner();
        let elapsed = start.elapsed();

        assert!(!resp.is_error, "non-blocking pipe read should not error");
        assert!(resp.content.is_empty(), "empty pipe should yield no bytes");
        assert!(
            elapsed < std::time::Duration::from_secs(2),
            "timeout_ms=0 pipe read must be non-blocking; took {elapsed:?} \
             (regression: handler overrode 0 -> 5000ms, blocking a worker and \
             starving the raft+VFS runtime)"
        );
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
