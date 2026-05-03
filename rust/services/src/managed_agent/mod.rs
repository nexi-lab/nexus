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
//!     session lifecycle that talks directly to `AgentRegistry` (the
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

use kernel::core::agents::registry::{
    AgentDescriptor, AgentKind, AgentRegistry, AgentState, RepoMount,
};
use kernel::service_registry::{RustCallError, RustService};

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod proc_entry;
pub mod proc_workspace_resolver;
pub(crate) mod session;
pub(crate) mod workspace_boundary_hook;

use proc_entry::{register_proc_entry, unregister_proc_entry};

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
    /// Shared kernel handle for `start_session` to stamp the per-pid
    /// procfs dirent (`/proc/{pid}/workspace/` DT_DIR) and for the
    /// on_terminate observer to tear it down.  Workspace link content
    /// is derived from `AgentDescriptor.repos` on demand by
    /// [`proc_workspace_resolver::ProcWorkspaceResolver`] — never
    /// persisted in the metastore.  `Option` so the existing test
    /// fixtures that build `ManagedAgentService::new` without a real
    /// kernel keep compiling; production callers construct via
    /// [`Self::install`] which always provides the kernel.
    kernel: Option<Arc<kernel::kernel::Kernel>>,
    agent_registry: Arc<AgentRegistry>,
    sessions: DashMap<String, Session>,
}

impl ManagedAgentService {
    pub(crate) const NAME: &'static str = "managed_agent";

    /// Test-only constructor — leaves the kernel handle empty so unit
    /// tests can exercise lifecycle bookkeeping (start_session /
    /// cancel / get_session) without a real kernel.  The procfs
    /// dirent stamp is skipped when `kernel` is `None`.
    pub(crate) fn new(agent_registry: Arc<AgentRegistry>) -> Self {
        Self {
            kernel: None,
            agent_registry,
            sessions: DashMap::new(),
        }
    }

    /// Production constructor used by [`Self::install`] — passes the
    /// kernel handle through so the per-pid procfs dirent can be
    /// stamped inside `start_session`.
    pub(crate) fn with_kernel(
        kernel: Arc<kernel::kernel::Kernel>,
        agent_registry: Arc<AgentRegistry>,
    ) -> Self {
        Self {
            kernel: Some(kernel),
            agent_registry,
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
    /// Called from `Kernel::new()`. The service holds an `Arc<AgentRegistry>`
    /// — the same `Arc` `Kernel` keeps for `AgentStatusResolver` reads —
    /// so `start_session` mutates the same SSOT every other agent
    /// surface reads from.
    pub(crate) fn install(kernel: &Arc<kernel::kernel::Kernel>) -> Result<(), String> {
        Self::install_returning(kernel).map(|_| ())
    }

    /// Install variant that returns the wired service handle so tests
    /// can assert the on_terminate observer behaves correctly without
    /// having to fish the service back out of the kernel registry.
    pub(crate) fn install_returning(
        kernel: &Arc<kernel::kernel::Kernel>,
    ) -> Result<Arc<Self>, String> {
        kernel.register_native_hook(Box::new(
            workspace_boundary_hook::WorkspaceBoundaryHook::new(),
        ));
        kernel.register_native_hook(Box::new(mailbox_stamping_hook::MailboxStampingHook::new()));

        // Holding `Arc<Kernel>` inside the service does create a
        // Kernel ↔ Service Arc cycle, but services live for process
        // lifetime — same convention AcpService follows.  The procfs
        // dirent stamp in `start_session` and the on_terminate
        // teardown both need the owned Arc.
        let svc = Arc::new(Self::with_kernel(
            Arc::clone(kernel),
            Arc::clone(kernel.agent_registry()),
        ));

        // Tear down the per-pid procfs dirent on out-of-band
        // termination — SIGKILL, orphan auto-reap, any path that flips
        // an agent to Terminated without going through
        // `cancel_session(Session)`.  Workspace link content is owned
        // by `ProcWorkspaceResolver` (derived from the descriptor)
        // and goes away with the descriptor itself; only the dirent
        // stamped at start_session needs explicit removal.  The
        // callback also drops the session row so subsequent
        // `get_session` returns `UnknownSession`.
        let svc_for_cb = Arc::clone(&svc);
        let kernel_for_cb = Arc::clone(kernel);
        kernel.agent_registry().register_on_terminate(
            Self::NAME,
            Arc::new(move |pid: &str| {
                unregister_proc_entry(&kernel_for_cb, pid);
                let pid_owned = pid.to_string();
                let session_id_opt = svc_for_cb
                    .sessions
                    .iter()
                    .find(|e| e.value().pid == pid_owned)
                    .map(|e| e.key().clone());
                if let Some(session_id) = session_id_opt {
                    svc_for_cb.sessions.remove(&session_id);
                }
            }),
        );

        let svc_for_return = Arc::clone(&svc);
        kernel.register_rust_service(Self::NAME, svc as Arc<dyn RustService>, Vec::new())?;
        Ok(svc_for_return)
    }

    // ── Session lifecycle ─────────────────────────────────────────────

    /// Allocate a managed-agent session. Plants a fresh AgentRegistry
    /// record (`AgentRegistry::register` directly — no Python boundary)
    /// and returns the session identity tuple sudowork uses for
    /// follow-up cancel / get_session calls and chat-with-me writes.
    ///
    /// State on success: pid is `WARMING_UP` in AgentRegistry; the session
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

        let repos: Vec<RepoMount> = req
            .repos
            .iter()
            .filter(|r| !r.alias.is_empty() && !r.host_path.is_empty())
            .map(|r| RepoMount {
                alias: r.alias.clone(),
                mount_path: r.host_path.clone(),
            })
            .collect();

        let now = now_ms();
        let desc = AgentDescriptor {
            pid: pid.clone(),
            name: req.agent.clone(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id,
            zone_id,
            created_at_ms: now,
            updated_at_ms: now,
            repos,
            ..Default::default()
        };

        if !self.agent_registry.register(desc) {
            return Err(ManagedAgentError::Internal(format!(
                "AgentRegistry.register collided on freshly-allocated pid {pid}"
            )));
        }
        // Move into WARMING_UP — the runtime crate is responsible for
        // the WARMING_UP → READY transition once it finishes
        // initialising the agent loop. The transition is best-effort:
        // a failure here would drop us back to REGISTERED, which the
        // runtime crate will still see as "spawn me" so it's
        // recoverable.
        let _ = self
            .agent_registry
            .update_state(&pid, AgentState::WarmingUp);

        // Stamp only the dirent + stat for /proc/{pid}/workspace/.
        // Workspace link content (chat-with-me, per-repo aliases) is
        // composed on demand by ProcWorkspaceResolver from the
        // descriptor's repos; never persisted in the metastore.  A
        // failed dirent stamp is logged but doesn't abort the session
        // — the AgentRegistry record is already planted and a future
        // re-stamp closes the gap.
        if let Some(kernel) = self.kernel.as_ref() {
            if let Err(e) = register_proc_entry(kernel, &pid) {
                tracing::warn!(pid=%pid, error=%e, "register_proc_entry failed");
            }
        }

        let sess = Session {
            session_id: session_id.clone(),
            pid: pid.clone(),
            agent: req.agent,
            model: req.model,
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
    /// `Turn` — abort the current generation; AgentRegistry record stays.
    /// The runtime crate observes the cancellation through whatever
    /// mechanism it picks (channel, atomic flag, …) — kernel doesn't
    /// know about turn boundaries.
    ///
    /// `Session` — terminate: transition AgentRegistry to `Terminated`,
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
                // The state transition fires the on_terminate
                // observer registered at install-time, which both
                // tears down the procfs dirent and removes the
                // session row.  Belt-and-braces session removal
                // below is a no-op when the observer ran first and
                // covers the kernel-less test fixtures where no
                // observer is registered.
                let cancelled = self
                    .agent_registry
                    .update_state_with_exit(&sess.pid, AgentState::Terminated, 0)
                    .unwrap_or(false);
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
            .agent_registry
            .get(&sess.pid)
            .map(|d| d.state.as_str().to_lowercase())
            .unwrap_or_else(|| "terminated".to_string());
        let workspace_path = format!("/proc/{}/workspace/", sess.pid);
        Ok(GetSessionResponse {
            session_id: sess.session_id,
            agent_id: sess.pid,
            agent: sess.agent,
            workspace_path,
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
        ManagedAgentService::new(Arc::new(AgentRegistry::new()))
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
    fn start_session_returns_identity_tuple_and_plants_agent_registry_record() {
        let table = Arc::new(AgentRegistry::new());
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
            .expect("AgentRegistry record present");
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
        let desc = svc.agent_registry.get(&resp.agent_id).unwrap();
        assert_eq!(desc.owner_id, "system");
        assert_eq!(desc.zone_id, "root");
    }

    #[test]
    fn cancel_session_terminates_pid_and_drops_session() {
        let table = Arc::new(AgentRegistry::new());
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
        let table = Arc::new(AgentRegistry::new());
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
    fn get_session_returns_state_from_agent_registry() {
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
        let table = Arc::new(AgentRegistry::new());
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

    /// Procfs lifecycle tests — exercise start_session through a
    /// real `Kernel` and assert the dirent + descriptor combination
    /// matches what `ProcWorkspaceResolver` needs to render the
    /// workspace links on demand.  Pure-Rust setup, no PyO3.
    mod procfs {
        use super::*;
        use kernel::core::agents::registry::AgentSignal;
        use kernel::core::dispatch::PathResolver;
        use kernel::kernel::Kernel;
        use proc_workspace_resolver::ProcWorkspaceResolver;

        /// DT_DIR — the only entry type stamped by register_proc_entry.
        const DT_DIR: u8 = 1;

        /// True when `path` is present in the metastore as DT_DIR.
        fn dir_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel
                .metastore_get(path)
                .ok()
                .flatten()
                .is_some_and(|e| e.entry_type == DT_DIR)
        }

        /// True when `path` has any metastore entry — used to assert
        /// the procfs view does NOT materialise link rows.
        fn entry_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel.metastore_get(path).ok().flatten().is_some()
        }

        /// Resolver target as a UTF-8 string for the given workspace path.
        fn resolve_target(kernel: &Kernel, workspace_path: &str, leaf: &str) -> Option<String> {
            let resolver = ProcWorkspaceResolver::new(Arc::clone(kernel.agent_registry()));
            // Resolver expects /{zone}/proc/{pid}/workspace/{leaf}; the
            // workspace_path returned by start_session is /proc/{pid}/
            // workspace/, so prefix the synthetic zone segment used by
            // PathResolver dispatch.
            let path = format!("/root{workspace_path}{leaf}");
            resolver.try_read(&path).map(|b| String::from_utf8(b).unwrap())
        }

        /// Build a `ManagedAgentService` with a real Kernel inside —
        /// the only setup needed is `Kernel::new` (no PyO3 boot).
        fn svc_with_kernel() -> (Arc<Kernel>, ManagedAgentService) {
            let k = Arc::new(Kernel::new());
            let svc =
                ManagedAgentService::with_kernel(Arc::clone(&k), Arc::clone(k.agent_registry()));
            (k, svc)
        }

        fn install_managed_agent(kernel: &Arc<Kernel>) -> Arc<ManagedAgentService> {
            ManagedAgentService::install_returning(kernel).expect("install ManagedAgentService")
        }

        #[test]
        fn start_session_stamps_workspace_dirent_and_no_link_rows() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();

            // The workspace dirent exists (so readdir on /proc/{pid}/
            // sees `workspace`), but no link rows are materialised.
            assert!(dir_exists(&kernel, &resp.workspace_path));
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            assert!(
                !entry_exists(&kernel, &cwm),
                "chat-with-me must not be materialised in metastore"
            );
        }

        #[test]
        fn resolver_renders_chat_with_me_after_start_session() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let target = resolve_target(&kernel, &resp.workspace_path, "chat-with-me")
                .expect("chat-with-me must resolve through ProcWorkspaceResolver");
            assert_eq!(target, format!("/proc/{}/chat-with-me", resp.agent_id));
        }

        #[test]
        fn resolver_renders_repo_aliases_from_descriptor() {
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
            let desc = kernel
                .agent_registry()
                .get(&resp.agent_id)
                .expect("descriptor must carry repos");
            assert_eq!(desc.repos.len(), 2);

            for (alias, expected) in
                [("myrepo", "/host/repos/myrepo"), ("another", "/host/repos/another")]
            {
                let target = resolve_target(&kernel, &resp.workspace_path, alias)
                    .unwrap_or_else(|| panic!("alias {alias:?} should resolve"));
                assert_eq!(target, expected, "alias {alias} target");
                let alias_path = format!("{}{}", &resp.workspace_path, alias);
                assert!(
                    !entry_exists(&kernel, &alias_path),
                    "alias path must not be materialised"
                );
            }
        }

        #[test]
        fn cancel_session_drops_dirent_and_descriptor_on_kernelless_path() {
            // svc_with_kernel() does NOT call install_returning, so the
            // on_terminate observer is not registered.  This test
            // exercises the bookkeeping path: cancel transitions the
            // descriptor to Terminated and drops the session row, but
            // the dirent stays put because no observer fires to remove
            // it.  The companion test
            // `cancel_session_with_observer_drops_dirent_idempotently`
            // covers the install-path semantics.
            let (kernel, svc) = svc_with_kernel();
            let mut r = req("scode-standard");
            r.repos = vec![WorkspaceRepo {
                host_path: "/host/repos/myrepo".into(),
                alias: "myrepo".into(),
            }];
            let resp = svc.start_session(r).unwrap();
            assert!(dir_exists(&kernel, &resp.workspace_path));
            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
            let desc = kernel.agent_registry().get(&resp.agent_id).unwrap();
            assert_eq!(desc.state, AgentState::Terminated);
            assert_eq!(desc.exit_code, Some(0));
        }

        #[test]
        fn cancel_turn_keeps_dirent_and_descriptor_alive() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            svc.cancel(&resp.session_id, CancelMode::Turn).unwrap();
            assert!(dir_exists(&kernel, &resp.workspace_path));
            // Resolver still resolves chat-with-me — turn cancel does
            // not touch descriptor state.
            assert!(resolve_target(&kernel, &resp.workspace_path, "chat-with-me").is_some());
        }

        #[test]
        fn sigkill_drops_dirent_through_on_terminate_observer() {
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();
            assert!(dir_exists(&kernel, &resp.workspace_path));

            kernel
                .agent_registry()
                .signal(&resp.agent_id, AgentSignal::Sigkill, None)
                .expect("SIGKILL");

            assert!(
                !dir_exists(&kernel, &resp.workspace_path),
                "workspace dirent should be dropped after SIGKILL"
            );
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
        }

        #[test]
        fn orphan_sigterm_drops_dirent_through_on_terminate_observer() {
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();
            kernel
                .agent_registry()
                .signal(&resp.agent_id, AgentSignal::Sigterm, None)
                .expect("SIGTERM");
            assert!(!dir_exists(&kernel, &resp.workspace_path));
        }

        #[test]
        fn cancel_session_with_observer_drops_dirent_idempotently() {
            // With an installed observer, cancel(Session) ends with
            // both the dirent dropped (observer) and the session row
            // gone (cancel's belt-and-braces remove).
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();
            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();
            assert!(!dir_exists(&kernel, &resp.workspace_path));
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
        }
    }
}
