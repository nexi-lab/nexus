//! `python_ffi::ServiceBridge` — generic Rust-tier service that
//! forwards `RustService::dispatch(method, payload)` calls into a
//! Python service instance via PyO3.
//!
//! Used to migrate Python `@rpc_expose` services onto the Rust gRPC
//! dispatch path WITHOUT porting their underlying business logic.
//! The Rust tonic Call handler routes by wire-form name prefix into
//! the bridge, the bridge calls back into the Python service, and the
//! @rpc_expose / dispatch.py / parse_method_params chain disappears.
//! Internal pure-Rust ports of each service follow as separate
//! commits.
//!
//! Wire contract preserved:
//!
//!   gRPC `Call(method="<svc_prefix>_<bare>", payload=<json>)`
//!     ↓ tonic resolve_rust_dispatch — prefix → ("<svc>", "<full method>")
//!     ↓ Kernel::dispatch_rust_call → ServiceRegistry::lookup_rust
//!     ↓ ServiceBridge::dispatch
//!     ↓ Python::with_gil — `self._py.<bare_method>(**json.loads(payload))`
//!     ↑ result.to_dict() → serde_json::to_vec
//!
//! Per-service install hooks (one per Python service) plant a
//! ServiceBridge instance in the kernel's registry under a stable
//! name and tell it which prefix to strip from wire-form names before
//! looking up the Python attribute.
//!
//! ## Example
//!
//! ```ignore
//! // From Python (or any cdylib caller):
//! services::python_ffi::install(
//!     &kernel,
//!     /* svc_name */ "mount",
//!     /* wire_prefix */ "",  // wire form already bare (mount_service uses
//!                            // unprefixed names like add_mount/remove_mount;
//!                            // prefix routing in tonic uses the dotted form
//!                            // `mount.add_mount` for those).
//!     /* py_service */ mount_service_pyobj,
//! )?;
//! ```

use std::sync::Arc;

use kernel::kernel::Kernel;
use kernel::service_registry::{RustCallError, RustService};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// Generic FFI dispatcher.  Holds a Python service instance and
/// dispatches `<wire_form>` method names onto it.
pub struct ServiceBridge {
    /// Service name in `Kernel::service_registry`.
    name: String,
    /// Prefix to strip from wire-form method names before looking up
    /// the Python attribute.  Empty string means use the wire-form
    /// name verbatim.  Examples:
    ///
    ///   * Python service = MountService.add_mount
    ///     wire form     = "mount.add_mount" (dotted) → prefix=""
    ///   * Python service = FederationRPCMixin.federation_client_whoami
    ///     wire form     = "federation_client_whoami" → prefix=""
    ///     (the federation prefix is stripped by tonic's
    ///     resolve_rust_dispatch BEFORE the wire-form name reaches
    ///     dispatch — the bridge sees the full method name).
    method_prefix: String,
    /// Python service instance.  Held by Arc<PyObject> so the bridge
    /// outlives any individual call.
    py_service: Py<PyAny>,
}

impl ServiceBridge {
    pub fn new(name: impl Into<String>, method_prefix: impl Into<String>, py_service: Py<PyAny>) -> Self {
        Self {
            name: name.into(),
            method_prefix: method_prefix.into(),
            py_service,
        }
    }

    /// Install this bridge as a Rust service in the kernel's registry.
    /// After install, `Kernel::dispatch_rust_call(name, method, payload)`
    /// hits this bridge, which forwards to Python.
    pub fn install(self, kernel: &Arc<Kernel>) -> Result<(), String> {
        let name = self.name.clone();
        kernel.register_rust_service(&name, Arc::new(self) as Arc<dyn RustService>, Vec::new())
    }

    fn resolve_attr_name<'a>(&self, method: &'a str) -> &'a str {
        if self.method_prefix.is_empty() {
            return method;
        }
        method.strip_prefix(&self.method_prefix).unwrap_or(method)
    }
}

impl RustService for ServiceBridge {
    fn name(&self) -> &str {
        &self.name
    }

    fn start(&self) -> Result<(), String> {
        Ok(())
    }

    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    /// Forward to the Python service via PyO3.
    ///
    /// Wire format: payload is a JSON object whose keys map to
    /// keyword arguments on the Python method.  The Python method's
    /// return value is encoded back via `json.dumps`.  Coroutine
    /// returns are awaited synchronously by spinning a fresh asyncio
    /// loop in the FFI call (matches what the legacy `dispatch_method`
    /// path did, so wire callers see no behavior diff).
    fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        let attr_name = self.resolve_attr_name(method).to_string();
        let payload_vec = payload.to_vec();
        Python::attach(|py| -> Result<Vec<u8>, RustCallError> {
            let svc = self.py_service.bind(py);
            let attr = svc.getattr(attr_name.as_str()).map_err(|_| RustCallError::NotFound)?;

            // Decode payload as a kwargs dict.
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

            // Invoke method.  Async methods get awaited by
            // running the coroutine through asyncio.run.
            let result = attr
                .call((), Some(&kwargs))
                .map_err(|e| RustCallError::Internal(format!("call {attr_name}: {e}")))?;

            // Detect coroutine return — async methods need explicit await.
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
