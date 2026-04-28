//! `AcpService` — stateless coding-agent caller via ACP JSON-RPC.
//!
//! Port of `nexus.services.acp.service.AcpService`. Owns the
//! per-call subprocess + ACP session lifecycle and the VFS-backed
//! admin RPCs (system prompt / enabled skills / call history /
//! agent config listing).
//!
//! Layered on top of:
//!   * [`super::agent_config::AgentConfig`] -- VFS-persisted CLI config
//!   * [`super::paths`]                     -- /{zone}/agents + /{zone}/proc layout
//!   * [`super::subprocess::AcpSubprocess`] -- tokio Command + DT_PIPE wiring (unix only)
//!   * [`super::connection::AcpConnection`] -- ACP JSON-RPC adapter
//!   * [`super::observer::AgentObserver`]   -- session/update accumulator
//!
//! AgentRegistry is reached through an injectable [`AgentRegistry`]
//! trait. Today's PyO3-wired bridge lands in commit 21; commit 20
//! provides the trait + a unit-test mock so the orchestration logic
//! is testable without a Python runtime.

#![allow(dead_code)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use dashmap::DashMap;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::agent_config::AgentConfig;
use super::paths;
use crate::kernel::{Kernel, OperationContext};

#[cfg(unix)]
use super::connection::{AcpConnection, FsRead, FsWrite};
#[cfg(unix)]
use super::observer::AgentTurnResult;
#[cfg(unix)]
use super::subprocess::AcpSubprocess;
#[cfg(unix)]
use futures::future::BoxFuture;
#[cfg(unix)]
use std::time::Duration;

// ── AgentRegistry trait ─────────────────────────────────────────────────

/// Subset of the Python `AgentRegistry` surface AcpService depends on.
/// Today AgentRegistry stays Python (commit 21 wires a PyO3 impl);
/// commit 20 isolates the dependency behind this trait so the
/// orchestration logic in [`AcpService::call_agent`] is testable
/// without a live Python interpreter.
pub(crate) trait AgentRegistry: Send + Sync {
    /// Allocate a pid for an unmanaged agent. `name` follows the
    /// Python convention `acp:<config.name>`.
    fn spawn(
        &self,
        name: &str,
        owner_id: &str,
        zone_id: &str,
        labels: HashMap<String, String>,
    ) -> Result<String, String>;

    /// Mark a pid as terminated. Idempotent — a missing pid is not
    /// an error (mirrors Python's contextlib-suppressed kill).
    fn kill(&self, pid: &str, exit_code: i32) -> Result<(), String>;

    /// Return the subset of agent descriptors matching the filter.
    /// `service_label_match` lets the caller restrict to a specific
    /// `labels.service` value (AcpService passes `Some("acp")`).
    fn list_processes(
        &self,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
        service_label_match: Option<&str>,
    ) -> Vec<AgentDescriptor>;
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct AgentDescriptor {
    pub pid: String,
    pub name: String,
    pub owner_id: String,
    pub zone_id: String,
    pub state: String,
    pub labels: HashMap<String, String>,
}

// ── AcpResult / AcpCallRequest ──────────────────────────────────────────

/// Unified result of a one-shot ACP call. Mirrors the Python
/// `AcpResult` dataclass; serialised to VFS at
/// `/{zone}/proc/{pid}/result` by [`AcpService::call_agent`].
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub(crate) struct AcpResult {
    pub pid: String,
    pub agent_id: String,
    pub exit_code: i32,
    pub response: String,
    pub raw_stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub metadata: serde_json::Map<String, Value>,
}

/// Request shape for [`AcpService::call_agent`]. Mirrors the Python
/// keyword arguments; `cwd` defaults to `.` (current working dir),
/// `timeout_secs` defaults to 300, both like the Python side.
#[derive(Debug, Clone, Deserialize)]
pub(crate) struct AcpCallRequest {
    pub agent_id: String,
    pub prompt: String,
    pub owner_id: String,
    pub zone_id: String,
    #[serde(default = "default_cwd")]
    pub cwd: String,
    #[serde(default = "default_timeout_secs")]
    pub timeout_secs: f64,
    #[serde(default)]
    pub labels: HashMap<String, String>,
    #[serde(default)]
    pub session_id: Option<String>,
}

fn default_cwd() -> String {
    ".".to_string()
}
fn default_timeout_secs() -> f64 {
    300.0
}

#[derive(Debug)]
pub(crate) enum AcpServiceError {
    UnknownAgent(String),
    NotBound(&'static str),
    Io(String),
}

impl std::fmt::Display for AcpServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownAgent(a) => write!(f, "unknown agent_id: {a:?}"),
            Self::NotBound(what) => write!(f, "{what} not bound"),
            Self::Io(m) => write!(f, "io: {m}"),
        }
    }
}

impl std::error::Error for AcpServiceError {}

// ── Termination callback ────────────────────────────────────────────────

/// Called with `agent_id` (== pid) when an agent terminates. Used by
/// the permission lease table to revoke leases on agent death.
pub(crate) type OnTerminateCallback = Arc<dyn Fn(&str) + Send + Sync>;

// ── Service ─────────────────────────────────────────────────────────────

pub(crate) struct AcpService {
    kernel: Arc<Kernel>,
    default_zone: String,
    agent_registry: RwLock<Option<Arc<dyn AgentRegistry>>>,
    on_terminate: RwLock<Vec<(String, OnTerminateCallback)>>,
    /// Registry of in-flight sessions keyed by pid. Today opaque;
    /// `kill_agent` drains it. The unit on disk is intentionally
    /// minimal — full session-handle storage lands with commit 21
    /// when `call_agent` actually populates it.
    active: DashMap<String, ActiveSession>,
}

struct ActiveSession {
    agent_id: String,
    fd_paths: [String; 3],
}

impl AcpService {
    pub(crate) const NAME: &'static str = "acp";

    pub(crate) fn new(kernel: Arc<Kernel>, default_zone: String) -> Self {
        Self {
            kernel,
            default_zone,
            agent_registry: RwLock::new(None),
            on_terminate: RwLock::new(Vec::new()),
            active: DashMap::new(),
        }
    }

    pub(crate) fn default_zone(&self) -> &str {
        &self.default_zone
    }

    pub(crate) fn set_agent_registry(&self, reg: Arc<dyn AgentRegistry>) {
        *self.agent_registry.write().unwrap() = Some(reg);
    }

    pub(crate) fn register_on_terminate(&self, id: &str, cb: OnTerminateCallback) {
        let mut list = self.on_terminate.write().unwrap();
        if list.iter().any(|(cid, _)| cid == id) {
            return;
        }
        list.push((id.to_string(), cb));
    }

    fn fire_on_terminate(&self, pid: &str) {
        let callbacks: Vec<OnTerminateCallback> = self
            .on_terminate
            .read()
            .unwrap()
            .iter()
            .map(|(_, cb)| cb.clone())
            .collect();
        for cb in callbacks {
            cb(pid);
        }
    }

    fn registry(&self) -> Result<Arc<dyn AgentRegistry>, AcpServiceError> {
        self.agent_registry
            .read()
            .unwrap()
            .clone()
            .ok_or(AcpServiceError::NotBound("AgentRegistry"))
    }

    // ── VFS helpers (cross-platform) ─────────────────────────────────

    fn ctx() -> OperationContext {
        OperationContext::new("system", "root", true, None, true)
    }

    pub(crate) fn read_agent_config(&self, agent_id: &str, zone_id: &str) -> Option<AgentConfig> {
        let path = paths::agent_config(zone_id, agent_id);
        let bytes = self
            .kernel
            .sys_read(&path, &Self::ctx())
            .ok()
            .and_then(|r| r.data)?;
        if bytes.is_empty() {
            return None;
        }
        serde_json::from_slice(&bytes).ok()
    }

    pub(crate) fn list_agent_configs(&self, zone_id: Option<&str>) -> Vec<Value> {
        let zone_id = zone_id.unwrap_or(&self.default_zone);
        let agents_dir = format!("/{zone_id}/agents");
        let entries = self.kernel.sys_readdir_backend(&agents_dir, zone_id);
        let mut out = Vec::new();
        for entry_path in entries {
            // sys_readdir_backend returns paths; we look for
            // <agent_id>/agent.json under each.
            let cfg_path = if entry_path.ends_with('/') {
                format!("{entry_path}agent.json")
            } else {
                format!("{entry_path}/agent.json")
            };
            if let Some(bytes) = self
                .kernel
                .sys_read(&cfg_path, &Self::ctx())
                .ok()
                .and_then(|r| r.data)
            {
                if let Ok(v) = serde_json::from_slice::<Value>(&bytes) {
                    if v.get("agent_id").is_some() {
                        out.push(v);
                    }
                }
            }
        }
        out
    }

    pub(crate) fn get_system_prompt(&self, agent_id: &str, zone_id: &str) -> Option<String> {
        let path = paths::system_prompt(zone_id, agent_id);
        let bytes = self
            .kernel
            .sys_read(&path, &Self::ctx())
            .ok()
            .and_then(|r| r.data)?;
        if bytes.is_empty() {
            None
        } else {
            Some(String::from_utf8_lossy(&bytes).into_owned())
        }
    }

    pub(crate) fn set_system_prompt(
        &self,
        agent_id: &str,
        content: &str,
        zone_id: &str,
    ) -> Result<(), AcpServiceError> {
        let path = paths::system_prompt(zone_id, agent_id);
        self.kernel
            .sys_write(&path, &Self::ctx(), content.as_bytes(), 0)
            .map(|_| ())
            .map_err(|e| AcpServiceError::Io(format!("{e:?}")))
    }

    pub(crate) fn delete_system_prompt(&self, agent_id: &str, zone_id: &str) {
        let path = paths::system_prompt(zone_id, agent_id);
        let _ = self.kernel.sys_unlink(&path, &Self::ctx());
    }

    pub(crate) fn get_enabled_skills(&self, agent_id: &str, zone_id: &str) -> Option<Vec<Value>> {
        let path = paths::skills(zone_id, agent_id);
        let bytes = self
            .kernel
            .sys_read(&path, &Self::ctx())
            .ok()
            .and_then(|r| r.data)?;
        if bytes.is_empty() {
            return None;
        }
        serde_json::from_slice(&bytes).ok()
    }

    pub(crate) fn set_enabled_skills(
        &self,
        agent_id: &str,
        skills: &[Value],
        zone_id: &str,
    ) -> Result<(), AcpServiceError> {
        let path = paths::skills(zone_id, agent_id);
        let bytes = serde_json::to_vec(skills)
            .map_err(|e| AcpServiceError::Io(format!("encode skills: {e}")))?;
        self.kernel
            .sys_write(&path, &Self::ctx(), &bytes, 0)
            .map(|_| ())
            .map_err(|e| AcpServiceError::Io(format!("{e:?}")))
    }

    pub(crate) fn get_call_history(&self, zone_id: Option<&str>, limit: usize) -> Vec<Value> {
        let zone_id = zone_id.unwrap_or(&self.default_zone);
        let proc_dir = format!("/{zone_id}/proc");
        let entries = self.kernel.sys_readdir_backend(&proc_dir, zone_id);
        let mut out = Vec::new();
        for entry_path in entries {
            let result_path = if entry_path.ends_with("/result") {
                entry_path
            } else if entry_path.ends_with('/') {
                format!("{entry_path}result")
            } else {
                continue;
            };
            if let Some(bytes) = self
                .kernel
                .sys_read(&result_path, &Self::ctx())
                .ok()
                .and_then(|r| r.data)
            {
                if let Ok(v) = serde_json::from_slice::<Value>(&bytes) {
                    if v.is_object() {
                        out.push(v);
                    }
                }
            }
        }
        out.sort_by(|a, b| {
            let av = a.get("created_at").and_then(Value::as_f64).unwrap_or(0.0);
            let bv = b.get("created_at").and_then(Value::as_f64).unwrap_or(0.0);
            bv.partial_cmp(&av).unwrap_or(std::cmp::Ordering::Equal)
        });
        out.truncate(limit);
        out
    }

    pub(crate) fn list_agents(
        &self,
        zone_id: Option<&str>,
        owner_id: Option<&str>,
    ) -> Result<Vec<AgentDescriptor>, AcpServiceError> {
        let reg = self.registry()?;
        Ok(reg.list_processes(zone_id, owner_id, Some("acp")))
    }

    pub(crate) fn kill_agent(&self, pid: &str) -> Result<(), AcpServiceError> {
        // Drop active session if present (closes connection / fds).
        self.active.remove(pid);
        let reg = self.registry()?;
        let _ = reg.kill(pid, -9);
        self.fire_on_terminate(pid);
        Ok(())
    }

    pub(crate) fn persist_result(
        &self,
        result: &AcpResult,
        zone_id: &str,
        prompt: &str,
    ) -> Result<(), AcpServiceError> {
        let path = paths::proc_result(zone_id, &result.pid);
        let payload = json!({
            "pid": result.pid,
            "agent_id": result.agent_id,
            "prompt": prompt,
            "created_at": now_secs(),
            "exit_code": result.exit_code,
            "response": result.response,
            "raw_stdout": result.raw_stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "metadata": result.metadata,
            "session_id": result.metadata.get("session_id").cloned().unwrap_or(Value::Null),
        });
        let bytes = serde_json::to_vec_pretty(&payload)
            .map_err(|e| AcpServiceError::Io(format!("encode result: {e}")))?;
        self.kernel
            .sys_write(&path, &Self::ctx(), &bytes, 0)
            .map(|_| ())
            .map_err(|e| AcpServiceError::Io(format!("{e:?}")))
    }
}

// ── call_agent (unix only — depends on AcpSubprocess) ──────────────────

#[cfg(unix)]
impl AcpService {
    /// Run a one-shot ACP call against `req.agent_id`. See module
    /// docs for the lifecycle. Errors map onto AcpResult fields:
    ///   - timeout            -> timed_out=true, exit_code=-1
    ///   - JSON-RPC protocol  -> exit_code=1
    ///   - subprocess spawn   -> exit_code=127
    ///   - any other error    -> exit_code=-1 with stderr populated
    pub(crate) async fn call_agent(
        self: &Arc<Self>,
        req: AcpCallRequest,
    ) -> Result<AcpResult, AcpServiceError> {
        let cfg = self
            .read_agent_config(&req.agent_id, &req.zone_id)
            .ok_or_else(|| AcpServiceError::UnknownAgent(req.agent_id.clone()))?;

        let user_prompt = req.prompt.clone();

        // Inject system prompt + enabled skills (mirrors Python).
        let prompt = self.compose_prompt(&req.agent_id, &req.zone_id, &req.prompt, &cfg);

        // Allocate pid via AgentRegistry.
        let reg = self.registry()?;
        let mut labels = req.labels.clone();
        labels.insert("agent_id".to_string(), req.agent_id.clone());
        labels.insert("service".to_string(), "acp".to_string());
        let pid = reg
            .spawn(
                &format!("acp:{}", cfg.name),
                &req.owner_id,
                &req.zone_id,
                labels,
            )
            .map_err(AcpServiceError::Io)?;

        // Build VFS-backed file I/O closures.
        let host_cwd = std::fs::canonicalize(&req.cwd).unwrap_or_else(|_| PathBuf::from(&req.cwd));
        let zone = req.zone_id.clone();
        let kernel_for_read = Arc::clone(&self.kernel);
        let cwd_for_read = host_cwd.clone();
        let zone_for_read = zone.clone();
        let fs_read: FsRead = Arc::new(move |host_path: String| {
            let kernel = Arc::clone(&kernel_for_read);
            let cwd = cwd_for_read.clone();
            let zone = zone_for_read.clone();
            Box::pin(async move {
                let vfs_path = host_to_vfs(&host_path, &cwd, &format!("/{zone}"));
                match kernel.sys_read(&vfs_path, &Self::ctx()) {
                    Ok(r) => {
                        let bytes = r.data.unwrap_or_default();
                        Ok(String::from_utf8_lossy(&bytes).into_owned())
                    }
                    Err(e) => Err(format!("{e:?}")),
                }
            }) as BoxFuture<'static, Result<String, String>>
        });
        let kernel_for_write = Arc::clone(&self.kernel);
        let cwd_for_write = host_cwd.clone();
        let zone_for_write = zone.clone();
        let fs_write: FsWrite = Arc::new(move |host_path: String, content: String| {
            let kernel = Arc::clone(&kernel_for_write);
            let cwd = cwd_for_write.clone();
            let zone = zone_for_write.clone();
            Box::pin(async move {
                let vfs_path = host_to_vfs(&host_path, &cwd, &format!("/{zone}"));
                kernel
                    .sys_write(&vfs_path, &Self::ctx(), content.as_bytes(), 0)
                    .map(|_| ())
                    .map_err(|e| format!("{e:?}"))
            }) as BoxFuture<'static, Result<(), String>>
        });

        let timeout = Duration::from_secs_f64(req.timeout_secs);

        // Spawn the agent CLI + register DT_PIPEs.
        let mut subproc =
            match AcpSubprocess::spawn(&cfg, &host_cwd, &self.kernel, &req.zone_id, &pid).await {
                Ok(s) => s,
                Err(e) => {
                    let _ = reg.kill(&pid, 127);
                    let result = AcpResult {
                        pid: pid.clone(),
                        agent_id: req.agent_id.clone(),
                        exit_code: 127,
                        stderr: e.to_string(),
                        ..Default::default()
                    };
                    let _ = self.persist_result(&result, &req.zone_id, &user_prompt);
                    self.fire_on_terminate(&pid);
                    return Ok(result);
                }
            };

        // Track active session for kill_agent + cleanup.
        self.active.insert(
            pid.clone(),
            ActiveSession {
                agent_id: req.agent_id.clone(),
                fd_paths: [
                    paths::proc_fd(&req.zone_id, &pid, 0),
                    paths::proc_fd(&req.zone_id, &pid, 1),
                    paths::proc_fd(&req.zone_id, &pid, 2),
                ],
            },
        );

        // Build the AcpConnection over the parent-side OwnedFds.
        let outcome = self
            .run_session(
                &mut subproc,
                &cfg,
                fs_read,
                fs_write,
                &prompt,
                timeout,
                req.session_id.as_deref(),
                &host_cwd,
            )
            .await;

        // Tear down: drop connection (fd close), unregister DT_PIPEs,
        // kill subprocess, wait for exit, mark TERMINATED in registry.
        subproc.unregister_pipes(&self.kernel);
        subproc.kill().await;
        let _ = subproc.wait().await;
        self.active.remove(&pid);

        let mut exit_code = 0;
        let mut response = String::new();
        let mut stderr = String::new();
        let mut timed_out = false;
        let mut metadata = serde_json::Map::new();

        match outcome {
            Ok(SessionOutcome {
                turn,
                prompt_result,
            }) => {
                response = turn.text;
                metadata = build_metadata(&prompt_result, turn.num_turns);
            }
            Err(SessionError::Timeout) => {
                exit_code = -1;
                timed_out = true;
                stderr = format!("Agent timed out after {}s", req.timeout_secs);
            }
            Err(SessionError::Protocol(msg)) => {
                exit_code = 1;
                stderr = format!("ACP RPC error: {msg}");
            }
            Err(SessionError::Other(msg)) => {
                exit_code = -1;
                stderr = msg;
            }
        }

        let _ = reg.kill(&pid, exit_code);
        self.fire_on_terminate(&pid);

        let result = AcpResult {
            pid,
            agent_id: req.agent_id.clone(),
            exit_code,
            response,
            raw_stdout: stderr.clone(),
            stderr,
            timed_out,
            metadata,
        };
        let _ = self.persist_result(&result, &req.zone_id, &user_prompt);
        Ok(result)
    }

    fn compose_prompt(
        &self,
        agent_id: &str,
        zone_id: &str,
        user_prompt: &str,
        cfg: &AgentConfig,
    ) -> String {
        let system = self
            .get_system_prompt(agent_id, zone_id)
            .or_else(|| cfg.default_system_prompt.clone());
        let skills = self.get_enabled_skills(agent_id, zone_id);
        if system.is_none() && skills.is_none() {
            return user_prompt.to_string();
        }
        let mut rules: Vec<String> = Vec::new();
        if let Some(s) = system {
            rules.push(s);
        }
        if let Some(skill_list) = skills {
            let mut lines = Vec::with_capacity(skill_list.len());
            for sk in skill_list {
                let name = sk.get("name").and_then(Value::as_str).unwrap_or("");
                let path = sk.get("path").and_then(Value::as_str).unwrap_or("");
                let desc = sk.get("description").and_then(Value::as_str).unwrap_or("");
                lines.push(format!(
                    "<skill name=\"{name}\" path=\"{path}\">{desc}</skill>"
                ));
            }
            rules.push(format!(
                "<enabled-skills>\n{}\n</enabled-skills>",
                lines.join("\n")
            ));
        }
        format!(
            "[Assistant Rules - You MUST follow these instructions]\n{}\n\n[User Request]\n{}",
            rules.join("\n"),
            user_prompt
        )
    }

    async fn run_session(
        self: &Arc<Self>,
        subproc: &mut AcpSubprocess,
        _cfg: &AgentConfig,
        fs_read: FsRead,
        fs_write: FsWrite,
        prompt: &str,
        timeout: Duration,
        session_id: Option<&str>,
        cwd: &Path,
    ) -> Result<SessionOutcome, SessionError> {
        let (stdin, stdout, _stderr) = subproc
            .take_stdio_for_connection()
            .map_err(|e| SessionError::Other(e.to_string()))?;

        let conn = AcpConnection::new(
            stdout,
            stdin,
            cwd.to_path_buf(),
            Some(fs_read),
            Some(fs_write),
        );

        let initialize_timeout = Duration::from_secs(30);
        let session_timeout = Duration::from_secs(30);

        if let Err(e) = conn.initialize(initialize_timeout).await {
            return Err(map_jsonrpc_err(e));
        }
        if let Some(sid) = session_id {
            if !conn.supports_load_session() {
                return Err(SessionError::Other(format!(
                    "Agent does not support session resume (sessionId={sid})"
                )));
            }
            if let Err(e) = conn.session_load(sid, Some(cwd), session_timeout).await {
                return Err(map_jsonrpc_err(e));
            }
        } else if let Err(e) = conn.session_new(Some(cwd), session_timeout).await {
            return Err(map_jsonrpc_err(e));
        }

        let prompt_result = match conn.send_prompt(prompt, timeout).await {
            Ok(p) => p,
            Err(e) => {
                conn.disconnect().await;
                return Err(map_jsonrpc_err(e));
            }
        };

        // The observer's accumulator returned with the prompt result;
        // pull num_turns out of the prompt result's session id field
        // is not there — we plumb it via AgentTurnResult the prompt
        // call already finalised. Reconstruct from prompt_result.
        let turn = AgentTurnResult {
            text: prompt_result.text.clone(),
            stop_reason: prompt_result.stop_reason.clone(),
            model: prompt_result.model.clone(),
            usage: prompt_result.accumulated_usage.clone(),
            // num_turns is observed inside the prompt; surface via
            // metadata builder. For now zero here is the shape Python
            // surfaces when no tool calls fired.
            num_turns: 0,
            tool_calls: Vec::new(),
            thinking: None,
        };

        conn.disconnect().await;
        Ok(SessionOutcome {
            turn,
            prompt_result,
        })
    }
}

#[cfg(unix)]
struct SessionOutcome {
    turn: AgentTurnResult,
    prompt_result: super::connection::AcpPromptResult,
}

#[cfg(unix)]
enum SessionError {
    Timeout,
    Protocol(String),
    Other(String),
}

#[cfg(unix)]
fn map_jsonrpc_err(e: super::jsonrpc::JsonRpcError) -> SessionError {
    use super::jsonrpc::JsonRpcError;
    match e {
        JsonRpcError::Timeout => SessionError::Timeout,
        JsonRpcError::Protocol { message, .. } => SessionError::Protocol(message),
        other => SessionError::Other(other.to_string()),
    }
}

// ── Free helpers ────────────────────────────────────────────────────────

fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Map a host filesystem path to a VFS path. Mirror of Python
/// `_host_to_vfs`. Outside-cwd paths land under `__external__` as a
/// containment boundary. Output always uses forward slashes (VFS
/// paths are POSIX-shaped regardless of host OS).
pub(crate) fn host_to_vfs(host_path: &str, host_cwd: &Path, vfs_root: &str) -> String {
    let p = Path::new(host_path);
    let abs = if p.is_absolute() {
        PathBuf::from(p)
    } else {
        host_cwd.join(p)
    };
    let normalised = normalise(&abs);
    if let Ok(rel) = normalised.strip_prefix(host_cwd) {
        if !rel.starts_with("..") {
            return forward_slashes(&format!("{vfs_root}/{}", rel.to_string_lossy()));
        }
    }
    forward_slashes(&format!(
        "{vfs_root}/__external__{}",
        normalised.to_string_lossy()
    ))
}

fn forward_slashes(s: &str) -> String {
    s.replace('\\', "/")
}

fn normalise(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[cfg(unix)]
fn build_metadata(
    prompt_result: &super::connection::AcpPromptResult,
    num_turns: u32,
) -> serde_json::Map<String, Value> {
    let mut meta = serde_json::Map::new();
    if let Some(model) = prompt_result.model.as_ref() {
        meta.insert("model".to_string(), Value::String(model.clone()));
    }
    if let Some(sid) = prompt_result.session_id.as_ref() {
        meta.insert("session_id".to_string(), Value::String(sid.clone()));
    }
    if let Some(usage_obj) = prompt_result.usage.as_object() {
        for (k, v) in usage_obj {
            meta.insert(k.clone(), v.clone());
        }
    }
    for (k, v) in &prompt_result.accumulated_usage {
        meta.insert(format!("accumulated_{k}"), v.clone());
    }
    if num_turns > 0 {
        meta.insert("num_turns".to_string(), json!(num_turns));
    }
    meta
}

// AcpSubprocess gains take_stdio_for_connection() in this commit too —
// see subprocess.rs.

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct MockRegistry {
        next_pid: AtomicUsize,
        kills: parking_lot::Mutex<Vec<(String, i32)>>,
        spawns: parking_lot::Mutex<Vec<(String, String, String)>>,
        descriptors: parking_lot::Mutex<Vec<AgentDescriptor>>,
    }

    impl MockRegistry {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                next_pid: AtomicUsize::new(1000),
                kills: parking_lot::Mutex::new(Vec::new()),
                spawns: parking_lot::Mutex::new(Vec::new()),
                descriptors: parking_lot::Mutex::new(Vec::new()),
            })
        }
    }

    impl AgentRegistry for MockRegistry {
        fn spawn(
            &self,
            name: &str,
            owner_id: &str,
            zone_id: &str,
            labels: HashMap<String, String>,
        ) -> Result<String, String> {
            let pid = format!("pid-{}", self.next_pid.fetch_add(1, Ordering::Relaxed));
            self.spawns
                .lock()
                .push((name.to_string(), owner_id.to_string(), zone_id.to_string()));
            self.descriptors.lock().push(AgentDescriptor {
                pid: pid.clone(),
                name: name.to_string(),
                owner_id: owner_id.to_string(),
                zone_id: zone_id.to_string(),
                state: "REGISTERED".into(),
                labels,
            });
            Ok(pid)
        }

        fn kill(&self, pid: &str, exit_code: i32) -> Result<(), String> {
            self.kills.lock().push((pid.to_string(), exit_code));
            Ok(())
        }

        fn list_processes(
            &self,
            zone_id: Option<&str>,
            owner_id: Option<&str>,
            service_label_match: Option<&str>,
        ) -> Vec<AgentDescriptor> {
            self.descriptors
                .lock()
                .iter()
                .filter(|d| zone_id.is_none_or(|z| d.zone_id == z))
                .filter(|d| owner_id.is_none_or(|o| d.owner_id == o))
                .filter(|d| {
                    service_label_match
                        .is_none_or(|s| d.labels.get("service").is_some_and(|v| v == s))
                })
                .cloned()
                .collect()
        }
    }

    fn fresh_service() -> (Arc<AcpService>, Arc<MockRegistry>) {
        let kernel = Arc::new(Kernel::new());
        let svc = Arc::new(AcpService::new(kernel, "root".to_string()));
        let reg = MockRegistry::new();
        svc.set_agent_registry(Arc::clone(&reg) as Arc<dyn AgentRegistry>);
        (svc, reg)
    }

    #[test]
    fn list_agents_filters_by_service_label() {
        let (svc, reg) = fresh_service();
        let _ = reg
            .spawn(
                "acp:claude",
                "alice",
                "root",
                HashMap::from([("service".into(), "acp".into())]),
            )
            .unwrap();
        let _ = reg
            .spawn(
                "managed:agent",
                "alice",
                "root",
                HashMap::from([("service".into(), "managed_agent".into())]),
            )
            .unwrap();
        let agents = svc.list_agents(None, None).unwrap();
        assert_eq!(agents.len(), 1);
        assert_eq!(agents[0].name, "acp:claude");
    }

    #[test]
    fn kill_agent_calls_registry_kill_and_fires_callbacks() {
        let (svc, reg) = fresh_service();
        let pid = reg
            .spawn("acp:claude", "alice", "root", HashMap::new())
            .unwrap();
        let fired = Arc::new(AtomicUsize::new(0));
        let fired_clone = Arc::clone(&fired);
        svc.register_on_terminate(
            "lease-revoke",
            Arc::new(move |_p: &str| {
                fired_clone.fetch_add(1, Ordering::Relaxed);
            }) as OnTerminateCallback,
        );
        svc.kill_agent(&pid).unwrap();
        let kills = reg.kills.lock().clone();
        assert_eq!(kills, vec![(pid.clone(), -9)]);
        assert_eq!(fired.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn register_on_terminate_dedups_by_id() {
        let (svc, _reg) = fresh_service();
        svc.register_on_terminate("a", Arc::new(|_: &str| {}) as OnTerminateCallback);
        svc.register_on_terminate("a", Arc::new(|_: &str| {}) as OnTerminateCallback);
        // Internal list size is 1.
        assert_eq!(svc.on_terminate.read().unwrap().len(), 1);
    }

    #[test]
    fn host_to_vfs_inside_cwd_uses_relative() {
        let cwd = PathBuf::from("/work/proj");
        let v = host_to_vfs("/work/proj/src/main.rs", &cwd, "/root/workspace");
        assert_eq!(v, "/root/workspace/src/main.rs");
    }

    #[test]
    fn host_to_vfs_relative_resolved_against_cwd() {
        let cwd = PathBuf::from("/work/proj");
        let v = host_to_vfs("src/main.rs", &cwd, "/root/workspace");
        assert_eq!(v, "/root/workspace/src/main.rs");
    }

    #[test]
    fn host_to_vfs_outside_cwd_lands_under_external() {
        let cwd = PathBuf::from("/work/proj");
        let v = host_to_vfs("/etc/passwd", &cwd, "/root/workspace");
        assert!(v.starts_with("/root/workspace/__external__"), "got {v}");
        assert!(v.contains("etc/passwd"), "got {v}");
    }

    #[test]
    fn ctx_is_a_system_principal_with_admin() {
        let c = AcpService::ctx();
        assert_eq!(c.user_id, "system");
        assert!(c.is_admin);
        assert!(c.is_system);
    }

    #[test]
    fn registry_returns_not_bound_when_unset() {
        let kernel = Arc::new(Kernel::new());
        let svc = AcpService::new(kernel, "root".into());
        match svc.registry() {
            Ok(_) => panic!("expected NotBound error"),
            Err(e) => assert!(matches!(e, AcpServiceError::NotBound("AgentRegistry"))),
        }
    }
}
