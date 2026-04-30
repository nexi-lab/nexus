//! `services::python` — services-tier PyO3 surface.
//!
//! Single `register(m)` entry point that the `nexus-cdylib` crate's
//! `#[pymodule] fn nexus_runtime` invokes alongside `lib::python::register`,
//! `kernel::python::register`, etc.  Same compositional pattern as every
//! other peer crate's PyO3 boundary.
//!
//! ## Currently exposed
//!
//! * `install_audit_hook(kernel, zone_id, stream_path)` — service-tier
//!   DI entry point that builds + registers a `services::audit::AuditHook`
//!   on the given Kernel.  Replaces the old `kernel.start_audit_hook()`
//!   PyO3 method on PyKernel — Phase 3 moved hook construction out of
//!   the kernel cdylib boundary into the owning service tier.

use kernel::generated_kernel_abi_pyo3::PyKernel;
use kernel::hal::backend_factory::set_enabled_drivers;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::audit;
use crate::managed_agent::ManagedAgentService;

/// Install an `AuditHook` on `kernel` for `zone_id`, backed by a
/// WAL-replicated DT_STREAM at `stream_path`.
///
/// Replaces the pre-Phase-3 `kernel.start_audit_hook(zone_id, stream_path)`
/// PyO3 method on PyKernel.  Service-tier owns hook lifecycle now —
/// kernel only exposes `prepare_audit_stream` (stream lifecycle) +
/// `register_native_hook` (LSM-style in-tree API), and this function
/// composes the two with the local `AuditHook::new`.
///
/// Python signature:
///
/// ```python
/// nexus_runtime.install_audit_hook(kernel, zone_id="root", stream_path="/audit/traces/")
/// ```
#[pyfunction]
#[pyo3(name = "install_audit_hook")]
fn install_audit_hook_py(
    kernel: PyRef<'_, PyKernel>,
    zone_id: &str,
    stream_path: &str,
) -> PyResult<()> {
    audit::install(kernel.kernel_ref(), zone_id, stream_path)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))
}

/// Install the deployment-profile-driven driver gate.
///
/// `drivers` is the union of every backend type the active profile
/// enables (e.g. `["local", "remote", "anthropic", "openai"]`).
/// Subsequent `sys_setattr(DT_MOUNT)` with a `backend_type` outside
/// the gate surfaces a clear error instead of silently falling
/// through to the kernel-default local-root branch.
///
/// Idempotent — repeated calls overwrite the gate, so a Python
/// reload that re-resolves the profile sees the updated set without
/// an interpreter restart.  Pass an empty list to lock down every
/// non-local-default driver.
#[pyfunction]
fn nx_set_enabled_drivers(drivers: Vec<String>) -> PyResult<()> {
    set_enabled_drivers(drivers);
    Ok(())
}

/// Install `ManagedAgentService` on `kernel`. Registers the chat-with-me
/// mailbox stamping hook, the workspace-boundary teaching hook, and
/// enlists the service into `ServiceRegistry` so the gRPC `Call`
/// dispatch path resolves `managed_agent.start_session_v1` etc.
///
/// Mirrors `nx_acp_install` for the unmanaged-agent half. Pure-Rust
/// embedders skip this and call
/// `services::managed_agent::ManagedAgentService::install(kernel)`
/// themselves.
#[pyfunction]
#[pyo3(name = "nx_managed_agent_install")]
fn nx_managed_agent_install(py_kernel: PyRef<'_, PyKernel>) -> PyResult<()> {
    ManagedAgentService::install(py_kernel.kernel_ref()).map_err(PyRuntimeError::new_err)
}

/// Generic in-process Rust-service dispatch entry point.
///
/// Mirrors the lookup the tonic `Call` handler runs internally
/// (`Kernel::dispatch_rust_call`). Returns:
/// * `Some(bytes)` — the service handled the call and returned a
///   JSON-encoded response.
/// * `None` — `service` does not resolve as a Rust-flavoured entry
///   in the kernel's `ServiceRegistry`. Python-side callers should
///   fall through to their existing `dispatch_method` path so the
///   195 `@rpc_expose` services keep working.
///
/// Single primitive — no per-service `nx_<svc>_dispatch` wrappers,
/// so audit / permission hooks added to the dispatch path land in
/// one place.
#[pyfunction]
fn nx_kernel_dispatch_rust_call<'py>(
    py: Python<'py>,
    py_kernel: PyRef<'_, PyKernel>,
    service: &str,
    method: &str,
    payload: &[u8],
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let kernel = py_kernel.kernel_arc();
    // RustService::dispatch may run an async tokio block_on
    // internally; release the GIL so other Python tasks can run.
    let outcome = py.detach(|| kernel.dispatch_rust_call(service, method, payload));
    match outcome {
        None => Ok(None),
        Some(Ok(bytes)) => Ok(Some(PyBytes::new(py, &bytes))),
        Some(Err(e)) => Err(PyRuntimeError::new_err(e.to_string())),
    }
}

/// Register every services-tier PyO3 export into the parent module.
/// Called from `nexus-cdylib`'s `#[pymodule] fn nexus_runtime`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(install_audit_hook_py, m)?)?;
    // DeploymentProfile-driven driver gate — Python boot calls this
    // with the profile's enabled driver set before any DT_MOUNT
    // sys_setattr fires.  Disabled drivers fail with a clear error
    // at mount time instead of silently degrading.
    m.add_function(wrap_pyfunction!(nx_set_enabled_drivers, m)?)?;
    // ManagedAgentService — boot install (kernel doesn't auto-call
    // because services lives in a peer crate; Python-side wiring
    // calls this from `_wired.py` after `Kernel::new` returns).
    m.add_function(wrap_pyfunction!(nx_managed_agent_install, m)?)?;
    // ACP service wiring — hand-written hooks (boot install + Python
    // AgentRegistry bridge + on-terminate callbacks). Hosts
    // `AgentKind::UNMANAGED` agents (subprocess + ACP-over-stdio).
    m.add_function(wrap_pyfunction!(crate::acp::pyo3::nx_acp_install, m)?)?;
    m.add_function(wrap_pyfunction!(
        crate::acp::pyo3::nx_acp_set_agent_registry,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        crate::acp::pyo3::nx_acp_register_on_terminate,
        m
    )?)?;
    // Generic Rust-service dispatch — same lookup the tonic Call
    // handler uses, exposed for in-process Python callers so we don't
    // grow per-service shortcuts that each need their own
    // audit-bypass review.
    m.add_function(wrap_pyfunction!(nx_kernel_dispatch_rust_call, m)?)?;
    // Phase 3 restructure plan #6: tasks pyclasses (PyTaskEngine /
    // PyTaskRecord / PyQueueStats) folded into nexus_runtime cdylib —
    // standalone _nexus_tasks.so retired.
    crate::tasks::register_python(m)?;
    Ok(())
}
