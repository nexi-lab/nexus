//! `ManagedAgentService` — Rust-flavoured service that owns the
//! managed-agent surface: the chat-with-me + workspace hooks plus the
//! session lifecycle behind the `proto/nexus/grpc/managed_agent` gRPC
//! contract.
//!
//! Registered to the kernel `ServiceRegistry` as a Rust service via the
//! `Kernel::register_rust_service` surface (parallel of `add_mount` for
//! drivers). Pre-existing services (AcpService for unmanaged agents,
//! AgentRegistry, ReBAC, …) keep their Python implementations; this is
//! the first Rust-flavoured service to land alongside them, owning
//! `AgentKind::MANAGED` agents end-to-end.
//!
//! Today's responsibilities, all generic to `AgentKind::MANAGED` (not
//! sudo-code-specific):
//!
//!   * On `install`, register `MailboxStampingHook` and
//!     `WorkspaceBoundaryHook` into the kernel's `KernelDispatch` so
//!     every `*/chat-with-me` write is stamped and every cross-owner
//!     `/proc/{pid}/workspace/` write is rejected.
//!   * On `enlist_rust`, take the place in the registry that
//!     `nx.service("managed_agent")` resolves to (Python lookup
//!     returns None — this service is reachable from Rust callers via
//!     `service_registry.lookup_rust("managed_agent")`).
//!   * `start_session` / `cancel` / `get_session` — Rust-native
//!     session lifecycle that talks directly to `AgentTable` (the
//!     Rust SSOT for agent state). Zero PyO3 boundary; managed agents
//!     don't go through Python `AgentRegistry` because their PCB
//!     metadata (cwd / external_info / subprocess handle) doesn't
//!     apply — those are unmanaged-agent fields.
//!
//! The actual managed-agent runtime (the sudo-code Rust crate that
//! drives the LLM loop after `start_session` allocates a pid) is a
//! separate Cargo dep that lands later. Today's `start_session` plants
//! the AgentRegistry record and returns the session identity tuple;
//! the runtime spawn is tracked separately so the gRPC contract works
//! ahead of the runtime crate.

// Until the tonic gRPC handler + runtime crate dep land, the
// session-lifecycle surface (request / response shapes,
// start_session / cancel / get_session) is reachable only from tests.
// The dead-code allowances below stop the unused-symbol warnings; each
// gets used as soon as its consumer commits.
#![allow(dead_code)]

use std::sync::Arc;

use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use kernel::core::agents::table::{AgentDescriptor, AgentKind, AgentState, AgentTable};
use kernel::service_registry::{RustCallError, RustService};

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod session;
pub(crate) mod workspace_boundary_hook;

use session::{alloc_pid, alloc_session_id, now_ms, Session};

// ── Public request / response shapes ────────────────────────────────────

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct WorkspaceRepo {
    pub host_path: String,
    pub alias: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct StartSessionRequest {
    pub agent: String,
    #[serde(default)]
    pub repos: Vec<WorkspaceRepo>,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub owner_id: String,
    #[serde(default)]
    pub zone_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct StartSessionResponse {
    pub session_id: String,
    pub agent_id: String,
    pub workspace_path: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct GetSessionResponse {
    pub session_id: String,
    pub agent_id: String,
    pub agent: String,
    pub workspace_path: String,
    pub model: String,
    pub state: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum CancelMode {
    Turn,
    Session,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct CancelRequest {
    pub session_id: String,
    pub mode: CancelMode,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct GetSessionRequest {
    pub session_id: String,
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub(crate) struct CancelResponse {
    pub cancelled: bool,
}

#[derive(Debug)]
pub(crate) enum ManagedAgentError {
    InvalidArgument(String),
    UnknownSession(String),
    Internal(String),
}

impl std::fmt::Display for ManagedAgentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidArgument(m) => write!(f, "invalid argument: {m}"),
            Self::UnknownSession(s) => write!(f, "unknown session_id {s:?}"),
            Self::Internal(m) => write!(f, "internal: {m}"),
        }
    }
}

impl std::error::Error for ManagedAgentError {}

// ── Service ─────────────────────────────────────────────────────────────

pub(crate) struct ManagedAgentService {
    /// Shared kernel handle for `start_session` / `cancel` to call
    /// `sys_setattr` / `sys_unlink` against — the path on which the
    /// workspace materialization (DT_DIR + DT_LINK chat-with-me +
    /// per-repo DT_LINKs) happens.  `Option` so the existing test
    /// fixtures that build `ManagedAgentService::new(agent_table)`
    /// without a real kernel keep compiling; production callers
    /// construct via [`Self::install`] which always provides the
    /// kernel.
    kernel: Option<Arc<kernel::kernel::Kernel>>,
    agent_table: Arc<AgentTable>,
    sessions: DashMap<String, Session>,
}

impl ManagedAgentService {
    pub(crate) const NAME: &'static str = "managed_agent";

    /// Test-only constructor — leaves the kernel handle empty so unit
    /// tests can exercise lifecycle bookkeeping (start_session /
    /// cancel / get_session) without a real kernel.  Workspace
    /// materialization is skipped when `kernel` is `None`.
    pub(crate) fn new(agent_table: Arc<AgentTable>) -> Self {
        Self {
            kernel: None,
            agent_table,
            sessions: DashMap::new(),
        }
    }

    /// Production constructor used by [`Self::install`] — passes the
    /// kernel handle through so workspace paths can be materialised
    /// inside `start_session`.
    pub(crate) fn with_kernel(
        kernel: Arc<kernel::kernel::Kernel>,
        agent_table: Arc<AgentTable>,
    ) -> Self {
        Self {
            kernel: Some(kernel),
            agent_table,
            sessions: DashMap::new(),
        }
    }

    /// Install the service into a freshly-constructed kernel:
    ///
    ///   1. Register the chat-with-me + workspace-boundary hooks into
    ///      the kernel's `KernelDispatch`.
    ///   2. Enlist the service into `ServiceRegistry` so future tonic
    ///      gRPC handlers + Python factory wiring can resolve it via
    ///      `service_registry.lookup_rust(NAME)`.
    ///
    /// Called from `Kernel::new()`. The service holds an `Arc<AgentTable>`
    /// — the same `Arc` `Kernel` keeps for `AgentStatusResolver` reads —
    /// so `start_session` mutates the same SSOT every other agent
    /// surface reads from.
    pub(crate) fn install(kernel: &Arc<kernel::kernel::Kernel>) -> Result<(), String> {
        kernel.register_native_hook(Box::new(
            workspace_boundary_hook::WorkspaceBoundaryHook::new(),
        ));
        kernel.register_native_hook(Box::new(mailbox_stamping_hook::MailboxStampingHook::new()));

        // Holding `Arc<Kernel>` inside the service does create a
        // Kernel ↔ Service Arc cycle, but services live for process
        // lifetime — same convention AcpService follows.  The
        // workspace materialisation in `start_session` requires the
        // owned Arc so it can issue `sys_setattr`/`sys_unlink`
        // against the kernel.
        let svc = Arc::new(Self::with_kernel(
            Arc::clone(kernel),
            Arc::clone(&kernel.agent_table),
        ));
        kernel.register_rust_service(Self::NAME, svc as Arc<dyn RustService>, Vec::new())
    }

    // ── Session lifecycle ─────────────────────────────────────────────

    /// Allocate a managed-agent session. Plants a fresh AgentRegistry
    /// record (`AgentTable::register` directly — no Python boundary)
    /// and returns the session identity tuple sudowork uses for
    /// follow-up cancel / get_session calls and chat-with-me writes.
    ///
    /// State on success: pid is `WARMING_UP` in AgentTable; the session
    /// row is in `self.sessions`. On `register` collision (effectively
    /// impossible given uuid-allocated pids) we surface `Internal` so
    /// the caller sees a hard error.
    pub(crate) fn start_session(
        &self,
        req: StartSessionRequest,
    ) -> Result<StartSessionResponse, ManagedAgentError> {
        if req.agent.is_empty() {
            return Err(ManagedAgentError::InvalidArgument(
                "'agent' is required".into(),
            ));
        }
        let owner_id = if req.owner_id.is_empty() {
            "system".to_string()
        } else {
            req.owner_id.clone()
        };
        let zone_id = if req.zone_id.is_empty() {
            "root".to_string()
        } else {
            req.zone_id.clone()
        };

        let pid = alloc_pid();
        let workspace_path = format!("/proc/{pid}/workspace/");
        let session_id = alloc_session_id();

        let desc = AgentDescriptor {
            pid: pid.clone(),
            name: req.agent.clone(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id,
            zone_id,
            created_at_ms: now_ms(),
            exit_code: None,
            parent_pid: None,
            connection_id: None,
            last_heartbeat_ms: None,
        };

        if !self.agent_table.register(desc) {
            return Err(ManagedAgentError::Internal(format!(
                "AgentTable.register collided on freshly-allocated pid {pid}"
            )));
        }
        // Move into WARMING_UP — the runtime crate is responsible for
        // the WARMING_UP → READY transition once it finishes
        // initialising the agent loop. The transition is best-effort:
        // a failure here would drop us back to REGISTERED, which the
        // runtime crate will still see as "spawn me" so it's
        // recoverable.
        self.agent_table.update_state(&pid, AgentState::WarmingUp);

        // Materialise the workspace inside the kernel — DT_DIR for the
        // workspace root, DT_LINK at chat-with-me pointing at the
        // per-pid stream, and one DT_LINK per requested repo aliased
        // under the workspace.  Pre-runtime-crate the runtime hasn't
        // been wired in yet, so a materialise failure is logged but
        // doesn't abort the session: the AgentTable record + session
        // map have already been planted, and a follow-up materialise
        // call (post-warmup) can close the gap.
        if let Err(e) = self.materialize_workspace(&workspace_path, &pid, &req.repos) {
            tracing::warn!(pid=%pid, error=%e, "workspace materialization failed");
        }

        let sess = Session {
            session_id: session_id.clone(),
            pid: pid.clone(),
            agent: req.agent,
            model: req.model,
            workspace_path: workspace_path.clone(),
            repo_aliases: req.repos.iter().map(|r| r.alias.clone()).collect(),
        };
        self.sessions.insert(session_id.clone(), sess);

        Ok(StartSessionResponse {
            session_id,
            agent_id: pid,
            workspace_path,
        })
    }

    // ── Workspace materialisation ─────────────────────────────────────

    /// Build `/proc/{pid}/workspace/`, the chat-with-me DT_LINK, and a
    /// DT_LINK per requested repo alias.  No-ops when the service was
    /// built without a kernel handle (test fixtures).
    fn materialize_workspace(
        &self,
        workspace_path: &str,
        pid: &str,
        repos: &[WorkspaceRepo],
    ) -> Result<(), String> {
        let Some(kernel) = self.kernel.as_ref() else {
            return Ok(());
        };

        // /proc and /proc/{pid} need to exist before /proc/{pid}/
        // workspace/ — sys_setattr(DT_DIR) is idempotent for
        // already-present dirs, so this is safe across restarts.
        let pid_root = format!("/proc/{pid}");
        let proc_root = "/proc";
        for dir in [proc_root, &pid_root, workspace_path.trim_end_matches('/')] {
            create_dt_dir(kernel, dir)
                .map_err(|e| format!("sys_setattr(DT_DIR at {dir:?}): {e}"))?;
        }

        // chat-with-me DT_LINK — workspace shortcut to
        // /proc/{pid}/chat-with-me so an agent writing relative to
        // its workspace cwd reaches its own mailbox without a
        // procfs path.
        let cwm_link = format!("{}chat-with-me", trailing_slash(workspace_path));
        let cwm_target = format!("/proc/{pid}/chat-with-me");
        create_dt_link(kernel, &cwm_link, &cwm_target)?;

        // Per-repo DT_LINKs — every repo aliased under the workspace
        // becomes a one-hop link to the host VFS path sudowork passed
        // in.  `WorkspaceRepo.host_path` is treated as a VFS path
        // (callers that need OS-level visibility mount the host FS
        // first; that's an out-of-scope concern of this service).
        for repo in repos {
            if repo.alias.is_empty() || repo.host_path.is_empty() {
                continue;
            }
            let link = format!("{}{}", trailing_slash(workspace_path), repo.alias);
            create_dt_link(kernel, &link, &repo.host_path)?;
        }
        Ok(())
    }

    /// Reverse of [`Self::materialize_workspace`] — remove every
    /// DT_LINK and the workspace directory.  Best-effort: missing
    /// entries (e.g. a partial materialise that failed) are ignored.
    fn reap_workspace(&self, workspace_path: &str, repo_aliases: &[String]) {
        let Some(kernel) = self.kernel.as_ref() else {
            return;
        };
        let ctx = kernel::kernel::OperationContext::new(
            "managed_agent_service",
            "root",
            /* is_admin */ true,
            None,
            /* is_system */ true,
        );

        // Reap entries directly through the metastore.  We could go
        // through `sys_unlink` for the full validate→route→hooks
        // path, but the workspace tree lives entirely in the kernel's
        // default in-memory metastore (sys_setattr created the
        // entries with `io_profile="memory"`), and a metastore
        // round-trip is the one path guaranteed to clear them
        // regardless of which mounts the running deployment has
        // configured.  `let _ = ...` keeps the reap best-effort —
        // missing entries from a partial materialisation are not an
        // error.  ctx is intentionally unused here; the metastore
        // surface is identity-free.
        let _ = ctx;
        for alias in repo_aliases {
            if alias.is_empty() {
                continue;
            }
            let link = format!("{}{}", trailing_slash(workspace_path), alias);
            let _ = kernel.metastore_delete(link.trim_end_matches('/'));
        }
        let cwm_link = format!("{}chat-with-me", trailing_slash(workspace_path));
        let _ = kernel.metastore_delete(cwm_link.trim_end_matches('/'));
        let _ = kernel.metastore_delete(workspace_path.trim_end_matches('/'));
    }

    /// Cancel an in-flight turn or terminate the entire session.
    ///
    /// `Turn` — abort the current generation; AgentTable record stays.
    /// The runtime crate observes the cancellation through whatever
    /// mechanism it picks (channel, atomic flag, …) — kernel doesn't
    /// know about turn boundaries.
    ///
    /// `Session` — terminate: transition AgentTable to `Terminated`,
    /// drop the session row. The runtime crate observes the state
    /// transition and shuts down the agent task.
    pub(crate) fn cancel(
        &self,
        session_id: &str,
        mode: CancelMode,
    ) -> Result<CancelResponse, ManagedAgentError> {
        let sess = match self.sessions.get(session_id) {
            Some(s) => s.clone(),
            None => return Err(ManagedAgentError::UnknownSession(session_id.to_string())),
        };

        match mode {
            CancelMode::Turn => {
                // No state transition; the runtime watches a
                // separate signal. Today this is a no-op at the
                // kernel layer — the runtime crate will plug in once
                // it lands.
                Ok(CancelResponse { cancelled: true })
            }
            CancelMode::Session => {
                let cancelled =
                    self.agent_table
                        .update_state_with_exit(&sess.pid, AgentState::Terminated, 0);
                // Tear down the workspace before we drop the session
                // row — DT_LINK targets live independently, so leaving
                // them dangling would clutter `sys_listdir(/proc)`
                // until the kernel restarts.
                self.reap_workspace(&sess.workspace_path, &sess.repo_aliases);
                self.sessions.remove(session_id);
                Ok(CancelResponse { cancelled })
            }
        }
    }

    /// Read-through liveness snapshot. Cheap by design; the live
    /// message flow uses `sys_watch` over `/proc/{pid}/chat-with-me`,
    /// not this RPC.
    pub(crate) fn get_session(
        &self,
        session_id: &str,
    ) -> Result<GetSessionResponse, ManagedAgentError> {
        let sess = match self.sessions.get(session_id) {
            Some(s) => s.clone(),
            None => return Err(ManagedAgentError::UnknownSession(session_id.to_string())),
        };
        let state = self
            .agent_table
            .get(&sess.pid)
            .map(|d| d.state.as_str().to_lowercase())
            .unwrap_or_else(|| "terminated".to_string());
        Ok(GetSessionResponse {
            session_id: sess.session_id,
            agent_id: sess.pid,
            agent: sess.agent,
            workspace_path: sess.workspace_path,
            model: sess.model,
            state,
        })
    }
}

impl From<ManagedAgentError> for RustCallError {
    fn from(e: ManagedAgentError) -> Self {
        match e {
            ManagedAgentError::InvalidArgument(m) => Self::InvalidArgument(m),
            ManagedAgentError::UnknownSession(s) => {
                Self::InvalidArgument(format!("unknown session_id {s:?}"))
            }
            ManagedAgentError::Internal(m) => Self::Internal(m),
        }
    }
}

// ── Free helpers (file-private) ────────────────────────────────────────
//
// Workspace materialisation calls `sys_setattr` with `zone_id="root"`
// + `io_profile="memory"` — the kernel default for non-mounted paths.
// No OperationContext is needed: `sys_setattr` resolves zone routing
// internally from the `zone_id` argument, and per-write authorization
// for agent paths is enforced by the `WorkspaceBoundaryHook`
// registered alongside this service.

/// Append `/` to a workspace prefix if not already present so we can
/// safely concatenate segment names below.
fn trailing_slash(p: &str) -> String {
    if p.ends_with('/') {
        p.to_string()
    } else {
        format!("{p}/")
    }
}

/// Create a DT_DIR at `path`.  Wraps the kernel's 16-arg
/// `sys_setattr` so callers stay readable; idempotent on
/// already-present directories.
fn create_dt_dir(kernel: &kernel::kernel::Kernel, path: &str) -> Result<(), String> {
    // DT_DIR = 1.  Kernel keeps the constant `pub(crate)` inside
    // `kernel::core::dcache`; encode the contract value here.
    const DT_DIR: i32 = 1;
    kernel
        .sys_setattr(
            path,
            DT_DIR,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            /* io_profile */ "memory",
            /* zone_id */ "root",
            /* is_external */ false,
            /* capacity */ 0,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* link_target */ None,
        )
        .map(|_| ())
        .map_err(|e| format!("{e:?}"))
}

/// Create a DT_LINK at `path` pointing at `target`.  Wraps the kernel's
/// 16-arg `sys_setattr` so callers stay readable.
fn create_dt_link(
    kernel: &kernel::kernel::Kernel,
    path: &str,
    target: &str,
) -> Result<(), String> {
    // DT_LINK = 6.  See `create_dt_dir` for the same constant-
    // visibility note.
    const DT_LINK: i32 = 6;
    kernel
        .sys_setattr(
            path,
            DT_LINK,
            /* backend_name */ "",
            /* backend */ None,
            /* metastore */ None,
            /* raft_backend */ None,
            /* io_profile */ "memory",
            /* zone_id */ "root",
            /* is_external */ false,
            /* capacity */ 0,
            /* read_fd */ None,
            /* write_fd */ None,
            /* mime_type */ None,
            /* modified_at_ms */ None,
            /* link_target */ Some(target),
        )
        .map(|_| ())
        .map_err(|e| format!("sys_setattr(DT_LINK at {path} -> {target}): {e:?}"))
}

impl RustService for ManagedAgentService {
    fn name(&self) -> &str {
        Self::NAME
    }

    fn start(&self) -> Result<(), String> {
        // Hooks were registered at `install` time so they're live from
        // kernel boot. No async state to spin up today; tonic gRPC
        // handler wiring goes here once that lands.
        Ok(())
    }

    fn stop(&self) -> Result<(), String> {
        Ok(())
    }

    /// Route the three session-lifecycle methods exposed over
    /// `NexusVFSService.Call`. Method names are versioned so the wire
    /// contract can evolve without breaking older sudowork clients.
    fn dispatch(&self, method: &str, payload: &[u8]) -> Result<Vec<u8>, RustCallError> {
        match method {
            "start_session_v1" => {
                let req: StartSessionRequest = serde_json::from_slice(payload)
                    .map_err(|e| RustCallError::InvalidArgument(e.to_string()))?;
                let resp = self.start_session(req)?;
                serde_json::to_vec(&resp).map_err(|e| RustCallError::Internal(e.to_string()))
            }
            "cancel_v1" => {
                let req: CancelRequest = serde_json::from_slice(payload)
                    .map_err(|e| RustCallError::InvalidArgument(e.to_string()))?;
                let resp = self.cancel(&req.session_id, req.mode)?;
                serde_json::to_vec(&resp).map_err(|e| RustCallError::Internal(e.to_string()))
            }
            "get_session_v1" => {
                let req: GetSessionRequest = serde_json::from_slice(payload)
                    .map_err(|e| RustCallError::InvalidArgument(e.to_string()))?;
                let resp = self.get_session(&req.session_id)?;
                serde_json::to_vec(&resp).map_err(|e| RustCallError::Internal(e.to_string()))
            }
            _ => Err(RustCallError::NotFound),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh_service() -> ManagedAgentService {
        ManagedAgentService::new(Arc::new(AgentTable::new()))
    }

    fn req(agent: &str) -> StartSessionRequest {
        StartSessionRequest {
            agent: agent.to_string(),
            repos: Vec::new(),
            model: "claude-sonnet-4-6".to_string(),
            owner_id: "ethan".to_string(),
            zone_id: "root".to_string(),
        }
    }

    #[test]
    fn service_has_canonical_name() {
        let svc = fresh_service();
        assert_eq!(svc.name(), "managed_agent");
        assert_eq!(ManagedAgentService::NAME, "managed_agent");
    }

    #[test]
    fn lifecycle_methods_succeed_on_empty_service() {
        let svc = fresh_service();
        svc.start().unwrap();
        svc.stop().unwrap();
    }

    #[test]
    fn start_session_returns_identity_tuple_and_plants_agent_table_record() {
        let table = Arc::new(AgentTable::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();

        assert!(resp.session_id.starts_with("sess-"));
        assert!(resp.agent_id.starts_with("pid-"));
        assert_eq!(
            resp.workspace_path,
            format!("/proc/{}/workspace/", resp.agent_id)
        );

        let desc = table
            .get(&resp.agent_id)
            .expect("AgentTable record present");
        assert_eq!(desc.name, "scode-standard");
        assert_eq!(desc.kind, AgentKind::Managed);
        assert_eq!(desc.state, AgentState::WarmingUp);
        assert_eq!(desc.owner_id, "ethan");
        assert_eq!(desc.zone_id, "root");
    }

    #[test]
    fn start_session_rejects_empty_agent_name() {
        let svc = fresh_service();
        let err = svc.start_session(req("")).unwrap_err();
        assert!(matches!(err, ManagedAgentError::InvalidArgument(_)));
    }

    #[test]
    fn start_session_defaults_owner_and_zone() {
        let svc = fresh_service();
        let r = StartSessionRequest {
            agent: "scode-standard".to_string(),
            ..Default::default()
        };
        let resp = svc.start_session(r).unwrap();
        let desc = svc.agent_table.get(&resp.agent_id).unwrap();
        assert_eq!(desc.owner_id, "system");
        assert_eq!(desc.zone_id, "root");
    }

    #[test]
    fn cancel_session_terminates_pid_and_drops_session() {
        let table = Arc::new(AgentTable::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let pid = resp.agent_id.clone();
        let session_id = resp.session_id.clone();

        let r = svc.cancel(&session_id, CancelMode::Session).unwrap();
        assert!(r.cancelled);

        let desc = table.get(&pid).unwrap();
        assert_eq!(desc.state, AgentState::Terminated);
        assert_eq!(desc.exit_code, Some(0));

        // Second cancel surfaces UnknownSession (the row was dropped).
        let err = svc.cancel(&session_id, CancelMode::Session).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn cancel_turn_keeps_pid_alive() {
        let table = Arc::new(AgentTable::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let pid = resp.agent_id.clone();

        let r = svc.cancel(&resp.session_id, CancelMode::Turn).unwrap();
        assert!(r.cancelled);
        // pid still WARMING_UP — turn cancel doesn't terminate.
        let desc = table.get(&pid).unwrap();
        assert_eq!(desc.state, AgentState::WarmingUp);
        // session row still present — get_session still works.
        let _ = svc.get_session(&resp.session_id).unwrap();
    }

    #[test]
    fn cancel_unknown_session_errors() {
        let svc = fresh_service();
        let err = svc.cancel("sess-bogus", CancelMode::Session).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn get_session_returns_state_from_agent_table() {
        let svc = fresh_service();
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let snap = svc.get_session(&resp.session_id).unwrap();
        assert_eq!(snap.session_id, resp.session_id);
        assert_eq!(snap.agent_id, resp.agent_id);
        assert_eq!(snap.agent, "scode-standard");
        assert_eq!(snap.workspace_path, resp.workspace_path);
        assert_eq!(snap.model, "claude-sonnet-4-6");
        assert_eq!(snap.state, "warming_up");
    }

    #[test]
    fn get_session_surfaces_terminated_for_reaped_pid() {
        let table = Arc::new(AgentTable::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        // Simulate out-of-band reap (e.g. SIGKILL from operator).
        table.unregister(&resp.agent_id);
        let snap = svc.get_session(&resp.session_id).unwrap();
        assert_eq!(snap.state, "terminated");
    }

    #[test]
    fn get_session_unknown_session_errors() {
        let svc = fresh_service();
        let err = svc.get_session("sess-bogus").unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    // ── dispatch round-trip ─────────────────────────────────────────

    mod dispatch {
        use super::*;
        use serde_json::json;

        #[test]
        fn start_session_v1_round_trip() {
            let svc = fresh_service();
            let payload = json!({
                "agent": "scode-standard",
                "model": "claude-sonnet-4-6",
                "owner_id": "ethan",
                "zone_id": "root",
                "repos": [{"host_path": "/x/repo", "alias": "repo"}],
            })
            .to_string();
            let bytes = svc
                .dispatch("start_session_v1", payload.as_bytes())
                .unwrap();
            let resp: StartSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(resp.session_id.starts_with("sess-"));
            assert!(resp.agent_id.starts_with("pid-"));
            assert_eq!(
                resp.workspace_path,
                format!("/proc/{}/workspace/", resp.agent_id)
            );
        }

        #[test]
        fn start_session_v1_defaults_optional_fields() {
            let svc = fresh_service();
            let payload = json!({"agent": "scode-standard"}).to_string();
            let bytes = svc
                .dispatch("start_session_v1", payload.as_bytes())
                .unwrap();
            let resp: StartSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(resp.session_id.starts_with("sess-"));
        }

        #[test]
        fn cancel_v1_session_round_trip() {
            let svc = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id, "mode": "session"}).to_string();
            let bytes = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap();
            let cancel: CancelResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(cancel.cancelled);
        }

        #[test]
        fn cancel_v1_turn_round_trip() {
            let svc = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id, "mode": "turn"}).to_string();
            let bytes = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap();
            let cancel: CancelResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(cancel.cancelled);
        }

        #[test]
        fn cancel_v1_unknown_session_surfaces_invalid_argument() {
            let svc = fresh_service();
            let payload = json!({"session_id": "sess-bogus", "mode": "session"}).to_string();
            let err = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap_err();
            assert!(matches!(err, RustCallError::InvalidArgument(_)));
        }

        #[test]
        fn get_session_v1_round_trip() {
            let svc = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id}).to_string();
            let bytes = svc.dispatch("get_session_v1", payload.as_bytes()).unwrap();
            let snap: GetSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(snap.session_id, resp.session_id);
            assert_eq!(snap.state, "warming_up");
        }

        #[test]
        fn unknown_method_returns_not_found() {
            let svc = fresh_service();
            let err = svc.dispatch("does_not_exist", b"{}").unwrap_err();
            assert!(matches!(err, RustCallError::NotFound));
        }

        #[test]
        fn malformed_payload_surfaces_invalid_argument() {
            let svc = fresh_service();
            let err = svc
                .dispatch("start_session_v1", b"this is not json")
                .unwrap_err();
            assert!(matches!(err, RustCallError::InvalidArgument(_)));
        }
    }

    /// Materialisation tests — exercise the real `sys_setattr` /
    /// `sys_unlink` paths through a freshly-constructed `Kernel`.
    /// Pure-Rust setup, no PyO3.
    mod materialize {
        use super::*;
        use kernel::kernel::Kernel;

        /// DT entry-type constants the kernel keeps `pub(crate)`.
        const DT_DIR: u8 = 1;
        const DT_LINK: u8 = 6;

        /// Helper that queries the metastore directly — sys_stat
        /// requires routing setup that isn't relevant to verifying
        /// materialisation; the metastore is the kernel SSOT for
        /// FileMetadata and is what sys_setattr writes through.
        /// Returns `(entry_type, link_target)` when the path exists.
        fn kernel_stat(
            kernel: &Kernel,
            path: &str,
            _zone: &str,
        ) -> Option<(u8, Option<String>)> {
            let path = path.trim_end_matches('/');
            let entry = kernel.metastore_get(path).ok().flatten()?;
            Some((entry.entry_type, entry.link_target.clone()))
        }

        /// Build a `ManagedAgentService` with a real Kernel inside —
        /// the only setup needed is `Kernel::new` (no PyO3 boot).
        fn svc_with_kernel() -> (Arc<Kernel>, ManagedAgentService) {
            let k = Arc::new(Kernel::new());
            let svc = ManagedAgentService::with_kernel(
                Arc::clone(&k),
                Arc::clone(&k.agent_table),
            );
            (k, svc)
        }

        #[test]
        fn materialize_workspace_directly_succeeds() {
            // Smoke test the helper in isolation so a future
            // regression points the finger at materialize, not
            // start_session.
            let (kernel, svc) = svc_with_kernel();
            svc.materialize_workspace("/proc/pid-test/workspace/", "pid-test", &[])
                .expect("materialize should succeed on fresh kernel");
            // The dir must be visible to subsequent reads via metastore.
            let entry = kernel
                .metastore_get("/proc/pid-test/workspace")
                .expect("metastore lookup")
                .expect("workspace dir entry");
            assert_eq!(entry.entry_type, DT_DIR);
        }

        #[test]
        fn start_session_creates_workspace_dir_and_chat_with_me_link() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let pid = &resp.agent_id;

            // 1. Workspace dir exists as DT_DIR.
            let (et, _) = kernel_stat(&kernel, &resp.workspace_path, "root")
                .expect("workspace dir should exist");
            assert_eq!(et, DT_DIR);

            // 2. chat-with-me link exists and points at /proc/{pid}/chat-with-me.
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            let (et, target) = kernel_stat(&kernel, &cwm, "root")
                .expect("chat-with-me DT_LINK should exist");
            assert_eq!(et, DT_LINK);
            assert_eq!(
                target.as_deref(),
                Some(format!("/proc/{pid}/chat-with-me").as_str()),
                "chat-with-me link target",
            );
        }

        #[test]
        fn start_session_creates_per_repo_dt_links() {
            let (kernel, svc) = svc_with_kernel();
            let mut r = req("scode-standard");
            r.repos = vec![
                WorkspaceRepo {
                    host_path: "/host/repos/myrepo".into(),
                    alias: "myrepo".into(),
                },
                WorkspaceRepo {
                    host_path: "/host/repos/another".into(),
                    alias: "another".into(),
                },
            ];
            let resp = svc.start_session(r).unwrap();

            for (alias, expected_target) in [
                ("myrepo", "/host/repos/myrepo"),
                ("another", "/host/repos/another"),
            ] {
                let link_path = format!("{}{}", &resp.workspace_path, alias);
                let (et, target) = kernel_stat(&kernel, &link_path, "root")
                    .unwrap_or_else(|| panic!("repo DT_LINK {link_path:?} missing"));
                assert_eq!(et, DT_LINK, "DT_LINK for repo {alias}");
                assert_eq!(target.as_deref(), Some(expected_target));
            }
        }

        #[test]
        fn cancel_session_reaps_workspace_and_links() {
            let (kernel, svc) = svc_with_kernel();
            let mut r = req("scode-standard");
            r.repos = vec![WorkspaceRepo {
                host_path: "/host/repos/myrepo".into(),
                alias: "myrepo".into(),
            }];
            let resp = svc.start_session(r).unwrap();

            // Sanity: pre-cancel, the link exists.
            let alias_link = format!("{}myrepo", &resp.workspace_path);
            assert!(kernel_stat(&kernel,&alias_link, "root").is_some());

            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();

            // Post-cancel: the per-repo link, the chat-with-me link,
            // and the workspace directory itself are all gone.
            for path in [
                &alias_link,
                &format!("{}chat-with-me", &resp.workspace_path),
                &resp.workspace_path,
            ] {
                assert!(
                    kernel_stat(&kernel,path, "root").is_none(),
                    "expected {path:?} to be reaped after cancel"
                );
            }
        }

        #[test]
        fn cancel_turn_does_not_reap_workspace() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();

            svc.cancel(&resp.session_id, CancelMode::Turn).unwrap();

            // Turn-mode cancel keeps the AgentTable record + workspace
            // alive — the runtime watches a separate signal.
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            assert!(kernel_stat(&kernel,&cwm, "root").is_some());
            assert!(kernel_stat(&kernel,&resp.workspace_path, "root").is_some());
        }
    }
}
