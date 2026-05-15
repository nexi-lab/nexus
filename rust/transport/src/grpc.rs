//! Rust-native gRPC server for `NexusVFSService`.
//!
//! Owns the :2028 socket via tonic. Auth is handled by
//! `services::auth::AuthProvider` (pure Rust).
//!
//! `Initialize` and `Call` RPCs are stubbed here (`Unimplemented`).
//! The legacy bridge in `transport::python::grpc_bridge` overrides
//! them for the Python deployment; that module is a delete target.
//!
//! Per-RPC architecture:
//!
//! | RPC                              | Path                                     |
//! | -------------------------------- | ---------------------------------------- |
//! | `Read`/`Write`/`Delete`/`Ping`   | Pure Rust → `Kernel::sys_*`              |
//! | `BatchRead`                      | Pure Rust → `Kernel::sys_read` (batch)   |
//! | `Initialize` / `Call`            | Stubbed; overridden by legacy bridge      |

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

#[cfg(feature = "python")]
use serde_json::Value as JsonValue;
use services::auth::AuthProvider;
use tokio::sync::oneshot;
use tonic::{transport::Server, Request, Response, Status};

use crate::TlsConfig;
#[cfg(feature = "python")]
use kernel::abc::object_store::ObjectStorePosixCapabilities;
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    BatchReadItemResponse, BatchReadRequest, BatchReadResponse, CallRequest, CallResponse,
    DeleteRequest, DeleteResponse, InitializeRequest, InitializeResponse, PingRequest,
    PingResponse, ReadRequest, ReadResponse, WriteRequest, WriteResponse,
};
use kernel::kernel::{Kernel, KernelError, OperationContext};
#[cfg(feature = "python")]
use kernel::vfs_router::extract_zone_from_canonical;

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
/// Auth is delegated to `Arc<dyn AuthProvider>`.  `Initialize` and
/// `Call` RPCs return `Unimplemented` — the legacy bridge in
/// `transport::python::grpc_bridge` wraps this service and overrides
/// those two RPCs for the Python deployment.
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
            KernelError::PipeClosed(m) | KernelError::StreamClosed(m) => {
                (RpcErrorCode::InternalError, m)
            }
            other => (RpcErrorCode::InternalError, format!("{:?}", other)),
        }
    }

    #[cfg(feature = "python")]
    pub(crate) fn rust_mounts_for_initialize(&self) -> serde_json::Map<String, JsonValue> {
        let mut mounts = serde_json::Map::new();
        for canonical in self.kernel.get_mount_points() {
            let (zone_id, mount_point) = extract_zone_from_canonical(&canonical);
            let route = match self.kernel.route(&mount_point, &zone_id) {
                Ok(route) => route,
                Err(err) => {
                    tracing::warn!(
                        "Initialize skipped unroutable mount canonical={} mount_point={} zone_id={}: {:?}",
                        canonical,
                        mount_point,
                        zone_id,
                        err
                    );
                    continue;
                }
            };
            let backend_name = route
                .backend
                .as_ref()
                .map(|backend| backend.name().to_string())
                .unwrap_or_default();
            let rust_native = route.backend.is_some();
            let posix = if route.is_external {
                ObjectStorePosixCapabilities::readonly()
            } else {
                route
                    .backend
                    .as_ref()
                    .map(|backend| backend.posix_capabilities())
                    .unwrap_or_else(ObjectStorePosixCapabilities::readonly)
            };
            mounts.insert(
                mount_point,
                serde_json::json!({
                    "backend_name": backend_name,
                    "backend_type": backend_name,
                    "posix": posix_capability_json(posix),
                    "features": [],
                    "extensions": [],
                    "rust_native": rust_native,
                    "external": route.is_external,
                }),
            );
        }
        mounts
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

#[cfg(feature = "python")]
fn posix_capability_json(caps: ObjectStorePosixCapabilities) -> JsonValue {
    serde_json::json!({
        "read": caps.read,
        "readdir": caps.readdir,
        "stat": caps.stat,
        "write": caps.write,
        "unlink": caps.unlink,
        "mkdir": caps.mkdir,
        "rmdir": caps.rmdir,
        "rename": caps.rename,
        "glob": caps.glob,
    })
}

#[cfg(feature = "python")]
pub(crate) fn auth_json_from_context(ctx: &OperationContext) -> JsonValue {
    let subject_id = ctx
        .subject_id
        .clone()
        .unwrap_or_else(|| ctx.user_id.clone());
    serde_json::json!({
        "authenticated": true,
        "subject_type": ctx.subject_type.clone(),
        "subject_id": subject_id,
        "zone_id": ctx.zone_id.clone(),
        "is_admin": ctx.is_admin,
        "x_agent_id": ctx.agent_id.clone(),
        "zone_perms": ctx.zone_perms.clone(),
    })
}

#[tonic::async_trait]
impl NexusVfsService for VfsServiceImpl {
    async fn initialize(
        &self,
        _req: Request<InitializeRequest>,
    ) -> Result<Response<InitializeResponse>, Status> {
        // Stubbed — the legacy bridge overrides this for Python deployment.
        // Cluster binary doesn't need it.
        Err(Status::unimplemented(
            "Initialize RPC requires the legacy bridge (use transport::python for full server)",
        ))
    }

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
        match self.kernel.sys_read_one(&req.path, &ctx, 5000, 0) {
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
        match self
            .kernel
            .sys_write_one(&req.path, &ctx, &req.content, 0)
        {
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
        match self.kernel.sys_unlink_one(&req.path, &ctx, req.recursive) {
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
                "federation token: use Call dispatch (read_bulk RPC) — typed BatchRead bypasses zone authorization",
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

    async fn call(&self, _req: Request<CallRequest>) -> Result<Response<CallResponse>, Status> {
        // Stubbed — the legacy bridge overrides this for Python deployment.
        // Cluster binary doesn't need it.
        Err(Status::unimplemented(
            "Call RPC requires the legacy bridge (use transport::python for full server)",
        ))
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

    let routes = build_vfs_routes(
        kernel,
        auth,
        cfg.max_message_bytes,
        &cfg.server_version,
    );

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

/// Resolve a `Call.method` string into `(service_name, dispatch_method)`
/// for the Rust dispatch attempt.
#[cfg(feature = "python")]
pub(crate) fn resolve_rust_dispatch(method: &str) -> Option<(&str, &str)> {
    if let Some((svc, bare)) = method.split_once('.') {
        if !svc.is_empty() && !bare.is_empty() {
            return Some((svc, bare));
        }
    }
    if method.starts_with("acp_") {
        return Some(("acp", method));
    }
    if method.starts_with("managed_agent_") {
        return Some(("managed_agent", method));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── resolve_rust_dispatch ──────────────────────────────────────────

    #[test]
    fn dotted_form_splits_on_first_dot() {
        assert_eq!(
            resolve_rust_dispatch("managed_agent.start_session_v1"),
            Some(("managed_agent", "start_session_v1"))
        );
    }

    #[test]
    fn dotted_form_keeps_inner_dots_in_method() {
        assert_eq!(
            resolve_rust_dispatch("acp.session.cancel"),
            Some(("acp", "session.cancel"))
        );
    }

    #[test]
    fn dotted_form_empty_service_falls_through() {
        assert_eq!(resolve_rust_dispatch(".start"), None);
    }

    #[test]
    fn dotted_form_empty_method_falls_through() {
        assert_eq!(resolve_rust_dispatch("acp."), None);
    }

    #[test]
    fn flat_acp_routes_to_acp_with_full_name() {
        assert_eq!(resolve_rust_dispatch("acp_call"), Some(("acp", "acp_call")));
        assert_eq!(resolve_rust_dispatch("acp_kill"), Some(("acp", "acp_kill")));
    }

    #[test]
    fn flat_managed_agent_routes_to_managed_agent_with_full_name() {
        assert_eq!(
            resolve_rust_dispatch("managed_agent_start_session_v1"),
            Some(("managed_agent", "managed_agent_start_session_v1"))
        );
    }

    #[test]
    fn unknown_flat_method_falls_through() {
        assert_eq!(resolve_rust_dispatch("get_capabilities"), None);
        assert_eq!(resolve_rust_dispatch("ping"), None);
        assert_eq!(resolve_rust_dispatch(""), None);
    }

    // ── BatchRead integration ──────────────────────────────────────────

    use std::collections::HashMap;
    use std::sync::Mutex as StdMutex;

    use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use kernel::kernel::vfs_proto::{
        nexus_vfs_service_server::NexusVfsService, BatchReadItemRequest, BatchReadRequest,
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
        )
        .expect("kernel_with_mem_backend: sys_setattr DT_MOUNT");
        k
    }

    #[tokio::test]
    async fn batch_read_returns_per_item_results_in_order() {
        let kernel = std::sync::Arc::new(kernel_with_mem_backend());
        let ctx = OperationContext::new("test", "root", true, None, true);
        kernel
            .sys_write_one("/x.txt", &ctx, b"hello", 0)
            .expect("write");

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
}
