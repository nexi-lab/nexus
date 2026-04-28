//! Hand-written PyO3 surface for AcpService.
//!
//! Three free functions exposed from the cdylib (registered in
//! `lib.rs`'s `#[pymodule]`):
//!
//!   * `nx_acp_install(py_kernel, default_zone)` — installs an
//!     `AcpService` instance into the kernel's `ServiceRegistry` so
//!     the `dispatch_rust_call` path resolves it. Must run AFTER
//!     `PyKernel::new()` has wrapped the kernel in `Arc<Kernel>`
//!     (the Arc is required for cross-await access in `call_agent`).
//!
//!   * `nx_acp_set_agent_registry(py_kernel, registry)` — late-binds
//!     a Python `AgentRegistry` instance behind the
//!     [`super::service::AgentRegistry`] trait. Until this is called
//!     `call_agent` / `kill_agent` / `list_agents` surface
//!     `AcpServiceError::NotBound`.
//!
//!   * `nx_acp_register_on_terminate(py_kernel, callback_id, callback)`
//!     — appends a Python callback fired with the terminating pid;
//!     used by the permission-lease table to revoke leases.
//!
//! `PyAgentRegistry` bridges the trait by acquiring the GIL inside
//! each method via `Python::attach`. AcpService sees a normal Rust
//! trait object; the Py<PyAny> indirection is invisible to it.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::service::{AcpService, AgentDescriptor, AgentRegistry, OnTerminateCallback};
use crate::generated_pyo3::PyKernel;

// ── Free PyO3 functions ─────────────────────────────────────────────────

/// Install AcpService into the kernel's ServiceRegistry.
#[pyfunction]
#[pyo3(signature = (py_kernel, default_zone="root"))]
pub(crate) fn nx_acp_install(py_kernel: PyRef<'_, PyKernel>, default_zone: &str) -> PyResult<()> {
    AcpService::install(&py_kernel.inner, default_zone).map_err(PyRuntimeError::new_err)
}

/// Late-bind the Python AgentRegistry behind the trait so AcpService
/// can spawn / kill / list processes against it.
#[pyfunction]
pub(crate) fn nx_acp_set_agent_registry(
    py_kernel: PyRef<'_, PyKernel>,
    registry: Py<PyAny>,
) -> PyResult<()> {
    let svc = lookup_acp(&py_kernel)?;
    svc.set_agent_registry(Arc::new(PyAgentRegistry::new(registry)) as Arc<dyn AgentRegistry>);
    Ok(())
}

/// Register a Python callback fired with the terminating pid when
/// any AcpService-owned agent dies. Idempotent per `callback_id`.
#[pyfunction]
pub(crate) fn nx_acp_register_on_terminate(
    py_kernel: PyRef<'_, PyKernel>,
    callback_id: &str,
    callback: Py<PyAny>,
) -> PyResult<()> {
    let svc = lookup_acp(&py_kernel)?;
    let cb: OnTerminateCallback = Arc::new(move |pid: &str| {
        Python::attach(|py| {
            let _ = callback.call1(py, (pid.to_string(),));
        });
    });
    svc.register_on_terminate(callback_id, cb);
    Ok(())
}

fn lookup_acp(_py_kernel: &PyRef<'_, PyKernel>) -> PyResult<Arc<AcpService>> {
    AcpService::handle()
        .ok_or_else(|| PyRuntimeError::new_err("AcpService not installed (call nx_acp_install)"))
}

// ── PyAgentRegistry — Py<PyAny> -> trait object ────────────────────────

pub(crate) struct PyAgentRegistry {
    py_obj: Py<PyAny>,
}

impl PyAgentRegistry {
    pub(crate) fn new(py_obj: Py<PyAny>) -> Self {
        Self { py_obj }
    }

    fn descriptor_from_py(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<AgentDescriptor> {
        let pid: String = obj.getattr("pid")?.extract()?;
        let name: String = obj.getattr("name")?.extract()?;
        let owner_id: String = obj.getattr("owner_id")?.extract()?;
        let zone_id: String = obj.getattr("zone_id")?.extract()?;
        // state may be an enum (.value attr) or a string.
        let state_obj = obj.getattr("state")?;
        let state: String = if let Ok(v) = state_obj.getattr("value") {
            v.extract()?
        } else {
            state_obj.extract()?
        };
        let labels: HashMap<String, String> = obj
            .getattr("labels")
            .ok()
            .and_then(|l| if l.is_none() { None } else { Some(l) })
            .map(|l| l.extract())
            .transpose()?
            .unwrap_or_default();
        let _ = py;
        Ok(AgentDescriptor {
            pid,
            name,
            owner_id,
            zone_id,
            state,
            labels,
        })
    }
}

impl AgentRegistry for PyAgentRegistry {
    fn spawn(
        &self,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        labels: HashMap<String, String>,
    ) -> Result<String, String> {
        Python::attach(|py| -> Result<String, String> {
            let kwargs = PyDict::new(py);
            // Map Python AgentKind.UNMANAGED via the registry's
            // module — we can't import nexus.contracts directly here
            // without coupling. Instead the Python-side _wired.py
            // wires a closure that knows the AgentKind enum; we just
            // hand kwargs in.
            kwargs.set_item("name", name).map_err(|e| e.to_string())?;
            kwargs
                .set_item("owner_id", owner_id)
                .map_err(|e| e.to_string())?;
            kwargs
                .set_item("zone_id", zone_id)
                .map_err(|e| e.to_string())?;
            kwargs
                .set_item("labels", labels)
                .map_err(|e| e.to_string())?;
            let result = self
                .py_obj
                .call_method(py, "spawn", (), Some(&kwargs))
                .map_err(|e| e.to_string())?;
            let bound = result.bind(py);
            let pid: String = bound
                .getattr("pid")
                .and_then(|p| p.extract())
                .map_err(|e| e.to_string())?;
            Ok(pid)
        })
    }

    fn kill(&self, pid: &str, exit_code: i32) -> Result<(), String> {
        Python::attach(|py| -> Result<(), String> {
            let kwargs = PyDict::new(py);
            kwargs
                .set_item("exit_code", exit_code)
                .map_err(|e| e.to_string())?;
            self.py_obj
                .call_method(py, "kill", (pid,), Some(&kwargs))
                .map_err(|e| e.to_string())?;
            Ok(())
        })
    }

    fn list_processes(
        &self,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
        service_label_match: Option<&str>,
    ) -> Vec<AgentDescriptor> {
        Python::attach(|py| {
            let kwargs = PyDict::new(py);
            if let Some(z) = zone_id {
                let _ = kwargs.set_item("zone_id", z);
            }
            if let Some(o) = owner_id {
                let _ = kwargs.set_item("owner_id", o);
            }
            let result = match self
                .py_obj
                .call_method(py, "list_processes", (), Some(&kwargs))
            {
                Ok(r) => r,
                Err(_) => return Vec::new(),
            };
            let list: Vec<AgentDescriptor> = match result.bind(py).cast::<PyList>() {
                Ok(l) => l
                    .iter()
                    .filter_map(|item| Self::descriptor_from_py(py, &item).ok())
                    .collect(),
                Err(_) => Vec::new(),
            };
            // Apply service label filter on the Rust side so the
            // Python registry doesn't need to know about ACP labels.
            match service_label_match {
                Some(s) => list
                    .into_iter()
                    .filter(|d| d.labels.get("service").is_some_and(|v| v == s))
                    .collect(),
                None => list,
            }
        })
    }
}
