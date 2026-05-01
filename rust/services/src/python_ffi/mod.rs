//! `python_ffi::PyFfiRouter` — single-service Rust dispatcher that
//! routes wire-form RPC method names into individual Python service
//! instances via PyO3.
//!
//! Replaces the per-service @rpc_expose / dispatch.py / parse_method_params
//! / handle_* chain with a single Rust→Python forward layer.  Tonic
//! Call's `resolve_rust_dispatch` falls through to ("python_ffi", method)
//! after the kernel-syscall + native Rust services don't match;
//! `PyFfiRouter::dispatch` then looks up the method in its HashMap of
//! wire-name → Python service handle and forwards the call.
//!
//! Wire contract preserved.  Pure-Rust ports of underlying business
//! logic (CreditsService DB, OAuth tokens, MCP protocol, ReBAC manager,
//! etc.) follow as separate commits and replace the bridge entry one
//! by one.
//!
//! Boot pattern (per Python service):
//!
//! ```python
//! nexus_runtime.nx_python_ffi_register(
//!     kernel,
//!     ["add_mount", "remove_mount", "list_mounts", ...],  # wire-form names
//!     mount_service_instance,                             # Python service
//! )
//! ```

use std::sync::Arc;

use dashmap::DashMap;
use kernel::kernel::Kernel;
use kernel::service_registry::{RustCallError, RustService};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// Canonical service name in `Kernel::service_registry`.
pub const NAME: &str = "python_ffi";

/// Wire-form method-name → Python service handle.  Concurrent so
/// `register` calls during boot don't block dispatch (although in
/// practice all registration happens during single-threaded boot).
pub struct PyFfiRouter {
    /// Method-name → (Python service instance, attribute name on
    /// the instance).  Attribute name usually equals the wire name
    /// but the indirection lets a wire-form alias resolve to a
    /// differently-named Python method.
    routes: DashMap<String, RouteEntry>,
}

struct RouteEntry {
    py_service: Py<PyAny>,
    attr_name: String,
}

impl PyFfiRouter {
    pub fn new() -> Self {
        Self {
            routes: DashMap::new(),
        }
    }

    /// Install the singleton router under `NAME` in the kernel's
    /// service registry.  Returns the freshly-installed router.
    /// Re-install (e.g. an idempotent boot retry) is rejected by
    /// `register_rust_service`; callers that want the existing one
    /// should hold onto the Arc returned by the first install.
    pub fn install(kernel: &Arc<Kernel>) -> Result<Arc<Self>, String> {
        let router = Arc::new(Self::new());
        kernel.register_rust_service(
            NAME,
            Arc::clone(&router) as Arc<dyn RustService>,
            Vec::new(),
        )?;
        Ok(router)
    }

    /// Register a wire-form name → Python service mapping.  Boot
    /// hooks call this once per @rpc_expose-equivalent method on the
    /// Python service.  `attr_name` defaults to the wire form when
    /// the Python attribute matches; pass a different name when
    /// they diverge.
    pub fn register(
        &self,
        wire_name: impl Into<String>,
        attr_name: impl Into<String>,
        py_service: Py<PyAny>,
    ) {
        let wire = wire_name.into();
        let attr = attr_name.into();
        self.routes.insert(
            wire,
            RouteEntry {
                py_service,
                attr_name: attr,
            },
        );
    }
}

impl Default for PyFfiRouter {
    fn default() -> Self {
        Self::new()
    }
}

impl RustService for PyFfiRouter {
    fn name(&self) -> &str {
        NAME
    }

    fn start(&self) -> Result<(), String> {
        Ok(())
    }

    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        let payload_vec = payload.to_vec();
        Python::attach(|py| -> Result<Vec<u8>, RustCallError> {
            let route = self.routes.get(method).ok_or(RustCallError::NotFound)?;
            let py_service = route.py_service.clone_ref(py);
            let attr_name = route.attr_name.clone();
            drop(route);

            let svc = py_service.bind(py);
            let attr = svc
                .getattr(attr_name.as_str())
                .map_err(|_| RustCallError::NotFound)?;

            // Decode payload as kwargs dict.
            let kwargs = if payload_vec.is_empty() {
                PyDict::new(py)
            } else {
                let json = py
                    .import("json")
                    .map_err(|e| RustCallError::Internal(format!("import json: {e}")))?;
                let obj = json
                    .call_method1("loads", (PyBytes::new(py, &payload_vec),))
                    .map_err(|e| RustCallError::InvalidArgument(format!("json.loads: {e}")))?;
                obj.cast::<PyDict>()
                    .map_err(|e| {
                        RustCallError::InvalidArgument(format!(
                            "payload must be a JSON object: {e}"
                        ))
                    })?
                    .clone()
            };

            // Auth context plumbing: tonic Call handler calls
            // `nexus.server._auth_ctx_local.set_auth(auth_dict)` before
            // `Kernel::dispatch_rust_call`.  We pull it out here, build
            // the OperationContext via `get_operation_context`, and
            // inject as `context=` kwarg if the target method accepts
            // it.  This keeps admin handlers / @rpc_expose'd methods
            // that need OperationContext working through the new
            // dispatch path without the legacy `servicer.py` chain.
            if let Ok(ctx_module) = py.import("nexus.server._auth_ctx_local") {
                if let Ok(get_auth) = ctx_module.getattr("get_auth") {
                    if let Ok(auth_obj) = get_auth.call0() {
                        if !auth_obj.is_none() {
                            if let Ok(deps) = py.import("nexus.server.dependencies") {
                                if let Ok(get_ctx) = deps.getattr("get_operation_context") {
                                    if let Ok(ctx) = get_ctx.call1((auth_obj,)) {
                                        // Set kwargs["context"] only if
                                        // the method's signature accepts
                                        // a "context" param — best-effort
                                        // via inspect.signature.
                                        let inspect_ok = py
                                            .import("inspect")
                                            .and_then(|m| m.getattr("signature"))
                                            .and_then(|f| f.call1((&attr,)));
                                        if let Ok(sig) = inspect_ok {
                                            if let Ok(params) = sig.getattr("parameters") {
                                                if params
                                                    .call_method1("__contains__", ("context",))
                                                    .and_then(|v| v.extract::<bool>())
                                                    .unwrap_or(false)
                                                {
                                                    let _ = kwargs.set_item("context", ctx);
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Invoke the Python attribute.  Coroutine returns get
            // awaited via asyncio.run so the dispatch is synchronous
            // from the caller's perspective (matches the legacy
            // dispatch_method behaviour).
            let result = attr
                .call((), Some(&kwargs))
                .map_err(|e| RustCallError::Internal(format!("call {attr_name}: {e}")))?;

            let is_coro = py
                .import("asyncio")
                .ok()
                .and_then(|m| m.getattr("iscoroutine").ok())
                .and_then(|f| f.call1((&result,)).ok())
                .and_then(|b| b.extract::<bool>().ok())
                .unwrap_or(false);

            let final_result = if is_coro {
                let asyncio = py
                    .import("asyncio")
                    .map_err(|e| RustCallError::Internal(format!("import asyncio: {e}")))?;
                asyncio
                    .call_method1("run", (result,))
                    .map_err(|e| RustCallError::Internal(format!("asyncio.run: {e}")))?
            } else {
                result
            };

            // Encode result as JSON.
            let json = py
                .import("json")
                .map_err(|e| RustCallError::Internal(format!("import json: {e}")))?;
            let s: String = json
                .call_method1("dumps", (&final_result,))
                .map_err(|e| RustCallError::Internal(format!("json.dumps: {e}")))?
                .extract()
                .map_err(|e| RustCallError::Internal(format!("json.dumps str: {e}")))?;
            Ok(s.into_bytes())
        })
    }
}
