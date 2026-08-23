//! Co-host adapter: bridge the nexus `SpawnTask` DI seam to the real
//! `sudocode` managed-agent runtime.
//!
//! This is the ONLY `nexus → sudocode` edge, and it lives at a binary LEAF
//! (`nexusd-cluster`, a `[[bin]]` nothing imports), so there is no crate
//! cycle: `sudocode_runtime → kernel`; `nexusd → {sudocode_*, services,
//! nexus-cluster}`; `kernel` / `services` stay sudocode-free (they know
//! only the `SpawnTask` DI trait). Compiled only under the
//! `cohost-sudocode` feature — the default slim cluster build never links
//! sudocode.
//!
//! The adapter is deliberately THIN. All agent construction (the LLM
//! `api_client`, the VFS-backed `tool_executor`, the system prompt, the
//! permission policy) is the SSOT of `sudocode`'s
//! `tools::managed_agent::spawn_managed_agent` factory; the adapter only
//! (1) forwards the kernel + descriptor into that factory, (2) maps
//! sudocode's `AgentLoopState` onto the services-tier enum on each
//! transition, and (3) wraps the returned handle so the service's
//! `on_terminate` observer can abort the loop. Evolving the agent means
//! changing the sudocode factory internals; this adapter's shape never
//! moves.

use std::sync::Arc;

use kernel::core::agents::registry::{AgentDescriptor, AgentKind};
use kernel::kernel::Kernel;
use managed_agent::{
    AgentLoopState as ServiceLoopState, SpawnHandle as ServiceSpawnHandle, SpawnTask,
};
use sudocode_runtime::spawn_task::{
    AgentLoopState as SudoLoopState, Mailbox, SpawnHandle as SudoSpawnHandle,
};

/// The concrete `SpawnTask<Kernel>` provider wired at the `nexusd-cluster`
/// binary edge via `install_managed_agent_with_spawn`. Monomorphises
/// `spawn_managed_agent::<Kernel>` internally, so the per-`sys_read` path
/// carries no vtable cost — only the once-per-`start_session` `spawn` call
/// is `dyn`-dispatched.
pub struct SudoCodeSpawnAdapter;

impl SpawnTask<Kernel> for SudoCodeSpawnAdapter {
    fn spawn(
        &self,
        kernel: Arc<Kernel>,
        desc: AgentDescriptor,
        state_observer: Arc<dyn Fn(ServiceLoopState) + Send + Sync>,
    ) -> Box<dyn ServiceSpawnHandle> {
        // The services-tier observer expects the services enum; sudocode's
        // factory emits the runtime enum. Map on every transition.
        // The co-host agent's mailbox is its persistent, cross-machine A2A
        // inbox `/agents/<name>/chat-with-me` (raft-replicated when federated),
        // so a duet partner on another host addresses it by name.
        let mailbox = Mailbox::A2aInbox {
            base: "/agents".to_string(),
            self_name: desc.name.clone(),
        };
        let handle: SudoSpawnHandle = sudocode_tools::managed_agent::spawn_managed_agent(
            kernel,
            desc,
            mailbox,
            move |sc_state| {
                state_observer(map_loop_state(sc_state));
            },
        );
        Box::new(SudoCodeSpawnHandle { inner: handle })
    }
}

/// Map sudocode's runtime loop-state onto the services-tier mirror. The
/// two enums are intentionally identical (same variants) — services owns
/// its copy to keep the DI trait runtime-agnostic; this is the single
/// translation point.
fn map_loop_state(state: SudoLoopState) -> ServiceLoopState {
    match state {
        SudoLoopState::WarmingUp => ServiceLoopState::WarmingUp,
        SudoLoopState::Ready => ServiceLoopState::Ready,
        SudoLoopState::Busy => ServiceLoopState::Busy,
    }
}

/// Wraps the sudocode `SpawnHandle` so the services tier sees only the
/// abort capability its `on_terminate` observer needs. `abort` signals the
/// runtime loop's shared `HookAbortSignal`; the worker thread observes it
/// and exits on its next poll (idempotent — the observer may fire
/// concurrently with an in-flight `cancel(Session)`).
struct SudoCodeSpawnHandle {
    inner: SudoSpawnHandle,
}

impl ServiceSpawnHandle for SudoCodeSpawnHandle {
    fn abort(&self) {
        self.inner.abort_signal.abort();
    }
}

// ── A2A co-host boot spawn ──────────────────────────────────────────────
//
// The production path for the Win↔macOS duet: co-host a sudocode agent bound
// to its REPLICATED A2A inbox `/agents/<name>/chat-with-me` (not the
// node-local `/proc` stream), so two co-hosted agents on different nodes
// converse over A2A with no bridge/relay. Config-driven at boot via env:
//
//   NEXUS_COHOST_AGENT=win-ai            # the A2A agent to co-host here
//   NEXUS_COHOST_MODEL=claude-haiku-4-5  # optional (defaults to haiku)
//
// The agent MUST already be minted (its replicated `/agents/<name>/`
// mailbox present) before this fires — mint once, then boot. Distinct from
// `SudoCodeSpawnAdapter` above (the node-local sudowork/hydra `start_session`
// gRPC path on `/proc/{pid}`): this is the boot-time cross-machine path.

/// Install a boot-time A2A co-host loop from env config. No-op when
/// `NEXUS_COHOST_AGENT` is unset (e.g. a founder that only relays). Wired as
/// a `ServiceDecl` so it gets the booted `Arc<Kernel>`.
pub fn install_a2a_cohost(kernel: &Arc<Kernel>) -> Result<(), String> {
    let Ok(agent) = std::env::var("NEXUS_COHOST_AGENT") else {
        return Ok(());
    };
    let agent = agent.trim().to_string();
    if agent.is_empty() {
        return Ok(());
    }
    let model = std::env::var("NEXUS_COHOST_MODEL")
        .ok()
        .filter(|m| !m.trim().is_empty())
        .unwrap_or_else(|| "claude-haiku-4-5".to_string());

    let mut desc = AgentDescriptor {
        pid: format!("cohost-{agent}"),
        name: agent.clone(),
        kind: AgentKind::Managed,
        owner_id: "root".to_string(),
        zone_id: "root".to_string(),
        ..Default::default()
    };
    desc.labels.insert("model".to_string(), model.clone());

    // Read its OWN replicated inbox; reply to each SENDER's inbox — raft
    // replication carries the reply to the peer's node.
    let mailbox = Mailbox::A2aInbox {
        base: "/agents".to_string(),
        self_name: agent.clone(),
    };
    eprintln!(
        "[cohost] co-hosting A2A agent {agent} (model {model}) on /agents/{agent}/chat-with-me"
    );

    let agent_for_cb = agent.clone();
    let _handle = sudocode_tools::managed_agent::spawn_managed_agent(
        Arc::clone(kernel),
        desc,
        mailbox,
        move |state| eprintln!("[cohost] agent {agent_for_cb} loop state: {state:?}"),
    );
    // `_handle` is intentionally dropped: dropping detaches the worker
    // thread (its `JoinHandle`) and does NOT signal abort, so the co-host
    // loop runs for the daemon's lifetime.
    Ok(())
}

/// `ServiceDecl` for the boot-time A2A co-host spawn (no-op unless
/// `NEXUS_COHOST_AGENT` is set).
pub fn service_decl() -> kernel::kernel::ServiceDecl {
    kernel::kernel::ServiceDecl {
        name: "cohost_a2a".to_string(),
        install: Box::new(install_a2a_cohost),
    }
}
