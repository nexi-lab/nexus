//! Rust-native gRPC server for `NexusVFSService` (Phase 1 of the
//! Python→Rust VFS server migration).
//!
//! Closes the asymmetry between Federation gRPC (server in Rust) and VFS
//! gRPC (server in Python until now). The :2028 socket is now owned by
//! tonic; Python's `grpc.aio.server` is gone.
//!
//! Per-RPC architecture:
//!
//! | RPC                    | Path                                     |
//! | ---------------------- | ---------------------------------------- |
//! | `Read`/`Write`/`Delete`/`Ping` | Pure Rust → `Kernel::sys_*` (zero PyO3 cost) |
//! | `Call`                 | PyO3 callback to Python `dispatch_method` (transitional — moves to Rust as the 195 `@rpc_expose` services migrate) |
//!
//! Auth: API key fast-path is pure Rust (HMAC compare). OIDC bearer
//! tokens delegate to a Python callback (`authlib`/`PyJWT` are
//! Python-only). When `Call` runs the OIDC callback already happened
//! once and the auth dict is forwarded so dispatch can apply
//! search-delegation/zone-scoping.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString};
use tokio::sync::oneshot;
use tonic::{transport::Server, Request, Response, Status};

use crate::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    CallRequest, CallResponse, DeleteRequest, DeleteResponse, PingRequest, PingResponse,
    ReadRequest, ReadResponse, WriteRequest, WriteResponse,
};
use crate::kernel::{Kernel, KernelError, OperationContext};
use transport::TlsConfig;

/// Configuration for the VFS gRPC server.
#[derive(Clone)]
pub struct VfsGrpcConfig {
    pub bind_addr: SocketAddr,
    /// Static API key for fast-path auth (HMAC-CT compare). When set,
    /// matching tokens skip the Python OIDC path entirely.
    pub api_key: Option<Arc<str>>,
    /// Optional mTLS config (PEM bytes). `None` = plaintext HTTP/2.
    pub tls: Option<TlsConfig>,
    /// Max gRPC message size in bytes (default 64 MiB to match
    /// `contracts::constants::MAX_GRPC_MESSAGE_BYTES`).
    pub max_message_bytes: usize,
    /// Server `version` advertised in `Ping` responses.
    pub server_version: String,
}

/// Python callbacks invoked from Rust handlers. Held as `Arc<PyObject>`
/// so the service impl is `Clone`-able across tonic worker tasks.
pub struct PyBridge {
    /// `(token: str) -> dict | None`. Returns the auth result dict
    /// (`authenticated`, `user_id`, `zone_id`, `is_admin`, ...) or
    /// `None` on failure. Synchronous — Python wrapper owns the
    /// asyncio loop bridge for OIDC validation.
    pub authenticate: Py<pyo3::PyAny>,
    /// `(method: str, payload: bytes, auth_result: dict) -> bytes`.
    /// Runs the existing async `dispatch_method` on the FastAPI event
    /// loop and blocks for the JSON-encoded response (success or error
    /// payload). Synchronous from Rust's view.
    pub dispatch_call: Py<pyo3::PyAny>,
}

/// Handle returned to Python at startup. Dropping it (or calling
/// `shutdown()`) triggers graceful shutdown of the tonic server. The
/// dedicated tokio runtime is dropped with the handle, so the server
/// task is guaranteed to stop.
pub struct VfsGrpcHandle {
    shutdown_tx: Option<oneshot::Sender<()>>,
    runtime: Option<tokio::runtime::Runtime>,
}

impl VfsGrpcHandle {
    pub fn shutdown_blocking(mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        // Dropping the runtime stops every spawned task. We give the
        // server a brief window to flush in-flight responses, then let
        // Drop tear down.
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

#[derive(Clone)]
struct VfsServiceImpl {
    kernel: Arc<Kernel>,
    api_key: Option<Arc<str>>,
    bridge: Arc<PyBridge>,
    server_started_at: Instant,
    server_version: Arc<str>,
    /// Best-effort uptime in seconds reported by `Ping`.
    started_secs: Arc<AtomicU64>,
}

impl VfsServiceImpl {
    /// Validate the bearer token and produce an `OperationContext`.
    /// API key fast-path is fully Rust; OIDC tokens delegate to Python.
    async fn resolve_context(&self, token: &str) -> Result<OperationContext, Status> {
        // Fast path: API key constant-time compare. user_id matches the
        // string `get_operation_context` produces from the same auth
        // dict ("api-key-user") — keeps audit/permission context
        // consistent across Read/Write/Delete/Ping (Rust) and Call
        // (Python dispatch).
        if let (Some(ref expected), false) = (&self.api_key, token.is_empty()) {
            if subtle_eq(expected.as_bytes(), token.as_bytes()) {
                return Ok(OperationContext::new(
                    "api-key-user",
                    /* zone_id */ "root",
                    /* is_admin */ true,
                    /* agent_id */ None,
                    /* is_system */ false,
                ));
            }
        }

        // No token + no API key → reject (matches Python servicer:
        // when api_key OR auth_provider is configured, anonymous is denied).
        if token.is_empty() {
            return Err(Status::unauthenticated("Authentication required"));
        }

        // OIDC path: delegate to Python authenticate callback.
        let bridge = self.bridge.clone();
        let token_owned = token.to_string();
        let auth_dict_serialized = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> PyResult<Option<AuthResult>> {
                let result = bridge.authenticate.call1(py, (token_owned,))?;
                if result.is_none(py) {
                    return Ok(None);
                }
                AuthResult::from_py(py, result.bind(py)).map(Some)
            })
        })
        .await
        .map_err(|e| Status::internal(format!("auth task: {e}")))?
        .map_err(|e| Status::unauthenticated(format!("auth backend: {e}")))?;

        match auth_dict_serialized {
            Some(auth) if auth.authenticated => Ok(OperationContext::new(
                &auth.user_id,
                &auth.zone_id,
                auth.is_admin,
                auth.agent_id.as_deref(),
                /* is_system */ false,
            )),
            _ => Err(Status::unauthenticated("Authentication failed")),
        }
    }

    fn map_kernel_err(&self, err: KernelError) -> (RpcErrorCode, String) {
        match err {
            KernelError::FileNotFound(p) => (RpcErrorCode::FileNotFound, p),
            KernelError::PermissionDenied(m) => (RpcErrorCode::PermissionError, m),
            KernelError::InvalidPath(m) => (RpcErrorCode::InvalidPath, m),
            KernelError::PipeClosed(m) | KernelError::StreamClosed(m) => {
                (RpcErrorCode::InternalError, m)
            }
            // KernelError doesn't impl Display — use Debug formatter.
            other => (RpcErrorCode::InternalError, format!("{:?}", other)),
        }
    }
}

#[tonic::async_trait]
impl NexusVfsService for VfsServiceImpl {
    async fn read(&self, req: Request<ReadRequest>) -> Result<Response<ReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_read(s))),
        };
        match self.kernel.sys_read(&req.path, &ctx) {
            Ok(result) => {
                // `sys_read.data` is `Option<Vec<u8>>` because the kernel
                // returns `None` for trie-resolved paths / IPC misses
                // that should fall through to Python. For VFS gRPC,
                // those misses are surfaced as FileNotFound (matches
                // Python servicer behavior — no Python fallback at the
                // RPC boundary).
                let bytes = result.data.unwrap_or_default();
                Ok(Response::new(ReadResponse {
                    size: bytes.len() as i64,
                    content: bytes,
                    etag: result.content_hash.unwrap_or_default(),
                    is_error: false,
                    error_payload: Vec::new(),
                }))
            }
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(ReadResponse {
                    content: Vec::new(),
                    etag: String::new(),
                    size: 0,
                    is_error: true,
                    error_payload: encode_rpc_error(code, &msg),
                }))
            }
        }
    }

    async fn write(&self, req: Request<WriteRequest>) -> Result<Response<WriteResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_write(s))),
        };
        // Phase 1 ignores `etag` (OCC) — `Write` traffic is REMOTE-profile
        // bulk content. OCC writes go through `Call → occ_write` (still
        // Python). When the OCC service migrates to Rust we'll honor
        // `req.etag` here too.
        match self
            .kernel
            .sys_write(&req.path, &ctx, &req.content, /* offset */ 0)
        {
            Ok(result) => Ok(Response::new(WriteResponse {
                etag: result.content_id.unwrap_or_default(),
                size: result.size as i64,
                is_error: false,
                error_payload: Vec::new(),
            })),
            Err(err) => {
                let (code, msg) = self.map_kernel_err(err);
                Ok(Response::new(WriteResponse {
                    etag: String::new(),
                    size: 0,
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
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_delete(s))),
        };
        match self.kernel.sys_unlink(&req.path, &ctx, req.recursive) {
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
        // Ping requires auth so callers can verify their token before
        // doing real work — matches Python servicer.
        let ctx = self.resolve_context(&req.into_inner().auth_token).await?;
        let uptime = self.server_started_at.elapsed().as_secs() as i64;
        self.started_secs.store(uptime as u64, Ordering::Relaxed);
        Ok(Response::new(PingResponse {
            version: self.server_version.to_string(),
            zone_id: ctx.zone_id,
            uptime_seconds: uptime,
        }))
    }

    async fn call(&self, req: Request<CallRequest>) -> Result<Response<CallResponse>, Status> {
        let req = req.into_inner();

        // Resolve auth — same as the typed RPCs. The OIDC dict goes
        // back to Python so dispatch can run search-delegation /
        // zone-scoping checks against the original auth result.
        let auth_dict_blob =
            if let (Some(ref expected), false) = (&self.api_key, req.auth_token.is_empty()) {
                if subtle_eq(expected.as_bytes(), req.auth_token.as_bytes()) {
                    Some(SerializedAuth::api_key())
                } else {
                    None
                }
            } else {
                None
            };

        let bridge = self.bridge.clone();
        let api_key = self.api_key.clone();
        let payload = req.payload;
        let method = req.method;
        let token = req.auth_token;

        let response_bytes = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> PyResult<(bool, Vec<u8>)> {
                // Build the auth dict for dispatch. API-key path fills
                // a synthetic admin dict (matches Python's
                // `get_operation_context` for static-key holders).
                let auth_pyobj = match auth_dict_blob {
                    Some(prebuilt) => prebuilt.into_py_dict(py)?,
                    None if token.is_empty() && api_key.is_none() => {
                        // No auth required (no API key set, no token) —
                        // anonymous dispatch.
                        SerializedAuth::anonymous().into_py_dict(py)?
                    }
                    None => {
                        // OIDC path: hand to Python authenticate.
                        let result = bridge.authenticate.call1(py, (token.clone(),))?;
                        if result.is_none(py) {
                            return Ok((
                                true,
                                encode_rpc_error_bytes(
                                    RpcErrorCode::AccessDenied,
                                    "Authentication failed",
                                ),
                            ));
                        }
                        result.into_pyobject(py)?.into_any().unbind()
                    }
                };

                let payload_bytes = PyBytes::new(py, &payload);
                let resp = bridge
                    .dispatch_call
                    .call1(py, (method.as_str(), payload_bytes, auth_pyobj))?;
                // Result is (is_error: bool, payload: bytes).
                let tup = resp.bind(py).cast::<pyo3::types::PyTuple>()?;
                let is_error: bool = tup.get_item(0)?.extract()?;
                let bytes_obj = tup.get_item(1)?;
                let bytes: &[u8] = bytes_obj.cast::<PyBytes>()?.as_bytes();
                Ok((is_error, bytes.to_vec()))
            })
        })
        .await
        .map_err(|e| Status::internal(format!("call task: {e}")))?;

        match response_bytes {
            Ok((is_error, payload)) => Ok(Response::new(CallResponse { payload, is_error })),
            Err(py_err) => Ok(Response::new(CallResponse {
                payload: encode_rpc_error_bytes(
                    RpcErrorCode::InternalError,
                    &format!("Call dispatch: {py_err}"),
                ),
                is_error: true,
            })),
        }
    }
}

/// Spawn the VFS gRPC server on a dedicated tokio runtime and return a
/// shutdown handle. The runtime is owned by the handle — drop the
/// handle (or call `shutdown_blocking`) to stop the server.
///
/// Re-entrancy: no process-wide guard. Multiple servers can coexist on
/// distinct bind addresses; if two callers ask for the same port, the
/// second one's `tonic::transport::Server::serve_with_shutdown` will
/// surface the OS-level `EADDRINUSE`. This is the right semantics for
/// tests that spin up several FastAPI lifespans inside one Python
/// process — each shutdown drops the handle and frees the port.
pub fn spawn(
    kernel: Arc<Kernel>,
    cfg: VfsGrpcConfig,
    bridge: PyBridge,
) -> Result<VfsGrpcHandle, String> {
    // Dedicated runtime so server lifetime tracks `VfsGrpcHandle`. 2
    // worker threads is sufficient for I/O-bound gRPC handlers; CPU
    // work happens inside `Kernel::sys_*` which uses its own pools.
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("nexus-vfs-grpc")
        .enable_all()
        .build()
        .map_err(|e| format!("vfs-grpc runtime: {e}"))?;

    let svc = VfsServiceImpl {
        kernel,
        api_key: cfg.api_key.clone(),
        bridge: Arc::new(bridge),
        server_started_at: Instant::now(),
        server_version: Arc::from(cfg.server_version.clone()),
        started_secs: Arc::new(AtomicU64::new(0)),
    };

    let mut server_builder = Server::builder()
        .max_concurrent_streams(Some(1024))
        .timeout(std::time::Duration::from_secs(60));

    if let Some(tls) = cfg.tls.clone() {
        let identity = tonic::transport::Identity::from_pem(&tls.cert_pem, &tls.key_pem);
        let ca = tonic::transport::Certificate::from_pem(&tls.ca_pem);
        let tls_cfg = tonic::transport::ServerTlsConfig::new()
            .identity(identity)
            .client_ca_root(ca);
        server_builder = server_builder
            .tls_config(tls_cfg)
            .map_err(|e| format!("TLS config: {e}"))?;
    }

    let max_msg = cfg.max_message_bytes;
    let server = NexusVfsServiceServer::new(svc)
        .max_decoding_message_size(max_msg)
        .max_encoding_message_size(max_msg);

    let (tx, rx) = oneshot::channel::<()>();
    let bind = cfg.bind_addr;
    runtime.spawn(async move {
        let result = server_builder
            .add_service(server)
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

// ── Helpers ──────────────────────────────────────────────────────────

/// Constant-time byte equality (rolled here to avoid pulling `subtle`
/// into the kernel dep tree just for this one call site).
fn subtle_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Subset of `RPCErrorCode` from `nexus.contracts.rpc_types`. Numerical
/// values match the Python enum so JSON-decoded error dicts stay
/// interchangeable.
#[derive(Copy, Clone)]
enum RpcErrorCode {
    InvalidPath = -32004,
    PermissionError = -32003,
    AccessDenied = -32018,
    FileNotFound = -32007,
    InternalError = -32603,
}

fn encode_rpc_error(code: RpcErrorCode, message: &str) -> Vec<u8> {
    encode_rpc_error_bytes(code, message)
}

fn encode_rpc_error_bytes(code: RpcErrorCode, message: &str) -> Vec<u8> {
    // Mirror `nexus.lib.rpc_codec.encode_rpc_message({code, message})`
    // — JSON dict, no special-typed wrappers (error dicts have no
    // `bytes` / `datetime` / `timedelta` fields).
    serde_json::to_vec(&serde_json::json!({
        "code": code as i64,
        "message": message,
    }))
    .unwrap_or_else(|_| b"{}".to_vec())
}

fn error_read(status: Status) -> ReadResponse {
    ReadResponse {
        content: Vec::new(),
        etag: String::new(),
        size: 0,
        is_error: true,
        error_payload: encode_rpc_error_bytes(status_to_code(&status), status.message()),
    }
}

fn error_write(status: Status) -> WriteResponse {
    WriteResponse {
        etag: String::new(),
        size: 0,
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

// ── Auth result extraction ───────────────────────────────────────────

struct AuthResult {
    authenticated: bool,
    user_id: String,
    zone_id: String,
    is_admin: bool,
    agent_id: Option<String>,
}

impl AuthResult {
    /// Read the auth-result dict using the same keys the Python
    /// dispatch path expects (`server.dependencies.get_operation_context`):
    /// `subject_id` is the user identity, `x_agent_id` is the optional
    /// agent override. Don't rename to `user_id` here — the dict
    /// shape is the wire contract between auth providers and dispatch.
    fn from_py(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        let dict = obj.cast::<PyDict>()?;
        let authenticated = dict
            .get_item("authenticated")?
            .map(|v| v.extract::<bool>())
            .transpose()?
            .unwrap_or(false);
        let user_id = dict
            .get_item("subject_id")?
            .map(|v| v.extract::<String>())
            .transpose()?
            .unwrap_or_else(|| "anonymous".to_string());
        let zone_id = dict
            .get_item("zone_id")?
            .map(|v| v.extract::<String>())
            .transpose()?
            .unwrap_or_else(|| "root".to_string());
        let is_admin = dict
            .get_item("is_admin")?
            .map(|v| v.extract::<bool>())
            .transpose()?
            .unwrap_or(false);
        // Agent identity: `subject_type=="agent"` means subject_id IS
        // the agent_id (Python does this remap inside
        // get_operation_context). For typed RPCs that don't run dispatch
        // we mirror that here so kernel context is consistent.
        let subject_type = dict
            .get_item("subject_type")?
            .map(|v| v.extract::<String>())
            .transpose()?
            .unwrap_or_default();
        let agent_id = if subject_type == "agent" {
            Some(user_id.clone())
        } else {
            dict.get_item("x_agent_id")?
                .map(|v| v.extract::<Option<String>>())
                .transpose()?
                .flatten()
        };
        let _ = py;
        Ok(Self {
            authenticated,
            user_id,
            zone_id,
            is_admin,
            agent_id,
        })
    }
}

/// Pre-built auth dicts handed to dispatch when Rust resolves auth
/// without round-tripping through Python. Key shape matches what
/// `nexus.server.dependencies.get_operation_context` expects:
/// `subject_type` / `subject_id` / `zone_id` / `is_admin`.
struct SerializedAuth {
    authenticated: bool,
    subject_id: &'static str,
    zone_id: &'static str,
    is_admin: bool,
}

impl SerializedAuth {
    /// Auth dict for the static-API-key fast path. Mirrors what the
    /// Python ``VFSCallDispatcher._authenticate`` returns when
    /// ``hmac.compare_digest`` matches — same admin context, same
    /// "api-key-user" subject id.
    fn api_key() -> Self {
        Self {
            authenticated: true,
            subject_id: "api-key-user",
            zone_id: "root",
            is_admin: true,
        }
    }

    fn anonymous() -> Self {
        Self {
            authenticated: true,
            subject_id: "anonymous",
            zone_id: "root",
            is_admin: false,
        }
    }

    fn into_py_dict(self, py: Python<'_>) -> PyResult<Py<pyo3::PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("authenticated", self.authenticated)?;
        dict.set_item("subject_type", PyString::new(py, "user"))?;
        dict.set_item("subject_id", PyString::new(py, self.subject_id))?;
        dict.set_item("zone_id", PyString::new(py, self.zone_id))?;
        dict.set_item("is_admin", self.is_admin)?;
        Ok(dict.into_pyobject(py)?.into_any().unbind())
    }
}

// ── PyO3 binding ─────────────────────────────────────────────────────

use crate::generated_kernel_abi_pyo3::PyKernel;
use pyo3::exceptions::PyRuntimeError;

/// Python-facing handle for the running gRPC server. Drop or call
/// `shutdown()` to stop. Marked `unsendable` because the inner
/// `tokio::runtime::Runtime` cannot be sent across threads while
/// owned by a Python object (PyO3 enforces single-thread access).
// `Send + Sync` because the inner `tokio::runtime::Runtime` is itself
// `Send + Sync`. `unsendable` was overly conservative — Python tests
// pass the handle around between threads (FastAPI lifespan + sync
// shutdown helper) and that's safe.
#[pyclass(name = "VfsGrpcServerHandle")]
pub struct PyVfsGrpcServerHandle {
    inner: Option<VfsGrpcHandle>,
}

#[pymethods]
impl PyVfsGrpcServerHandle {
    /// Stop the server gracefully. Idempotent — safe to call from
    /// FastAPI shutdown hook even if the server already stopped.
    fn shutdown(&mut self) {
        if let Some(handle) = self.inner.take() {
            handle.shutdown_blocking();
        }
    }

    fn __repr__(&self) -> String {
        match &self.inner {
            Some(_) => "VfsGrpcServerHandle(running)".to_string(),
            None => "VfsGrpcServerHandle(stopped)".to_string(),
        }
    }
}

/// Start the Rust-native VFS gRPC server. Replaces Python's
/// `grpc.aio.server()` — `:2028` is owned by tonic from boot.
///
/// Args:
///     kernel: PyKernel — the running Rust kernel; server holds an
///             `Arc<Kernel>` clone so syscalls dispatch directly.
///     bind_addr: e.g. "0.0.0.0:2028" or "127.0.0.1:2028".
///     api_key: optional static bearer token for fast-path auth.
///     tls_cert_pem / tls_key_pem / tls_ca_pem: PEM bytes for mTLS.
///                  Either all three are provided or all are None.
///     server_version: string echoed in `Ping` responses.
///     authenticate: Python sync callable `(token: str) -> dict | None`.
///                   Called for OIDC tokens that don't match `api_key`.
///                   Must be safe to call under the GIL from a tokio
///                   blocking thread.
///     dispatch_call: Python sync callable
///                    `(method: str, payload: bytes, auth: dict) -> (is_error: bool, payload: bytes)`.
///                    Bridges the generic `Call` RPC to Python's
///                    `dispatch_method` until the 195 services migrate.
#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (
    kernel,
    bind_addr,
    api_key,
    tls_cert_pem,
    tls_key_pem,
    tls_ca_pem,
    server_version,
    authenticate,
    dispatch_call,
))]
pub fn start_vfs_grpc_server(
    kernel: &Bound<'_, PyKernel>,
    bind_addr: &str,
    api_key: Option<String>,
    tls_cert_pem: Option<Vec<u8>>,
    tls_key_pem: Option<Vec<u8>>,
    tls_ca_pem: Option<Vec<u8>>,
    server_version: String,
    authenticate: Py<pyo3::PyAny>,
    dispatch_call: Py<pyo3::PyAny>,
) -> PyResult<PyVfsGrpcServerHandle> {
    let parsed: SocketAddr = bind_addr
        .parse()
        .map_err(|e| PyRuntimeError::new_err(format!("invalid bind_addr {bind_addr}: {e}")))?;

    let tls = match (tls_cert_pem, tls_key_pem, tls_ca_pem) {
        (Some(cert), Some(key), Some(ca)) => Some(TlsConfig {
            cert_pem: cert,
            key_pem: key,
            ca_pem: ca,
        }),
        (None, None, None) => None,
        _ => {
            return Err(PyRuntimeError::new_err(
                "tls_cert_pem / tls_key_pem / tls_ca_pem must be all-set or all-None",
            ))
        }
    };

    // Match Python servicer's MAX_GRPC_MESSAGE_BYTES (64 MiB).
    const MAX_MSG: usize = 64 * 1024 * 1024;

    let cfg = VfsGrpcConfig {
        bind_addr: parsed,
        api_key: api_key.map(Arc::from),
        tls,
        max_message_bytes: MAX_MSG,
        server_version,
    };

    let bridge = PyBridge {
        authenticate,
        dispatch_call,
    };

    // Phase 4: file moved from `kernel/src/grpc_server.rs` into
    // `transport/src/grpc.rs`; `kernel.inner` is `pub(crate)` so the
    // pre-Phase-4 direct field access broke at the crate boundary.
    // Replaced with the `kernel_arc()` accessor (codegen-emitted on
    // PyKernel) — same Arc clone, just goes through a `pub fn`.
    let kernel_arc = kernel.borrow().kernel_arc();
    let handle = spawn(kernel_arc, cfg, bridge).map_err(PyRuntimeError::new_err)?;

    Ok(PyVfsGrpcServerHandle {
        inner: Some(handle),
    })
}
