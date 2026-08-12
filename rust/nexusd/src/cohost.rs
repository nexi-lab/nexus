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

use kernel::core::agents::registry::AgentDescriptor;
use kernel::kernel::Kernel;
use services::managed_agent::{
    AgentLoopState as ServiceLoopState, SpawnHandle as ServiceSpawnHandle, SpawnTask,
};
use sudocode_runtime::spawn_task::{
    AgentLoopState as SudoLoopState, SpawnHandle as SudoSpawnHandle,
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
        let handle: SudoSpawnHandle =
            sudocode_tools::managed_agent::spawn_managed_agent(kernel, desc, move |sc_state| {
                state_observer(map_loop_state(sc_state));
            });
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
