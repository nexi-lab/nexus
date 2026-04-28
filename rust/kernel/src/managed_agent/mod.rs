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

use crate::service_registry::RustService;
use crate::core::agents::table::{AgentDescriptor, AgentKind, AgentState, AgentTable};

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod session;
pub(crate) mod workspace_boundary_hook;

use session::{alloc_pid, alloc_session_id, now_ms, Session};

// ── Public request / response shapes ────────────────────────────────────

#[derive(Clone, Debug, Default)]
pub(crate) struct WorkspaceRepo {
    pub host_path: String,
    pub alias: String,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct StartSessionRequest {
    pub agent: String,
    pub repos: Vec<WorkspaceRepo>,
    pub model: String,
    pub owner_id: String,
    pub zone_id: String,
}

#[derive(Clone, Debug)]
pub(crate) struct StartSessionResponse {
    pub session_id: String,
    pub agent_id: String,
    pub workspace_path: String,
}

#[derive(Clone, Debug)]
pub(crate) struct GetSessionResponse {
    pub session_id: String,
    pub agent_id: String,
    pub agent: String,
    pub workspace_path: String,
    pub model: String,
    pub state: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CancelMode {
    Turn,
    Session,
}

#[derive(Clone, Copy, Debug, Default)]
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
    agent_table: Arc<AgentTable>,
    sessions: DashMap<String, Session>,
}

impl ManagedAgentService {
    pub(crate) const NAME: &'static str = "managed_agent";

    pub(crate) fn new(agent_table: Arc<AgentTable>) -> Self {
        Self {
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
    pub(crate) fn install(kernel: &crate::kernel::Kernel) -> Result<(), String> {
        kernel.register_native_hook(Box::new(
            workspace_boundary_hook::WorkspaceBoundaryHook::new(),
        ));
        kernel.register_native_hook(Box::new(mailbox_stamping_hook::MailboxStampingHook::new()));

        let svc = Arc::new(Self::new(Arc::clone(&kernel.agent_table)));
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

        let _ = req.repos; // consumed by the runtime crate when it lands
        let sess = Session {
            session_id: session_id.clone(),
            pid: pid.clone(),
            agent: req.agent,
            model: req.model,
            workspace_path: workspace_path.clone(),
        };
        self.sessions.insert(session_id.clone(), sess);

        Ok(StartSessionResponse {
            session_id,
            agent_id: pid,
            workspace_path,
        })
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
}
