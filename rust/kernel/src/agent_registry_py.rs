//! `PyAgentRegistry` — Python-facing wrapper over the kernel
//! [`AgentRegistry`] SSOT.
//!
//! Exposed as `nexus_runtime.AgentRegistry`. In-process Python callers
//! obtain a handle through `kernel.agent_registry`, which builds a new
//! wrapper sharing the kernel's `Arc<AgentRegistry>` — no clone of state.
//!
//! Surface mirrors the Python service-tier `AgentRegistry` shim that the
//! agent-registry-python-fold PR is collapsing: `spawn`, `kill`, `signal`,
//! `register_external`, `unregister_external`, `heartbeat`,
//! `count_by_state`, `list_by_priority`, `get`, `list_processes`,
//! `update_state`, `wait_for_state`, `count`. Every method that returns a
//! descriptor returns a Python dict with the same keys as
//! `contracts/process_types.py:AgentDescriptor.to_dict`.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::core::agents::registry::{
    AgentDescriptor, AgentError, AgentKind, AgentRegistry, AgentSignal, AgentState,
};

fn agent_error_to_pyerr(e: AgentError) -> PyErr {
    match e {
        AgentError::NotFound(_) => pyo3::exceptions::PyKeyError::new_err(e.to_string()),
        AgentError::AlreadyExists(_) => pyo3::exceptions::PyValueError::new_err(e.to_string()),
        AgentError::InvalidTransition { .. } => {
            pyo3::exceptions::PyValueError::new_err(e.to_string())
        }
        AgentError::InvalidKind(_) | AgentError::Protocol(_) => {
            pyo3::exceptions::PyValueError::new_err(e.to_string())
        }
        AgentError::PidExhausted => pyo3::exceptions::PyRuntimeError::new_err(e.to_string()),
    }
}

fn descriptor_to_dict<'py>(
    py: Python<'py>,
    desc: &AgentDescriptor,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("pid", &desc.pid)?;
    dict.set_item("ppid", desc.parent_pid.as_deref())?;
    dict.set_item("name", &desc.name)?;
    dict.set_item("owner_id", &desc.owner_id)?;
    dict.set_item("zone_id", &desc.zone_id)?;
    dict.set_item("kind", desc.kind.as_str())?;
    dict.set_item("state", desc.state.as_str())?;
    dict.set_item("exit_code", desc.exit_code)?;
    dict.set_item("generation", desc.generation)?;
    dict.set_item("cwd", &desc.cwd)?;
    dict.set_item("root", &desc.root)?;
    dict.set_item("children", desc.children.clone())?;
    dict.set_item("created_at_ms", desc.created_at_ms)?;
    dict.set_item("updated_at_ms", desc.updated_at_ms)?;
    dict.set_item("last_heartbeat_ms", desc.last_heartbeat_ms)?;
    dict.set_item("connection_id", desc.connection_id.as_deref())?;
    dict.set_item("labels", desc.labels.clone())?;
    if let Some(info) = desc.external_info.as_ref() {
        let ext = PyDict::new(py);
        ext.set_item("connection_id", &info.connection_id)?;
        ext.set_item("host_pid", info.host_pid)?;
        ext.set_item("remote_addr", info.remote_addr.as_deref())?;
        ext.set_item("protocol", &info.protocol)?;
        ext.set_item("last_heartbeat_ms", info.last_heartbeat_ms)?;
        dict.set_item("external_info", ext)?;
    } else {
        dict.set_item("external_info", py.None())?;
    }
    Ok(dict)
}

/// Python-facing handle for the kernel `AgentRegistry`.
#[pyclass(module = "nexus_runtime", name = "AgentRegistry", unsendable)]
pub struct PyAgentRegistry {
    inner: Arc<AgentRegistry>,
}

impl PyAgentRegistry {
    pub fn new(inner: Arc<AgentRegistry>) -> Self {
        Self { inner }
    }
}

#[pymethods]
impl PyAgentRegistry {
    /// Number of registered agents.
    #[getter]
    fn count(&self) -> usize {
        self.inner.count()
    }

    /// Spawn a new agent in REGISTERED state. Returns the descriptor dict.
    #[pyo3(signature = (
        name,
        owner_id,
        zone_id,
        *,
        kind = "managed",
        pid = None,
        parent_pid = None,
        cwd = "/",
        labels = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn spawn<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        kind: &str,
        pid: Option<&str>,
        parent_pid: Option<&str>,
        cwd: &str,
        labels: Option<HashMap<String, String>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let kind = AgentKind::from_str(kind).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown agent kind: {kind}"))
        })?;
        let desc = self
            .inner
            .spawn(
                name.to_string(),
                owner_id.to_string(),
                zone_id.to_string(),
                kind,
                parent_pid.map(|s| s.to_string()),
                pid.map(|s| s.to_string()),
                cwd.to_string(),
                None,
                labels.unwrap_or_default(),
            )
            .map_err(agent_error_to_pyerr)?;
        descriptor_to_dict(py, &desc)
    }

    /// Unregister an agent by pid (no parent.children cleanup). Returns
    /// True if a row was removed.
    fn unregister(&self, pid: &str) -> bool {
        self.inner.unregister(pid).is_some()
    }

    /// Reap an agent (remove + clean up parent.children).
    fn reap(&self, pid: &str) -> bool {
        self.inner.reap(pid)
    }

    /// Look up by pid. Returns None when missing.
    fn get<'py>(&self, py: Python<'py>, pid: &str) -> PyResult<Option<Bound<'py, PyDict>>> {
        match self.inner.get(pid) {
            Some(desc) => Ok(Some(descriptor_to_dict(py, &desc)?)),
            None => Ok(None),
        }
    }

    /// Update state with VALID_AGENT_TRANSITIONS validation. Returns True
    /// when the row exists and the transition is applied (or is a no-op).
    /// Raises ValueError on rejected transitions.
    fn update_state(&self, pid: &str, new_state: &str) -> PyResult<bool> {
        let target = AgentState::from_str(new_state).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown agent state: {new_state}"))
        })?;
        self.inner
            .update_state(pid, target)
            .map_err(agent_error_to_pyerr)
    }

    /// Same as `update_state` but also stamps an exit code.
    fn update_state_with_exit(&self, pid: &str, new_state: &str, exit_code: i32) -> PyResult<bool> {
        let target = AgentState::from_str(new_state).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown agent state: {new_state}"))
        })?;
        self.inner
            .update_state_with_exit(pid, target, exit_code)
            .map_err(agent_error_to_pyerr)
    }

    /// Send a signal to a process. Returns the post-signal descriptor
    /// dict.
    #[pyo3(signature = (pid, sig, payload = None))]
    fn signal<'py>(
        &self,
        py: Python<'py>,
        pid: &str,
        sig: &str,
        payload: Option<HashMap<String, String>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let signal = AgentSignal::from_str(sig).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown signal: {sig}"))
        })?;
        let desc = self
            .inner
            .signal(pid, signal, payload)
            .map_err(agent_error_to_pyerr)?;
        descriptor_to_dict(py, &desc)
    }

    /// Kill (TERMINATED + auto-reap if orphan). Returns post-kill
    /// descriptor dict.
    #[pyo3(signature = (pid, exit_code = 0))]
    fn kill<'py>(
        &self,
        py: Python<'py>,
        pid: &str,
        exit_code: i32,
    ) -> PyResult<Bound<'py, PyDict>> {
        let desc = self
            .inner
            .kill(pid, exit_code)
            .map_err(agent_error_to_pyerr)?;
        descriptor_to_dict(py, &desc)
    }

    /// Register an external (gRPC/MCP) process. The connection_id is
    /// adopted as the pid.
    #[pyo3(signature = (
        name,
        owner_id,
        zone_id,
        *,
        connection_id,
        host_pid = None,
        remote_addr = None,
        protocol = "grpc",
        parent_pid = None,
        labels = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn register_external<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        connection_id: &str,
        host_pid: Option<i64>,
        remote_addr: Option<&str>,
        protocol: &str,
        parent_pid: Option<&str>,
        labels: Option<HashMap<String, String>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let desc = self
            .inner
            .register_external(
                name.to_string(),
                owner_id.to_string(),
                zone_id.to_string(),
                connection_id.to_string(),
                host_pid,
                remote_addr.map(|s| s.to_string()),
                protocol.to_string(),
                parent_pid.map(|s| s.to_string()),
                labels.unwrap_or_default(),
            )
            .map_err(agent_error_to_pyerr)?;
        descriptor_to_dict(py, &desc)
    }

    /// Unregister an external process — TERMINATED + reap.
    fn unregister_external(&self, pid: &str) -> PyResult<()> {
        self.inner
            .unregister_external(pid)
            .map_err(agent_error_to_pyerr)
    }

    /// Heartbeat for an UNMANAGED process. Raises KeyError if pid is
    /// unknown, ValueError if the agent is MANAGED or has no
    /// `external_info`.
    fn heartbeat<'py>(&self, py: Python<'py>, pid: &str) -> PyResult<Bound<'py, PyDict>> {
        self.inner.heartbeat(pid).map_err(agent_error_to_pyerr)?;
        let desc = self
            .inner
            .get(pid)
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(pid.to_string()))?;
        descriptor_to_dict(py, &desc)
    }

    /// Heartbeat with an explicit timestamp. Used by dual-write callers
    /// that already hold a timestamp; no kind/info validation.
    fn heartbeat_at(&self, pid: &str, timestamp_ms: u64) -> bool {
        self.inner.heartbeat_at(pid, timestamp_ms)
    }

    /// Count agents in `state`, optionally scoped to a zone.
    #[pyo3(signature = (state, zone_id = None))]
    fn count_by_state(&self, state: &str, zone_id: Option<&str>) -> PyResult<usize> {
        let target = AgentState::from_str(state).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown agent state: {state}"))
        })?;
        Ok(self.inner.count_by_state(target, zone_id))
    }

    /// List BUSY agents ordered by eviction priority then LRU. Returns
    /// at most `batch_size` descriptor dicts.
    #[pyo3(signature = (zone_id = None, batch_size = 10))]
    fn list_by_priority<'py>(
        &self,
        py: Python<'py>,
        zone_id: Option<&str>,
        batch_size: usize,
    ) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let agents = self.inner.list_by_priority(zone_id, batch_size);
        agents.iter().map(|d| descriptor_to_dict(py, d)).collect()
    }

    /// List agents with optional filters. Returns descriptor dicts.
    #[pyo3(signature = (zone_id = None, owner_id = None, kind = None, state = None))]
    fn list_processes<'py>(
        &self,
        py: Python<'py>,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
        kind: Option<&str>,
        state: Option<&str>,
    ) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let kind_filter = kind.and_then(AgentKind::from_str);
        let state_filter = state.and_then(AgentState::from_str);
        let agents = self.inner.list(
            zone_id,
            owner_id,
            kind_filter.as_ref(),
            state_filter.as_ref(),
        );
        agents.iter().map(|d| descriptor_to_dict(py, d)).collect()
    }

    /// Block (GIL-free) until `pid` reaches `target_state` or timeout.
    /// Returns the final state string. Raises RuntimeError on timeout
    /// or unknown pid.
    fn wait_for_state(
        &self,
        py: Python<'_>,
        pid: &str,
        target_state: &str,
        timeout_ms: u64,
    ) -> PyResult<String> {
        let target = AgentState::from_str(target_state).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown agent state: {target_state}"))
        })?;
        let pid = pid.to_string();
        let registry = Arc::clone(&self.inner);
        py.detach(|| {
            registry
                .wait_for_state(&pid, &target, timeout_ms)
                .map_err(pyo3::exceptions::PyRuntimeError::new_err)
        })
    }

    /// Drain: terminate + reap every process. Used at shutdown.
    fn close_all(&self) {
        self.inner.close_all()
    }
}

// Re-export so the kernel pymodule register() can find the type without
// reaching into module internals.
pub use self::PyAgentRegistry as AgentRegistryPyType;

// Helper for the codegen template — takes `&Kernel` and yields a fresh
// PyAgentRegistry wrapping the kernel's Arc. Lives here so the codegen
// emits a one-line method body.
pub fn from_kernel(kernel: &crate::kernel::Kernel) -> PyAgentRegistry {
    PyAgentRegistry::new(Arc::clone(&kernel.agent_registry))
}
