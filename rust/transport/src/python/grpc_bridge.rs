//! **LEGACY** PyO3 bridge for VFS gRPC — `Initialize` + `Call` RPCs + Python auth.
//!
//! Temporary shim: wraps the pure-Rust `grpc::VfsServiceImpl` with a
//! `LegacyLegacyLegacyPyBridgedVfsService` that intercepts `Initialize` and `Call`
//! RPCs, delegating them to Python callbacks via `LegacyLegacyPyBridge`.
//! All other RPCs (Read / Write / Delete / Ping / BatchRead) pass
//! through unchanged.
//!
//! **Delete target**: this entire module goes away once the remaining
//! 195 `@rpc_expose` Python services migrate to Rust (next PR).

use std::net::SocketAddr;
use std::sync::atomic::AtomicU64;
use std::sync::Arc;
use std::time::Instant;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString, PyTuple};
use pyo3::IntoPyObjectExt;
use serde_json::Value as JsonValue;

use kernel::generated_kernel_abi_pyo3::PyKernel;
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::NexusVfsService, BackendCapabilities, BatchReadRequest,
    BatchReadResponse, CallRequest, CallResponse, Capabilities, CommandCapabilities,
    CommandSupport, DeleteRequest, DeleteResponse, InitializeRequest, InitializeResponse,
    PingRequest, PingResponse, PosixCapabilities, ReadRequest, ReadResponse, StringFilter,
    WorkspaceCapabilities, WriteRequest, WriteResponse,
};
use kernel::kernel::OperationContext;
use pyo3::exceptions::PyRuntimeError;
use services::auth::AuthProvider;
use tonic::{Request, Response, Status};

use crate::grpc::{
    auth_json_from_context, encode_rpc_error_bytes, resolve_rust_dispatch, RpcErrorCode,
    VfsGrpcConfig, VfsGrpcHandle, VfsServiceImpl,
};
use crate::TlsConfig;

// ── LegacyPyBridge ────────────────────────────────────────────────────────

/// Python callbacks invoked from Rust handlers.
pub(crate) struct LegacyPyBridge {
    pub dispatch_call: Py<pyo3::PyAny>,
    pub initialize: Py<pyo3::PyAny>,
}

// ── LegacyLegacyPyBridgeAuth ────────────────────────────────────────────────────

/// Auth provider that combines API-key fast-path with Python OIDC.
pub(crate) struct LegacyLegacyPyBridgeAuth {
    api_key: Option<Arc<str>>,
    authenticate: Py<pyo3::PyAny>,
}

impl LegacyLegacyPyBridgeAuth {
    pub fn new(api_key: Option<Arc<str>>, authenticate: Py<pyo3::PyAny>) -> Self {
        Self {
            api_key,
            authenticate,
        }
    }
}

impl AuthProvider for LegacyLegacyPyBridgeAuth {
    fn resolve(&self, token: &str) -> Result<OperationContext, Status> {
        // Fast path: API key constant-time compare.
        if let (Some(ref expected), false) = (&self.api_key, token.is_empty()) {
            if subtle_eq(expected.as_bytes(), token.as_bytes()) {
                return Ok(OperationContext::new(
                    "api-key-user",
                    "root",
                    true,
                    None,
                    false,
                ));
            }
        }

        if token.is_empty() {
            return Err(Status::unauthenticated("Authentication required"));
        }

        // OIDC path: delegate to Python.
        let token_owned = token.to_string();
        let auth_fn = &self.authenticate;

        // `Python::attach` is synchronous — borrows `self.authenticate`
        // through the closure. This is safe because `resolve` returns
        // before `self` is dropped.
        let auth_result = Python::attach(|py| -> PyResult<Option<AuthResult>> {
            let result = auth_fn.call1(py, (token_owned,))?;
            if result.is_none(py) {
                return Ok(None);
            }
            AuthResult::from_py(py, result.bind(py)).map(Some)
        })
        .map_err(|e| Status::unauthenticated(format!("auth backend: {e}")))?;

        match auth_result {
            Some(auth) if auth.authenticated => {
                let mut ctx = OperationContext::new(
                    &auth.user_id,
                    &auth.zone_id,
                    auth.is_admin,
                    auth.agent_id.as_deref(),
                    false,
                );
                ctx.zone_perms = auth.zone_perms;
                Ok(ctx)
            }
            _ => Err(Status::unauthenticated("Authentication failed")),
        }
    }
}

/// Constant-time byte equality.
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

// ── AuthResult extraction ───────────────────────────────────────────

struct AuthResult {
    authenticated: bool,
    user_id: String,
    zone_id: String,
    is_admin: bool,
    agent_id: Option<String>,
    zone_perms: Vec<(String, String)>,
}

impl AuthResult {
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

// ── SerializedAuth (for Call RPC) ───────────────────────────────────

struct SerializedAuth {
    authenticated: bool,
    subject_id: &'static str,
    zone_id: &'static str,
    is_admin: bool,
}

impl SerializedAuth {
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

// ── LegacyLegacyPyBridgedVfsService ─────────────────────────────────────────────

/// Wraps `VfsServiceImpl` with Python-bridged `Initialize` and `Call`.
#[derive(Clone)]
pub(crate) struct LegacyLegacyPyBridgedVfsService {
    inner: VfsServiceImpl,
    bridge: Arc<LegacyPyBridge>,
    /// Separate copy of api_key for Call's auth-dict building.
    api_key: Option<Arc<str>>,
}

#[tonic::async_trait]
impl NexusVfsService for LegacyLegacyPyBridgedVfsService {
    async fn initialize(
        &self,
        req: Request<InitializeRequest>,
    ) -> Result<Response<InitializeResponse>, Status> {
        let req = req.into_inner();
        let ctx = self.inner.resolve_context(&req.auth_token)?;
        let request_json = serde_json::json!({
            "client_name": req.client_name,
            "client_version": req.client_version,
            "protocol_version": req.protocol_version,
        });
        let auth_json = auth_json_from_context(&ctx);
        let rust_mounts_json = JsonValue::Object(self.inner.rust_mounts_for_initialize());
        let bridge = self.bridge.clone();

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
        self.inner.read(req).await
    }

    async fn write(&self, req: Request<WriteRequest>) -> Result<Response<WriteResponse>, Status> {
        self.inner.write(req).await
    }

    async fn delete(
        &self,
        req: Request<DeleteRequest>,
    ) -> Result<Response<DeleteResponse>, Status> {
        self.inner.delete(req).await
    }

    async fn ping(&self, req: Request<PingRequest>) -> Result<Response<PingResponse>, Status> {
        self.inner.ping(req).await
    }

    async fn batch_read(
        &self,
        req: Request<BatchReadRequest>,
    ) -> Result<Response<BatchReadResponse>, Status> {
        self.inner.batch_read(req).await
    }

    async fn call(&self, req: Request<CallRequest>) -> Result<Response<CallResponse>, Status> {
        let req = req.into_inner();

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

        let kernel = Arc::clone(&self.inner.kernel);
        let bridge = self.bridge.clone();
        let api_key = self.api_key.clone();
        let payload = req.payload;
        let method = req.method;
        let token = req.auth_token;
        let auth_provider = self.inner.auth.clone();

        let response_bytes = tokio::task::spawn_blocking(move || {
            Python::attach(|py| -> PyResult<(bool, Vec<u8>)> {
                let auth_pyobj = match auth_dict_blob {
                    Some(prebuilt) => prebuilt.into_py_dict(py)?,
                    None if token.is_empty() && api_key.is_none() => {
                        SerializedAuth::anonymous().into_py_dict(py)?
                    }
                    None => {
                        // OIDC path: resolve via the auth provider, then
                        // build a Python dict from the result.
                        match auth_provider.resolve(&token) {
                            Ok(ctx) => {
                                let auth_json = auth_json_from_context(&ctx);
                                json_to_py(py, &auth_json)?
                            }
                            Err(_) => {
                                return Ok((
                                    true,
                                    encode_rpc_error_bytes(
                                        RpcErrorCode::AccessDenied,
                                        "Authentication failed",
                                    ),
                                ));
                            }
                        }
                    }
                };

                // Try Rust dispatch first.
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
                        Some(Err(kernel::service_registry::RustCallError::NotFound)) | None => {}
                    }
                }

                let payload_bytes = PyBytes::new(py, &payload);
                let resp = bridge
                    .dispatch_call
                    .call1(py, (method.as_str(), payload_bytes, auth_pyobj))?;
                let tup = resp.bind(py).cast::<PyTuple>()?;
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

// ── PyO3 binding ────────────────────────────────────────────────────

/// Python-facing handle for the running gRPC server.
#[pyclass]
pub struct PyVfsGrpcServerHandle {
    inner: Option<VfsGrpcHandle>,
}

#[pymethods]
impl PyVfsGrpcServerHandle {
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

/// Start the Rust-native VFS gRPC server with Python bridge.
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

    const MAX_MSG: usize = 64 * 1024 * 1024;
    let api_key_arc: Option<Arc<str>> = api_key.map(|k| Arc::from(k.as_str()));

    let cfg = VfsGrpcConfig {
        bind_addr: parsed,
        tls,
        max_message_bytes: MAX_MSG,
        server_version,
    };

    let auth: Arc<dyn AuthProvider> = Arc::new(LegacyLegacyPyBridgeAuth::new(
        api_key_arc.clone(),
        authenticate,
    ));

    let bridge = Arc::new(LegacyPyBridge {
        dispatch_call,
        initialize,
    });

    let kernel_arc = kernel.borrow().kernel_arc();
    let handle =
        spawn_bridged(kernel_arc, cfg, auth, bridge, api_key_arc).map_err(PyRuntimeError::new_err)?;

    Ok(PyVfsGrpcServerHandle {
        inner: Some(handle),
    })
}

/// Spawn a Python-bridged VFS gRPC server (Initialize + Call via PyO3).
fn spawn_bridged(
    kernel: Arc<kernel::kernel::Kernel>,
    cfg: VfsGrpcConfig,
    auth: Arc<dyn AuthProvider>,
    bridge: Arc<LegacyPyBridge>,
    api_key: Option<Arc<str>>,
) -> Result<VfsGrpcHandle, String> {
    use kernel::kernel::vfs_proto::nexus_vfs_service_server::NexusVfsServiceServer;
    use tonic::transport::Server;

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("nexus-vfs-grpc")
        .enable_all()
        .build()
        .map_err(|e| format!("vfs-grpc runtime: {e}"))?;

    let inner = VfsServiceImpl {
        kernel,
        auth,
        server_started_at: Instant::now(),
        server_version: Arc::from(cfg.server_version.clone()),
        started_secs: Arc::new(AtomicU64::new(0)),
    };

    let svc = LegacyLegacyPyBridgedVfsService {
        inner,
        bridge,
        api_key,
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

    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
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

// ── Python dict helpers ─────────────────────────────────────────────

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
