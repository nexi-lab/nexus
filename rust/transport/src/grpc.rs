//! Rust-native gRPC server for `NexusVFSService`.
//!
//! Owns the :2028 socket via tonic. Symmetric with the Federation
//! gRPC server (also Rust-native).
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
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString};
use pyo3::IntoPyObjectExt;
use serde_json::Value as JsonValue;
use tokio::sync::oneshot;
use tonic::{transport::Server, Request, Response, Status};

use crate::TlsConfig;
use kernel::abc::object_store::ObjectStorePosixCapabilities;
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    BackendCapabilities, BatchReadItemResponse, BatchReadRequest, BatchReadResponse, CallRequest,
    CallResponse, Capabilities, CommandCapabilities, CommandSupport, DeleteRequest, DeleteResponse,
    InitializeRequest, InitializeResponse, PingRequest, PingResponse, PosixCapabilities,
    ReadRequest, ReadResponse, StringFilter, WorkspaceCapabilities, WriteRequest, WriteResponse,
};
use kernel::kernel::{Kernel, KernelError, OperationContext};
use kernel::vfs_router::extract_zone_from_canonical;

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

/// Python callbacks invoked from Rust handlers. Held as `Py<PyAny>`
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
    /// `(request: dict, auth: dict, rust_mounts: dict) -> dict`.
    /// Builds the capability-discovery response for `Initialize`.
    pub initialize: Py<pyo3::PyAny>,
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
    /// `None` only in `#[cfg(test)]` scenarios where the API-key fast-path
    /// is always taken and the Python bridge is never reached.
    bridge: Option<Arc<PyBridge>>,
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
        let bridge = match self.bridge.clone() {
            Some(b) => b,
            None => return Err(Status::unauthenticated("no auth bridge configured")),
        };
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
            Some(auth) if auth.authenticated => {
                let mut ctx = OperationContext::new(
                    &auth.user_id,
                    &auth.zone_id,
                    auth.is_admin,
                    auth.agent_id.as_deref(),
                    /* is_system */ false,
                );
                ctx.zone_perms = auth.zone_perms;
                Ok(ctx)
            }
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

    fn rust_mounts_for_initialize(&self) -> serde_json::Map<String, JsonValue> {
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

    /// Test-only constructor: bypasses the `PyBridge` requirement.
    /// The instance uses the API-key fast-path exclusively — Python is
    /// never invoked. The token `"test-key"` is hard-coded so test
    /// callers just pass `auth_token: "test-key".into()`.
    #[cfg(test)]
    pub(crate) fn for_test(kernel: Arc<Kernel>) -> Self {
        Self {
            kernel,
            api_key: Some(Arc::from("test-key")),
            bridge: None,
            server_started_at: Instant::now(),
            server_version: Arc::from("test"),
            started_secs: Arc::new(AtomicU64::new(0)),
        }
    }
}

fn py_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    Ok(dict
        .get_item(key)?
        .map(|v| v.extract::<bool>())
        .transpose()?
        .unwrap_or(false))
}

fn py_optional_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<bool>> {
    dict.get_item(key)?.map(|v| v.extract::<bool>()).transpose()
}

fn py_string(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    Ok(dict
        .get_item(key)?
        .map(|v| v.extract::<String>())
        .transpose()?
        .unwrap_or_default())
}

fn py_string_list(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<String>> {
    Ok(dict
        .get_item(key)?
        .map(|v| v.extract::<Vec<String>>())
        .transpose()?
        .unwrap_or_default())
}

fn py_posix(value: Option<Bound<'_, PyAny>>) -> PyResult<Option<PosixCapabilities>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    Ok(Some(PosixCapabilities {
        read: py_optional_bool(dict, "read")?,
        readdir: py_optional_bool(dict, "readdir")?,
        stat: py_optional_bool(dict, "stat")?,
        write: py_optional_bool(dict, "write")?,
        unlink: py_optional_bool(dict, "unlink")?,
        mkdir: py_optional_bool(dict, "mkdir")?,
        rmdir: py_optional_bool(dict, "rmdir")?,
        rename: py_optional_bool(dict, "rename")?,
        glob: py_optional_bool(dict, "glob")?,
    }))
}

fn py_string_filter(value: Option<Bound<'_, PyAny>>) -> PyResult<Option<StringFilter>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    Ok(Some(StringFilter {
        allow: py_string_list(dict, "allow")?,
        deny: py_string_list(dict, "deny")?,
    }))
}

fn py_command_support(value: Option<Bound<'_, PyAny>>) -> PyResult<Option<CommandSupport>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    Ok(Some(CommandSupport {
        supported: py_bool(dict, "supported")?,
        filetype: py_string_filter(dict.get_item("filetype")?)?,
    }))
}

fn py_command_capabilities(
    value: Option<Bound<'_, PyAny>>,
) -> PyResult<Option<CommandCapabilities>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    Ok(Some(CommandCapabilities {
        grep: py_command_support(dict.get_item("grep")?)?,
        glob: py_command_support(dict.get_item("glob")?)?,
    }))
}

fn py_workspace_capabilities(
    value: Option<Bound<'_, PyAny>>,
) -> PyResult<Option<WorkspaceCapabilities>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    Ok(Some(WorkspaceCapabilities {
        snapshot: py_bool(dict, "snapshot")?,
        restore: py_bool(dict, "restore")?,
        watch: py_bool(dict, "watch")?,
    }))
}

fn py_backend_capabilities(value: Bound<'_, PyAny>) -> PyResult<BackendCapabilities> {
    let dict = value.cast::<PyDict>()?;
    Ok(BackendCapabilities {
        backend_name: py_string(dict, "backend_name")?,
        backend_type: py_string(dict, "backend_type")?,
        posix: py_posix(dict.get_item("posix")?)?,
        features: py_string_list(dict, "features")?,
        extensions: py_string_list(dict, "extensions")?,
        rust_native: py_bool(dict, "rust_native")?,
        external: py_bool(dict, "external")?,
    })
}

fn py_capabilities(value: Option<Bound<'_, PyAny>>) -> PyResult<Option<Capabilities>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let dict = value.cast::<PyDict>()?;
    let mut backends = std::collections::HashMap::new();
    if let Some(raw_backends) = dict.get_item("backends")? {
        let backend_dict = raw_backends.cast::<PyDict>()?;
        for (key, value) in backend_dict.iter() {
            let mount_point = key.extract::<String>()?;
            backends.insert(mount_point, py_backend_capabilities(value)?);
        }
    }
    Ok(Some(Capabilities {
        posix: py_posix(dict.get_item("posix")?)?,
        commands: py_command_capabilities(dict.get_item("commands")?)?,
        workspace: py_workspace_capabilities(dict.get_item("workspace")?)?,
        backends,
        extensions: py_string_list(dict, "extensions")?,
    }))
}

fn initialize_response_from_py(value: &Bound<'_, PyAny>) -> PyResult<InitializeResponse> {
    let dict = value.cast::<PyDict>()?;
    Ok(InitializeResponse {
        server_name: py_string(dict, "server_name")?,
        server_version: py_string(dict, "server_version")?,
        protocol_version: py_string(dict, "protocol_version")?,
        capabilities: py_capabilities(dict.get_item("capabilities")?)?,
    })
}

fn json_to_py(py: Python<'_>, value: &JsonValue) -> PyResult<Py<PyAny>> {
    let obj = match value {
        JsonValue::Null => py.None().into_bound(py),
        JsonValue::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        JsonValue::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_bound_py_any(py)?
            } else if let Some(u) = n.as_u64() {
                u.into_bound_py_any(py)?
            } else if let Some(f) = n.as_f64() {
                PyFloat::new(py, f).into_any()
            } else {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "JSON number not representable: {n}"
                )));
            }
        }
        JsonValue::String(s) => PyString::new(py, s).into_any(),
        JsonValue::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(json_to_py(py, item)?)?;
            }
            list.into_any()
        }
        JsonValue::Object(map) => {
            let dict = PyDict::new(py);
            for (key, item) in map {
                dict.set_item(key, json_to_py(py, item)?)?;
            }
            dict.into_any()
        }
    };
    Ok(obj.unbind())
}

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

fn auth_json_from_context(ctx: &OperationContext) -> JsonValue {
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
        req: Request<InitializeRequest>,
    ) -> Result<Response<InitializeResponse>, Status> {
        let req = req.into_inner();
        let ctx = self.resolve_context(&req.auth_token).await?;
        let request_json = serde_json::json!({
            "client_name": req.client_name,
            "client_version": req.client_version,
            "protocol_version": req.protocol_version,
        });
        let auth_json = auth_json_from_context(&ctx);
        let rust_mounts_json = JsonValue::Object(self.rust_mounts_for_initialize());
        let bridge = match self.bridge.clone() {
            Some(bridge) => bridge,
            None => {
                return Err(Status::internal(
                    "Initialize RPC requires Python bridge (not configured)",
                ))
            }
        };

        let response = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> PyResult<InitializeResponse> {
                let request_py = json_to_py(py, &request_json)?;
                let auth_py = json_to_py(py, &auth_json)?;
                let rust_mounts_py = json_to_py(py, &rust_mounts_json)?;
                let response = bridge
                    .initialize
                    .call1(py, (request_py, auth_py, rust_mounts_py))?;
                initialize_response_from_py(response.bind(py))
            })
        })
        .await
        .map_err(|e| Status::internal(format!("initialize task: {e}")))?
        .map_err(|e| Status::internal(format!("Initialize dispatch: {e}")))?;

        Ok(Response::new(response))
    }

    async fn read(&self, req: Request<ReadRequest>) -> Result<Response<ReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_read(s))),
        };
        // Issue #3786 / Codex Round 5 finding #1: federation tokens
        // (multi-zone) must use Call dispatch so Python can build a
        // request-scoped zone_perms context.  Typed Read bypasses Python
        // dispatch entirely — accepting it here would let a federation
        // token read across its zone allow-list without enforcement.
        if !ctx.zone_perms.is_empty() {
            return Ok(Response::new(error_read(Status::permission_denied(
                "federation token: use Call dispatch (sys_read RPC) — typed Read bypasses zone authorization",
            ))));
        }
        match self.kernel.sys_read_one(&req.path, &ctx, 5000, 0) {
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
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Ok(Response::new(error_write(s))),
        };
        if !ctx.zone_perms.is_empty() {
            return Ok(Response::new(error_write(Status::permission_denied(
                "federation token: use Call dispatch (sys_write RPC) — typed Write bypasses zone authorization",
            ))));
        }
        // Ignores `content_id` (OCC) — `Write` traffic is REMOTE-profile
        // bulk content. OCC writes go through `Call → occ_write` (still
        // Python). When the OCC service migrates to Rust this site
        // will honor `req.content_id` too.
        match self
            .kernel
            .sys_write_one(&req.path, &ctx, &req.content, /* offset */ 0)
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
        let ctx = match self.resolve_context(&req.auth_token).await {
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

    async fn batch_read(
        &self,
        req: Request<BatchReadRequest>,
    ) -> Result<Response<BatchReadResponse>, Status> {
        let req = req.into_inner();
        let ctx = match self.resolve_context(&req.auth_token).await {
            Ok(c) => c,
            Err(s) => return Err(s),
        };
        // Federation tokens use Call dispatch (same rule as typed Read).
        if !ctx.zone_perms.is_empty() {
            return Err(Status::permission_denied(
                "federation token: use Call dispatch (read_bulk RPC) — typed BatchRead bypasses zone authorization",
            ));
        }

        // proto3 `optional uint64 length` lands as `Option<u64>` on the
        // generated struct, so callers can distinguish "absent" (whole
        // file from offset) from `Some(0)` (zero-byte slice).
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

        // Aggregate-bytes cap is now a kernel config (default 100 MiB).
        // The cap is enforced inside sys_read AFTER Phase A authorization,
        // so denied paths contribute 0 and cannot be probed via
        // resource_exhausted.
        let results = self.kernel.sys_read(&rust_reqs, &ctx);

        // Post-read defense-in-depth — catches paths whose metadata wasn't
        // present in the upfront estimate (cold PAS, virtual resolver,
        // external connector). Reads have already completed; we still
        // refuse to serialize the response over the wire.
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

        let kernel = Arc::clone(&self.kernel);
        let bridge = match self.bridge.clone() {
            Some(b) => b,
            None => {
                return Ok(Response::new(CallResponse {
                    payload: encode_rpc_error_bytes(
                        RpcErrorCode::InternalError,
                        "Call RPC requires Python bridge (not configured)",
                    ),
                    is_error: true,
                }))
            }
        };
        let api_key = self.api_key.clone();
        let payload = req.payload;
        let method = req.method;
        let token = req.auth_token;

        let response_bytes = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> PyResult<(bool, Vec<u8>)> {
                // Build the auth dict for dispatch. API-key path fills
                // a synthetic admin dict (matches Python's
                // `get_operation_context` for static-key holders).
                // Resolved up front so admin-only checks still apply
                // before either the Rust or the Python dispatch path
                // sees the call.
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

                // Try Rust dispatch first; on miss, fall through to
                // the Python `dispatch_method` path so the existing
                // 195 `@rpc_expose` services keep working without
                // changes.
                if let Some((svc_name, rust_method)) = resolve_rust_dispatch(&method) {
                    match kernel.dispatch_rust_call(svc_name, rust_method, &payload) {
                        Some(Ok(bytes)) => return Ok((false, bytes)),
                        Some(Err(kernel::service_registry::RustCallError::InvalidArgument(m))) => {
                            return Ok((
                                true,
                                encode_rpc_error_bytes(RpcErrorCode::InvalidPath, &m),
                            ));
                        }
                        Some(Err(kernel::service_registry::RustCallError::Internal(m))) => {
                            return Ok((
                                true,
                                encode_rpc_error_bytes(RpcErrorCode::InternalError, &m),
                            ));
                        }
                        // NotFound = service exists but doesn't expose
                        // this method; None = name doesn't resolve to
                        // a Rust service. Both cases fall through.
                        Some(Err(kernel::service_registry::RustCallError::NotFound)) | None => {}
                    }
                }

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
        bridge: Some(Arc::new(bridge)),
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
/// for the Rust dispatch attempt. Returning `None` means the method
/// cannot be routed to a Rust service; the call handler falls through
/// to the Python `dispatch_method` path with the original method name.
///
/// Resolution rules:
///   1. Dotted form `service.method` is canonical: split on the first
///      `.` and dispatch the bare method name on that service.
///   2. Flat backward-compat: methods starting with `acp_` route to the
///      `acp` service with the FULL method name preserved (Python
///      `@rpc_expose` names keep the service prefix, e.g. `acp_call`).
///      Methods starting with `managed_agent_` route to the
///      `managed_agent` service with the full name; transitional only,
///      future clients use the dotted form.
///   3. Anything else returns `None` — straight to Python.
fn resolve_rust_dispatch(method: &str) -> Option<(&str, &str)> {
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

// ── Auth result extraction ───────────────────────────────────────────

struct AuthResult {
    authenticated: bool,
    user_id: String,
    zone_id: String,
    is_admin: bool,
    agent_id: Option<String>,
    /// Zone permission grants from federation tokens — list of
    /// (zone_id, perm_chars) pairs.  Single-zone tokens carry an
    /// empty Vec; only multi-zone federation tokens populate this.
    /// PermissionEnforcer + request_zone_perms_scope enforce the grants.
    zone_perms: Vec<(String, String)>,
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
        // zone_perms: federation tokens encode their zone allow-list as
        // a list of [zone_id, perm_chars] pairs.  Missing key is treated
        // as a non-federation token (single-zone with empty grants).  If
        // the key IS present but cannot be decoded, surface the error so
        // we fail closed rather than silently treating it as empty.
        // Codex Round 5 finding #1.
        let zone_perms: Vec<(String, String)> = match dict.get_item("zone_perms")? {
            None => Vec::new(),
            Some(v) => v.extract::<Vec<(String, String)>>().map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "auth dict zone_perms is malformed (expected list of [zone, perms]): {e}"
                ))
            })?,
        };
        let _ = py;
        Ok(Self {
            authenticated,
            user_id,
            zone_id,
            is_admin,
            agent_id,
            zone_perms,
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

use kernel::generated_kernel_abi_pyo3::PyKernel;
use pyo3::exceptions::PyRuntimeError;

/// Python-facing handle for the running gRPC server. Drop or call
/// `shutdown()` to stop. Marked `unsendable` because the inner
/// `tokio::runtime::Runtime` cannot be sent across threads while
/// owned by a Python object (PyO3 enforces single-thread access).
// `Send + Sync` because the inner `tokio::runtime::Runtime` is itself
// `Send + Sync`. `unsendable` was overly conservative — Python tests
// pass the handle around between threads (FastAPI lifespan + sync
// shutdown helper) and that's safe.
#[pyclass]
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
///     initialize: Python sync callable
///                 `(request: dict, auth: dict, rust_mounts: dict) -> dict`.
///                 Bridges the `Initialize` capability handshake to Python.
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
    initialize,
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
    initialize: Py<pyo3::PyAny>,
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
        initialize,
    };

    // `kernel.inner` is `pub(crate)`; the `kernel_arc()` accessor
    // (codegen-emitted on PyKernel) clones the inner Arc through a
    // `pub fn` so this transport-side caller can reach it.
    let kernel_arc = kernel.borrow().kernel_arc();
    let handle = spawn(kernel_arc, cfg, bridge).map_err(PyRuntimeError::new_err)?;

    Ok(PyVfsGrpcServerHandle {
        inner: Some(handle),
    })
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
        // Future versions may use `service.namespace.method`; only the
        // first dot is the split point.
        assert_eq!(
            resolve_rust_dispatch("acp.session.cancel"),
            Some(("acp", "session.cancel"))
        );
    }

    #[test]
    fn dotted_form_empty_service_falls_through() {
        // Leading dot is malformed — fall through.
        assert_eq!(resolve_rust_dispatch(".start"), None);
    }

    #[test]
    fn dotted_form_empty_method_falls_through() {
        // Trailing dot is malformed — fall through.
        assert_eq!(resolve_rust_dispatch("acp."), None);
    }

    #[test]
    fn flat_acp_routes_to_acp_with_full_name() {
        // Python @rpc_expose keeps the `acp_` prefix in the method
        // name, so the full string is what the Rust port will register.
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
        // Methods that don't match either prefix and don't have a dot
        // belong to one of the existing 195 Python @rpc_expose
        // services — go straight to Python.
        assert_eq!(resolve_rust_dispatch("get_capabilities"), None);
        assert_eq!(resolve_rust_dispatch("ping"), None);
        assert_eq!(resolve_rust_dispatch(""), None);
    }

    #[test]
    fn initialize_response_from_py_maps_capability_payload() {
        Python::initialize();
        Python::attach(|py| {
            let root_posix = PyDict::new(py);
            root_posix.set_item("read", true).unwrap();
            root_posix.set_item("readdir", true).unwrap();
            root_posix.set_item("stat", true).unwrap();
            root_posix.set_item("write", true).unwrap();

            let backend_posix = PyDict::new(py);
            backend_posix.set_item("read", true).unwrap();
            backend_posix.set_item("readdir", true).unwrap();
            backend_posix.set_item("stat", true).unwrap();
            backend_posix.set_item("write", false).unwrap();

            let backend = PyDict::new(py);
            backend.set_item("backend_name", "local").unwrap();
            backend.set_item("backend_type", "local").unwrap();
            backend.set_item("posix", &backend_posix).unwrap();
            backend
                .set_item("features", vec!["directory_listing"])
                .unwrap();
            backend.set_item("extensions", PyList::empty(py)).unwrap();
            backend.set_item("rust_native", true).unwrap();
            backend.set_item("external", false).unwrap();

            let backends = PyDict::new(py);
            backends.set_item("/", &backend).unwrap();

            let capabilities = PyDict::new(py);
            capabilities.set_item("posix", &root_posix).unwrap();
            capabilities.set_item("backends", &backends).unwrap();
            capabilities
                .set_item("extensions", PyList::empty(py))
                .unwrap();

            let payload = PyDict::new(py);
            payload.set_item("server_name", "nexus").unwrap();
            payload.set_item("server_version", "0.0").unwrap();
            payload.set_item("protocol_version", "0.1.0").unwrap();
            payload.set_item("capabilities", &capabilities).unwrap();

            let payload_any = payload.into_any();
            let response = initialize_response_from_py(&payload_any).unwrap();
            assert_eq!(response.server_name, "nexus");
            let capabilities = response.capabilities.unwrap();
            assert_eq!(capabilities.posix.unwrap().write, Some(true));
            let backend = capabilities.backends.get("/").unwrap();
            let backend_posix = backend.posix.as_ref().unwrap();
            assert_eq!(backend_posix.read, Some(true));
            assert_eq!(backend_posix.readdir, Some(true));
            assert_eq!(backend_posix.stat, Some(true));
            assert_eq!(backend_posix.write, Some(false));
        });
    }

    #[test]
    fn initialize_response_from_py_preserves_missing_capability_sections() {
        Python::initialize();
        Python::attach(|py| {
            let backend = PyDict::new(py);
            backend.set_item("backend_name", "legacy").unwrap();

            let backends = PyDict::new(py);
            backends.set_item("/", &backend).unwrap();

            let capabilities = PyDict::new(py);
            capabilities.set_item("backends", &backends).unwrap();

            let payload = PyDict::new(py);
            payload.set_item("server_name", "nexus").unwrap();
            payload.set_item("server_version", "0.0").unwrap();
            payload.set_item("protocol_version", "0.1.0").unwrap();
            payload.set_item("capabilities", &capabilities).unwrap();

            let payload_any = payload.into_any();
            let response = initialize_response_from_py(&payload_any).unwrap();
            let capabilities = response.capabilities.unwrap();
            assert!(capabilities.posix.is_none());
            assert!(capabilities.commands.is_none());
            assert!(capabilities.workspace.is_none());
            assert!(capabilities.backends.get("/").unwrap().posix.is_none());
        });
    }

    #[test]
    fn initialize_response_from_py_rejects_malformed_backends() {
        Python::initialize();
        Python::attach(|py| {
            let capabilities = PyDict::new(py);
            capabilities.set_item("backends", "not-a-dict").unwrap();

            let payload = PyDict::new(py);
            payload.set_item("server_name", "nexus").unwrap();
            payload.set_item("capabilities", &capabilities).unwrap();

            let payload_any = payload.into_any();
            assert!(initialize_response_from_py(&payload_any).is_err());
        });
    }

    // ── BatchRead integration ──────────────────────────────────────────
    //
    // Wiring: rather than pulling `kernel`'s `test-utils` Cargo feature
    // (which leaks `MemBackend` into production builds via feature
    // unification), we define a minimal `MemBackend` here, implement
    // `kernel::abc::object_store::ObjectStore` on it, and mount it via
    // the public `Kernel::sys_setattr(DT_MOUNT=2)` path.  Only
    // `write_content` and `read_content` are required; everything else
    // has a default `NotSupported` impl in the trait.

    use std::collections::HashMap;
    use std::sync::Mutex as StdMutex;

    use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
    use kernel::kernel::vfs_proto::{
        nexus_vfs_service_server::NexusVfsService, BatchReadItemRequest, BatchReadRequest,
    };
    use kernel::kernel::Kernel;

    /// Minimal in-memory `ObjectStore` for transport integration tests.
    /// Kept entirely inside `#[cfg(test)]` — never compiled into production.
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

    /// Create a `Kernel` pre-wired with an in-memory backend at `/`.
    /// Uses only the public `sys_setattr(DT_MOUNT)` API — no
    /// cross-crate `test-utils` feature required.
    fn kernel_with_mem_backend() -> Kernel {
        let k = Kernel::new();
        let backend: std::sync::Arc<dyn ObjectStore> = std::sync::Arc::new(MemBackend::default());
        k.sys_setattr(
            "/",
            2, // DT_MOUNT
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
                    length: None, // whole file from offset
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

        // item[0]: /x.txt from offset 0, full file
        assert!(!resp.results[0].is_error, "item[0] should succeed");
        assert_eq!(
            resp.results[0].content, b"hello",
            "item[0] content mismatch"
        );

        // item[1]: /missing.txt — should be a per-item error
        assert!(
            resp.results[1].is_error,
            "item[1] should be error (file not found)"
        );

        // item[2]: /x.txt offset=1, len=3 → "ell"
        assert!(!resp.results[2].is_error, "item[2] should succeed");
        assert_eq!(resp.results[2].content, b"ell", "item[2] slice mismatch");
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
