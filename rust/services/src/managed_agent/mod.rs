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
//!   * On `install`, register `WorkspaceBoundaryHook` into the kernel's
//!     `KernelDispatch` so every cross-owner `/proc/{pid}/workspace/`
//!     write is rejected. The mailbox `from`-stamp hook now lives in the
//!     `a2a` messaging substrate (nexus-vfs), armed once at cluster boot
//!     — every `*/chat-with-me` write is stamped there, for all writers.
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

use serde::{Deserialize, Serialize};

use kernel::core::agents::registry::{
    AgentDescriptor, AgentKind, AgentRegistry, AgentState, RepoMount,
};
use kernel::kernel::syscall::KernelSyscall;
use kernel::service_registry::{RustCallError, RustService};

// The raw ACP-subprocess control-plane spawner (spawn + memory-DT_STREAM
// byte tunnel). Kernel-concrete (drives Kernel-inherent stream ops), so
// it's injected at install and reached through `dyn RawSpawn`; unix +
// `subprocess-host` only. Slim / non-unix builds leave `raw_spawn = None`
// and reject `spawn_spec` at runtime with InvalidArgument.
#[cfg(all(unix, feature = "subprocess-host"))]
pub(crate) mod raw_spawn;

pub(crate) mod proc_entry;
pub(crate) mod session;
pub(crate) mod workspace_boundary_hook;

use proc_entry::{register_proc_entry, unregister_proc_entry};

/// Install ManagedAgentService on `kernel` with an injected
/// [`SpawnTask`] provider. This is the entry the binary edge
/// (`profiles/cluster` binary,
/// `profiles/cluster` for the cluster binary) calls with a
/// concrete adapter that wraps a runtime crate (e.g.
/// `sudocode_runtime::spawn_task`).
///
/// Pure-Rust slim builds without a runtime body call
/// [`install_managed_agent`] (no spawn provider) instead.
pub fn install_managed_agent_with_spawn(
    kernel: &Arc<kernel::kernel::Kernel>,
    spawn_provider: Arc<dyn SpawnTask<kernel::kernel::Kernel>>,
) -> Result<(), String> {
    ManagedAgentService::<kernel::kernel::Kernel>::install_with_spawn(kernel, spawn_provider)
}

/// Install ManagedAgentService on `kernel` without a runtime body
/// (procfs + AgentRegistry only) — for callers that do not ship a
/// runtime spawn provider.
pub fn install_managed_agent(kernel: &Arc<kernel::kernel::Kernel>) -> Result<(), String> {
    ManagedAgentService::<kernel::kernel::Kernel>::install(kernel)
}

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
    /// Embedder-computed raw spawn spec (ACP control-plane contract, 2026-08-01). When
    /// set, `start_session` spawns THIS subprocess (stdio surfaced as DT_PIPEs at
    /// `/{zone}/proc/{pid}/fd/{0,1,2}`) instead of resolving the `agent_id` profile — the
    /// caller (sudowork/hydra) owns ALL launch logic (auth-mode/env/args/token/model);
    /// nexus only executes + supervises + exposes the fd pipes for the client's ACP tunnel.
    #[serde(default)]
    pub spawn_spec: Option<SpawnSpec>,
}

/// Raw subprocess spec computed by the embedder. The launch-logic SSOT stays client-side
/// (e.g. sudowork's `acpConnectors.ts`); nexus executes `cmd`+`args` with `env` in `cwd`,
/// supervises the child, and surfaces its stdio as fd DT_PIPEs. nexus never parses ACP.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub(crate) struct SpawnSpec {
    pub cmd: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: std::collections::HashMap<String, String>,
    #[serde(default)]
    pub cwd: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct StartSessionResponse {
    /// AgentRegistry pid for the spawned managed agent.  cancel /
    /// get_session take this back.
    pub session_id: String,
    pub workspace_path: String,
    /// Real OS pid of the spawned subprocess — set ONLY on the raw ACP
    /// control-plane path (`spawn_spec` supplied); `None` for the
    /// runtime-body / procfs-only paths. Surfaced SEPARATELY from
    /// `session_id` (which stays the synthetic AgentRegistry pid) per
    /// the frozen contract ④: the embedder (sudowork) keys its
    /// pid-bound auth-proxy on the OS pid, but the durable session
    /// handle for cancel / get_session is `session_id`.
    #[serde(default)]
    pub os_pid: Option<u32>,
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

// ── Spawn-task DI surface ──────────────────────────────────────────────
//
// Services rlib does NOT depend on the sudocode crate (cross-repo
// git-deps would couple services' build to a specific sudocode rev,
// which is the wrong layer for cross-repo coupling — same reason
// `KernelSyscall` lives at the trait boundary). Instead, services
// declares a small DI trait that nexus's binary edge
// (`profiles/cluster` for all builds — the sole binary edge
// Python wheel) implements by wrapping `sudocode_runtime::spawn_task`.
// The trait method is `dyn`-dispatched but only fires once per
// `start_session` call (out of the hot path); the spawn body itself
// is fully monomorphised over `K: KernelSyscall` inside the concrete
// impl, so there's no per-`sys_read` vtable cost.

/// Per-pid spawn handle returned by [`SpawnTask::spawn`]. Concrete
/// impls (e.g. the sudocode-runtime adapter) hold whatever
/// in-process state the spawn body needs (worker thread join handle,
/// abort signal, future tokio task handle). The services-tier sees
/// only the abort capability — that's what the on_terminate observer
/// needs to tear the loop down on session termination.
pub trait SpawnHandle: Send + Sync {
    /// Signal the spawn body to stop. Implementations MUST be
    /// idempotent — the on_terminate observer can fire concurrently
    /// with an in-progress `cancel(Session)`.
    fn abort(&self);
}

/// Lifecycle-state notifications a [`SpawnTask`] body emits as the
/// runtime loop progresses. Service-side mirror of the runtime
/// crate's equivalent enum (today: `sudocode_runtime::spawn_task::
/// AgentLoopState`); the binary-edge adapter maps the runtime enum
/// to this one on the way through the [`SpawnTask::spawn`]
/// `state_observer`.
///
/// Services owns this enum (not the runtime crate) because state
/// transition semantics are a managed-agent concern — keeping the
/// trait surface runtime-agnostic preserves the services rlib's
/// no-cross-repo-runtime-dep boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentLoopState {
    /// Runtime is initialising (loading prompt, system setup); maps
    /// to [`AgentState::WarmingUp`].
    WarmingUp,
    /// Runtime is parked waiting for input on the mailbox; maps to
    /// [`AgentState::Ready`].
    Ready,
    /// Runtime is mid-turn (LLM streaming, tool execution); maps to
    /// [`AgentState::Busy`].
    Busy,
}

/// Spawn-task provider. `start_session` calls
/// [`Self::spawn`] after `register_proc_entry` succeeds to kick off
/// the per-pid runtime body. The concrete impl wraps whatever
/// runtime crate the deployment chose (today: sudocode); generic K
/// keeps the spawn body monomorphised against the same kernel
/// concrete that the service holds.
pub trait SpawnTask<K: KernelSyscall>: Send + Sync + 'static {
    /// Spawn the per-pid runtime body. Returns an opaque handle the
    /// service stores in its `spawn_handles` sidecar; the
    /// on_terminate observer aborts via the handle on session
    /// termination.
    ///
    /// `state_observer` is the SSOT writer for the session's
    /// [`AgentState`]. The closure is constructed by
    /// `ManagedAgentService::start_session` — it captures the
    /// service's `Arc<AgentRegistry>` and the session's pid, maps
    /// the runtime-side [`AgentLoopState`] onto [`AgentState`], and
    /// calls `AgentRegistry::update_state`. The spawn body's only
    /// role w.r.t. state is to invoke the observer on each
    /// transition; it MUST NOT write to AgentRegistry through any
    /// other path.
    fn spawn(
        &self,
        kernel: Arc<K>,
        desc: AgentDescriptor,
        state_observer: Arc<dyn Fn(AgentLoopState) + Send + Sync>,
    ) -> Box<dyn SpawnHandle>;
}

/// Raw ACP-subprocess control-plane spawner — the DI seam for the
/// `spawn_spec` path (frozen contract 2026-08-01). Distinct from
/// [`SpawnTask`] (the in-process LLM runtime body): this launches an
/// external subprocess and exposes its stdio as a byte tunnel that the
/// CLIENT drives, whereas `SpawnTask` runs the agent loop in-process.
///
/// The concrete impl ([`raw_spawn::KernelRawSpawn`]) drives Kernel-inherent
/// stream ops that aren't on the `KernelSyscall` trait, so it's Kernel-
/// concrete and injected at install (Kernel-specific); this trait erases
/// `K` so the generic `start_session` can call it. `None` in slim /
/// non-unix builds ⇒ `spawn_spec` is rejected with InvalidArgument.
pub(crate) trait RawSpawn: Send + Sync {
    /// Launch `spec`, wire its stdio to node-local memory DT_STREAMs at
    /// `/proc/{pid}/fd/{0,1,2}`, start the pumps + supervisor, and store
    /// an abort handle in the shared `spawn_handles` (keyed by `pid`, so
    /// the `on_terminate` observer tears it down). Returns the real OS pid
    /// (contract ④). `Err` on bad spec / launch failure — the impl rolls
    /// back the half-planted session.
    fn spawn(&self, pid: &str, spec: SpawnSpec) -> Result<Option<u32>, String>;
}

// ── Service ─────────────────────────────────────────────────────────────

pub(crate) struct ManagedAgentService<K: KernelSyscall> {
    /// Shared kernel handle for `start_session` to stamp the per-pid
    /// procfs subtree (`/proc/{pid}/`, `/proc/{pid}/workspace/`,
    /// workspace shortcut DT_LINK, per-repo alias DT_LINKs) and for
    /// the on_terminate observer to tear it down.
    kernel: Arc<K>,
    agent_registry: Arc<AgentRegistry>,
    /// Optional per-pid spawn provider. `None` for slim deployments
    /// that ship managed-agent without a runtime body (procfs +
    /// AgentRegistry only); `Some` when `install_with_spawn` injects
    /// a concrete provider (production: the binary edge wraps
    /// `sudocode_runtime::spawn_task`). `start_session` calls
    /// `provider.spawn(...)` after `register_proc_entry` and stores
    /// the returned handle in [`Self::spawn_handles`].
    spawn_provider: Option<Arc<dyn SpawnTask<K>>>,
    /// Per-pid spawn handles populated by `start_session` (both the
    /// `SpawnTask` runtime path and the `RawSpawn` tunnel path). The
    /// on_terminate observer (registered in `install_returning`) removes
    /// and aborts the handle on session termination so the worker /
    /// supervisor leaves cleanly.
    spawn_handles: Arc<dashmap::DashMap<String, Box<dyn SpawnHandle>>>,
    /// Optional raw-subprocess control-plane spawner for the `spawn_spec`
    /// path. `Some` only when `install_returning` wired the Kernel-concrete
    /// [`raw_spawn::KernelRawSpawn`] (unix + `subprocess-host`); `None`
    /// elsewhere, in which case a `spawn_spec` request is rejected.
    raw_spawn: Option<Arc<dyn RawSpawn>>,
}

impl<K: KernelSyscall> ManagedAgentService<K> {
    pub(crate) const NAME: &'static str = "managed_agent";

    /// Service constructor.  Production callers reach this through
    /// [`ManagedAgentService::<Kernel>::install`] against the boot-time
    /// `Arc<Kernel>`; tests instantiate directly with a `Kernel::new()`
    /// (cheap in-memory construction) so the per-pid procfs entries
    /// land in the same metastore the assertion helpers read back.
    pub(crate) fn new(kernel: Arc<K>, agent_registry: Arc<AgentRegistry>) -> Self {
        Self {
            kernel,
            agent_registry,
            spawn_provider: None,
            spawn_handles: Arc::new(dashmap::DashMap::new()),
            raw_spawn: None,
        }
    }

    /// Constructor variant that injects a [`SpawnTask`] provider.
    /// The binary edge (`profiles/cluster`) calls
    /// this with a concrete adapter (e.g. sudocode-runtime
    /// `spawn_task` wrapper) so `start_session` actually kicks off a
    /// runtime body. Pure-Rust slim builds without a runtime body
    /// use [`Self::new`] which leaves the provider unset.
    pub(crate) fn with_spawn(
        kernel: Arc<K>,
        agent_registry: Arc<AgentRegistry>,
        spawn_provider: Arc<dyn SpawnTask<K>>,
    ) -> Self {
        Self {
            kernel,
            agent_registry,
            spawn_provider: Some(spawn_provider),
            spawn_handles: Arc::new(dashmap::DashMap::new()),
            raw_spawn: None,
        }
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
        mut req: StartSessionRequest,
    ) -> Result<StartSessionResponse, ManagedAgentError> {
        if req.agent_id.is_empty() {
            return Err(ManagedAgentError::InvalidArgument(
                "'agent_id' is required".into(),
            ));
        }
        // Take the raw spawn spec out early — it selects the spawn
        // strategy below (raw ACP subprocess vs. runtime body) and the
        // rest of the descriptor build doesn't touch it.
        let spawn_spec = req.spawn_spec.take();
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
        // Two mutually-exclusive spawn strategies select on the request
        // shape (a session is one or the other), both fired right after
        // procfs is stamped so the first sys_read on
        // `/proc/{pid}/chat-with-me` (or `/proc/{pid}/fd/*`) routes:
        //
        //   * `spawn_spec` present → the RAW ACP subprocess control-plane
        //     path (frozen contract 2026-08-01). The injected `raw_spawn`
        //     spawner launches the embedder-computed subprocess and pumps
        //     its stdio through node-local memory DT_STREAMs at
        //     `/proc/{pid}/fd/{0,1,2}`; the CLIENT (sudowork) drives it
        //     over that raw-byte tunnel — nexus never frames or parses
        //     ACP. Returns the real OS pid (contract ④).
        //
        //   * else + `spawn_provider` → the managed LLM runtime body
        //     (sudocode today) nexus runs IN-PROCESS, driven by the
        //     mailbox. The DI trait `SpawnTask<K>` keeps services rlib
        //     free of a hard dep on the runtime crate; the concrete
        //     adapter lives at the binary edge (`profiles/cluster`) and
        //     monomorphises `spawn_task::<K>` internally — no
        //     per-`sys_read` vtable cost.
        //
        // Slim builds with neither run procfs-only.
        let mut os_pid: Option<u32> = None;
        if let Some(desc) = self.agent_registry.get(&pid) {
            if let Err(e) = register_proc_entry(self.kernel.as_ref(), &desc) {
                tracing::warn!(pid=%pid, error=%e, "register_proc_entry failed");
            }
            if let Some(spec) = spawn_spec {
                match self.raw_spawn.as_ref() {
                    Some(rs) => {
                        os_pid = rs.spawn(&pid, spec).map_err(ManagedAgentError::Internal)?;
                    }
                    None => {
                        // No raw spawner wired (slim / non-unix). Roll back
                        // the half-planted session so the caller sees a
                        // clean failure rather than a zombie record.
                        let _ = self.agent_registry.kill(&pid, 127);
                        return Err(ManagedAgentError::InvalidArgument(
                            "spawn_spec (raw subprocess host) requires a unix build with the \
                             subprocess-host feature"
                                .into(),
                        ));
                    }
                }
            } else if let Some(provider) = self.spawn_provider.as_ref() {
                // Construct the SSOT state observer. AgentRegistry is
                // the single writer of AgentState in the runtime path
                // (see kernel::core::agents::registry::update_state +
                // can_transition_to FSM); the spawn body calls this
                // closure on each AgentLoopState transition and the
                // closure forwards the update through update_state.
                // InvalidTransition is logged rather than panicked so
                // an FSM bug in the runtime body surfaces as a warning
                // instead of taking down the worker thread.
                let registry = Arc::clone(&self.agent_registry);
                let pid_for_observer = pid.clone();
                let observer: Arc<dyn Fn(AgentLoopState) + Send + Sync> =
                    Arc::new(move |loop_state: AgentLoopState| {
                        let target = match loop_state {
                            AgentLoopState::WarmingUp => AgentState::WarmingUp,
                            AgentLoopState::Ready => AgentState::Ready,
                            AgentLoopState::Busy => AgentState::Busy,
                        };
                        if let Err(e) = registry.update_state(&pid_for_observer, target) {
                            tracing::warn!(
                                pid = %pid_for_observer,
                                state = ?target,
                                error = %e,
                                "AgentRegistry.update_state rejected runtime-side transition",
                            );
                        }
                    });
                let handle = provider.spawn(Arc::clone(&self.kernel), desc, observer);
                self.spawn_handles.insert(pid.clone(), handle);
            }
        }

        Ok(StartSessionResponse {
            session_id: pid,
            workspace_path,
            os_pid,
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
                // No state transition; the runtime watches a
                // separate signal. Today this is a no-op at the
                // kernel layer — the runtime crate will plug in once
                // it lands.
                Ok(CancelResponse { cancelled: true })
            }
            CancelMode::Session => {
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

// Production install path stays specific to the concrete `Kernel`
// because `register_on_terminate` is an inherent accessor on
// `AgentRegistry` reached through `kernel.agent_registry()` — that
// accessor is deliberately *not* on the `KernelSyscall` trait (the v2
// audit pulled kernel-internal struct accessors out of the
// service-facing surface). Lifecycle methods stay generic above so
// non-Kernel `K: KernelSyscall` test fixtures and future runtime targets
// (sudo-code in-process spawn) compile without `Kernel`.
impl ManagedAgentService<kernel::kernel::Kernel> {
    /// Install the service into a freshly-constructed kernel:
    ///
    ///   1. Register the chat-with-me + workspace-boundary hooks into
    ///      the kernel's `KernelDispatch`.
    ///   2. Enlist the service into `ServiceRegistry` so future tonic
    ///      gRPC handlers + Python factory wiring can resolve it via
    ///      `service_registry.lookup_rust(NAME)`.
    ///
    /// Called from `Kernel::new()`. The service holds an
    /// `Arc<AgentRegistry>` — the same `Arc` `Kernel` keeps for
    /// `AgentStatusResolver` reads — so `start_session` mutates the
    /// same SSOT every other agent surface reads from.
    pub(crate) fn install(kernel: &Arc<kernel::kernel::Kernel>) -> Result<(), String> {
        Self::install_returning(kernel, None).map(|_| ())
    }

    /// Install variant that injects a [`SpawnTask`] provider. Used
    /// by the binary edge (`profiles/cluster`) to
    /// wire the sudocode-runtime adapter — the actual managed-agent
    /// runtime body. Slim builds without a runtime body call
    /// [`Self::install`] which leaves `spawn_provider = None` and
    /// ships procfs + AgentRegistry only.
    pub(crate) fn install_with_spawn(
        kernel: &Arc<kernel::kernel::Kernel>,
        spawn_provider: Arc<dyn SpawnTask<kernel::kernel::Kernel>>,
    ) -> Result<(), String> {
        Self::install_returning(kernel, Some(spawn_provider)).map(|_| ())
    }

    /// Install variant that returns the wired service handle so tests
    /// can assert the on_terminate observer behaves correctly without
    /// having to fish the service back out of the kernel registry.
    pub(crate) fn install_returning(
        kernel: &Arc<kernel::kernel::Kernel>,
        spawn_provider: Option<Arc<dyn SpawnTask<kernel::kernel::Kernel>>>,
    ) -> Result<Arc<Self>, String> {
        // Mount the /proc namespace this service stamps into. VFSRouter
        // routes by mount-point lookup, so `sys_unlink` on
        // `/proc/{pid}/...` paths needs the mount entry to exist
        // (`route()` returns NotMounted otherwise and unlink no-ops).
        // No backing store / per-mount metastore — `metastore=None`
        // means dirent reads/writes fall through to the global
        // metastore (matches the procfs-virtualised semantics this
        // mount represents). Idempotent re-call: VFSRouter::add_mount
        // ignores duplicates.
        kernel
            .vfs_router_arc()
            .add_mount("/proc", "root", None, false);

        // Holding `Arc<Kernel>` inside the service does create a
        // Kernel ↔ Service Arc cycle, but services live for process
        // lifetime — same convention AcpService follows. The procfs
        // dirent stamp in `start_session` and the on_terminate
        // teardown both need the owned Arc.
        //
        // Built as a struct literal (not `new`/`with_spawn`) so the raw
        // control-plane spawner can share the SAME `agent_registry` +
        // `spawn_handles` the service and its on_terminate observer use.
        let agent_registry = Arc::clone(kernel.agent_registry());
        let spawn_handles: Arc<dashmap::DashMap<String, Box<dyn SpawnHandle>>> =
            Arc::new(dashmap::DashMap::new());

        // Raw ACP subprocess control-plane spawner (memory-DT_STREAM byte
        // tunnel). Kernel-concrete — built here because it drives
        // Kernel-inherent stream ops off the service surface. Unix +
        // `subprocess-host` only; `None` elsewhere ⇒ `spawn_spec` rejected.
        #[cfg(all(unix, feature = "subprocess-host"))]
        let raw_spawn: Option<Arc<dyn RawSpawn>> = Some(Arc::new(raw_spawn::KernelRawSpawn::new(
            Arc::clone(kernel),
            Arc::clone(&agent_registry),
            Arc::clone(&spawn_handles),
        )));
        #[cfg(not(all(unix, feature = "subprocess-host")))]
        let raw_spawn: Option<Arc<dyn RawSpawn>> = None;

        let svc = Arc::new(ManagedAgentService {
            kernel: Arc::clone(kernel),
            agent_registry,
            spawn_provider,
            spawn_handles,
            raw_spawn,
        });

        // Tear down the per-pid procfs subtree on out-of-band
        // termination — SIGKILL, orphan auto-reap, any path that flips
        // an agent to Terminated without going through
        // `cancel_session(Session)`. `fire_on_terminate` runs before
        // `AgentRegistry::reap` on the orphan path, so the descriptor
        // is still reachable here and we can use its `repos` to drop
        // the per-alias DT_LINK rows alongside the dirents. The
        // descriptor itself is reaped by AgentRegistry after the
        // observer returns, so subsequent `get_session` returns
        // `UnknownSession`.
        //
        // The spawn-handles sidecar lives on the service itself, so
        // the observer captures `Arc::clone(&svc.spawn_handles)`.
        // Removing the handle and aborting it is best-effort — the
        // worker thread also exits naturally when its sys_read on
        // the now-missing chat-with-me path returns FileNotFound,
        // but the explicit abort gives a clean exit without an
        // error-path walk.
        let kernel_for_cb = Arc::clone(kernel);
        let registry_for_cb = Arc::clone(kernel.agent_registry());
        let spawn_handles_for_cb = Arc::clone(&svc.spawn_handles);
        kernel.agent_registry().register_on_terminate(
            Self::NAME,
            Arc::new(move |pid: &str| {
                if let Some((_, handle)) = spawn_handles_for_cb.remove(pid) {
                    handle.abort();
                }
                if let Some(desc) = registry_for_cb.get(pid) {
                    unregister_proc_entry(kernel_for_cb.as_ref(), &desc);
                }
            }),
        );

        let svc_for_return = Arc::clone(&svc);
        kernel.register_rust_service(Self::NAME, svc as Arc<dyn RustService>, Vec::new())?;

        // Register the hook the service owns via the enforced ownership
        // surface — the handle binds it to this service's
        // `ServiceRegistry` entry so
        // `Kernel::unregister_service("managed_agent")` / `swap_managed_service`
        // batch-remove it alongside the service instance.  Hooks are
        // stateless so ordering (enlist-then-hook) has no correctness
        // dependency; the two-step flow matches the pattern
        // `services::audit::install_root` established: enlist the
        // owning entity first, then plug hooks in through the handle.
        //
        // The mailbox `from`-stamp hook is NOT registered here anymore:
        // it belongs to the `a2a` messaging substrate (nexus-vfs) and is
        // armed once at cluster boot, so the guarantee holds for every
        // writer — not only agents spawned through this service.
        let handle = kernel
            .service_handle(Self::NAME)
            .expect("just enlisted managed_agent above; handle must exist");
        kernel.register_service_hook(
            &handle,
            Box::new(workspace_boundary_hook::WorkspaceBoundaryHook::new()),
        );

        Ok(svc_for_return)
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

impl<K: KernelSyscall> RustService for ManagedAgentService<K> {
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

    use kernel::kernel::Kernel;

    /// Build a `ManagedAgentService` with a real `Kernel::new()`.  The
    /// returned tuple shares the AgentRegistry between caller and
    /// service so tests can read the descriptor table without going
    /// through the service's read accessors.
    fn fresh_service() -> (Arc<Kernel>, Arc<AgentRegistry>, ManagedAgentService<Kernel>) {
        let kernel = Arc::new(Kernel::new());
        let registry = Arc::clone(kernel.agent_registry());
        let svc = ManagedAgentService::<Kernel>::new(Arc::clone(&kernel), Arc::clone(&registry));
        (kernel, registry, svc)
    }

    fn req(agent_id: &str) -> StartSessionRequest {
        StartSessionRequest {
            agent_id: agent_id.to_string(),
            repos: Vec::new(),
            model: "claude-sonnet-4-6".to_string(),
            owner_id: "ethan".to_string(),
            zone_id: "root".to_string(),
            spawn_spec: None,
        }
    }

    /// Mock [`SpawnTask`] that invokes the injected `state_observer`
    /// with a scripted WARMING_UP → READY → BUSY transition sequence,
    /// then returns a no-op handle. Used by the
    /// `state_observer_drives_agent_registry_through_loop_states`
    /// test to verify the service-constructed observer closure is the
    /// SSOT writer of AgentState.
    struct ScriptedSpawn;
    struct NoopHandle;
    impl SpawnHandle for NoopHandle {
        fn abort(&self) {}
    }
    impl SpawnTask<Kernel> for ScriptedSpawn {
        fn spawn(
            &self,
            _kernel: Arc<Kernel>,
            _desc: AgentDescriptor,
            state_observer: Arc<dyn Fn(AgentLoopState) + Send + Sync>,
        ) -> Box<dyn SpawnHandle> {
            state_observer(AgentLoopState::WarmingUp);
            state_observer(AgentLoopState::Ready);
            state_observer(AgentLoopState::Busy);
            Box::new(NoopHandle)
        }
    }

    #[test]
    fn service_has_canonical_name() {
        let (_kernel, _table, svc) = fresh_service();
        assert_eq!(svc.name(), "managed_agent");
        assert_eq!(ManagedAgentService::<Kernel>::NAME, "managed_agent");
    }

    #[test]
    fn state_observer_drives_agent_registry_through_loop_states() {
        let kernel = Arc::new(Kernel::new());
        let registry = Arc::clone(kernel.agent_registry());
        let svc = ManagedAgentService::<Kernel>::with_spawn(
            Arc::clone(&kernel),
            Arc::clone(&registry),
            Arc::new(ScriptedSpawn),
        );

        let resp = svc.start_session(req("scode-standard")).unwrap();
        let desc = registry
            .get(&resp.session_id)
            .expect("AgentRegistry record present");
        // The scripted observer fired WARMING_UP → READY → BUSY; final
        // state in the SSOT is BUSY. (start_session already moves
        // REGISTERED → WARMING_UP before spawn, so a re-fired
        // WARMING_UP from the observer is a no-op via the from==new
        // shortcut in update_state.)
        assert_eq!(desc.state, AgentState::Busy);
    }

    #[test]
    fn lifecycle_methods_succeed_on_empty_service() {
        let (_kernel, _table, svc) = fresh_service();
        svc.start().unwrap();
        svc.stop().unwrap();
    }

    #[test]
    fn start_session_returns_identity_tuple_and_plants_agent_registry_record() {
        let (_kernel, table, svc) = fresh_service();
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
        let (_kernel, _table, svc) = fresh_service();
        let err = svc.start_session(req("")).unwrap_err();
        assert!(matches!(err, ManagedAgentError::InvalidArgument(_)));
    }

    #[test]
    fn start_session_defaults_owner_and_zone() {
        let (_kernel, _table, svc) = fresh_service();
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
        let (_kernel, table, svc) = fresh_service();
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
        let (_kernel, table, svc) = fresh_service();
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
        let (_kernel, _table, svc) = fresh_service();
        let err = svc.cancel("pid-bogus", CancelMode::Session).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn get_session_returns_state_from_agent_registry() {
        let (_kernel, _table, svc) = fresh_service();
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
        let (_kernel, table, svc) = fresh_service();
        let resp = svc.start_session(req("scode-standard")).unwrap();
        table.unregister(&resp.session_id);
        let err = svc.get_session(&resp.session_id).unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    #[test]
    fn get_session_unknown_session_errors() {
        let (_kernel, _table, svc) = fresh_service();
        let err = svc.get_session("pid-bogus").unwrap_err();
        assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
    }

    // ── dispatch round-trip ─────────────────────────────────────────

    mod dispatch {
        use super::*;
        use serde_json::json;

        #[test]
        fn start_session_v1_round_trip() {
            let (_kernel, _table, svc) = fresh_service();
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
            let (_kernel, _table, svc) = fresh_service();
            let payload = json!({"agent_id": "scode-standard"}).to_string();
            let bytes = svc
                .dispatch("start_session_v1", payload.as_bytes())
                .unwrap();
            let resp: StartSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(resp.session_id.starts_with("pid-"));
        }

        #[test]
        fn cancel_v1_session_round_trip() {
            let (_kernel, _table, svc) = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id, "mode": "session"}).to_string();
            let bytes = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap();
            let cancel: CancelResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(cancel.cancelled);
        }

        #[test]
        fn cancel_v1_turn_round_trip() {
            let (_kernel, _table, svc) = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id, "mode": "turn"}).to_string();
            let bytes = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap();
            let cancel: CancelResponse = serde_json::from_slice(&bytes).unwrap();
            assert!(cancel.cancelled);
        }

        #[test]
        fn cancel_v1_unknown_session_surfaces_invalid_argument() {
            let (_kernel, _table, svc) = fresh_service();
            let payload = json!({"session_id": "pid-bogus", "mode": "session"}).to_string();
            let err = svc.dispatch("cancel_v1", payload.as_bytes()).unwrap_err();
            assert!(matches!(err, RustCallError::InvalidArgument(_)));
        }

        #[test]
        fn get_session_v1_round_trip() {
            let (_kernel, _table, svc) = fresh_service();
            let resp = svc.start_session(req("scode-standard")).unwrap();
            let payload = json!({"session_id": resp.session_id}).to_string();
            let bytes = svc.dispatch("get_session_v1", payload.as_bytes()).unwrap();
            let snap: GetSessionResponse = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(snap.session_id, resp.session_id);
            assert_eq!(snap.state, "warming_up");
        }

        #[test]
        fn unknown_method_returns_not_found() {
            let (_kernel, _table, svc) = fresh_service();
            let err = svc.dispatch("does_not_exist", b"{}").unwrap_err();
            assert!(matches!(err, RustCallError::NotFound));
        }

        #[test]
        fn malformed_payload_surfaces_invalid_argument() {
            let (_kernel, _table, svc) = fresh_service();
            let err = svc
                .dispatch("start_session_v1", b"this is not json")
                .unwrap_err();
            assert!(matches!(err, RustCallError::InvalidArgument(_)));
        }
    }

    /// Procfs lifecycle tests — exercise start_session through a real
    /// `Kernel` and assert the metastore carries the dirents + DT_LINK
    /// rows the integration doc §2.2 promises.
    mod procfs {
        use super::*;
        use kernel::core::agents::registry::AgentSignal;
        use kernel::kernel::Kernel;
        use kernel::ROOT_ZONE_ID;

        const DT_DIR: u8 = 1;
        const DT_STREAM: u8 = 4;
        const DT_LINK: u8 = 6;

        /// True when `path` is present in the metastore as DT_DIR.
        fn dir_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel
                .sys_stat(path, ROOT_ZONE_ID)
                .is_some_and(|e| e.entry_type == DT_DIR)
        }

        /// True when `path` has any metastore entry.
        fn entry_exists(kernel: &Kernel, path: &str) -> bool {
            let path = path.trim_end_matches('/');
            kernel.access(path, ROOT_ZONE_ID)
        }

        /// DT_LINK target string at `path` — None if the entry is
        /// missing or not a DT_LINK.
        fn link_target_at(kernel: &Kernel, path: &str) -> Option<String> {
            kernel
                .sys_stat(path, ROOT_ZONE_ID)
                .filter(|e| e.entry_type == DT_LINK)
                .and_then(|e| e.link_target)
        }

        /// Build a `ManagedAgentService` with a real Kernel inside —
        /// the only setup needed is `Kernel::new`.
        fn svc_with_kernel() -> (Arc<Kernel>, ManagedAgentService<Kernel>) {
            let k = Arc::new(Kernel::new());
            let svc = ManagedAgentService::new(Arc::clone(&k), Arc::clone(k.agent_registry()));
            (k, svc)
        }

        fn install_managed_agent(kernel: &Arc<Kernel>) -> Arc<ManagedAgentService<Kernel>> {
            ManagedAgentService::install_returning(kernel, None)
                .expect("install ManagedAgentService")
        }

        #[test]
        fn start_session_stamps_workspace_dirent_and_chat_with_me_link() {
            let (kernel, svc) = svc_with_kernel();
            let resp = svc.start_session(req("scode-standard")).unwrap();

            assert!(dir_exists(&kernel, &resp.workspace_path));
            let cwm = format!("{}chat-with-me", resp.workspace_path);
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

            for (alias, expected) in [
                ("myrepo", "/host/repos/myrepo"),
                ("another", "/host/repos/another"),
            ] {
                let alias_path = format!("{}{alias}", resp.workspace_path);
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
            let cwm = format!("{}chat-with-me", resp.workspace_path);
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
            let alias_path = format!("{}core", resp.workspace_path);
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
            let cwm = format!("{}chat-with-me", resp.workspace_path);
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

            let shortcut = format!("{}chat-with-me", resp.workspace_path);
            let canonical = format!("/proc/{}/chat-with-me", resp.session_id);
            // Pre-stamp the envelope's `from` field with the caller's
            // agent_id so MailboxStampingHook's rewrite is a no-op for
            // this test — keeps the assertion focused on "bytes
            // followed the DT_LINK to the canonical stream" without
            // coupling to the stamping policy.  The MailboxStamping
            // e2e companion exercises the rewrite path explicitly.
            let payload = br#"{"from":"scode-standard","to":"human-ethan","body":"ping"}"#;

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
                zone_perms: vec![],
                propagates_cross_node: false,
            };

            // Use UFCS through KernelSyscall so we get the single-path
            // trait wrappers (sys_read_single / sys_write_with_link_depth)
            // — the inherent Kernel::sys_read/sys_write are now batch-shaped
            // (&[ReadRequest] / &[WriteRequest]).
            KernelSyscall::sys_write(kernel.as_ref(), &shortcut, &ctx, payload, 0)
                .expect("sys_write through workspace shortcut DT_LINK");

            let read = KernelSyscall::sys_read(
                kernel.as_ref(),
                &canonical,
                &ctx,
                /* timeout_ms */ 0,
                0,
            )
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
            // The stamp hook now lives in the a2a substrate (armed at
            // cluster boot in production). Arm it here so this test still
            // exercises managed_agent's DT_LINK shortcut routing THROUGH
            // the stamp hook end-to-end.
            let a2a_handle = kernel
                .enlist_hook_only_service("a2a")
                .expect("enlist a2a hook-only service");
            kernel.register_service_hook(&a2a_handle, Box::new(a2a::MailboxStampingHook::new()));
            let resp = svc.start_session(req("scode-standard")).unwrap();

            let shortcut = format!("{}chat-with-me", resp.workspace_path);
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
                zone_perms: vec![],
                propagates_cross_node: false,
            };

            KernelSyscall::sys_write(kernel.as_ref(), &shortcut, &ctx, &llm_authored, 0)
                .expect("sys_write through workspace shortcut DT_LINK");

            let read = KernelSyscall::sys_read(kernel.as_ref(), &canonical, &ctx, 0, 0)
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
            let shortcut = format!("{}chat-with-me", resp.workspace_path);
            let canonical = format!("/proc/{}/chat-with-me", resp.session_id);

            // Workspace shortcut is a DT_LINK whose target is the
            // canonical path.
            let shortcut_meta = kernel
                .sys_stat(&shortcut, ROOT_ZONE_ID)
                .expect("workspace shortcut entry present");
            assert_eq!(shortcut_meta.entry_type, DT_LINK);
            assert_eq!(
                shortcut_meta.link_target.as_deref(),
                Some(canonical.as_str())
            );

            // Canonical path holds the DT_STREAM the link points at.
            let canonical_meta = kernel
                .sys_stat(&canonical, ROOT_ZONE_ID)
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
            let alias_path = format!("{}core", resp.workspace_path);
            assert!(!entry_exists(&kernel, &alias_path));
            assert!(kernel.agent_registry().get(&resp.session_id).is_none());
            let err = svc.get_session(&resp.session_id).unwrap_err();
            assert!(matches!(err, ManagedAgentError::UnknownSession(_)));
        }
    }

    /// Raw ACP-subprocess control-plane path (#36 slice 1, frozen
    /// contract 2026-08-01). Proves the raw-byte tunnel end-to-end and
    /// the ③ exit / reap semantics. Unix + subprocess-host only, because
    /// `HostedSubprocess` is unix-only.
    #[cfg(all(unix, feature = "subprocess-host"))]
    mod raw_spawn {
        use super::*;
        use kernel::kernel::{Kernel, OperationContext};
        use std::time::Duration;

        fn cat_on_path() -> bool {
            std::process::Command::new("cat")
                .arg("--version")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        }

        fn sys_ctx() -> OperationContext {
            OperationContext::new("system", "root", true, None, true)
        }

        /// A spawn spec that inherits the runner's PATH so a bare command
        /// name (`cat`, `sh`) resolves. The raw path runs `spec.env`
        /// VERBATIM under `env_clear()` (the client owns ALL launch
        /// logic — contract SSOT), so PATH must be supplied explicitly:
        /// with an empty env the exec would fail ENOENT. This mirrors
        /// what sudowork's `acpConnectors.ts` must include per interface
        /// spec.
        fn spec(cmd: &str, args: &[&str]) -> SpawnSpec {
            SpawnSpec {
                cmd: cmd.to_string(),
                args: args.iter().map(|s| s.to_string()).collect(),
                env: std::collections::HashMap::from([(
                    "PATH".to_string(),
                    std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_string()),
                )]),
                cwd: std::env::temp_dir().to_string_lossy().into_owned(),
            }
        }

        /// LIVE tunnel smoke: `start_session(spawn_spec=cat)` spawns
        /// `cat` and wires its stdio as fd DT_PIPEs; we drive the
        /// RAW-BYTE tunnel over VFS — write `/proc/{pid}/fd/0`, read the
        /// echo back from `/proc/{pid}/fd/1` — then `cancel(Session)` and
        /// assert the supervisor reaped the session. Confirms nexus is a
        /// pure process host + byte pipe (it never parses ACP).
        ///
        /// `#[ignore]`: needs `cat` on PATH + a live multi-thread
        /// runtime. Run on a unix box:
        ///   cargo test -p services \
        ///     --features "service-managed-agent service-acp" \
        ///     managed_agent::tests::raw_spawn -- --ignored --nocapture
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        #[ignore]
        async fn cat_tunnel_roundtrip_through_start_session() {
            if !cat_on_path() {
                eprintln!("cat not on PATH — skipping raw_spawn tunnel smoke");
                return;
            }
            let kernel = Arc::new(Kernel::new());
            let svc = ManagedAgentService::install_returning(&kernel, None)
                .expect("install ManagedAgentService");

            let start_req = StartSessionRequest {
                agent_id: "acp-cat".to_string(),
                spawn_spec: Some(spec("cat", &[])),
                ..Default::default()
            };

            // start_session block_on's the async spawn internally, so it
            // must run on a blocking thread — the gRPC handler does the
            // same via spawn_blocking.
            let svc_for_start = Arc::clone(&svc);
            let resp = tokio::task::spawn_blocking(move || svc_for_start.start_session(start_req))
                .await
                .expect("join start_session")
                .expect("start_session ok");

            assert!(
                resp.os_pid.is_some(),
                "raw spawn must surface the real OS pid (contract ④)",
            );
            let pid = resp.session_id.clone();
            let fd0 = format!("/proc/{pid}/fd/0");
            let fd1 = format!("/proc/{pid}/fd/1");

            // Write into the subprocess stdin over the tunnel — exactly as
            // the client does: append to the stdin stream.
            kernel
                .stream_write_nowait(&fd0, b"hello tunnel\n", &sys_ctx())
                .expect("append to stdin stream");

            // Read the echo back over the tunnel exactly as the client
            // does: stream_read_at_blocking(offset) → (data, next_offset);
            // Err (WouldBlock) = timeout, keep polling. Accumulate until the
            // line shows up.
            let read_kernel = Arc::clone(&kernel);
            let fd1_for_read = fd1.clone();
            let echoed = tokio::task::spawn_blocking(move || {
                let mut acc = Vec::new();
                let mut offset = 0usize;
                for _ in 0..50 {
                    // Err = timeout with no new bytes — keep polling.
                    if let Ok((data, next)) =
                        read_kernel.stream_read_at_blocking(&fd1_for_read, offset, 200)
                    {
                        acc.extend_from_slice(&data);
                        offset = next;
                        if acc
                            .windows(b"hello tunnel".len())
                            .any(|w| w == b"hello tunnel")
                        {
                            break;
                        }
                    }
                }
                acc
            })
            .await
            .expect("join tunnel read");

            let text = String::from_utf8_lossy(&echoed);
            assert!(
                text.contains("hello tunnel"),
                "tunnel should echo the bytes written to fd/0; got {text:?}",
            );

            // Terminate the session — the supervisor kills cat, collapses
            // the tunnel, and reaps the descriptor.
            let svc_for_cancel = Arc::clone(&svc);
            let pid_for_cancel = pid.clone();
            tokio::task::spawn_blocking(move || {
                svc_for_cancel.cancel(&pid_for_cancel, CancelMode::Session)
            })
            .await
            .expect("join cancel")
            .expect("cancel ok");

            // Give the detached supervisor a beat to finish teardown.
            tokio::time::sleep(Duration::from_millis(300)).await;
            assert!(
                kernel.agent_registry().get(&pid).is_none(),
                "session must be reaped after cancel(Session)",
            );
        }

        /// ③ exit-event path: a subprocess that exits ON ITS OWN (no
        /// cancel) drives the supervisor's `wait()` arm, which reaps the
        /// session. Proves nexus notices process death and tears the
        /// session down without a client cancel.
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        #[ignore]
        async fn self_exit_reaps_session() {
            if !cat_on_path() {
                eprintln!("shell not available — skipping self_exit smoke");
                return;
            }
            let kernel = Arc::new(Kernel::new());
            let svc = ManagedAgentService::install_returning(&kernel, None)
                .expect("install ManagedAgentService");

            // `sh -c 'exit 7'` terminates immediately with code 7.
            let start_req = StartSessionRequest {
                agent_id: "acp-selfexit".to_string(),
                spawn_spec: Some(spec("sh", &["-c", "exit 7"])),
                ..Default::default()
            };
            let svc_for_start = Arc::clone(&svc);
            let resp = tokio::task::spawn_blocking(move || svc_for_start.start_session(start_req))
                .await
                .expect("join start_session")
                .expect("start_session ok");
            let pid = resp.session_id.clone();

            // Poll for the supervisor to observe exit + reap (bounded).
            let mut reaped = false;
            for _ in 0..50 {
                if kernel.agent_registry().get(&pid).is_none() {
                    reaped = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            assert!(
                reaped,
                "supervisor must reap the session when the subprocess self-exits",
            );
        }

        /// Spawn-failure path: an un-resolvable command fails the launch;
        /// `start_session` surfaces a hard error and the half-planted
        /// session is rolled back (registry.kill on the error path), so
        /// no zombie descriptor leaks.
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        #[ignore]
        async fn spawn_failure_surfaces_error_and_reaps() {
            let kernel = Arc::new(Kernel::new());
            let svc = ManagedAgentService::install_returning(&kernel, None)
                .expect("install ManagedAgentService");

            let before = kernel.agent_registry().count();
            let start_req = StartSessionRequest {
                agent_id: "acp-nope".to_string(),
                spawn_spec: Some(spec("nexus-no-such-binary-xyz", &[])),
                ..Default::default()
            };
            let svc_for_start = Arc::clone(&svc);
            let err = tokio::task::spawn_blocking(move || svc_for_start.start_session(start_req))
                .await
                .expect("join start_session")
                .expect_err("spawn of a missing binary must fail");
            assert!(
                matches!(err, ManagedAgentError::Internal(_)),
                "expected Internal spawn error, got {err:?}",
            );
            // No net descriptor growth — the error path reaped the pid.
            let after = kernel.agent_registry().count();
            assert_eq!(before, after, "spawn-failure must not leak a descriptor");
        }

        /// Tunnel READ contract + regression guard for the fd-DT_PIPE cut
        /// this replaced (whose non-blocking read treated "no data yet" as
        /// EOF and permanently killed the tunnel on the first idle poll):
        ///   * `Ok((data, next))` → RAW bytes (no framing, no newline
        ///     injection) → feed the reader, advance `offset`.
        ///   * `Err` (WouldBlock timeout) → no bytes yet → keep polling;
        ///     the stream STAYS OPEN across idle polls.
        ///   * after reap → the stdout stream is closed + drained → read
        ///     errors = disconnect (the eof signal sudowork keys on).
        #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
        #[ignore]
        async fn tunnel_is_raw_byte_and_survives_idle_polls() {
            if !cat_on_path() {
                eprintln!("cat not on PATH — skipping tunnel raw-byte smoke");
                return;
            }
            let kernel = Arc::new(Kernel::new());
            let svc = ManagedAgentService::install_returning(&kernel, None)
                .expect("install ManagedAgentService");
            let start_req = StartSessionRequest {
                agent_id: "acp-frame".to_string(),
                spawn_spec: Some(spec("cat", &[])),
                ..Default::default()
            };
            let svc_for_start = Arc::clone(&svc);
            let resp = tokio::task::spawn_blocking(move || svc_for_start.start_session(start_req))
                .await
                .expect("join")
                .expect("start_session ok");
            let pid = resp.session_id.clone();
            let fd0 = format!("/proc/{pid}/fd/0");
            let fd1 = format!("/proc/{pid}/fd/1");

            // Read raw bytes from fd/1 starting at `offset`, accumulating
            // across timeouts until at least `want` bytes arrive.
            async fn read_until(
                k: Arc<Kernel>,
                path: String,
                start: usize,
                want: usize,
            ) -> (Vec<u8>, usize) {
                tokio::task::spawn_blocking(move || {
                    let mut acc = Vec::new();
                    let mut offset = start;
                    for _ in 0..40 {
                        if let Ok((data, next)) = k.stream_read_at_blocking(&path, offset, 200) {
                            acc.extend_from_slice(&data);
                            offset = next;
                            if acc.len() >= want {
                                break;
                            }
                        }
                    }
                    (acc, offset)
                })
                .await
                .unwrap()
            }

            // 1. A payload written WITHOUT a trailing newline comes back
            //    byte-identical — no line-framing, no `\n` injection.
            kernel
                .stream_write_nowait(&fd0, b"chunk-a", &sys_ctx())
                .expect("append chunk-a");
            let (got_a, mut offset) =
                read_until(Arc::clone(&kernel), fd1.clone(), 0, b"chunk-a".len()).await;
            assert_eq!(
                got_a,
                b"chunk-a".to_vec(),
                "raw passthrough — no framing / no newline injection",
            );

            // 2. Idle polls while cat is alive with no pending data. Each
            //    returns Err(WouldBlock); the stream MUST stay open (the
            //    fd-pipe cut permanently closed here — regression guard).
            for _ in 0..3 {
                let k = Arc::clone(&kernel);
                let p = fd1.clone();
                let o = offset;
                let r = tokio::task::spawn_blocking(move || k.stream_read_at_blocking(&p, o, 100))
                    .await
                    .unwrap();
                assert!(
                    r.is_err(),
                    "idle poll must return Err(WouldBlock), got data"
                );
            }

            // 3. A fresh write STILL flows — proving the stream survived the
            //    idle polls (the fd-pipe bug would have killed it here).
            kernel
                .stream_write_nowait(&fd0, b"chunk-b", &sys_ctx())
                .expect("append chunk-b");
            let (got_b, off_b) =
                read_until(Arc::clone(&kernel), fd1.clone(), offset, b"chunk-b".len()).await;
            offset = off_b;
            assert_eq!(
                got_b,
                b"chunk-b".to_vec(),
                "stream must survive idle polls and keep delivering",
            );

            // 4. Terminate → the supervisor closes the tunnel then reaps.
            //    Once reaped, the drained+closed stdout stream reads as an
            //    error — the disconnect signal.
            let svc_c = Arc::clone(&svc);
            let pid_c = pid.clone();
            tokio::task::spawn_blocking(move || svc_c.cancel(&pid_c, CancelMode::Session))
                .await
                .unwrap()
                .unwrap();
            let mut reaped = false;
            for _ in 0..60 {
                if kernel.agent_registry().get(&pid).is_none() {
                    reaped = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            assert!(reaped, "session must be reaped after cancel(Session)");
            let k = Arc::clone(&kernel);
            let p = fd1.clone();
            let o = offset;
            let post = tokio::task::spawn_blocking(move || k.stream_read_at_blocking(&p, o, 100))
                .await
                .unwrap();
            assert!(
                post.is_err(),
                "reaped session's stdout stream must read as eof/closed (disconnect)",
            );
        }
    }
}
