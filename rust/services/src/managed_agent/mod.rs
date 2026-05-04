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
use sudo_code::spawn_task::{spawn_task, SpawnHandle};

pub(crate) mod mailbox_stamping_hook;
pub(crate) mod mailbox_stamping_policy;
pub(crate) mod proc_entry;
pub(crate) mod session;
pub(crate) mod workspace_boundary_hook;

use proc_entry::{register_proc_entry, unregister_proc_entry};

/// Label key used to stash the LLM model id on the descriptor so
/// `get_session` can echo it back without a sidecar table.  Read by
/// `GetSessionResponse.model`; the runtime crate may also read it
/// when wiring the loop.
const MODEL_LABEL: &str = "model";

use session::{alloc_pid, now_ms};

// ── Public request / response shapes ────────────────────────────────────

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct WorkspaceRepo {
    pub host_path: String,
    pub alias: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct StartSessionRequest {
    /// Static agent profile id (e.g. `scode-standard`) — names the
    /// directory under `/agents/{agent_id}/`.  Same `agent_id`
    /// terminology the ACP service uses.
    pub agent_id: String,
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
    /// AgentRegistry pid for the spawned managed agent.  cancel /
    /// get_session take this back.
    pub session_id: String,
    pub workspace_path: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct GetSessionResponse {
    pub session_id: String,
    /// Static agent profile id (mirrors `StartSessionRequest.agent_id`).
    pub agent_id: String,
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
    /// procfs subtree (`/proc/{pid}/`, `/proc/{pid}/workspace/`,
    /// workspace shortcut DT_LINK, per-repo alias DT_LINKs) and for the
    /// on_terminate observer to tear it down.  `Option` so the existing
    /// test fixtures that build `ManagedAgentService::new` without a
    /// real kernel keep compiling; production callers construct via
    /// [`Self::install`] which always provides the kernel.
    kernel: Option<Arc<kernel::kernel::Kernel>>,
    agent_registry: Arc<AgentRegistry>,
    /// Sidecar table of live sudo-code spawn handles, keyed by pid.
    /// AgentRegistry is the SSOT for descriptor state but has no place
    /// for an OS thread join handle, so this rides alongside.
    /// `start_session` inserts after `register_proc_entry` succeeds;
    /// `cancel` and the `on_terminate` observer abort the
    /// `HookAbortSignal` and remove the entry. `Arc<DashMap>` (rather
    /// than a plain field) so the on_terminate closure can hold a
    /// clone without depending on the service's `Arc<Self>`.
    spawn_handles: Arc<DashMap<String, SpawnHandle>>,
}

impl ManagedAgentService {
    pub(crate) const NAME: &'static str = "managed_agent";

    /// Test-only constructor — leaves the kernel handle empty so unit
    /// tests can exercise lifecycle bookkeeping (start_session /
    /// cancel / get_session) without a real kernel.  The procfs
    /// entries are skipped when `kernel` is `None`.
    pub(crate) fn new(agent_registry: Arc<AgentRegistry>) -> Self {
        Self {
            kernel: None,
            agent_registry,
            spawn_handles: Arc::new(DashMap::new()),
        }
    }

    /// Production constructor used by [`Self::install`] — passes the
    /// kernel handle through so the per-pid procfs entries can be
    /// stamped inside `start_session`.
    pub(crate) fn with_kernel(
        kernel: Arc<kernel::kernel::Kernel>,
        agent_registry: Arc<AgentRegistry>,
    ) -> Self {
        Self {
            kernel: Some(kernel),
            agent_registry,
            spawn_handles: Arc::new(DashMap::new()),
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

        // Tear down the per-pid procfs subtree on out-of-band
        // termination — SIGKILL, orphan auto-reap, any path that flips
        // an agent to Terminated without going through
        // `cancel_session(Session)`.  `fire_on_terminate` runs before
        // `AgentRegistry::reap` on the orphan path, so the descriptor
        // is still reachable here and we can use its `repos` to drop
        // the per-alias DT_LINK rows alongside the dirents.  The
        // descriptor itself is reaped by AgentRegistry after the
        // observer returns, so subsequent `get_session` returns
        // `UnknownSession`.
        let kernel_for_cb = Arc::clone(kernel);
        let registry_for_cb = Arc::clone(kernel.agent_registry());
        let spawn_handles_for_cb = Arc::clone(&svc.spawn_handles);
        kernel.agent_registry().register_on_terminate(
            Self::NAME,
            Arc::new(move |pid: &str| {
                // Drop the spawn handle FIRST so the sudo-code loop
                // sees the abort while procfs is still present —
                // ensures the in-flight sys_read returns cleanly
                // (path-still-there) rather than crashing on a
                // half-torn-down subtree.  remove() takes ownership
                // and runs the SpawnHandle's destructor (drops the
                // JoinHandle, detaching the worker thread); the
                // worker exits on the next poll.
                if let Some((_, handle)) = spawn_handles_for_cb.remove(pid) {
                    handle.abort_signal.abort();
                }
                if let Some(desc) = registry_for_cb.get(pid) {
                    unregister_proc_entry(&kernel_for_cb, &desc);
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
    /// `session_id` and `agent_id` are the same value: the AgentRegistry
    /// pid.  No second identifier is allocated — the descriptor is the
    /// SSOT for everything cancel / get_session needs.  On `register`
    /// collision (effectively impossible given uuid-allocated pids) we
    /// surface `Internal` so the caller sees a hard error.
    pub(crate) fn start_session(
        &self,
        req: StartSessionRequest,
    ) -> Result<StartSessionResponse, ManagedAgentError> {
        if req.agent_id.is_empty() {
            return Err(ManagedAgentError::InvalidArgument(
                "'agent_id' is required".into(),
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

        let repos: Vec<RepoMount> = req
            .repos
            .iter()
            .filter(|r| !r.alias.is_empty() && !r.host_path.is_empty())
            .map(|r| RepoMount {
                alias: r.alias.clone(),
                mount_path: r.host_path.clone(),
            })
            .collect();

        let mut labels = std::collections::HashMap::new();
        if !req.model.is_empty() {
            labels.insert(MODEL_LABEL.to_string(), req.model.clone());
        }

        let now = now_ms();
        let desc = AgentDescriptor {
            pid: pid.clone(),
            name: req.agent_id.clone(),
            kind: AgentKind::Managed,
            state: AgentState::Registered,
            owner_id,
            zone_id,
            created_at_ms: now,
            updated_at_ms: now,
            labels,
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

        // Stamp the per-pid procfs subtree: dirents for /proc/,
        // /proc/{pid}/, /proc/{pid}/workspace/, plus the workspace
        // shortcut DT_LINK and one DT_LINK per repo alias. VFSRouter
        // follows the DT_LINK rows transparently on read/write.  A
        // failed stamp is logged but doesn't abort the session — the
        // AgentRegistry record is already planted and a future
        // re-stamp closes the gap.
        if let Some(kernel) = self.kernel.as_ref() {
            if let Some(desc) = self.agent_registry.get(&pid) {
                if let Err(e) = register_proc_entry(kernel, &desc) {
                    tracing::warn!(pid=%pid, error=%e, "register_proc_entry failed");
                } else {
                    // Stamp succeeded — spawn the sudo-code
                    // managed-agent loop. The handle goes into the
                    // sidecar table; cancel(Turn|Session) and the
                    // on_terminate observer reach into the table to
                    // abort the loop. `desc` is cloned because
                    // spawn_task takes ownership for use inside the
                    // worker thread.
                    let handle = spawn_task(Arc::clone(kernel), desc);
                    self.spawn_handles.insert(pid.clone(), handle);
                }
            }
        }

        Ok(StartSessionResponse {
            session_id: pid,
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
    /// `Session` — terminate: transition AgentRegistry to `Terminated`.
    /// The on_terminate observer registered at install time tears down
    /// the per-pid procfs dirent.  The runtime crate observes the state
    /// transition and shuts down the agent task.
    pub(crate) fn cancel(
        &self,
        session_id: &str,
        mode: CancelMode,
    ) -> Result<CancelResponse, ManagedAgentError> {
        // session_id IS the pid in AgentRegistry (no second identifier).
        if self.agent_registry.get(session_id).is_none() {
            return Err(ManagedAgentError::UnknownSession(session_id.to_string()));
        }

        match mode {
            CancelMode::Turn => {
                // Turn cancel signals the in-flight sudo-code loop
                // through the shared `HookAbortSignal` — the same wire
                // the v1 `ConversationRuntime::with_hook_abort_signal`
                // builder reads.  AgentRegistry record stays so a
                // follow-up prompt write can still address this pid;
                // the sidecar entry stays in the map for the same
                // reason.  v0 stub's loop body has no per-turn
                // boundary, so today turn-cancel terminates the
                // worker thread on the next poll iteration —
                // documented behaviour, gets fixed when v1 wires
                // run_turn into the loop.
                if let Some(entry) = self.spawn_handles.get(session_id) {
                    entry.abort_signal.abort();
                }
                Ok(CancelResponse { cancelled: true })
            }
            CancelMode::Session => {
                // Abort the sudo-code loop FIRST so the worker thread
                // sees the signal before procfs disappears — keeps
                // the in-flight sys_read on a clean tear-down path.
                // The on_terminate observer registered at install
                // also performs this abort+remove for the SIGKILL /
                // orphan auto-reap path; here we do it explicitly
                // because cancel(Session) is the well-formed exit
                // route.  Both paths are idempotent against an
                // already-removed entry.
                if let Some((_, handle)) = self.spawn_handles.remove(session_id) {
                    handle.abort_signal.abort();
                }
                // `kill` transitions to Terminated (firing the
                // on_terminate observer that drops the procfs dirent)
                // and auto-reaps the descriptor when the agent is an
                // orphan — which managed agents always are today
                // (start_session passes parent_pid=None).  Reaping is
                // what surfaces `UnknownSession` on a follow-up
                // cancel / get_session.
                let cancelled = self
                    .agent_registry
                    .kill(session_id, 0)
                    .map(|_| true)
                    .unwrap_or(false);
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
        // session_id IS the pid; the descriptor is the SSOT.
        let desc = self
            .agent_registry
            .get(session_id)
            .ok_or_else(|| ManagedAgentError::UnknownSession(session_id.to_string()))?;
        let workspace_path = format!("/proc/{}/workspace/", desc.pid);
        let model = desc.labels.get(MODEL_LABEL).cloned().unwrap_or_default();
        Ok(GetSessionResponse {
            session_id: desc.pid.clone(),
            agent_id: desc.name.clone(),
            workspace_path,
            model,
            state: desc.state.as_str().to_lowercase(),
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

    fn req(agent_id: &str) -> StartSessionRequest {
        StartSessionRequest {
            agent_id: agent_id.to_string(),
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

        // session_id IS the pid — no second identifier.
        assert!(resp.session_id.starts_with("pid-"));
        assert_eq!(
            resp.workspace_path,
            format!("/proc/{}/workspace/", resp.session_id)
        );

        let desc = table
            .get(&resp.session_id)
            .expect("AgentRegistry record present");
        assert_eq!(desc.name, "scode-standard");
        assert_eq!(desc.kind, AgentKind::Managed);
        assert_eq!(desc.state, AgentState::WarmingUp);
        assert_eq!(desc.owner_id, "ethan");
        assert_eq!(desc.zone_id, "root");
        // Model lands on the descriptor as a label so get_session can
        // echo it back without a sidecar table.
        assert_eq!(
            desc.labels.get("model").map(String::as_str),
            Some("claude-sonnet-4-6")
        );
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
            agent_id: "scode-standard".to_string(),
            ..Default::default()
        };
        let resp = svc.start_session(r).unwrap();
        let desc = svc.agent_registry.get(&resp.session_id).unwrap();
        assert_eq!(desc.owner_id, "system");
        assert_eq!(desc.zone_id, "root");
    }

    #[test]
    fn cancel_session_terminates_pid_and_reaps_descriptor() {
        let table = Arc::new(AgentRegistry::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let pid = resp.session_id.clone();

        let r = svc.cancel(&pid, CancelMode::Session).unwrap();
        assert!(r.cancelled);

        // Managed agents are orphans (start_session passes parent_pid=
        // None), so AgentRegistry::kill auto-reaps the descriptor on
        // the Terminated transition.
        assert!(table.get(&pid).is_none());

        // Second cancel surfaces UnknownSession (descriptor reaped).
        let err = svc.cancel(&pid, CancelMode::Session).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn cancel_turn_keeps_pid_alive() {
        let table = Arc::new(AgentRegistry::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let pid = resp.session_id.clone();

        let r = svc.cancel(&pid, CancelMode::Turn).unwrap();
        assert!(r.cancelled);
        // pid still WARMING_UP — turn cancel doesn't terminate.
        let desc = table.get(&pid).unwrap();
        assert_eq!(desc.state, AgentState::WarmingUp);
        // Descriptor still present — get_session still works.
        let _ = svc.get_session(&pid).unwrap();
    }

    #[test]
    fn cancel_unknown_session_errors() {
        let svc = fresh_service();
        let err = svc.cancel("pid-bogus", CancelMode::Session).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn get_session_returns_state_from_agent_registry() {
        let svc = fresh_service();
        let resp = svc.start_session(req("scode-standard")).unwrap();
        let snap = svc.get_session(&resp.session_id).unwrap();
        assert_eq!(snap.session_id, resp.session_id);
        // agent_id in the response is the static profile name.
        assert_eq!(snap.agent_id, "scode-standard");
        assert_eq!(snap.workspace_path, resp.workspace_path);
        assert_eq!(snap.model, "claude-sonnet-4-6");
        assert_eq!(snap.state, "warming_up");
    }

    #[test]
    fn get_session_surfaces_unknown_for_reaped_pid() {
        // Pre-collapse, the service kept its own session row so a
        // get_session against a reaped pid returned the snapshot with
        // state="terminated".  Post-collapse the descriptor IS the
        // SSOT: once it's reaped, get_session must surface
        // UnknownSession.
        let table = Arc::new(AgentRegistry::new());
        let svc = ManagedAgentService::new(Arc::clone(&table));
        let resp = svc.start_session(req("scode-standard")).unwrap();
        table.unregister(&resp.session_id);
        let err = svc.get_session(&resp.session_id).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn get_session_unknown_session_errors() {
        let svc = fresh_service();
        let err = svc.get_session("pid-bogus").unwrap_err();
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
                "agent_id": "scode-standard",
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
            assert!(resp.session_id.starts_with("pid-"));
            assert_eq!(
                resp.workspace_path,
                format!("/proc/{}/workspace/", resp.session_id)
            );
        }

        #[test]
        fn start_session_v1_defaults_optional_fields() {
            let svc = fresh_service();
            let payload = json!({"agent_id": "scode-standard"}).to_string();
            let bytes = svc
                .dispatch("start_session_v1", payload.as_bytes())
                .unwrap();
            let resp: StartSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(resp.session_id.starts_with("pid-"));
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
            let payload = json!({"session_id": "pid-bogus", "mode": "session"}).to_string();
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

    /// Procfs lifecycle tests — exercise start_session through a real
    /// `Kernel` and assert the metastore carries the dirents + DT_LINK
    /// rows the integration doc §2.2 promises.  Pure-Rust setup, no
    /// PyO3.
    mod procfs {
        use super::*;
        use kernel::core::agents::registry::AgentSignal;
        use kernel::kernel::Kernel;

        const DT_DIR: u8 = 1;
        const DT_STREAM: u8 = 4;
        const DT_LINK: u8 = 6;

        /// True when `path` is present in the metastore as DT_DIR.
        fn dir_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel
                .metastore_get(path)
                .ok()
                .flatten()
                .is_some_and(|e| e.entry_type == DT_DIR)
        }

        /// True when `path` has any metastore entry.
        fn entry_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel.metastore_get(path).ok().flatten().is_some()
        }

        /// DT_LINK target string at `path` — None if the entry is
        /// missing or not a DT_LINK.
        fn link_target_at(kernel: &Kernel, path: &str) -> Option<String> {
            kernel
                .metastore_get(path)
                .ok()
                .flatten()
                .filter(|e| e.entry_type == DT_LINK)
                .and_then(|e| e.link_target)
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
        fn start_session_stamps_workspace_dirent_and_chat_with_me_link() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();

            assert!(dir_exists(&kernel, &resp.workspace_path));
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            assert_eq!(
                link_target_at(&kernel, &cwm).as_deref(),
                Some(format!("/proc/{}/chat-with-me", resp.session_id).as_str()),
            );
        }

        #[test]
        fn start_session_stamps_one_dt_link_per_repo() {
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
                .get(&resp.session_id)
                .expect("descriptor must carry repos");
            assert_eq!(desc.repos.len(), 2);

            for (alias, expected) in
                [("myrepo", "/host/repos/myrepo"), ("another", "/host/repos/another")]
            {
                let alias_path = format!("{}{}", &resp.workspace_path, alias);
                assert_eq!(
                    link_target_at(&kernel, &alias_path).as_deref(),
                    Some(expected),
                    "alias {alias} DT_LINK target",
                );
            }
        }

        #[test]
        fn cancel_session_reaps_descriptor_on_kernelless_path() {
            // svc_with_kernel() does NOT call install_returning, so the
            // on_terminate observer is not registered.  cancel(Session)
            // still runs `kill` which auto-reaps the orphan descriptor;
            // the procfs subtree however stays put because no observer
            // fires to remove it.  The companion test
            // `cancel_session_with_observer_reaps_descriptor_and_subtree`
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
            // Descriptor reaped → both get_session and a follow-up
            // cancel surface UnknownSession.
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
            assert!(kernel.agent_registry().get(&resp.session_id).is_none());
            // Dirent still in metastore — no observer ran.
            assert!(dir_exists(&kernel, &resp.workspace_path));
        }

        #[test]
        fn cancel_turn_keeps_subtree_and_descriptor_alive() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            svc.cancel(&resp.session_id, CancelMode::Turn).unwrap();
            assert!(dir_exists(&kernel, &resp.workspace_path));
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            assert!(
                link_target_at(&kernel, &cwm).is_some(),
                "chat-with-me DT_LINK should survive turn cancel",
            );
        }

        #[test]
        fn sigkill_drops_subtree_through_on_terminate_observer() {
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let mut r = req("scode-standard");
            r.repos = vec![WorkspaceRepo {
                host_path: "/host/core".into(),
                alias: "core".into(),
            }];
            let resp = svc.start_session(r).unwrap();
            assert!(dir_exists(&kernel, &resp.workspace_path));
            let alias_path = format!("{}core", &resp.workspace_path);
            assert!(entry_exists(&kernel, &alias_path));

            kernel
                .agent_registry()
                .signal(&resp.session_id, AgentSignal::Sigkill, None)
                .expect("SIGKILL");

            assert!(
                !dir_exists(&kernel, &resp.workspace_path),
                "workspace dirent should be dropped after SIGKILL",
            );
            assert!(
                !entry_exists(&kernel, &alias_path),
                "per-repo DT_LINK should be dropped after SIGKILL",
            );
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
        }

        #[test]
        fn orphan_sigterm_drops_subtree_through_on_terminate_observer() {
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();
            kernel
                .agent_registry()
                .signal(&resp.session_id, AgentSignal::Sigterm, None)
                .expect("SIGTERM");
            assert!(!dir_exists(&kernel, &resp.workspace_path));
            let cwm = format!("{}chat-with-me", &resp.workspace_path);
            assert!(!entry_exists(&kernel, &cwm));
        }

        /// Register `/proc` as a route entry on the kernel's
        /// VFSRouter. The mount carries no per-mount backend or
        /// metastore — `Kernel::with_metastore` falls back to the
        /// global metastore on miss, which is where sys_setattr's
        /// DT_DIR / DT_STREAM / DT_LINK writes land for these paths.
        /// Without this, `sys_read` / `sys_write` against any
        /// `/proc/*` path errors at `vfs_router.route()` before ever
        /// consulting the metastore.
        fn mount_proc(kernel: &Kernel) {
            kernel
                .vfs_router_arc()
                .add_mount("/proc", "root", None, false);
        }

        /// End-to-end cross-link: write through the workspace shortcut
        /// DT_LINK lands in the canonical chat-with-me DT_STREAM;
        /// reading the canonical path returns the bytes. Validates
        /// VFSRouter follows DT_LINK transparently for sys_write +
        /// sys_read — the load-bearing assumption behind dropping
        /// ProcWorkspaceResolver in favour of plain metastore DT_LINK
        /// rows.
        #[test]
        fn workspace_shortcut_write_lands_in_canonical_chat_with_me_stream() {
            use kernel::kernel::OperationContext;

            let kernel = Arc::new(Kernel::new());
            mount_proc(&kernel);
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();

            let shortcut = format!("{}chat-with-me", &resp.workspace_path);
            let canonical = format!("/proc/{}/chat-with-me", resp.session_id);
            // Pre-stamp the envelope's `from` field with the caller's
            // agent_id so MailboxStampingHook's rewrite is a no-op for
            // this test — keeps the assertion focused on "bytes
            // followed the DT_LINK to the canonical stream" without
            // coupling to the stamping policy.  The MailboxStamping
            // e2e companion exercises the rewrite path explicitly.
            let payload =
                br#"{"from":"scode-standard","to":"human-ethan","body":"ping"}"#;

            let ctx = OperationContext {
                user_id: "ethan".into(),
                zone_id: "root".into(),
                is_admin: false,
                agent_id: Some("scode-standard".into()),
                is_system: false,
                groups: vec![],
                admin_capabilities: vec![],
                subject_type: "user".into(),
                subject_id: None,
                request_id: "req-cross-link".into(),
                context_zone_id: None,
            };

            kernel
                .sys_write(&shortcut, &ctx, payload, 0)
                .expect("sys_write through workspace shortcut DT_LINK");

            let read = kernel
                .sys_read(&canonical, &ctx, /* timeout_ms */ 0, 0)
                .expect("sys_read on canonical chat-with-me");
            let bytes = read.data.expect("stream data present after write");
            assert_eq!(bytes.as_slice(), payload);
        }

        /// MailboxStampingHook end-to-end: a sys_write through the
        /// workspace shortcut DT_LINK runs through the registered hook,
        /// which rewrites the envelope's `from` field to match
        /// `OperationContext.agent_id`. Reading the canonical stream
        /// returns the stamped envelope, not the LLM-authored one.
        /// Validates dispatch_native_pre_with_replacement is wired
        /// through sys_write_with_link_depth's EXECUTE phase.
        #[test]
        fn mailbox_stamping_hook_rewrites_envelope_through_link_path() {
            use kernel::kernel::OperationContext;

            let kernel = Arc::new(Kernel::new());
            mount_proc(&kernel);
            let svc = install_managed_agent(&kernel);
            let resp = svc.start_session(req("scode-standard")).unwrap();

            let shortcut = format!("{}chat-with-me", &resp.workspace_path);
            let canonical = format!("/proc/{}/chat-with-me", resp.session_id);
            // LLM-authored envelope claims to be from "scode-standard"
            // but the real caller is human-ethan; the hook should
            // rewrite the `from` field.
            let llm_authored =
                br#"{"from":"scode-standard","to":"human-ethan","body":"hi"}"#.to_vec();

            let ctx = OperationContext {
                user_id: "ethan".into(),
                zone_id: "root".into(),
                is_admin: false,
                agent_id: Some("human-ethan".into()),
                is_system: false,
                groups: vec![],
                admin_capabilities: vec![],
                subject_type: "user".into(),
                subject_id: None,
                request_id: "req-stamp".into(),
                context_zone_id: None,
            };

            kernel
                .sys_write(&shortcut, &ctx, &llm_authored, 0)
                .expect("sys_write through workspace shortcut DT_LINK");

            let read = kernel
                .sys_read(&canonical, &ctx, 0, 0)
                .expect("sys_read on canonical chat-with-me");
            let bytes = read.data.expect("stream data present");
            let json: serde_json::Value =
                serde_json::from_slice(&bytes).expect("envelope is valid JSON");
            assert_eq!(
                json.get("from").and_then(|v| v.as_str()),
                Some("human-ethan"),
                "MailboxStampingHook should overwrite from-field with caller agent_id",
            );
        }

        /// Companion structural assertion — keeps the metastore-level
        /// invariant explicit even if the e2e write/read above is ever
        /// skipped on a CI matrix that can't satisfy the route().
        #[test]
        fn workspace_shortcut_link_targets_canonical_chat_with_me_stream() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let shortcut = format!("{}chat-with-me", &resp.workspace_path);
            let canonical = format!("/proc/{}/chat-with-me", resp.session_id);

            // Workspace shortcut is a DT_LINK whose target is the
            // canonical path.
            let shortcut_meta = kernel
                .metastore_get(&shortcut)
                .ok()
                .flatten()
                .expect("workspace shortcut entry present");
            assert_eq!(shortcut_meta.entry_type, DT_LINK);
            assert_eq!(shortcut_meta.link_target.as_deref(), Some(canonical.as_str()));

            // Canonical path holds the DT_STREAM the link points at.
            let canonical_meta = kernel
                .metastore_get(&canonical)
                .ok()
                .flatten()
                .expect("canonical chat-with-me entry present");
            assert_eq!(canonical_meta.entry_type, DT_STREAM);
        }

        #[test]
        fn cancel_session_with_observer_reaps_descriptor_and_subtree() {
            // With an installed observer, cancel(Session) ends with
            // both the descriptor reaped (orphan auto-reap inside
            // AgentRegistry::kill) and the procfs subtree dropped
            // (on_terminate observer).
            let kernel = Arc::new(Kernel::new());
            let svc = install_managed_agent(&kernel);
            let mut r = req("scode-standard");
            r.repos = vec![WorkspaceRepo {
                host_path: "/host/core".into(),
                alias: "core".into(),
            }];
            let resp = svc.start_session(r).unwrap();
            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();
            assert!(!dir_exists(&kernel, &resp.workspace_path));
            let alias_path = format!("{}core", &resp.workspace_path);
            assert!(!entry_exists(&kernel, &alias_path));
            assert!(kernel.agent_registry().get(&resp.session_id).is_none());
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
        }
    }

    /// End-to-end coverage of the `ManagedAgentService → sudo_code::
    /// spawn_task` wiring landed in PR-C: a `start_session` call
    /// allocates a pid, spawns the managed-agent loop, and a write to
    /// `/proc/{pid}/chat-with-me` round-trips through the spawned
    /// thread to a stamped echo response on the same DT_STREAM.
    /// Cancellation aborts the loop and reaps the sidecar entry.
    ///
    /// Today's loop body is the v0 echo stub (see
    /// `sudo_code::spawn_task` docs); when the v1 LLM body lands
    /// these tests grow to cover real `run_turn` driving without
    /// changing the wiring layer this PR adds.
    mod spawn_task_wiring {
        use super::*;
        use kernel::kernel::{Kernel, OperationContext};
        use std::time::{Duration, Instant};

        const POLL_TIMEOUT: Duration = Duration::from_secs(2);

        fn mount_proc(kernel: &Kernel) {
            kernel
                .vfs_router_arc()
                .add_mount("/proc", "root", None, false);
        }

        fn user_ctx() -> OperationContext {
            OperationContext::new(
                "ethan",
                "root",
                /* is_admin */ false,
                Some("human-ethan"),
                /* is_system */ false,
            )
        }

        fn read_envelopes(
            kernel: &Kernel,
            path: &str,
            ctx: &OperationContext,
            from_offset: u64,
        ) -> (Vec<serde_json::Value>, u64) {
            let mut offset = from_offset;
            let mut out = Vec::new();
            loop {
                match kernel.sys_read(path, ctx, 0, offset) {
                    Ok(result) => {
                        if let Some(bytes) = result.data.as_ref() {
                            if !bytes.is_empty() {
                                if let Ok(v) =
                                    serde_json::from_slice::<serde_json::Value>(bytes)
                                {
                                    out.push(v);
                                }
                            }
                        }
                        let next = result.stream_next_offset.map_or(offset, |o| o as u64);
                        if next == offset {
                            break;
                        }
                        offset = next;
                    }
                    Err(_) => break,
                }
            }
            (out, offset)
        }

        fn wait_for_envelope(
            kernel: &Kernel,
            path: &str,
            ctx: &OperationContext,
            body_eq: &str,
            timeout: Duration,
        ) -> Option<serde_json::Value> {
            let deadline = Instant::now() + timeout;
            let mut offset = 0u64;
            while Instant::now() < deadline {
                let (envelopes, next) = read_envelopes(kernel, path, ctx, offset);
                offset = next;
                for env in envelopes {
                    if env.get("body").and_then(|v| v.as_str()) == Some(body_eq) {
                        return Some(env);
                    }
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            None
        }

        /// Full prompt → response round trip through the per-pid
        /// chat-with-me DT_STREAM: user writes a prompt envelope,
        /// MailboxStampingHook stamps the `from` field with the
        /// caller's agent_id, the spawned sudo-code loop reads it,
        /// writes back an echo envelope, and the user reads the
        /// response from the same stream. Validates the ManagedAgent
        /// → sudo_code wiring (Cargo edge + `start_session` spawn +
        /// per-pid `OperationContext`) without depending on any LLM
        /// wiring (v0 stub body).
        #[test]
        fn prompt_round_trips_through_spawned_loop() {
            let kernel = Arc::new(Kernel::new());
            mount_proc(&kernel);
            let svc = ManagedAgentService::install_returning(&kernel)
                .expect("install ManagedAgentService");
            let resp = svc.start_session(req("scode-standard")).unwrap();

            let cwm = format!("/proc/{}/chat-with-me", resp.session_id);
            let ctx = user_ctx();
            let prompt = serde_json::json!({
                "to": "scode-standard",
                "body": "hello",
            });
            kernel
                .sys_write(&cwm, &ctx, &serde_json::to_vec(&prompt).unwrap(), 0)
                .expect("user write to chat-with-me");

            let echo = wait_for_envelope(&kernel, &cwm, &ctx, "echo: hello", POLL_TIMEOUT)
                .expect("agent echo did not arrive within timeout");
            assert_eq!(
                echo.get("from").and_then(|v| v.as_str()),
                Some("scode-standard"),
                "echo response should carry the agent's stamped from-field",
            );
            assert_eq!(
                echo.get("to").and_then(|v| v.as_str()),
                Some("human-ethan"),
                "echo response should be addressed back to the original sender",
            );

            // Clean shutdown — drains the sidecar entry, drops the
            // procfs subtree, reaps the descriptor.
            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();
        }

        /// `cancel(Session)` aborts the spawned loop and drops its
        /// sidecar table entry. Procfs teardown + descriptor reap are
        /// covered by the sibling `procfs::cancel_session_with_observer_…`
        /// suite; this test focuses on the new spawn-handle bookkeeping
        /// PR-C added.
        #[test]
        fn cancel_session_aborts_spawned_loop_and_drains_sidecar() {
            let kernel = Arc::new(Kernel::new());
            mount_proc(&kernel);
            let svc = ManagedAgentService::install_returning(&kernel)
                .expect("install ManagedAgentService");
            let resp = svc.start_session(req("scode-standard")).unwrap();

            assert_eq!(
                svc.spawn_handles.len(),
                1,
                "start_session should plant exactly one spawn handle",
            );

            svc.cancel(&resp.session_id, CancelMode::Session).unwrap();

            assert_eq!(
                svc.spawn_handles.len(),
                0,
                "cancel(Session) should drain the sidecar table",
            );
            assert!(kernel.agent_registry().get(&resp.session_id).is_none());
        }

        /// `cancel(Turn)` aborts the worker via the same
        /// `HookAbortSignal` the v1 runtime will use, but leaves the
        /// AgentRegistry record + procfs subtree intact (the v0 loop
        /// body has no per-turn boundary, so the worker thread
        /// terminates today — but the surrounding ManagedAgent state
        /// is unchanged, which is the well-formed behaviour the v1
        /// run_turn body keeps when it lands).
        #[test]
        fn cancel_turn_aborts_loop_but_keeps_session_alive() {
            let kernel = Arc::new(Kernel::new());
            mount_proc(&kernel);
            let svc = ManagedAgentService::install_returning(&kernel)
                .expect("install ManagedAgentService");
            let resp = svc.start_session(req("scode-standard")).unwrap();

            svc.cancel(&resp.session_id, CancelMode::Turn).unwrap();

            // Sidecar entry stays so the gRPC surface can still
            // address the pid; AgentRegistry record stays;
            // procfs subtree stays; chat-with-me read still works.
            assert!(kernel.agent_registry().get(&resp.session_id).is_some());
            let cwm = format!("/proc/{}/chat-with-me", resp.session_id);
            let ctx = user_ctx();
            assert!(kernel.sys_read(&cwm, &ctx, 0, 0).is_ok());
        }
    }
}
