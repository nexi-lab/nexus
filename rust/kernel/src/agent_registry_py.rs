//! `PyAgentRegistry` — Python-facing wrapper over the kernel
//! [`AgentRegistry`] SSOT.
//!
//! Exposed as `nexus_runtime.AgentRegistry`. In-process Python callers
//! obtain a handle through `kernel.agent_registry`, which builds a new
//! wrapper sharing the kernel's `Arc<AgentRegistry>` — no clone of state.
//!
//! Methods return [`PyAgentDescriptor`] (exposed as
//! `nexus_runtime.AgentDescriptor`). Field access mirrors
//! `contracts/process_types.py:AgentDescriptor` so existing callers can
//! switch from the Python shim to `kernel.agent_registry.X` without
//! changing every `.pid` / `.kind` / `.state` site to dict subscripts.
//!
//! `kind` / `state` getters return the lowercase string used by the
//! StrEnum on the Python side (`"managed"`, `"ready"`, …) so
//! `AgentKind(desc.kind)` / `AgentState(desc.state)` keeps working.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::core::agents::registry::{
    AgentDescriptor, AgentError, AgentKind, AgentRegistry, AgentSignal, AgentState, RepoMount,
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

fn lowercase(s: &str) -> String {
    s.to_ascii_lowercase()
}

/// Python-facing snapshot of an [`AgentDescriptor`].
///
/// Read-only — descriptors are immutable on the Python side; mutate the
/// underlying state through `PyAgentRegistry` methods. Field names match
/// `contracts/process_types.py:AgentDescriptor` so legacy callers keep
/// using `desc.pid` / `desc.state` after the cutover.
#[pyclass(module = "nexus_runtime", name = "AgentDescriptor", frozen)]
#[derive(Clone)]
pub struct PyAgentDescriptor {
    inner: AgentDescriptor,
}

impl PyAgentDescriptor {
    pub fn new(inner: AgentDescriptor) -> Self {
        Self { inner }
    }

    pub fn into_inner(self) -> AgentDescriptor {
        self.inner
    }
}

#[pymethods]
impl PyAgentDescriptor {
    #[getter]
    fn pid(&self) -> &str {
        &self.inner.pid
    }
    #[getter]
    fn ppid(&self) -> Option<&str> {
        self.inner.parent_pid.as_deref()
    }
    #[getter]
    fn name(&self) -> &str {
        &self.inner.name
    }
    #[getter]
    fn owner_id(&self) -> &str {
        &self.inner.owner_id
    }
    #[getter]
    fn zone_id(&self) -> &str {
        &self.inner.zone_id
    }
    /// Lowercase kind string (matches `AgentKind` StrEnum value).
    #[getter]
    fn kind(&self) -> String {
        lowercase(self.inner.kind.as_str())
    }
    /// Lowercase state string (matches `AgentState` StrEnum value).
    #[getter]
    fn state(&self) -> String {
        lowercase(self.inner.state.as_str())
    }
    #[getter]
    fn exit_code(&self) -> Option<i32> {
        self.inner.exit_code
    }
    #[getter]
    fn generation(&self) -> u32 {
        self.inner.generation
    }
    #[getter]
    fn cwd(&self) -> &str {
        &self.inner.cwd
    }
    #[getter]
    fn root(&self) -> &str {
        &self.inner.root
    }
    #[getter]
    fn children(&self) -> Vec<String> {
        self.inner.children.clone()
    }
    #[getter]
    fn created_at_ms(&self) -> u64 {
        self.inner.created_at_ms
    }
    #[getter]
    fn updated_at_ms(&self) -> u64 {
        self.inner.updated_at_ms
    }
    #[getter]
    fn last_heartbeat_ms(&self) -> Option<u64> {
        self.inner.last_heartbeat_ms
    }
    #[getter]
    fn connection_id(&self) -> Option<&str> {
        self.inner.connection_id.as_deref()
    }
    #[getter]
    fn labels(&self) -> HashMap<String, String> {
        self.inner.labels.clone()
    }
    /// Workspace repo mounts as a list of dicts (`{alias, mount_path}`).
    /// Drives the per-alias DT_LINK rows stamped under
    /// `/proc/{pid}/workspace/{alias}` at start_session time.
    #[getter]
    fn repos<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        self.inner
            .repos
            .iter()
            .map(|r| repo_to_dict(py, r))
            .collect()
    }
    /// `external_info` as a dict (matches Python ExternalProcessInfo
    /// shape) or None for managed agents.
    #[getter]
    fn external_info<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let info = match self.inner.external_info.as_ref() {
            Some(i) => i,
            None => return Ok(None),
        };
        let dict = PyDict::new(py);
        dict.set_item("connection_id", &info.connection_id)?;
        dict.set_item("host_pid", info.host_pid)?;
        dict.set_item("remote_addr", info.remote_addr.as_deref())?;
        dict.set_item("protocol", &info.protocol)?;
        dict.set_item("last_heartbeat_ms", info.last_heartbeat_ms)?;
        Ok(Some(dict))
    }

    /// Mirror Python `AgentDescriptor.to_dict()` so serializers (audit
    /// logs, RPC envelopes) keep their existing shape.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        descriptor_to_dict(py, &self.inner)
    }

    fn __repr__(&self) -> String {
        format!(
            "AgentDescriptor(pid='{}', name='{}', kind='{}', state='{}')",
            self.inner.pid,
            self.inner.name,
            lowercase(self.inner.kind.as_str()),
            lowercase(self.inner.state.as_str())
        )
    }
}

fn repo_to_dict<'py>(py: Python<'py>, repo: &RepoMount) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("alias", &repo.alias)?;
    dict.set_item("mount_path", &repo.mount_path)?;
    Ok(dict)
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
    dict.set_item("kind", lowercase(desc.kind.as_str()))?;
    dict.set_item("state", lowercase(desc.state.as_str()))?;
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
    let repos: Vec<Bound<'py, PyDict>> = desc
        .repos
        .iter()
        .map(|r| repo_to_dict(py, r))
        .collect::<PyResult<_>>()?;
    dict.set_item("repos", repos)?;
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
///
/// Send + Sync — every `kernel.agent_registry` access yields a fresh
/// wrapper sharing `Arc<AgentRegistry>`, and the underlying registry is
/// thread-safe (DashMap + parking_lot). `Py<PyAny>` for the late-bound
/// provisioner is also Send. Dropping `unsendable` lets callers post
/// methods through `asyncio.to_thread` for blocking helpers like
/// `wait_for_state`.
#[pyclass(module = "nexus_runtime", name = "AgentRegistry")]
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
    /// Test-only constructor: build a standalone AgentRegistry not
    /// bound to any kernel. Production callers reach the SSOT through
    /// `kernel.agent_registry`.
    #[new]
    fn py_new() -> Self {
        Self {
            inner: Arc::new(AgentRegistry::new()),
        }
    }

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
    fn spawn(
        &self,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        kind: &str,
        pid: Option<&str>,
        parent_pid: Option<&str>,
        cwd: &str,
        labels: Option<HashMap<String, String>>,
    ) -> PyResult<PyAgentDescriptor> {
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
        Ok(PyAgentDescriptor::new(desc))
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
    fn get(&self, pid: &str) -> Option<PyAgentDescriptor> {
        self.inner.get(pid).map(PyAgentDescriptor::new)
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

    /// Send a signal to a process. Returns the post-signal descriptor.
    #[pyo3(signature = (pid, sig, payload = None))]
    fn signal(
        &self,
        pid: &str,
        sig: &str,
        payload: Option<HashMap<String, String>>,
    ) -> PyResult<PyAgentDescriptor> {
        let signal = AgentSignal::from_str(sig).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("unknown signal: {sig}"))
        })?;
        let desc = self
            .inner
            .signal(pid, signal, payload)
            .map_err(agent_error_to_pyerr)?;
        Ok(PyAgentDescriptor::new(desc))
    }

    /// Kill (TERMINATED + auto-reap if orphan).
    #[pyo3(signature = (pid, exit_code = 0))]
    fn kill(&self, pid: &str, exit_code: i32) -> PyResult<PyAgentDescriptor> {
        let desc = self
            .inner
            .kill(pid, exit_code)
            .map_err(agent_error_to_pyerr)?;
        Ok(PyAgentDescriptor::new(desc))
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
    fn register_external(
        &self,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        connection_id: &str,
        host_pid: Option<i64>,
        remote_addr: Option<&str>,
        protocol: &str,
        parent_pid: Option<&str>,
        labels: Option<HashMap<String, String>>,
    ) -> PyResult<PyAgentDescriptor> {
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
        Ok(PyAgentDescriptor::new(desc))
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
    fn heartbeat(&self, pid: &str) -> PyResult<PyAgentDescriptor> {
        self.inner.heartbeat(pid).map_err(agent_error_to_pyerr)?;
        let desc = self
            .inner
            .get(pid)
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(pid.to_string()))?;
        Ok(PyAgentDescriptor::new(desc))
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
    /// at most `batch_size` descriptors.
    #[pyo3(signature = (zone_id = None, batch_size = 10))]
    fn list_by_priority(&self, zone_id: Option<&str>, batch_size: usize) -> Vec<PyAgentDescriptor> {
        self.inner
            .list_by_priority(zone_id, batch_size)
            .into_iter()
            .map(PyAgentDescriptor::new)
            .collect()
    }

    /// List agents with optional filters.
    #[pyo3(signature = (zone_id = None, owner_id = None, kind = None, state = None))]
    fn list_processes(
        &self,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
        kind: Option<&str>,
        state: Option<&str>,
    ) -> Vec<PyAgentDescriptor> {
        let kind_filter = kind.and_then(AgentKind::from_str);
        let state_filter = state.and_then(AgentState::from_str);
        self.inner
            .list(
                zone_id,
                owner_id,
                kind_filter.as_ref(),
                state_filter.as_ref(),
            )
            .into_iter()
            .map(PyAgentDescriptor::new)
            .collect()
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

    /// Bind a Python-side provisioner. The provisioner is expected to
    /// expose an async `provision(agent_id, *, name=None, skills=None,
    /// metadata=None)` method; callers (`agent_registration.py`) fetch
    /// the stored handle through `get_provisioner` and `await` the
    /// returned coroutine themselves so the asyncio loop owns the wait.
    ///
    /// Idempotent — calling with a fresh callable replaces the prior
    /// binding. Pass the result of `take_provisioner` to release the
    /// reference at shutdown.
    fn set_provisioner(&self, callback: Py<PyAny>) {
        self.inner.set_provisioner(callback);
    }

    /// Return the Python provisioner bound by `set_provisioner`, or
    /// None when the provisioner has not been wired (test boot, minimal
    /// profiles).
    fn get_provisioner(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.inner.provisioner(py)
    }

    /// Drop the stored provisioner. Returns the prior reference if any
    /// so the caller can perform cleanup.
    fn take_provisioner(&self) -> Option<Py<PyAny>> {
        self.inner.take_provisioner()
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
